from __future__ import annotations

import time

import pytest

from navi.auth import AuthInspector
from navi.cli_providers import CliProviderSpec, get_cli_provider_spec
from navi.daemon import SystemDaemon
from navi.memory import MemoryStore
from navi.paths import ensure_home, navi_home
from navi.runs import RunStore


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


def test_auth_inspector_reports_missing_binary():
    inspector = AuthInspector()
    missing = CliProviderSpec(name="missing", binary="missing-binary")

    status = inspector._status_for(missing)

    assert status.installed is False
    assert status.authenticated is False
    assert "binary not found" in status.detail


def test_auth_inspector_detects_binary_and_auth_file(tmp_path, monkeypatch):
    fake_bin = tmp_path / "fake-cli"
    fake_bin.write_text("#!/bin/sh\necho fake-cli 1.0\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path))

    status = AuthInspector()._status_for(
        CliProviderSpec(
            name="fake",
            binary="fake-cli",
            version_args=("--version",),
            auth_files=(str(auth_file),),
        )
    )

    assert status.installed is True
    assert status.authenticated is True
    assert status.version == "fake-cli 1.0"


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
    store = RunStore(tmp_path)
    watch = store.create_watch(
        cron="*/5 * * * *",
        prompt="check coverage",
        peer_id="peer",
        sender_id="sender",
        next_run_at=time.time() - 1,
    )

    results = await SystemDaemon(tmp_path).process_watches_once()

    assert len(results) == 1
    assert results[0]["action"] == "watch"
    assert results[0]["run_id"] == ""
    assert results[0]["peer_id"] == "peer"
    assert "check coverage" in results[0]["message"]
    assert store.list_by_status("awaiting_approval") == []
    updated = store.list_watches()[0]
    assert updated.id == watch.id
    assert updated.last_run_at > 0
    assert updated.next_run_at > updated.last_run_at


@pytest.mark.asyncio
async def test_daemon_disables_one_shot_watch_after_run(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    store = RunStore(tmp_path)
    watch = store.create_watch(
        cron="once",
        prompt="pmp related knowledge",
        peer_id="peer",
        sender_id="sender",
        next_run_at=time.time() - 1,
        kind="once",
    )

    results = await SystemDaemon(tmp_path).process_watches_once()

    assert len(results) == 1
    updated = store.get_watch(watch.id)
    assert updated is not None
    assert updated.kind == "once"
    assert updated.enabled is False
    assert updated.last_run_at > 0


@pytest.mark.asyncio
async def test_daemon_prunes_excess_failed_watch_delegate_spawns(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    daemon = SystemDaemon(tmp_path)
    async def no_events():
        return []

    monkeypatch.setattr(daemon, "process_events_once", no_events)
    for index in range(55):
        store.create(f"old failed watch {index}", status="failed", source="watch", kind="delegation")
    local_failed = store.create("keep local failed", status="failed", source="local", kind="delegation")

    results = await daemon.process_watches_once()

    assert results == []
    assert store.count_runs(status="failed", source="watch", kind="delegation") == 50
    assert store.get(local_failed.id) is not None
