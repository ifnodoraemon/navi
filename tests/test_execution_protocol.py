from __future__ import annotations

import json

import pytest

from navi.execution import EXECUTION_PROTOCOL_VERSION, ExecutionService, NaviExecutionProvider
from navi.provider import ChatMessage, ModelPool
from navi.tasks import TaskStore


class ScriptedProvider:
    def __init__(self, response: str):
        self.response = response
        self.messages: list[list[ChatMessage]] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages.append(messages)
        return self.response


@pytest.mark.asyncio
async def test_execution_uses_structured_actuator_protocol(tmp_path):
    protocol = {
        "navi_execution": {
            "version": EXECUTION_PROTOCOL_VERSION,
            "phase": "execute",
            "actions": [
                {"kind": "inspect", "target": "task_context", "status": "completed"},
                {"kind": "mutation", "target": "none", "status": "not_performed"},
            ],
            "evidence": [{"kind": "observation", "summary": "No filesystem mutation was available to this pass."}],
            "verification": {"status": "verified", "checks": ["context reviewed"], "reason": "no actuator mutation needed"},
            "completion": {"status": "completed", "summary": "Answered with explicit evidence."},
        }
    }
    provider = ScriptedProvider(json.dumps(protocol))
    tasks = TaskStore(tmp_path)
    task = tasks.create("Protocol task", prompt="summarize local state", workspace=str(tmp_path))
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    updated = await execution.execute_task(task)

    assert updated.status == "completed"
    assert updated.result_summary == "Answered with explicit evidence."
    system_prompt = provider.messages[0][0].content
    assert "navi_execution" in system_prompt
    assert "actions" in system_prompt
    assert "evidence" in system_prompt
    assert "verification" in system_prompt

    protocol_logs = [log for log in tasks.list_execution_logs(task.id) if log.phase == "execute_protocol"]
    assert len(protocol_logs) == 1
    recorded = json.loads(protocol_logs[0].stdout)
    assert recorded["version"] == EXECUTION_PROTOCOL_VERSION
    assert recorded["phase"] == "execute"
    assert recorded["task_id"] == task.id
    assert recorded["actions"][1]["status"] == "not_performed"
    assert recorded["verification"]["status"] == "verified"


@pytest.mark.asyncio
async def test_free_form_execution_output_gets_unverified_protocol_fallback(tmp_path):
    provider = ScriptedProvider("Plain execution response")
    tasks = TaskStore(tmp_path)
    task = tasks.create("Fallback task", prompt="answer plainly", workspace=str(tmp_path))
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    updated = await execution.execute_task(task)

    assert updated.status == "completed"
    assert updated.result_summary == "Plain execution response"
    protocol_log = next(log for log in tasks.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    assert recorded["verification"]["status"] == "unverified"
    assert recorded["actions"][0]["kind"] == "model_response"
    assert recorded["evidence"][0]["kind"] == "model_output"
