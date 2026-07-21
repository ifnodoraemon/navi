from __future__ import annotations

from types import SimpleNamespace

from navi import diagnostics
from navi.service import build_systemd_user_unit


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
    assert "Restart=no" in unit
    assert "RestartSec=" not in unit


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
