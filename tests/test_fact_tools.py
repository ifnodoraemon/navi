from __future__ import annotations

from navi.fact_tools import render_run_facts, run_facts
from navi.runs import RunStore


def test_run_facts_reports_status_approvals_and_logs(tmp_path):
    store = RunStore(tmp_path)
    run = store.create("check service", status="preparing")
    approval = store.create_approval(run_id=run.id, peer_id="peer", sender_id="sender")
    store.add_execution_log(
        run_id=run.id,
        provider="navi",
        phase="prepare",
        command="navi internal prepare",
        stdout="",
        stderr="timeout",
        exit_code=124,
        started_at=1.0,
        ended_at=2.0,
    )

    rendered = render_run_facts(run_facts(tmp_path, run.id))

    assert f"Run `{run.id}` facts:" in rendered
    assert "- status: preparing" in rendered
    assert f"code={approval.code}" not in rendered
    assert "code_present=True" in rendered
    assert "exit_code=124" in rendered
