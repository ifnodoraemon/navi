"""Connector-neutral durable delivery protocol.

The outbox owns only deterministic transport mechanics: a stable idempotency
key, receipt persistence, bounded retry scheduling, and recovery of an
interrupted worker.  It deliberately does not choose a channel, alter a
payload, or decide whether a user should be notified.  Those remain connector
and model responsibilities respectively.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, fields
from typing import Any, Protocol

from .db import check_schema_version, connect, write_schema_version
from .paths import db_paths
from .schema import Column, Table, assert_schema_exact


DELIVERY_OUTBOX_SCHEMA_VERSION = 1
DEFAULT_MAX_ATTEMPTS = 3
STALE_SENDING_SECONDS = 300.0


@dataclass(frozen=True)
class DeliveryItem:
    """One independently receipted transport operation.

    A response containing text and a file becomes two items in one batch.  A
    failed caption therefore cannot prevent the connector from attempting the
    already-authorized attachment.
    """

    id: str
    batch_id: str
    channel: str
    peer_id: str
    sender_id: str
    trace_id: str
    run_id: str
    goal_id: str
    kind: str
    payload_json: str
    transport_context_json: str
    body: str
    body_provenance: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    error: str
    receipt_json: str
    delivery_id: str
    projected_at: float
    created_at: float
    updated_at: float
    sent_at: float

    @property
    def payload(self) -> dict[str, Any]:
        raw = _json_object(self.payload_json)
        return raw if isinstance(raw, dict) else {}

    @property
    def receipt(self) -> dict[str, Any]:
        raw = _json_object(self.receipt_json)
        return raw if isinstance(raw, dict) else {}

    @property
    def transport_context(self) -> dict[str, Any]:
        raw = _json_object(self.transport_context_json)
        return raw if isinstance(raw, dict) else {}


@dataclass(frozen=True)
class DeliveryEnvelope:
    """A connector-neutral response prepared for durable transport."""

    batch_id: str
    channel: str
    peer_id: str
    sender_id: str = ""
    trace_id: str = ""
    run_id: str = ""
    goal_id: str = ""
    text: str = ""
    body_provenance: str = ""
    file_path: str = ""
    transport_context: dict[str, Any] | None = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    def items(self, *, now: float | None = None) -> tuple[DeliveryItem, ...]:
        current_time = time.time() if now is None else float(now)
        batch_id = _stable_batch_id(self.batch_id)

        def item(*, item_id: str, kind: str, payload_json: str, body: str) -> DeliveryItem:
            return DeliveryItem(
                id=item_id,
                batch_id=batch_id,
                channel=self.channel.strip(),
                peer_id=self.peer_id.strip(),
                sender_id=self.sender_id.strip(),
                trace_id=self.trace_id.strip(),
                run_id=self.run_id.strip(),
                goal_id=self.goal_id.strip(),
                kind=kind,
                payload_json=payload_json,
                transport_context_json=_json_dump(dict(self.transport_context or {})),
                body=body,
                body_provenance=self.body_provenance.strip(),
                status="pending",
                attempts=0,
                max_attempts=max(1, int(self.max_attempts)),
                next_attempt_at=current_time,
                error="",
                receipt_json="{}",
                delivery_id="",
                projected_at=0.0,
                created_at=current_time,
                updated_at=current_time,
                sent_at=0.0,
            )

        items: list[DeliveryItem] = []
        text = self.text.strip()
        if text:
            items.append(
                item(
                    item_id=f"{batch_id}:text",
                    kind="text",
                    payload_json=_json_dump({"text": text}),
                    body=text,
                )
            )
        path = self.file_path.strip()
        if path:
            items.append(
                item(
                    item_id=f"{batch_id}:file",
                    kind="file",
                    payload_json=_json_dump({"path": path}),
                    body="",
                )
            )
        return tuple(items)


@dataclass(frozen=True)
class DeliveryReceipt:
    """Connector receipt facts for one successfully accepted item."""

    transport: str
    media_count: int = 0
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "media_count": max(0, int(self.media_count)),
            "details": dict(self.details or {}),
        }


@dataclass(frozen=True)
class DeliveryFailure:
    """A connector-classified transport failure, not a semantic decision."""

    reason: str
    error: str
    retryable: bool
    retry_after_seconds: float = 0.0
    provider_code: str = ""


@dataclass(frozen=True)
class DeliveryOutcome:
    item: DeliveryItem
    state: str
    receipt: DeliveryReceipt | None = None
    failure: DeliveryFailure | None = None


class DeliveryTransport(Protocol):
    """Connector adapter required by the generic delivery coordinator."""

    channel: str

    async def deliver(self, item: DeliveryItem) -> DeliveryReceipt: ...

    def classify_failure(self, exc: Exception) -> DeliveryFailure: ...


class DeliveryOutboxStore:
    """Durable, idempotent transport queue shared by every connector."""

    def __init__(self, home):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).delivery_outbox
        self._init_db()
        self._import_legacy_goal_outbox()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            check_schema_version(conn, "delivery_outbox", DELIVERY_OUTBOX_SCHEMA_VERSION)
            conn.execute(DELIVERY_OUTBOX_TABLE.ddl)
            assert_schema_exact(conn, DELIVERY_OUTBOX_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_delivery_outbox_ready "
                "ON delivery_outbox(channel, status, next_attempt_at, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_delivery_outbox_batch "
                "ON delivery_outbox(batch_id, status, projected_at)"
            )
            write_schema_version(conn, "delivery_outbox", DELIVERY_OUTBOX_SCHEMA_VERSION)

    def enqueue(self, envelope: DeliveryEnvelope) -> tuple[DeliveryItem, ...]:
        items = envelope.items()
        if not items:
            return ()
        if not envelope.channel.strip() or not envelope.peer_id.strip():
            raise ValueError("delivery requires channel and peer_id")
        with connect(self.db_path) as conn:
            for item in items:
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO delivery_outbox({DELIVERY_OUTBOX_TABLE.select_list})
                    VALUES ({", ".join("?" for _ in fields(DeliveryItem))})
                    """,
                    _item_values(item),
                )
                row = conn.execute(
                    "SELECT batch_id, kind, payload_json FROM delivery_outbox WHERE id = ?",
                    (item.id,),
                ).fetchone()
                if row != (item.batch_id, item.kind, item.payload_json):
                    raise RuntimeError(
                        "delivery idempotency key conflicts with a different payload"
                    )
        return tuple(self.get(item.id) or item for item in items)

    def get(self, item_id: str) -> DeliveryItem | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {DELIVERY_OUTBOX_TABLE.select_list} FROM delivery_outbox WHERE id = ?",
                (item_id,),
            ).fetchone()
        return _item_from_row(row) if row else None

    def latest_for_run(self, run_id: str) -> DeliveryItem | None:
        if not run_id:
            return None
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"""
                SELECT {DELIVERY_OUTBOX_TABLE.select_list}
                FROM delivery_outbox WHERE run_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return _item_from_row(row) if row else None

    def claim_ready(
        self,
        *,
        channel: str,
        limit: int = 10,
        now: float | None = None,
        minimum_priority: int | None = None,
    ) -> list[DeliveryItem]:
        current_time = time.time() if now is None else float(now)
        claimed: list[DeliveryItem] = []
        priority_clause = (
            "AND COALESCE(CAST(json_extract(transport_context_json, '$.priority') "
            "AS INTEGER), 0) >= ?"
            if minimum_priority is not None
            else ""
        )
        params: list[Any] = [channel, current_time]
        if minimum_priority is not None:
            params.append(int(minimum_priority))
        params.append(max(1, int(limit)))
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT {DELIVERY_OUTBOX_TABLE.select_list}
                FROM delivery_outbox
                WHERE channel = ? AND status = 'pending' AND next_attempt_at <= ?
                {priority_clause}
                ORDER BY COALESCE(
                    CAST(json_extract(transport_context_json, '$.priority') AS INTEGER), 0
                ) DESC, created_at ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            for row in rows:
                item = _item_from_row(row)
                cursor = conn.execute(
                    """
                    UPDATE delivery_outbox
                    SET status = 'sending', attempts = attempts + 1, updated_at = ?
                    WHERE id = ? AND status = 'pending' AND next_attempt_at <= ?
                    """,
                    (current_time, item.id, current_time),
                )
                if cursor.rowcount == 1:
                    claimed.append(
                        _replace_item(
                            item,
                            status="sending",
                            attempts=item.attempts + 1,
                            updated_at=current_time,
                        )
                    )
        return claimed

    def mark_sent(
        self,
        item_id: str,
        *,
        receipt: DeliveryReceipt,
        delivery_id: str = "",
        sent_at: float | None = None,
    ) -> DeliveryItem | None:
        current_time = time.time()
        accepted_at = current_time if sent_at is None else float(sent_at)
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE delivery_outbox
                SET status = 'sent', receipt_json = ?, delivery_id = ?, error = '',
                    sent_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'sending')
                """,
                (
                    _json_dump(receipt.to_dict()),
                    delivery_id or item_id,
                    accepted_at,
                    current_time,
                    item_id,
                ),
            )
            if cursor.rowcount != 1:
                return self.get(item_id)
        return self.get(item_id)

    def schedule_retry(
        self,
        item_id: str,
        *,
        error: str,
        retry_after_seconds: float,
        now: float | None = None,
    ) -> DeliveryItem | None:
        current_time = time.time() if now is None else float(now)
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE delivery_outbox
                SET status = 'pending', error = ?, next_attempt_at = ?, updated_at = ?
                WHERE id = ? AND status = 'sending'
                """,
                (
                    error[:1000],
                    current_time + max(0.0, float(retry_after_seconds)),
                    current_time,
                    item_id,
                ),
            )
        return self.get(item_id)

    def mark_failed(self, item_id: str, *, error: str) -> DeliveryItem | None:
        current_time = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE delivery_outbox
                SET status = 'failed', error = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'sending')
                """,
                (error[:1000], current_time, item_id),
            )
        return self.get(item_id)

    def requeue_failed(self, item_id: str, *, now: float | None = None) -> DeliveryItem:
        """Explicitly requeue one failed, non-expired item with the same payload and key."""
        current_time = time.time() if now is None else float(now)
        item = self.get(item_id)
        if item is None:
            raise KeyError(f"delivery item not found: {item_id}")
        if item.status != "failed":
            raise ValueError(f"delivery item is not failed: {item_id} status={item.status}")
        expires_at = _delivery_expires_at(item)
        if expires_at and expires_at <= current_time:
            raise ValueError(f"delivery item has expired and cannot be retried: {item_id}")
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE delivery_outbox
                SET status = 'pending', attempts = 0, next_attempt_at = ?,
                    error = '', updated_at = ?, projected_at = 0
                WHERE id = ? AND status = 'failed'
                """,
                (current_time, current_time, item_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"delivery item changed while requeueing: {item_id}")
        restored = self.get(item_id)
        if restored is None:
            raise RuntimeError(f"delivery item disappeared while requeueing: {item_id}")
        return restored

    def recover_stale_sending(
        self,
        *,
        channel: str,
        stale_after_seconds: float = STALE_SENDING_SECONDS,
        now: float | None = None,
        limit: int = 100,
    ) -> list[DeliveryItem]:
        """Return interrupted idempotent items to the queue without changing their key."""
        current_time = time.time() if now is None else float(now)
        cutoff = current_time - max(1.0, float(stale_after_seconds))
        recovered: list[DeliveryItem] = []
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT {DELIVERY_OUTBOX_TABLE.select_list}
                FROM delivery_outbox
                WHERE channel = ? AND status = 'sending' AND updated_at <= ?
                ORDER BY updated_at ASC LIMIT ?
                """,
                (channel, cutoff, max(1, int(limit))),
            ).fetchall()
            for row in rows:
                item = _item_from_row(row)
                cursor = conn.execute(
                    """
                    UPDATE delivery_outbox
                    SET status = 'pending', error = ?, next_attempt_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'sending' AND updated_at <= ?
                    """,
                    (
                        "connector_delivery_interrupted: retrying with the same idempotency key",
                        current_time,
                        current_time,
                        item.id,
                        cutoff,
                    ),
                )
                if cursor.rowcount == 1:
                    recovered.append(
                        _replace_item(
                            item,
                            status="pending",
                            error="connector_delivery_interrupted: retrying with the same idempotency key",
                            next_attempt_at=current_time,
                            updated_at=current_time,
                        )
                    )
        return recovered

    def complete_unprojected_batches(
        self, *, channel: str, limit: int = 100
    ) -> list[tuple[str, list[DeliveryItem]]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {DELIVERY_OUTBOX_TABLE.select_list}
                FROM delivery_outbox
                WHERE channel = ?
                ORDER BY batch_id ASC, created_at ASC
                """,
                (channel,),
            ).fetchall()
        grouped: dict[str, list[DeliveryItem]] = {}
        for row in rows:
            item = _item_from_row(row)
            grouped.setdefault(item.batch_id, []).append(item)
        completed: list[tuple[str, list[DeliveryItem]]] = []
        for batch_id, items in grouped.items():
            if len(completed) >= max(1, int(limit)):
                break
            if any(item.projected_at > 0 for item in items):
                continue
            if all(item.status == "sent" for item in items):
                completed.append((batch_id, items))
        return completed

    def mark_batch_projected(self, batch_id: str, *, projected_at: float | None = None) -> None:
        current_time = time.time() if projected_at is None else float(projected_at)
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE delivery_outbox SET projected_at = ? WHERE batch_id = ? AND projected_at = 0",
                (current_time, batch_id),
            )

    def list_batch(self, batch_id: str) -> list[DeliveryItem]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {DELIVERY_OUTBOX_TABLE.select_list}
                FROM delivery_outbox WHERE batch_id = ? ORDER BY created_at ASC
                """,
                (batch_id,),
            ).fetchall()
        return [_item_from_row(row) for row in rows]

    def list_items(
        self,
        *,
        channel: str,
        status: str = "",
        limit: int = 50,
    ) -> list[DeliveryItem]:
        clauses = ["channel = ?"]
        params: list[Any] = [channel]
        if status:
            clauses.append("status = ?")
            params.append(status)
        params.append(max(1, int(limit)))
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {DELIVERY_OUTBOX_TABLE.select_list}
                FROM delivery_outbox
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_item_from_row(row) for row in rows]

    def _import_legacy_goal_outbox(self) -> None:
        """Copy historical Goal outbox rows once into the connector-neutral store.

        The source table remains an audit artifact.  New code never reads it,
        so this is a one-way state migration rather than a compatibility path.
        """
        with connect(self.db_path) as conn:
            imported = conn.execute(
                "SELECT 1 FROM schema_versions WHERE component = ?",
                ("delivery_outbox_goal_legacy_import",),
            ).fetchone()
        if imported is not None:
            return
        legacy_path = db_paths(self.home).goals
        if not legacy_path.exists():
            self._mark_legacy_goal_outbox_imported()
            return
        try:
            with connect(legacy_path) as legacy:
                exists = legacy.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'goal_delivery_outbox'"
                ).fetchone()
                if not exists:
                    self._mark_legacy_goal_outbox_imported()
                    return
                rows = legacy.execute(
                    """
                    SELECT id, goal_id, run_id, channel, source, peer_id, sender_id,
                           trace_id, body, body_provenance, status, attempts, error,
                           delivery_id, created_at, updated_at, sent_at
                    FROM goal_delivery_outbox
                    """
                ).fetchall()
        except Exception:
            return
        with connect(self.db_path) as conn:
            for row in rows:
                (
                    legacy_id,
                    goal_id,
                    run_id,
                    channel,
                    _source,
                    peer_id,
                    sender_id,
                    trace_id,
                    body,
                    provenance,
                    status,
                    attempts,
                    error,
                    delivery_id,
                    created_at,
                    updated_at,
                    sent_at,
                ) = row
                migrated_status = "unknown" if status == "sending" else str(status)
                item = DeliveryItem(
                    id=f"legacy:{legacy_id}:text",
                    batch_id=f"legacy:{legacy_id}",
                    channel=str(channel),
                    peer_id=str(peer_id),
                    sender_id=str(sender_id),
                    trace_id=str(trace_id),
                    run_id=str(run_id),
                    goal_id=str(goal_id),
                    kind="text",
                    payload_json=_json_dump({"text": str(body)}),
                    transport_context_json="{}",
                    body=str(body),
                    body_provenance=str(provenance),
                    status=migrated_status,
                    attempts=int(attempts),
                    max_attempts=DEFAULT_MAX_ATTEMPTS,
                    next_attempt_at=float(updated_at),
                    error=(
                        "connector_delivery_outcome_unknown: legacy worker stopped before receipt"
                        if migrated_status == "unknown"
                        else str(error)
                    ),
                    receipt_json="{}",
                    delivery_id=str(delivery_id),
                    projected_at=0.0,
                    created_at=float(created_at),
                    updated_at=float(updated_at),
                    sent_at=float(sent_at),
                )
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO delivery_outbox({DELIVERY_OUTBOX_TABLE.select_list})
                    VALUES ({", ".join("?" for _ in fields(DeliveryItem))})
                    """,
                    _item_values(item),
                )
        self._mark_legacy_goal_outbox_imported()

    def _mark_legacy_goal_outbox_imported(self) -> None:
        with connect(self.db_path) as conn:
            write_schema_version(conn, "delivery_outbox_goal_legacy_import", 1)


class DeliveryCoordinator:
    """Generic delivery worker; adapters only implement transport semantics."""

    def __init__(self, store: DeliveryOutboxStore):
        self.store = store

    async def drain(
        self,
        transport: DeliveryTransport,
        *,
        limit: int = 10,
        minimum_priority: int | None = None,
    ) -> list[DeliveryOutcome]:
        self.store.recover_stale_sending(channel=transport.channel)
        outcomes: list[DeliveryOutcome] = []
        for _ in range(max(1, int(limit))):
            claimed = self.store.claim_ready(
                channel=transport.channel,
                limit=1,
                minimum_priority=minimum_priority,
            )
            if not claimed:
                break
            item = claimed[0]
            expires_at = _delivery_expires_at(item)
            if expires_at and expires_at <= time.time():
                failure = DeliveryFailure(
                    reason="connector_delivery_expired",
                    error=f"delivery deadline {expires_at:g} elapsed before connector receipt",
                    retryable=False,
                )
                error = f"{failure.reason}: {failure.error}"
                stored = self.store.mark_failed(item.id, error=error)
                outcomes.append(
                    DeliveryOutcome(item=stored or item, state="expired", failure=failure)
                )
                continue
            try:
                receipt = await transport.deliver(item)
            except Exception as exc:
                failure = transport.classify_failure(exc)
                error = f"{failure.reason}: {failure.error}"[:1000]
                if failure.retryable and item.attempts < item.max_attempts:
                    stored = self.store.schedule_retry(
                        item.id,
                        error=error,
                        retry_after_seconds=_retry_delay(
                            item.attempts,
                            base_seconds=failure.retry_after_seconds,
                        ),
                    )
                    outcomes.append(
                        DeliveryOutcome(
                            item=stored or item, state="retry_scheduled", failure=failure
                        )
                    )
                    if failure.reason == "connector_rate_limited":
                        break
                else:
                    stored = self.store.mark_failed(item.id, error=error)
                    outcomes.append(
                        DeliveryOutcome(item=stored or item, state="failed", failure=failure)
                    )
                continue
            stored = self.store.mark_sent(item.id, receipt=receipt, delivery_id=item.id)
            outcomes.append(DeliveryOutcome(item=stored or item, state="sent", receipt=receipt))
        return outcomes


def envelope_from_response(
    *,
    channel: str,
    peer_id: str,
    sender_id: str,
    trace_id: str,
    text: str,
    connector_delivery: Any = None,
    run_id: str = "",
    goal_id: str = "",
    body_provenance: str = "",
    transport_context: dict[str, Any] | None = None,
) -> DeliveryEnvelope:
    """Build one durable batch from a normal response or connector file fact."""
    delivery_id = str(getattr(connector_delivery, "delivery_id", "") or "")
    delivery_text = str(getattr(connector_delivery, "text", "") or "")
    delivery_path = str(getattr(connector_delivery, "path", "") or "")
    delivery_run = str(getattr(connector_delivery, "run_id", "") or "")
    delivery_goal = str(getattr(connector_delivery, "goal_id", "") or "")
    seed = delivery_id or trace_id or _response_seed(channel, peer_id, text, delivery_path)
    return DeliveryEnvelope(
        batch_id=seed,
        channel=channel,
        peer_id=peer_id,
        sender_id=sender_id,
        trace_id=trace_id,
        run_id=delivery_run or run_id,
        goal_id=delivery_goal or goal_id,
        text=delivery_text if connector_delivery is not None else text,
        body_provenance=body_provenance,
        file_path=delivery_path,
        transport_context=transport_context,
    )


def _retry_delay(attempts: int, *, base_seconds: float = 0.0) -> float:
    base = max(1.0, float(base_seconds))
    return min(3600.0, base * float(2 ** max(0, int(attempts) - 1)))


def _delivery_expires_at(item: DeliveryItem) -> float:
    try:
        return max(0.0, float(item.transport_context.get("expires_at") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _stable_batch_id(value: str) -> str:
    raw = value.strip()
    if raw:
        return raw[:240]
    return hashlib.sha256(str(time.time_ns()).encode("utf-8")).hexdigest()


def _response_seed(channel: str, peer_id: str, text: str, path: str) -> str:
    payload = "\x1f".join((channel, peer_id, text, path))
    return f"response-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


def _json_dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _item_values(item: DeliveryItem) -> tuple[Any, ...]:
    return tuple(getattr(item, field.name) for field in fields(DeliveryItem))


def _item_from_row(row: tuple[Any, ...]) -> DeliveryItem:
    return DeliveryItem(
        id=str(row[0]),
        batch_id=str(row[1]),
        channel=str(row[2]),
        peer_id=str(row[3]),
        sender_id=str(row[4]),
        trace_id=str(row[5]),
        run_id=str(row[6]),
        goal_id=str(row[7]),
        kind=str(row[8]),
        payload_json=str(row[9]),
        transport_context_json=str(row[10]),
        body=str(row[11]),
        body_provenance=str(row[12]),
        status=str(row[13]),
        attempts=int(row[14]),
        max_attempts=int(row[15]),
        next_attempt_at=float(row[16]),
        error=str(row[17]),
        receipt_json=str(row[18]),
        delivery_id=str(row[19]),
        projected_at=float(row[20]),
        created_at=float(row[21]),
        updated_at=float(row[22]),
        sent_at=float(row[23]),
    )


def _replace_item(item: DeliveryItem, **updates: Any) -> DeliveryItem:
    return DeliveryItem(**{**item.__dict__, **updates})


DELIVERY_OUTBOX_TABLE = Table(
    "delivery_outbox",
    [
        Column("id", "TEXT", primary_key=True),
        Column("batch_id", "TEXT", nullable=False),
        Column("channel", "TEXT", nullable=False),
        Column("peer_id", "TEXT", nullable=False),
        Column("sender_id", "TEXT", nullable=False),
        Column("trace_id", "TEXT", nullable=False),
        Column("run_id", "TEXT", nullable=False),
        Column("goal_id", "TEXT", nullable=False),
        Column("kind", "TEXT", nullable=False),
        Column("payload_json", "TEXT", nullable=False),
        Column("transport_context_json", "TEXT", nullable=False),
        Column("body", "TEXT", nullable=False),
        Column("body_provenance", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("attempts", "INTEGER", nullable=False),
        Column("max_attempts", "INTEGER", nullable=False),
        Column("next_attempt_at", "REAL", nullable=False),
        Column("error", "TEXT", nullable=False),
        Column("receipt_json", "TEXT", nullable=False),
        Column("delivery_id", "TEXT", nullable=False),
        Column("projected_at", "REAL", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
        Column("sent_at", "REAL", nullable=False),
    ],
)
