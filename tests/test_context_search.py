from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.memory import MemoryStore
from navi.memory.provider import SQLiteMemoryProvider
from navi.paths import db_paths
from navi.syscalls import ModelSyscallPlanner


def _context(home: Path, *, sender_id: str, session_id: str) -> CapabilityContext:
    return CapabilityContext(
        home=home,
        source="weixin",
        peer_id="room-1",
        sender_id=sender_id,
        session_id=session_id,
        workspace=str(home),
        permission_ceiling="write",
        trace_id=f"trace-{sender_id}",
    )


def test_legacy_message_schema_migrates_to_identity_fields(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = db_paths(home).memory
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO messages(session_id, role, content, created_at)
            VALUES ('legacy-session', 'user', 'legacy alpha budget note', 123.0)
            """
        )
        conn.commit()

    store = MemoryStore(home)
    messages = store.get_messages("legacy-session")
    matches = store.search_messages(
        "legacy alpha budget",
        session_id="legacy-session",
    )

    assert messages[0].message_id == "legacy:1"
    assert messages[0].source == ""
    assert matches
    assert matches[0][0].message_id == "legacy:1"


def test_memory_provider_rejects_new_messages_without_identity(tmp_path: Path) -> None:
    provider = SQLiteMemoryProvider(tmp_path / "memory.db")

    with pytest.raises(ValueError, match="message_id is required"):
        provider.add_message(
            "session-a",
            "user",
            "identity is required",
            123.0,
            message_id="",
        )


@pytest.mark.asyncio
async def test_context_search_uses_actor_identity_without_cross_sender_leak(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    store = MemoryStore(tmp_path)
    store.add_message(
        "session-a",
        "user",
        "alpha budget belongs to sender a",
        source="weixin",
        peer_id="room-1",
        sender_id="sender-a",
        trace_id="trace-a",
        run_id="run-a",
    )
    store.add_message(
        "session-b",
        "user",
        "beta roadmap belongs to sender b",
        source="weixin",
        peer_id="room-1",
        sender_id="sender-b",
        trace_id="trace-b",
        run_id="run-b",
    )

    actor_a = _context(tmp_path, sender_id="sender-a", session_id="session-a")
    alpha = await registry.invoke(
        "context.search",
        {"query": "alpha budget", "max_items": 10},
        permission="read",
        context=actor_a,
    )
    beta = await registry.invoke(
        "context.search",
        {"query": "beta roadmap", "max_items": 10},
        permission="read",
        context=actor_a,
    )
    term_only = await registry.invoke(
        "context.search",
        {"terms": ["alpha budget"], "max_items": 10},
        permission="read",
        context=actor_a,
    )

    assert alpha.ok is True
    assert any("alpha budget" in item["content"] for item in alpha.facts["evidence"])
    assert all("beta roadmap" not in item["content"] for item in beta.facts["evidence"])
    assert any("alpha budget" in item["content"] for item in term_only.facts["evidence"])
    assert beta.facts["identity"]["sender_id"] == "sender-a"
    assert beta.facts["model_decides_usage"] is True


def test_planner_syscall_accepts_used_evidence_ids() -> None:
    parsed = ModelSyscallPlanner._parse_syscalls(
        json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "respond",
                        "permission": "read",
                        "args": {"message": "ok"},
                        "used_evidence_ids": ["msg:abc", "mem:def"],
                    }
                ]
            }
        )
    )

    assert parsed[0].tool == "respond"
    assert parsed[0].used_evidence_ids == ("msg:abc", "mem:def")
