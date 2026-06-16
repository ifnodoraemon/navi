from __future__ import annotations

import json
from pathlib import Path

import pytest

from navi.governance import GovernanceEngine
from navi.runs import RunStore
from navi.tools import ToolResult, ToolSpec, TURN_CONTEXT


def test_governance_blocks_task_without_prepare_protocol(tmp_path):
    """Task with no prepare_protocol log should be classified as high risk → blocked."""
    runs = RunStore(tmp_path)
    task = runs.create("risky task", prompt="do something", workspace=str(tmp_path))
    runs.update_run(task.id, status="queued")
    task = runs.get(task.id)

    gov = GovernanceEngine(tmp_path)
    assert gov.execution_allowed(task) is False


def test_governance_allows_task_with_low_risk_protocol(tmp_path):
    """Task with prepare_protocol containing only low-risk tools should be allowed."""
    runs = RunStore(tmp_path)
    task = runs.create("safe task", prompt="list files", workspace=str(tmp_path))
    runs.update_run(task.id, status="queued")

    protocol = {
        "version": "navi.actuator.v1",
        "phase": "prepare",
        "run_id": task.id,
        "plan_id": f"prepare:{task.id}",
        "steps": [
            {
                "id": "step1",
                "actions": [
                    {"tool": "memory.list", "permission": "read", "args": {}},
                ],
            }
        ],
        "completion": {"status": "completed", "summary": "done"},
        "evidence": [],
        "verification": {"status": "verified", "checks": [], "reason": "ok"},
    }
    runs.add_execution_log(
        run_id=task.id,
        provider="navi",
        phase="prepare_protocol",
        command=f"navi protocol prepare {task.id}",
        stdout=json.dumps(protocol),
        stderr="",
        exit_code=0,
        started_at=1.0,
        ended_at=2.0,
    )

    task = runs.get(task.id)
    gov = GovernanceEngine(tmp_path)
    assert gov.execution_allowed(task) is True


def test_governance_blocks_task_with_high_risk_protocol(tmp_path):
    """Task with prepare_protocol containing high-risk tools should be blocked."""
    runs = RunStore(tmp_path)
    task = runs.create("write task", prompt="write files", workspace=str(tmp_path))
    runs.update_run(task.id, status="queued")

    protocol = {
        "version": "navi.actuator.v1",
        "phase": "prepare",
        "run_id": task.id,
        "plan_id": f"prepare:{task.id}",
        "steps": [
            {
                "id": "step1",
                "actions": [
                    {"tool": "shell.run", "permission": "write", "args": {"command": "rm -rf /"}},
                ],
            }
        ],
        "completion": {"status": "completed", "summary": "done"},
        "evidence": [],
        "verification": {"status": "verified", "checks": [], "reason": "ok"},
    }
    runs.add_execution_log(
        run_id=task.id,
        provider="navi",
        phase="prepare_protocol",
        command=f"navi protocol prepare {task.id}",
        stdout=json.dumps(protocol),
        stderr="",
        exit_code=0,
        started_at=1.0,
        ended_at=2.0,
    )

    task = runs.get(task.id)
    gov = GovernanceEngine(tmp_path)
    assert gov.execution_allowed(task) is False


def test_governance_allows_task_with_explicit_approval(tmp_path):
    """Task with explicit approval should always be allowed regardless of risk."""
    runs = RunStore(tmp_path)
    task = runs.create("approved task", prompt="do anything", workspace=str(tmp_path))
    runs.update_run(task.id, status="queued")

    approval = runs.create_approval(run_id=task.id, peer_id="test", sender_id="user")
    runs.resolve_approval(approval.code, "user", "approved")

    task = runs.get(task.id)
    gov = GovernanceEngine(tmp_path)
    assert gov.execution_allowed(task) is True


def test_tool_result_accepts_facts_parameter():
    """ToolResult should accept facts= dict, not data= list."""
    result = ToolResult(
        tool="codebase.search",
        ok=True,
        facts={"results": [{"path": "foo.py", "snippet": "bar", "rank": 1}]},
    )
    assert result.ok is True
    assert result.facts["results"][0]["path"] == "foo.py"


def test_tool_result_rejects_unknown_parameters():
    """ToolResult should not accept arbitrary keyword arguments like data=."""
    with pytest.raises(TypeError):
        ToolResult(
            tool="codebase.search",
            ok=True,
            data=[{"path": "foo.py"}],  # type: ignore[call-arg]
        )
