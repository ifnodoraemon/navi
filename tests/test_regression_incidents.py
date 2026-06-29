from __future__ import annotations

import json
import sqlite3

import pytest
import yaml

from navi.engine import HernessEngine, _dynamic_intent_facts
from navi.event_bus import EventBus
from navi.evolution import EvolutionEngine, EvolutionLedger
from navi._engine_phases import EnginePhasesMixin
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.control import SurfaceContext
from navi.engine_types import AgentTurnResult
from navi.execution import ExecutionService
from navi.provider import ChatMessage, _extract_anthropic_content, _extract_openai_content
from navi.runtime import AgentRuntime
from navi.runs import RunStore
from navi.syscalls import ModelSyscallPlanner
from navi.trace import TraceStore


def test_evolution_ledger_uses_latest_run_id_schema(tmp_path):
    EvolutionLedger(tmp_path)

    with sqlite3.connect(tmp_path / "evolution.db") as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(evolution_events)").fetchall()}

    assert "run_id" in columns
    assert "task_id" not in columns


def test_provider_rejects_structured_json_hidden_in_reasoning_content():
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "reasoning_content": (
                        "reasoning omitted\nresponse"
                        '{"tool":"final.answer","permission":"read","args":{"message":"ok"}}'
                    ),
                },
                "finish_reason": "stop",
            }
        ]
    }

    with pytest.raises(RuntimeError, match="Provider response content is empty"):
        _extract_openai_content(data)


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
                "tool": "final.answer",
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
                    "tool": "final.answer",
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
    engine = HernessEngine(
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
    assert decision.tool == "final.answer"
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
        '```json\n{"tool":"final.answer","permission":"read","args":{},'
        '"model_role":"responder","confidence":1,"reason":"done"}\n```'
    )

    assert decision.tool == "system.planner_error"
    assert decision.reason == "planner returned invalid JSON"


def test_planner_parser_accepts_missing_optional_audit_fields():
    decision = ModelSyscallPlanner._parse_syscall(
        json.dumps(
            {
                "tool": "final.answer",
                "permission": "read",
                "args": {"message": "ok"},
                "model_role": "responder",
            }
        )
    )

    assert decision.tool == "final.answer"
    assert decision.confidence == 0.0
    assert decision.reason == ""


def test_planner_parser_rejects_missing_required_schema_fields():
    decision = ModelSyscallPlanner._parse_syscall(
        json.dumps(
            {
                "tool": "final.answer",
                "permission": "read",
                "args": {"message": "ok"},
            }
        )
    )

    assert decision.tool == "system.planner_error"
    assert decision.reason == "planner decision schema mismatch"
    assert "$.model_role is required" in decision.args["schema_errors"]


def test_completion_checker_ignores_unrelated_prepared_runs(tmp_path):
    runs = RunStore(tmp_path)
    runs.create(
        "old prepared task",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        status="prepared",
    )
    current = runs.create(
        "current task",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        status="awaiting_approval",
    )

    class Harness:
        home = tmp_path
        governed_workflow_id = ""

    block = EnginePhasesMixin._completion_block_reason(
        Harness(),
        [
            {
                "tool": "shell.run",
                "ok": False,
                "facts": {
                    "entity_type": "approval_request",
                    "run_id": current.id,
                    "state_transition": "created",
                },
            }
        ],
        state_context=SurfaceContext(
            home=tmp_path,
            source="weixin",
            peer_id="peer-1",
            sender_id="sender-1",
        ),
        current_run_id=current.id,
    )

    assert block is None


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


class _DeleteExpiredProvider:
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
            return json.dumps(
                {
                    "tool": "delegate.delete",
                    "permission": "write",
                    "args": {
                        "status": "expired",
                        "kind": "delegation",
                        "reason": "delete expired tasks",
                    },
                    "model_role": "responder",
                    "confidence": 1.0,
                    "reason": "clean up expired delegation runs",
                }
            )
        if role == "responder":
            self.responder_calls += 1
            content = "\n".join(message.content for message in messages)
            assert '"cleanup_complete": true' in content
            assert '"deleted_count": 1' in content
            return "已删除 1 个过期任务。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


class _ApproveCodeProvider:
    def __init__(self, code: str) -> None:
        self.code = code
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
            return json.dumps(
                {
                    "tool": "approval.resolve",
                    "permission": "write",
                    "args": {
                        "decision": "approve",
                        "selection": "explicit_code",
                        "code": self.code,
                    },
                    "model_role": "planner",
                    "confidence": 1.0,
                    "reason": "approve explicit code",
                }
            )
        if role in {"planner", "responder"}:
            self.responder_calls += 1
            content = "\n".join(message.content for message in messages)
            assert '"approval_status": "approved"' in content
            assert '"completion_evidence": true' in content
            return "批准成功，任务已进入队列。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


class _ApproveCodeNotInInputProvider(_ApproveCodeProvider):
    def __init__(self, code: str) -> None:
        self.code = code
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
            if self.planner_calls == 1:
                return json.dumps(
                    {
                        "tool": "approval.resolve",
                        "permission": "write",
                        "args": {
                            "decision": "approve",
                            "selection": "explicit_code",
                            "code": self.code,
                        },
                        "model_role": "planner",
                        "confidence": 1.0,
                        "reason": "try visible approval code",
                    }
                )
            content = "\n".join(message.content for message in messages)
            assert "approval_code_not_in_user_input" in content
            assert "visible_pending_approvals" in content
            return json.dumps(
                {
                    "tool": "final.answer",
                    "permission": "read",
                    "args": {"message": "当前输入没有包含审批码；我只看到了待审批事实。"},
                    "model_role": "responder",
                    "confidence": 1.0,
                    "reason": "report approval facts",
                }
            )
        raise AssertionError(f"unexpected role: {role}")


class _ApproveLatestVisibleBatchProvider:
    def __init__(self) -> None:
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
            if self.planner_calls == 1:
                return json.dumps(
                    {
                        "tool": "approval.resolve",
                        "permission": "write",
                        "args": {
                            "decision": "approve",
                            "selection": "latest_visible_batch",
                        },
                        "model_role": "planner",
                        "confidence": 1.0,
                        "reason": "try visible approval selection",
                    }
                )
            content = "\n".join(message.content for message in messages)
            assert "approval_code_required_in_user_input" in content
            return json.dumps(
                {
                    "tool": "final.answer",
                    "permission": "read",
                    "args": {"message": "需要当前输入中的审批码。"},
                    "model_role": "responder",
                    "confidence": 1.0,
                    "reason": "report approval facts",
                }
            )
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
            assert "Runtime convergence" not in content
            assert "Capability observations:" in content
            return "当前没有任务。"
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
            args = (
                {"query": f"task {self.run_id} approval history"}
                if self.planner_calls == 1
                else {"run_id": self.run_id}
            )
            return json.dumps(
                {
                    "tool": "delegate.status",
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
                        "tool": "final.answer",
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
    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict | None = None,
    ) -> str:
        del messages
        if role == "planner" and output_schema is not None:
            return json.dumps(
                {
                    "tool": "final.answer",
                    "permission": "read",
                    "args": {},
                    "model_role": "responder",
                    "confidence": 1.0,
                    "reason": "attempt final answer",
                }
            )
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


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
        status="queued",
    )

    result = await ExecutionService(tmp_path).execute_task(task)

    assert result.status == "blocked"
    assert result.result_summary == "请提供文件位置。"
    assert "waiting for user input" in result.error


@pytest.mark.asyncio
async def test_expired_task_cleanup_finishes_from_completion_facts(tmp_path):
    runs = RunStore(tmp_path)
    expired = runs.create(
        "在用户电脑上找到简历文件并发送给用户",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        status="expired",
    )
    provider = _DeleteExpiredProvider()
    engine = HernessEngine(
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

    assert result.text == ""
    assert result.terminal is True
    assert runs.get(expired.id) is None
    assert provider.planner_calls == 1
    assert provider.responder_calls == 0
    events = TraceStore(tmp_path).list_events(result.trace_id)
    phases = [event.phase for event in events]
    assert "runtime.converged" not in phases
    loop_decisions = [
        json.loads(event.output_json)
        for event in events
        if event.phase == "loop.decision"
    ]
    assert any(
        item["decision"] == "finalize" and item["reason"] == "completion_evidence_true"
        for item in loop_decisions
    )


@pytest.mark.asyncio
async def test_approval_resolve_finishes_from_completion_facts(tmp_path):
    runs = RunStore(tmp_path)
    run = runs.create(
        "approve gated task",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        status="awaiting_approval",
    )
    approval = runs.create_approval(
        run_id=run.id,
        peer_id="peer-1",
        sender_id="sender-1",
    )
    provider = _ApproveCodeProvider(approval.code)
    engine = HernessEngine(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        f"批准 {approval.code}",
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        session_alias="weixin:peer-1:sender-1",
    )

    assert result.text == ""
    assert runs.get(run.id).status == "queued"
    assert provider.planner_calls == 1
    assert provider.responder_calls == 0
    events = TraceStore(tmp_path).list_events(result.trace_id)
    phases = [event.phase for event in events]
    assert "runtime.converged" not in phases
    loop_decisions = [
        json.loads(event.output_json)
        for event in events
        if event.phase == "loop.decision"
    ]
    assert any(
        item["decision"] == "finalize" and item["reason"] == "completion_evidence_true"
        for item in loop_decisions
    )





@pytest.mark.asyncio
async def test_repeated_stable_capability_result_converges(tmp_path):
    provider = _RepeatListProvider()
    engine = HernessEngine(
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

    assert result.text == ""
    assert result.action == "execute:system.loop_converged"
    assert provider.planner_calls == 5
    assert provider.responder_calls == 0
    events = TraceStore(tmp_path).list_events(result.trace_id)
    phases = [event.phase for event in events]
    assert "runtime.converged" in phases
    loop_decisions = [
        json.loads(event.output_json)
        for event in events
        if event.phase == "loop.decision"
    ]
    assert any(
        item["decision"] == "converged" and item["reason"] == "repeated_progress_signature"
        for item in loop_decisions
    )


@pytest.mark.asyncio
async def test_same_status_facts_with_different_args_converges(tmp_path):
    runs = RunStore(tmp_path)
    run = runs.create(
        "find resume",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        status="expired",
    )
    provider = _RepeatStatusDifferentArgsProvider(run.id)
    engine = HernessEngine(
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

    assert result.text == ""
    assert result.action == "execute:system.loop_converged"
    assert provider.planner_calls == 5
    assert provider.responder_calls == 0
    events = TraceStore(tmp_path).list_events(result.trace_id)
    loop_decisions = [
        json.loads(event.output_json)
        for event in events
        if event.phase == "loop.decision"
    ]
    assert any(
        item["decision"] == "converged" and item["reason"] == "repeated_progress_signature"
        for item in loop_decisions
    )


@pytest.mark.asyncio
async def test_delegate_spawn_awaiting_approval_becomes_model_owned_answer(tmp_path):
    provider = _DelegateSpawnApprovalProvider(str(tmp_path))
    engine = HernessEngine(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
        event_bus=EventBus(),
    )

    try:
        result = await engine.handle(
            "在家目录查找简历",
            peer_id="peer-approval",
            sender_id="sender-approval",
            source="weixin",
            session_alias="weixin:peer-approval:sender-approval",
        )
    finally:
        await engine.shutdown()

    assert provider.planner_calls == 2
    assert provider.responder_calls == 0
    assert result.action == "chat"
    assert result.text == "需要审批后执行。"
    assert TraceStore(tmp_path).list_evaluations(result.trace_id)[0].outcome == "success"
    decisions = [
        json.loads(event.output_json)
        for event in TraceStore(tmp_path).list_loop_decisions(result.trace_id)
    ]
    assert any(
        item["decision"] == "pause_for_approval"
        and item["reason"] == "approval_required"
        for item in decisions
    )


@pytest.mark.asyncio
async def test_capability_input_schema_mismatch_triggers_loop(tmp_path):
    engine = HernessEngine(
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

    assert result.ok is False
    assert result.error_reason == "loop_converged"
    evaluations = TraceStore(tmp_path).list_evaluations(result.trace_id)
    assert evaluations[0].failure_domain == "loop_no_progress"
    loop_decisions = [
        json.loads(event.output_json)
        for event in TraceStore(tmp_path).list_events(result.trace_id)
        if event.phase == "loop.decision"
    ]
    assert loop_decisions[-1]["failure_domain"] == "loop_no_progress"
    assert loop_decisions[-1]["gate_results"][0]["name"] == "no_progress_gate"


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
