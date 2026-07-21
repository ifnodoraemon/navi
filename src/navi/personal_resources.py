from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .db import connect
from .paths import db_paths


RESOURCE_KINDS = frozenset(
    {"calendar_event", "reminder", "contact", "mail_draft", "attention_policy"}
)


class PersonalResourceConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PersonalResource:
    id: str
    kind: str
    owner_scope: str
    status: str
    data: dict[str, Any]
    version: int
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PersonalResourceAdapter(Protocol):
    kind: str

    def normalize(self, data: dict[str, Any], *, existing: bool) -> dict[str, Any]: ...


class _CalendarAdapter:
    kind = "calendar_event"

    def normalize(self, data: dict[str, Any], *, existing: bool) -> dict[str, Any]:
        normalized = _allowed(
            data,
            "title",
            "starts_at",
            "ends_at",
            "timezone",
            "location",
            "notes",
            "attendees",
        )
        _required_text(normalized, "title", existing=existing)
        _required_text(normalized, "starts_at", existing=existing)
        _validate_datetime(normalized, "starts_at")
        _validate_datetime(normalized, "ends_at")
        _string_list(normalized, "attendees")
        return normalized


class _ReminderAdapter:
    kind = "reminder"

    def normalize(self, data: dict[str, Any], *, existing: bool) -> dict[str, Any]:
        normalized = _allowed(
            data,
            "title",
            "due_at",
            "timezone",
            "notes",
            "recurrence",
            "priority",
            "completed",
        )
        _required_text(normalized, "title", existing=existing)
        _validate_datetime(normalized, "due_at")
        if "completed" in normalized and not isinstance(normalized["completed"], bool):
            raise ValueError("reminder completed must be boolean")
        return normalized


class _ContactAdapter:
    kind = "contact"

    def normalize(self, data: dict[str, Any], *, existing: bool) -> dict[str, Any]:
        normalized = _allowed(
            data,
            "display_name",
            "emails",
            "phones",
            "organization",
            "notes",
            "tags",
        )
        _required_text(normalized, "display_name", existing=existing)
        for key in ("emails", "phones", "tags"):
            _string_list(normalized, key)
        return normalized


class _MailDraftAdapter:
    kind = "mail_draft"

    def normalize(self, data: dict[str, Any], *, existing: bool) -> dict[str, Any]:
        normalized = _allowed(data, "to", "cc", "bcc", "subject", "body", "reply_to")
        for key in ("to", "cc", "bcc"):
            _string_list(normalized, key)
        if not existing and not normalized.get("to"):
            raise ValueError("mail_draft to requires at least one recipient")
        _required_text(normalized, "subject", existing=existing)
        _required_text(normalized, "body", existing=existing)
        return normalized


class _AttentionPolicyAdapter:
    kind = "attention_policy"

    def normalize(self, data: dict[str, Any], *, existing: bool) -> dict[str, Any]:
        normalized = _allowed(
            data,
            "channel",
            "enabled",
            "quiet_hours_start",
            "quiet_hours_end",
            "timezone",
            "minimum_priority",
            "categories",
        )
        _required_text(normalized, "channel", existing=existing)
        if "enabled" in normalized and not isinstance(normalized["enabled"], bool):
            raise ValueError("attention_policy enabled must be boolean")
        _string_list(normalized, "categories")
        return normalized


class PersonalResourceAdapterRegistry:
    def __init__(self, adapters: tuple[PersonalResourceAdapter, ...] | None = None):
        builtins: tuple[PersonalResourceAdapter, ...] = (
            _CalendarAdapter(),
            _ReminderAdapter(),
            _ContactAdapter(),
            _MailDraftAdapter(),
            _AttentionPolicyAdapter(),
        )
        self._adapters = {adapter.kind: adapter for adapter in (adapters or builtins)}

    def get(self, kind: str) -> PersonalResourceAdapter:
        try:
            return self._adapters[kind]
        except KeyError as exc:
            raise ValueError(f"unsupported personal resource kind: {kind}") from exc

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


class PersonalResourceStore:
    def __init__(
        self,
        home: Path,
        *,
        adapters: PersonalResourceAdapterRegistry | None = None,
    ):
        self.home = home
        self.db_path = db_paths(home).personal_resources
        self.adapters = adapters or PersonalResourceAdapterRegistry()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS personal_resources (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    owner_scope TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_personal_resources_scope "
                "ON personal_resources(owner_scope, kind, status, updated_at)"
            )

    def create(self, *, kind: str, owner_scope: str, data: dict[str, Any]) -> PersonalResource:
        adapter = self.adapters.get(kind)
        normalized = adapter.normalize(dict(data), existing=False)
        now = time.time()
        item = PersonalResource(
            id=uuid.uuid4().hex,
            kind=kind,
            owner_scope=owner_scope,
            status="active",
            data=normalized,
            version=1,
            created_at=now,
            updated_at=now,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO personal_resources(
                    id, kind, owner_scope, status, data_json, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.kind,
                    item.owner_scope,
                    item.status,
                    json.dumps(item.data, ensure_ascii=False, sort_keys=True),
                    item.version,
                    item.created_at,
                    item.updated_at,
                ),
            )
        return item

    def get(self, resource_id: str, *, owner_scopes: set[str]) -> PersonalResource | None:
        if not owner_scopes:
            return None
        placeholders = ", ".join("?" for _ in owner_scopes)
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"""
                SELECT id, kind, owner_scope, status, data_json, version, created_at, updated_at
                FROM personal_resources
                WHERE id = ? AND owner_scope IN ({placeholders})
                """,
                (resource_id, *sorted(owner_scopes)),
            ).fetchone()
        return _resource_from_row(row) if row else None

    def query(
        self,
        *,
        owner_scopes: set[str],
        kinds: tuple[str, ...] = (),
        query: str = "",
        include_deleted: bool = False,
        limit: int = 50,
    ) -> list[PersonalResource]:
        if not owner_scopes:
            return []
        clauses = [f"owner_scope IN ({', '.join('?' for _ in owner_scopes)})"]
        params: list[Any] = sorted(owner_scopes)
        if kinds:
            for kind in kinds:
                self.adapters.get(kind)
            clauses.append(f"kind IN ({', '.join('?' for _ in kinds)})")
            params.extend(kinds)
        if not include_deleted:
            clauses.append("status != 'deleted'")
        if query.strip():
            clauses.append("lower(data_json) LIKE ?")
            params.append(f"%{query.strip().lower()}%")
        params.append(max(1, min(int(limit), 200)))
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, kind, owner_scope, status, data_json, version, created_at, updated_at
                FROM personal_resources WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [_resource_from_row(row) for row in rows]

    def update(
        self,
        resource_id: str,
        *,
        owner_scopes: set[str],
        patch: dict[str, Any],
        expected_version: int,
        status: str = "",
    ) -> PersonalResource:
        current = self.get(resource_id, owner_scopes=owner_scopes)
        if current is None:
            raise KeyError("personal resource not found")
        if expected_version != current.version:
            raise PersonalResourceConflict(
                f"personal resource version changed: expected {expected_version}, current {current.version}"
            )
        normalized_patch = self.adapters.get(current.kind).normalize(dict(patch), existing=True)
        data = {**current.data, **normalized_patch}
        target_status = status or current.status
        if target_status not in {"active", "completed", "deleted"}:
            raise ValueError("personal resource status is invalid")
        now = time.time()
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE personal_resources
                SET data_json = ?, status = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND owner_scope = ? AND version = ?
                """,
                (
                    json.dumps(data, ensure_ascii=False, sort_keys=True),
                    target_status,
                    now,
                    resource_id,
                    current.owner_scope,
                    expected_version,
                ),
            )
        if cursor.rowcount != 1:
            raise PersonalResourceConflict("personal resource was concurrently modified")
        updated = self.get(resource_id, owner_scopes=owner_scopes)
        if updated is None:
            raise RuntimeError("personal resource disappeared after update")
        return updated


def _resource_from_row(row: Any) -> PersonalResource:
    return PersonalResource(
        id=str(row[0]),
        kind=str(row[1]),
        owner_scope=str(row[2]),
        status=str(row[3]),
        data=json.loads(str(row[4])),
        version=int(row[5]),
        created_at=float(row[6]),
        updated_at=float(row[7]),
    )


def _allowed(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    unknown = set(data) - set(keys)
    if unknown:
        raise ValueError(f"unsupported resource fields: {', '.join(sorted(unknown))}")
    return {key: value for key, value in data.items() if value is not None}


def _required_text(data: dict[str, Any], key: str, *, existing: bool) -> None:
    if key not in data and existing:
        return
    if not isinstance(data.get(key), str) or not str(data[key]).strip():
        raise ValueError(f"personal resource {key} is required")
    data[key] = str(data[key]).strip()


def _string_list(data: dict[str, Any], key: str) -> None:
    if key not in data:
        return
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"personal resource {key} must be a string array")
    data[key] = [item.strip() for item in value if item.strip()]


def _validate_datetime(data: dict[str, Any], key: str) -> None:
    if key not in data or data[key] == "":
        return
    value = data[key]
    if not isinstance(value, str):
        raise ValueError(f"personal resource {key} must be an ISO-8601 string")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"personal resource {key} must be an ISO-8601 string") from exc
