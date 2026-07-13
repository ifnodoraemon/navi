from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from navi.mcp_tools import load_mcp_config
from navi.mcp_client import MCPClient, MCPServerConfig
from navi.tools import build_tool_gateway


def _write_mcp_config(home: Path, servers: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(
        json.dumps({"mcpServers": servers}),
        encoding="utf-8",
    )


def test_mcp_config_supports_remote_and_stdio_without_embedded_credentials(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_mcp_config(
        home,
        {
            "exa-search": {
                "url": "https://mcp.example.test/mcp?tools=search",
                "headers": {"authorization": "${TEST_MCP_TOKEN}"},
                "permission": "network",
                "allowed_tools": ["search"],
            },
            "local-files": {
                "command": "example-mcp-server",
                "args": ["--stdio"],
            },
            "bad": {"url": "https://user:password@example.test/mcp"},
        },
    )
    (home / "env").write_text("TEST_MCP_TOKEN=secret-value\n", encoding="utf-8")

    report = load_mcp_config(home)

    assert [server.name for server in report.servers] == ["exa", "exa_search", "local_files"]
    assert report.servers[1].headers == {"authorization": "secret-value"}
    assert report.servers[1].safe_endpoint == "https://mcp.example.test/mcp"
    assert report.servers[1].permission == "network"
    assert report.servers[2].transport == "stdio"
    assert report.servers[2].permission == "write"
    assert report.errors == ("mcpServers.bad: MCP url must not contain credentials",)


@pytest.mark.asyncio
async def test_mcp_stdio_transport_discovers_and_calls_tool(tmp_path: Path) -> None:
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
            "structuredContent": {"echo": value},
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
            timeout_seconds=5,
            allowed_tools=("echo",),
        )
    )

    tools = await client.list_tools()
    called = await client.call_tool("echo", {"message": "stdio-ok"})

    assert tools[0]["name"] == "echo"
    assert called["ok"] is True
    assert called["structured_content"] == {"echo": "stdio-ok"}


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
                "permission": "network",
                "allowed_tools": ["echo"],
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
    assert blocked.facts["error_reason"] == "mcp_tool_not_allowed"
    assert blocked.facts["retryable"] is False
