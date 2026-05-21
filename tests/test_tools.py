from __future__ import annotations

from navi.tasks import TaskStore
from navi.tools import build_tool_gateway


def test_core_tool_registry_lists_fact_only_tools(tmp_path):
    registry = build_tool_gateway(tmp_path, project_dir=tmp_path)

    specs = {spec.name: spec for spec in registry.list_specs()}

    assert {
        "connector.weixin.status",
        "connector.telegram.status",
        "filesystem.list",
        "git.status",
        "provider.config",
        "service.status",
        "task.status",
    } <= set(specs)
    assert all(spec.facts_only for spec in specs.values())
    assert all(not spec.mutates for spec in specs.values())
    assert specs["connector.weixin.status"].source == "connector.weixin"
    assert specs["connector.telegram.status"].source == "connector.telegram"


def test_task_status_tool_returns_task_facts(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create("tool task", status="preparing")
    approval = store.create_approval(task_id=task.id, peer_id="peer", sender_id="sender")
    registry = build_tool_gateway(tmp_path, project_dir=tmp_path)

    result = registry.call("task.status", {"task_id": task.id})

    assert result.ok is True
    assert result.facts["task"]["id"] == task.id
    assert "code" not in result.facts["approvals"][0]
    assert result.facts["approvals"][0]["code_present"] is True
    assert result.error == ""
    assert approval.code
    logs = store.list_tool_call_logs()
    assert logs[0].tool == "task.status"
    assert logs[0].ok is True


def test_filesystem_list_tool_returns_directory_facts(tmp_path):
    (tmp_path / "alpha.txt").write_text("alpha", encoding="utf-8")
    registry = build_tool_gateway(tmp_path, project_dir=tmp_path)

    result = registry.call("filesystem.list", {"path": str(tmp_path), "limit": 5})

    assert result.ok is True
    assert result.facts["path"] == str(tmp_path)
    assert result.facts["entries"][0]["name"] == "alpha.txt"


def test_unknown_tool_returns_structured_error(tmp_path):
    registry = build_tool_gateway(tmp_path, project_dir=tmp_path)

    result = registry.call("missing.tool", {})

    assert result.ok is False
    assert result.error == "tool not found: missing.tool"


def test_tool_gateway_lists_sources_and_can_filter_tools(tmp_path):
    gateway = build_tool_gateway(tmp_path, project_dir=tmp_path)

    assert {"core", "connector.weixin", "connector.telegram"} <= set(gateway.list_sources())

    core_only = build_tool_gateway(tmp_path, project_dir=tmp_path, allow_sources={"core"})
    names = {spec.name for spec in core_only.list_specs()}

    assert "service.status" in names
    assert "connector.weixin.status" not in names
    assert "connector.telegram.status" not in names

    disabled = build_tool_gateway(tmp_path, project_dir=tmp_path, disabled_tools={"service.status"})
    assert disabled.get("service.status") is None


def test_tool_gateway_filters_by_permission_ceiling(tmp_path):
    gateway = build_tool_gateway(tmp_path, project_dir=tmp_path, permission_ceiling="read")

    assert {spec.permission for spec in gateway.list_specs()} == {"read"}
