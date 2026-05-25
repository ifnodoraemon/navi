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
    tasks = TaskStore(tmp_path)
    task = tasks.create("Protocol task", prompt="summarize local state", workspace=str(tmp_path))
    protocol = {
        "navi_execution": {
            "version": EXECUTION_PROTOCOL_VERSION,
            "phase": "execute",
            "task_id": task.id,
            "actions": [
                {"tool": "provider.config", "permission": "read", "args": {}},
                {"tool": "final.answer", "permission": "read", "args": {"message": "Answered with explicit evidence."}},
            ],
            "evidence": [{"kind": "observation", "summary": "No filesystem mutation was available to this pass."}],
            "verification": {"status": "verified", "checks": ["context reviewed"], "reason": "no actuator mutation needed"},
            "completion": {"status": "completed", "summary": "Answered with explicit evidence."},
        }
    }
    provider = ScriptedProvider(json.dumps(protocol))
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
    assert recorded["actions"][0]["status"] == "completed"
    assert recorded["actions"][1]["status"] == "completed"
    assert recorded["evidence"][0]["kind"] == "capability_result"
    assert recorded["evidence"][0]["tool"] == "provider.config"
    assert recorded["verification"]["status"] == "verified"


@pytest.mark.asyncio
async def test_free_form_execution_output_fails_required_protocol(tmp_path):
    provider = ScriptedProvider("Plain execution response")
    tasks = TaskStore(tmp_path)
    task = tasks.create("Strict task", prompt="answer plainly", workspace=str(tmp_path))
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    updated = await execution.execute_task(task)

    assert updated.status == "failed"
    assert updated.result_summary == "execution protocol missing navi_execution object"
    assert updated.error == "execution protocol missing navi_execution object"
    protocol_log = next(log for log in tasks.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    assert recorded["completion"]["status"] == "failed"
    assert recorded["verification"]["reason"] == "provider output violated the required execution protocol"
    assert recorded["actions"][0]["kind"] == "execution_error"


@pytest.mark.asyncio
async def test_protocol_actions_must_be_capability_calls(tmp_path):
    tasks = TaskStore(tmp_path)
    task = tasks.create("Actuator task", prompt="inspect without a tool", workspace=str(tmp_path))
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "execute",
                    "task_id": task.id,
                    "actions": [{"kind": "inspect", "target": "task_context"}],
                    "evidence": [{"kind": "model_claim", "summary": "I inspected context."}],
                    "verification": {"status": "proposed", "checks": ["context"], "reason": "model claim"},
                    "completion": {"status": "proposed", "summary": "done"},
                }
            }
        )
    )
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    updated = await execution.execute_task(task)

    assert updated.status == "failed"
    assert updated.result_summary == "action 1 missing capability tool"
    protocol_log = next(log for log in tasks.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    assert recorded["completion"]["status"] == "failed"
    assert recorded["evidence"][0]["kind"] == "capability_result"
    assert recorded["evidence"][0]["ok"] is False


@pytest.mark.asyncio
async def test_protocol_actions_execute_local_file_actuators(tmp_path):
    tasks = TaskStore(tmp_path)
    task = tasks.create("File actuator task", prompt="write a note", workspace=str(tmp_path))
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "execute",
                    "task_id": task.id,
                    "actions": [
                        {
                            "tool": "file.write",
                            "permission": "write",
                            "args": {"path": "notes/result.txt", "content": "actuated", "create_dirs": True},
                        },
                        {"tool": "file.read", "permission": "read", "args": {"path": "notes/result.txt"}},
                    ],
                    "evidence": [{"kind": "model_plan", "summary": "write then read"}],
                    "verification": {"status": "proposed", "checks": ["file.read"], "reason": "read back file"},
                    "completion": {"status": "proposed", "summary": "file updated"},
                }
            }
        )
    )
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    updated = await execution.execute_task(task)

    assert updated.status == "completed"
    assert (tmp_path / "notes" / "result.txt").read_text(encoding="utf-8") == "actuated"
    protocol_log = next(log for log in tasks.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    assert [item["tool"] for item in recorded["evidence"]] == ["file.write", "file.read"]
    assert recorded["evidence"][1]["facts"]["content"] == "actuated"
