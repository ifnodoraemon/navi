from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .db import check_schema_version, connect, write_schema_version
from .loop_contracts import VaultHandle
from .paths import db_paths
from .schema import Column, Table, assert_schema_exact

VAULT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StoredSecretHandle:
    uri: str
    purpose: str
    env_var: str
    created_at: float
    updated_at: float

    def to_prompt_dict(self) -> dict[str, str]:
        data = {"handle": self.uri, "purpose": self.purpose}
        if self.env_var:
            data["env_var"] = self.env_var
        return data


class VaultStore:
    """Persistent secret store exposed to models only through handles."""

    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).vault
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            check_schema_version(conn, "vault", VAULT_SCHEMA_VERSION)
            conn.execute(VAULT_SECRETS_TABLE.ddl)
            assert_schema_exact(conn, VAULT_SECRETS_TABLE)
            write_schema_version(conn, "vault", VAULT_SCHEMA_VERSION)

    def put(self, handle: VaultHandle, value: str) -> None:
        handle.validate()
        if not value:
            raise ValueError("secret value is required")
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO vault_secrets(uri, purpose, env_var, value, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(uri)
                DO UPDATE SET purpose = excluded.purpose, env_var = excluded.env_var,
                              value = excluded.value, updated_at = excluded.updated_at
                """,
                (handle.uri, handle.purpose, handle.env_var, value, now, now),
            )

    def resolve_env(self, handles: tuple[VaultHandle, ...]) -> tuple[dict[str, str], tuple[str, ...]]:
        env: dict[str, str] = {}
        secret_values: list[str] = []
        with connect(self.db_path) as conn:
            for handle in handles:
                handle.validate()
                row = conn.execute(
                    "SELECT env_var, value FROM vault_secrets WHERE uri = ?",
                    (handle.uri,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"secret handle not found: {handle.uri}")
                env_var = handle.env_var or str(row[0] or "")
                if not env_var:
                    raise ValueError("VaultHandle.env_var is required for command injection")
                value = str(row[1])
                env[env_var] = value
                secret_values.append(value)
        return env, tuple(secret_values)

    def list_handles(self, *, limit: int = 100) -> tuple[StoredSecretHandle, ...]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT uri, purpose, env_var, created_at, updated_at
                FROM vault_secrets
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(StoredSecretHandle(*row) for row in rows)


VAULT_SECRETS_TABLE = Table(
    "vault_secrets",
    [
        Column("uri", "TEXT", primary_key=True),
        Column("purpose", "TEXT", nullable=False),
        Column("env_var", "TEXT", nullable=False),
        Column("value", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
    ],
)
