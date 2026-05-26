from __future__ import annotations

import asyncio
import os
from pathlib import Path

from navi.connector_runtime import ConnectorIngressRuntime, ConnectorMessage
from navi.runtime import AgentRuntime

from .client import MockTelegramClient, TelegramClient
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
    ):
        self.home = home
        self.config = config
        self.runtime = runtime
        self.local_source = local_source
        self.session_alias_prefix = session_alias_prefix
        self.client = self._build_client()
        self.seen: set[str] = set()
        self.ingress = ConnectorIngressRuntime(
            home=home,
            runtime=runtime,
            allow_sources={"action", "core"},
        )

    def _build_client(self):
        if os.environ.get("NAVI_TELEGRAM_MOCK", "").lower() in {"1", "true", "yes"}:
            return MockTelegramClient()
        if not self.config.bot_token:
            raise RuntimeError("Telegram is not configured. Set TELEGRAM_BOT_TOKEN first.")
        return TelegramClient(api_base_url=self.config.api_base_url, bot_token=self.config.bot_token)

    def status(self) -> dict:
        return {
            "configured": bool(self.config.bot_token),
            "dm_policy": self.config.dm_policy,
            "home_chat_id": self.config.home_chat_id,
            "allowed_users_count": len(self.config.allowed_users),
        }

    def update_status(self, status: str, error: str = "") -> None:
        import time
        import json
        status_dir = self.home / "telegram"
        status_dir.mkdir(parents=True, exist_ok=True)
        status_file = status_dir / "status.json"
        try:
            status_file.write_text(
                json.dumps({
                    "status": status,
                    "error": error,
                    "last_update": time.time(),
                }, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    async def run(self, *, once: bool = False) -> None:
        import time
        from navi.runs import RunStore
        runs = RunStore(self.home)
        offset: int | None = None
        sleep_time = 1.0
        retry_count = 0
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
                    active_runs = runs.list_by_statuses(["queued", "running", "preparing"])
                    has_active_runs = len(active_runs) > 0
                    last_tasks_check = now

                has_activity = len(updates) > 0 or has_active_runs
                if has_activity:
                    sleep_time = 0.05
                else:
                    sleep_time = min(1.0, sleep_time + 0.1)
                
                retry_count = 0
                self.update_status("healthy")
                
            except Exception as e:
                retry_count += 1
                error_msg = str(e)
                if retry_count <= 5:
                    status = "retrying"
                    err_sleep = min(16.0, 1.5 ** retry_count)
                    self.update_status(status, error_msg)
                    sleep_time = err_sleep
                else:
                    status = "fatal"
                    self.update_status(status, error_msg)
                    raise e
            
            if once:
                return
            await asyncio.sleep(sleep_time)

    async def handle_update(self, update: TelegramUpdate) -> bool:
        message_key = f"telegram:{update.chat_id}:{update.message_id}"
        message = ConnectorMessage(
            message_id=message_key,
            peer_id=update.chat_id,
            sender_id=update.sender_id,
            text=update.text,
            source=self.local_source,
            session_alias_prefix=self.session_alias_prefix,
        )
        if message_key in self.seen or message.content_key in self.seen:
            return False
        self.seen.update({message_key, message.content_key})
        if not self._allowed(update):
            return False
        text = await self.ingress.handle(message)
        await self.client.send_message(chat_id=update.chat_id, text=text)
        return True

    def _allowed(self, update: TelegramUpdate) -> bool:
        if self.config.dm_policy == "disabled":
            return False
        if self.config.dm_policy in {"allowlist", "pairing"}:
            return update.sender_id in self.config.allowed_users
        return self.config.dm_policy == "open"
