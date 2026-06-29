from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict
from pathlib import Path
from collections.abc import Callable

from navi.connector_runtime import ConnectorIngressRuntime, ConnectorMessage
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.runs import Run
from navi.safeguards import redact_secrets
from navi.daemon import SystemDaemon

from .client import TYPING_START, TYPING_STOP, WeixinClient
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
        project_dir: Path,
        tool_policy=None,
        client=None,
    ):
        self.home = home
        self.project_dir = project_dir.resolve()
        self.config = config
        self.runtime = runtime
        self.local_source = local_source
        self.session_alias_prefix = session_alias_prefix
        self.store = WeixinStore(home)
        self.context_tokens = ContextTokenStore(home)
        self.dedup = MessageDeduplicator()
        self.client = client if client is not None else self._build_client()
        self.typing_tickets: dict[str, str] = {}
        self.daemon = SystemDaemon(home, project_dir=self.project_dir)
        self.active = self.daemon
        ingress_kwargs = {
            "home": home,
            "runtime": runtime,
            "project_dir": self.project_dir,
            "allow_sources": {"action", "core"},
        }
        if tool_policy is not None:
            ingress_kwargs["tool_policy"] = tool_policy
        self.ingress = ConnectorIngressRuntime(**ingress_kwargs)

    def _build_client(self):
        token = self.config.token
        if self.config.account_id and not token:
            account = self.store.load_account(self.config.account_id)
            token = account.token if account else ""
        return WeixinClient(
            base_url=self.config.base_url,
            token=token,
            cdn_base_url=self.config.cdn_base_url,
            media_dir=self.home / "weixin" / "media" / "inbound",
        )

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
                return f"Weixin setup timed out: qr_url={qr.qrcode_url}"
            await asyncio.sleep(1)

    def update_status(self, status: str, error: str = "") -> None:
        status_dir = self.home / "weixin"
        status_dir.mkdir(parents=True, exist_ok=True)
        status_file = status_dir / "status.json"
        try:
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
        except Exception as e:
            import logging

            logging.getLogger("navi.weixin").warning("Failed to update status: %s", e)

    def record_event(self, event: str, **facts) -> None:
        event_dir = self.home / "weixin"
        event_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.time(),
            "event": event,
            **_redact_event_facts(facts),
        }
        try:
            with (event_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
        except Exception as e:
            import logging

            logging.getLogger("navi.weixin").warning("Failed to record event: %s", e)

    async def run(self, *, once: bool = False) -> None:
        import time

        account = self._resolve_account()
        sync_buf = self.store.load_sync_buf(account.account_id)
        sleep_time = 1.0
        retry_count = 0
        self.update_status("healthy")
        last_tasks_check = 0.0
        has_active_runs = False
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
                    active_runs = self.daemon.runs.list_by_statuses(
                        ["queued", "running", "preparing"]
                    )
                    has_active_runs = len(active_runs) > 0
                    last_tasks_check = now

                has_activity = len(batch.updates) > 0 or has_active_runs
                if has_activity:
                    sleep_time = 0.05
                else:
                    sleep_time = min(1.0, sleep_time + 0.1)

                retry_count = 0
                self.update_status("healthy")

            except Exception as e:
                retry_count += 1
                error_msg = str(e)
                self.record_event("poll.error", retry_count=retry_count, error=error_msg)
                if retry_count <= 5:
                    status = "retrying"
                    err_sleep = min(16.0, 1.5**retry_count)
                    self.update_status(status, error_msg)
                    sleep_time = err_sleep
                else:
                    status = "degraded"
                    sleep_time = min(60.0, 5.0 * retry_count)
                    self.update_status(status, error_msg)

            if once:
                return
            await asyncio.sleep(sleep_time)

    async def handle_update(self, account: WeixinAccount, update: WeixinUpdate) -> bool:
        if self.dedup.seen(update.message_id):
            self.record_event(
                "message.duplicate", message_id=update.message_id, peer_id=update.peer_id
            )
            return False
        message = ConnectorMessage(
            message_id=update.message_id,
            peer_id=update.peer_id,
            sender_id=update.sender_id,
            text=update.text,
            source=self.local_source,
            session_alias_prefix=self.session_alias_prefix,
            facts=_weixin_message_facts(update),
        )
        if self.dedup.seen(message.content_key):
            self.record_event(
                "message.duplicate_content", message_id=update.message_id, peer_id=update.peer_id
            )
            return False
        if not self._allowed(update):
            self.record_event(
                "message.blocked",
                message_id=update.message_id,
                peer_id=update.peer_id,
                sender_id=update.sender_id,
            )
            return False
        self.record_event(
            "message.received",
            message_id=update.message_id,
            peer_id=update.peer_id,
            sender_id=update.sender_id,
            text_preview=update.text[:120],
            attachment_count=len(update.attachments),
        )
        self.context_tokens.put(account.account_id, update.peer_id, update.context_token)
        context_token = self.context_tokens.get(account.account_id, update.peer_id)
        text = await self._handle_with_typing(update, message, context_token=context_token)
        try:
            delivery = await self._send_reply(
                account=account,
                peer_id=update.peer_id,
                text=text,
                context_token=context_token,
            )
        except Exception as exc:
            self.record_event(
                "reply.error", peer_id=update.peer_id, error=f"{type(exc).__name__}: {exc}"
            )
            raise
        self.record_event(
            "reply.sent",
            peer_id=update.peer_id,
            text_preview=delivery["text_preview"],
            media_count=delivery["media_count"],
        )
        return True

    async def _handle_with_typing(
        self, update: WeixinUpdate, message: ConnectorMessage, *, context_token: str
    ) -> str:
        typing_ticket = await self._typing_ticket(update.sender_id, context_token=context_token)
        stop_typing = asyncio.Event()
        typing_task = (
            asyncio.create_task(self._keep_typing(update.sender_id, typing_ticket, stop_typing))
            if typing_ticket
            else None
        )
        try:
            return await self.ingress.handle(message)
        except Exception as exc:
            self.update_status("degraded", f"message handler failed: {type(exc).__name__}: {exc}")
            self.record_event(
                "handler.error",
                peer_id=update.peer_id,
                sender_id=update.sender_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return (
                "本地处理链路失败；"
                f"message_id={update.message_id}；"
                f"error_type={type(exc).__name__}。"
            )
        finally:
            stop_typing.set()
            if typing_task:
                await typing_task

    async def _typing_ticket(self, sender_id: str, *, context_token: str) -> str:
        if not sender_id:
            return ""
        cached = self.typing_tickets.get(sender_id)
        if cached:
            return cached
        try:
            ticket = await self.client.get_typing_ticket(
                user_id=sender_id, context_token=context_token
            )
        except Exception:
            return ""
        if ticket:
            self.typing_tickets[sender_id] = ticket
            self.record_event("typing.ticket", sender_id=sender_id)
        return ticket

    async def _keep_typing(
        self, peer_id: str, typing_ticket: str, stop_event: asyncio.Event
    ) -> None:
        try:
            while not stop_event.is_set():
                await self._send_typing_safely(peer_id, typing_ticket, TYPING_START)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=3.0)
                except TimeoutError:
                    continue
        finally:
            await self._send_typing_safely(peer_id, typing_ticket, TYPING_STOP)

    async def _send_typing_safely(self, peer_id: str, typing_ticket: str, status: int) -> None:
        try:
            await asyncio.wait_for(
                self.client.send_typing(
                    peer_id=peer_id, typing_ticket=typing_ticket, status=status
                ),
                timeout=1.5,
            )
            self.record_event("typing.sent", peer_id=peer_id, status=status)
        except Exception as e:
            self.record_event("typing.error", peer_id=peer_id, status=status)
            import logging

            logging.getLogger("navi.weixin").warning("Failed to send typing: %s", e)

    async def process_background(self, account: WeixinAccount) -> None:
        for result in await self.daemon.process_watches_once():
            run_id = str(result.get("run_id") or "")
            task = self.daemon.runs.get(run_id) if run_id else None
            peer_id = str(result.get("peer_id") or "") or (
                task.peer_id if task else self.config.home_channel
            )
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
            await self._send_reply(
                account=account,
                peer_id=peer_id,
                text=text,
                context_token=self.context_tokens.get(account.account_id, peer_id),
            )
            self.record_event(
                "background.sent",
                peer_id=peer_id,
                background_event="watch_result" if not task else "watch_task_prepared",
                text_preview=text[:120],
            )
        for task in await self.daemon.process_queue_once():
            if not task.peer_id:
                continue
            text = await self._compose_background_message(
                {
                    "event": "run_execution_finished",
                    "task": asdict(task),
                },
                fallback=self._task_fallback(task),
            )
            await self._send_reply(
                account=account,
                peer_id=task.peer_id,
                text=text,
                context_token=self.context_tokens.get(account.account_id, task.peer_id),
            )
            self.record_event(
                "background.sent",
                peer_id=task.peer_id,
                background_event="run_execution_finished",
                text_preview=text[:120],
            )

    async def _compose_background_message(self, facts: dict, *, fallback: str) -> str:
        if facts.get("event") == "watch_result":
            raw_result = str(facts.get("raw_result") or "").strip()
            if raw_result:
                return redact_secrets(raw_result)
        stripped = fallback.strip() or json.dumps(facts, ensure_ascii=False, sort_keys=True)
        return redact_secrets(stripped)

    async def _send_reply(
        self,
        *,
        account: WeixinAccount,
        peer_id: str,
        text: str,
        context_token: str,
    ) -> dict[str, object]:
        media_paths, cleaned_text = _extract_media_directives(text)
        sent_media = 0
        for media_path in media_paths:
            allowed_path = self._allowed_outbound_media_path(media_path)
            if allowed_path is None:
                self.record_event("reply.media.blocked", peer_id=peer_id, path=media_path)
                continue
            await self.client.send_file(
                account_id=account.account_id,
                peer_id=peer_id,
                file_path=allowed_path,
                context_token=context_token,
            )
            sent_media += 1
            self.record_event(
                "reply.media.sent",
                peer_id=peer_id,
                path=str(allowed_path),
                media_count=sent_media,
            )
        outbound_text = cleaned_text.strip()
        if outbound_text:
            await self.client.send_message(
                account_id=account.account_id,
                peer_id=peer_id,
                text=outbound_text,
                context_token=context_token,
            )
        return {"media_count": sent_media, "text_preview": outbound_text[:120]}

    def _allowed_outbound_media_path(self, raw_path: str) -> Path | None:
        try:
            candidate = Path(raw_path).expanduser().resolve()
        except OSError:
            return None
        allowed_roots = (
            self.home / "weixin" / "outbox",
            self.home / "weixin" / "media" / "outbound",
        )
        for root in allowed_roots:
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                continue
            return candidate if candidate.is_file() else None
        return None

    @staticmethod
    def _task_fallback(task: Run) -> str:
        # awaiting_approval carries the re-approval prompt (with the fresh code)
        # in result_summary; surface it verbatim rather than the empty error.
        if task.status in ("awaiting_approval", "blocked"):
            return (task.result_summary or "").strip() or f"Run `{task.id}` {task.status}。"
        details = task.result_summary if task.status == "completed" else task.error
        return f"Run `{task.id}` {task.status}. {details or ''}".strip()

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


def _redact_event_facts(facts: dict) -> dict:
    """Redact secrets from event facts before they hit events.jsonl.

    Free-text fields (text_preview, error, message, raw_result) are scanned
    for bearer tokens / api keys / passwords / PEM blocks and redacted in
    place, so a user-pasted secret is never persisted verbatim. Field keys
    that name a secret outright are masked regardless of value.
    """
    redacted = {}
    sensitive_keys = ("token", "secret", "password", "key", "code")
    free_text_keys = ("text_preview", "error", "message", "raw_result", "detail")
    for key, value in facts.items():
        key_text = str(key).lower()
        if any(token in key_text for token in sensitive_keys):
            redacted[key] = "[redacted]"
        elif key_text in free_text_keys and isinstance(value, str):
            redacted[key] = redact_secrets(value)
        else:
            redacted[key] = value
    return redacted


_MEDIA_DIRECTIVE_RE = re.compile(r"(?m)^\s*MEDIA:(?P<path>\S+)\s*$")


def _extract_media_directives(text: str) -> tuple[list[str], str]:
    media_paths: list[str] = []

    def _capture(match: re.Match[str]) -> str:
        media_paths.append(match.group("path").strip())
        return ""

    cleaned = _MEDIA_DIRECTIVE_RE.sub(_capture, text or "")
    cleaned = "\n".join(line for line in cleaned.splitlines() if line.strip()).strip()
    return media_paths, cleaned


def _weixin_message_facts(update: WeixinUpdate) -> dict[str, object]:
    attachments = [asdict(attachment) for attachment in update.attachments]
    return {
        "connector": "weixin",
        "message_id": update.message_id,
        "peer_id": update.peer_id,
        "sender_id": update.sender_id,
        "is_group": update.is_group,
        "attachment_count": len(attachments),
        "attachments": attachments,
    }
