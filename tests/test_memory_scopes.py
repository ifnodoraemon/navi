from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.db import connect
from navi.memory import MemoryStore
from navi.memory import provider as memory_provider_module
from navi.memory.scopes import memory_scopes_for_context
from navi.paths import db_paths
from navi.tools import API_CONTEXT


def _context(home: Path, *, sender_id: str, session_id: str) -> CapabilityContext:
    return CapabilityContext(
        home=home,
        source="weixin",
        peer_id="peer-1",
        sender_id=sender_id,
        session_id=session_id,
        workspace=str(home),
        permission_ceiling="write",
    )


def test_memory_recall_uses_embedding_candidates_when_fts_has_no_seed(tmp_path: Path) -> None:
    class Embeddings:
        def embed(self, text: str) -> list[float]:
            return [1.0, 0.0] if text in {"short answers", "keep it brief"} else [0.0, 1.0]

    store = MemoryStore(tmp_path, embedding_service=Embeddings())
    item = store.add_item(
        "preference",
        "keep it brief",
        source="user",
        status="active",
        reason="explicit preference",
        provenance="test",
    )

    recalled = store.recall("short answers")

    assert [entry.item.id for entry in recalled] == [item.id]
    assert any("hybrid_similarity" in reason for reason in recalled[0].reasons)


def test_memory_recall_propagates_fts_failure(tmp_path: Path, monkeypatch) -> None:
    store = MemoryStore(tmp_path)

    def fail_connect(path):
        del path
        raise RuntimeError("memory database unavailable")

    monkeypatch.setattr(memory_provider_module, "connect", fail_connect)

    with pytest.raises(RuntimeError, match="memory database unavailable"):
        store.recall("known preference")


def test_memory_recall_propagates_embedding_failure(tmp_path: Path) -> None:
    class BrokenEmbeddings:
        def embed(self, text: str) -> list[float]:
            del text
            raise RuntimeError("embedding unavailable")

    store = MemoryStore(tmp_path, embedding_service=BrokenEmbeddings())

    with pytest.raises(RuntimeError, match="embedding unavailable"):
        store.recall("known preference")


def test_working_memory_propagates_goal_store_failure(tmp_path: Path) -> None:
    class BrokenGoalStore:
        def list(self, *, limit: int):
            del limit
            raise RuntimeError("goal state unavailable")

    with pytest.raises(RuntimeError, match="goal state unavailable"):
        MemoryStore(tmp_path).render_working_memory(goal_store=BrokenGoalStore())


@pytest.mark.asyncio
async def test_conversation_memory_consolidation_is_durable_and_actor_scoped(
    tmp_path: Path,
) -> None:
    class Provider:
        async def complete_for(self, role, messages, *, output_schema=None):
            assert role == "consolidator"
            assert output_schema is not None
            assert "unrelated second run" not in messages[-1].content
            return (
                '{"learnings":[{"action":"add","type":"preference",'
                '"content":"Use concise status updates","confidence":0.9,'
                '"reason":"explicit user preference"}]}'
            )

    store = MemoryStore(tmp_path)
    store.add_message(
        "session-a",
        "user",
        "Please use concise status updates from now on.",
        source="weixin",
        peer_id="peer-1",
        sender_id="user-a",
        run_id="run-a",
    )
    store.add_message(
        "session-a",
        "user",
        "unrelated second run",
        source="weixin",
        peer_id="peer-1",
        sender_id="user-a",
        run_id="run-b",
    )
    job_id = store.enqueue_consolidation(
        session_id="session-a",
        run_id="run-a",
        source="weixin",
        peer_id="peer-1",
        sender_id="user-a",
    )

    claimed = store.claim_consolidation_jobs(owner="worker-a")
    assert [job.id for job in claimed] == [job_id]
    affected = await store.consolidate_job(
        claimed[0],
        SimpleNamespace(provider=Provider()),
    )

    assert len(affected) == 1
    assert affected[0].status == "proposed"
    assert affected[0].scope.startswith("actor:")
    assert affected[0].provenance == f"memory-job:{job_id}:run:run-a"
    assert store.claim_consolidation_jobs(owner="worker-b") == []


def test_expired_consolidation_lease_is_reclaimed_and_failures_dead_letter(
    tmp_path: Path,
) -> None:
    store = MemoryStore(tmp_path)
    job_id = store.enqueue_consolidation(
        session_id="session-a",
        run_id="run-a",
        source="cli",
        peer_id="cli",
        sender_id="cli",
    )
    claimed = store.claim_consolidation_jobs(
        owner="worker-a",
        lease_seconds=10,
        now=100.0,
    )

    assert [job.id for job in claimed] == [job_id]
    assert store.claim_consolidation_jobs(owner="worker-b", now=105.0) == []
    reclaimed = store.claim_consolidation_jobs(owner="worker-b", now=111.0)
    assert [job.id for job in reclaimed] == [job_id]
    assert reclaimed[0].attempts == 2

    with connect(db_paths(tmp_path).memory) as conn:
        conn.execute(
            """
            UPDATE memory_consolidation_jobs
            SET status = 'failed', owner = '', attempts = 5, updated_at = 0
            WHERE id = ?
            """,
            (job_id,),
        )
    assert store.claim_consolidation_jobs(owner="worker-c", now=1000.0) == []
    with connect(db_paths(tmp_path).memory) as conn:
        status = conn.execute(
            "SELECT status FROM memory_consolidation_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()[0]
    assert status == "dead_letter"


@pytest.mark.asyncio
async def test_memory_add_recall_and_revoke_stay_inside_actor_scope(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    actor_a = _context(tmp_path, sender_id="user-a", session_id="session-a")
    actor_b = _context(tmp_path, sender_id="user-b", session_id="session-b")

    added = await registry.invoke(
        "memory.add",
        {
            "operation": "add",
            "type": "preference",
            "content": "zqxj actor-a prefers concise replies",
            "status": "active",
            "reason": "user stated preference",
            "provenance": "trace-a",
        },
        permission="write",
        context=actor_a,
    )
    assert added.ok is True
    item_id = added.facts["memory_id"]
    assert added.facts["item"]["scope"].startswith("actor:")

    visible = await registry.invoke(
        "memory.recall",
        {"query": "zqxj actor-a prefers"},
        permission="read",
        context=actor_a,
    )
    hidden = await registry.invoke(
        "memory.recall",
        {"query": "zqxj actor-a prefers"},
        permission="read",
        context=actor_b,
    )
    assert item_id in visible.facts["activation_candidate_ids"]
    assert item_id not in hidden.facts["activation_candidate_ids"]

    rejected_revoke = await registry.invoke(
        "memory.add",
        {
            "operation": "revoke",
            "memory_id": item_id,
            "reason": "not this actor's memory",
            "provenance": "trace-b",
        },
        permission="write",
        context=actor_b,
    )
    assert rejected_revoke.ok is False
    assert rejected_revoke.error_reason == "permission_denied"
    assert MemoryStore(tmp_path).get_item(item_id).status == "active"

    revoked = await registry.invoke(
        "memory.add",
        {
            "operation": "revoke",
            "memory_id": item_id,
            "reason": "user withdrew preference",
            "provenance": "trace-a-2",
        },
        permission="write",
        context=actor_a,
    )
    assert revoked.ok is True
    assert revoked.facts["item"]["status"] == "revoked"


@pytest.mark.asyncio
async def test_explicit_identity_link_migrates_memory_across_surfaces(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    registry.sensitive_approval_mode = "skip"
    weixin = _context(tmp_path, sender_id="user-a", session_id="wx-session")
    cli = CapabilityContext(
        home=tmp_path,
        source="cli",
        peer_id="terminal-a",
        sender_id="local-a",
        session_id="cli-session",
        workspace=str(tmp_path),
        permission_ceiling="write",
    )
    added = await registry.invoke(
        "memory.add",
        {
            "type": "preference",
            "content": "zqxj cross-surface preference",
            "status": "active",
            "reason": "explicit preference",
            "provenance": "trace-before-link",
        },
        permission="write",
        context=weixin,
    )

    requested = await registry.invoke(
        "identity.link",
        {
            "operation": "request",
            "other_source": cli.source,
            "other_peer_id": cli.peer_id,
            "other_sender_id": cli.sender_id,
            "reason": "user requested cross-surface continuity",
        },
        permission="write",
        context=weixin,
    )
    linked = await registry.invoke(
        "identity.link",
        {
            "operation": "confirm",
            "verification_code": requested.facts["verification_code"],
            "reason": "target channel confirmed possession",
        },
        permission="write",
        context=cli,
    )
    visible = await registry.invoke(
        "memory.recall",
        {"query": "zqxj cross-surface"},
        permission="read",
        context=cli,
    )
    state = await registry.invoke(
        "identity.state",
        {},
        permission="read",
        context=cli,
    )

    assert added.ok is True
    assert requested.ok is True
    assert linked.ok is True
    assert linked.facts["migrated_memory_count"] == 1
    assert added.facts["memory_id"] in visible.facts["activation_candidate_ids"]
    assert state.facts["linked"] is True
    assert len(state.facts["aliases"]) == 2
    assert all("peer_id" not in alias for alias in state.facts["aliases"])


@pytest.mark.asyncio
async def test_identity_link_cannot_be_confirmed_from_an_unrelated_channel(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    registry.sensitive_approval_mode = "skip"
    initiator = _context(tmp_path, sender_id="user-a", session_id="session-a")
    target = CapabilityContext(
        home=tmp_path,
        source="cli",
        peer_id="terminal-a",
        sender_id="local-a",
        session_id="target-session",
        workspace=str(tmp_path),
        permission_ceiling="write",
    )
    attacker = CapabilityContext(
        home=tmp_path,
        source="cli",
        peer_id="terminal-b",
        sender_id="local-b",
        session_id="attacker-session",
        workspace=str(tmp_path),
        permission_ceiling="write",
    )
    added = await registry.invoke(
        "memory.add",
        {
            "operation": "add",
            "type": "preference",
            "content": "zqxj private preference",
            "status": "active",
            "reason": "private",
            "provenance": "identity-attack-test",
        },
        permission="write",
        context=initiator,
    )
    requested = await registry.invoke(
        "identity.link",
        {
            "operation": "request",
            "other_source": target.source,
            "other_peer_id": target.peer_id,
            "other_sender_id": target.sender_id,
            "reason": "link target",
        },
        permission="write",
        context=initiator,
    )

    rejected = await registry.invoke(
        "identity.link",
        {
            "operation": "confirm",
            "verification_code": requested.facts["verification_code"],
            "reason": "attacker tries code",
        },
        permission="write",
        context=attacker,
    )
    attacker_view = await registry.invoke(
        "memory.recall",
        {"query": "zqxj private"},
        permission="read",
        context=attacker,
    )

    assert added.ok is True
    assert requested.ok is True
    assert rejected.ok is False
    assert rejected.error_reason == "schema_mismatch"
    assert added.facts["memory_id"] not in attacker_view.facts["activation_candidate_ids"]


def test_consolidation_cannot_revoke_memory_outside_visible_scope(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    private = store.add_item(
        "preference",
        "private-a",
        source="user",
        status="active",
        reason="test",
        provenance="scope-a",
        scope="actor:a",
    )
    visible = store.add_item(
        "preference",
        "visible-b",
        source="user",
        status="active",
        reason="test",
        provenance="scope-b",
        scope="actor:b",
    )

    affected = store._apply_learnings(
        [{"action": "revoke", "id": private.id, "reason": "malicious output"}],
        [visible],
        source="consolidation",
        provenance="scope-test",
        ledger_run_id="scope-test",
        default_add_reason="test",
        scope="actor:b",
    )

    assert affected == []
    assert store.get_item(private.id).status == "active"


@pytest.mark.asyncio
async def test_memory_scope_kinds_resolve_and_global_write_is_local_only(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    actor = _context(tmp_path, sender_id="user-a", session_id="session-a")

    session_item = await registry.invoke(
        "memory.add",
        {
            "type": "fact",
            "content": "zqxj session-only fact",
            "scope": "session",
            "status": "active",
            "reason": "turn-local continuity",
            "provenance": "trace-session",
        },
        permission="write",
        context=actor,
    )
    expected_session_scope = next(
        scope
        for scope in memory_scopes_for_context(
            source=actor.source,
            peer_id=actor.peer_id,
            sender_id=actor.sender_id,
            session_id=actor.session_id or "",
            workspace=actor.workspace,
        )
        if scope.startswith("session:")
    )
    assert session_item.ok is True
    assert session_item.facts["item"]["scope"] == expected_session_scope

    cross_session = _context(tmp_path, sender_id="user-a", session_id="session-b")
    hidden = await registry.invoke(
        "memory.recall",
        {"query": "zqxj session-only"},
        permission="read",
        context=cross_session,
    )
    assert session_item.facts["memory_id"] not in hidden.facts["activation_candidate_ids"]

    untrusted_global = await registry.invoke(
        "memory.add",
        {
            "type": "constraint",
            "content": "zqxj poison all actors",
            "scope": "global",
            "reason": "attempted global write",
            "provenance": "trace-untrusted",
        },
        permission="write",
        context=actor,
    )
    assert untrusted_global.ok is False
    assert untrusted_global.error_reason == "permission_denied"

    local = CapabilityContext(
        home=tmp_path,
        source="local",
        peer_id="local",
        sender_id="local",
        workspace=str(tmp_path),
        permission_ceiling="write",
    )
    local_registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        execution_context=API_CONTEXT,
    )
    trusted_global = await local_registry.invoke(
        "memory.add",
        {
            "type": "constraint",
            "content": "zqxj trusted global constraint",
            "scope": "global",
            "status": "active",
            "reason": "local administrator policy",
            "provenance": "local-control",
        },
        permission="write",
        context=local,
    )
    assert trusted_global.ok is True
    assert trusted_global.facts["item"]["scope"] == "global"


@pytest.mark.asyncio
async def test_memory_activation_cannot_cross_scope_through_tool_gateway(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    actor_a = _context(tmp_path, sender_id="user-a", session_id="session-a")
    actor_b = _context(tmp_path, sender_id="user-b", session_id="session-b")
    actor_a_scope = next(
        scope
        for scope in memory_scopes_for_context(
            source=actor_a.source,
            peer_id=actor_a.peer_id,
            sender_id=actor_a.sender_id,
            session_id=actor_a.session_id or "",
            workspace=actor_a.workspace,
        )
        if scope.startswith("actor:")
    )
    item = MemoryStore(tmp_path).add_item(
        "fact",
        "actor-a activation fact",
        source="test",
        scope=actor_a_scope,
        status="active",
        reason="scope test",
        provenance="tests/test_memory_scopes.py",
    )

    # Skip the approval layer here: this test targets ToolCapability's injected
    # scope envelope and the core handler's item-level enforcement.
    registry.sensitive_approval_mode = "skip"
    blocked = await registry.invoke(
        "memory.record_activation",
        {
            "item_ids": [item.id],
            "reason": "attempted cross-actor activation",
            "provenance": "trace-b",
        },
        permission="write",
        context=actor_b,
    )
    assert blocked.ok is True
    assert blocked.facts["activated_count"] == 0
    assert blocked.facts["missing_item_ids"] == [item.id]
    assert MemoryStore(tmp_path).get_item(item.id).metadata.get("recall_count") is None
