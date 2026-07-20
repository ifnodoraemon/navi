from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from collections.abc import Callable
from typing import TypedDict

from navi.connector_runtime import (
    ConnectorIngressDeduplicator,
    ConnectorIngressRuntime,
    ConnectorMessage,
)
from navi.connector_delivery import connector_delivery_from_facts
from navi.event_bus import ResponseReadyEvent
from navi.finalization import synthesize_background_notification
from navi.goals import GoalDeliveryOutboxItem, GoalStore
from navi.runtime import AgentRuntime
from navi.runs import Run
from navi.daemon import SystemDaemon

from .client import TYPING_START, TYPING_STOP, WeixinClient
from .config import WeixinConfig
from .models import WeixinAccount, WeixinUpdate

from .store import ContextTokenStore, WeixinStore


class DeliveryReceipt(TypedDict):
    media_count: int
    text_preview: str


_BACKGROUND_NOTIFICATION_SCHEMA = {
    "name": "background_notification",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "notify": {"type": "boolean"},
            "message": {"type": "string"},
        },
        "required": ["notify", "message"],
        "additionalProperties": False,
    },
}


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
        self.dedup = ConnectorIngressDeduplicator(home)
        self.client = client if client is not None else self._build_client()
        self.typing_tickets: dict[str, str] = {}
        self.daemon = SystemDaemon(home, project_dir=self.project_dir)
        self.active = self.daemon
        self.ingress = ConnectorIngressRuntime(
            home=home,
            runtime=runtime,
            project_dir=self.project_dir,
        )

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
                    active_runs = self.daemon.runs.list_by_phases(
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
        message = ConnectorMessage(
            message_id=update.message_id,
            peer_id=update.peer_id,
            sender_id=update.sender_id,
            text=update.text,
            source=self.local_source,
            session_alias_prefix=self.session_alias_prefix,
            facts=_weixin_message_facts(update),
        )
        duplicate = self.dedup.check(message)
        if duplicate.duplicate:
            event_name = (
                "message.duplicate"
                if duplicate.reason == "message_id"
                else "message.duplicate_content"
            )
            self.record_event(
                event_name,
                message_id=update.message_id,
                peer_id=update.peer_id,
                dedup_key=duplicate.key,
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
        response = await self._handle_with_typing(update, message, context_token=context_token)
        if not response:
            return True
        response_delivery = connector_delivery_from_facts(response.facts)
        if response_delivery is None and not response.text.strip():
            self.record_event(
                "reply.skipped",
                peer_id=update.peer_id,
                reason="empty_response",
                action=response.action,
            )
            try:
                from navi.trace import TraceStore, TracePhase

                TraceStore(self.home).add_event(
                    trace_id=update.message_id,
                    phase=TracePhase.CHANNEL_EGRESS,
                    run_id="",
                    source=self.local_source,
                    peer_id=update.peer_id,
                    sender_id=update.sender_id,
                    output_data={
                        "action": response.action,
                        "reason": "empty_response",
                        "delivery_attempted": False,
                        "media_count": 0,
                    },
                    message="Skipped empty channel response",
                )
                TraceStore(self.home).evaluate_trace(update.message_id)
            except Exception:
                pass
            return True
        try:
            delivery = await self._send_reply(
                account=account,
                peer_id=update.peer_id,
                text=response.text,
                action=response.action,
                facts=response.facts,
                context_token=context_token,
            )
        except Exception as exc:
            self.record_event(
                "reply.error", peer_id=update.peer_id, error=f"{type(exc).__name__}: {exc}"
            )
            failed_delivery = connector_delivery_from_facts(response.facts)
            if failed_delivery is not None and failed_delivery.run_id:
                try:
                    from navi.goals import GoalStore

                    GoalStore(self.home).record_delivery_failure(
                        run_id=failed_delivery.run_id,
                        channel=self.local_source,
                        error=f"{type(exc).__name__}: {exc}",
                        trace_id=update.message_id,
                        delivery_id=failed_delivery.delivery_id,
                    )
                except Exception:
                    pass
            try:
                from navi.trace import TraceStore, TracePhase

                TraceStore(self.home).add_event(
                    trace_id=update.message_id,
                    phase=TracePhase.CHANNEL_EGRESS,
                    run_id=failed_delivery.run_id if failed_delivery is not None else "",
                    source=self.local_source,
                    peer_id=update.peer_id,
                    sender_id=update.sender_id,
                    output_data={
                        "error": f"{type(exc).__name__}: {exc}",
                        "media_count": 0,
                        "delivery_id": (
                            failed_delivery.delivery_id if failed_delivery is not None else ""
                        ),
                    },
                    message="Channel delivery failed",
                    ok=False,
                )
                TraceStore(self.home).evaluate_trace(update.message_id)
            except Exception:
                pass
            raise
        self.record_event(
            "reply.sent",
            peer_id=update.peer_id,
            text_preview=delivery["text_preview"],
            media_count=delivery["media_count"],
        )
        connector_delivery = connector_delivery_from_facts(response.facts)
        delivery_run_id = connector_delivery.run_id if connector_delivery is not None else ""
        if connector_delivery is not None and delivery_run_id:
            try:
                from navi.goals import GoalStore

                GoalStore(self.home).record_delivery(
                    run_id=delivery_run_id,
                    channel=self.local_source,
                    text_preview=str(delivery["text_preview"]),
                    text_length=len(response.text.strip()),
                    media_count=delivery["media_count"],
                    trace_id=update.message_id,
                    delivery_id=connector_delivery.delivery_id,
                )
            except Exception:
                self.record_event(
                    "reply.receipt.error",
                    peer_id=update.peer_id,
                    delivery_id=connector_delivery.delivery_id,
                )
        try:
            from navi.trace import TraceStore, TracePhase

            TraceStore(self.home).add_event(
                trace_id=update.message_id,
                phase=TracePhase.CHANNEL_EGRESS,
                run_id=delivery_run_id,
                source=self.local_source,
                peer_id=update.peer_id,
                sender_id=update.sender_id,
                output_data={
                    "response": delivery["text_preview"],
                    "action": response.action,
                    "media_count": delivery["media_count"],
                    "delivery_id": (
                        connector_delivery.delivery_id if connector_delivery is not None else ""
                    ),
                },
                message="Delivered response to channel",
            )
            TraceStore(self.home).evaluate_trace(update.message_id)
        except Exception:
            pass
        return True

    async def _handle_with_typing(
        self, update: WeixinUpdate, message: ConnectorMessage, *, context_token: str
    ) -> "ResponseReadyEvent | None":
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
            try:
                from navi.trace import TraceStore, TracePhase

                TraceStore(self.home).add_event(
                    trace_id=update.message_id,
                    phase=TracePhase.RESPONSE_READY,
                    source=self.local_source,
                    peer_id=update.peer_id,
                    sender_id=update.sender_id,
                    output_data={"error": str(exc)},
                    message="Failed to prepare channel response",
                    ok=False,
                )
            except Exception:
                pass
            return None
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
        for result in await self.daemon.process_background_once():
            if result.get("surface") is False:
                self.record_event(
                    "background.skipped",
                    background_event="scheduled_run_queued",
                    reason="wait_for_run_completion",
                    run_id=str(result.get("run_id") or ""),
                )
                continue
            await self._surface_background_event(account, result)
        for task in await self.daemon.process_queue_once():
            await self._surface_background_task(account, task)
        await self._drain_delivery_outbox(account)

    async def _surface_background_event(
        self,
        account: WeixinAccount,
        result: dict,
    ) -> None:
        run_id = str(result.get("run_id") or "")
        trace_id = str(result.get("trace_id") or "") or run_id or uuid.uuid4().hex
        peer_id = str(result.get("peer_id") or "") or self.config.home_channel
        if not peer_id:
            return
        event_facts = result.get("facts") if isinstance(result.get("facts"), dict) else {}
        text = await self._compose_event_notification(
            {
                "event": "background_event",
                "facts": event_facts,
                "workspace": str(result.get("workspace") or ""),
                "trace_id": trace_id,
                "run_id": run_id,
                "peer_id": peer_id,
            },
        )
        if not text.strip():
            self.record_event(
                "background.skipped",
                peer_id=peer_id,
                background_event="background_event",
                reason="empty_surface_text",
                run_id=run_id,
            )
            return
        await self._send_reply(
            account=account,
            peer_id=peer_id,
            text=text,
            action="chat",
            facts={},
            context_token=self.context_tokens.get(account.account_id, peer_id),
        )
        self.record_event(
            "background.sent",
            peer_id=peer_id,
            background_event="background_event",
            text_preview=text[:120],
        )
        try:
            from navi.trace import TracePhase, TraceStore

            TraceStore(self.home).add_event(
                trace_id=trace_id,
                phase=TracePhase.CHANNEL_EGRESS,
                run_id=run_id,
                source=self.local_source,
                peer_id=peer_id,
                output_data={
                    "response": text,
                    "background_event": "background_event",
                },
                message="Sent background event notification to channel",
            )
        except Exception:
            pass

    async def _surface_background_task(
        self,
        account: WeixinAccount,
        task: Run,
    ) -> None:
        if not task.peer_id:
            return
        task_facts = self._background_task_facts(task)
        has_notify_input = bool(
            str(task_facts.get("error") or "").strip()
            or task.resolution not in {"", "none", "success"}
        )
        if not has_notify_input:
            self.record_event(
                "background.skipped",
                peer_id=task.peer_id,
                background_event="background_task_result",
                reason="empty_surface_text",
                run_id=task.id,
                phase=task.phase,
                governance=task.governance,
                resolution=task.resolution,
            )
            return
        decision_text = await self._compose_event_notification(
            {
                "event": "background_task_result",
                "facts": task_facts,
                "workspace": task.workspace,
                "trace_id": task.id,
                "run_id": task.id,
                "peer_id": task.peer_id,
            },
        )
        if not decision_text.strip():
            self.record_event(
                "background.skipped",
                peer_id=task.peer_id,
                background_event="background_task_result",
                reason="notification_declined",
                run_id=task.id,
                phase=task.phase,
                governance=task.governance,
                resolution=task.resolution,
            )
            return
        text = decision_text
        delivery = await self._send_reply(
            account=account,
            peer_id=task.peer_id,
            text=text,
            action="chat",
            facts={},
            context_token=self.context_tokens.get(account.account_id, task.peer_id),
        )
        self.record_event(
            "background.sent",
            peer_id=task.peer_id,
            background_event="background_task_result",
            text_preview=delivery["text_preview"],
            media_count=delivery["media_count"],
        )
        try:
            from navi.trace import TracePhase, TraceStore

            TraceStore(self.home).add_event(
                trace_id=task.id,
                phase=TracePhase.CHANNEL_EGRESS,
                run_id=task.id,
                source=self.local_source,
                peer_id=task.peer_id,
                output_data={
                    "response": text,
                    "background_event": "background_task_result",
                    "media_count": delivery["media_count"],
                },
                message="Sent background task result to channel",
            )
        except Exception:
            pass

    async def _drain_delivery_outbox(self, account: WeixinAccount) -> None:
        store = GoalStore(self.home)
        for item in store.claim_pending_delivery_outbox(channel=self.local_source, limit=10):
            try:
                await self._send_delivery_outbox_item(account, item)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                store.mark_delivery_outbox_failed(item.id, error=error)
                if item.attempts >= 3:
                    store.record_delivery_failure(
                        run_id=item.run_id,
                        channel=self.local_source,
                        error=error,
                        trace_id=item.trace_id,
                        delivery_id=item.id,
                    )
                self.record_event(
                    "background.delivery_outbox.error",
                    peer_id=item.peer_id,
                    run_id=item.run_id,
                    error=error,
                )

    async def _send_delivery_outbox_item(
        self,
        account: WeixinAccount,
        item: GoalDeliveryOutboxItem,
    ) -> None:
        delivery = await self._send_reply(
            account=account,
            peer_id=item.peer_id,
            text=item.body,
            action="chat",
            facts={},
            context_token=self.context_tokens.get(account.account_id, item.peer_id),
        )
        store = GoalStore(self.home)
        store.record_delivery(
            run_id=item.run_id,
            channel=self.local_source,
            text_preview=str(delivery.get("text_preview") or ""),
            text_length=len(item.body.strip()),
            media_count=delivery["media_count"],
            trace_id=item.trace_id,
            delivery_id=item.id,
        )
        store.mark_delivery_outbox_sent(item.id, delivery_id=item.id)
        self.record_event(
            "background.sent",
            peer_id=item.peer_id,
            background_event="accepted_result_delivery",
            text_preview=delivery["text_preview"],
            media_count=delivery["media_count"],
            run_id=item.run_id,
        )
        try:
            from navi.trace import TracePhase, TraceStore

            TraceStore(self.home).add_event(
                trace_id=item.trace_id,
                phase=TracePhase.CHANNEL_EGRESS,
                run_id=item.run_id,
                source=self.local_source,
                peer_id=item.peer_id,
                output_data={
                    "response": item.body,
                    "background_event": "accepted_result_delivery",
                    "media_count": delivery["media_count"],
                    "outbox_id": item.id,
                    "body_provenance": item.body_provenance,
                },
                message="Sent accepted background result to channel",
            )
        except Exception:
            pass

    def _background_task_facts(self, task: Run) -> dict[str, object]:
        return {
            "kind": "background_task_result",
            "run_id": task.id,
            "title": task.title,
            "phase": task.phase,
            "governance": task.governance,
            "acceptance": task.acceptance,
            "resolution": task.resolution,
            "source": task.source,
            "peer_id": task.peer_id,
            "sender_id": task.sender_id,
            "workspace": task.workspace,
            "error": str(task.error or ""),
        }

    async def _compose_event_notification(self, facts: dict) -> str:
        if facts.get("event") not in {"background_event", "background_task_result"}:
            return ""
        event_facts = facts.get("facts")
        if not isinstance(event_facts, dict) or not event_facts:
            return ""
        notification_facts = dict(facts)
        try:
            decision = await synthesize_background_notification(
                self.runtime,
                facts=notification_facts,
                output_schema=_BACKGROUND_NOTIFICATION_SCHEMA,
            )
            self._record_notification_role_result(
                facts=facts,
                verified_facts=decision.verified_facts,
                notify=decision.notify,
                message=decision.message,
            )
            if not decision.notify:
                return ""
            return decision.message
        except Exception as exc:
            self.record_event(
                "background.notification.error",
                trace_id=str(facts.get("trace_id") or ""),
                run_id=str(facts.get("run_id") or ""),
                error=f"{type(exc).__name__}: {exc}",
            )
            return ""

    def _record_notification_role_result(
        self,
        *,
        facts: dict,
        verified_facts: dict,
        notify: bool,
        message: str,
    ) -> None:
        try:
            from navi.trace import TracePhase, TraceStore

            TraceStore(self.home).add_event(
                trace_id=str(facts.get("trace_id") or "") or TraceStore.new_trace_id(),
                phase=TracePhase.AGENT_ROLE_RESULT,
                run_id=str(facts.get("run_id") or ""),
                source=self.local_source,
                peer_id=str(facts.get("peer_id") or ""),
                model_role="notification",
                input_data=verified_facts,
                output_data={"notify": notify, "message": message},
                message="Notification model evaluated verified background facts",
            )
        except Exception as exc:
            self.record_event(
                "background.notification.trace_error",
                trace_id=str(facts.get("trace_id") or ""),
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _send_reply(
        self,
        *,
        account: WeixinAccount,
        peer_id: str,
        text: str,
        action: str = "chat",
        facts: dict | None = None,
        context_token: str,
    ) -> DeliveryReceipt:
        facts = facts or {}
        delivery = connector_delivery_from_facts(facts)
        if action == "connector_outbound" and delivery is None:
            raise RuntimeError("connector_outbound response is missing a valid delivery contract")
        sent_media = 0
        outbound_text = (delivery.text if delivery is not None else text).strip()
        if not outbound_text and delivery is None:
            raise RuntimeError("refusing to record an empty connector response as delivered")
        if outbound_text:
            await self.client.send_message(
                account_id=account.account_id,
                peer_id=peer_id,
                text=outbound_text,
                context_token=context_token,
            )
        if delivery is not None:
            file_path = Path(delivery.path).expanduser().resolve()
            if not file_path.is_file():
                raise FileNotFoundError(f"connector delivery file not found: {file_path}")
            await self.client.send_file(
                account_id=account.account_id,
                peer_id=peer_id,
                file_path=file_path,
                context_token=context_token,
                idempotency_key=delivery.delivery_id,
            )
            sent_media = 1
            self.record_event(
                "reply.media.sent",
                peer_id=peer_id,
                path=str(file_path),
                delivery_id=delivery.delivery_id,
                media_count=sent_media,
            )
        return {"media_count": sent_media, "text_preview": outbound_text[:120]}

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
    from ..safeguards import _REDACT_FIELD_NAMES, redact_secrets

    redacted = {}
    free_text_keys = ("text_preview", "error", "message", "raw_result", "detail")
    for key, value in facts.items():
        key_text = str(key).lower()
        if key_text in _REDACT_FIELD_NAMES:
            redacted[key] = "[redacted]"
        elif key_text in free_text_keys and isinstance(value, str):
            redacted[key] = redact_secrets(value)
        else:
            redacted[key] = value
    return redacted


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
