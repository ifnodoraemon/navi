from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .db import connect
from .paths import db_paths


@dataclass(frozen=True, slots=True)
class IdentityLinkResult:
    identity_id: str
    aliases: tuple[dict[str, str], ...]
    migrated_memory_count: int


@dataclass(frozen=True, slots=True)
class IdentityLinkRequest:
    request_id: str
    verification_code: str
    expires_at: float


class IdentityStore:
    """Explicit, hashed cross-surface identity links for memory scoping."""

    def __init__(self, home: Path):
        self.home = home
        self.db_path = db_paths(home).memory
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS identity_aliases (
                    alias_key TEXT PRIMARY KEY,
                    identity_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_identity_aliases_identity "
                "ON identity_aliases(identity_id, source)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS identity_link_requests (
                    request_id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL UNIQUE,
                    current_alias_key TEXT NOT NULL,
                    current_source TEXT NOT NULL,
                    current_actor_scope TEXT NOT NULL,
                    other_alias_key TEXT NOT NULL,
                    other_source TEXT NOT NULL,
                    other_actor_scope TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_identity_link_requests_alias "
                "ON identity_link_requests(other_alias_key, status, expires_at)"
            )

    def resolve(self, *, source: str, peer_id: str, sender_id: str) -> str:
        alias_key = identity_alias_key(source, peer_id, sender_id)
        if not alias_key:
            return ""
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT identity_id FROM identity_aliases WHERE alias_key = ?",
                (alias_key,),
            ).fetchone()
        return str(row[0]) if row else ""

    def aliases(self, identity_id: str) -> tuple[dict[str, str], ...]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT source, alias_key FROM identity_aliases
                WHERE identity_id = ? ORDER BY created_at ASC
                """,
                (identity_id,),
            ).fetchall()
        return tuple(
            {"source": str(source), "alias_fingerprint": str(alias_key)[:12]}
            for source, alias_key in rows
        )

    def request_link(
        self,
        *,
        current_source: str,
        current_peer_id: str,
        current_sender_id: str,
        other_source: str,
        other_peer_id: str,
        other_sender_id: str,
        ttl_seconds: float = 600.0,
    ) -> IdentityLinkRequest:
        current_key = identity_alias_key(
            current_source, current_peer_id, current_sender_id
        )
        other_key = identity_alias_key(other_source, other_peer_id, other_sender_id)
        if not current_key or not other_key:
            raise ValueError("both identity aliases require source, peer_id, and sender_id")
        if current_key == other_key:
            raise ValueError("identity aliases must be different")
        now = time.time()
        request_id = uuid.uuid4().hex
        verification_code = secrets.token_urlsafe(18)
        code_hash = _digest("identity-link", verification_code)
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT alias_key, identity_id FROM identity_aliases WHERE alias_key IN (?, ?)",
                (current_key, other_key),
            ).fetchall()
            identities = {str(row[1]) for row in rows}
            if len(identities) > 1:
                raise ValueError("identity aliases are already linked to different people")
            if len(rows) == 2 and len(identities) == 1:
                raise ValueError("identity aliases are already linked")
            conn.execute(
                """
                UPDATE identity_link_requests
                SET status = 'superseded', updated_at = ?
                WHERE status = 'pending'
                  AND current_alias_key = ? AND other_alias_key = ?
                """,
                (now, current_key, other_key),
            )
            conn.execute(
                """
                INSERT INTO identity_link_requests(
                    request_id, code_hash, current_alias_key, current_source,
                    current_actor_scope, other_alias_key, other_source,
                    other_actor_scope, status, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    request_id,
                    code_hash,
                    current_key,
                    current_source,
                    actor_memory_scope(
                        current_source, current_peer_id, current_sender_id
                    ),
                    other_key,
                    other_source,
                    actor_memory_scope(other_source, other_peer_id, other_sender_id),
                    now + max(60.0, ttl_seconds),
                    now,
                    now,
                ),
            )
        return IdentityLinkRequest(
            request_id=request_id,
            verification_code=verification_code,
            expires_at=now + max(60.0, ttl_seconds),
        )

    def confirm_link(
        self,
        *,
        source: str,
        peer_id: str,
        sender_id: str,
        verification_code: str,
    ) -> IdentityLinkResult:
        alias_key = identity_alias_key(source, peer_id, sender_id)
        if not alias_key or not verification_code:
            raise ValueError("confirmation requires a channel identity and verification code")
        now = time.time()
        code_hash = _digest("identity-link", verification_code)
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT request_id, current_alias_key, current_source,
                       current_actor_scope, other_alias_key, other_source,
                       other_actor_scope, status, expires_at
                FROM identity_link_requests WHERE code_hash = ?
                """,
                (code_hash,),
            ).fetchone()
            if row is None:
                raise ValueError("identity link verification code is invalid")
            if str(row[7]) != "pending":
                raise ValueError("identity link request is no longer pending")
            if float(row[8]) <= now:
                conn.execute(
                    "UPDATE identity_link_requests SET status = 'expired', updated_at = ? "
                    "WHERE request_id = ?",
                    (now, str(row[0])),
                )
                raise ValueError("identity link verification code has expired")
            if alias_key != str(row[4]):
                raise ValueError("identity link must be confirmed from the target channel")

            current_key = str(row[1])
            other_key = str(row[4])
            aliases = conn.execute(
                "SELECT alias_key, identity_id FROM identity_aliases WHERE alias_key IN (?, ?)",
                (current_key, other_key),
            ).fetchall()
            identities = {str(item[1]) for item in aliases}
            if len(identities) > 1:
                raise ValueError("identity aliases are already linked to different people")
            identity_id = next(iter(identities), uuid.uuid4().hex)
            for linked_key, linked_source in (
                (current_key, str(row[2])),
                (other_key, str(row[5])),
            ):
                conn.execute(
                    """
                    INSERT INTO identity_aliases(
                        alias_key, identity_id, source, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(alias_key) DO UPDATE SET updated_at = excluded.updated_at
                    """,
                    (linked_key, identity_id, linked_source, now, now),
                )
            cursor = conn.execute(
                "UPDATE memory_items SET scope = ?, updated_at = ? WHERE scope IN (?, ?)",
                (
                    identity_memory_scope(identity_id),
                    now,
                    str(row[3]),
                    str(row[6]),
                ),
            )
            migrated = int(cursor.rowcount)
            conn.execute(
                "UPDATE identity_link_requests SET status = 'completed', updated_at = ? "
                "WHERE request_id = ?",
                (now, str(row[0])),
            )
        return IdentityLinkResult(
            identity_id=identity_id,
            aliases=self.aliases(identity_id),
            migrated_memory_count=migrated,
        )

    def unlink_current(self, *, source: str, peer_id: str, sender_id: str) -> bool:
        alias_key = identity_alias_key(source, peer_id, sender_id)
        if not alias_key:
            return False
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM identity_aliases WHERE alias_key = ?", (alias_key,)
            )
        return cursor.rowcount == 1


def identity_alias_key(source: str, peer_id: str, sender_id: str) -> str:
    if not (source or peer_id or sender_id):
        return ""
    return _digest("alias", source, peer_id, sender_id)


def actor_memory_scope(source: str, peer_id: str, sender_id: str) -> str:
    return f"actor:{_digest(source, peer_id, sender_id)[:24]}"


def identity_memory_scope(identity_id: str) -> str:
    return f"person:{_digest(identity_id)[:24]}"


def _digest(*parts: str) -> str:
    raw = "\x00".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
