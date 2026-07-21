from __future__ import annotations

import time

from navi.memory.store import MemoryStore


def _add_memory(
    store: MemoryStore,
    content: str,
    *,
    status: str,
    expires_at: float = 0.0,
    memory_type: str = "working",
    confidence: float = 0.8,
    last_verified_at: float | None = None,
):
    return store.add_item(
        memory_type,
        content,
        source="test",
        status=status,
        confidence=confidence,
        last_verified_at=last_verified_at,
        expires_at=expires_at,
        reason="unit test",
        provenance="tests/test_memory_gc.py",
    )


def test_memory_gc_expires_active_and_archives_nonactive_items(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    now = time.time()
    active = _add_memory(store, "active expired", status="active", expires_at=now - 1)
    proposed = _add_memory(store, "proposed expired", status="proposed", expires_at=now - 1)
    retained = _add_memory(store, "active retained", status="active", expires_at=now + 60)

    facts = store.garbage_collect(now=now)

    active_after = store.get_item(active.id)
    proposed_after = store.get_item(proposed.id)
    retained_after = store.get_item(retained.id)
    assert facts["gc"] == "working_memory"
    assert facts["expired_count"] == 2
    assert facts["decayed_count"] == 0
    assert active_after is not None
    assert active_after.status == "stale"
    assert proposed_after is not None
    assert proposed_after.status == "archived"
    assert retained_after is not None
    assert retained_after.status == "active"
    assert facts["active_count"] == 1


def test_memory_supersede_archives_old_item_with_replacement_metadata(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    old = _add_memory(store, "old constraint", status="active")
    replacement = _add_memory(store, "new constraint", status="active")

    updated = store.supersede_item(
        old.id,
        replacement_item_id=replacement.id,
        reason="newer goal constraint",
    )

    assert updated is not None
    assert updated.status == "archived"
    assert updated.metadata["superseded_by"] == [replacement.id]
    assert updated.metadata["supersede_reason"] == "newer goal constraint"
    replacement_after = store.get_item(replacement.id)
    assert replacement_after is not None
    assert replacement_after.status == "active"


def test_memory_supersede_requires_existing_items_and_reason(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    item = _add_memory(store, "old", status="active")

    assert store.supersede_item(item.id, replacement_item_id="missing", reason="no replacement") is None


def test_memory_gc_decays_old_learnable_memory_without_touching_constraints(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    now = time.time()
    old_anchor = now - 120 * 24 * 60 * 60
    preference = _add_memory(
        store,
        "old preference",
        status="active",
        memory_type="preference",
        confidence=0.8,
        last_verified_at=old_anchor,
    )
    constraint = _add_memory(
        store,
        "old constraint",
        status="active",
        memory_type="constraint",
        confidence=0.8,
        last_verified_at=old_anchor,
    )

    facts = store.garbage_collect(now=now)

    preference_after = store.get_item(preference.id)
    constraint_after = store.get_item(constraint.id)
    assert facts["decayed_count"] == 1
    assert facts["decayed_items"][0]["id"] == preference.id
    assert preference_after is not None
    assert round(preference_after.confidence, 2) == 0.75
    assert preference_after.status == "active"
    assert constraint_after is not None
    assert constraint_after.confidence == 0.8
    assert constraint_after.status == "active"


def test_memory_gc_marks_low_confidence_decayed_memory_stale(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    now = time.time()
    old_anchor = now - 120 * 24 * 60 * 60
    item = _add_memory(
        store,
        "low confidence fact",
        status="active",
        memory_type="fact",
        confidence=0.22,
        last_verified_at=old_anchor,
    )

    facts = store.garbage_collect(now=now)

    updated = store.get_item(item.id)
    assert facts["decayed_count"] == 1
    assert updated is not None
    assert round(updated.confidence, 2) == 0.17
    assert updated.status == "stale"


def test_llm_learning_adds_proposed_memory_even_with_high_confidence(tmp_path) -> None:
    store = MemoryStore(tmp_path)

    affected = store._apply_learnings(
        [
            {
                "action": "add",
                "type": "preference",
                "content": "prefer concise architecture reports",
                "confidence": 0.99,
                "reason": "observed from conversation",
            }
        ],
        [],
        source="llm_learning",
        provenance="unit-test",
        ledger_run_id="run-1",
        add_reason_fallback="memory_learning_added",
    )

    assert len(affected) == 1
    item = store.get_item(affected[0].id)
    assert item is not None
    assert item.scope == "global"
    assert item.status == "proposed"


def test_replayed_learning_does_not_duplicate_an_existing_proposal(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    existing = store.add_item(
        "preference",
        "prefer concise architecture reports",
        source="conversation",
        status="proposed",
        scope="actor:one",
        confidence=0.9,
        reason="first extraction",
        provenance="job:first",
    )

    affected = store._apply_learnings(
        [
            {
                "action": "add",
                "type": "preference",
                "content": "prefer concise architecture reports",
                "confidence": 0.9,
                "reason": "retry extraction",
            }
        ],
        [],
        source="conversation",
        provenance="job:retry",
        ledger_run_id="run-retry",
        add_reason_fallback="retry",
        scope="actor:one",
    )

    assert affected == []
    assert store.get_item(existing.id) is not None


def test_memory_activation_prevents_recently_used_item_decay(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    now = time.time()
    old_anchor = now - 120 * 24 * 60 * 60
    item = _add_memory(
        store,
        "recently used preference",
        status="active",
        memory_type="preference",
        confidence=0.8,
        last_verified_at=old_anchor,
    )

    activated = store.record_activation(
        item.id,
        now=now - 1,
        reason="used in planner context",
        provenance="unit-test",
    )
    facts = store.garbage_collect(now=now)

    updated = store.get_item(item.id)
    assert activated is not None
    assert activated.metadata["recall_count"] == 1
    assert activated.metadata["last_recalled_at"] == now - 1
    assert facts["decayed_count"] == 0
    assert updated is not None
    assert updated.confidence == 0.8
    assert updated.status == "active"
