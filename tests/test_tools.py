from __future__ import annotations

from navi.actions.specs import ACTION_SPECS
from navi.runs import RunStore
from navi.safeguards import classify_capability
from navi.tools import build_tool_gateway
from navi.config import write_default_config


def test_core_tool_registry_lists_fact_only_tools(tmp_path):
    registry = build_tool_gateway(tmp_path, project_dir=tmp_path)

    specs = {spec.name: spec for spec in registry.list_specs()}

    assert {
        "connector.weixin.status",
        "connector.telegram.status",
        "file.read",
        "file.write",
        "directory.list",
        "git.status",
        "hooks.list",
        "provider.config",
        "shell.run",
        "delegate.list",
        "service.status",
        "delegate.status",
        "test.run",
    } <= set(specs)
    assert specs["file.read"].facts_only is True
    assert specs["file.write"].mutates is True
    assert specs["file.write"].permission == "write"
    assert specs["shell.run"].mutates is True
    assert specs["test.run"].mutates is True
    assert specs["browser.screenshot"].permission == "write"
    assert specs["browser.screenshot"].mutates is True
    assert specs["connector.weixin.status"].source == "connector.weixin"
    assert specs["connector.telegram.status"].source == "connector.telegram"
    assert all(spec.facts_only is True for spec in specs.values())
    assert all(spec.facts_only is True for spec in ACTION_SPECS)
    assert all(spec.permission == "write" for spec in specs.values() if spec.mutates)
    assert all("properties" in spec.output_schema or "items" in spec.output_schema for spec in specs.values())
    assert "items" in specs["memory.recall"].output_schema["properties"]
    assert "conflicts" in specs["memory.conflicts"].output_schema["properties"]
    assert "exit_code" in specs["shell.run"].output_schema["properties"]
    assert "configured" in specs["connector.weixin.status"].output_schema["properties"]
    assert "configured" in specs["connector.telegram.status"].output_schema["properties"]
    assert classify_capability(specs["shell.run"]).risk_class == "high"
    assert classify_capability(specs["shell.run"]).confirmation_required is True
    assert "terminal" in classify_capability(specs["shell.run"]).sensitive_contexts
    assert "filesystem" in classify_capability(specs["file.write"]).sensitive_contexts
    assert "browser" in classify_capability(specs["browser.screenshot"]).sensitive_contexts


def test_workflow_action_specs_declare_dynamic_workflow_safeguards():
    specs = {spec.name: spec for spec in ACTION_SPECS}

    assert specs["workflow.propose"].permission == "prepare"
    assert specs["workflow.status"].permission == "read"
    assert specs["workflow.run"].permission == "write"
    assert classify_capability(specs["workflow.propose"]).risk_class == "medium"
    assert "dynamic_workflow" in classify_capability(specs["workflow.propose"]).sensitive_contexts
    assert classify_capability(specs["workflow.run"]).confirmation_required is True


def test_run_status_tool_returns_task_facts(tmp_path):
    store = RunStore(tmp_path)
    task = store.create("tool task", status="preparing", workspace=str(tmp_path))
    approval = store.create_approval(run_id=task.id, peer_id="peer", sender_id="sender")
    registry = build_tool_gateway(tmp_path, project_dir=tmp_path)

    result = registry.call("delegate.status", {"run_id": task.id})

    assert result.ok is True
    assert result.facts["run"]["id"] == task.id
    assert "code" not in result.facts["approvals"][0]
    assert result.facts["approvals"][0]["code_present"] is True
    assert result.error == ""
    assert approval.code
    logs = store.list_tool_call_logs()
    assert logs[0].tool == "delegate.status"
    assert logs[0].ok is True


def test_task_list_tool_returns_tasks_and_watches(tmp_path):
    store = RunStore(tmp_path)
    task = store.create("tool task", status="preparing", workspace=str(tmp_path))
    failed = store.create("failed task", status="failed", workspace=str(tmp_path))
    watch = store.create_watch(
        cron="0 20 * * *",
        prompt="teach common knowledge",
        peer_id="peer",
        sender_id="sender",
        next_run_at=1,
        workspace=str(tmp_path),
    )
    registry = build_tool_gateway(tmp_path, project_dir=tmp_path)

    result = registry.call("delegate.list", {"limit": 10})

    assert result.ok is True
    assert {item["id"] for item in result.facts["runs"]} == {task.id, failed.id}
    assert result.facts["watches"][0]["id"] == watch.id
    assert result.facts["run_status_counts"] == {"failed": 1, "preparing": 1}
    assert result.facts["returned_run_count"] == 2
    assert result.facts["run_limit"] == 10


def test_memory_conflicts_tool_returns_declared_conflict_facts(tmp_path):
    from navi.memory import MemoryStore

    store = MemoryStore(tmp_path)
    old_item = store.add_item("preference", "Prefer legacy tests.", source="test", status="active")
    new_item = store.add_item(
        "preference",
        "Prefer current tests.",
        source="test",
        status="active",
        metadata={"contradicts": [old_item.id]},
    )
    registry = build_tool_gateway(tmp_path, project_dir=tmp_path)

    result = registry.call("memory.conflicts", {})
    recall = registry.call("memory.recall", {"query": "current"})

    assert result.ok is True
    assert result.facts["count"] == 1
    assert result.facts["unresolved_count"] == 1
    conflict = result.facts["conflicts"][0]
    assert conflict["item"]["id"] == new_item.id
    assert conflict["conflicting_item_id"] == old_item.id
    assert conflict["status"] == "unresolved"
    assert recall.facts["items"][0]["conflicts"]


def test_directory_list_tool_returns_directory_facts(tmp_path):
    (tmp_path / "alpha.txt").write_text("alpha", encoding="utf-8")
    registry = build_tool_gateway(tmp_path, project_dir=tmp_path)

    result = registry.call("directory.list", {"path": str(tmp_path), "limit": 5})

    assert result.ok is True
    assert result.facts["path"] == str(tmp_path)
    assert result.facts["entries"][0]["name"] == "alpha.txt"


def test_directory_and_git_tools_reject_paths_outside_project(tmp_path):
    outside = tmp_path.parent
    registry = build_tool_gateway(tmp_path, project_dir=tmp_path)

    directory = registry.call("directory.list", {"path": str(outside)})
    git = registry.call("git.status", {"path": str(outside)})

    assert directory.ok is False
    assert directory.error == "path must be within the project directory"
    assert git.ok is False
    assert git.error == "path must be within the project directory"


def test_file_read_and_write_are_workspace_scoped_and_audited(tmp_path):
    registry = build_tool_gateway(tmp_path, project_dir=tmp_path)

    written = registry.call("file.write", {"path": "nested/out.txt", "content": "hello", "create_dirs": True})
    read = registry.call("file.read", {"path": "nested/out.txt"})
    outside = registry.call("file.write", {"path": str(tmp_path.parent / "outside.txt"), "content": "no"})

    assert written.ok is True
    assert written.facts["bytes_written"] == 5
    assert read.ok is True
    assert read.facts["content"] == "hello"
    assert outside.ok is False
    assert outside.error == "path must be within the project directory"
    assert [log.tool for log in RunStore(tmp_path).list_tool_call_logs(limit=3)] == [
        "file.write",
        "file.read",
        "file.write",
    ]


def test_shell_and_test_run_use_non_shell_commands_inside_workspace(tmp_path):
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    registry = build_tool_gateway(tmp_path, project_dir=tmp_path)

    shell = registry.call("shell.run", {"command": ["python", "-c", "print('ok')"], "timeout_seconds": 5})
    tests = registry.call("test.run", {"command": ["pytest", "-q", "test_sample.py"], "timeout_seconds": 20})
    outside = registry.call("shell.run", {"command": ["python", "-V"], "cwd": str(tmp_path.parent)})

    assert shell.ok is True
    assert shell.facts["stdout"].strip() == "ok"
    assert tests.ok is True
    assert "1 passed" in tests.facts["stdout"]
    assert outside.ok is False
    assert outside.error == "path must be within the project directory"


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

    allowed = build_tool_gateway(tmp_path, project_dir=tmp_path, allowed_tools={"service.status"})
    allowed_names = {spec.name for spec in allowed.list_specs()}
    assert allowed_names == {"service.status"}


def test_tool_gateway_filters_by_permission_ceiling(tmp_path):
    gateway = build_tool_gateway(tmp_path, project_dir=tmp_path, permission_ceiling="read")

    assert {spec.permission for spec in gateway.list_specs()} == {"read"}
    assert "file.write" not in {spec.name for spec in gateway.list_specs()}
    assert "shell.run" not in {spec.name for spec in gateway.list_specs()}
    assert "browser.screenshot" not in {spec.name for spec in gateway.list_specs()}


def test_provider_config_tool_reports_model_fallbacks(tmp_path):
    write_default_config(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  provider: private-primary",
                "  kind: openai-compatible",
                "  model: primary-model",
                "  api_base_url: http://primary.local/v1",
                "  fallbacks:",
                "    - provider: mock",
                "      model: mock",
                "  routes:",
                "    responder:",
                "      provider: mock",
                "      model: mock",
            ]
        ),
        encoding="utf-8",
    )
    gateway = build_tool_gateway(tmp_path, project_dir=tmp_path, allow_sources={"core"})

    result = gateway.call("provider.config", {})

    assert result.ok is True
    assert result.facts["provider"] == "private-primary"
    assert result.facts["fallbacks"][0]["provider"] == "mock"
    assert result.facts["routes"]["responder"]["provider"] == "mock"


def test_tool_schema_validation(tmp_path):
    from navi.tools import ToolRegistry, ToolSpec, ToolResult
    registry = ToolRegistry(home=tmp_path, project_dir=tmp_path)
    
    spec = ToolSpec(
        name="test.calculator",
        capability_class="test",
        execution_contexts=("turn",),
        description="A simple calculator",
        input_schema={
            "type": "object",
            "required": ["a", "b", "operation"],
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "number"},
                "operation": {"type": "string"},
                "options": {
                    "type": "object",
                    "properties": {
                        "precision": {"type": "integer"},
                        "verbose": {"type": "boolean"},
                    }
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            }
        },
        output_schema={},
    )
    
    def handler(args):
        return ToolResult(tool="test.calculator", ok=True, facts={"result": 42})
        
    registry.register(spec, handler)
    
    # 1. Valid invocation
    res = registry.call("test.calculator", {
        "a": 5,
        "b": 3.14,
        "operation": "add",
        "options": {"precision": 2, "verbose": True},
        "tags": ["math", "simple"]
    })
    assert res.ok is True
    assert res.facts["result"] == 42
    
    # 2. Missing required fields
    res = registry.call("test.calculator", {
        "a": 5,
        "operation": "add"
    })
    assert res.ok is False
    assert "missing required property: b" in res.error
    
    # 3. Type violations
    res = registry.call("test.calculator", {
        "a": "not_an_int",
        "b": True,
        "operation": 123,
        "options": "should_be_object",
        "tags": ["valid", 123]
    })
    assert res.ok is False
    assert "'a' must be an integer" in res.error
    assert "'b' must be a number" in res.error
    assert "'operation' must be a string" in res.error
    assert "'options' must be an object" in res.error
    assert "'tags[1]' must be a string" in res.error


def test_provider_config_tool_handles_exceptions_robustly(tmp_path):
    (tmp_path / "config.yaml").write_text("invalid: - [unbalanced array", encoding="utf-8")
    from navi.core_tools import _provider_config
    result = _provider_config(tmp_path)

    assert result.ok is False
    assert "Failed to load config" in result.error
    assert len(result.facts["validation_errors"]) > 0
    assert "Failed to load config" in result.facts["validation_errors"][0]
