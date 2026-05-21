from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from navi.agent_kernel import AgentKernel
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
        self.agent = AgentKernel(
            home=home,
            runtime=runtime,
            project_dir=Path.cwd(),
            allow_sources={"core"},
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

    async def run(self, *, once: bool = False) -> None:
        offset: int | None = None
        while True:
            for update in await self.client.get_updates(offset=offset):
                offset = max(offset or 0, update.update_id + 1)
                await self.handle_update(update)
            if once:
                return
            await asyncio.sleep(1)

    async def handle_update(self, update: TelegramUpdate) -> bool:
        message_key = f"telegram:{update.chat_id}:{update.message_id}"
        content_key = f"content:{update.sender_id}:{hashlib.md5(update.text.encode()).hexdigest()}"
        if message_key in self.seen or content_key in self.seen:
            return False
        self.seen.update({message_key, content_key})
        if not self._allowed(update):
            return False
        result = await self.agent.handle(
            update.text,
            peer_id=update.chat_id,
            sender_id=update.sender_id,
            source=self.local_source,
            session_alias=self._session_alias(update.chat_id),
        )
        await self.client.send_message(chat_id=update.chat_id, text=result.text)
        return True

    def _allowed(self, update: TelegramUpdate) -> bool:
        if self.config.dm_policy == "disabled":
            return False
        if self.config.dm_policy in {"allowlist", "pairing"}:
            return update.sender_id in self.config.allowed_users
        return self.config.dm_policy == "open"

    def _session_alias(self, chat_id: str) -> str:
        return f"{self.session_alias_prefix}:{chat_id}"
