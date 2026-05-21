from __future__ import annotations

import pytest

from navi.action_tools import load_action_tool_specs
from navi.syscalls import ModelSyscallPlanner
from navi.provider import ChatMessage
from navi.tools import build_tool_gateway


class ScriptedProvider:
    def __init__(self, response: str):
        self.response = response
        self.messages: list[ChatMessage] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages = messages
        return self.response


def _tools(tmp_path):
    return [*load_action_tool_specs(), *build_tool_gateway(tmp_path, project_dir=tmp_path).list_specs()]


@pytest.mark.asyncio
async def test_model_syscall_planner_asks_when_schedule_time_is_vague(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"clarify.ask","args":{"message":"你希望每天晚上几点上通识课？"},"confidence":0.92,"reason":"recurring request has vague time"}'
    )
    planner = ModelSyscallPlanner(provider)

    call = await planner.plan("每天晚上上一个通识课给我", tools=_tools(tmp_path))

    assert call.tool == "clarify.ask"
    assert "几点" in call.message
    system = provider.messages[0].content
    assert "model syscall planner" in system
    assert "capability manifest" in system
    assert "Built-in control tools" not in provider.messages[1].content
    assert "Permission ceiling: write" in provider.messages[1].content
    assert "Available tools:" in provider.messages[1].content
    assert "clarify.ask" in provider.messages[1].content


@pytest.mark.asyncio
async def test_model_syscall_planner_receives_recent_conversation_context(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"task.create","permission":"prepare","args":{"prompt":"删除上一轮确认要删除的旧任务入口"},"confidence":0.9,"reason":"follow-up refers to recent conversation"}'
    )
    planner = ModelSyscallPlanner(provider)

    call = await planner.plan(
        "删除",
        tools=_tools(tmp_path),
        conversation_context="assistant: 旧任务入口可以删除，是否继续？",
    )

    assert call.tool == "task.create"
    assert call.args["prompt"] == "删除上一轮确认要删除的旧任务入口"
    assert "Recent conversation:" in provider.messages[1].content
    assert "旧任务入口可以删除" in provider.messages[1].content


@pytest.mark.asyncio
async def test_model_syscall_planner_parses_watch_syscall(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"watch.create","permission":"prepare","args":{"prompt":"上一个通识课给我","cron":"0 21 * * *"},"confidence":0.95,"reason":"exact time provided"}'
    )
    planner = ModelSyscallPlanner(provider)

    call = await planner.plan("每天 21 点上一个通识课给我", tools=_tools(tmp_path))

    assert call.permission == "prepare"
    assert call.tool == "watch.create"
    assert call.args == {"prompt": "上一个通识课给我", "cron": "0 21 * * *"}


@pytest.mark.asyncio
async def test_model_syscall_planner_parses_read_syscall(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"service.status","permission":"read","args":{"name":"navi.service"},"confidence":0.95,"reason":"status lookup"}'
    )
    planner = ModelSyscallPlanner(provider)

    call = await planner.plan("检查 navi.service 状态", tools=_tools(tmp_path))

    assert call.tool == "service.status"
    assert call.permission == "read"
    assert call.args == {"name": "navi.service"}


@pytest.mark.asyncio
async def test_model_syscall_planner_parses_approval_syscall(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"approval.resolve","permission":"write","args":{"decision":"approve","code":"123456"},"confidence":0.95,"reason":"explicit approval"}'
    )
    planner = ModelSyscallPlanner(provider)

    call = await planner.plan("批准 123456", tools=_tools(tmp_path))

    assert call.permission == "write"
    assert call.tool == "approval.resolve"
    assert call.args == {"decision": "approve", "code": "123456"}


@pytest.mark.asyncio
async def test_model_syscall_planner_prompt_routes_engineering_investigation_to_task(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"task.create","permission":"prepare","args":{"prompt":"检查配置到运行时的映射问题"},"confidence":0.9,"reason":"engineering investigation"}'
    )
    planner = ModelSyscallPlanner(provider)

    call = await planner.plan("配置项写了但运行时好像没消费，帮我检查配置到运行时的映射问题", tools=_tools(tmp_path))

    assert call.tool == "task.create"
    assert "config-to-runtime mapping" in provider.messages[1].content


@pytest.mark.asyncio
async def test_model_syscall_planner_preserves_approval_args(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"approval.resolve","permission":"write","args":{"decision":"reject","task_id":"123456"},"confidence":0.95,"reason":"explicit rejection"}'
    )
    planner = ModelSyscallPlanner(provider)

    call = await planner.plan("拒绝 123456", tools=_tools(tmp_path))

    assert call.tool == "approval.resolve"
    assert call.args == {"decision": "reject", "task_id": "123456"}


@pytest.mark.asyncio
async def test_model_syscall_planner_preserves_declared_permission(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"approval.resolve","permission":"prepare","args":{"decision":"approve","code":"123456"},"confidence":0.95,"reason":"explicit approval"}'
    )
    planner = ModelSyscallPlanner(provider)

    call = await planner.plan("批准 123456", tools=_tools(tmp_path))

    assert call.tool == "approval.resolve"
    assert call.permission == "prepare"


@pytest.mark.asyncio
async def test_model_syscall_planner_does_not_infer_missing_decision(tmp_path):
    provider = ScriptedProvider(
        '{"tool":"approval.resolve","permission":"write","args":{"code":"123456"},"confidence":0.95,"reason":"User explicitly requested to reject the given code."}'
    )
    planner = ModelSyscallPlanner(provider)

    call = await planner.plan("拒绝 123456", tools=_tools(tmp_path))

    assert call.tool == "approval.resolve"
    assert call.args == {"code": "123456"}
