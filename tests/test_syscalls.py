from __future__ import annotations

import pytest

from navi.capabilities import CapabilityRegistry
from navi.prompt_os import assemble_planner_system_prompt, assemble_planner_turn_input
from navi.syscalls import ModelSyscallPlanner, _extract_json_object
from navi.provider import ChatMessage, ModelPool


class ScriptedProvider:
    def __init__(self, response: str):
        self.response = response
        self.messages: list[ChatMessage] = []
        self.output_schema = None

    async def complete(self, messages: list[ChatMessage], *, output_schema=None) -> str:
        self.messages = messages
        self.output_schema = output_schema
        return self.response


def _tools(tmp_path):
    return CapabilityRegistry(home=tmp_path, project_dir=tmp_path).list_specs()


def test_model_syscall_planner_prompt_loads_routing_rules_from_spec():
    system = assemble_planner_system_prompt().render()

    assert "TASK ROUTING RULES" in system
    assert "PROMPT BOUNDARIES" in system
    assert "OBSERVATION INVARIANTS" in system
    assert "Use delegation runs for complex local work" in system
    assert "After delegate.spawn" in system
    assert "cleanup_complete=true" in system
    assert "SECURITY GUIDELINE" in system


@pytest.mark.asyncio
async def test_model_syscall_planner_asks_when_schedule_time_is_vague(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"clarify.ask","args":{"message":"你希望每天晚上几点上通识课？"},"confidence":0.92,"reason":"recurring request has vague time"}'
    )
    planner = ModelSyscallPlanner(ModelPool(default=provider))

    call = await planner.plan("每天晚上上一个通识课给我", tools=_tools(tmp_path))

    assert call.tool == "clarify.ask"
    assert "几点" in call.message
    system = provider.messages[0].content
    assert "model syscall planner" in system
    assert "capability manifest" in system
    assert "Built-in control tools" not in provider.messages[1].content
    assert "[PERMISSION CEILING]" in provider.messages[1].content
    assert "<user_message>" in provider.messages[1].content
    assert "[MODEL ROLES]" in provider.messages[1].content
    assert "[MODEL ROLE CONTRACTS]" in provider.messages[1].content
    assert "critic" in provider.messages[1].content
    assert "executor" in provider.messages[1].content
    assert "[TOOL MANIFEST]" in provider.messages[1].content
    assert "clarify.ask" in provider.messages[1].content
    assert provider.output_schema["name"] == "navi_syscall"


@pytest.mark.asyncio
async def test_model_syscall_planner_receives_recent_conversation_context(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"delegate.spawn","permission":"prepare","args":{"prompt":"删除上一轮确认要删除的旧任务入口"},"confidence":0.9,"reason":"follow-up refers to recent conversation"}'
    )
    planner = ModelSyscallPlanner(ModelPool(default=provider))

    call = await planner.plan(
        "删除",
        tools=_tools(tmp_path),
        conversation_context="assistant: 旧任务入口可以删除，是否继续？",
    )

    assert call.tool == "delegate.spawn"
    assert call.args["prompt"] == "删除上一轮确认要删除的旧任务入口"
    assert "CONVERSATION HISTORY" in provider.messages[1].content
    assert "旧任务入口可以删除" in provider.messages[1].content


@pytest.mark.asyncio
async def test_model_syscall_planner_parses_watch_syscall(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"watch.create","permission":"prepare","args":{"prompt":"上一个通识课给我","cron":"0 21 * * *"},"confidence":0.95,"reason":"exact time provided"}'
    )
    planner = ModelSyscallPlanner(ModelPool(default=provider))

    call = await planner.plan("每天 21 点上一个通识课给我", tools=_tools(tmp_path))

    assert call.permission == "prepare"
    assert call.tool == "watch.create"
    assert call.args == {"prompt": "上一个通识课给我", "cron": "0 21 * * *"}


@pytest.mark.asyncio
async def test_model_syscall_planner_parses_read_syscall(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"service.status","permission":"read","args":{"name":"navi.service"},"confidence":0.95,"reason":"status lookup"}'
    )
    planner = ModelSyscallPlanner(ModelPool(default=provider))

    call = await planner.plan("检查 navi.service 状态", tools=_tools(tmp_path))

    assert call.tool == "service.status"
    assert call.permission == "read"
    assert call.args == {"name": "navi.service"}


@pytest.mark.asyncio
async def test_model_syscall_planner_parses_approval_syscall(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"approval.resolve","permission":"write","args":{"decision":"approve","code":"123456"},"confidence":0.95,"reason":"explicit approval"}'
    )
    planner = ModelSyscallPlanner(ModelPool(default=provider))

    call = await planner.plan("批准 123456", tools=_tools(tmp_path))

    assert call.permission == "write"
    assert call.tool == "approval.resolve"
    assert call.args == {"decision": "approve", "code": "123456"}


@pytest.mark.asyncio
async def test_model_syscall_planner_prompt_routes_engineering_investigation_to_task(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"delegate.spawn","permission":"prepare","args":{"prompt":"检查配置到运行时的映射问题"},"confidence":0.9,"reason":"engineering investigation"}'
    )
    planner = ModelSyscallPlanner(ModelPool(default=provider))

    call = await planner.plan("配置项写了但运行时好像没消费，帮我检查配置到运行时的映射问题", tools=_tools(tmp_path))

    assert call.tool == "delegate.spawn"
    assert "configuration-to-runtime mapping" in provider.messages[0].content


@pytest.mark.asyncio
async def test_model_syscall_planner_preserves_approval_args(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"approval.resolve","permission":"write","args":{"decision":"reject","run_id":"123456"},"confidence":0.95,"reason":"explicit rejection"}'
    )
    planner = ModelSyscallPlanner(ModelPool(default=provider))

    call = await planner.plan("拒绝 123456", tools=_tools(tmp_path))

    assert call.tool == "approval.resolve"
    assert call.args == {"decision": "reject", "run_id": "123456"}


@pytest.mark.asyncio
async def test_model_syscall_planner_preserves_declared_permission(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"approval.resolve","permission":"prepare","args":{"decision":"approve","code":"123456"},"confidence":0.95,"reason":"explicit approval"}'
    )
    planner = ModelSyscallPlanner(ModelPool(default=provider))

    call = await planner.plan("批准 123456", tools=_tools(tmp_path))

    assert call.tool == "approval.resolve"
    assert call.permission == "prepare"


@pytest.mark.asyncio
async def test_model_syscall_planner_does_not_infer_missing_decision(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"approval.resolve","permission":"write","args":{"code":"123456"},"confidence":0.95,"reason":"User explicitly requested to reject the given code."}'
    )
    planner = ModelSyscallPlanner(ModelPool(default=provider))

    call = await planner.plan("拒绝 123456", tools=_tools(tmp_path))

    assert call.tool == "approval.resolve"
    assert call.args == {"code": "123456"}


@pytest.mark.asyncio
async def test_model_syscall_planner_parses_model_role(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"service.status","permission":"read","args":{"name":"navi.service"},"model_role":"observer","confidence":0.95,"reason":"status lookup"}'
    )
    planner = ModelSyscallPlanner(ModelPool(default=provider))

    call = await planner.plan(
        "检查 navi.service 状态",
        tools=_tools(tmp_path),
        model_roles=["planner", "observer", "responder"],
    )

    assert call.model_role == "observer"
    assert '"observer"' in provider.messages[1].content


def test_extract_json_object_handles_nested_braces_and_fenced_text():
    text = 'before```json\n{"tool":"final.answer","args":{"message":"a } brace"}}\n```after'

    extracted = _extract_json_object(text)

    assert extracted == '{"tool":"final.answer","args":{"message":"a } brace"}}'


def test_prompt_os_assembles_planner_policy_and_turn_data_separately(tmp_path):
    tools = _tools(tmp_path)
    system = assemble_planner_system_prompt()
    turn = assemble_planner_turn_input(
        "创建一个提醒",
        tools=tools,
        conversation_context="assistant: 之前没有创建。",
        observations=['{"state_transition":"created","turn_scope":"current"}'],
    )

    system_manifest = system.manifest()
    turn_manifest = turn.manifest()

    assert system_manifest["name"] == "planner_system"
    assert turn_manifest["name"] == "planner_turn_input"
    assert "TASK ROUTING RULES" in system.render()
    assert "[TOOL MANIFEST]" not in system.render()
    assert "<user_message>" in turn.render()
    assert "[TOOL MANIFEST]" in turn.render()
    assert any(block["tier"] == "manifest" for block in turn_manifest["blocks"])
    assert any(block["source"] == "capability_registry" for block in turn_manifest["blocks"])
