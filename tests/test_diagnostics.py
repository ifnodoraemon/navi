from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from navi.config import NaviConfig, write_default_config
from navi.connector_registry import ConnectorAdapter, ConnectorSpec
from navi.diagnostics import (
    DiagnosticCheck,
    _api_config_checks,
    _api_connectivity_checks,
    _browser_dependency_checks,
    _check_path,
    _computer_use_checks,
    _connector_config_checks,
    _connector_status_file_checks,
    _external_tool_checks,
    _mcp_config_checks,
    _search_config_checks,
    _search_connectivity_checks,
    _service_facts,
    _service_runtime_check,
    _state_checks,
    run_diagnostics,
)


def test_check_path_existing_and_missing(tmp_path: Path):
    existing = tmp_path / "hello.txt"
    existing.write_text("content", encoding="utf-8")
    missing = tmp_path / "missing.txt"

    res_ok = _check_path("test_file", existing, required=True)
    assert res_ok.status == "ok"
    assert res_ok.name == "test_file"

    res_err = _check_path("test_file", missing, required=True)
    assert res_err.status == "error"

    res_miss = _check_path("test_file", missing, required=False)
    assert res_miss.status == "missing"


def test_state_checks(tmp_path: Path):
    checks = _state_checks(tmp_path)
    names = [c.name for c in checks]
    assert "skills.dir" in names
    assert "state.memory.db" in names
    assert "state.traces.db" in names


def test_external_tool_checks():
    def fake_which(name: str):
        lookup = {"git": "/usr/bin/git"}
        return lookup.get(name)

    with patch("shutil.which", side_effect=fake_which):
        checks = _external_tool_checks(("git", "missing_cmd"))
        status_map = {c.name: c.status for c in checks}
        assert status_map["tool.git"] == "ok"
        assert status_map["tool.missing_cmd"] == "missing"


def test_browser_dependency_checks():
    with patch("shutil.which", return_value="/fake/playwright"), patch(
        "navi.diagnostics.playwright_browser_executable", return_value=Path("/fake/chromium")
    ):
        checks = _browser_dependency_checks()
        status_map = {c.name: c.status for c in checks}
        assert status_map["browser.playwright"] == "ok"
        assert status_map["browser.chromium"] == "ok"

    with patch("shutil.which", return_value=None), patch(
        "navi.diagnostics.playwright_browser_executable", return_value=None
    ):
        checks = _browser_dependency_checks()
        status_map = {c.name: c.status for c in checks}
        assert status_map["browser.playwright"] == "missing"
        assert status_map["browser.chromium"] == "missing"


def test_computer_use_checks(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DISPLAY", ":0")
    with patch("shutil.which", return_value="/fake/xdotool"):
        checks = _computer_use_checks()
        status_map = {c.name: c.status for c in checks}
        assert status_map["computer.display"] == "ok"
        assert status_map["computer.tool.xdotool"] == "ok"

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with patch("shutil.which", return_value=None):
        checks = _computer_use_checks()
        status_map = {c.name: c.status for c in checks}
        assert status_map["computer.display"] == "missing"
        assert status_map["computer.tool.xdotool"] == "missing"


def test_connector_checks(tmp_path: Path):
    def fake_diagnostics(home: Path):
        return [
            {"name": "telegram.token", "status": "ok", "detail": "present"},
            {"status": "warn"},
        ]

    spec_tg = ConnectorSpec(
        name="telegram",
        surface="chat",
        status_tool="telegram_status",
        status_description="Telegram",
        session_alias_prefix="tg",
        local_source="telegram",
    )
    spec_wx = ConnectorSpec(
        name="weixin",
        surface="chat",
        status_tool="weixin_status",
        status_description="Weixin",
        session_alias_prefix="wx",
        local_source="weixin",
    )
    adapters = [
        ConnectorAdapter(
            spec=spec_tg,
            enabled=lambda p: True,
            status=lambda p: {},
            diagnostics=fake_diagnostics,
            register_tools=MagicMock(),
        ),
        ConnectorAdapter(
            spec=spec_wx,
            enabled=lambda p: True,
            status=lambda p: {},
            diagnostics=None,
            register_tools=MagicMock(),
        ),
    ]

    cfg_checks = _connector_config_checks(tmp_path, adapters)
    assert len(cfg_checks) == 2
    assert cfg_checks[0].name == "telegram.token"
    assert cfg_checks[0].status == "ok"
    assert cfg_checks[1].name == "connector.telegram.diagnostics"
    assert cfg_checks[1].status == "warn"

    file_checks = _connector_status_file_checks(tmp_path, adapters)
    assert len(file_checks) == 2
    assert file_checks[0].name == "connector.telegram.status_file"
    assert file_checks[0].status == "missing"


def test_api_config_checks():
    config = NaviConfig()
    config.model.api_base_url = ""
    config.model.api_key = ""
    checks = _api_config_checks(config)
    assert checks[0].status == "error"
    assert "api_base_url missing" in checks[0].detail

    config.model.api_base_url = "https://api.example.com"
    config.model.api_key = ""
    checks = _api_config_checks(config)
    assert checks[0].status == "error"
    assert "api_key missing" in checks[0].detail

    config.model.api_key = "sk-secret-token"
    checks = _api_config_checks(config)
    assert checks[0].status == "ok"
    assert "sk-secret-token" not in checks[0].detail


def test_search_config_checks():
    config = NaviConfig()
    checks = _search_config_checks(config)
    assert checks[0].name == "search.config"


def test_mcp_config_checks(tmp_path: Path):
    with patch("navi.mcp_tools.load_mcp_config") as mock_load:
        mock_report = MagicMock()
        mock_report.errors = ["invalid json in config"]
        mock_load.return_value = mock_report
        checks = _mcp_config_checks(tmp_path)
        assert checks[0].status == "error"
        assert "invalid json" in checks[0].detail

        mock_report.errors = []
        mock_report.servers = ["srv1", "srv2"]
        mock_report.path = tmp_path / "mcp.json"
        checks = _mcp_config_checks(tmp_path)
        assert checks[0].status == "ok"
        assert "2 enabled server(s)" in checks[0].detail


def test_search_connectivity_checks(tmp_path: Path):
    write_default_config(tmp_path)
    with patch("navi.core_tools.web_search.enabled_search_provider_ids", return_value=["bing", "searxng"]), patch(
        "navi.core_tools.web_search._web_search"
    ) as mock_search:
        ok_result = MagicMock(ok=True, facts={"provider_kind": "bing", "results": [1, 2]})
        err_result = MagicMock(ok=False, facts={"error_reason": "timeout"}, error="Connection timed out")
        mock_search.side_effect = [ok_result, err_result]

        checks = _search_connectivity_checks(tmp_path)
        assert len(checks) == 2
        assert checks[0].status == "ok"
        assert checks[1].status == "error"


def test_api_connectivity_checks():
    config = NaviConfig()
    config.model.api_base_url = ""
    assert _api_connectivity_checks(config)[0].status == "warn"

    config.model.api_base_url = "https://api.openai.com/v1"
    config.model.api_key = "dummy"
    config.model.provider = "openai-compatible"
    config.model.model = "gpt-4o"

    with patch("httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp
        checks = _api_connectivity_checks(config)
        assert checks[0].status == "ok"

        config.model.provider = "anthropic"
        checks_ant = _api_connectivity_checks(config)
        assert checks_ant[0].status == "ok"

        mock_post.side_effect = RuntimeError("network down")
        checks_err = _api_connectivity_checks(config)
        assert checks_err[0].status == "warn"
        assert "RuntimeError" in checks_err[0].detail


def test_service_facts_and_runtime_check():
    with patch("shutil.which", return_value=None):
        facts = _service_facts("navi.service")
        assert facts["exit_code"] == 127

    with patch("shutil.which", return_value="/usr/bin/systemctl"), patch(
        "subprocess.run"
    ) as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ActiveState=active\nSubState=running\n", stderr=""
        )
        facts = _service_facts("navi.service")
        assert facts["exit_code"] == 0
        assert facts["properties"]["ActiveState"] == "active"

        chk = _service_runtime_check("navi.service")
        assert chk.status == "ok"

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=3, stdout="ActiveState=inactive\nSubState=dead\n", stderr=""
        )
        chk_inactive = _service_runtime_check("navi.service")
        assert chk_inactive.status == "warn"

        mock_run.side_effect = subprocess.SubprocessError("ctl fail")
        chk_err = _service_runtime_check("navi.service")
        assert chk_err.status == "warn"


def test_run_diagnostics_integration(tmp_path: Path):
    write_default_config(tmp_path)
    checks = run_diagnostics(tmp_path, project_dir=tmp_path, include_connectivity=False)
    assert len(checks) > 10
    names = {c.name for c in checks}
    assert "home" in names
    assert "config" in names
    assert "capabilities" in names


def test_run_diagnostics_with_connectivity(tmp_path: Path):
    write_default_config(tmp_path)
    with patch("navi.diagnostics._api_connectivity_checks", return_value=[DiagnosticCheck("api.conn", "ok")]), patch(
        "navi.diagnostics._search_connectivity_checks", return_value=[DiagnosticCheck("search.conn", "ok")]
    ):
        checks = run_diagnostics(tmp_path, project_dir=tmp_path, include_connectivity=True)
        names = {c.name for c in checks}
        assert "api.conn" in names
        assert "search.conn" in names

