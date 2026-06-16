from __future__ import annotations

from navi.config import write_default_config
from navi.diagnostics import run_diagnostics


def test_run_diagnostics_reports_config_capabilities_and_state(tmp_path):
    write_default_config(tmp_path)
    checks = run_diagnostics(tmp_path, project_dir=tmp_path)
    by_name = {check.name: check for check in checks}

    assert by_name["home"].status == "ok"
    assert by_name["config"].status == "ok"
    assert by_name["config.validation"].status == "ok"
    assert by_name["capabilities"].status == "ok"
    assert "registered" in by_name["capabilities"].detail
    assert by_name["service.runtime"].status in {"ok", "warn"}
    assert by_name["api.model.config"].status == "ok"
    assert by_name["browser.playwright"].status in {"ok", "missing"}
    assert by_name["computer.display"].status in {"ok", "missing"}
    assert by_name["connector.telegram.config"].status in {"ok", "missing"}
    assert by_name["connector.weixin.config"].status in {"ok", "missing"}
    assert "state.memory.db" in by_name
    assert "tool.git" in by_name
    assert "auth.codex" in by_name


def test_run_diagnostics_can_include_mock_connectivity(tmp_path):
    write_default_config(tmp_path)

    checks = run_diagnostics(tmp_path, project_dir=tmp_path, include_connectivity=True)
    by_name = {check.name: check for check in checks}

    assert by_name["api.model.connectivity"].status == "ok"
    assert by_name["api.model.connectivity"].detail == "mock provider"
