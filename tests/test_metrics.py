from __future__ import annotations

from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.db import connect
from navi.effect_journal import EffectJournal
from navi.loop_control_service import LoopControlService, OpenGoalRequest
from navi.memory import MemoryStore
from navi.metrics import MetricsProjector
from navi.paths import db_paths


def test_metrics_projector_surfaces_durable_invariant_breaches(tmp_path: Path) -> None:
    opened = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(objective="integrity test", workspace=str(tmp_path))
    )
    with connect(db_paths(tmp_path).runs) as conn:
        conn.execute("DELETE FROM runs WHERE id = ?", (opened.run.id,))
    effect = EffectJournal(tmp_path)
    effect.reserve(
        effect_key="uncertain-effect",
        loop_run_id=opened.loop_run.run_id,
        tool="personal.update",
        owner="worker-a",
    )
    effect.fail("uncertain-effect", owner="worker-a", error="outcome unknown")
    memory = MemoryStore(tmp_path)
    job_id = memory.enqueue_consolidation(
        session_id="session-a",
        run_id="run-missing",
        source="cli",
        peer_id="cli",
        sender_id="cli",
    )
    with connect(db_paths(tmp_path).memory) as conn:
        conn.execute(
            "UPDATE memory_consolidation_jobs SET status = 'failed', attempts = 3 WHERE id = ?",
            (job_id,),
        )

    snapshot = MetricsProjector(tmp_path).snapshot()
    slo = {item.name: item for item in snapshot.slos}

    assert snapshot.overall_status == "breached"
    assert slo["lifecycle_projection_integrity"].status == "breached"
    assert slo["mutating_effect_certainty"].status == "breached"
    assert slo["memory_pipeline_health"].status == "breached"
    assert snapshot.diagnostics["orphan_goal_ids"] == [opened.goal.id]


@pytest.mark.asyncio
async def test_system_metrics_capability_returns_content_free_snapshot(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    result = await registry.invoke(
        "system.metrics",
        {},
        permission="read",
        context=CapabilityContext(
            home=tmp_path,
            source="cli",
            peer_id="cli",
            sender_id="cli",
            workspace=str(tmp_path),
        ),
    )

    assert result.ok is True
    assert result.facts["entity_type"] == "system_metrics"
    assert result.facts["overall_status"] == "insufficient_data"
    assert all("content" not in metric for metric in result.facts["metrics"])
