from __future__ import annotations

import json
import subprocess

import pytest

from navi.execution import EXECUTION_PROTOCOL_VERSION, SUBAGENT_EXECUTOR_ROLE, ExecutionService, NaviExecutionProvider
from navi.goals import GOAL_STATUS_VERIFIED_COMPLETE, GoalStore
from navi.provider import ChatMessage, ModelPool
from navi.runs import RunStore
from navi.subagents import SubagentRunStore


class ScriptedProvider:
    def __init__(self, response: str | list[str]):
        self.response = response
        self.messages: list[list[ChatMessage]] = []
        self.output_schemas: list[dict | None] = []

    async def complete(self, messages: list[ChatMessage], *, output_schema=None) -> str:
        self.messages.append(messages)
        self.output_schemas.append(output_schema)
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response


@pytest.mark.asyncio
async def test_execution_uses_structured_actuator_protocol(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("Protocol task", prompt="summarize local state", workspace=str(tmp_path))
    protocol = {
        "navi_execution": {
            "version": EXECUTION_PROTOCOL_VERSION,
            "phase": "execute",
            "run_id": task.id,
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
    execution_logs = [log for log in runs.list_execution_logs(task.id) if log.phase == "execute"]
    assert execution_logs[0].command.startswith(f"navi subagent {SUBAGENT_EXECUTOR_ROLE} execute")
    system_prompt = provider.messages[0][0].content
    assert "executor sub-agent" in system_prompt
    schema = provider.output_schemas[0]["schema"]["properties"]["navi_execution"]
    assert schema["properties"]["steps"]["items"]["properties"]["actions"]
    assert schema["properties"]["evidence"]
    assert schema["properties"]["verification"]

    protocol_logs = [log for log in runs.list_execution_logs(task.id) if log.phase == "execute_protocol"]
    assert len(protocol_logs) == 1
    recorded = json.loads(protocol_logs[0].stdout)
    assert recorded["version"] == EXECUTION_PROTOCOL_VERSION
    assert recorded["phase"] == "execute"
    assert recorded["run_id"] == task.id
    assert recorded["steps"][0]["actions"][0]["status"] == "completed"
    assert recorded["steps"][0]["actions"][1]["status"] == "completed"
    capability = [item for item in recorded["evidence"] if item["kind"] == "capability_result"]
    assert capability[0]["tool"] == "provider.config"
    assert recorded["verification"]["status"] == "verified"
    critic_log = next(log for log in runs.list_execution_logs(task.id) if log.phase == "critic")
    assert json.loads(critic_log.stdout)["passed"] is True
    subagents = SubagentRunStore(tmp_path).list(run_id=task.id)
    assert [(item.role, item.phase, item.status) for item in subagents] == [
        ("critic", "verify", "completed"),
        ("executor", "execute", "completed"),
    ]


@pytest.mark.asyncio
async def test_watch_notification_protocol_summary_is_logged_without_actuator(tmp_path):
    protocol = {
        "navi_execution": {
            "version": EXECUTION_PROTOCOL_VERSION,
            "phase": "watch",
            "run_id": "",
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
    assert result.protocol.steps[0]["actions"][0]["kind"] == "watch_notification"
    assert result.protocol.verification["status"] == "completed"
    logs = RunStore(tmp_path).list_execution_logs()
    assert {log.phase for log in logs} == {"watch", "watch_protocol"}
    protocol_log = next(log for log in logs if log.phase == "watch_protocol")
    recorded = json.loads(protocol_log.stdout)
    assert recorded["phase"] == "watch"
    assert recorded["evidence"][0]["kind"] == "internal_state"


@pytest.mark.asyncio
async def test_free_form_execution_output_fails_required_protocol(tmp_path):
    provider = ScriptedProvider("Plain execution response")
    runs = RunStore(tmp_path)
    task = runs.create("Strict task", prompt="answer plainly", workspace=str(tmp_path))
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    updated = await execution.execute_task(task)

    assert updated.status == "failed"
    assert updated.result_summary == "execution protocol missing navi_execution object"
    assert updated.error == "execution protocol missing navi_execution object"
    protocol_log = next(log for log in runs.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    assert recorded["completion"]["status"] == "failed"
    assert recorded["verification"]["reason"] == "provider output violated the required execution protocol"


@pytest.mark.asyncio
async def test_execution_protocol_shape_error_gets_one_repair_attempt(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("Repair protocol task", prompt="prepare a simple answer", workspace=str(tmp_path))
    repaired = {
        "navi_execution": {
            "version": EXECUTION_PROTOCOL_VERSION,
            "phase": "prepare",
            "run_id": task.id,
            "plan_id": "repair-ok",
            "steps": [
                {
                    "id": "answer",
                    "actions": [{"tool": "final.answer", "permission": "read", "args": {"message": "准备完成。"}}],
                    "verification": {"checks": [], "reason": "corrected schema"},
                    "on_failure": "stop",
                }
            ],
            "evidence": [{"kind": "model_repair", "summary": "schema corrected"}],
            "verification": {"status": "proposed", "checks": [], "reason": "corrected schema"},
            "completion": {"status": "proposed", "summary": "准备完成。"},
        }
    }
    provider = ScriptedProvider(
        [
            json.dumps(
                {
                    "navi_execution": {
                        "version": EXECUTION_PROTOCOL_VERSION,
                        "phase": "prepare",
                        "run_id": task.id,
                        "plan_id": "bad-shape",
                        "steps": [
                            {
                                "id": "bad",
                                "actions": [{"tool": "final.answer", "permission": "read", "args": {"message": "bad"}}],
                                "verification": ["bad shape"],
                            }
                        ],
                        "evidence": {"bad": "shape"},
                        "verification": ["bad shape"],
                        "completion": {"status": "proposed"},
                    }
                }
            ),
            json.dumps(repaired),
        ]
    )
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    updated = await execution.plan_task(task)

    assert updated.status == "prepared"
    assert updated.plan_summary == "准备完成。"
    assert len(provider.messages) == 2
    assert provider.output_schemas[0]["name"] == "navi_prepare_execution"
    assert "execution protocol" in provider.messages[1][-1].content
    prepare_log = next(log for log in runs.list_execution_logs(task.id) if log.phase == "prepare")
    assert "--protocol-repair" in prepare_log.command


@pytest.mark.asyncio
async def test_watch_notification_does_not_fail_on_malformed_execution_protocol(tmp_path):
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "watch",
                    "plan_id": "watch-pmp-001",
                    "steps": [
                        {
                            "id": "notify",
                            "actions": [{"tool": "final.answer", "permission": "read", "args": {"message": "pmp"}}],
                            "verification": ["bad shape from real provider"],
                        }
                    ],
                    "evidence": [{"kind": "provider", "summary": "pmp"}],
                    "verification": ["bad top-level shape"],
                    "completion": {"status": "completed", "summary": "pmp"},
                }
            }
        )
    )
    execution = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    result = await execution.run_watch(
        prompt="pmp",
        source="watch",
        peer_id="peer",
        sender_id="sender",
        workspace=str(tmp_path),
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.summary == "pmp"
    assert result.protocol.phase == "watch"
    assert result.protocol.completion["status"] == "completed"
    assert "navi_execution" not in provider.messages[0][0].content


@pytest.mark.asyncio
async def test_watch_notification_extracts_summary_from_valid_protocol(tmp_path):
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "watch",
                    "plan_id": "watch-summary",
                    "steps": [
                        {
                            "id": "notify",
                            "actions": [{"tool": "final.answer", "permission": "read", "args": {"message": "PMP reminder"}}],
                            "verification": {"checks": [], "reason": "notification only"},
                        }
                    ],
                    "evidence": [{"kind": "provider", "summary": "PMP reminder"}],
                    "verification": {"status": "completed", "checks": [], "reason": "notification only"},
                    "completion": {"status": "completed", "summary": "PMP reminder"},
                }
            }
        )
    )
    execution = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    result = await execution.run_watch(
        prompt="pmp",
        source="watch",
        peer_id="peer",
        sender_id="sender",
        workspace=str(tmp_path),
    )

    assert result.exit_code == 0
    assert result.summary == "PMP reminder"


@pytest.mark.asyncio
async def test_watch_notification_retries_title_only_output(tmp_path):
    provider = ScriptedProvider(
        [
            "通识讲解",
            "通识讲解：今天用一个例子理解沉没成本。已经付出的成本不应该决定下一步选择，关键是继续投入是否还能带来新的价值。日常决策里可以问自己：如果今天才开始，我还会选它吗？",
        ]
    )
    execution = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    result = await execution.run_watch(
        prompt="通识讲解",
        source="watch",
        peer_id="peer",
        sender_id="sender",
        workspace=str(tmp_path),
    )

    assert result.exit_code == 0
    assert result.summary.startswith("通识讲解：今天用一个例子理解沉没成本")
    assert len(provider.messages) == 2


@pytest.mark.asyncio
async def test_protocol_actions_must_be_capability_calls(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("Actuator task", prompt="inspect without a tool", workspace=str(tmp_path))
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "execute",
                    "run_id": task.id,
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
    protocol_log = next(log for log in runs.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    assert recorded["completion"]["status"] == "failed"
    capability = [item for item in recorded["evidence"] if item["kind"] == "capability_result"]
    assert capability[0]["ok"] is False


@pytest.mark.asyncio
async def test_protocol_actions_execute_local_file_actuators(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("File actuator task", prompt="write a note", workspace=str(tmp_path))
    goal = GoalStore(tmp_path).create(objective=task.prompt, run_id=task.id, workspace=task.workspace)
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "execute",
                    "run_id": task.id,
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
    protocol_log = next(log for log in runs.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    capability = [item for item in recorded["evidence"] if item["kind"] == "capability_result"]
    assert [item["tool"] for item in capability] == ["file.write", "file.read"]
    assert capability[1]["facts"]["content"] == "actuated"
    verification = [item for item in recorded["evidence"] if item["kind"] == "verification_result"]
    assert [item["check"] for item in verification] == ["file.exists", "file.contains"]
    assert all(item["ok"] for item in verification)
    updated_goal = GoalStore(tmp_path).get(goal.id)
    assert updated_goal is not None
    assert updated_goal.status == GOAL_STATUS_VERIFIED_COMPLETE


@pytest.mark.asyncio
async def test_critic_gate_blocks_mutation_without_independent_verification(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("Unverified mutation", prompt="write without checking", workspace=str(tmp_path))
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "execute",
                    "run_id": task.id,
                    "plan_id": "unverified-mutation",
                    "steps": [
                        {
                            "id": "write-only",
                            "actions": [
                                {
                                    "tool": "file.write",
                                    "permission": "write",
                                    "args": {"path": "notes/result.txt", "content": "actuated", "create_dirs": True},
                                }
                            ],
                            "verification": {"checks": [], "reason": "model says done"},
                            "on_failure": "stop",
                        }
                    ],
                    "evidence": [{"kind": "model_plan", "summary": "write only"}],
                    "verification": {"status": "proposed", "checks": [], "reason": "model says done"},
                    "completion": {"status": "proposed", "summary": "file updated"},
                }
            }
        )
    )
    execution = ExecutionService(tmp_path)
    execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    updated = await execution.execute_task(task)

    assert updated.status == "failed"
    assert "critic gate blocked completion" in updated.error
    assert "mutating execution lacks independent verification checks" in updated.error
    critic_log = next(log for log in runs.list_execution_logs(task.id) if log.phase == "critic")
    critic = json.loads(critic_log.stdout)
    assert critic["passed"] is False
    assert "mutating execution lacks independent verification checks" in critic["findings"]


@pytest.mark.asyncio
async def test_verifier_policy_can_fail_after_successful_capability_actions(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("Verifier task", prompt="write wrong content", workspace=str(tmp_path))
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "execute",
                    "run_id": task.id,
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
    protocol_log = next(log for log in runs.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    assert recorded["completion"]["status"] == "failed"
    assert recorded["verification"]["status"] == "failed"
    verification = [item for item in recorded["evidence"] if item["kind"] == "verification_result"]
    assert verification[0]["check"] == "file.contains"
    assert verification[0]["ok"] is False


@pytest.mark.asyncio
async def test_execution_rejects_plans_over_step_budget(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("Budget task", prompt="too many steps", workspace=str(tmp_path))
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
                    "run_id": task.id,
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
    runs = RunStore(tmp_path)
    task = runs.create("Retry step task", prompt="retry failed step", workspace=str(tmp_path))
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "execute",
                    "run_id": task.id,
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
    protocol_log = next(log for log in runs.list_execution_logs(task.id) if log.phase == "execute_protocol")
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
    runs = RunStore(tmp_path)
    task = runs.create("Rollback hint task", prompt="write wrong file", workspace=str(project))
    provider = ScriptedProvider(
        json.dumps(
            {
                "navi_execution": {
                    "version": EXECUTION_PROTOCOL_VERSION,
                    "phase": "execute",
                    "run_id": task.id,
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
    protocol_log = next(log for log in runs.list_execution_logs(task.id) if log.phase == "execute_protocol")
    recorded = json.loads(protocol_log.stdout)
    rollback = [item for item in recorded["evidence"] if item["kind"] == "rollback_hint"]
    assert rollback
    assert "artifact.txt" in rollback[0]["after_git_status"]
