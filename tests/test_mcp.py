from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from navi.mcp_tools import load_mcp_config
from navi.mcp_client import MCPClient, MCPServerConfig
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.tools import build_tool_gateway


def _write_mcp_config(home: Path, servers: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"mcp": {"servers": servers}}, sort_keys=False),
        encoding="utf-8",
    )


def test_mcp_config_supports_remote_and_stdio_from_global_config(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_mcp_config(
        home,
        {
            "exa_search": {
                "url": "https://mcp.example.test/mcp?tools=search",
                "headers": {"authorization": "secret-value"},
                "tool_permissions": {"search": "network"},
            },
            "local_files": {
                "command": "example-mcp-server",
                "args": ["--stdio"],
                "tool_permissions": {"read_file": "write"},
            },
            "bad": {"url": "https://user:password@example.test/mcp"},
        },
    )
    report = load_mcp_config(home)

    assert [server.name for server in report.servers] == ["exa_search", "local_files"]
    assert report.servers[0].headers == {"authorization": "secret-value"}
    assert report.servers[0].safe_endpoint == "https://mcp.example.test/mcp"
    assert report.servers[0].permission_for("search") == "network"
    assert report.servers[1].transport == "stdio"
    assert report.servers[1].permission_for("read_file") == "write"
    assert report.errors == (
        "mcp.servers.bad: MCP url must not contain credentials",
        "mcp.servers.bad: tool_permissions must explicitly allow at least one tool",
    )


def test_mcp_config_rejects_environment_references(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_mcp_config(
        home,
        {
            "remote": {
                "url": "https://mcp.example.test/mcp",
                "headers": {"authorization": "${TEST_MCP_TOKEN}"},
            }
        },
    )

    report = load_mcp_config(home)

    assert report.servers == ()
    assert "environment references are unsupported" in report.errors[0]


@pytest.mark.asyncio
async def test_mcp_stdio_transport_discovers_and_calls_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAVI_TEST_SECRET", "must-not-reach-mcp")
    server_script = tmp_path / "stdio_mcp.py"
    server_script.write_text(
        """
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        result = {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [{
                "name": "echo",
                "description": "Echo text",
                "inputSchema": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            }]
        }
    elif method == "tools/call":
        value = message["params"]["arguments"]["message"]
        result = {
            "content": [{"type": "text", "text": value}],
            "structuredContent": {
                "echo": value,
                "host_secret": __import__("os").environ.get("NAVI_TEST_SECRET", ""),
                "configured": __import__("os").environ.get("MCP_VISIBLE", ""),
            },
            "isError": False,
        }
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    client = MCPClient(
        MCPServerConfig(
            name="stdio_fixture",
            transport="stdio",
            command=sys.executable,
            args=(str(server_script),),
            env={"MCP_VISIBLE": "yes"},
            timeout_seconds=5,
            tool_permissions={"echo": "write"},
        )
    )

    tools = await client.list_tools()
    called = await client.call_tool("echo", {"message": "stdio-ok"})

    assert tools[0]["name"] == "echo"
    assert called["ok"] is True
    assert called["structured_content"] == {
        "echo": "stdio-ok",
        "host_secret": "",
        "configured": "yes",
    }


@pytest.mark.asyncio
async def test_mcp_server_is_discovered_and_called_through_gateway(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_mcp_config(
        home,
        {
            "echo": {
                "url": "https://mcp.example.test/mcp",
                "tool_permissions": {"echo": "network"},
                "timeout_seconds": 10,
            }
        },
    )

    async def fake_list(self):
        return [
            {
                "name": "echo",
                "title": "",
                "description": "Echo text.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "annotations": {"destructiveHint": True},
            }
        ]

    async def fake_call(self, name, arguments):
        assert name == "echo"
        assert arguments == {"message": "hello"}
        return {
            "ok": True,
            "is_error": False,
            "content": [{"type": "text", "text": '{"echo":"hello"}'}],
            "structured_content": {"result": {"echo": "hello"}},
            "text": '{"echo":"hello"}',
            "truncated": False,
        }

    monkeypatch.setattr(MCPClient, "list_tools", fake_list)
    monkeypatch.setattr(MCPClient, "call_tool", fake_call)
    gateway = build_tool_gateway(home, project_dir=workspace)

    specs = {spec.name: spec for spec in gateway.list_specs()}
    listed = await gateway.call("mcp.echo.tools", {})
    called = await gateway.call(
        "mcp.echo.call",
        {"tool": "echo", "arguments": {"message": "hello"}},
    )

    assert specs["mcp.echo.call"].permission == "network"
    assert specs["mcp.echo.call"].source == "mcp:echo"
    assert listed.ok is True
    assert listed.facts["tools"][0]["name"] == "echo"
    assert listed.facts["annotations_trusted_for_permission"] is False
    assert called.ok is True
    assert called.facts["structured_content"] == {"result": {"echo": "hello"}}
    assert called.facts["content"][0]["text"] == '{"echo":"hello"}'
    assert called.facts["response"] == {"text_length": 16, "truncated": False}

    blocked = await gateway.call(
        "mcp.echo.call",
        {"tool": "future_destructive_tool", "arguments": {}},
    )
    assert blocked.ok is False
    assert blocked.facts["error_reason"] == "invalid_arguments"
    assert blocked.facts["retryable"] is False


@pytest.mark.asyncio
async def test_mcp_call_permission_is_bound_to_selected_local_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_mcp_config(
        home,
        {
            "mixed": {
                "url": "https://mcp.example.test/mcp",
                "tool_permissions": {
                    "lookup": "network",
                    "publish": "write",
                },
            }
        },
    )

    async def fake_call(self, name, arguments):
        return {
            "ok": True,
            "is_error": False,
            "content": [],
            "structured_content": {"tool": name},
            "text": "",
            "truncated": False,
        }

    monkeypatch.setattr(MCPClient, "call_tool", fake_call)
    registry = build_capability_registry(home, project_dir=workspace)
    context = CapabilityContext(
        home=home,
        source="local",
        peer_id="cli",
        sender_id="tester",
        workspace=str(workspace),
        permission_ceiling="write",
    )

    lookup = await registry.invoke(
        "mcp.mixed.call",
        {"tool": "lookup", "arguments": {}},
        permission="network",
        context=context,
    )
    publish = await registry.invoke(
        "mcp.mixed.call",
        {"tool": "publish", "arguments": {}},
        permission="network",
        context=context,
    )

    assert lookup.ok is True
    assert publish.ok is False
    assert publish.yields_control is True
    assert publish.facts["requested_permission"] == "write"
    assert publish.facts["risk"]["evidence"]["argument_value"] == "publish"
