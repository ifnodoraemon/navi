from __future__ import annotations

from navi.tasks import TaskStore
from navi.tools import build_core_tool_registry


def test_core_tool_registry_lists_fact_only_tools(tmp_path):
    registry = build_core_tool_registry(tmp_path, project_dir=tmp_path)

    specs = {spec.name: spec for spec in registry.list_specs()}

    assert {
        "connector.weixin.status",
        "filesystem.list",
        "git.status",
        "provider.config",
        "service.status",
        "task.status",
    } <= set(specs)
    assert all(spec.facts_only for spec in specs.values())
    assert all(not spec.mutates for spec in specs.values())


def test_task_status_tool_returns_task_facts(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create("tool task", status="preparing")
    approval = store.create_approval(task_id=task.id, peer_id="peer", sender_id="sender")
    registry = build_core_tool_registry(tmp_path, project_dir=tmp_path)

    result = registry.call("task.status", {"task_id": task.id})

    assert result.ok is True
    assert result.facts["task"]["id"] == task.id
    assert result.facts["approvals"][0]["code"] == approval.code
    assert result.error == ""


def test_filesystem_list_tool_returns_directory_facts(tmp_path):
    (tmp_path / "alpha.txt").write_text("alpha", encoding="utf-8")
    registry = build_core_tool_registry(tmp_path, project_dir=tmp_path)

    result = registry.call("filesystem.list", {"path": str(tmp_path), "limit": 5})

    assert result.ok is True
    assert result.facts["path"] == str(tmp_path)
    assert result.facts["entries"][0]["name"] == "alpha.txt"


def test_unknown_tool_returns_structured_error(tmp_path):
    registry = build_core_tool_registry(tmp_path, project_dir=tmp_path)

    result = registry.call("missing.tool", {})

    assert result.ok is False
    assert result.error == "tool not found: missing.tool"
