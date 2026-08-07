from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from navi import diagnostics
from navi.service import (
    SystemdNotifier,
    build_systemd_user_unit,
    run_with_systemd_watchdog,
    runtime_environment_error,
)


def test_build_systemd_user_unit_uses_project_and_home(tmp_path):
    (tmp_path / "src").mkdir()
    navi_home = tmp_path / ".navi"

    unit = build_systemd_user_unit(project_dir=tmp_path, navi_home=navi_home)

    assert "Description=Navi active assistant" in unit
    assert f"WorkingDirectory={tmp_path}" in unit
    assert f"Environment=PYTHONPATH={tmp_path / 'src'}" in unit
    assert f"Environment=NAVI_HOME={navi_home}" in unit
    assert "ExecStart=" in unit
    assert "-m navi.cli run" in unit
    assert "EnvironmentFile=" not in unit
    assert "ExecStartPre=/usr/bin/test -x " in unit
    assert "NotifyAccess=main" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=5s" in unit
    assert "WatchdogSec=90s" in unit
    assert "TimeoutStopSec=30s" in unit


def test_systemd_notifier_sends_ready_and_watchdog(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.addresses: list[str | bytes] = []
            self.payloads: list[bytes] = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def connect(self, address):
            self.addresses.append(address)

        def sendall(self, payload):
            self.payloads.append(payload)

    fake = FakeSocket()
    monkeypatch.setattr("navi.service.socket.socket", lambda *args: fake)
    notifier = SystemdNotifier.from_environment(
        {
            "NOTIFY_SOCKET": "@navi-notify",
            "WATCHDOG_USEC": "90000000",
            "WATCHDOG_PID": str(os.getpid()),
        }
    )

    assert notifier.watchdog_interval_seconds == 30.0
    assert notifier.ready("Navi active") is True
    assert fake.addresses == [b"\0navi-notify"]
    assert fake.payloads == [b"READY=1\nSTATUS=Navi active"]
    assert notifier.notify("WATCHDOG=1") is True
    assert fake.payloads[-1] == b"WATCHDOG=1"


def test_runtime_environment_error_reports_missing_executable(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    monkeypatch.setattr("navi.service.sys.executable", str(tmp_path / "missing-python"))
    monkeypatch.setattr("navi.service.sys.prefix", str(prefix))

    assert runtime_environment_error() == (
        f"python executable missing: {tmp_path / 'missing-python'}"
    )


@pytest.mark.asyncio
async def test_watchdog_runtime_failure_cancels_resident_workload():
    cancelled = False

    async def workload():
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True

    notifier = SystemdNotifier(watchdog_interval_seconds=0.001)
    with pytest.raises(RuntimeError, match="python executable missing"):
        await run_with_systemd_watchdog(
            workload(),
            status="Navi active",
            notifier=notifier,
            runtime_check=lambda: "python executable missing: /missing/python",
        )

    assert cancelled is True


def test_service_diagnostic_does_not_retry_with_another_systemctl_command(monkeypatch):
    calls: list[list[str]] = []

    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout="", stderr="unit unavailable")

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)

    check = diagnostics._service_runtime_check("navi.service")

    assert check.status == "warn"
    assert check.detail == "navi.service unknown/unknown exit_code=1"
    assert len(calls) == 1
    assert calls[0][0:3] == ["systemctl", "--user", "show"]


def test_playwright_browser_diagnostic_requires_real_executable(tmp_path):
    empty_cache = tmp_path / "empty-cache" / "1.0"
    empty_cache.mkdir(parents=True)

    assert diagnostics.playwright_browser_executable((empty_cache.parent,)) is None

    browser = tmp_path / "browser-cache" / "chromium-1" / "chrome-linux" / "chrome"
    browser.parent.mkdir(parents=True)
    browser.write_text("#!/bin/sh\n", encoding="utf-8")
    browser.chmod(0o755)

    assert diagnostics.playwright_browser_executable((browser.parents[2],)) == browser
