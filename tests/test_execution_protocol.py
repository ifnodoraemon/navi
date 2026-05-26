from __future__ import annotations

import json
import subprocess

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
            "plan_id": "execute-basic",
            "steps": [
                {
                    "id": "answer",
                    "actions": [
                        {"tool": "provider.config", "permission": "read", "args": {}},
                        {"tool": "final.answer", "permission": "read", "args": {"message": "Answered with explicit evidence."}},
                    ],
                    "verification": {"checks": ["context reviewed"], "reason": "no actuator mutation needed"},
                    "on_failure": "stop",
                }
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
    assert recorded["steps"][0]["actions"][0]["status"] == "completed"
    assert recorded["steps"][0]["actions"][1]["status"] == "completed"
    capability = [item for item in recorded["evidence"] if item["kind"] == "capability_result"]
    assert capability[0]["tool"] == "provider.config"
    assert recorded["verification"]["status"] == "verified"


@pytest.mark.asyncio
async def test_watch_protocol_empty_model_evidence_is_actuated_and_logged(tmp_path):
    protocol = {
        "navi_execution": {
            "version": EXECUTION_PROTOCOL_VERSION,
            "phase": "watch",
            "task_id": "",
            "plan_id": "watch-answer",
            "steps": [
                {
                    "id": "notify",
                    "actions": [
                        {
                            "tool": "final.answer",
                            "permission": "read",
                            "args": {"message": "今晚的通识知识：证据由 Navi actuator 生成。"},
                        }
                    ],
                    "verification": {"checks": [], "reason": "scheduled notification"},
                    "on_failure": "stop",
                }
            ],
            "evidence": [],
            "verification": {"status": "proposed", "checks": [], "reason": "model proposed"},
            "completion": {"status": "proposed", "summary": "今晚的通识知识"},
        }
    }
    provider = ScriptedProvider(json.dumps(protocol))
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    result = await execution.run_watch(
        prompt="每天晚上8点讲解通识知识",
        source="watch",
        peer_id="peer",
        sender_id="sender",
        workspace=str(tmp_path),
    )

    assert result.exit_code == 0
    assert result.summary == "今晚的通识知识：证据由 Navi actuator 生成。"
    capability = [item for item in result.protocol.evidence if item["kind"] == "capability_result"]
    assert capability[0]["tool"] == "final.answer"
    assert result.protocol.verification["status"] == "verified"
    logs = TaskStore(tmp_path).list_execution_logs()
    assert {log.phase for log in logs} == {"watch", "watch_protocol"}
    protocol_log = next(log for log in logs if log.phase == "watch_protocol")
    recorded = json.loads(protocol_log.stdout)
    assert recorded["phase"] == "watch"
    assert recorded["evidence"]


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
    assert recorded["steps"][0]["actions"][0]["kind"] == "execution_error"


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
                    "plan_id": "bad-action",
                    "steps": [
                        {
                            "id": "bad",
                            "actions": [{"kind": "inspect", "target": "task_context"}],
                            "verification": {"checks": ["context"], "reason": "model claim"},
                            "on_failure": "stop",
                        }
                    ],
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
    assert updated.result_summary == "step 1 action 1 missing capability tool"
    protocol_log = next(log for log in tasks.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    assert recorded["completion"]["status"] == "failed"
    capability = [item for item in recorded["evidence"] if item["kind"] == "capability_result"]
    assert capability[0]["ok"] is False


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
                    "plan_id": "file-actuator",
                    "steps": [
                        {
                            "id": "write-read",
                            "actions": [
                                {
                                    "tool": "file.write",
                                    "permission": "write",
                                    "args": {"path": "notes/result.txt", "content": "actuated", "create_dirs": True},
                                },
                                {"tool": "file.read", "permission": "read", "args": {"path": "notes/result.txt"}},
                            ],
                            "verification": {
                                "checks": [
                                    {"type": "file.exists", "path": "notes/result.txt"},
                                    {"type": "file.contains", "path": "notes/result.txt", "text": "actuated"},
                                ],
                                "reason": "read back file",
                            },
                            "on_failure": "stop",
                        }
                    ],
                    "evidence": [{"kind": "model_plan", "summary": "write then read"}],
                    "verification": {"status": "proposed", "checks": [], "reason": "step verified"},
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
    capability = [item for item in recorded["evidence"] if item["kind"] == "capability_result"]
    assert [item["tool"] for item in capability] == ["file.write", "file.read"]
    assert capability[1]["facts"]["content"] == "actuated"
    verification = [item for item in recorded["evidence"] if item["kind"] == "verification_result"]
    assert [item["check"] for item in verification] == ["file.exists", "file.contains"]
    assert all(item["ok"] for item in verification)


@pytest.mark.asyncio
async def test_verifier_policy_can_fail_after_successful_capability_actions(tmp_path):
    tasks = TaskStore(tmp_path)
    task = tasks.create("Verifier task", prompt="write wrong content", workspace=str(tmp_path))
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "execute",
                    "task_id": task.id,
                    "plan_id": "verifier-failure",
                    "steps": [
                        {
                            "id": "write-wrong",
                            "actions": [
                                {
                                    "tool": "file.write",
                                    "permission": "write",
                                    "args": {"path": "notes/result.txt", "content": "actual", "create_dirs": True},
                                }
                            ],
                            "verification": {
                                "checks": [{"type": "file.contains", "path": "notes/result.txt", "text": "expected"}],
                                "reason": "expected artifact content",
                            },
                            "on_failure": "stop",
                        }
                    ],
                    "evidence": [{"kind": "model_plan", "summary": "write wrong content"}],
                    "verification": {"status": "proposed", "checks": [], "reason": "step verified"},
                    "completion": {"status": "proposed", "summary": "file updated"},
                }
            }
        )
    )
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    updated = await execution.execute_task(task)

    assert updated.status == "failed"
    assert updated.result_summary == "expected text not found"
    protocol_log = next(log for log in tasks.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    assert recorded["completion"]["status"] == "failed"
    assert recorded["verification"]["status"] == "failed"
    verification = [item for item in recorded["evidence"] if item["kind"] == "verification_result"]
    assert verification[0]["check"] == "file.contains"
    assert verification[0]["ok"] is False


@pytest.mark.asyncio
async def test_execution_rejects_plans_over_step_budget(tmp_path):
    tasks = TaskStore(tmp_path)
    task = tasks.create("Budget task", prompt="too many steps", workspace=str(tmp_path))
    steps = [
        {
            "id": f"step-{index}",
            "actions": [{"tool": "final.answer", "permission": "read", "args": {"message": str(index)}}],
            "verification": {"checks": [], "reason": "noop"},
        }
        for index in range(6)
    ]
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "execute",
                    "task_id": task.id,
                    "plan_id": "too-many-steps",
                    "steps": steps,
                    "evidence": [{"kind": "model_plan", "summary": "too many"}],
                    "verification": {"status": "proposed", "checks": [], "reason": "budget"},
                    "completion": {"status": "proposed", "summary": "too many"},
                }
            }
        )
    )
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    updated = await execution.execute_task(task)

    assert updated.status == "failed"
    assert updated.result_summary == "execution protocol exceeds step budget 5"


@pytest.mark.asyncio
async def test_execution_retry_once_policy_repeats_failed_step(tmp_path):
    tasks = TaskStore(tmp_path)
    task = tasks.create("Retry step task", prompt="retry failed step", workspace=str(tmp_path))
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "execute",
                    "task_id": task.id,
                    "plan_id": "retry-once",
                    "steps": [
                        {
                            "id": "missing-file",
                            "actions": [{"tool": "file.read", "permission": "read", "args": {"path": "missing.txt"}}],
                            "verification": {"checks": [], "reason": "read missing file"},
                            "on_failure": "retry_once",
                        }
                    ],
                    "evidence": [{"kind": "model_plan", "summary": "retry"}],
                    "verification": {"status": "proposed", "checks": [], "reason": "retry"},
                    "completion": {"status": "proposed", "summary": "retry"},
                }
            }
        )
    )
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    updated = await execution.execute_task(task)

    assert updated.status == "failed"
    protocol_log = next(log for log in tasks.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    capability = [item for item in recorded["evidence"] if item["kind"] == "capability_result"]
    assert [item["attempt"] for item in capability] == [1, 2]
    recovery = [item for item in recorded["evidence"] if item["kind"] == "recovery_decision"]
    assert recovery[0]["policy"] == "retry_once"


@pytest.mark.asyncio
async def test_failed_dirty_execution_records_rollback_hint(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    tasks = TaskStore(tmp_path)
    task = tasks.create("Rollback hint task", prompt="write wrong file", workspace=str(project))
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "execute",
                    "task_id": task.id,
                    "plan_id": "dirty-failure",
                    "steps": [
                        {
                            "id": "write-wrong",
                            "actions": [
                                {
                                    "tool": "file.write",
                                    "permission": "write",
                                    "args": {"path": "artifact.txt", "content": "actual"},
                                }
                            ],
                            "verification": {
                                "checks": [{"type": "file.contains", "path": "artifact.txt", "text": "expected"}],
                                "reason": "expected content",
                            },
                            "on_failure": "stop",
                        }
                    ],
                    "evidence": [{"kind": "model_plan", "summary": "dirty failure"}],
                    "verification": {"status": "proposed", "checks": [], "reason": "dirty failure"},
                    "completion": {"status": "proposed", "summary": "dirty failure"},
                }
            }
        )
    )
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    updated = await execution.execute_task(task)

    assert updated.status == "failed"
    protocol_log = next(log for log in tasks.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    rollback = [item for item in recorded["evidence"] if item["kind"] == "rollback_hint"]
    assert rollback
    assert "artifact.txt" in rollback[0]["after_git_status"]
