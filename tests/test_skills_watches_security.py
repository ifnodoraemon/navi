from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
import pytest

from navi.skills import SkillStore
from navi.tasks import TaskStore, Watch
from navi.daemon import SystemDaemon
from navi.capabilities import CapabilityContext, CapabilityResult


def test_skills_workspace_scoping_and_security_banner(tmp_path):
    # Set up global user skills, built-in skills, and workspace skills
    skills_dir = tmp_path / "global_skills"
    builtin_dir = tmp_path / "builtin_skills"
    workspace_dir = tmp_path / "my_project"
    workspace_skills_dir = workspace_dir / ".navi" / "skills"

    skills_dir.mkdir(parents=True)
    builtin_dir.mkdir(parents=True)
    workspace_skills_dir.mkdir(parents=True)

    # 1. Global user skill
    (skills_dir / "global_skill").mkdir()
    (skills_dir / "global_skill" / "SKILL.md").write_text(
        "---\nname: global_skill\nrole: developer\n---\nGlobal skill content",
        encoding="utf-8"
    )

    # 2. Workspace skill (unverified)
    (workspace_skills_dir / "work_skill").mkdir()
    (workspace_skills_dir / "work_skill" / "SKILL.md").write_text(
        "---\nname: work_skill\nrole: developer\n---\nWorkspace skill content",
        encoding="utf-8"
    )

    # 3. Built-in skill
    (builtin_dir / "builtin_skill").mkdir()
    (builtin_dir / "builtin_skill" / "SKILL.md").write_text(
        "---\nname: builtin_skill\nrole: operator\n---\nBuilt-in skill content",
        encoding="utf-8"
    )

    store = SkillStore(home=tmp_path)
    store.skills_dir = skills_dir
    store.builtin_skills_dir = builtin_dir

    # Check listing skills without role filtering
    skills = store.list_skills(workspace=workspace_dir)
    skill_names = {s.name for s in skills}
    assert "global_skill" in skill_names
    assert "work_skill" in skill_names
    assert "builtin_skill" in skill_names

    # Assert that work_skill is marked as verified=False and others are verified=True
    for skill in skills:
        if skill.name == "work_skill":
            assert skill.verified is False
            assert skill.source == "workspace"
        else:
            assert skill.verified is True

    # Check role filtering: "developer"
    skills_dev = store.list_skills(workspace=workspace_dir, role="developer")
    skill_names_dev = {s.name for s in skills_dev}
    assert "global_skill" in skill_names_dev
    assert "work_skill" in skill_names_dev
    assert "builtin_skill" not in skill_names_dev

    # Check prompt rendering containing security warnings
    prompt = store.render_prompt(workspace=workspace_dir)
    assert "Global skill content" in prompt
    assert "Built-in skill content" in prompt
    assert "Workspace skill content" in prompt
    assert "[SECURITY WARNING: UNVERIFIED SKILL]" in prompt
    assert "loaded from an untrusted project workspace: work_skill" in prompt


def test_watches_workspace_persistence_and_migration(tmp_path):
    db_path = tmp_path / "tasks.db"

    # Step 1: Create an old-version SQLite database WITHOUT 'workspace' column
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watches (
            id TEXT PRIMARY KEY,
            cron TEXT NOT NULL,
            prompt TEXT NOT NULL,
            peer_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            next_run_at REAL NOT NULL,
            last_run_at REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    # Insert an old watch
    conn.execute(
        """
        INSERT INTO watches(id, cron, prompt, peer_id, sender_id, enabled, next_run_at, last_run_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("old-id", "*/5 * * * *", "Old prompt", "peer-1", "sender-1", 1, 1000.0, 0.0, 500.0, 500.0)
    )
    conn.commit()
    conn.close()

    # Step 2: Initialize TaskStore which should automatically trigger database schema migration (adding 'workspace')
    store = TaskStore(tmp_path)

    # Verify migration did not destroy old watch data and set the default correctly
    old_watch = store.get_watch("old-id")
    assert old_watch is not None
    assert old_watch.prompt == "Old prompt"
    assert old_watch.workspace == ""

    # Step 3: Create a new watch with a specified workspace context and assert persistence
    new_watch = store.create_watch(
        cron="*/10 * * * *",
        prompt="New prompt",
        peer_id="peer-2",
        sender_id="sender-2",
        next_run_at=2000.0,
        workspace="/path/to/my_workspace"
    )

    assert new_watch.workspace == "/path/to/my_workspace"

    # Verify fetching new watch
    fetched_watch = store.get_watch(new_watch.id)
    assert fetched_watch is not None
    assert fetched_watch.workspace == "/path/to/my_workspace"

    # Verify listing and due lists
    all_watches = store.list_watches()
    assert len(all_watches) == 2
    assert any(w.workspace == "/path/to/my_workspace" for w in all_watches)

    due = store.due_watches(3000.0)
    assert len(due) == 2
    assert any(w.workspace == "/path/to/my_workspace" for w in due)


@pytest.mark.asyncio
async def test_watches_context_propagation_in_daemon(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    # Register a watch with a specific workspace
    watch = store.create_watch(
        cron="*/5 * * * *",
        prompt="Execute custom watch task",
        peer_id="user-peer",
        sender_id="user-sender",
        next_run_at=time.time() - 10,
        workspace="/home/user/project_workspace"
    )

    daemon = SystemDaemon(tmp_path)
    
    # Mock self.capabilities.invoke to capture the context used during execution
    invoked_contexts = []
    async def mock_invoke(tool_name, args, *, permission, context):
        invoked_contexts.append(context)
        return CapabilityResult(
            ok=True,
            action="task",
            observation="Created task successfully",
            task_id="mock-task-id"
        )
    
    monkeypatch.setattr(daemon.capabilities, "invoke", mock_invoke)

    # Process due watches
    created = await daemon.process_watches_once()

    assert len(created) == 1
    assert len(invoked_contexts) == 1
    # Verify context integrity: watch's workspace must propagate perfectly to execution context
    assert invoked_contexts[0].workspace == "/home/user/project_workspace"
    assert invoked_contexts[0].peer_id == "user-peer"
    assert invoked_contexts[0].sender_id == "user-sender"
