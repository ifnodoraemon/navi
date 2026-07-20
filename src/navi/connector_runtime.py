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
        peer_id = self.peer_id.strip() or "unknown"
        sender_id = self.sender_id.strip() or "unknown"
        return f"{self.session_alias_prefix}:{peer_id}:{sender_id}"

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
        from .intent_agent import IntentAgent

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

            from .safeguards import redact_secrets

            logging.getLogger("navi.connector").error(
                "Agent turn failed for correlation %s: %s",
                event.correlation_id,
                exc,
                exc_info=True,
            )
            error = redact_secrets(str(exc))
            return AgentTurnResult(
                text="",
                session_id=event.session_id,
                action="chat",
                trace_id=event.correlation_id,
                ok=False,
                error_reason="runtime_exception",
                facts={
                    "entity_type": "runtime_exception",
                    "entity_id": event.correlation_id,
                    "state_transition": "failed",
                    "turn_scope": "current",
                    "source_agent": "main_agent",
                    "reason": "agent_turn_exception",
                    "exception_type": type(exc).__name__,
                    "error": error,
                    "model_response_present": False,
                    "finalization": {
                        "reason": "runtime_exception",
                        "trace_id": event.correlation_id,
                        "model_response_present": False,
                    },
                },
            )
        finally:
            heartbeat_task.cancel()

    async def handle(self, message: ConnectorMessage) -> "ResponseReadyEvent | None":
        response = await self.router.route(message)
        return response
