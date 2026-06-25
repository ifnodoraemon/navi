from __future__ import annotations

from pathlib import Path

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
