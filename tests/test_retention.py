from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager

import pytest

from navi.db import connect
from navi.loop_control_service import LoopControlService, OpenGoalRequest
from navi.memory import MemoryStore
from navi.paths import db_paths
from navi.retention import DataRetentionManager
import navi.retention as retention_module
from navi.runs import RunStore
from navi.trace import TraceStore


def test_missing_memory_job_is_reconstructed_from_run_transcript(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    memory.add_message(
        "session-a",
        "user",
        "remember this",
        run_id="run-a",
        source="weixin",
        peer_id="peer-a",
        sender_id="sender-a",
    )

    manager = DataRetentionManager(tmp_path)

    assert manager._memory_ready("run-a") is False
    with connect(db_paths(tmp_path).memory) as conn:
        job = conn.execute(
            """
            SELECT session_id, source, peer_id, sender_id, status
            FROM memory_consolidation_jobs WHERE run_id = ?
            """,
            ("run-a",),
        ).fetchone()
    assert tuple(job) == (
        "session-a",
        "weixin",
        "peer-a",
        "sender-a",
        "pending",
    )


@pytest.mark.asyncio
async def test_expired_transient_turn_keeps_summary_and_purges_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        async def complete_for(self, role, messages, *, output_schema=None):
            return '{"learnings":[]}'

    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="private one-off request",
            workspace=str(tmp_path),
            loop_kind="turn",
            source="cli",
            peer_id="cli",
            sender_id="cli",
            session_id="session-a",
        )
    )
    memory = MemoryStore(tmp_path)
    memory.add_message(
        "session-a",
        "user",
        "private one-off request",
        run_id=opened.run.id,
    )
    job_id = memory.enqueue_consolidation(
        session_id="session-a",
        run_id=opened.run.id,
        source="cli",
        peer_id="cli",
        sender_id="cli",
    )
    job = memory.claim_consolidation_jobs(owner="test-worker")[0]
    await memory.consolidate_job(job, SimpleNamespace(provider=Provider()))
    RunStore(tmp_path).add_tool_call_log(
        tool="shell.run",
        args_json='{"command":"private"}',
        ok=True,
        facts_json='{"private":"fact"}',
        error="",
        started_at=1,
        ended_at=2,
        run_id=opened.run.id,
        trace_id="trace-a",
    )
    TraceStore(tmp_path).add_event(
        trace_id="trace-a",
        run_id=opened.run.id,
        phase="tool",
        input_data={"private": "value"},
        output_data={"ok": True},
    )
    with connect(db_paths(tmp_path).loop_runs) as conn:
        conn.execute(
            "UPDATE loop_runs SET terminal_state = 'converged', updated_at = 1 WHERE id = ?",
            (opened.loop_run.run_id,),
        )

    original_connect = retention_module.connect
    failed = False

    @contextmanager
    def fail_trace_once(path):
        nonlocal failed
        if path == db_paths(tmp_path).traces and not failed:
            failed = True
            raise RuntimeError("injected trace compaction failure")
        with original_connect(path) as conn:
            yield conn

    monkeypatch.setattr(retention_module, "connect", fail_trace_once)
    with pytest.raises(RuntimeError, match="injected trace"):
        DataRetentionManager(tmp_path).compact_expired(now=100000)
    assert service.loop_runs.get_spec_json(opened.loop_spec.id)

    monkeypatch.setattr(retention_module, "connect", original_connect)
    facts = DataRetentionManager(tmp_path).compact_expired(now=100000)

    assert facts.compacted == 1
    assert facts.run_ids == (opened.run.id,)
    assert memory.get_messages("session-a") == []
    assert service.goals.get(opened.goal.id).objective == "[expired transient turn]"
    with connect(db_paths(tmp_path).runs) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tool_call_logs WHERE run_id = ?",
            (opened.run.id,),
        ).fetchone()[0] == 0
    with connect(db_paths(tmp_path).traces) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM trace_events WHERE run_id = ?",
            (opened.run.id,),
        ).fetchone()[0] == 0
    with connect(db_paths(tmp_path).memory) as conn:
        assert conn.execute(
            "SELECT status FROM memory_consolidation_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()[0] == "purged"
    with pytest.raises(KeyError):
        service.loop_runs.get_spec_json(opened.loop_spec.id)
