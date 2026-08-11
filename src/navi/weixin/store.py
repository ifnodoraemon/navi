from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_WEIXIN_BASE_URL

from .models import WeixinAccount

CONTEXT_TOKEN_MAX_AGE_SECONDS = 86400.0
WEIXIN_INGRESS_STALE_AFTER_SECONDS = 180.0


class WeixinStore:
    @staticmethod
    def connector_name() -> str:
        return Path(__file__).parent.name

    def __init__(self, home: Path):
        self.dir = home / "weixin" / "accounts"
        self.dir.mkdir(parents=True, exist_ok=True)

    def account_path(self, account_id: str) -> Path:
        return self.dir / f"{account_id}.json"

    def save_account(self, account: WeixinAccount) -> None:
        payload = {
            "account_id": account.account_id,
            "token": account.token,
            "base_url": account.base_url,
            "user_id": account.user_id,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        path = self.account_path(account.account_id)
        _atomic_json_write(path, payload)
        try:
            path.chmod(0o600)
        except OSError as e:
            import logging

            logging.getLogger("navi.weixin").warning("Failed to chmod account file: %s", e)

    def load_account(self, account_id: str) -> WeixinAccount | None:
        path = self.account_path(account_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return WeixinAccount(
            account_id=str(data["account_id"]),
            token=str(data["token"]),
            base_url=str(data.get("base_url") or DEFAULT_WEIXIN_BASE_URL),
            user_id=str(data.get("user_id") or ""),
        )

    def list_accounts(self) -> list[str]:
        return [
            path.stem
            for path in sorted(self.dir.glob("*.json"))
            if not path.name.endswith(".context-tokens.json")
            and not path.name.endswith(".sync.json")
        ]

    def sync_path(self, account_id: str) -> Path:
        return self.dir / f"{account_id}.sync.json"

    def load_sync_buf(self, account_id: str) -> str:
        path = self.sync_path(account_id)
        if not path.exists():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("get_updates_buf") or "")

    def save_sync_buf(self, account_id: str, sync_buf: str) -> None:
        _atomic_json_write(self.sync_path(account_id), {"get_updates_buf": sync_buf})


@dataclass(frozen=True)
class WeixinPeerSession:
    account_id: str
    peer_id: str
    context_token: str
    observed_at: float
    invalidated_at: float = 0.0
    invalidation_reason: str = ""

    def is_current(
        self,
        *,
        now: float | None = None,
        max_age_seconds: float = CONTEXT_TOKEN_MAX_AGE_SECONDS,
    ) -> bool:
        current_time = time.time() if now is None else float(now)
        return bool(
            self.context_token
            and self.observed_at > self.invalidated_at
            and current_time - self.observed_at <= max(0.0, float(max_age_seconds))
        )


class WeixinSessionStore:
    """Durable peer-scoped iLink context state.

    Context tokens are transport credentials observed on inbound messages. They
    are never logged and are only returned while current and not invalidated by
    an authoritative iLink session rejection.
    """

    def __init__(self, home: Path):
        self.path = home / "weixin" / "peer-sessions.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sessions = self._load()

    @staticmethod
    def _key(account_id: str, peer_id: str) -> str:
        return f"{account_id}:{peer_id}"

    def _load(self) -> dict[str, WeixinPeerSession]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        raw_sessions = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(raw_sessions, dict):
            raise ValueError("weixin peer-sessions.json must contain a sessions object")
        sessions: dict[str, WeixinPeerSession] = {}
        for key, value in raw_sessions.items():
            if not isinstance(value, dict):
                raise ValueError(f"weixin peer session {key!r} must be an object")
            session = WeixinPeerSession(
                account_id=str(value.get("account_id") or ""),
                peer_id=str(value.get("peer_id") or ""),
                context_token=str(value.get("context_token") or ""),
                observed_at=float(value.get("observed_at") or 0.0),
                invalidated_at=float(value.get("invalidated_at") or 0.0),
                invalidation_reason=str(value.get("invalidation_reason") or ""),
            )
            if not session.account_id or not session.peer_id:
                raise ValueError(f"weixin peer session {key!r} is missing identity")
            sessions[self._key(session.account_id, session.peer_id)] = session
        return sessions

    def get(
        self,
        account_id: str,
        peer_id: str,
        *,
        now: float | None = None,
        max_age_seconds: float = CONTEXT_TOKEN_MAX_AGE_SECONDS,
    ) -> str:
        self._sessions = self._load()
        session = self._sessions.get(self._key(account_id, peer_id))
        if session is None or not session.is_current(now=now, max_age_seconds=max_age_seconds):
            return ""
        return session.context_token

    def resolve(
        self,
        account_id: str,
        peer_id: str,
        *,
        fallback: str = "",
        now: float | None = None,
        max_age_seconds: float = CONTEXT_TOKEN_MAX_AGE_SECONDS,
    ) -> str:
        self._sessions = self._load()
        if self._key(account_id, peer_id) not in self._sessions:
            return fallback
        session = self._sessions[self._key(account_id, peer_id)]
        if not session.is_current(now=now, max_age_seconds=max_age_seconds):
            return ""
        return session.context_token

    def put(
        self,
        account_id: str,
        peer_id: str,
        context_token: str,
        *,
        observed_at: float | None = None,
    ) -> bool:
        token = context_token.strip()
        if not account_id or not peer_id or not token:
            return False
        self._sessions = self._load()
        timestamp = time.time() if observed_at is None else float(observed_at)
        key = self._key(account_id, peer_id)
        existing = self._sessions.get(key)
        if existing is not None and existing.observed_at >= timestamp:
            return False
        self._sessions[key] = WeixinPeerSession(
            account_id=account_id,
            peer_id=peer_id,
            context_token=token,
            observed_at=timestamp,
        )
        self._persist()
        return True

    def invalidate(
        self,
        account_id: str,
        peer_id: str,
        *,
        reason: str,
        invalidated_at: float | None = None,
    ) -> bool:
        self._sessions = self._load()
        key = self._key(account_id, peer_id)
        existing = self._sessions.get(key)
        if existing is None:
            return False
        timestamp = time.time() if invalidated_at is None else float(invalidated_at)
        self._sessions[key] = WeixinPeerSession(
            account_id=existing.account_id,
            peer_id=existing.peer_id,
            context_token="",
            observed_at=existing.observed_at,
            invalidated_at=timestamp,
            invalidation_reason=reason[:200],
        )
        self._persist()
        return True

    def _persist(self) -> None:
        payload = {
            "version": 1,
            "sessions": {
                key: {
                    "account_id": session.account_id,
                    "peer_id": session.peer_id,
                    "context_token": session.context_token,
                    "observed_at": session.observed_at,
                    "invalidated_at": session.invalidated_at,
                    "invalidation_reason": session.invalidation_reason,
                }
                for key, session in sorted(self._sessions.items())
            },
        }
        _atomic_json_write(self.path, payload)
        self.path.chmod(0o600)


class WeixinStatusStore:
    """Durable ingress/reactive/proactive connector health projection."""

    def __init__(self, home: Path):
        self.home = home
        self.path = home / "weixin" / "status.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "status": "unknown",
            "error": "",
            "last_update": 0.0,
            "ingress_status": "unknown",
            "ingress_error": "",
            "last_ingress_update": 0.0,
            "egress_status": "unknown",
            "egress_error": "",
            "last_egress_attempt_at": 0.0,
            "last_egress_success_at": 0.0,
            "consecutive_egress_failures": 0,
            "consecutive_reactive_egress_failures": 0,
            "consecutive_proactive_egress_failures": 0,
            "reactive_egress_status": "unknown",
            "proactive_egress_status": "unknown",
            "reactive_egress_error": "",
            "proactive_egress_error": "",
            "last_reactive_egress_success_at": 0.0,
            "last_proactive_egress_success_at": 0.0,
            "proactive_circuit_open_until": 0.0,
            "last_provider_code": "",
            "instantaneous_egress_status": "unknown",
            "proactive_delivery_windows": {},
            "delivery_incident_status": "unknown",
            "delivery_incident_windows": [],
            "delivery_reliability_error": "",
        }
        if not self.path.exists():
            return defaults
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return defaults
        if not isinstance(data, dict):
            return defaults
        return {**defaults, **data}

    def snapshot(
        self,
        *,
        now: float | None = None,
        ingress_stale_after_seconds: float = WEIXIN_INGRESS_STALE_AFTER_SECONDS,
    ) -> dict[str, Any]:
        """Return current health with a read-time heartbeat freshness check."""

        state = self.load()
        current_time = time.time() if now is None else float(now)
        last_ingress_update = float(state.get("last_ingress_update") or 0.0)
        ingress_age = (
            max(0.0, current_time - last_ingress_update)
            if last_ingress_update > 0
            else 0.0
        )
        state["ingress_age_seconds"] = ingress_age
        state["ingress_stale_after_seconds"] = float(ingress_stale_after_seconds)
        if (
            last_ingress_update > 0
            and ingress_age > max(0.0, float(ingress_stale_after_seconds))
        ):
            state["ingress_status"] = "stale"
            state["ingress_error"] = (
                f"connector heartbeat is stale: last ingress update "
                f"{ingress_age:.1f}s ago"
            )
        return self._project(self._with_delivery_reliability(state, now=current_time))

    def update_ingress(self, status: str, error: str = "") -> dict[str, Any]:
        state = self.load()
        state.update(
            {
                "ingress_status": status,
                "ingress_error": error[:1000],
                "last_ingress_update": time.time(),
            }
        )
        return self._write(state)

    def record_user_activity(self) -> dict[str, Any]:
        state = self.load()
        state["proactive_circuit_open_until"] = 0.0
        return self._write(state)

    def record_egress_success(self, *, proactive: bool, at: float | None = None) -> dict[str, Any]:
        timestamp = time.time() if at is None else float(at)
        state = self.load()
        state.update(
            {
                "last_egress_attempt_at": timestamp,
                "last_egress_success_at": timestamp,
            }
        )
        if proactive:
            state["proactive_egress_status"] = "healthy"
            state["proactive_egress_error"] = ""
            state["last_proactive_egress_success_at"] = timestamp
            state["proactive_circuit_open_until"] = 0.0
            state["consecutive_proactive_egress_failures"] = 0
        else:
            state["reactive_egress_status"] = "healthy"
            state["reactive_egress_error"] = ""
            state["last_reactive_egress_success_at"] = timestamp
            state["consecutive_reactive_egress_failures"] = 0
        state["consecutive_egress_failures"] = max(
            int(state.get("consecutive_reactive_egress_failures") or 0),
            int(state.get("consecutive_proactive_egress_failures") or 0),
        )
        return self._write(state)

    def record_egress_failure(
        self,
        *,
        proactive: bool,
        error: str,
        provider_code: str = "",
        retry_after_seconds: float = 0.0,
        at: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if at is None else float(at)
        state = self.load()
        failure_key = (
            "consecutive_proactive_egress_failures"
            if proactive
            else "consecutive_reactive_egress_failures"
        )
        state[failure_key] = int(state.get(failure_key) or 0) + 1
        state.update(
            {
                "last_egress_attempt_at": timestamp,
                "last_provider_code": provider_code[:100],
            }
        )
        state["consecutive_egress_failures"] = max(
            int(state.get("consecutive_reactive_egress_failures") or 0),
            int(state.get("consecutive_proactive_egress_failures") or 0),
        )
        if proactive:
            state["proactive_egress_status"] = "degraded"
            state["proactive_egress_error"] = error[:1000]
            if retry_after_seconds > 0:
                state["proactive_circuit_open_until"] = max(
                    float(state.get("proactive_circuit_open_until") or 0.0),
                    timestamp + float(retry_after_seconds),
                )
        else:
            state["reactive_egress_status"] = "degraded"
            state["reactive_egress_error"] = error[:1000]
        return self._write(state)

    def proactive_circuit_open(self, *, now: float | None = None) -> bool:
        current_time = time.time() if now is None else float(now)
        return float(self.load().get("proactive_circuit_open_until") or 0.0) > current_time

    def _write(self, state: dict[str, Any]) -> dict[str, Any]:
        state = self._project(state)
        state["last_update"] = time.time()
        _atomic_json_write(self.path, state)
        return state

    def _with_delivery_reliability(
        self,
        state: dict[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        windows: dict[str, dict[str, Any]] = {}
        reliability_error = ""
        db_path = self.home / "delivery_outbox.db"
        window_seconds = (("1h", 3_600.0), ("24h", 86_400.0), ("7d", 604_800.0))
        if db_path.exists():
            try:
                with closing(
                    sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                ) as conn:
                    for label, seconds in window_seconds:
                        row = conn.execute(
                            """
                            SELECT COUNT(*),
                                   SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END)
                            FROM delivery_outbox
                            WHERE created_at >= ?
                              AND channel = 'weixin'
                              AND body_provenance != 'response_ready'
                              AND status IN ('sent', 'failed')
                            """,
                            (now - seconds,),
                        ).fetchone()
                        samples = int(row[0] or 0) if row else 0
                        sent = int(row[1] or 0) if row else 0
                        rate = sent / samples if samples else 0.0
                        windows[label] = {
                            "samples": samples,
                            "sent": sent,
                            "success_rate": rate,
                            "status": (
                                "insufficient_data"
                                if samples < 5
                                else "met"
                                if rate >= 0.95
                                else "breached"
                            ),
                        }
            except sqlite3.Error as exc:
                reliability_error = f"delivery reliability read failed: {type(exc).__name__}"
                windows = {
                    label: {
                        "samples": 0,
                        "sent": 0,
                        "success_rate": 0.0,
                        "status": "unknown",
                    }
                    for label, _ in window_seconds
                }
        else:
            windows = {
                label: {
                    "samples": 0,
                    "sent": 0,
                    "success_rate": 0.0,
                    "status": "insufficient_data",
                }
                for label, _ in window_seconds
            }
        state["proactive_delivery_windows"] = windows
        breached = [label for label, facts in windows.items() if facts["status"] == "breached"]
        statuses = {str(facts.get("status") or "unknown") for facts in windows.values()}
        state["delivery_incident_status"] = (
            "open"
            if breached
            else "unknown"
            if "unknown" in statuses
            else "insufficient_data"
            if "insufficient_data" in statuses
            else "closed"
        )
        state["delivery_incident_windows"] = breached
        state["delivery_reliability_error"] = reliability_error
        return state

    @staticmethod
    def _project(state: dict[str, Any]) -> dict[str, Any]:
        ingress = str(state.get("ingress_status") or "unknown")
        reactive = str(state.get("reactive_egress_status") or "unknown")
        proactive = str(state.get("proactive_egress_status") or "unknown")
        if "degraded" in {reactive, proactive}:
            egress = "degraded"
        elif reactive == "healthy" and proactive == "healthy":
            egress = "healthy"
        elif "healthy" in {reactive, proactive}:
            egress = "partial"
        else:
            egress = "unknown"
        state["instantaneous_egress_status"] = egress
        delivery_incident_status = str(state.get("delivery_incident_status") or "unknown")
        if delivery_incident_status == "open" and egress in {"healthy", "partial"}:
            egress = "degraded"
        elif delivery_incident_status in {"unknown", "insufficient_data"} and egress == "healthy":
            egress = "partial"
        state["egress_status"] = egress
        state["egress_error"] = (
            str(state.get("proactive_egress_error") or "")
            if proactive == "degraded"
            else str(state.get("reactive_egress_error") or "")
            if reactive == "degraded"
            else "rolling proactive delivery SLO is breached"
            if delivery_incident_status == "open"
            else str(state.get("delivery_reliability_error") or "")
            if delivery_incident_status == "unknown"
            else "rolling proactive delivery SLO has insufficient data"
            if delivery_incident_status == "insufficient_data"
            else ""
        )
        if ingress in {"fatal", "degraded", "stale"}:
            overall = ingress
            error = str(state.get("ingress_error") or "")
        elif egress == "degraded":
            overall = "degraded"
            error = str(state.get("egress_error") or "")
        elif ingress == "healthy" and egress == "healthy":
            overall = "healthy"
            error = ""
        elif ingress == "healthy":
            overall = "partial"
            error = ""
        else:
            overall = "unknown"
            error = ""
        state.update({"status": overall, "error": error[:1000]})
        return state


def extract_text(payload: dict[str, Any]) -> str:
    item_list = payload.get("item_list")
    if isinstance(item_list, list):
        text = _extract_text_items(item_list)
        if text:
            return text
    for key in ("text", "content", "message", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    items = payload.get("items")
    if isinstance(items, list):
        parts = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def _extract_text_items(items: list[Any]) -> str:
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == 1:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            if isinstance(ref_item, dict):
                ref_text = _extract_text_items([ref_item])
                title = str(ref.get("title") or "")
                if ref_text or title:
                    prefix = " | ".join(part for part in (title, ref_text) if part)
                    return f"[引用: {prefix}]\n{text}".strip()
            return text.strip()
    for item in items:
        if isinstance(item, dict) and item.get("type") == 3:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text.strip():
                return voice_text.strip()
    return ""


def split_text_for_weixin(content: str, max_length: int = 2000) -> list[str]:
    if not content or not content.strip():
        return []
    content = content.strip()
    if len(content) <= max_length:
        lines = content.splitlines()
        if 1 < len(lines) <= 3 and all(
            line.strip() and not line.startswith(("#", "-", "|", "```")) for line in lines
        ):
            return [line.strip() for line in lines]
        return [content]
    chunks: list[str] = []
    current = ""
    for block in content.split("\n\n"):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = ""
        while len(block) > max_length:
            chunks.append(block[:max_length])
            block = block[max_length:]
        current = block
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError as e:
            import logging

            logging.getLogger("navi.weixin").warning("Failed to unlink tmp file: %s", e)
        raise
