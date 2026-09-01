from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from navi.connector_contract import SYNTHETIC_MESSAGE_ID_PREFIX
from navi.connector_runtime import (
    ConnectorIngressDeduplicator,
    ConnectorIngressRuntime,
    ConnectorMessage,
)
from navi.runtime import AgentRuntime
from navi.runs import RunStore
from navi.trace import TracePhase, TraceStore

from .client import TelegramClient
from .config import TelegramConfig
from .models import TelegramUpdate


class TelegramService:
    def __init__(
        self,
        *,
        home: Path,
        config: TelegramConfig,
        runtime: AgentRuntime,
        local_source: str = "telegram",
        session_alias_prefix: str = "connector:telegram",
        project_dir: Path,
        client=None,
    ):
        self.home = home
        self.project_dir = project_dir.resolve()
        self.config = config
        self.runtime = runtime
        self.local_source = local_source
        self.session_alias_prefix = session_alias_prefix
        self.client = client if client is not None else self._build_client()
        self.dedup = ConnectorIngressDeduplicator(home)
        self.ingress = ConnectorIngressRuntime(
            home=home,
            runtime=runtime,
            project_dir=self.project_dir,
        )

    def _build_client(self):
        if not self.config.bot_token:
            raise RuntimeError(
                "Telegram is not configured: connectors.telegram.bot_token is required"
            )
        return TelegramClient(
            api_base_url=self.config.api_base_url,
            bot_token=self.config.bot_token,
            media_dir=self.home / "telegram" / "media" / "inbound",
        )

    def status(self) -> dict:
        return {
            "configured": bool(self.config.bot_token),
            "dm_policy": self.config.dm_policy,
            "home_chat_id": self.config.home_chat_id,
            "allowed_users_count": len(self.config.allowed_users),
        }

    def update_status(self, status: str, error: str = "") -> None:
        status_dir = self.home / "telegram"
        status_dir.mkdir(parents=True, exist_ok=True)
        status_file = status_dir / "status.json"
        status_file.write_text(
            json.dumps(
                {
                    "status": status,
                    "error": error,
                    "last_update": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    async def run(self, *, once: bool = False) -> None:
        runs = RunStore(self.home)
        offset: int | None = None
        sleep_time = 1.0
        self.update_status("healthy")
        last_tasks_check = 0.0
        has_active_runs = False
        while True:
            try:
                updates = await self.client.get_updates(offset=offset)
                for update in updates:
                    offset = max(offset or 0, update.update_id + 1)
                    await self.handle_update(update)

                # Check for activity to adapt sleep time
                now = time.time()
                if now - last_tasks_check >= 2.0:
                    active_runs = runs.list_by_phases(["queued", "running", "preparing"])
                    has_active_runs = len(active_runs) > 0
                    last_tasks_check = now

                has_activity = len(updates) > 0 or has_active_runs
                if has_activity:
                    sleep_time = 0.05
                else:
                    sleep_time = min(1.0, sleep_time + 0.1)

                self.update_status("healthy")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                error_msg = str(e)
                self.update_status("degraded", error_msg)
                if once:
                    raise
                sleep_time = min(30.0, max(1.0, sleep_time * 2.0))
                await asyncio.sleep(sleep_time)
                continue

            if once:
                return
            await asyncio.sleep(sleep_time)

    async def handle_update(self, update: TelegramUpdate) -> bool:
        if update.message_id:
            message_key = f"telegram:{update.chat_id}:{update.message_id}"
        else:
            message_key = f"{SYNTHETIC_MESSAGE_ID_PREFIX}telegram:{update.chat_id}:{uuid.uuid4().hex}"
        text = update.text
        if not text.strip() and update.attachments:
            # Media-only messages still need a user turn; an empty turn
            # objective dies before any model call. Attachment paths travel
            # in facts.
            names = ", ".join(
                attachment.file_name or attachment.kind
                for attachment in update.attachments
            )
            text = f"[media] {names}".strip()
        message = ConnectorMessage(
            message_id=message_key,
            peer_id=update.chat_id,
            sender_id=update.sender_id,
            text=text,
            source=self.local_source,
            session_alias_prefix=self.session_alias_prefix,
            facts=_telegram_message_facts(update),
        )
        if self.dedup.check(message).duplicate:
            return False
        if not self._allowed(update):
            return False
        response = await self.ingress.handle(message)
        if response is None or not response.text.strip():
            finalization = (
                response.facts.get("finalization")
                if response is not None and isinstance(response.facts, dict)
                else None
            )
            if isinstance(finalization, dict) and finalization.get("durable_retry_pending") is True:
                TraceStore(self.home).add_event(
                    trace_id=message_key,
                    phase=TracePhase.CHANNEL_EGRESS,
                    source=self.local_source,
                    peer_id=update.chat_id,
                    sender_id=update.sender_id,
                    output_data={
                        "delivery_attempted": False,
                        "reason": str(
                            finalization.get("reason") or "provider_transport_retry_pending"
                        ),
                        "durable_retry_pending": True,
                    },
                    message="Response deferred to durable transport recovery",
                    ok=True,
                )
                return True
            TraceStore(self.home).add_event(
                trace_id=message_key,
                phase=TracePhase.CHANNEL_EGRESS,
                source=self.local_source,
                peer_id=update.chat_id,
                sender_id=update.sender_id,
                output_data={
                    "delivery_attempted": False,
                    "reason": "empty_response",
                },
                message="Telegram response failed because it was empty",
                ok=False,
            )
            TraceStore(self.home).evaluate_trace(message_key)
            raise RuntimeError("channel response text is empty")
        try:
            await self.client.send_message(chat_id=update.chat_id, text=response.text)
        except Exception as exc:
            TraceStore(self.home).add_event(
                trace_id=message_key,
                phase=TracePhase.CHANNEL_EGRESS,
                source=self.local_source,
                peer_id=update.chat_id,
                sender_id=update.sender_id,
                output_data={
                    "delivery_attempted": True,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                message="Telegram delivery failed",
                ok=False,
            )
            TraceStore(self.home).evaluate_trace(message_key)
            raise
        TraceStore(self.home).add_event(
            trace_id=message_key,
            phase=TracePhase.CHANNEL_EGRESS,
            source=self.local_source,
            peer_id=update.chat_id,
            sender_id=update.sender_id,
            output_data={"response": response.text, "action": response.action},
            message="Delivered response to Telegram",
        )
        TraceStore(self.home).evaluate_trace(message_key)
        return True

    def _allowed(self, update: TelegramUpdate) -> bool:
        if self.config.dm_policy == "disabled":
            return False
        if self.config.dm_policy in {"allowlist", "pairing"}:
            return update.sender_id in self.config.allowed_users
        return self.config.dm_policy == "open"


def _telegram_message_facts(update: TelegramUpdate) -> dict[str, object]:
    from dataclasses import asdict

    attachments = [asdict(attachment) for attachment in update.attachments]
    return {
        "connector": "telegram",
        "message_id": f"telegram:{update.chat_id}:{update.message_id}",
        "peer_id": update.chat_id,
        "sender_id": update.sender_id,
        "attachment_count": len(attachments),
        "attachments": attachments,
    }
