"""CUBRID 기반 암호화 Provider credential 저장소 (ADR 0013).

``SQLiteCredentialRepository`` 와 동일한 ``CredentialRepository`` Protocol 을 SQLAlchemy
Core 로 구현한다. **ciphertext 만** 저장하며, AES-GCM AAD(``associated_data``)와 owner
검증(``validate_owner_id``)은 store.py 의 단일 함수를 공유한다 — 백엔드가 달라도
암복호 시맨틱이 동일하다.

이 모듈은 ``_credential_repository_from_env`` 의 cubrid 분기에서만 import 된다 —
``sqlalchemy`` 를 import 하므로 기본(sqlite) 경로에 optional 의존성을 끌어들이지 않는다.

동시성: 프로세스 전역 단일 Engine(커넥션 풀 + pool_pre_ping)을 받아 연산마다 짧은
커넥션을 빌린다. put 은 dialect 독립적으로 단일 트랜잭션 내 delete+insert 로 upsert 한다.
credential 쓰기는 사용자 액션이므로 (파생 인덱스와 달리) 예외를 삼키지 않고 전파한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    Column,
    LargeBinary,
    MetaData,
    String,
    Table,
    delete,
    insert,
    select,
)

from .crypto import CredentialCipher
from .models import CredentialMetadata
from .store import _MASK, associated_data, normalize_provider, validate_owner_id

if TYPE_CHECKING:
    from sqlalchemy import Engine


class CubridCredentialRepository:
    """ciphertext 만 CUBRID 에 기록하는 credential repository (ADR 0013)."""

    def __init__(self, engine: Engine, cipher: CredentialCipher) -> None:
        self._engine = engine
        self._cipher = cipher
        self._metadata = MetaData()
        self._table = Table(
            "provider_credentials",
            self._metadata,
            Column("owner_id", String(255), primary_key=True),
            Column("provider", String(64), primary_key=True),
            # CUBRID 는 BLOB 컬럼에 NOT NULL 제약을 허용하지 않는다(errno -1014). 따라서
            # nullable 로 두고, 애플리케이션이 항상 ciphertext 를 기록/검증한다(put 은
            # 비어있지 않은 credential 만 받고 encrypt 결과를 저장, get_secret 은 None 처리).
            Column("ciphertext", LargeBinary),
            Column("updated_at", String(40), nullable=False),
        )
        self._table.create(self._engine, checkfirst=True)

    def get_metadata(self, owner_id: str, provider: str) -> CredentialMetadata:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        stmt = select(self._table.c.updated_at).where(
            self._table.c.owner_id == owner_id, self._table.c.provider == provider
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return CredentialMetadata(provider, False, None, None)
        return CredentialMetadata(provider, True, _MASK, str(row[0]))

    def get_secret(self, owner_id: str, provider: str) -> str | None:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        stmt = select(self._table.c.ciphertext).where(
            self._table.c.owner_id == owner_id, self._table.c.provider == provider
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return None
        return self._cipher.decrypt(
            bytes(row[0]), associated_data=associated_data(owner_id, provider)
        )

    def list_configured_providers(self, owner_id: str) -> Sequence[str]:
        validate_owner_id(owner_id)
        stmt = (
            select(self._table.c.provider)
            .where(self._table.c.owner_id == owner_id)
            .order_by(self._table.c.provider)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return tuple(str(row[0]) for row in rows)

    def put(self, owner_id: str, provider: str, credential: str) -> CredentialMetadata:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        if not credential or not credential.strip():
            raise ValueError("credential must be a non-empty string")
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ciphertext = self._cipher.encrypt(
            credential, associated_data=associated_data(owner_id, provider)
        )
        # 단일 트랜잭션 내 delete+insert — dialect upsert 에 의존하지 않는다.
        with self._engine.begin() as conn:
            conn.execute(
                delete(self._table).where(
                    self._table.c.owner_id == owner_id, self._table.c.provider == provider
                )
            )
            conn.execute(
                insert(self._table).values(
                    owner_id=owner_id,
                    provider=provider,
                    ciphertext=ciphertext,
                    updated_at=updated_at,
                )
            )
        return CredentialMetadata(provider, True, _MASK, updated_at)

    def delete(self, owner_id: str, provider: str) -> bool:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        stmt = delete(self._table).where(
            self._table.c.owner_id == owner_id, self._table.c.provider == provider
        )
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
        return bool(result.rowcount and result.rowcount > 0)


__all__ = ["CubridCredentialRepository"]
