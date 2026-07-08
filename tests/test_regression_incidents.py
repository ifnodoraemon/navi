from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
import yaml

from navi.control import CurrentStateBuilder, SurfaceContext, current_state_facts
from navi.control_plane import TurnController, _dynamic_intent_facts
from navi.connector_runtime import ConnectorMessage, ConnectorIngressRuntime
from navi.evolution import EvolutionEngine, EvolutionLedger
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.turn_result import AgentTurnResult
from navi.execution import ExecutionService
from navi.goals import GoalStore
from navi.lifecycle import Acceptance, Governance, Phase, Resolution
from navi.provider import ChatMessage, _extract_anthropic_content, _extract_openai_content
from navi.runtime import AgentRuntime
from navi.runs import RunStore
from navi.syscalls import ModelSyscallPlanner
from navi.tools import ToolSpec
from navi.trace import TraceStore


def test_evolution_ledger_uses_latest_run_id_schema(tmp_path):
    EvolutionLedger(tmp_path)

    with closing(sqlite3.connect(tmp_path / "evolution.db")) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(evolution_events)").fetchall()}

    assert "run_id" in columns
    assert "task_id" not in columns


def test_run_execution_rollback_restores_lifecycle_state(tmp_path):
    runs = RunStore(tmp_path)
    run = runs.create(
        "rollback lifecycle test",
        workspace=str(tmp_path),
        phase=Phase.RUNNING,
        governance=Governance.NONE,
        acceptance=Acceptance.NONE,
        resolution=Resolution.NONE,
    )
    before = {
        "phase": Phase.PAUSED,
        "governance": Governance.AWAITING_APPROVAL,
        "acceptance": Acceptance.NONE,
        "resolution": Resolution.BLOCKED,
        "result_summary": "approval_requested",
        "error": "",
    }
    event = EvolutionLedger(tmp_path).record(
        run_id=run.id,
        target_type="run_execution",
        target_id=run.id,
        reason="test rollback",
        before=json.dumps(before, sort_keys=True),
        after=json.dumps({"phase": Phase.ENDED, "resolution": Resolution.SUCCESS}),
    )

    updated = runs.update_run(
        run.id,
        phase=Phase.ENDED,
        governance=Governance.NONE,
        acceptance=Acceptance.ACCEPTED,
        resolution=Resolution.SUCCESS,
        result_summary="done",
    )
    assert updated is not None

    EvolutionEngine(tmp_path).rollback(event.id)

    restored = runs.get(run.id)
    assert restored is not None
    assert restored.phase == Phase.PAUSED
    assert restored.governance == Governance.AWAITING_APPROVAL
    assert restored.acceptance == Acceptance.NONE
    assert restored.resolution == Resolution.BLOCKED
    assert restored.result_summary == "approval_requested"


def test_run_execution_rollback_rejects_legacy_status_shape(tmp_path):
    runs = RunStore(tmp_path)
    run = runs.create("legacy rollback shape", workspace=str(tmp_path))
    event = EvolutionLedger(tmp_path).record(
        run_id=run.id,
        target_type="run_execution",
        target_id=run.id,
        reason="legacy rollback",
        before=json.dumps({"status": "paused"}, sort_keys=True),
        after=json.dumps({"phase": Phase.ENDED, "resolution": Resolution.SUCCESS}),
    )

    with pytest.raises(ValueError, match="missing lifecycle fields"):
        EvolutionEngine(tmp_path).rollback(event.id)


def test_provider_rejects_structured_json_hidden_in_reasoning_content():
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "reasoning_content": (
                        "reasoning omitted\nresponse"
                        '{"tool":"respond","permission":"read","args":{"message":"ok"}}'
                    ),
                },
                "finish_reason": "stop",
            }
        ]
    }

    with pytest.raises(RuntimeError, match="Provider response content is empty"):
        _extract_openai_content(data)





def test_current_state_exposes_only_pending_approval_codes(tmp_path) -> None:
    runs = RunStore(tmp_path)
    run = runs.create(
        "send file",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase=Phase.PAUSED,
        governance=Governance.AWAITING_APPROVAL,
        resolution=Resolution.BLOCKED,
    )
    runs.update_run(
        run.id,
        result_summary="approval_requested\napproval_code=111111\nstatus=pending",
    )
    approved = runs.create_approval(
        run_id=run.id,
        action="capability",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        requested_tool="connector.weixin.send_file",
        requested_permission="write",
        code="111111",
    )
    runs.resolve_approval(approved.id, decision="approve", resolved_by="sender-1")
    pending = runs.create_approval(
        run_id=run.id,
        action="capability",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        requested_tool="connector.weixin.send_file",
        requested_permission="write",
        code="222222",
    )

    state = CurrentStateBuilder(tmp_path).build(
        SurfaceContext(
            home=tmp_path,
            source="weixin",
            peer_id="peer-1",
            sender_id="sender-1",
        )
    )
    facts = current_state_facts(state)

    assert facts["active_runs"][0]["result_summary"] == "approval_requested\nstatus=pending"
    assert facts["pending_approvals"] == [
        {
            "id": pending.id,
            "run_id": run.id,
            "action": "capability",
            "requested_tool": "connector.weixin.send_file",
            "requested_permission": "write",
            "source": "weixin",
            "peer_id": "peer-1",
            "sender_id": "sender-1",
            "status": "pending",
            "code": "222222",
            "expires_at": pending.expires_at,
            "created_at": pending.created_at,
            "updated_at": pending.updated_at,
            "reason": "",
        }
    ]


class _PlannerSchemaProvider:
    def __init__(self) -> None:
        self.output_schema: dict | None = None

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict | None = None,
    ) -> str:
        self.output_schema = output_schema
        return json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "respond",
                        "permission": "read",
                        "args": {"message": "ok"},
                        "model_role": "responder",
                    }
                ]
            }
        )


class _PromptCaptureProvider:
    def __init__(self) -> None:
        self.planner_user_prompt = ""
        self.responder_prompt = ""

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict | None = None,
    ) -> str:
        if role == "responder":
            self.responder_prompt = messages[-1].content
            return "你好"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


def test_dynamic_intent_current_state_is_not_duplicated() -> None:
    duplicate_state = {
        "source_agent": "intent_agent",
        "intent_basis": "current_state_facts",
        "current_state": {"marker": "duplicate-current-state-marker"},
    }

    assert _dynamic_intent_facts(duplicate_state) == {}

    filtered = _dynamic_intent_facts(
        {
            **duplicate_state,
            "connector_message": {"message_id": "msg-1"},
        }
    )

    assert filtered == {
        "source_agent": "intent_agent",
        "intent_basis": "current_state_facts",
        "connector_message": {"message_id": "msg-1"},
    }


@pytest.mark.asyncio
async def test_weixin_intent_current_state_is_not_repeated_in_planner_runtime_facts(
    tmp_path,
) -> None:
    provider = _PromptCaptureProvider()
    engine = TurnController(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "你好",
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        session_alias="weixin:peer-1:sender-1",
        intent_facts={
            "source_agent": "intent_agent",
            "intent_basis": "current_state_facts",
            "current_state": {"marker": "duplicate-current-state-marker"},
        },
    )

    assert result.ok is True
    assert provider.planner_user_prompt == ""
    assert "duplicate-current-state-marker" not in provider.responder_prompt


@pytest.mark.asyncio
async def test_connector_approval_command_returns_explicit_unresolved_fact(tmp_path):
    class ApprovalProvider:
        def __init__(self):
            self.calls = 0

        async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
            self.calls += 1
            raise AssertionError("connector approval control envelope should not call the model")
        def list_roles(self) -> list[str]:
            return ["planner"]

    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=ApprovalProvider()),
        project_dir=tmp_path,
    )
    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="msg-approval",
                peer_id="peer-1",
                sender_id="sender-1",
                text="批准 123456",
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )
    finally:
        await ingress.event_bus.shutdown()

    assert response.text.startswith("approval_not_resolved\n")
    assert "reason=approval_code_not_found" in response.text
    assert ingress.agent.runtime.provider.calls == 0


@pytest.mark.asyncio
async def test_governed_sensitive_shell_call_suspends_until_matching_approval(tmp_path):
    runs = RunStore(tmp_path)
    run = runs.create(
        "sensitive command",
        kind="delegation",
        source="local",
        peer_id="local",
        sender_id="user-1",
        workspace=str(tmp_path),
        phase=Phase.PENDING,
    )
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        governed_run_id=run.id,
    )
    context = CapabilityContext(
        home=tmp_path,
        peer_id="local",
        sender_id="user-1",
        source="local",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    args = {"command": ["python", "-c", "print('approved')"]}

    suspended = await registry.invoke("shell.run", args, permission="write", context=context)

    assert suspended.ok is False
    assert suspended.error_reason == "sensitive_op_requires_approval"
    approval = RunStore(tmp_path).list_approvals(run_id=run.id)[0]
    assert approval.action == "capability"
    assert approval.requested_tool == "shell.run"
    assert approval.requested_permission == "write"
    suspended_run = RunStore(tmp_path).get(run.id)
    assert suspended_run is not None
    assert suspended_run.phase == Phase.PAUSED
    assert suspended_run.governance == Governance.AWAITING_APPROVAL
    assert suspended_run.resolution == Resolution.BLOCKED

    resolved = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=context,
    )
    assert resolved.ok is True

    executed = await registry.invoke("shell.run", args, permission="write", context=context)
    assert executed.ok is True
    assert "approved" in (executed.facts or {}).get("stdout", "")


@pytest.mark.asyncio
async def test_active_turn_sensitive_shell_call_creates_durable_approval(tmp_path):
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="local",
        sender_id="user-1",
        source="local",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    args = {"command": ["python", "-c", "print('needs approval')"]}

    suspended = await registry.invoke("shell.run", args, permission="write", context=context)

    assert suspended.ok is False
    assert suspended.terminal is False
    assert suspended.error_reason == "sensitive_op_requires_approval"
    assert suspended.facts["entity_type"] == "approval_request"
    assert suspended.facts["requested_tool"] == "shell.run"
    run = RunStore(tmp_path).get(suspended.run_id)
    assert run is not None
    assert run.kind == "capability_approval"
    assert run.phase == Phase.PAUSED
    assert run.governance == Governance.AWAITING_APPROVAL
    approval = RunStore(tmp_path).list_approvals(run_id=run.id)[0]
    assert approval.action == "capability"
    assert approval.requested_tool == "shell.run"
    assert approval.requested_permission == "write"


def test_network_tools_are_not_plain_read_capabilities(tmp_path):
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    read_names = {spec.name for spec in registry.planner_specs(permission_ceiling="read")}
    network_names = {spec.name for spec in registry.planner_specs(permission_ceiling="network")}

    assert "web.search" not in read_names
    assert "http.fetch" not in read_names
    assert "web.search" in network_names
    assert "http.fetch" in network_names
    assert registry.get("web.search").permission == "network"
    assert registry.get("http.fetch").permission == "network"


@pytest.mark.asyncio
async def test_planner_structured_output_wrapper_is_not_a_capability_name():
    provider = _PlannerSchemaProvider()
    planner = ModelSyscallPlanner(provider)

    decision = await planner.plan("hi", tools=[])

    assert provider.output_schema["name"] == "planner_decision"
    assert provider.output_schema["schema"]["required"] == ["syscalls"]
    assert isinstance(decision, list)
    assert len(decision) == 1
    assert decision[0].tool == "respond"
    assert decision[0].confidence == 0.0
    assert decision[0].reason == ""


def test_anthropic_structured_wrapper_returns_inner_planner_decision():
    raw = {
        "content": [
            {
                "type": "tool_use",
                "name": "planner_decision",
                "input": {
                    "tool": "delegate.list",
                    "permission": "read",
                    "args": {"limit": 10},
                    "model_role": "responder",
                    "confidence": 1,
                    "reason": "inspect run facts",
                },
            }
        ]
    }

    parsed = json.loads(_extract_anthropic_content(raw, tool_name="planner_decision"))

    assert parsed["tool"] == "delegate.list"


def test_anthropic_direct_tool_call_is_not_reconstructed_as_planner_decision():
    raw = {
        "content": [
            {
                "type": "tool_use",
                "name": "delegate.list",
                "input": {"limit": 10},
            }
        ]
    }

    with pytest.raises(RuntimeError, match="did not include tool output planner_decision"):
        _extract_anthropic_content(raw, tool_name="planner_decision")


def test_planner_parser_rejects_markdown_fenced_json():
    decisions = ModelSyscallPlanner._parse_syscalls(
        '```json\n{"tool":"respond","permission":"read","args":{},'
        '"model_role":"responder","confidence":1,"reason":"done"}\n```'
    )

    assert isinstance(decisions, list)
    assert len(decisions) == 1
    assert decisions[0].tool == "system.planner_error"
    assert decisions[0].reason == "planner returned invalid JSON"


def test_planner_parser_accepts_missing_optional_audit_fields():
    decisions = ModelSyscallPlanner._parse_syscalls(
        json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "respond",
                        "permission": "read",
                        "args": {"message": "ok"},
                        "model_role": "responder",
                    }
                ]
            }
        )
    )

    assert isinstance(decisions, list)
    assert len(decisions) == 1
    assert decisions[0].tool == "respond"
    assert decisions[0].confidence == 0.0
    assert decisions[0].reason == ""


def test_planner_parser_rejects_missing_required_schema_fields():
    decisions = ModelSyscallPlanner._parse_syscalls(
        json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "respond",
                        "permission": "read",
                        "args": {"message": "ok"},
                    }
                ]
            }
        )
    )

    assert isinstance(decisions, list)
    assert len(decisions) == 1
    assert decisions[0].tool == "system.planner_error"
    assert decisions[0].reason == "planner decision schema mismatch"
    assert "$.syscalls[0].model_role is required" in decisions[0].args["schema_errors"]


@pytest.mark.asyncio
async def test_planner_rejects_selected_capability_args_schema_mismatch():
    class Provider:
        async def complete_for(
            self,
            role: str,
            messages: list[ChatMessage],
            *,
            output_schema: dict | None = None,
        ) -> str:
            del role, messages, output_schema
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "respond",
                            "permission": "read",
                            "args": {},
                            "model_role": "responder",
                        }
                    ]
                }
            )

    decision = await ModelSyscallPlanner(Provider()).plan(
        "hi",
        tools=[
            ToolSpec(
                name="respond",
                capability_class="conversation",
                execution_contexts=("turn",),
                description="Return a final user-facing message.",
                input_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
                output_schema={"type": "object", "properties": {}},
            )
        ],
    )

    assert isinstance(decision, list)
    assert len(decision) == 1
    assert decision[0].tool == "system.planner_error"
    assert decision[0].reason == "planner capability arguments schema mismatch"
    assert decision[0].args["selected_tool"] == "respond"
    assert "$.message is required" in decision[0].args["schema_errors"]


def test_planner_parser_parses_multiple_syscalls():
    decisions = ModelSyscallPlanner._parse_syscalls(
        json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "delegate.spawn",
                        "permission": "prepare",
                        "args": {"objective": "x"},
                        "model_role": "planner",
                    },
                    {
                        "tool": "respond",
                        "permission": "read",
                        "args": {"message": "done"},
                        "model_role": "responder",
                    },
                ]
            }
        )
    )

    assert isinstance(decisions, list)
    assert len(decisions) == 2
    assert decisions[0].tool == "delegate.spawn"
    assert decisions[1].tool == "respond"
    assert decisions[1].message == "done"


def test_planner_parser_rejects_empty_syscalls_list():
    decisions = ModelSyscallPlanner._parse_syscalls(
        json.dumps({"syscalls": []})
    )

    assert isinstance(decisions, list)
    assert len(decisions) == 1
    assert decisions[0].tool == "system.planner_error"
    assert decisions[0].reason == "planner 'syscalls' list was empty"


def test_planner_parser_rejects_non_list_syscalls():
    decisions = ModelSyscallPlanner._parse_syscalls(
        json.dumps({"syscalls": "not a list"})
    )

    assert isinstance(decisions, list)
    assert len(decisions) == 1
    assert decisions[0].tool == "system.planner_error"
    assert decisions[0].reason == "planner decision schema mismatch"
    assert "$.syscalls expected array" in decisions[0].args["schema_errors"]


def test_planner_parser_rejects_single_syscall_without_wrapper():
    decisions = ModelSyscallPlanner._parse_syscalls(
        json.dumps(
            {
                "tool": "respond",
                "permission": "read",
                "args": {"message": "hi"},
                "model_role": "responder",
            }
        )
    )

    assert isinstance(decisions, list)
    assert len(decisions) == 1
    assert decisions[0].tool == "system.planner_error"
    assert decisions[0].reason == "planner decision schema mismatch"
    assert "$.syscalls is required" in decisions[0].args["schema_errors"]


class _StructuredJourneyProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict | None = None,
    ) -> str:
        self.calls.append(
            {
                "role": role,
                "messages": messages,
                "output_schema": output_schema,
            }
        )
        return json.dumps(
            {
                "id": "repo_review_history",
                "user_goal": "Review repository principles after code changes",
                "steps": [
                    {
                        "user": "再次全面审查，只列问题",
                        "expect": {"text_contains": "问题"},
                    }
                ],
            }
        )


class _RemoteDeleteUnavailableProvider:
    def __init__(self) -> None:
        self.planner_calls = 0
        self.responder_calls = 0

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict | None = None,
    ) -> str:
        if role == "planner" and output_schema is not None:
            self.planner_calls += 1
            content = "\n".join(message.content for message in messages)
            assert "delegate.delete" not in content
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "respond",
                            "permission": "read",
                            "args": {"message": "remote_delete_not_available"},
                            "model_role": "responder",
                            "confidence": 1.0,
                            "reason": "delete capability is not visible on remote surface",
                        }
                    ]
                }
            )
        if role == "responder":
            self.responder_calls += 1
            return "remote_delete_not_available"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _DelegateSpawnApprovalProvider:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.planner_calls = 0
        self.responder_calls = 0

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict | None = None,
    ) -> str:
        if role == "planner":
            self.planner_calls += 1
            if self.planner_calls > 1:
                content = "\n".join(message.content for message in messages)
                assert "awaiting_approval" in content
                return json.dumps(
                    {
                        "syscalls": [
                            {
                                "tool": "respond",
                                "permission": "read",
                                "args": {"message": "需要审批后执行。"},
                                "model_role": "responder",
                                "confidence": 1.0,
                                "reason": "report approval pause facts",
                            }
                        ]
                    }
                )
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "delegate.spawn",
                            "permission": "prepare",
                            "args": {
                                "objective": "在家目录查找简历文件",
                                "context": "用户明确要求在家目录中查找简历。",
                                "plan": "在家目录搜索简历文件。",
                                "success_criteria": "返回找到的简历文件事实或未找到事实。",
                                "workspace": self.workspace,
                            },
                            "model_role": "responder",
                            "confidence": 1.0,
                            "reason": "create delegated run",
                        }
                    ]
                }
            )
        if role == "responder":
            self.responder_calls += 1
            return "需要审批后执行。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _AskOnlyEngine:
    def __init__(self, **kwargs):
        pass

    async def handle(self, *args, **kwargs) -> AgentTurnResult:
        return AgentTurnResult(
            text="请提供文件位置。",
            action="ask",
            model_role="responder",
            terminal=True,
            yields_control=True,
        )


class _CompletedEngine:
    def __init__(self, **kwargs):
        pass

    async def handle(self, *args, **kwargs) -> AgentTurnResult:
        return AgentTurnResult(
            text="MEDIA:/tmp/resume.docx\n已找到并发送简历。",
            action="respond",
            model_role="responder",
            terminal=True,
            ok=True,
        )


@pytest.mark.asyncio
async def test_executor_ask_result_blocks_run_instead_of_marking_completed(
    tmp_path,
    monkeypatch,
):
    import navi.execution as execution_module

    monkeypatch.setattr(execution_module, "get_turn_controller_class", lambda: _AskOnlyEngine)
    runs = RunStore(tmp_path)
    task = runs.create(
        "在用户电脑上找到简历文件并发送给用户",
        prompt="Objective:\n找到简历\n\nSuccess Criteria:\n找到并发送",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase=Phase.PENDING,
    )

    result = await ExecutionService(tmp_path).execute_task(task)

    assert result.phase == Phase.PAUSED
    assert result.resolution == Resolution.BLOCKED
    assert result.result_summary == "请提供文件位置。"


@pytest.mark.asyncio
async def test_executor_terminal_response_marks_governed_run_completed(
    tmp_path,
    monkeypatch,
):
    import navi.execution as execution_module

    monkeypatch.setattr(execution_module, "get_turn_controller_class", lambda: _CompletedEngine)
    runs = RunStore(tmp_path)
    task = runs.create(
        "在用户电脑上找到简历文件并发送给用户",
        prompt="Objective:\n找到简历\n\nSuccess Criteria:\n找到并发送",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase=Phase.PENDING,
    )

    result = await ExecutionService(tmp_path).execute_task(task)

    assert result.phase == Phase.ENDED
    assert result.resolution == Resolution.SUCCESS
    assert result.result_summary == "MEDIA:/tmp/resume.docx\n已找到并发送简历。"


@pytest.mark.asyncio
async def test_executor_preserves_connector_outbound_media_directive(
    tmp_path,
    monkeypatch,
):
    import navi.execution as execution_module

    outbound = tmp_path / "weixin" / "outbox" / "resume.docx"
    outbound.parent.mkdir(parents=True)
    outbound.write_bytes(b"resume")

    class _ConnectorOutboundEngine:
        def __init__(self, **kwargs):
            pass

        async def handle(self, *args, **kwargs) -> AgentTurnResult:
            return AgentTurnResult(
                text="Here is your resume file found in the home directory.",
                action="connector_outbound",
                model_role="planner",
                terminal=True,
                ok=True,
                facts={
                    "entity_type": "outbound_media",
                    "outbound_path": str(outbound),
                },
            )

    monkeypatch.setattr(execution_module, "get_turn_controller_class", lambda: _ConnectorOutboundEngine)
    runs = RunStore(tmp_path)
    task = runs.create(
        "在用户电脑上找到简历文件并发送给用户",
        prompt="Objective:\n找到简历\n\nSuccess Criteria:\n找到并发送",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase=Phase.PENDING,
    )

    result = await ExecutionService(tmp_path).execute_task(task)

    assert result.phase == Phase.ENDED
    assert result.resolution == Resolution.SUCCESS
    assert (
        result.result_summary
        == f"MEDIA:{outbound}\nHere is your resume file found in the home directory."
    )


@pytest.mark.asyncio
async def test_executor_uses_run_id_as_trace_id_for_delegated_turn(
    tmp_path,
    monkeypatch,
):
    import navi.execution as execution_module

    seen_trace_ids: list[str | None] = []

    class _TraceCapturingEngine:
        def __init__(self, **kwargs):
            pass

        async def handle(self, *args, **kwargs) -> AgentTurnResult:
            seen_trace_ids.append(kwargs.get("trace_id"))
            return AgentTurnResult(
                text="done",
                action="respond",
                model_role="responder",
                terminal=True,
                ok=True,
            )

    monkeypatch.setattr(execution_module, "get_turn_controller_class", lambda: _TraceCapturingEngine)
    runs = RunStore(tmp_path)
    task = runs.create(
        "trace correlated execution",
        prompt="Objective:\nwork\n\nSuccess Criteria:\ndone",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase=Phase.PENDING,
    )

    result = await ExecutionService(tmp_path).execute_task(task)

    assert result.phase == Phase.ENDED
    assert seen_trace_ids == [task.id]


@pytest.mark.asyncio
async def test_remote_expired_task_cleanup_does_not_expose_delete(tmp_path):
    runs = RunStore(tmp_path)
    expired = runs.create(
        "在用户电脑上找到简历文件并发送给用户",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase=Phase.ENDED,
        resolution=Resolution.BLOCKED,
    )
    provider = _RemoteDeleteUnavailableProvider()
    engine = TurnController(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "删除过期的任务",
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        session_alias="weixin:peer-1:sender-1",
    )

    assert result.text == "remote_delete_not_available"
    assert result.terminal is True
    assert runs.get(expired.id) is not None
    assert provider.planner_calls == 0
    assert provider.responder_calls == 1
    events = TraceStore(tmp_path).list_events(result.trace_id)
    phases = [event.phase for event in events]
    assert "runtime.converged" not in phases
    assert all(event.tool != "delegate.delete" for event in events)


@pytest.mark.asyncio
async def test_evolution_engine_extracts_daily_eval_from_session_trace(tmp_path):
    home = tmp_path / "home"
    trace = TraceStore(home)
    trace_id = "trace-1"
    session_id = "session-1"
    run_id = "run-1"
    trace.add_event(
        trace_id=trace_id,
        session_id=session_id,
        run_id=run_id,
        phase="turn.start",
        input_data={"message": "再次全面审查，只列问题"},
    )
    trace.add_event(
        trace_id=trace_id,
        session_id=session_id,
        run_id=run_id,
        phase="planner.syscall",
        tool="tools.list",
        output_data={"tool": "tools.list", "reason": "collect facts first"},
    )
    trace.add_event(
        trace_id=trace_id,
        session_id=session_id,
        run_id=run_id,
        phase="capability.result",
        tool="tools.list",
        message="42 tools available",
    )
    trace.add_event(
        trace_id=trace_id,
        session_id=session_id,
        run_id=run_id,
        phase="turn.final",
        message="问题：无",
    )

    provider = _StructuredJourneyProvider()
    engine = EvolutionEngine(home)
    engine.provider = provider

    await engine.extract_evals_from_session(session_id, run_id=run_id)

    assert provider.calls
    call = provider.calls[0]
    assert call["role"] == "planner"
    assert call["output_schema"]["name"] == "daily_journey_eval"
    assert "tools.list" in call["messages"][0].content

    data = yaml.safe_load((tmp_path / "evals" / "auto_captured_journeys.yaml").read_text())
    assert data["journeys"][0]["id"] == "repo_review_history"
