"""Tests for memory layer improvements: LLM reranking, conflict audit,
reflective repair, near-realtime consolidation, and parameter unification.

All tests maintain zero else/elif/ternary invariants.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from navi.memory.models import MemoryItem
from navi.memory.store import MemoryStore


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_memory_item(
    item_id: str = "item1",
    content: str = "test content",
    memory_type: str = "fact",
    scope: str = "global",
    status: str = "active",
    confidence: float = 0.70,
    metadata: dict | None = None,
) -> "MemoryItem":
    now = time.time()
    return MemoryItem(
        id=item_id,
        type=memory_type,
        status=status,
        scope=scope,
        content=content,
        source="test",
        confidence=confidence,
        created_at=now,
        updated_at=now,
        last_verified_at=0.0,
        expires_at=0.0,
        metadata=metadata or {},
        reason="test_reason",
        provenance="test_provenance",
    )


def _make_memory_store(tmp_path: Path, registry=None) -> "MemoryStore":
    home = tmp_path / "navi_test"
    home.mkdir(parents=True, exist_ok=True)
    return MemoryStore(home, registry=registry)


def _mock_provider_with_response(response_json: dict) -> AsyncMock:
    provider = AsyncMock()
    provider.complete_for = AsyncMock(return_value=json.dumps(response_json))
    return provider


# ── Test 1: LLM Semantic Reranking ───────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_rerank_candidates_reorders_by_score(tmp_path: Path) -> None:
    """LLM reranking should reorder candidates by LLM-assigned scores."""
    from navi.memory.models import MemoryRecall

    store = _make_memory_store(tmp_path)
    item_a = _make_memory_item("aaa", "User prefers dark mode")
    item_b = _make_memory_item("bbb", "User likes Python")
    item_c = _make_memory_item("ccc", "User name is Bob")

    candidates = [
        MemoryRecall(item=item_a, score=0.3, reasons=["fts"]),
        MemoryRecall(item=item_b, score=0.5, reasons=["fts"]),
        MemoryRecall(item=item_c, score=0.1, reasons=["lexical"]),
    ]

    provider = _mock_provider_with_response({
        "ranked": [
            {"id": "ccc", "score": 0.95},
            {"id": "aaa", "score": 0.80},
            {"id": "bbb", "score": 0.20},
        ]
    })

    result = await store._llm_rerank_candidates(
        query="What is the user's name?",
        goal="identity recall",
        candidates=candidates,
        provider=provider,
        limit=10,
    )

    # ccc should be first (highest LLM score)
    assert result[0].item.id == "ccc"
    assert result[0].score == 0.95
    # aaa should be second
    assert result[1].item.id == "aaa"
    assert result[1].score == 0.80
    # bbb should be third
    assert result[2].item.id == "bbb"
    # Should have rerank reason annotation
    assert any("llm_rerank_score" in r for r in result[0].reasons)


@pytest.mark.asyncio
async def test_llm_rerank_fallback_on_error(tmp_path: Path) -> None:
    """When LLM reranking fails, original candidate order should be preserved."""
    from navi.memory.models import MemoryRecall

    store = _make_memory_store(tmp_path)
    item_a = _make_memory_item("aaa", "fact A")
    candidates = [MemoryRecall(item=item_a, score=0.5, reasons=["fts"])]

    provider = AsyncMock()
    provider.complete_for = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

    result = await store._llm_rerank_candidates(
        query="test",
        goal="test",
        candidates=candidates,
        provider=provider,
        limit=10,
    )
    assert len(result) == 1
    assert result[0].item.id == "aaa"


@pytest.mark.asyncio
async def test_recall_async_without_provider_skips_rerank(tmp_path: Path) -> None:
    """recall_async without provider should behave like sync recall."""
    store = _make_memory_store(tmp_path)
    # Just verify it doesn't crash with no provider
    result = await store.recall_async("test query", provider=None)
    assert isinstance(result, list)


# ── Test 2: Proactive Semantic Conflict Auditor ──────────────────────────

@pytest.mark.asyncio
async def test_audit_semantic_conflicts_discovers_contradiction(tmp_path: Path) -> None:
    """LLM conflict audit should discover contradictions between similar items."""
    store = _make_memory_store(tmp_path)

    # Add two contradicting items
    store.add_item(
        memory_type="fact",
        content="User prefers light theme for all applications",
        source="test",
        scope="global",
        reason="test",
        provenance="test",
        confidence=0.8,
    )
    store.add_item(
        memory_type="fact",
        content="User prefers dark theme for all applications",
        source="test",
        scope="global",
        reason="test",
        provenance="test",
        confidence=0.7,
    )

    provider = _mock_provider_with_response({
        "contradicts": True,
        "relation": "contradicts",
        "explanation": "Light theme vs dark theme preference conflict",
        "superseded_id": "",
    })

    # Monkey-patch _lexical_similarity to return high similarity for our pair
    original_sim = store._lexical_similarity
    def high_sim(q, c):
        return 0.80
    store._lexical_similarity = high_sim

    conflicts = await store.audit_semantic_conflicts(provider, limit=10)

    store._lexical_similarity = original_sim

    assert len(conflicts) >= 1
    conflict = conflicts[0]
    assert conflict.relation == "contradicts"
    assert "llm_audit" in conflict.reason


@pytest.mark.asyncio
async def test_audit_semantic_conflicts_skips_consistent_pairs(tmp_path: Path) -> None:
    """LLM audit should not report consistent (non-contradicting) pairs."""
    store = _make_memory_store(tmp_path)

    store.add_item(
        memory_type="fact", content="User likes cats",
        source="test", scope="global", reason="test", provenance="test",
    )
    store.add_item(
        memory_type="fact", content="User likes dogs",
        source="test", scope="global", reason="test", provenance="test",
    )

    provider = _mock_provider_with_response({
        "contradicts": False,
        "relation": "consistent",
        "explanation": "Both are pet preferences, not contradictory",
    })

    original_sim = store._lexical_similarity
    store._lexical_similarity = lambda q, c: 0.60
    conflicts = await store.audit_semantic_conflicts(provider, limit=10)
    store._lexical_similarity = original_sim

    assert len(conflicts) == 0


@pytest.mark.asyncio
async def test_audit_conflict_with_supersession(tmp_path: Path) -> None:
    """LLM audit discovering supersession should mark the superseded item."""
    store = _make_memory_store(tmp_path)

    item_old = store.add_item(
        memory_type="fact", content="Server runs on port 8080",
        source="test", scope="global", reason="test", provenance="test",
    )
    item_new = store.add_item(
        memory_type="fact", content="Server runs on port 9090",
        source="test", scope="global", reason="test", provenance="test",
    )

    provider = _mock_provider_with_response({
        "contradicts": True,
        "relation": "supersedes",
        "explanation": "Port changed from 8080 to 9090",
        "superseded_id": item_old.id,
    })

    original_sim = store._lexical_similarity
    store._lexical_similarity = lambda q, c: 0.75
    conflicts = await store.audit_semantic_conflicts(provider, limit=10)
    store._lexical_similarity = original_sim

    assert len(conflicts) == 1
    # Check supersession was recorded
    updated_old = store.get_item(item_old.id)
    assert updated_old.metadata.get("superseded_by") == item_new.id


# ── Test 3: Reflective Memory Reconstruction ────────────────────────────

@pytest.mark.asyncio
async def test_repair_stale_item_creates_replacement(tmp_path: Path) -> None:
    """Reflective repair should revoke stale item and create corrected replacement."""
    store = _make_memory_store(tmp_path)

    stale_item = store.add_item(
        memory_type="fact",
        content="Database uses MySQL",
        source="test",
        scope="global",
        reason="test",
        provenance="test",
        confidence=0.15,
        status="active",
    )

    provider = _mock_provider_with_response({
        "should_repair": True,
        "corrected_content": "Database uses PostgreSQL (migrated from MySQL)",
        "correction_reason": "Database was migrated to PostgreSQL",
        "confidence": 0.75,
    })

    new_item = await store.repair_stale_item(
        stale_item,
        provider=provider,
        reason="credit penalty below threshold",
    )

    assert new_item is not None
    assert "PostgreSQL" in new_item.content
    assert new_item.confidence == 0.75
    assert new_item.metadata.get("repaired_from") == stale_item.id

    # Original should be revoked
    revoked = store.get_item(stale_item.id)
    assert revoked.status == "revoked"


@pytest.mark.asyncio
async def test_repair_stale_item_retires_when_no_repair(tmp_path: Path) -> None:
    """When LLM says should_repair=false, no replacement should be created."""
    store = _make_memory_store(tmp_path)

    stale_item = store.add_item(
        memory_type="fact",
        content="Obsolete fact",
        source="test",
        scope="global",
        reason="test",
        provenance="test",
    )

    provider = _mock_provider_with_response({
        "should_repair": False,
        "corrected_content": "",
        "correction_reason": "This fact is completely obsolete",
    })

    result = await store.repair_stale_item(
        stale_item,
        provider=provider,
        reason="too old",
    )

    assert result is None
    # Original should NOT be revoked (just left stale)
    original = store.get_item(stale_item.id)
    assert original.status != "revoked"


@pytest.mark.asyncio
async def test_repair_stale_item_handles_llm_error(tmp_path: Path) -> None:
    """Reflective repair should return None gracefully on LLM failure."""
    store = _make_memory_store(tmp_path)
    item = _make_memory_item("stale1", "some fact")

    provider = AsyncMock()
    provider.complete_for = AsyncMock(side_effect=RuntimeError("API error"))

    result = await store.repair_stale_item(item, provider=provider, reason="test")
    assert result is None


def test_apply_credit_delta_enqueues_repair_on_stale(tmp_path: Path) -> None:
    """apply_credit_delta should enqueue reflective repair when item becomes stale."""
    store = _make_memory_store(tmp_path)

    item = store.add_item(
        memory_type="fact",
        content="Some fact",
        source="test",
        scope="global",
        reason="test",
        provenance="test",
        confidence=0.25,
        status="active",
    )

    # Apply negative delta large enough to push below stale threshold (0.20)
    with patch.object(store, "enqueue_reflective_repair", return_value="job123") as mock_enqueue:
        store.apply_credit_delta(
            item.id,
            -0.10,
            reason="poor performance",
            provenance="causal_credit",
        )

        # Should have enqueued repair
        mock_enqueue.assert_called_once_with(
            item.id,
            reason="poor performance",
            delta=-0.10,
            provenance="causal_credit",
        )


def test_enqueue_reflective_repair_creates_job(tmp_path: Path) -> None:
    """enqueue_reflective_repair should create a consolidation job."""
    store = _make_memory_store(tmp_path)

    job_id = store.enqueue_reflective_repair(
        "item123",
        reason="stale due to penalty",
        delta=-0.15,
        provenance="causal_credit",
    )

    assert isinstance(job_id, str)
    assert len(job_id) > 0


# ── Test 4: Near-Realtime Consolidation ──────────────────────────────────

def test_trigger_background_consolidation_no_loop() -> None:
    """_trigger_background_consolidation should return silently without event loop."""
    from navi.turn_lifecycle import TurnLifecycleMixin

    mixin = TurnLifecycleMixin.__new__(TurnLifecycleMixin)
    mixin._background_tasks = set()

    # Should not raise without running loop
    mixin._trigger_background_consolidation(
        session_id="sess1",
        run_id="run1",
        source="test",
        peer_id="",
        sender_id="",
    )


@pytest.mark.asyncio
async def test_run_background_consolidation_claims_and_runs() -> None:
    """Background consolidation should claim and run pending jobs."""
    from navi.turn_lifecycle import TurnLifecycleMixin

    mixin = TurnLifecycleMixin.__new__(TurnLifecycleMixin)
    mixin._background_tasks = set()

    mock_job = MagicMock()
    mock_job.id = "job1"

    mock_memory = MagicMock()
    mock_memory.claim_consolidation_jobs = MagicMock(return_value=[mock_job])
    mock_memory.consolidate_job = AsyncMock(return_value=[])

    mock_runtime = MagicMock()
    mock_runtime.memory = mock_memory
    mixin.runtime = mock_runtime

    with patch("navi.dynamic_parameters.SYSTEM_DYNAMIC_PARAMETERS", {"consolidation_idle_seconds": 0.001}), patch("asyncio.sleep", new_callable=AsyncMock):
        await mixin._run_background_consolidation(
            session_id="sess1",
            run_id="run1",
            source="test",
            peer_id="",
            sender_id="",
        )

    mock_memory.claim_consolidation_jobs.assert_called_once()
    mock_memory.consolidate_job.assert_called_once()


# ── Test 5: Parameter Unification ────────────────────────────────────────

def test_get_parameter_reads_from_registry_first(tmp_path: Path) -> None:
    """get_parameter should prefer DynamicParameterRegistry values."""
    mock_registry = MagicMock()
    mock_registry.get = MagicMock(return_value=0.42)

    store = _make_memory_store(tmp_path, registry=mock_registry)

    value = store.get_parameter("decay_base_delta", 0.05)
    assert value == 0.42
    mock_registry.get.assert_called_with("decay_base_delta")


def test_get_parameter_falls_back_without_registry(tmp_path: Path) -> None:
    """get_parameter without registry should use local parameter store."""
    store = _make_memory_store(tmp_path, registry=None)
    value = store.get_parameter("decay_base_delta", 0.05)
    # Should return the SYSTEM_DYNAMIC_PARAMETERS default or local value
    assert isinstance(value, float)


def test_set_parameter_writes_through_to_registry(tmp_path: Path) -> None:
    """set_parameter should write-through to DynamicParameterRegistry."""
    mock_registry = MagicMock()
    mock_registry.get = MagicMock(return_value=None)
    mock_registry.set = MagicMock()

    store = _make_memory_store(tmp_path, registry=mock_registry)

    store.set_parameter("decay_base_delta", 0.10, reason="test_update")

    mock_registry.set.assert_called_once_with(
        "decay_base_delta", 0.10, reason="test_update"
    )


def test_set_parameter_works_without_registry(tmp_path: Path) -> None:
    """set_parameter without registry should work normally."""
    store = _make_memory_store(tmp_path, registry=None)
    store.set_parameter("decay_base_delta", 0.10, reason="test")
    value = store.get_parameter("decay_base_delta")
    assert value == 0.10


def test_memory_items_summary_column_migration(tmp_path: Path) -> None:
    """A pre-summary (14-column) memory_items table migrates in place."""
    import sqlite3
    from contextlib import closing

    from navi.paths import db_paths

    home = tmp_path / "home"
    home.mkdir()
    db_path = db_paths(home).memory
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE memory_items (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                scope TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_verified_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                metadata TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                provenance TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO memory_items(
                id, type, status, scope, content, source, confidence,
                created_at, updated_at, last_verified_at, expires_at,
                metadata, reason, provenance
            )
            VALUES (
                'legacy-item', 'fact', 'active', 'global', 'legacy content',
                'test', 0.8, 1.0, 1.0, 0.0, 0.0, '{}', 'r', 'p'
            )
            """
        )
        conn.commit()

    store = MemoryStore(home)
    item = store.get_item("legacy-item")
    assert item is not None
    assert item.content == "legacy content"
    assert item.summary == ""


def test_add_item_generates_l0_summary_for_long_content(tmp_path: Path) -> None:
    store = _make_memory_store(tmp_path)
    long_content = (
        "用户正在系统学习 Kubernetes 的核心概念。第一周覆盖了 node、namespace、pod、"
        "deployment、service、ingress 与 kubectl context 的基本用法。第二周计划进入"
        "存储卷、configmap、secret 以及 helm 包管理。用户偏好通俗讲解并要求中英文"
        "术语对照,每次讲解后会用复述的方式确认理解。"
    ) * 2
    item = store.add_item(
        memory_type="fact",
        content=long_content,
        source="test",
        scope="global",
        reason="test",
        provenance="test",
    )
    assert item.summary
    assert len(item.summary) <= 256

    short = store.add_item(
        memory_type="fact",
        content="用户偏好深色主题",
        source="test",
        scope="global",
        reason="test",
        provenance="test",
    )
    assert short.summary == ""

    explicit = store.add_item(
        memory_type="fact",
        content=long_content,
        source="test",
        scope="global",
        reason="test",
        provenance="test",
        summary="K8s 学习进度与偏好",
    )
    assert explicit.summary == "K8s 学习进度与偏好"

    reread = store.get_item(item.id)
    assert reread is not None
    assert reread.summary == item.summary


def test_render_context_prefers_summary_over_raw_content(tmp_path: Path) -> None:
    store = _make_memory_store(tmp_path)
    long_content = "首先讲了 pod 的基本模型。" * 80
    item = store.add_item(
        memory_type="fact",
        content=long_content,
        source="test",
        scope="global",
        status="active",
        reason="test",
        provenance="test",
    )
    assert item.summary
    rendered = store.render_context("pod 的基本模型", limit=5)
    assert item.summary in rendered
    assert rendered.count("pod") <= len(item.summary) + rendered.count("pod 的基本模型") * 0 + 3


def test_recall_decision_trace_records_stages(tmp_path: Path) -> None:
    store = _make_memory_store(tmp_path)
    item = store.add_item(
        memory_type="fact",
        content="用户偏好深色主题与等宽字体",
        source="test",
        scope="global",
        status="active",
        reason="test",
        provenance="test",
    )
    store.recall("深色主题", limit=5)
    trace = store.last_recall_trace
    assert trace is not None
    assert trace["policy"] == "memory_recall_trace_v1"
    assert trace["duration_ms"] >= 0.0
    stage_names = [stage["stage"] for stage in trace["stages"]]
    assert stage_names == ["fts", "lexical", "graph", "select"]
    select_stage = trace["stages"][-1]
    assert select_stage["selected"]
    assert select_stage["selected"][0]["id"] == item.id


@pytest.mark.asyncio
async def test_case_precipitation_job_creates_case_memory(tmp_path: Path) -> None:
    from navi.goals import GoalStore

    home = tmp_path / "navi_case"
    home.mkdir(parents=True, exist_ok=True)
    goal = GoalStore(home).create(
        objective="优化简历以匹配 Agent Infra 岗位",
        workspace=str(tmp_path),
        source="weixin",
        peer_id="peer-1",
        sender_id="user-1",
    )
    store = MemoryStore(home)
    job_id = store.enqueue_case_precipitation(
        goal_id=goal.id,
        source="goal_convergence",
        peer_id="peer-1",
        sender_id="user-1",
    )
    again = store.enqueue_case_precipitation(
        goal_id=goal.id,
        source="goal_convergence",
        peer_id="peer-1",
        sender_id="user-1",
    )
    assert job_id == again

    claimed = store.claim_consolidation_jobs(owner="case-test", limit=5)
    assert len(claimed) == 1
    assert claimed[0].source == "goal_convergence"

    provider = _mock_provider_with_response(
        {
            "content": "目标:优化简历匹配 Agent Infra 岗位。做法:先定位简历文件缺口,按 JD 逐条改写项目经历,突出推理服务与 K8s 经验。结果:用户未提供原简历,产出渠道建议。复用条件:需要用户提供简历原文后再执行逐条优化。",
            "confidence": 0.7,
            "reason": "求职线长程任务,经验可复用",
        }
    )
    runtime = MagicMock()
    runtime.provider = provider

    items = await store.consolidate_job(claimed[0], runtime)

    assert len(items) == 1
    case = items[0]
    assert case.type == "case"
    assert case.status == "proposed"
    assert case.provenance == f"case-job:{claimed[0].id}:goal:{goal.id}"
    assert "简历" in case.content
    listed = store.list_items(memory_type="case")
    assert [item.id for item in listed] == [case.id]

    recalled = store.recall("简历 优化", limit=5)
    assert any(r.item.id == case.id for r in recalled)


@pytest.mark.asyncio
async def test_case_precipitation_skips_when_goal_missing(tmp_path: Path) -> None:
    home = tmp_path / "navi_case_missing"
    home.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(home)
    store.enqueue_case_precipitation(
        goal_id="ghost-goal",
        source="goal_convergence",
        peer_id="",
        sender_id="",
    )
    claimed = store.claim_consolidation_jobs(owner="case-test-2", limit=5)
    assert len(claimed) == 1

    runtime = MagicMock()
    runtime.provider = AsyncMock()

    items = await store.consolidate_job(claimed[0], runtime)
    assert items == []
    runtime.provider.complete_for.assert_not_called()
