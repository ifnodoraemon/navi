from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path
from collections.abc import Callable

from navi.connector_runtime import ConnectorIngressRuntime, ConnectorMessage
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.tasks import Task
from navi.daemon import SystemDaemon

from .client import MockWeixinClient, WeixinClient
from .config import WeixinConfig
from .models import WeixinAccount, WeixinUpdate
from .store import ContextTokenStore, MessageDeduplicator, WeixinStore


class WeixinService:
    def __init__(
        self,
        *,
        home: Path,
        config: WeixinConfig,
        runtime: AgentRuntime,
        local_source: str = "weixin",
        session_alias_prefix: str = "connector:weixin",
    ):
        self.home = home
        self.config = config
        self.runtime = runtime
        self.local_source = local_source
        self.session_alias_prefix = session_alias_prefix
        self.store = WeixinStore(home)
        self.context_tokens = ContextTokenStore(home)
        self.dedup = MessageDeduplicator()
        self.client = self._build_client()
        self.daemon = SystemDaemon(home)
        self.active = self.daemon
        self.ingress = ConnectorIngressRuntime(
            home=home,
            runtime=runtime,
            allow_sources={"action", "core"},
        )

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

    def update_status(self, status: str, error: str = "") -> None:
        import time
        status_dir = self.home / "weixin"
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
        account = self._resolve_account()
        sync_buf = self.store.load_sync_buf(account.account_id)
        sleep_time = 1.0
        retry_count = 0
        self.update_status("healthy")
        last_tasks_check = 0.0
        has_active_tasks = False
        last_bg_check = 0.0
        while True:
            try:
                batch = await self.client.get_updates(account.account_id, sync_buf=sync_buf)
                if batch.sync_buf:
                    sync_buf = batch.sync_buf
                    self.store.save_sync_buf(account.account_id, sync_buf)
                for update in batch.updates:
                    await self.handle_update(account, update)
                
                # Throttle background processing to at most once per second,
                # unless there is incoming user activity in the current batch.
                now = time.time()
                if len(batch.updates) > 0 or (now - last_bg_check >= 1.0):
                    await self.process_background(account)
                    last_bg_check = now
                
                # Check for activity to adapt sleep time
                if now - last_tasks_check >= 2.0:
                    active_tasks = self.daemon.tasks.list_by_statuses(["queued", "running", "preparing"])
                    has_active_tasks = len(active_tasks) > 0
                    last_tasks_check = now

                has_activity = len(batch.updates) > 0 or has_active_tasks
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

    async def handle_update(self, account: WeixinAccount, update: WeixinUpdate) -> bool:
        if self.dedup.seen(update.message_id):
            return False
        message = ConnectorMessage(
            message_id=update.message_id,
            peer_id=update.peer_id,
            sender_id=update.sender_id,
            text=update.text,
            source=self.local_source,
            session_alias_prefix=self.session_alias_prefix,
        )
        if self.dedup.seen(message.content_key):
            return False
        if not self._allowed(update):
            return False
        self.context_tokens.put(account.account_id, update.peer_id, update.context_token)
        text = await self.ingress.handle(message)
        context_token = self.context_tokens.get(account.account_id, update.peer_id)
        await self.client.send_message(
            account_id=account.account_id,
            peer_id=update.peer_id,
            text=text,
            context_token=context_token,
        )
        return True

    async def process_background(self, account: WeixinAccount) -> None:
        for result in await self.daemon.process_watches_once():
            task_id = str(result.get("task_id") or "")
            task = self.daemon.tasks.get(task_id) if task_id else None
            peer_id = str(result.get("peer_id") or "") or (task.peer_id if task else self.config.home_channel)
            if not peer_id:
                continue
            text = await self._compose_background_message(
                {
                    "event": "watch_result" if not task else "watch_task_prepared",
                    "task": asdict(task) if task else None,
                    "raw_result": result.get("message") or result.get("observation") or "",
                },
                fallback=str(result.get("message") or result.get("observation") or ""),
            )
            await self.client.send_message(
                account_id=account.account_id,
                peer_id=peer_id,
                text=text,
                context_token=self.context_tokens.get(account.account_id, peer_id),
            )
        for task in await self.daemon.process_queue_once():
            if not task.peer_id:
                continue
            text = await self._compose_background_message(
                {
                    "event": "task_execution_finished",
                    "task": asdict(task),
                },
                fallback=self._task_fallback(task),
            )
            await self.client.send_message(
                account_id=account.account_id,
                peer_id=task.peer_id,
                text=text,
                context_token=self.context_tokens.get(account.account_id, task.peer_id),
            )

    async def _compose_background_message(self, facts: dict, *, fallback: str) -> str:
        try:
            text = await self.runtime.complete(
                [
                    ChatMessage(
                        "system",
                        "\n".join(
                            (
                                "You are Navi composing a concise connector notification.",
                                "Use only the supplied facts.",
                                "Preserve task ids, approval codes, status, errors, and important result text.",
                                "Do not mention connector internals, JSON, or hidden routers.",
                            )
                        ),
                    ),
                    ChatMessage("user", json.dumps(facts, ensure_ascii=False, sort_keys=True)),
                ],
                role="notification",
            )
        except Exception:
            return fallback
        stripped = text.strip()
        return stripped or fallback

    @staticmethod
    def _task_fallback(task: Task) -> str:
        details = task.result_summary if task.status == "completed" else task.error
        return f"Task `{task.id}` {task.status}. {details or ''}".strip()

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
        raise RuntimeError("Weixin is not configured. Run `navi connectors setup weixin` first.")

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
        if policy in {"allowlist", "pairing"}:
            return identity in allowed
        return policy == "open"
