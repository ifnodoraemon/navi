from __future__ import annotations

import json

from navi.recovery import CompletionBlock, RecoveryPlanner


def test_recovery_planner_returns_task_state_facts():
    plan = RecoveryPlanner().plan_completion_failure(
        block=CompletionBlock(
            reason_code="delegation_run_incomplete",
            run_id="run-1",
            run_status="pending",
        ),
        events=[],
    )

    assert plan.details == {
        "blocked_entity_type": "delegation_run",
        "run_id": "run-1",
        "run_status": "pending",
    }
    observation = json.loads(plan.to_observation())
    assert observation["observation_type"] == "loop_checker_fact"
    assert observation["facts"] == {
        "blocked": True,
        "blocked_entity_type": "delegation_run",
        "reason_code": "delegation_run_incomplete",
        "run_id": "run-1",
        "run_status": "pending",
        "trigger": "loop.check",
    }
    assert "choices" not in observation
    assert "recommended" not in observation


def test_recovery_planner_returns_cleanup_state_facts():
    plan = RecoveryPlanner().plan_completion_failure(
        block=CompletionBlock(
            reason_code="bulk_delete_incomplete",
        ),
        events=[
            {
                "tool": "delegate.delete",
                "facts": {
                    "entity_type": "bulk_delete",
                    "completion_evidence": False,
                    "cleanup_complete": False,
                    "remaining_count": 2,
                    "source_filter": "watch",
                    "kind_filter": "",
                },
            }
        ],
    )

    assert plan.details == {
        "blocked_entity_type": "delegation_cleanup",
        "cleanup_complete": False,
        "remaining_count": 2,
        "source_filter": "watch",
    }
    assert "choices" not in plan.to_observation()
    assert "recommended" not in plan.to_observation()


def test_reduce_confidence_lowers_memory_item_confidence(tmp_path):
    """MemoryStore.reduce_confidence lowers confidence by delta."""
    from navi.memory.store import MemoryStore

    store = MemoryStore(tmp_path)
    item = store.add_item(
        memory_type="preference",
        content="prefers dark mode",
        source="test",
        reason="user stated preference",
        provenance="test",
        confidence=0.8,
    )
    store.reduce_confidence(item.id, delta=0.3)
    updated = store.get_item(item.id)
    assert updated.confidence == 0.5


def test_reduce_confidence_clamps_to_zero(tmp_path):
    """MemoryStore.reduce_confidence clamps confidence at 0.0."""
    from navi.memory.store import MemoryStore

    store = MemoryStore(tmp_path)
    item = store.add_item(
        memory_type="preference",
        content="prefers tabs",
        source="test",
        reason="user stated preference",
        provenance="test",
        confidence=0.2,
    )
    store.reduce_confidence(item.id, delta=0.5)
    updated = store.get_item(item.id)
    assert updated.confidence == 0.0
