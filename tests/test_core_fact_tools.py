from __future__ import annotations

import subprocess
from pathlib import Path

from navi.core_tools.utils import _web_search
from navi.tools import build_tool_gateway


def test_codebase_search_uses_runtime_rag_and_navi_home_cache(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("def target_workflow():\n    return 'ok'\n", encoding="utf-8")

    gateway = build_tool_gateway(home, project_dir=workspace)
    result = gateway.call("codebase.search", {"query": "target_workflow", "limit": 1})

    assert result.ok is True
    assert result.facts["results"]
    assert (home / "codebase_rag.db").exists()
    assert not (workspace / ".navi" / "codebase_rag.db").exists()


def test_directory_list_returns_workspace_entries(tmp_path: Path) -> None:
    (tmp_path / "visible.txt").write_text("ok", encoding="utf-8")
    (tmp_path / ".hidden").write_text("secret", encoding="utf-8")

    gateway = build_tool_gateway(tmp_path / "home", project_dir=tmp_path)
    result = gateway.call("directory.list", {"path": "."})

    assert result.ok is True
    names = {entry["name"] for entry in result.facts["entries"]}
    assert "visible.txt" in names
    assert ".hidden" not in names
    assert "response" not in result.facts


def test_web_search_uses_reachable_bing_endpoint(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        del kwargs
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="<html><body>Search result</body></html>",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _web_search({"query": "navi smoke"})

    assert result.ok is True
    assert "https://www.bing.com/search?q=navi%20smoke" in captured["cmd"]
    assert result.facts["provider"] == "curl_bing"
    assert "Search result" in result.facts["response"]["text"]


def test_web_search_returns_curl_transport_facts(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            cmd,
            35,
            stdout="",
            stderr="SSL_ERROR_SYSCALL",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _web_search({"query": "navi smoke"})

    assert result.ok is False
    assert result.facts["provider"] == "curl_bing"
    assert result.facts["error_reason"] == "search_provider_error"
    assert result.facts["curl_exit_code"] == 35
    assert result.facts["stderr"] == "SSL_ERROR_SYSCALL"
