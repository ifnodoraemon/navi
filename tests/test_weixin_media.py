from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from navi.approval_contract import APPROVAL_ACTION_CAPABILITY, APPROVAL_DECISION_APPROVE
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.connector_delivery import ConnectorDelivery, connector_delivery_client_id
from navi.event_bus import ResponseReadyEvent
from navi.goals import GoalStore
from navi.lifecycle import Acceptance, Governance, Phase, Resolution
from navi.loop_contracts import LoopTerminalState
from navi.loop_control_service import LoopControlService, OpenGoalRequest
from navi.loop_runs import LoopRunStore
from navi.runtime import AgentRuntime
from navi.runs import Run, RunStore
from navi.trace import TraceStore
from navi.weixin.client import ITEM_FILE, MEDIA_FILE, TYPING_START, TYPING_STOP, WeixinClient
from navi.weixin.config import WeixinConfig
from navi.weixin.models import WeixinAccount, WeixinUpdate
from navi.weixin.service import WeixinService
from navi.weixin.store import WeixinStore


class NoModelCalls:
    async def complete_for(self, role: str, messages: list[Any], **kwargs: Any) -> str:
        raise AssertionError(f"unexpected model call: {role}")

    def list_roles(self) -> list[str]:
        return []


def test_weixin_store_rejects_corrupt_sync_cursor(tmp_path: Path):
    store = WeixinStore(tmp_path)
    store.sync_path("acct").write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError):
        store.load_sync_buf("acct")


class WatchNotificationProvider:
    def __init__(self, *, notify: bool, message: str) -> None:
        self.notify = notify
        self.message = message
        self.calls: list[tuple[str, list[Any], dict[str, Any]]] = []

    async def complete_for(self, role: str, messages: list[Any], **kwargs: Any) -> str:
        self.calls.append((role, messages, kwargs))
        return json.dumps({"notify": self.notify, "message": self.message})

    def list_roles(self) -> list[str]:
        return ["notification"]


class BareApprovedFileProvider:
    """Model path for a file request followed by a bare approval turn."""

    def __init__(self, target: Path) -> None:
        self.target = target
        self.approval_run_id = ""
        self.planner_calls = 0
        self.calls: list[str] = []

    async def complete_for(self, role: str, messages: list[Any], **kwargs: Any) -> str:
        del messages, kwargs
        self.calls.append(role)
        if role == "planner":
            self.planner_calls += 1
            if self.planner_calls == 1:
                return json.dumps(
                    {
                        "syscalls": [
                            {
                                "tool": "channel.send_file",
                                "permission": "write",
                                "args": {
                                    "path": str(self.target),
                                    "text": "这是你要的文件。",
                                },
                                "reason": "deliver the requested file",
                            }
                        ]
                    }
                )
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "approval.resolve",
                            "permission": "prepare",
                            "args": {
                                "decision": "approve",
                                "run_id": self.approval_run_id,
                            },
                            "reason": "apply the user's explicit approval",
                        }
                    ]
                }
            )
        if role == "checker":
            return json.dumps(
                {
                    "passed": True,
                    "should_continue": False,
                    "evidence_summary": "delivery contract is ready",
                }
            )
        if role == "responder":
            raise AssertionError("structured delivery must not be replaced by model prose")
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "checker", "responder"]

    def usage_for(self, role: str) -> dict[str, Any]:
        del role
        return {}


@pytest.mark.asyncio
async def test_get_updates_keeps_file_only_messages_as_attachment(monkeypatch: pytest.MonkeyPatch):
    client = WeixinClient(base_url="https://ilink.example", token="token")

    async def fake_post(path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        assert path == "/ilink/bot/getupdates"
        return {
            "msgs": [
                {
                    "message_id": "msg-file",
                    "from_user_id": "wx-user",
                    "item_list": [
                        {
                            "type": ITEM_FILE,
                            "file_item": {
                                "file_name": "report.pdf",
                                "len": "42",
                                "media": {"encrypt_query_param": "encrypted-media"},
                            },
                        }
                    ],
                }
            ],
            "get_updates_buf": "next",
        }

    monkeypatch.setattr(client, "_post", fake_post)

    batch = await client.get_updates("acct", sync_buf="prev")

    assert batch.sync_buf == "next"
    assert len(batch.updates) == 1
    update = batch.updates[0]
    assert update.text == ""
    assert update.attachments[0].kind == "file"
    assert update.attachments[0].file_name == "report.pdf"
    assert update.attachments[0].media_id == "encrypted-media"


@pytest.mark.asyncio
async def test_send_message_propagates_first_provider_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = WeixinClient(base_url="https://ilink.example", token="token")
    calls = 0

    async def fake_post(path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        nonlocal calls
        del payload, timeout
        assert path == "/ilink/bot/sendmessage"
        calls += 1
        return {"ret": -2, "errcode": -2, "errmsg": "rate limited"}

    monkeypatch.setattr(client, "_post", fake_post)

    with pytest.raises(RuntimeError, match="rate limited"):
        await client.send_message(
            account_id="acct",
            peer_id="wx-user",
            text="hello",
            context_token="ctx",
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_send_file_uploads_cdn_media_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF-1.4 test")
    client = WeixinClient(
        base_url="https://ilink.example",
        cdn_base_url="https://cdn.example/c2c",
        token="token",
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_post(path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        del timeout
        calls.append((path, payload))
        if path == "/ilink/bot/getuploadurl":
            assert payload["media_type"] == MEDIA_FILE
            assert payload["to_user_id"] == "wx-user"
            assert payload["rawsize"] == target.stat().st_size
            return {"upload_param": "upload-param"}
        if path == "/ilink/bot/sendmessage":
            return {"ret": 0, "errcode": 0}
        raise AssertionError(f"unexpected path: {path}")

    async def fake_upload_ciphertext(*, upload_url: str, ciphertext: bytes) -> str:
        assert upload_url.startswith("https://cdn.example/c2c/upload?")
        assert len(ciphertext) >= target.stat().st_size
        return "encrypted-query"

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(client, "_upload_ciphertext", fake_upload_ciphertext)

    await client.send_file(
        account_id="acct",
        peer_id="wx-user",
        file_path=target,
        context_token="ctx",
        idempotency_key="delivery-1",
    )

    send_payload = calls[-1][1]["msg"]
    assert send_payload["to_user_id"] == "wx-user"
    assert send_payload["context_token"] == "ctx"
    assert send_payload["client_id"] == connector_delivery_client_id(
        "delivery-1", prefix="navi-weixin"
    )
    item = send_payload["item_list"][0]
    assert item["type"] == ITEM_FILE
    assert item["file_item"]["file_name"] == "report.pdf"
    assert item["file_item"]["len"] == str(target.stat().st_size)
    assert item["file_item"]["media"]["encrypt_query_param"] == "encrypted-query"


class CaptureWeixinClient:
    def __init__(self) -> None:
        self.files: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    async def get_typing_ticket(self, *, user_id: str, context_token: str = "") -> str:
        return ""

    async def send_file(self, **kwargs: Any) -> None:
        self.files.append(kwargs)

    async def send_message(self, **kwargs: Any) -> None:
        self.messages.append(kwargs)


class FailingFileWeixinClient(CaptureWeixinClient):
    async def send_file(self, **kwargs: Any) -> None:
        self.files.append(kwargs)
        raise RuntimeError("upload failed")


class FailingTypingWeixinClient(CaptureWeixinClient):
    async def send_typing(self, **kwargs: Any) -> None:
        raise RuntimeError(f"typing unavailable: {kwargs['status']}")


class StaticIngress:
    def __init__(
        self,
        text: str,
        *,
        action: str = "chat",
        facts: dict[str, Any] | None = None,
    ) -> None:
        self.text = text
        self.action = action
        self.facts = facts or {}

    async def handle(self, message: Any) -> "ResponseReadyEvent":
        return ResponseReadyEvent(
            text=self.text,
            source="weixin",
            action=self.action,
            facts=self.facts,
        )


@pytest.mark.asyncio
async def test_typing_failures_are_nonfatal_but_traceable(tmp_path: Path) -> None:
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=FailingTypingWeixinClient(),
    )

    await service._keep_typing("wx-user", "ticket", asyncio.Event())

    events = [
        json.loads(line)
        for line in (tmp_path / "weixin" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    typing_errors = [event for event in events if event["event"] == "typing.error"]
    assert [event["status"] for event in typing_errors] == [TYPING_START, TYPING_STOP]
    assert all("RuntimeError: typing unavailable" in event["error"] for event in typing_errors)


class StaticDaemon:
    def __init__(self, tasks: list[Run], watch_results: list[dict[str, Any]] | None = None) -> None:
        self._tasks = tasks
        self._watch_results = list(watch_results or [])
        self.runs = {task.id: task for task in tasks}

    async def process_background_once(self) -> list[dict[str, Any]]:
        return self._watch_results

    async def process_queue_once(self) -> list[Run]:
        return self._tasks


@pytest.mark.asyncio
async def test_service_executes_structured_delivery_from_original_path(tmp_path: Path):
    report = tmp_path / "report.pdf"
    report.write_bytes(b"report")
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    contract = ConnectorDelivery(
        path=str(report.resolve()),
        text="已生成报告。",
        delivery_id="delivery-report",
    )
    service.ingress = StaticIngress(
        contract.text,
        action="connector_outbound",
        facts={"connector_delivery": contract.to_dict()},
    )

    handled = await service.handle_update(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example"),
        WeixinUpdate(
            message_id="msg-1",
            peer_id="wx-user",
            sender_id="wx-user",
            text="发报告",
            context_token="ctx",
        ),
    )

    assert handled is True
    assert client.files[0]["file_path"] == report.resolve()
    assert client.files[0]["context_token"] == "ctx"
    assert client.files[0]["idempotency_key"] == "delivery-report"
    assert client.messages[0]["text"] == "已生成报告。"
    assert not (tmp_path / "weixin" / "outbox").exists()


@pytest.mark.asyncio
async def test_service_records_empty_runtime_response_as_failure(tmp_path: Path):
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.ingress = StaticIngress(
        "",
        action="error",
        facts={"error_reason": "internal_error"},
    )

    with pytest.raises(RuntimeError, match="channel response text is empty"):
        await service.handle_update(
            WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example"),
            WeixinUpdate(
                message_id="msg-empty-runtime-response",
                peer_id="wx-user",
                sender_id="wx-user",
                text="你好",
                context_token="ctx",
            ),
        )

    assert client.messages == []
    assert client.files == []
    events = (tmp_path / "weixin" / "events.jsonl").read_text(encoding="utf-8")
    assert '"event": "reply.failed"' in events
    assert '"reason": "empty_response"' in events
    assert '"event": "reply.error"' not in events
    egress_events = [
        event
        for event in TraceStore(tmp_path).list_events("msg-empty-runtime-response")
        if event.phase == "channel.egress"
    ]
    assert len(egress_events) == 1
    assert egress_events[0].ok is False
    assert egress_events[0].message == "Channel response failed because it was empty"
    egress_output = json.loads(egress_events[0].output_json)
    assert egress_output["delivery_attempted"] is False


@pytest.mark.asyncio
async def test_service_propagates_ingress_failure(tmp_path: Path):
    class FailingIngress:
        async def handle(self, message: Any):
            del message
            raise RuntimeError("agent ingress failed")

    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=CaptureWeixinClient(),
    )
    service.ingress = FailingIngress()

    with pytest.raises(RuntimeError, match="agent ingress failed"):
        await service.handle_update(
            WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example"),
            WeixinUpdate(
                message_id="msg-ingress-failure",
                peer_id="wx-user",
                sender_id="wx-user",
                text="你好",
                context_token="ctx",
            ),
        )

    status = json.loads((tmp_path / "weixin" / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "fatal"
    assert status["error"] == "message handler failed: RuntimeError: agent ingress failed"


@pytest.mark.asyncio
async def test_background_outbox_sends_accepted_result_verbatim(tmp_path: Path):
    resume = tmp_path / "resume.docx"
    resume.write_bytes(b"resume")
    body = f"MEDIA:{resume}\nHere is your resume file found in the home directory."
    run = RunStore(tmp_path).create(
        "send resume",
        kind="loop:durable_goal",
        source="weixin",
        peer_id="wx-user",
        sender_id="wx-user",
        workspace=str(tmp_path),
    )
    goal_store = GoalStore(tmp_path)
    goal = goal_store.create(
        objective=run.prompt,
        workspace=str(tmp_path),
        source=run.source,
        peer_id=run.peer_id,
        sender_id=run.sender_id,
        run_id=run.id,
    )
    goal_store.record_result_delivery_outbox(
        run=run,
        goal=goal,
        body=body,
        body_provenance="state_graph.evidence.responded_message",
        channel="weixin",
        trace_id=run.id,
    )
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.daemon = StaticDaemon([])

    await service.process_background(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    )

    delivery = GoalStore(tmp_path).latest_delivery(goal.id)
    assert client.files == []
    assert client.messages[0]["text"] == body
    assert delivery["state_transition"] == "delivered"
    assert delivery["text_length"] == len(body)
    events = (tmp_path / "weixin" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert not any('"event": "reply.media.sent"' in line for line in events)
    assert any(
        '"event": "background.sent"' in line
        and '"background_event": "accepted_result_delivery"' in line
        for line in events
    )


@pytest.mark.asyncio
async def test_background_send_records_delivery_fact_after_client_success(tmp_path: Path):
    body = "Lesson 2: supervised learning."
    run = RunStore(tmp_path).create(
        "scheduled lesson",
        kind="loop:durable_goal",
        source="weixin",
        peer_id="wx-user",
        sender_id="wx-user",
        workspace=str(tmp_path),
    )
    goal = GoalStore(tmp_path).create(
        objective=run.prompt,
        workspace=str(tmp_path),
        source=run.source,
        peer_id=run.peer_id,
        sender_id=run.sender_id,
        run_id=run.id,
    )
    GoalStore(tmp_path).record_result_delivery_outbox(
        run=run,
        goal=goal,
        body=body,
        body_provenance="state_graph.evidence.responded_message",
        channel="weixin",
        trace_id=run.id,
    )
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.daemon = StaticDaemon([])

    await service.process_background(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    )

    delivery = GoalStore(tmp_path).latest_delivery(goal.id)
    assert client.messages[0]["text"] == body
    assert delivery["state_transition"] == "delivered"
    assert delivery["channel"] == "weixin"
    assert delivery["text_length"] == len(body)
    assert delivery["media_count"] == 0


@pytest.mark.asyncio
async def test_background_failure_task_respects_notification_decision_not_to_notify(tmp_path: Path):
    task = Run(
        id="run-muted",
        title="scheduled lesson",
        phase=Phase.ENDED,
        governance=Governance.NONE,
        acceptance=Acceptance.REJECTED,
        resolution=Resolution.FAILED,
        created_at=1.0,
        updated_at=1.0,
        prompt="scheduled lesson",
        source="weixin",
        peer_id="wx-user",
        sender_id="wx-user",
        error="loop_failed",
    )
    client = CaptureWeixinClient()
    provider = WatchNotificationProvider(notify=False, message="")
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        client=client,
    )
    service.daemon = StaticDaemon([task])

    await service.process_background(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    )

    assert client.messages == []
    assert provider.calls[0][0] == "notification"


def test_background_failure_facts_include_persisted_loop_diagnostics(tmp_path: Path):
    opened = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="report current account usage",
            workspace=str(tmp_path),
            source="weixin",
            peer_id="wx-user",
            sender_id="wx-user",
            allowed_capabilities=("account.usage", "respond"),
            auto_start=False,
            execution_mode="background",
        )
    )
    loop_store = LoopRunStore(tmp_path)
    owner = "test-worker"
    assert loop_store.claim_for_execution(opened.loop_run.run_id, owner=owner) is not None
    loop_store.fail_active_run(
        opened.loop_run.run_id,
        lease_owner=owner,
        evidence={
            "reason_code": "semantic_check_failed",
            "reason": "repeated_progress_signature",
            "repeat_count": 4,
            "checker_results": [
                {
                    "name": "objective_check",
                    "passed": False,
                    "reason": "semantic_check_failed",
                    "evidence": {
                        "evaluator_role": "checker",
                        "evidence_summary": "current result did not satisfy the objective",
                    },
                }
            ],
            "executor": {
                "ok": True,
                "action": "account.usage",
                "facts": {"available": True, "quota_remaining_percent": 43.0},
            },
            "facts": {
                "recovery": {
                    "failure_domain": "verification_failed",
                    "reason_code": "semantic_check_failed",
                }
            },
        },
    )
    task = RunStore(tmp_path).update_run(
        opened.run.id,
        phase=Phase.ENDED,
        acceptance=Acceptance.REJECTED,
        resolution=Resolution.BLOCKED,
        error="loop_blocked",
    )
    assert task is not None
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=CaptureWeixinClient(),
    )

    facts = service._background_task_facts(task)

    diagnostics = facts["loop_diagnostics"]
    assert isinstance(diagnostics, dict)
    assert diagnostics["loop_run_id"] == opened.loop_run.run_id
    assert diagnostics["terminal_state"] == LoopTerminalState.FAILED
    assert diagnostics["reason_code"] == "semantic_check_failed"
    assert diagnostics["failure_domain"] == "verification_failed"
    assert diagnostics["checker_results"][0]["evidence_summary"] == (
        "current result did not satisfy the objective"
    )
    assert diagnostics["last_capability"]["facts"]["quota_remaining_percent"] == 43.0


@pytest.mark.asyncio
async def test_realtime_file_delivery_records_success_only_after_transport(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.xlsx"
    report.write_bytes(b"report")
    run = RunStore(tmp_path).create(
        "deliver report",
        kind="delegation",
        source="weixin",
        peer_id="wx-user",
        sender_id="wx-user",
        workspace=str(tmp_path),
    )
    goal = GoalStore(tmp_path).create(
        objective=run.prompt,
        workspace=str(tmp_path),
        source=run.source,
        peer_id=run.peer_id,
        sender_id=run.sender_id,
        run_id=run.id,
    )
    contract = ConnectorDelivery(
        path=str(report.resolve()),
        text="报告已发送。",
        delivery_id="delivery-success",
        run_id=run.id,
        goal_id=goal.id,
    )
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.ingress = StaticIngress(
        contract.text,
        action="connector_outbound",
        facts={"connector_delivery": contract.to_dict()},
    )

    assert (
        await service.handle_update(
            WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example"),
            WeixinUpdate(
                message_id="msg-delivery-success",
                peer_id="wx-user",
                sender_id="wx-user",
                text="发送报告",
                context_token="ctx",
            ),
        )
        is True
    )

    recorded = GoalStore(tmp_path).latest_delivery(goal.id)
    assert recorded["state_transition"] == "delivered"
    assert recorded["media_count"] == 1
    assert recorded["channel"] == "weixin"
    egress = [
        event
        for event in TraceStore(tmp_path).list_events(trace_id="msg-delivery-success")
        if event.phase == "channel.egress"
    ]
    assert len(egress) == 1
    assert egress[0].ok is True


@pytest.mark.asyncio
async def test_bare_approval_delivery_closes_original_and_transport_loops(
    tmp_path: Path,
) -> None:
    report = tmp_path / "approved-report.md"
    report.write_text("approved report\n", encoding="utf-8")
    provider = BareApprovedFileProvider(report)
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "send approved report",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["channel.send_file"],
        },
        permission="prepare",
        context=CapabilityContext(
            home=tmp_path,
            source="weixin",
            peer_id="wx-user",
            sender_id="wx-user",
            permission_ceiling="write",
            workspace=str(tmp_path),
            trace_id="request-approved-report",
        ),
    )
    provider.approval_run_id = opened.run_id
    approval = RunStore(tmp_path).pending_approval_for_run(opened.run_id)
    assert approval is not None
    assert opened.facts["loop_terminal_state"] == LoopTerminalState.WAITING_APPROVAL

    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=runtime,
        project_dir=tmp_path,
        client=client,
    )
    try:
        handled = await service.handle_update(
            WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example"),
            WeixinUpdate(
                message_id="bare-approved-delivery",
                peer_id="wx-user",
                sender_id="wx-user",
                text="批准",
                context_token="ctx",
            ),
        )
    finally:
        await service.ingress.event_bus.shutdown()

    assert handled is True
    assert len(client.files) == 1
    assert client.files[0]["file_path"] == report.resolve()
    assert [message["text"] for message in client.messages] == ["这是你要的文件。"]
    original_run = RunStore(tmp_path).get(opened.run_id)
    assert original_run is not None
    assert original_run.phase == Phase.ENDED
    assert original_run.resolution == Resolution.SUCCESS
    original_loop = LoopRunStore(tmp_path).get_run(opened.facts["loop_run_id"])
    assert original_loop is not None
    assert original_loop.terminal_state == LoopTerminalState.CONVERGED
    assert LoopRunStore(tmp_path).list_active() == []
    goals = GoalStore(tmp_path).list(limit=10)
    assert len(goals) == 2
    assert all(goal.phase == Phase.ENDED for goal in goals)
    assert all(goal.resolution == Resolution.SUCCESS for goal in goals)
    assert all(
        RunStore(tmp_path).get(goal.run_id).resolution == Resolution.SUCCESS for goal in goals
    )

    decisions = [
        json.loads(event.output_json)
        for event in TraceStore(tmp_path).list_loop_decisions("bare-approved-delivery")
    ]
    conditions = [str(item.get("evidence", {}).get("condition") or "") for item in decisions]
    assert "approval_required" not in conditions
    assert "responder" not in provider.calls


@pytest.mark.asyncio
async def test_realtime_file_delivery_failure_does_not_record_success(
    tmp_path: Path,
) -> None:
    report = tmp_path / "failed-report.xlsx"
    report.write_bytes(b"report")
    run = RunStore(tmp_path).create(
        "deliver failed report",
        kind="delegation",
        source="weixin",
        peer_id="wx-user",
        sender_id="wx-user",
        workspace=str(tmp_path),
    )
    goal = GoalStore(tmp_path).create(
        objective=run.prompt,
        workspace=str(tmp_path),
        source=run.source,
        peer_id=run.peer_id,
        sender_id=run.sender_id,
        run_id=run.id,
    )
    contract = ConnectorDelivery(
        path=str(report.resolve()),
        delivery_id="delivery-failure",
        run_id=run.id,
        goal_id=goal.id,
    )
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=FailingFileWeixinClient(),
    )
    service.ingress = StaticIngress(
        "",
        action="connector_outbound",
        facts={"connector_delivery": contract.to_dict()},
    )

    with pytest.raises(RuntimeError, match="upload failed"):
        await service.handle_update(
            WeixinAccount(
                account_id="acct",
                token="token",
                base_url="https://ilink.example",
            ),
            WeixinUpdate(
                message_id="msg-delivery-failure",
                peer_id="wx-user",
                sender_id="wx-user",
                text="发送报告",
                context_token="ctx",
            ),
        )

    assert GoalStore(tmp_path).latest_delivery(goal.id) == {}
    failure_events = [
        event
        for event in GoalStore(tmp_path).list_events(goal.id)
        if event.event_type == "goal.delivery_failed"
    ]
    assert len(failure_events) == 1
    events = (tmp_path / "weixin" / "events.jsonl").read_text(encoding="utf-8")
    assert '"event": "reply.error"' in events
    assert '"event": "reply.sent"' not in events
    failed_egress = [
        event
        for event in TraceStore(tmp_path).list_events(trace_id="msg-delivery-failure")
        if event.phase == "channel.egress"
    ]
    assert len(failed_egress) == 1
    assert failed_egress[0].ok is False


@pytest.mark.asyncio
async def test_service_deduplicates_message_id_across_instances(tmp_path: Path):
    account = WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    update = WeixinUpdate(
        message_id="msg-repeat",
        peer_id="wx-user",
        sender_id="wx-user",
        text="你好",
        context_token="ctx",
    )

    first_client = CaptureWeixinClient()
    first = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=first_client,
    )
    first.ingress = StaticIngress("第一次回复")

    second_client = CaptureWeixinClient()
    second = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=second_client,
    )
    second.ingress = StaticIngress("第二次不应回复")

    assert await first.handle_update(account, update) is True
    assert await second.handle_update(account, update) is False
    assert first_client.messages[0]["text"] == "第一次回复"
    assert second_client.messages == []

    events = (tmp_path / "weixin" / "events.jsonl").read_text(encoding="utf-8")
    assert '"event": "message.duplicate"' in events


@pytest.mark.asyncio
async def test_send_file_returns_connector_neutral_synchronous_delivery(tmp_path: Path):
    source = tmp_path / "resume.docx"
    source.write_bytes(b"resume")
    args = {"path": str(source)}
    runs = RunStore(tmp_path)
    run = runs.create(
        "deliver file",
        kind="delegation",
        source="local",
        workspace=str(tmp_path),
        phase=Phase.RUNNING,
        governance=Governance.APPROVED,
    )
    approval = runs.create_approval(
        run_id=run.id,
        action=APPROVAL_ACTION_CAPABILITY,
        requested_tool="channel.send_file",
        requested_permission="write",
        args_json=json.dumps(args, ensure_ascii=False, sort_keys=True),
        reason="test approved outbound media delivery",
    )
    runs.resolve_approval(approval.id, decision=APPROVAL_DECISION_APPROVE, resolved_by="test")
    registry = build_capability_registry(tmp_path, project_dir=tmp_path, governed_run_id=run.id)

    result = await registry.invoke(
        "channel.send_file",
        args,
        permission="write",
        context=CapabilityContext(home=tmp_path, source="local", workspace=str(tmp_path)),
    )

    assert result.ok is True
    assert result.terminal is False
    assert result.yields_control is True
    assert result.action == "connector_outbound"
    assert result.facts is not None
    assert "entity_type" in result.facts
    assert result.facts["entity_type"] == "connector_delivery"
    assert result.facts["side_effect_scope"] == "external"
    assert result.facts["side_effect_state"] == "delivery_requested"
    assert result.facts["side_effect_artifact"] == str(source.resolve())
    delivery = result.facts["connector_delivery"]
    assert delivery["kind"] == "file"
    assert delivery["mode"] == "synchronous"
    assert delivery["channel"] == "current"
    assert delivery["path"] == str(source.resolve())
    assert not (tmp_path / "weixin" / "outbox").exists()


@pytest.mark.asyncio
async def test_background_task_without_surface_text_does_not_synthesize_reply(tmp_path: Path):
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.daemon = StaticDaemon(
        [
            Run(
                id="run-empty",
                title="empty completed task",
                phase=Phase.ENDED,
                governance=Governance.NONE,
                acceptance=Acceptance.NONE,
                resolution=Resolution.SUCCESS,
                created_at=1.0,
                updated_at=1.0,
                prompt="do work",
                source="weixin",
                peer_id="wx-user",
                sender_id="wx-user",
            )
        ]
    )

    await service.process_background(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    )

    assert client.messages == []


@pytest.mark.asyncio
async def test_background_watch_without_surface_text_does_not_record_sent_reply(tmp_path: Path):
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(home_channel="wx-home"),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.daemon = StaticDaemon(tasks=[], watch_results=[{"run_id": "watch-empty"}])

    await service.process_background(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    )

    assert client.messages == []
    events = (tmp_path / "weixin" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"event": "background.skipped"' in line for line in events)
    assert not any('"event": "background.sent"' in line for line in events)


@pytest.mark.asyncio
async def test_background_watch_notification_is_model_owned_and_fact_bound(tmp_path: Path):
    client = CaptureWeixinClient()
    provider = WatchNotificationProvider(
        notify=True,
        message="app.py 有新的未提交修改。",
    )
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(home_channel="wx-home"),
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        client=client,
    )
    service.daemon = StaticDaemon(
        tasks=[],
        watch_results=[
            {
                "message": "runtime-authored text must not be sent",
                "workspace": str(tmp_path),
                "facts": {
                    "kind": "git_status_changed",
                    "changed_files": ["M app.py"],
                },
            }
        ],
    )

    await service.process_background(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    )

    assert [message["text"] for message in client.messages] == ["app.py 有新的未提交修改。"]
    assert provider.calls[0][0] == "notification"
    assert "runtime-authored text must not be sent" not in provider.calls[0][1][-1].content
    assert "git_status_changed" in provider.calls[0][1][-1].content
    assert provider.calls[0][2]["output_schema"]["name"] == "background_notification"
    trace_store = TraceStore(tmp_path)
    role_events = [
        event
        for trace_id in trace_store.list_trace_ids(limit=20)
        for event in trace_store.list_events(trace_id)
        if event.model_role == "notification"
    ]
    assert len(role_events) == 1
    assert role_events[0].phase == "agent.role_result"


@pytest.mark.asyncio
async def test_background_watch_respects_model_decision_not_to_notify(tmp_path: Path):
    client = CaptureWeixinClient()
    provider = WatchNotificationProvider(notify=False, message="")
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(home_channel="wx-home"),
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        client=client,
    )
    service.daemon = StaticDaemon(
        tasks=[],
        watch_results=[{"facts": {"kind": "port_went_offline", "port": 8000}}],
    )

    await service.process_background(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    )

    assert client.messages == []
    assert provider.calls[0][0] == "notification"


@pytest.mark.asyncio
async def test_background_notification_model_failure_propagates_without_fallback(
    tmp_path: Path,
) -> None:
    class FailingNotificationProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete_for(self, role: str, messages: list[Any], **kwargs: Any) -> str:
            del role, messages, kwargs
            self.calls += 1
            raise RuntimeError("notification model unavailable")

        def list_roles(self) -> list[str]:
            return ["notification"]

    client = CaptureWeixinClient()
    provider = FailingNotificationProvider()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(home_channel="wx-home"),
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        client=client,
    )
    service.daemon = StaticDaemon(
        tasks=[],
        watch_results=[{"facts": {"kind": "port_went_offline", "port": 8000}}],
    )

    with pytest.raises(RuntimeError, match="notification model unavailable"):
        await service.process_background(
            WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
        )

    assert provider.calls == 1
    assert client.messages == []
