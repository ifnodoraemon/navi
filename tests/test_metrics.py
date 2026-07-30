from __future__ import annotations

from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.db import connect
from navi.delivery_outbox import (
    DeliveryEnvelope,
    DeliveryOutboxStore,
    DeliveryReceipt,
)
from navi.effect_journal import EffectJournal
from navi.evolution import EvolutionLedger
from navi.loop_control_service import LoopControlService, OpenGoalRequest
from navi.lifecycle import Phase, Resolution
from navi.memory import MemoryStore
from navi.metrics import MetricsProjector
from navi.paths import db_paths
from navi.runs import RunStore
from navi.trace import TraceStore


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


def test_metrics_treat_empty_invariant_samples_as_insufficient(tmp_path: Path) -> None:
    snapshot = MetricsProjector(tmp_path).snapshot()
    invariant_slos = {
        item.name: item
        for item in snapshot.slos
        if item.name
        in {
            "lifecycle_projection_integrity",
            "mutating_effect_certainty",
            "execution_lease_health",
            "resource_release_integrity",
            "memory_pipeline_health",
            "evolution_activation_safety",
        }
    }

    assert invariant_slos
    assert all(item.status == "insufficient_data" for item in invariant_slos.values())
    assert all(item.samples == 0 for item in invariant_slos.values())


def test_task_success_rate_reports_cancellation_without_counting_it_as_failure(
    tmp_path: Path,
) -> None:
    runs = RunStore(tmp_path)
    for title, resolution in (
        ("success", Resolution.SUCCESS),
        ("failure", Resolution.FAILED),
        ("canceled", Resolution.CANCELED),
    ):
        run = runs.create(title, workspace=str(tmp_path))
        runs.update_run(run.id, phase=Phase.ENDED, resolution=resolution)

    snapshot = MetricsProjector(tmp_path).snapshot()
    metric = {item.name: item for item in snapshot.metrics}["task_success_rate"]

    assert metric.value == 0.5
    assert metric.samples == 2
    assert snapshot.diagnostics["terminal_runs"] == 3
    assert snapshot.diagnostics["evaluated_runs"] == 2
    assert snapshot.diagnostics["canceled_runs"] == 1


def test_metrics_find_loop_runs_without_goals_and_expired_effects(tmp_path: Path) -> None:
    orphan = RunStore(tmp_path).create(
        "orphan loop run",
        kind="loop:turn",
        workspace=str(tmp_path),
    )
    EffectJournal(tmp_path).reserve(
        effect_key="expired-effect",
        loop_run_id="missing-loop",
        tool="personal.update",
        owner="worker-a",
        lease_seconds=10,
        now=100.0,
    )

    snapshot = MetricsProjector(tmp_path).snapshot(now=111.0)

    assert snapshot.diagnostics["orphan_run_ids"] == [orphan.id]
    assert snapshot.diagnostics["uncertain_effects"] == 1


def test_metrics_breach_on_stale_unowned_active_loop(tmp_path: Path) -> None:
    opened = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="interrupted foreground execution",
            workspace=str(tmp_path),
            auto_start=False,
            execution_mode="foreground",
        )
    )
    with connect(db_paths(tmp_path).loop_runs) as conn:
        conn.execute(
            "UPDATE loop_runs SET updated_at = 1, lease_owner = '', lease_expires_at = 0 "
            "WHERE id = ?",
            (opened.loop_run.run_id,),
        )

    snapshot = MetricsProjector(tmp_path).snapshot(now=1000.0)
    metric = {item.name: item for item in snapshot.metrics}[
        "expired_execution_lease_count"
    ]
    slo = {item.name: item for item in snapshot.slos}["execution_lease_health"]

    assert metric.value == 1.0
    assert snapshot.diagnostics["stale_unowned_loops"] == 1
    assert slo.status == "breached"
    assert slo.evidence == {"expired_leases": 0, "stale_unowned_loops": 1}


def test_metrics_surface_dead_letters_and_uncertain_evolution_apply(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    job_id = memory.enqueue_consolidation(
        session_id="session-a",
        run_id="run-a",
        source="cli",
        peer_id="cli",
        sender_id="cli",
    )
    with connect(db_paths(tmp_path).memory) as conn:
        conn.execute(
            "UPDATE memory_consolidation_jobs SET status = 'dead_letter' WHERE id = ?",
            (job_id,),
        )
    evolution = EvolutionLedger(tmp_path)
    proposal = evolution.propose(
        target_type="prompt_layer",
        target_id="planner",
        reason="test uncertain application",
        expected_benefit="exercise safety projection",
        risk="none",
        before="before",
        after="after",
        rollback_plan="restore before",
        eval_cases=["case-a"],
    )
    evolution.claim_for_apply(proposal.id)
    evolution.mark_apply_uncertain(proposal.id)

    snapshot = MetricsProjector(tmp_path).snapshot()
    slo = {item.name: item for item in snapshot.slos}

    assert slo["memory_pipeline_health"].status == "breached"
    assert slo["evolution_activation_safety"].status == "breached"
    assert slo["evolution_activation_safety"].evidence["uncertain_applies"] == 1


def test_trace_metrics_use_only_the_latest_evaluation_per_trace(tmp_path: Path) -> None:
    traces = TraceStore(tmp_path)
    first = traces.record_evaluation(
        trace_id="trace-one",
        outcome="success",
        failure_domain="none",
    )
    latest = traces.record_evaluation(
        trace_id="trace-one",
        outcome="failure",
        failure_domain="provider",
    )

    snapshot = MetricsProjector(tmp_path).snapshot()

    assert first.id == latest.id
    assert len(traces.list_evaluations("trace-one")) == 1
    assert snapshot.diagnostics["evaluated_traces"] == 1
    assert snapshot.diagnostics["failed_traces"] == 1


def test_metrics_surface_proactive_delivery_success_and_backlog(tmp_path: Path) -> None:
    store = DeliveryOutboxStore(tmp_path)
    for index in range(5):
        item = store.enqueue(
            DeliveryEnvelope(
                batch_id=f"proactive-{index}",
                channel="weixin",
                peer_id="peer",
                text=f"notification {index}",
                body_provenance="background_notification",
            )
        )[0]
        claimed = store.claim_ready(channel="weixin", limit=1)[0]
        assert claimed.id == item.id
        if index < 4:
            store.mark_sent(
                item.id,
                receipt=DeliveryReceipt(transport="test"),
            )
        else:
            store.mark_failed(item.id, error="connector_rejected")

    snapshot = MetricsProjector(tmp_path).snapshot()
    metrics = {item.name: item for item in snapshot.metrics}
    slos = {item.name: item for item in snapshot.slos}

    assert metrics["proactive_delivery_success_rate"].value == 0.8
    assert metrics["proactive_delivery_success_rate"].samples == 5
    assert slos["proactive_delivery_success_rate"].status == "breached"
    assert snapshot.diagnostics["proactive_sent"] == 4


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
