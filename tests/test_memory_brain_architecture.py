from __future__ import annotations

from pathlib import Path
import pytest

from navi.graph import GraphStore
from navi.memory.store import MemoryStore


def test_proposed_memory_is_recalled_with_provisional_tag(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    item = store.add_item(
        "fact",
        "user prefers remote software development roles",
        source="consolidator",
        status="proposed",
        confidence=0.7,
        reason="conversation consolidation",
        provenance="test-run-1",
    )

    recalls = store.recall("software development")

    assert len(recalls) == 1
    assert recalls[0].item.id == item.id
    assert "status=proposed" in recalls[0].reasons


def test_ltp_activation_promotes_proposed_to_active(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    item = store.add_item(
        "preference",
        "filter jobs with hourly rate >= 120 RMB",
        source="consolidator",
        status="proposed",
        confidence=0.7,
        reason="inferred from conversation",
        provenance="test-run-2",
    )

    activated = store.record_activation(
        item.id,
        reason="planner used constraint to filter candidate jobs",
        provenance="test-trace:planner",
    )

    assert activated is not None
    assert activated.status == "active"
    assert activated.confidence == pytest.approx(0.75)
    assert activated.metadata["recall_count"] == 1
    assert activated.metadata["activation_reason"] == "planner used constraint to filter candidate jobs"

    # Subsequent activation retains active status
    activated_again = store.record_activation(
        item.id,
        reason="second activation",
        provenance="test-trace:planner",
    )
    assert activated_again is not None
    assert activated_again.status == "active"
    assert activated_again.confidence == pytest.approx(0.75)
    assert activated_again.metadata["recall_count"] == 2


def test_lexical_recall_short_chinese_cues_without_embeddings(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    item_resume = store.add_item(
        "fact",
        "用户的个人简历存放在家目录，具有10年Python与AI Agent开发经验",
        source="user",
        status="proposed",
        confidence=0.8,
        reason="user stated",
        provenance="session-1",
    )
    item_part_time = store.add_item(
        "preference",
        "正在寻找远程兼职工作，要求时薪不低于120元",
        source="user",
        status="active",
        confidence=0.9,
        reason="user stated",
        provenance="session-1",
    )

    recalls_resume = store.recall("简历")
    assert any(r.item.id == item_resume.id for r in recalls_resume)

    recalls_part_time = store.recall("兼职")
    assert any(r.item.id == item_part_time.id for r in recalls_part_time)


def test_spreading_activation_across_actor_scope_hub(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    graph = GraphStore(tmp_path)

    actor_scope = "actor:weixin:user-test"
    item1 = store.add_item(
        "fact",
        "alpha unique keyword user profile detail",
        source="user",
        scope=actor_scope,
        status="active",
        reason="user profile",
        provenance="test",
    )
    item2 = store.add_item(
        "preference",
        "beta completely different words about notification preferences",
        source="user",
        scope=actor_scope,
        status="active",
        reason="user settings",
        provenance="test",
    )

    store.sync_semantic_graph(graph_store=graph)

    # When querying for item1's unique keyword, FTS/lexical matches item1.
    # Spreading activation through the shared actor scope hub then reaches item2.
    recalls = store.recall("alpha unique keyword", limit=5)
    recalled_ids = [r.item.id for r in recalls]
    assert item1.id in recalled_ids
    assert item2.id in recalled_ids

    by_id = {r.item.id: r for r in recalls}
    item2_reasons = by_id[item2.id].reasons
    assert any("semantic_graph_spreading=MemoryScope:actor:weixin:user-test" in reason for reason in item2_reasons)


def test_consolidation_sweep_enqueues_unconsolidated_episodes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.add_message("session-orphan-1", "user", "I need part-time remote work")
    store.add_message("session-orphan-1", "assistant", "I will search remote roles for you")

    # Before sweep: no consolidation jobs exist
    assert len(store.list_consolidation_jobs()) == 0

    # Sleep/idle episodic consolidation sweep discovers the orphan session
    enqueued = store.enqueue_unconsolidated_episodes()
    assert len(enqueued) == 1

    jobs = store.list_consolidation_jobs()
    assert len(jobs) == 1
    assert jobs[0].session_id == "session-orphan-1"
    assert jobs[0].status == "pending"


def test_consolidation_hebbian_reinforcement_strengthens_memory(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    active_items = []
    learnings = [
        {
            "action": "add",
            "type": "preference",
            "content": "preferred editor is vim",
            "confidence": 0.7,
            "reason": "user preference",
        }
    ]

    # First turn: memory created as proposed
    affected_1 = store._apply_learnings(
        learnings,
        active_items,
        source="test",
        provenance="run-1",
        ledger_run_id="run-1",
        default_add_reason="test",
    )
    assert len(affected_1) == 1
    item_id = affected_1[0].id
    assert affected_1[0].status == "proposed"
    assert affected_1[0].confidence == pytest.approx(0.7)

    # Replay of identical learning is idempotent (no duplicate created)
    affected_2 = store._apply_learnings(
        learnings,
        active_items,
        source="test",
        provenance="run-2",
        ledger_run_id="run-2",
        default_add_reason="test",
    )
    assert affected_2 == []

    # Explicit LTP activation promotes the provisional proposed item to active with confidence boost
    activated = store.record_activation(item_id, reason="used by planner", provenance="trace:planner")
    assert activated is not None
    assert activated.status == "active"
    assert activated.confidence == pytest.approx(0.75)
    assert activated.metadata["recall_count"] == 1


def test_global_episodic_message_search_without_identity_gate(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.add_message(
        "historical-session-xyz",
        "assistant",
        "已找到您电脑上的简历文件：/home/ifnodoraemon/金录保_简历_大模型方向.md",
    )

    # Global episodic search across historical messages without rigid identity filters
    results = store.search_messages("金录保")
    assert len(results) >= 1
    matched_msg, rank, reasons = results[0]
    assert "金录保" in matched_msg.content
    assert matched_msg.session_id == "historical-session-xyz"

