from __future__ import annotations

from pathlib import Path

import pytest

import navi.workspaces as workspaces_module
from navi.control import CurrentStateBuilder, SurfaceContext, current_state_facts
from navi.loop_contracts import LockMode, MergeStatus
from navi.workspaces import ShadowWorkspaceManager, WorkspaceLockStore, fingerprint_workspace


def test_shadow_workspace_merge_back_applies_clean_agent_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('base')\n", encoding="utf-8")

    manager = ShadowWorkspaceManager(tmp_path / ".navi")
    shadow = manager.create_shadow(run_id="run-1", workspace=repo)
    shadow_root = Path(shadow.shadow_workspace)
    (shadow_root / "app.py").write_text("print('agent')\n", encoding="utf-8")
    (shadow_root / "new.txt").write_text("created by agent\n", encoding="utf-8")

    result = manager.merge_back(shadow)

    assert result.status == MergeStatus.CLEAN
    assert result.conflicts == ()
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('agent')\n"
    assert (repo / "new.txt").read_text(encoding="utf-8") == "created by agent\n"


def test_shadow_workspace_merge_back_preserves_human_edits_on_conflict(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "app.py"
    target.write_text("print('base')\n", encoding="utf-8")

    manager = ShadowWorkspaceManager(tmp_path / ".navi")
    shadow = manager.create_shadow(run_id="run-2", workspace=repo)
    Path(shadow.shadow_workspace, "app.py").write_text("print('agent')\n", encoding="utf-8")
    target.write_text("print('human')\n", encoding="utf-8")

    result = manager.merge_back(shadow)

    assert result.status == MergeStatus.CONFLICTED
    assert result.conflicts == ("app.py",)
    assert target.read_text(encoding="utf-8") == "print('human')\n"
    artifact = Path(result.artifact_path) / "app.py"
    text = artifact.read_text(encoding="utf-8")
    assert "<<<<<<< CURRENT" in text
    assert "print('human')" in text
    assert "print('agent')" in text


def test_shadow_workspace_merge_back_keeps_non_overlapping_human_edits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.py").write_text("base agent\n", encoding="utf-8")
    (repo / "human.py").write_text("base human\n", encoding="utf-8")

    manager = ShadowWorkspaceManager(tmp_path / ".navi")
    shadow = manager.create_shadow(run_id="run-3", workspace=repo)
    Path(shadow.shadow_workspace, "agent.py").write_text("agent edit\n", encoding="utf-8")
    (repo / "human.py").write_text("human edit\n", encoding="utf-8")

    result = manager.merge_back(shadow)

    assert result.status == MergeStatus.CLEAN
    assert (repo / "agent.py").read_text(encoding="utf-8") == "agent edit\n"
    assert (repo / "human.py").read_text(encoding="utf-8") == "human edit\n"


def test_shadow_workspace_merge_back_rolls_back_on_apply_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("base-a\n", encoding="utf-8")
    (repo / "b.py").write_text("base-b\n", encoding="utf-8")
    manager = ShadowWorkspaceManager(tmp_path / ".navi")
    shadow = manager.create_shadow(run_id="run-rollback", workspace=repo)
    shadow_root = Path(shadow.shadow_workspace)
    (shadow_root / "a.py").write_text("agent-a\n", encoding="utf-8")
    (shadow_root / "b.py").write_text("agent-b\n", encoding="utf-8")

    original_apply = workspaces_module._apply_shadow_file
    calls = 0

    def fail_second_apply(source: Path, dest: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated apply failure")
        original_apply(source, dest)

    monkeypatch.setattr(workspaces_module, "_apply_shadow_file", fail_second_apply)

    with pytest.raises(RuntimeError, match="simulated apply failure"):
        manager.merge_back(shadow)

    assert (repo / "a.py").read_text(encoding="utf-8") == "base-a\n"
    assert (repo / "b.py").read_text(encoding="utf-8") == "base-b\n"


def test_workspace_lock_store_blocks_conflicting_writers_and_releases(tmp_path: Path) -> None:
    locks = WorkspaceLockStore(tmp_path)

    writer = locks.acquire(
        owner_run_id="run-a",
        resource="src/navi/api.py",
        mode=LockMode.WRITE,
        ttl_seconds=60,
    )
    blocked = locks.acquire(
        owner_run_id="run-b",
        resource="src/navi/api.py",
        mode=LockMode.READ,
        ttl_seconds=60,
    )
    other_file = locks.acquire(
        owner_run_id="run-b",
        resource="src/navi/control.py",
        mode=LockMode.WRITE,
        ttl_seconds=60,
    )

    assert writer.acquired is True
    assert blocked.acquired is False
    assert blocked.conflicts[0].owner_run_id == "run-a"
    assert other_file.acquired is True

    assert locks.release(owner_run_id="run-a", resource="src/navi/api.py") == 1
    reader = locks.acquire(
        owner_run_id="run-b",
        resource="src/navi/api.py",
        mode=LockMode.READ,
        ttl_seconds=60,
    )
    assert reader.acquired is True


def test_current_state_includes_active_workspace_locks(tmp_path: Path) -> None:
    WorkspaceLockStore(tmp_path).acquire(
        owner_run_id="loop-run-1",
        resource="src/navi/api.py",
        mode=LockMode.WRITE,
        ttl_seconds=60,
    )

    state = CurrentStateBuilder(tmp_path).build(
        SurfaceContext(
            home=tmp_path,
            source="cli",
            peer_id="cli",
            sender_id="tester",
            workspace=str(tmp_path),
        )
    )
    facts = current_state_facts(state)

    assert facts["lock_state"][0]["owner_run_id"] == "loop-run-1"
    assert facts["lock_state"][0]["resource"] == "src/navi/api.py"
    assert facts["lock_state"][0]["mode"] == LockMode.WRITE


def test_workspace_fingerprint_changes_when_file_content_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "app.py"
    target.write_text("one\n", encoding="utf-8")
    before = fingerprint_workspace(repo)

    target.write_text("two\n", encoding="utf-8")
    after = fingerprint_workspace(repo)

    assert before.digest != after.digest
    assert before.hash_for("app.py") != after.hash_for("app.py")
