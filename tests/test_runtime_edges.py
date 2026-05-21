from __future__ import annotations

import time

import pytest

from navi.auth import AuthInspector
from navi.cli_providers import CliProviderSpec, get_cli_provider_spec
from navi.daemon import SystemDaemon
from navi.memory import MemoryStore
from navi.paths import ensure_home, navi_home
from navi.tasks import TaskStore


def test_paths_use_env_or_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_HOME", str(tmp_path / "custom"))
    assert navi_home() == (tmp_path / "custom").resolve()
    assert ensure_home().exists()

    monkeypatch.delenv("NAVI_HOME")
    monkeypatch.chdir(tmp_path)
    assert navi_home() == (tmp_path / ".navi").resolve()


def test_cli_provider_lookup():
    assert get_cli_provider_spec("codex") is not None
    assert get_cli_provider_spec("missing") is None


def test_auth_inspector_handles_missing_and_negative_auth(monkeypatch):
    inspector = AuthInspector()
    missing = CliProviderSpec(name="missing", binary="missing-binary")
    assert inspector._status_for(missing).installed is False

    monkeypatch.setattr("navi.auth.shutil.which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr("navi.auth.AuthInspector._run", staticmethod(lambda command: "not logged in"))
    spec = CliProviderSpec(
        name="tool",
        binary="tool",
        version_args=("--version",),
        auth_status_args=("auth", "status"),
        auth_negative_markers=("not logged in",),
    )

    status = inspector._status_for(spec)

    assert status.installed is True
    assert status.authenticated is False


def test_memory_rejects_invalid_types_and_statuses(tmp_path):
    store = MemoryStore(tmp_path)

    with pytest.raises(ValueError, match="Unsupported memory type"):
        store.add_item("unknown", "content", source="test")

    with pytest.raises(ValueError, match="Unsupported memory status"):
        store.add_item("fact", "content", source="test", status="unknown")

    item = store.add_item("fact", "coverage fact", source="test", status="active")
    assert store.get_item(item.id) == item

    with pytest.raises(ValueError, match="Unsupported memory status"):
        store.set_status(item.id, "unknown")

    assert store.set_status(item.id, "archived").status == "archived"
    assert store.get_item("missing") is None


def test_memory_recall_filters_status_and_expiry(tmp_path):
    store = MemoryStore(tmp_path)
    store.add_item("fact", "active coverage fact", source="test", status="active")
    store.add_item("fact", "proposed coverage fact", source="test", status="proposed")
    store.add_item(
        "fact",
        "expired coverage fact",
        source="test",
        status="active",
        expires_at=time.time() - 1,
    )

    rendered = store.render_context("coverage")

    assert "active coverage fact" in rendered
    assert "proposed coverage fact" not in rendered
    assert "expired coverage fact" not in rendered


@pytest.mark.asyncio
async def test_daemon_processes_due_watches(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    store = TaskStore(tmp_path)
    watch = store.create_watch(
        cron="*/5 * * * *",
        prompt="check coverage",
        peer_id="peer",
        sender_id="sender",
        next_run_at=time.time() - 1,
    )

    results = await SystemDaemon(tmp_path).process_watches_once()

    assert len(results) == 1
    assert results[0]["task_id"]
    updated = store.list_watches()[0]
    assert updated.id == watch.id
    assert updated.last_run_at > 0
    assert updated.next_run_at > updated.last_run_at
