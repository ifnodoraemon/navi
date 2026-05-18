from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from collections.abc import Callable

from navi.action_router import ActionRouter
from navi.assistant import ActiveAssistant
from navi.config import WeixinConfig
from navi.prompting import PromptContext
from navi.runtime import AgentRuntime

from .client import MockWeixinClient, WeixinClient
from .models import WeixinAccount, WeixinUpdate
from .store import ContextTokenStore, MessageDeduplicator, WeixinStore


class WeixinService:
    def __init__(self, *, home: Path, config: WeixinConfig, runtime: AgentRuntime):
        self.home = home
        self.config = config
        self.runtime = runtime
        self.store = WeixinStore(home)
        self.context_tokens = ContextTokenStore(home)
        self.dedup = MessageDeduplicator()
        self.client = self._build_client()
        self.active = ActiveAssistant(home)
        self.router = ActionRouter()

    def _build_client(self):
        if os.environ.get("NAVI_WEIXIN_MOCK", "").lower() in {"1", "true", "yes"}:
            return MockWeixinClient()
        token = self.config.token
        if self.config.account_id and not token:
            account = self.store.load_account(self.config.account_id)
            token = account.token if account else ""
        return WeixinClient(base_url=self.config.base_url, token=token)

    def status(self) -> dict:
        configured = bool(self.config.account_id or self.store.list_accounts())
        return {
            "configured": configured,
            "account_id": self.config.account_id,
            "saved_accounts": self.store.list_accounts(),
            "dm_policy": self.config.dm_policy,
            "group_policy": self.config.group_policy,
        }

    async def setup(
        self,
        *,
        timeout_seconds: int = 480,
        on_qr: Callable[[str], None] | None = None,
    ) -> str:
        qr = await self.client.request_qr()
        if on_qr:
            on_qr(qr.qrcode_url)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            account = await self.client.poll_qr_status(qr.ticket)
            if account is not None:
                self.store.save_account(account)
                return (
                    f"Weixin connected: account_id={account.account_id}\n"
                    f"Add WEIXIN_ACCOUNT_ID={account.account_id} to .navi/env or environment."
                )
            if asyncio.get_running_loop().time() >= deadline:
                return f"Weixin setup timed out. Scan and confirm this QR URL, then run setup again: {qr.qrcode_url}"
            await asyncio.sleep(1)

    async def run(self, *, once: bool = False) -> None:
        account = self._resolve_account()
        sync_buf = self.store.load_sync_buf(account.account_id)
        while True:
            batch = await self.client.get_updates(account.account_id, sync_buf=sync_buf)
            if batch.sync_buf:
                sync_buf = batch.sync_buf
                self.store.save_sync_buf(account.account_id, sync_buf)
            for update in batch.updates:
                await self.handle_update(account, update)
            await self.process_background(account)
            if once:
                return
            await asyncio.sleep(1)

    async def handle_update(self, account: WeixinAccount, update: WeixinUpdate) -> bool:
        if self.dedup.seen(update.message_id):
            return False
        is_command = update.text.strip().startswith("/")
        if not is_command:
            content_key = f"content:{update.sender_id}:{hashlib.md5(update.text.encode()).hexdigest()}"
            if self.dedup.seen(content_key):
                return False
        if not self._allowed(update):
            return False
        self.context_tokens.put(account.account_id, update.peer_id, update.context_token)
        if is_command:
            connector_result = self._handle_connector_command(update.text, peer_id=update.peer_id)
            if connector_result:
                await self.client.send_message(
                    account_id=account.account_id,
                    peer_id=update.peer_id,
                    text=connector_result,
                    context_token=self.context_tokens.get(account.account_id, update.peer_id),
                )
                return True
            result = await self.active.handle_weixin_command(
                update.text,
                peer_id=update.peer_id,
                sender_id=update.sender_id,
            )
            await self.client.send_message(
                account_id=account.account_id,
                peer_id=update.peer_id,
                text=result.text,
                context_token=self.context_tokens.get(account.account_id, update.peer_id),
            )
            return True
        routed = self.router.route(update.text)
        if routed.kind == "watch":
            result = self.active.create_watch_cron(
                routed.cron,
                routed.prompt,
                peer_id=update.peer_id,
                sender_id=update.sender_id,
            )
            await self.client.send_message(
                account_id=account.account_id,
                peer_id=update.peer_id,
                text=result.text,
                context_token=self.context_tokens.get(account.account_id, update.peer_id),
            )
            return True
        if routed.kind == "task":
            result = await self.active.create_task(
                routed.prompt,
                peer_id=update.peer_id,
                sender_id=update.sender_id,
                source="weixin",
            )
            await self.client.send_message(
                account_id=account.account_id,
                peer_id=update.peer_id,
                text=result.text,
                context_token=self.context_tokens.get(account.account_id, update.peer_id),
            )
            return True
        session_id = self.runtime.memory.current_session_id(self._session_alias(update.peer_id))
        reply = await self.runtime.chat(
            update.text,
            session_id=session_id,
            prompt_context=self._prompt_context(),
        )
        context_token = self.context_tokens.get(account.account_id, update.peer_id)
        await self.client.send_message(
            account_id=account.account_id,
            peer_id=update.peer_id,
            text=reply.content,
            context_token=context_token,
        )
        return True

    async def process_background(self, account: WeixinAccount) -> None:
        for result in await self.active.process_watches_once():
            task = self.active.tasks.get(result.task_id) if result.task_id else None
            peer_id = task.peer_id if task else self.config.home_channel
            if not peer_id:
                continue
            await self.client.send_message(
                account_id=account.account_id,
                peer_id=peer_id,
                text=result.text,
                context_token=self.context_tokens.get(account.account_id, peer_id),
            )
        for task in await self.active.process_queue_once():
            if not task.peer_id:
                continue
            text = (
                f"Task `{task.id}` {task.status}.\n"
                f"Result: {task.result_summary or '-'}\n"
                f"Error: {task.error or '-'}"
            )
            await self.client.send_message(
                account_id=account.account_id,
                peer_id=task.peer_id,
                text=text,
                context_token=self.context_tokens.get(account.account_id, task.peer_id),
            )

    def _resolve_account(self) -> WeixinAccount:
        if self.config.account_id:
            account = self.store.load_account(self.config.account_id)
            if account:
                return account
            if self.config.token:
                return WeixinAccount(
                    account_id=self.config.account_id,
                    token=self.config.token,
                    base_url=self.config.base_url,
                )
        accounts = self.store.list_accounts()
        if accounts:
            account = self.store.load_account(accounts[0])
            if account:
                return account
        raise RuntimeError("Weixin is not configured. Run `navi weixin setup` first.")

    def _allowed(self, update: WeixinUpdate) -> bool:
        if update.is_group:
            return self._policy_allows(
                self.config.group_policy,
                update.peer_id,
                self.config.group_allowed_users,
            )
        return self._policy_allows(
            self.config.dm_policy,
            update.sender_id,
            self.config.allowed_users,
        )

    @staticmethod
    def _policy_allows(policy: str, identity: str, allowed: list[str]) -> bool:
        if policy == "disabled":
            return False
        if policy == "allowlist":
            return identity in allowed
        return policy in {"open", "pairing"}

    @staticmethod
    def _prompt_context() -> PromptContext:
        return PromptContext(
            surface="Weixin connector",
            affordances=(
                "Use /task <natural-language request> to submit local actions into Navi's tracked task path.",
                "Use /approve <code> or /reject <code> when Navi returns an approval code.",
                "Use /status or /jobs to inspect tracked work.",
                "Use /new to start a fresh conversation session for this peer.",
            ),
        )

    def _handle_connector_command(self, text: str, *, peer_id: str) -> str:
        command = text.strip().split(maxsplit=1)[0].lower()
        if command not in {"/new", "/reset"}:
            return ""
        session = self.runtime.memory.rotate_session(self._session_alias(peer_id))
        return f"Started a new conversation session: {session.session_id}"

    @staticmethod
    def _session_alias(peer_id: str) -> str:
        return f"connector:weixin:{peer_id}"
