from __future__ import annotations

from navi.fact_tools import render_task_facts, task_facts
from navi.tasks import TaskStore


def test_task_facts_reports_status_approvals_and_logs(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create("check service", status="preparing")
    approval = store.create_approval(task_id=task.id, peer_id="peer", sender_id="sender")
    store.add_execution_log(
        task_id=task.id,
        provider="navi",
        phase="prepare",
        command="navi internal prepare",
        stdout="",
        stderr="timeout",
        exit_code=124,
        started_at=1.0,
        ended_at=2.0,
    )

    rendered = render_task_facts(task_facts(tmp_path, task.id))

    assert f"Task `{task.id}` facts:" in rendered
    assert "- status: preparing" in rendered
    assert f"code={approval.code}" not in rendered
    assert "code_present=True" in rendered
    assert "exit_code=124" in rendered
