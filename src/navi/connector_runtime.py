from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .control_plane import TurnController
from .turn_result import AgentTurnResult
from .runtime import AgentRuntime

if TYPE_CHECKING:
    from .event_bus import EventBus, NaviEvent, ResponseReadyEvent


# How often a still-running turn signals liveness on its response channel. Must
# be comfortably below the router's IDLE_TIMEOUT_SECONDS so a live turn never
# trips the idle timeout between two heartbeats.
HEARTBEAT_INTERVAL_SECONDS = 20.0


def connector_fact_text(event: str, facts: dict[str, Any]) -> str:
    payload = {"event": event, **facts}
    lines: list[str] = []
    for key, value in sorted(payload.items()):
        if isinstance(value, (dict, list)):
            value_text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        else:
            value_text = str(value)
        lines.append(f"{key}={value_text}")
    return "\n".join(lines)


def format_approval_notification(
    home: "Path",
    source: str,
    approval_code: str,
    run_id: str = "",
) -> str:
    """Render a human-readable approval notification using the connector's template.

    Returns the rendered text, or empty string when no template is available
    so callers can fall back to their own default.
    """
    from .connector_registry import load_connector_adapters
    from .runs import RunStore

    spec = None
    raw_source = source.strip()
    for adapter in load_connector_adapters():
        s = adapter.spec
        if raw_source in {s.name, s.surface, s.local_source}:
            spec = s
            break

    if not spec or not spec.approval_template:
        return ""

    runs = RunStore(home)
    approval = None
    if approval_code:
        approval = runs.pending_approval_by_code(approval_code)
    if approval is None and run_id:
        approval = runs.pending_approval_for_run(run_id)
    if approval is None:
        return ""

    approve_cmd = spec.approval_approve_commands[0] if spec.approval_approve_commands else "approve"
    reject_cmd = spec.approval_reject_commands[0] if spec.approval_reject_commands else "reject"

    task_line = approval.requested_tool or approval.action or "unknown"
    if approval.reason:
        task_line = f"{task_line} — {approval.reason}"

    expiry = ""
    if approval.expires_at:
        from datetime import datetime
        try:
            dt = datetime.fromtimestamp(approval.expires_at).astimezone()
            expiry = f"过期时间: {dt.strftime('%Y-%m-%d %H:%M')}"
        except (ValueError, TypeError, OSError):
            expiry = ""

    try:
        return spec.approval_template.format(
            task_line=task_line,
            code=approval.code,
            expiry=expiry,
            approve_command=approve_cmd,
            reject_command=reject_cmd,
        ).strip()
    except (KeyError, IndexError):
        return ""


@dataclass(frozen=True)
class ConnectorMessage:
    message_id: str
    peer_id: str
    sender_id: str
    text: str
    source: str
    session_alias_prefix: str
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def session_alias(self) -> str:
        return f"{self.session_alias_prefix}:{self.peer_id}"

    @property
    def content_key(self) -> str:
        payload = json.dumps(
            {
                "source": self.source,
                "peer_id": self.peer_id,
                "sender_id": self.sender_id,
                "text": self.text,
                "facts": self.facts,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        digest = hashlib.md5(payload.encode()).hexdigest()
        return f"content:{self.source}:{self.peer_id}:{self.sender_id}:{digest}"


@dataclass(frozen=True)
class ConnectorDedupResult:
    duplicate: bool
    reason: str = ""
    key: str = ""


class ConnectorIngressDeduplicator:
    """Shared connector ingress idempotency boundary.

    The agent loop should see each connector message once per source/message id
    or source/content key, even if an upstream long-poll endpoint redelivers it
    or a connector service object is recreated.
    """

    def __init__(self, home: Path, *, ttl_seconds: int = 300):
        self.path = home / "connectors" / "ingress-dedup.json"
        self.ttl_seconds = ttl_seconds

    def check(self, message: ConnectorMessage) -> ConnectorDedupResult:
        now = time.time()
        keys = self._keys(message)
        if not keys:
            return ConnectorDedupResult(False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            import fcntl

            with lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                seen = self._pruned(self._load_seen(), now=now)
                duplicate_key = next((key for key in keys if key in seen), "")
                for key in keys:
                    seen[key] = now + self.ttl_seconds
                _atomic_json_write(self.path, seen)
                if duplicate_key:
                    return ConnectorDedupResult(
                        True,
                        reason="message_id" if duplicate_key.startswith("id:") else "content",
                        key=duplicate_key,
                    )
                return ConnectorDedupResult(False)
        except OSError:
            return ConnectorDedupResult(False)

    @staticmethod
    def _keys(message: ConnectorMessage) -> list[str]:
        source = message.source.strip() or "unknown"
        peer_id = message.peer_id.strip() or "unknown"
        sender_id = message.sender_id.strip() or "unknown"
        keys: list[str] = []
        if message.message_id:
            keys.append(f"id:{source}:{peer_id}:{sender_id}:{message.message_id}")
        keys.append(message.content_key)
        return keys

    @staticmethod
    def _pruned(seen: dict[str, float], *, now: float) -> dict[str, float]:
        return {key: expires_at for key, expires_at in seen.items() if expires_at > now}

    def _load_seen(self) -> dict[str, float]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        seen: dict[str, float] = {}
        for key, value in raw.items():
            try:
                seen[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return seen


def _atomic_json_write(path: Path, payload: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            Path(tmp_name).unlink()
        except FileNotFoundError:
            pass


class ConnectorIngressRuntime:
    """Shared IM ingress path into the agent kernel via event bus."""

    def __init__(
        self,
        *,
        home: Path,
        runtime: AgentRuntime,
        project_dir: Path,
        allow_sources: set[str] | None = None,
        allowed_tools: set[str] | None = None,
        disabled_tools: set[str] | None = None,
        disabled_capability_classes: frozenset[str] = frozenset(),
        permission_ceiling: str = "write",
        event_bus: "EventBus | None" = None,
    ):
        from .connector_router import ConnectorRouter
        from .event_bus import EventBus as _EventBus

        self.event_bus = event_bus or _EventBus()
        self.router = ConnectorRouter(home, self.event_bus, runtime=runtime)
        self.agent = TurnController(
            home=home,
            runtime=runtime,
            project_dir=project_dir,
            allow_sources=allow_sources,
            allowed_tools=allowed_tools,
            disabled_tools=disabled_tools,
            disabled_capability_classes=disabled_capability_classes,
            permission_ceiling=permission_ceiling,
            event_bus=self.event_bus,
        )
        self._setup_event_subscriptions()

    def _setup_event_subscriptions(self) -> None:
        from .event_bus import ResponseReadyEvent, UserIntentEvent
        from .governance_agent import GovernanceAgent
        from .intent_agent import IntentAgent

        self._governance = GovernanceAgent(self.agent.home, self.event_bus)
        self._intent = IntentAgent(self.agent.home, self.agent.runtime, self.event_bus)

        async def on_user_intent(event: "NaviEvent") -> None:
            assert isinstance(event, UserIntentEvent)
            result = await self._handle_with_heartbeat(event)
            if result:
                text = result.surfaced_text()
                action = result.action
                facts = result.facts or {}
            else:
                text = ""
                action = "chat"
                facts = {}

            await self.event_bus.send_response(
                ResponseReadyEvent(
                    source_agent="main_agent",
                    correlation_id=event.correlation_id,
                    peer_id=event.peer_id,
                    sender_id=event.sender_id,
                    text=text,
                    source=event.source,
                    action=action,
                    facts=facts,
                )
            )

        self.event_bus.subscribe("user_intent", on_user_intent)

        from .event_bus import ActionSuspendedEvent

        async def on_action_suspended(event: "NaviEvent") -> None:
            assert isinstance(event, ActionSuspendedEvent)
            text = format_approval_notification(
                home=self.agent.home,
                source=event.source,
                approval_code=event.approval_code,
                run_id=event.run_id,
            )
            if not text:
                text = f"action_suspended\nrun_id={event.run_id}\napproval_code={event.approval_code}"
            await self.event_bus.send_response(
                ResponseReadyEvent(
                    source_agent="governance_agent",
                    correlation_id=event.correlation_id,
                    peer_id=event.peer_id,
                    sender_id=event.sender_id,
                    text=text,
                    source=event.source,
                )
            )

        self.event_bus.subscribe("action_suspended", on_action_suspended)

        async def on_run_suspended(event) -> None:
            from .event_bus import RunSuspendedEvent

            assert isinstance(event, RunSuspendedEvent)
            await self.event_bus.broadcast_proactive(
                ResponseReadyEvent(
                    source_agent="runtime",
                    correlation_id="",
                    peer_id=event.peer_id,
                    sender_id=event.sender_id,
                    text=event.text,
                    source=event.source,
                )
            )

        self.event_bus.subscribe("run_suspended", on_run_suspended)

    async def _handle_with_heartbeat(self, event) -> "AgentTurnResult | None":
        """Run the agent turn while emitting heartbeats so a slow-but-live turn
        is never mistaken for a stuck upstream by the router's idle timeout.

        The handler runs as a task; a companion loop pings the response channel
        until it finishes. Handler failures remain structured runtime facts;
        this layer does not invent user-facing failure copy."""
        handler_task = asyncio.ensure_future(
            self.agent.handle(
                event.text,
                peer_id=event.peer_id,
                sender_id=event.sender_id,
                source=event.source,
                session_alias=event.session_alias,
                intent_facts=event.facts,
                trace_id=event.correlation_id,
            )
        )

        async def beat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                await self.event_bus.send_heartbeat(event.correlation_id)

        heartbeat_task = asyncio.ensure_future(beat())
        try:
            return await handler_task
        except Exception as exc:
            import logging

            logging.getLogger("navi.connector").error(
                "Agent turn failed for correlation %s: %s",
                event.correlation_id,
                exc,
                exc_info=True,
            )
            return None
        finally:
            heartbeat_task.cancel()

    async def handle(self, message: ConnectorMessage) -> "ResponseReadyEvent | None":
        response = await self.router.route(message)
        return response
