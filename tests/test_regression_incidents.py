from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing

import pytest
import yaml

from navi.context import ContextManager
from navi.control import CurrentStateBuilder, SurfaceContext, current_state_facts
from navi.engine import LoopEngine, _dynamic_intent_facts
from navi.connector_runtime import ConnectorMessage, ConnectorIngressRuntime
from navi.connector_router import ConnectorRouter
from navi.event_bus import EventBus
from navi.evolution import EvolutionEngine, EvolutionLedger
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.engine_types import AgentTurnResult
from navi.execution import ExecutionService
from navi.goals import GoalStore
from navi.lifecycle import Acceptance, Governance, Phase, Resolution
from navi.provider import ChatMessage, _extract_anthropic_content, _extract_openai_content
from navi.runtime import AgentRuntime
from navi.runs import RunStore
from navi.syscalls import ModelSyscallPlanner
from navi.tools import ToolSpec
from navi.trace import TraceStore


def _empty_provider_usage(self, role: str) -> dict:
    return {}


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


def test_planner_history_redacts_stale_approval_codes() -> None:
    class Message:
        def __init__(self, role: str, content: str) -> None:
            self.role = role
            self.content = content

    context = ContextManager(recent_turns=6).build_conversation_context(
        [
            Message(
                "assistant",
                (
                    "approval_code=408239\n"
                    "approval code 408239\n"
                    "审批码包括：670343, 357979, 408239"
                ),
            ),
            Message("user", "你好"),
        ]
    )

    assert "408239" not in context
    assert "670343" not in context
    assert "357979" not in context
    assert "approval_code=[redacted-history-approval-code]" in context
    assert "approval code [redacted-history-approval-code]" in context
    assert "审批码包括：[redacted-history-approval-codes]" in context


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
        requested_tool="connector.weixin.stage_file",
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
        requested_tool="connector.weixin.stage_file",
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
            "requested_tool": "connector.weixin.stage_file",
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
                "tool": "respond",
                "permission": "read",
                "args": {"message": "ok"},
                "model_role": "responder",
            }
        )


class _PromptCaptureProvider:
    def __init__(self) -> None:
        self.planner_user_prompt = ""

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict | None = None,
    ) -> str:
        if role == "planner" and output_schema is not None:
            self.planner_user_prompt = messages[-1].content
            return json.dumps(
                {
                    "tool": "respond",
                    "permission": "read",
                    "args": {"message": "你好"},
                    "model_role": "responder",
                }
            )
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


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
async def test_weixin_intent_current_state_is_not_repeated_in_planner_observations(
    tmp_path,
) -> None:
    provider = _PromptCaptureProvider()
    engine = LoopEngine(
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
    assert provider.planner_user_prompt.count('"observation_type": "current_state"') == 1
    assert '"observation_type": "dynamic_intent"' not in provider.planner_user_prompt
    assert "duplicate-current-state-marker" not in provider.planner_user_prompt


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

    assert response.startswith("approval_not_resolved\n")
    assert "reason=approval_code_not_found" in response
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
async def test_planner_structured_output_wrapper_is_not_a_capability_name():
    provider = _PlannerSchemaProvider()
    planner = ModelSyscallPlanner(provider)

    decision = await planner.plan("hi", tools=[])

    assert provider.output_schema["name"] == "planner_decision"
    assert provider.output_schema["schema"]["required"] == [
        "tool",
        "permission",
        "args",
        "model_role",
    ]
    assert decision.tool == "respond"
    assert decision.confidence == 0.0
    assert decision.reason == ""


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
    decision = ModelSyscallPlanner._parse_syscall(
        '```json\n{"tool":"respond","permission":"read","args":{},'
        '"model_role":"responder","confidence":1,"reason":"done"}\n```'
    )

    assert decision.tool == "system.planner_error"
    assert decision.reason == "planner returned invalid JSON"


def test_planner_parser_accepts_missing_optional_audit_fields():
    decision = ModelSyscallPlanner._parse_syscall(
        json.dumps(
            {
                "tool": "respond",
                "permission": "read",
                "args": {"message": "ok"},
                "model_role": "responder",
            }
        )
    )

    assert decision.tool == "respond"
    assert decision.confidence == 0.0
    assert decision.reason == ""


def test_planner_parser_rejects_missing_required_schema_fields():
    decision = ModelSyscallPlanner._parse_syscall(
        json.dumps(
            {
                "tool": "respond",
                "permission": "read",
                "args": {"message": "ok"},
            }
        )
    )

    assert decision.tool == "system.planner_error"
    assert decision.reason == "planner decision schema mismatch"
    assert "$.model_role is required" in decision.args["schema_errors"]


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
                    "tool": "respond",
                    "permission": "read",
                    "args": {},
                    "model_role": "responder",
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

    assert decision.tool == "system.planner_error"
    assert decision.reason == "planner capability arguments schema mismatch"
    assert decision.args["selected_tool"] == "respond"
    assert "$.message is required" in decision.args["schema_errors"]


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
                    "tool": "respond",
                    "permission": "read",
                    "args": {"message": "remote_delete_not_available"},
                    "model_role": "responder",
                    "confidence": 1.0,
                    "reason": "delete capability is not visible on remote surface",
                }
            )
        if role == "responder":
            self.responder_calls += 1
            return "remote_delete_not_available"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


class _RepeatListProvider:
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
        if role == "planner":
            self.planner_calls += 1
            if self.planner_calls > 5:
                return json.dumps(
                    {
                        "tool": "respond",
                        "permission": "read",
                        "args": {"message": "当前没有任务。"},
                        "model_role": "responder",
                        "confidence": 1.0,
                        "reason": "repeated_action observed, switching to respond",
                    }
                )
            return json.dumps(
                {
                    "tool": "delegate.list",
                    "permission": "read",
                    "args": {"limit": 20},
                    "model_role": "responder",
                    "confidence": 1.0,
                    "reason": "inspect current tasks",
                }
            )
        if role == "responder":
            self.responder_calls += 1
            content = "\n".join(message.content for message in messages)
            assert '"reason": "repeated_progress_signature"' in content
            assert '"tool": "delegate.list"' in content
            assert "Loop Reflection" not in content
            assert "Capability observations:" not in content
            return "当前没有任务。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


class _RepeatCompletionDeleteProvider:
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
        if role == "planner":
            assert output_schema is not None
            self.planner_calls += 1
            return json.dumps(
                {
                    "tool": "delegate.delete",
                    "permission": "write",
                    "args": {
                        "source": "weixin",
                        "kind": "delegation",
                        "phase": Phase.ENDED,
                        "reason": "delete all tasks",
                    },
                    "model_role": "planner",
                    "confidence": 1.0,
                    "reason": "delete all tasks",
                }
            )
        if role == "responder":
            self.responder_calls += 1
            content = "\n".join(message.content for message in messages)
            if '"cleanup_complete": true' not in content:
                assert '"reason": "repeated_progress_signature"' in content
                assert '"tool": "delegate.delete"' in content
                assert '"ok": false' in content
                return "当前渠道不能直接删除这些任务。"
            return "任务清理已完成。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


class _RepeatStatusDifferentArgsProvider:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
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
            if self.planner_calls > 5:
                return json.dumps(
                    {
                        "tool": "respond",
                        "permission": "read",
                        "args": {"message": "任务已过期。"},
                        "model_role": "responder",
                        "confidence": 1.0,
                        "reason": "repeated_action observed, switching to respond",
                    }
                )
            args = (
                {"query": f"task {self.run_id} approval history"}
                if self.planner_calls == 1
                else {"run_id": self.run_id}
            )
            return json.dumps(
                {
                    "tool": "delegate.state",
                    "permission": "read",
                    "args": args,
                    "model_role": "responder",
                    "confidence": 1.0,
                    "reason": "inspect run facts",
                }
            )
        if role == "responder":
            self.responder_calls += 1
            content = "\n".join(message.content for message in messages)
            assert self.run_id in content
            return "任务已过期。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


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
                        "tool": "respond",
                        "permission": "read",
                        "args": {"message": "需要审批后执行。"},
                        "model_role": "responder",
                        "confidence": 1.0,
                        "reason": "report approval pause facts",
                    }
                )
            return json.dumps(
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
            )
        if role == "responder":
            self.responder_calls += 1
            return "需要审批后执行。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


class _InvalidCapabilityArgsProvider:
    def __init__(self) -> None:
        self.call_count = 0
        self.responder_calls = 0
        self.last_prompt = ""

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict | None = None,
    ) -> str:
        if role == "planner" and output_schema is not None:
            self.call_count += 1
            self.last_prompt = "\n".join(message.content for message in messages)
            if self.call_count > 5:
                assert '"observation_type": "planner_error"' in self.last_prompt
                assert '"selected_tool": "respond"' in self.last_prompt
                assert "$.message is required" in self.last_prompt
                return json.dumps(
                    {
                        "tool": "respond",
                        "permission": "read",
                        "args": {"message": "回答完毕。"},
                        "model_role": "responder",
                        "confidence": 1.0,
                        "reason": "repeated_action observed, switching to respond",
                    }
                )
            return json.dumps(
                {
                    "tool": "respond",
                    "permission": "read",
                    "args": {},
                    "model_role": "responder",
                    "confidence": 1.0,
                    "reason": "attempt final answer",
                }
            )
        if role == "responder":
            self.responder_calls += 1
            content = "\n".join(message.content for message in messages)
            assert '"tool": "system.planner_error"' in content
            assert "$.message is required" in content
            return "回答完毕。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


class _WatchCreateThenRespondProvider:
    def __init__(self, run_at: float) -> None:
        self.run_at = run_at
        self.planner_calls = 0

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
            if self.planner_calls == 1:
                return json.dumps(
                    {
                        "tool": "watch.create",
                        "permission": "prepare",
                        "args": {
                            "prompt": "检查天气",
                            "kind": "once",
                            "run_at": self.run_at,
                        },
                        "model_role": "planner",
                    }
                )
            assert '"observation_type": "capability_result"' in content
            assert '"tool": "watch.create"' in content
            assert '"completion_evidence": true' in content
            assert "Watch created" not in content
            return json.dumps(
                {
                    "tool": "respond",
                    "permission": "read",
                    "args": {"message": "已创建。"},
                    "model_role": "responder",
                }
            )
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


for _provider_cls in (
    _PromptCaptureProvider,
    _RemoteDeleteUnavailableProvider,
    _RepeatListProvider,
    _RepeatCompletionDeleteProvider,
    _RepeatStatusDifferentArgsProvider,
    _InvalidCapabilityArgsProvider,
    _WatchCreateThenRespondProvider,
):
    _provider_cls.usage_for = _empty_provider_usage


@pytest.mark.asyncio
async def test_delegate_spawn_returns_existing_active_run_for_same_fact_scope(tmp_path):
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    args = {
        "objective": "在用户电脑上找到简历文件并发送给用户",
        "context": "用户请求发送简历文件。",
        "plan": "在工作区搜索简历文件。",
        "success_criteria": "返回搜索事实。",
    }

    first = await registry.invoke("delegate.spawn", args, permission="prepare", context=context)
    second = await registry.invoke("delegate.spawn", args, permission="prepare", context=context)

    runs = RunStore(tmp_path).list(limit=20)
    assert first.ok is True
    assert second.ok is True
    assert second.run_id == first.run_id
    assert second.facts["deduplicated"] is True
    assert len([run for run in runs if run.kind == "delegation"]) == 1


@pytest.mark.asyncio
async def test_delegate_spawn_deduplicates_same_objective_with_rewritten_plan(tmp_path):
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    objective = "在用户电脑上找到简历文件并发送给用户"

    first = await registry.invoke(
        "delegate.spawn",
        {
            "objective": objective,
            "context": "用户请求发送简历文件。",
            "plan": "在工作区搜索简历文件。",
            "success_criteria": "返回搜索事实。",
        },
        permission="prepare",
        context=context,
    )
    second = await registry.invoke(
        "delegate.spawn",
        {
            "objective": objective,
            "context": "用户请求获取电脑上的简历文件，当前系统空闲。",
            "plan": "在家目录、文档目录和下载目录搜索简历文件。",
            "success_criteria": "成功找到并发送简历，或说明未找到。",
        },
        permission="prepare",
        context=context,
    )

    assert first.ok is True
    assert second.ok is True
    assert second.run_id == first.run_id
    assert second.facts["deduplicated"] is True
    assert len([run for run in RunStore(tmp_path).list(limit=20) if run.kind == "delegation"]) == 1


@pytest.mark.asyncio
async def test_delegate_list_facts_omit_prompt_and_control_summaries(tmp_path):
    runs = RunStore(tmp_path)
    run = runs.create(
        "needs approval",
        prompt="Objective:\nrun something\n\nPlan:\napproval_code=123456",
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
        plan_summary="Plan:\nwait for approval_code=123456",
        result_summary="approval_requested\napproval_code=123456\nstatus=pending",
    )
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        permission_ceiling="read",
        workspace=str(tmp_path),
    )

    result = await registry.invoke(
        "delegate.list",
        {"limit": 20},
        permission="read",
        context=context,
    )

    run_fact = result.facts["runs"][0]
    assert "prompt" not in run_fact
    assert "plan_summary" not in run_fact
    assert "result_summary" not in run_fact
    assert "approval_code" not in json.dumps(result.facts, ensure_ascii=False)


@pytest.mark.asyncio
async def test_delegate_delete_blocks_linked_goal(tmp_path):
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="peer-1",
        sender_id="sender-1",
        source="local",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    spawned = await registry.invoke(
        "delegate.spawn",
        {
            "objective": "delete linked goal task",
            "context": "test",
            "plan": "test",
            "success_criteria": "test",
        },
        permission="prepare",
        context=context,
    )
    assert spawned.ok is True

    deleted = await registry.invoke(
        "delegate.delete",
        {"run_id": spawned.run_id, "reason": "test cleanup"},
        permission="write",
        context=context,
    )

    assert deleted.ok is True
    goal = GoalStore(tmp_path).get_by_run(spawned.run_id)
    assert goal is not None
    assert goal.phase == Phase.ENDED
    assert goal.resolution == Resolution.BLOCKED
    assert goal.blocked_reason == "delegation_run_deleted"


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

    monkeypatch.setattr(execution_module, "get_engine_class", lambda: _AskOnlyEngine)
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

    monkeypatch.setattr(execution_module, "get_engine_class", lambda: _CompletedEngine)
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
    engine = LoopEngine(
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
    assert provider.planner_calls == 1
    assert provider.responder_calls == 0
    events = TraceStore(tmp_path).list_events(result.trace_id)
    phases = [event.phase for event in events]
    assert "runtime.converged" not in phases
    assert all(event.tool != "delegate.delete" for event in events)


@pytest.mark.asyncio
async def test_completion_evidence_returns_to_model_for_response(tmp_path):
    provider = _WatchCreateThenRespondProvider(time.time() + 3600)
    engine = LoopEngine(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "一小时后提醒我检查天气",
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        session_alias="weixin:peer-1:sender-1",
    )

    assert result.text == "已创建。"
    assert result.action == "chat"
    assert provider.planner_calls == 2
    decisions = [
        json.loads(event.output_json)
        for event in TraceStore(tmp_path).list_events(result.trace_id)
        if event.phase == "loop.decision"
    ]
    assert any(
        decision.get("tool") == "watch.create"
        and decision.get("decision") == "continue"
        and decision.get("reason") == "completion_evidence_true"
        for decision in decisions
    )


@pytest.mark.asyncio
async def test_repeated_completion_evidence_finalizes_without_looping(tmp_path):
    provider = _RepeatCompletionDeleteProvider()
    engine = LoopEngine(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "删除所有的任务",
        peer_id="local",
        sender_id="local",
        source="local",
        session_alias="local:peer-1:sender-1",
    )

    assert 3 <= provider.planner_calls <= 4
    assert provider.responder_calls == 1
    assert result.text == "任务清理已完成。"
    assert result.action == "execute:system.task_complete"
    assert result.ok is True
    assert result.facts["cleanup_complete"] is True
    decisions = [
        json.loads(event.output_json)
        for event in TraceStore(tmp_path).list_events(result.trace_id)
        if event.phase == "loop.decision"
    ]
    assert decisions[-1]["decision"] == "finalize"
    assert decisions[-1]["reason"] == "completion_evidence_true"


@pytest.mark.asyncio
async def test_repeated_unavailable_remote_tool_does_not_complete_task(tmp_path):
    provider = _RepeatCompletionDeleteProvider()
    engine = LoopEngine(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "删除所有的任务",
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        session_alias="weixin:peer-1:sender-1",
    )

    assert 3 <= provider.planner_calls <= 4
    assert provider.responder_calls == 1
    assert result.text == ""
    assert result.action == "execute:system.task_complete"
    assert result.ok is True
    assert result.error_reason == ""
    decisions = [
        json.loads(event.output_json)
        for event in TraceStore(tmp_path).list_events(result.trace_id)
        if event.phase == "loop.decision"
    ]
    assert decisions[-1]["decision"] == "converged"
    assert decisions[-1]["reason"] == "repeated_progress_signature"
    assert decisions[-1]["failure_domain"] == "loop_no_progress"


@pytest.mark.asyncio
async def test_repeated_stable_capability_result_converges(tmp_path):
    provider = _RepeatListProvider()
    engine = LoopEngine(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "我们现在都要哪些任务",
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        session_alias="weixin:peer-1:sender-1",
    )

    assert result.text == "当前没有任务。"
    assert result.action == "execute:system.task_complete"
    assert 3 <= provider.planner_calls <= 4
    assert provider.responder_calls == 1
    decisions = [
        json.loads(event.output_json)
        for event in TraceStore(tmp_path).list_events(result.trace_id)
        if event.phase == "loop.decision"
    ]
    assert any(
        decision.get("tool") == "delegate.list"
        and decision.get("reason") == "repeated_progress_signature"
        and decision.get("decision") == "converged"
        for decision in decisions
    )


@pytest.mark.asyncio
async def test_same_state_facts_with_different_args_converges(tmp_path):
    runs = RunStore(tmp_path)
    run = runs.create(
        "find resume",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase=Phase.ENDED,
        resolution=Resolution.BLOCKED,
    )
    provider = _RepeatStatusDifferentArgsProvider(run.id)
    engine = LoopEngine(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "我们不是批准了这个任务吗",
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        session_alias="weixin:peer-1:sender-1",
    )

    assert result.text == "任务已过期。"
    assert result.action == "execute:system.task_complete"
    assert 3 <= provider.planner_calls <= 4
    assert provider.responder_calls == 1
    assert "prompt" not in json.dumps(result.facts, ensure_ascii=False)
    assert "plan_summary" not in json.dumps(result.facts, ensure_ascii=False)
    assert "result_summary" not in json.dumps(result.facts, ensure_ascii=False)


@pytest.mark.asyncio
async def test_planner_capability_args_schema_mismatch_triggers_loop(tmp_path):
    engine = LoopEngine(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=_InvalidCapabilityArgsProvider()),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "回答一下",
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        session_alias="weixin:peer-1:sender-1",
    )

    assert result.text == "回答完毕。"
    assert result.action == "execute:system.task_complete"
    assert result.ok is True
    assert result.error_reason == ""
    assert engine.runtime.provider.call_count == 3
    assert engine.runtime.provider.responder_calls == 1
    events = TraceStore(tmp_path).list_events(result.trace_id)
    assert any(event.phase == "planner.parse_error" for event in events)
    decisions = [
        json.loads(event.output_json)
        for event in events
        if event.phase == "loop.decision"
    ]
    assert decisions[-1]["decision"] == "converged"
    assert decisions[-1]["failure_domain"] == "loop_no_progress"


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
