from __future__ import annotations

from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.memory import MemoryStore
from navi.memory.scopes import memory_scopes_for_context
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
