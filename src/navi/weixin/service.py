from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from collections.abc import Callable
from typing import Any, TypedDict

from navi.connector_runtime import (
    ConnectorIngressDeduplicator,
    ConnectorIngressRuntime,
    ConnectorMessage,
)
from navi.connector_delivery import connector_delivery_from_facts
from navi.delivery_outbox import (
    DeliveryCoordinator,
    DeliveryOutboxStore,
    DeliveryOutcome,
    envelope_from_response,
)
from navi.event_bus import ResponseReadyEvent
from navi.finalization import synthesize_background_notification
from navi.goals import GoalStore
from navi.loop_runs import LoopRunStore
from navi.lifecycle import Phase
from navi.runtime import AgentRuntime
from navi.runs import Run
from navi.trace import TracePhase, TraceStore
from navi.daemon import SystemDaemon

from .client import TYPING_START, TYPING_STOP, WeixinClient
from .config import WeixinConfig
from .delivery import WeixinDeliveryTransport
from .models import WeixinAccount, WeixinUpdate

from .store import WeixinStore


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
        self.dedup = ConnectorIngressDeduplicator(home)
        self.client = client if client is not None else self._build_client()
        self.delivery_outbox = DeliveryOutboxStore(home)
        self.delivery_coordinator = DeliveryCoordinator(self.delivery_outbox)
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
                    f"Set connectors.weixin.account_id={account.account_id} in config.yaml."
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Weixin setup timed out: qr_url={qr.qrcode_url}")
            await asyncio.sleep(1)

    def update_status(self, status: str, error: str = "") -> None:
        status_dir = self.home / "weixin"
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

    def record_event(self, event: str, **facts) -> None:
        event_dir = self.home / "weixin"
        event_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.time(),
            "event": event,
            **_redact_event_facts(facts),
        }
        with (event_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    async def run(self, *, once: bool = False) -> None:
        import time

        account = self._resolve_account()
        sync_buf = self.store.load_sync_buf(account.account_id)
        sleep_time = 1.0
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

                self.update_status("healthy")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                error_msg = str(e)
                self.record_event("poll.error", error=error_msg)
                self.update_status("degraded", error_msg)
                if once:
                    raise
                sleep_time = min(30.0, max(1.0, sleep_time * 2.0))
                await asyncio.sleep(sleep_time)
                continue

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
        context_token = update.context_token
        response = await self._handle_with_typing(update, message, context_token=context_token)
        if not response:
            raise RuntimeError("channel response is empty")
        response_delivery = connector_delivery_from_facts(response.facts)
        if response_delivery is None and not response.text.strip():
            self.record_event(
                "reply.failed",
                peer_id=update.peer_id,
                reason="empty_response",
                action=response.action,
            )
            try:
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
                    message="Channel response failed because it was empty",
                    ok=False,
                )
                TraceStore(self.home).evaluate_trace(update.message_id)
            except Exception as trace_exc:
                self.record_event(
                    "trace.error",
                    trace_id=update.message_id,
                    operation="record_empty_response",
                    error=f"{type(trace_exc).__name__}: {trace_exc}",
                )
            raise RuntimeError("channel response text is empty")
        items = self.delivery_outbox.enqueue(
            envelope_from_response(
                channel=self.local_source,
                peer_id=update.peer_id,
                sender_id=update.sender_id,
                trace_id=update.message_id,
                text=response.text,
                connector_delivery=response_delivery,
                body_provenance="response_ready",
                transport_context={"context_token": context_token},
            )
        )
        self.record_event(
            "reply.queued",
            peer_id=update.peer_id,
            outbox_ids=[item.id for item in items],
            media_count=sum(1 for item in items if item.kind == "file"),
        )
        await self._drain_delivery_outbox(account)
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
            self.update_status("fatal", f"message handler failed: {type(exc).__name__}: {exc}")
            self.record_event(
                "handler.error",
                peer_id=update.peer_id,
                sender_id=update.sender_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            try:
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
            except Exception as trace_exc:
                self.record_event(
                    "trace.error",
                    trace_id=update.message_id,
                    operation="record_handler_failure",
                    error=f"{type(trace_exc).__name__}: {trace_exc}",
                )
            raise
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
        ticket = await self.client.get_typing_ticket(user_id=sender_id, context_token=context_token)
        if ticket:
            self.typing_tickets[sender_id] = ticket
            self.record_event("typing.ticket", sender_id=sender_id)
        return ticket

    async def _keep_typing(
        self, peer_id: str, typing_ticket: str, stop_event: asyncio.Event
    ) -> None:
        try:
            while not stop_event.is_set():
                try:
                    await self._send_typing(peer_id, typing_ticket, TYPING_START)
                except Exception as exc:
                    self._record_typing_error(peer_id, TYPING_START, exc)
                    break
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=3.0)
                except TimeoutError:
                    continue
        finally:
            try:
                await self._send_typing(peer_id, typing_ticket, TYPING_STOP)
            except Exception as exc:
                self._record_typing_error(peer_id, TYPING_STOP, exc)

    async def _send_typing(self, peer_id: str, typing_ticket: str, status: int) -> None:
        await asyncio.wait_for(
            self.client.send_typing(peer_id=peer_id, typing_ticket=typing_ticket, status=status),
            timeout=1.5,
        )
        self.record_event("typing.sent", peer_id=peer_id, status=status)

    def _record_typing_error(self, peer_id: str, status: int, exc: Exception) -> None:
        self.record_event(
            "typing.error",
            peer_id=peer_id,
            status=status,
            error=f"{type(exc).__name__}: {exc}",
        )

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
        del account
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
        items = self.delivery_outbox.enqueue(
            envelope_from_response(
                channel=self.local_source,
                peer_id=peer_id,
                sender_id="",
                trace_id=trace_id,
                text=text,
                run_id=run_id,
                body_provenance="background_notification",
            )
        )
        self.record_event(
            "background.queued",
            peer_id=peer_id,
            background_event="background_event",
            text_preview=text[:120],
            outbox_ids=[item.id for item in items],
        )

    async def _surface_background_task(
        self,
        account: WeixinAccount,
        task: Run,
    ) -> None:
        del account
        if not task.peer_id:
            return
        task_facts = self._background_task_facts(task)
        if _is_transient_background_resource_pause(task_facts):
            self.record_event(
                "background.skipped",
                peer_id=task.peer_id,
                background_event="background_task_result",
                reason="transient_resource_pause",
                run_id=task.id,
            )
            return
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
        items = self.delivery_outbox.enqueue(
            envelope_from_response(
                channel=self.local_source,
                peer_id=task.peer_id,
                sender_id=task.sender_id,
                trace_id=task.id,
                text=text,
                body_provenance="background_notification",
            )
        )
        self.record_event(
            "background.queued",
            peer_id=task.peer_id,
            background_event="background_task_result",
            text_preview=text[:120],
            outbox_ids=[item.id for item in items],
        )

    async def _drain_delivery_outbox(self, account: WeixinAccount) -> None:
        transport = WeixinDeliveryTransport(
            client=self.client,
            account=account,
            channel=self.local_source,
        )
        outcomes = await self.delivery_coordinator.drain(transport, limit=10)
        for outcome in outcomes:
            self._record_delivery_outbox_outcome(outcome)
        self._project_completed_delivery_batches()

    def _record_delivery_outbox_outcome(self, outcome: DeliveryOutcome) -> None:
        item = outcome.item
        payload: dict[str, Any] = {
            "outbox_id": item.id,
            "batch_id": item.batch_id,
            "kind": item.kind,
            "attempts": item.attempts,
        }
        ok = outcome.state == "sent"
        if outcome.receipt is not None:
            payload["receipt"] = outcome.receipt.to_dict()
        if outcome.failure is not None:
            payload["error_reason"] = outcome.failure.reason
            payload["error"] = outcome.failure.error
            payload["retry_scheduled"] = outcome.state == "retry_scheduled"
        self.record_event(
            "reply.sent"
            if ok
            else "reply.deferred"
            if outcome.state == "retry_scheduled"
            else "reply.error",
            peer_id=item.peer_id,
            run_id=item.run_id,
            **payload,
        )
        try:
            trace = TraceStore(self.home)
            trace.add_event(
                trace_id=item.trace_id,
                phase=TracePhase.CHANNEL_EGRESS,
                run_id=item.run_id,
                source=self.local_source,
                peer_id=item.peer_id,
                sender_id=item.sender_id,
                ok=ok,
                output_data=payload,
                message=(
                    "Connector delivery item accepted"
                    if ok
                    else "Connector delivery retry scheduled"
                    if outcome.state == "retry_scheduled"
                    else "Connector delivery item failed"
                ),
            )
            if outcome.state != "retry_scheduled":
                trace.evaluate_trace(item.trace_id)
        except Exception as trace_exc:
            self.record_event(
                "trace.error",
                trace_id=item.trace_id,
                operation="record_delivery_outbox_outcome",
                error=f"{type(trace_exc).__name__}: {trace_exc}",
            )
        if outcome.state == "failed" and item.goal_id and item.run_id:
            try:
                GoalStore(self.home).record_delivery_failure(
                    run_id=item.run_id,
                    channel=self.local_source,
                    error=f"{outcome.failure.reason}: {outcome.failure.error}"
                    if outcome.failure
                    else item.error,
                    error_reason=outcome.failure.reason
                    if outcome.failure
                    else "connector_delivery_failed",
                    trace_id=item.trace_id,
                    delivery_id=item.batch_id,
                )
            except Exception as projection_exc:
                self.record_event(
                    "delivery_state.error",
                    run_id=item.run_id,
                    operation="project_failed_delivery",
                    error=f"{type(projection_exc).__name__}: {projection_exc}",
                )

    def _project_completed_delivery_batches(self) -> None:
        goals = GoalStore(self.home)
        for batch_id, items in self.delivery_outbox.complete_unprojected_batches(
            channel=self.local_source,
            limit=100,
        ):
            anchor = items[0]
            try:
                if anchor.goal_id and anchor.run_id:
                    goal = goals.get(anchor.goal_id)
                    if goal is not None and goal.phase != Phase.ENDED:
                        text = next((item.body for item in items if item.kind == "text"), "")
                        media_count = sum(
                            int(item.receipt.get("media_count") or 0) for item in items
                        )
                        goals.record_delivery(
                            run_id=anchor.run_id,
                            channel=self.local_source,
                            text_preview=text[:200],
                            text_length=len(text),
                            media_count=media_count,
                            trace_id=anchor.trace_id,
                            delivery_id=batch_id,
                            sent_at=max(item.sent_at for item in items),
                        )
                        self.record_event(
                            "background.sent",
                            peer_id=anchor.peer_id,
                            background_event="accepted_result_delivery",
                            text_preview=text[:120],
                            media_count=media_count,
                            run_id=anchor.run_id,
                            batch_id=batch_id,
                        )
                self.delivery_outbox.mark_batch_projected(batch_id)
            except Exception as projection_exc:
                self.record_event(
                    "delivery_state.error",
                    run_id=anchor.run_id,
                    operation="project_completed_delivery_batch",
                    error=f"{type(projection_exc).__name__}: {projection_exc}",
                )

    def _background_task_facts(self, task: Run) -> dict[str, object]:
        facts: dict[str, object] = {
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
        goal = GoalStore(self.home).get_by_run(task.id)
        if goal is None:
            return facts
        facts["goal_id"] = goal.id
        facts["goal_state"] = {
            "phase": goal.phase,
            "acceptance": goal.acceptance,
            "resolution": goal.resolution,
            "task_status": goal.task_status,
            "blocked_reason": goal.blocked_reason,
        }
        loop_runs = LoopRunStore(self.home).list_by_goal(goal.id, limit=1)
        if loop_runs:
            facts["loop_diagnostics"] = _background_loop_diagnostics(loop_runs[0])
        return facts

    async def _compose_event_notification(self, facts: dict) -> str:
        if facts.get("event") not in {"background_event", "background_task_result"}:
            return ""
        event_facts = facts.get("facts")
        if not isinstance(event_facts, dict) or not event_facts:
            return ""
        notification_facts = dict(facts)
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

    def _record_notification_role_result(
        self,
        *,
        facts: dict,
        verified_facts: dict,
        notify: bool,
        message: str,
    ) -> None:
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
        """Compatibility-free diagnostic helper backed by the shared outbox.

        Production paths enqueue first and drain from their normal loop.  This
        helper keeps the opt-in connector smoke on that exact protocol while
        requiring a receipt before it returns.
        """
        facts = facts or {}
        delivery = connector_delivery_from_facts(facts)
        if action == "connector_outbound" and delivery is None:
            raise RuntimeError("connector_outbound response is missing a valid delivery contract")
        outbound_text = (delivery.text if delivery is not None else text).strip()
        if not outbound_text and delivery is None:
            raise RuntimeError("refusing to record an empty connector response as delivered")
        trace_id = str(getattr(delivery, "delivery_id", "") or "") or TraceStore.new_trace_id()
        items = self.delivery_outbox.enqueue(
            envelope_from_response(
                channel=self.local_source,
                peer_id=peer_id,
                sender_id="",
                trace_id=trace_id,
                text=text,
                connector_delivery=delivery,
                body_provenance="connector_smoke",
                transport_context={"context_token": context_token},
            )
        )
        await self._drain_delivery_outbox(account)
        batch = self.delivery_outbox.list_batch(items[0].batch_id)
        if not batch or not all(item.status == "sent" for item in batch):
            error = next(
                (item.error for item in batch if item.error), "delivery receipt is pending"
            )
            raise RuntimeError(error)
        return {
            "media_count": sum(int(item.receipt.get("media_count") or 0) for item in batch),
            "text_preview": outbound_text[:120],
        }

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


def _background_loop_diagnostics(state: Any) -> dict[str, object]:
    """Project bounded persisted failure facts for the notification model."""
    evidence = state.evidence if isinstance(state.evidence, dict) else {}
    recovery: dict[str, Any] = {}
    facts = evidence.get("facts")
    if isinstance(facts, dict) and isinstance(facts.get("recovery"), dict):
        recovery = facts["recovery"]

    diagnostics: dict[str, object] = {
        "loop_run_id": state.run_id,
        "node": str(state.node),
        "terminal_state": str(state.terminal_state),
        "attempt": state.attempt,
        "reason_code": str(evidence.get("reason_code") or recovery.get("reason_code") or ""),
        "failure_domain": str(recovery.get("failure_domain") or ""),
        "reason": str(evidence.get("reason") or ""),
        "repeat_count": int(evidence.get("repeat_count") or 0),
    }
    resource_grant = evidence.get("resource_grant")
    if isinstance(resource_grant, dict):
        diagnostics["resource_grant"] = resource_grant
        diagnostics["resource_pause_reason"] = str(resource_grant.get("reason") or "")
        diagnostics["resource_retry_after_seconds"] = max(
            0.0,
            float(resource_grant.get("retry_after_seconds") or 0.0),
        )

    raw_checker_results = evidence.get("checker_results")
    if isinstance(raw_checker_results, list):
        checker_results: list[dict[str, object]] = []
        for raw in raw_checker_results[-3:]:
            if not isinstance(raw, dict):
                continue
            checker_evidence = raw.get("evidence")
            checker_facts = checker_evidence if isinstance(checker_evidence, dict) else {}
            checker_results.append(
                {
                    "name": str(raw.get("name") or ""),
                    "passed": bool(raw.get("passed", False)),
                    "reason": str(raw.get("reason") or ""),
                    "evaluator_role": str(checker_facts.get("evaluator_role") or ""),
                    "evidence_summary": str(checker_facts.get("evidence_summary") or "")[:2000],
                }
            )
        diagnostics["checker_results"] = checker_results

    executor = evidence.get("executor")
    if isinstance(executor, dict):
        diagnostics["last_capability"] = {
            "action": str(executor.get("action") or ""),
            "ok": bool(executor.get("ok", False)),
            "error_reason": str(executor.get("error_reason") or ""),
            "terminal": bool(executor.get("terminal", False)),
            "facts": executor.get("facts") if isinstance(executor.get("facts"), dict) else {},
        }
    return diagnostics


def _is_transient_background_resource_pause(task_facts: dict[str, object]) -> bool:
    from ..loop_runs import is_retryable_resource_pause

    diagnostics = task_facts.get("loop_diagnostics")
    if not isinstance(diagnostics, dict):
        return False
    return str(diagnostics.get("terminal_state") or "") == "paused" and is_retryable_resource_pause(
        diagnostics.get("resource_grant")
    )


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


def _connector_error_reason(exc: Exception) -> str:
    reason = str(getattr(exc, "reason", "") or "").strip()
    return reason if reason.startswith("connector_") else "connector_delivery_failed"
