"""Configuration and governed tool surfaces for MCP servers."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capability_contract import CAPABILITY_ERROR_REASON_KEY, CAPABILITY_RETRYABLE_KEY
from .config import load_runtime_env
from .mcp_client import (
    DEFAULT_EXA_MCP_URL,
    MCPClient,
    MCPServerConfig,
    describe_mcp_exception,
)
from .tools import ALL_EXECUTION_CONTEXTS, ToolRegistry, ToolResult, ToolSpec

logger = logging.getLogger(__name__)
_ENV_REFERENCE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")


@dataclass(frozen=True)
class MCPConfigReport:
    path: Path
    servers: tuple[MCPServerConfig, ...]
    errors: tuple[str, ...]


def load_mcp_config(home: Path) -> MCPConfigReport:
    path = home / "mcp.json"
    env = load_runtime_env(home)
    config_errors: list[str] = []
    if not path.exists():
        raw_servers: dict[str, Any] = {}
    else:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raw = {}
            config_errors = [str(exc)]
        raw_servers_value = raw.get("mcpServers") if isinstance(raw, dict) else None
        if not isinstance(raw_servers_value, dict):
            raw_servers = {}
            config_errors = ["mcp.json must contain an mcpServers object"]
        else:
            raw_servers = raw_servers_value

    servers: list[MCPServerConfig] = []
    errors: list[str] = config_errors
    namespaces: set[str] = set()
    for name, item in raw_servers.items():
        if not isinstance(item, dict):
            errors.append(f"mcpServers.{name} must be an object")
            continue
        namespace = _server_namespace(str(name))
        if not namespace:
            errors.append(f"mcpServers.{name} has no usable name")
            continue
        if namespace in namespaces:
            errors.append(f"mcpServers.{name} collides with namespace {namespace}")
            continue
        namespaces.add(namespace)
        url = str(item.get("url") or item.get("serverUrl") or "").strip()
        transport = str(item.get("transport") or ("streamable_http" if url else "stdio"))
        server = MCPServerConfig(
            name=namespace,
            transport=transport.replace("-", "_").lower(),
            url=url,
            command=str(item.get("command") or "").strip(),
            args=tuple(str(value) for value in item.get("args") or ()),
            env=_string_mapping(item.get("env"), env=env),
            headers=_string_mapping(item.get("headers"), env=env),
            cwd=str(item.get("cwd") or "").strip(),
            timeout_seconds=_positive_float(item.get("timeout_seconds"), default=30.0),
            permission=str(item.get("permission") or "write").strip().lower(),
            allowed_tools=_string_tuple(item.get("allowed_tools")),
            enabled=item.get("enabled") is not False,
        )
        validation_errors = server.validate()
        if validation_errors:
            errors.extend(f"mcpServers.{name}: {error}" for error in validation_errors)
            continue
        if server.enabled:
            servers.append(server)
    if "exa" not in namespaces:
        api_key = str(env.get("NAVI_EXA_API_KEY") or env.get("EXA_API_KEY") or "")
        servers.insert(
            0,
            MCPServerConfig(
                name="exa",
                transport="streamable_http",
                url=DEFAULT_EXA_MCP_URL,
                headers={"x-api-key": api_key} if api_key else {},
                permission="network",
                allowed_tools=("web_search_exa", "web_fetch_exa"),
            ),
        )
    return MCPConfigReport(path=path, servers=tuple(servers), errors=tuple(errors))


def register_mcp_tools(registry: ToolRegistry, *, home: Path) -> None:
    report = load_mcp_config(home)
    for error in report.errors:
        logger.warning("invalid MCP configuration: %s", error)
    for server in report.servers:
        _register_server_tools(registry, server)


def _register_server_tools(registry: ToolRegistry, server: MCPServerConfig) -> None:
    source = f"mcp:{server.name}"

    async def list_server_tools(_args: dict[str, Any]) -> ToolResult:
        return await _list_server_tools(server)

    async def call_server_tool(args: dict[str, Any]) -> ToolResult:
        return await _call_server_tool(server, args)

    registry.register(
        ToolSpec(
            name=f"mcp.{server.name}.tools",
            capability_class="mcp",
            execution_contexts=ALL_EXECUTION_CONTEXTS,
            description=f"Discover tools exposed by configured MCP server {server.name}.",
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "transport": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "object"}},
                    "count": {"type": "integer"},
                },
            },
            permission="network",
            source=source,
        ),
        list_server_tools,
    )
    registry.register(
        ToolSpec(
            name=f"mcp.{server.name}.call",
            capability_class="mcp",
            execution_contexts=ALL_EXECUTION_CONTEXTS,
            description=(
                f"Call one tool on configured MCP server {server.name}. "
                f"Current input schemas are returned by mcp.{server.name}.tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "arguments": {"type": "object", "default": {}},
                },
                "required": ["tool"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "tool": {"type": "string"},
                    "content": {"type": "array", "items": {"type": "object"}},
                    "structured_content": {"type": "object"},
                    "response": {"type": "object"},
                },
            },
            permission=server.permission,
            source=source,
        ),
        call_server_tool,
    )


async def _list_server_tools(server: MCPServerConfig) -> ToolResult:
    try:
        tools = await MCPClient(server).list_tools()
    except Exception as exc:
        return _mcp_failure(server, "", exc)
    if server.allowed_tools:
        tools = [tool for tool in tools if tool["name"] in server.allowed_tools]
    return ToolResult(
        tool=f"mcp.{server.name}.tools",
        ok=True,
        facts={
            "server": server.name,
            "transport": server.transport,
            "endpoint": server.safe_endpoint,
            "tools": tools,
            "count": len(tools),
            "allowed_tools": list(server.allowed_tools),
            "annotations_trusted_for_permission": False,
        },
    )


async def _call_server_tool(server: MCPServerConfig, args: dict[str, Any]) -> ToolResult:
    tool_name = str(args.get("tool") or "").strip()
    if not tool_name:
        return ToolResult(
            tool=f"mcp.{server.name}.call",
            ok=False,
            error="tool is required",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "missing_required_argument",
                CAPABILITY_RETRYABLE_KEY: False,
                "server": server.name,
            },
        )
    if server.allowed_tools and tool_name not in server.allowed_tools:
        return ToolResult(
            tool=f"mcp.{server.name}.call",
            ok=False,
            error=f"MCP tool is not allowed by local configuration: {tool_name}",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "mcp_tool_not_allowed",
                CAPABILITY_RETRYABLE_KEY: False,
                "server": server.name,
                "mcp_tool": tool_name,
                "allowed_tools": list(server.allowed_tools),
            },
        )
    arguments = args.get("arguments")
    if not isinstance(arguments, dict):
        return ToolResult(
            tool=f"mcp.{server.name}.call",
            ok=False,
            error="arguments must be an object",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "invalid_arguments",
                CAPABILITY_RETRYABLE_KEY: False,
                "server": server.name,
                "mcp_tool": tool_name,
            },
        )
    try:
        result = await MCPClient(server).call_tool(tool_name, arguments)
    except Exception as exc:
        return _mcp_failure(server, tool_name, exc)
    facts = {
        "server": server.name,
        "transport": server.transport,
        "endpoint": server.safe_endpoint,
        "mcp_tool": tool_name,
        "content": result["content"],
        "structured_content": result["structured_content"],
        "response": {
            "text_length": len(result["text"]),
            "truncated": result["truncated"],
        },
    }
    if not result["ok"]:
        facts[CAPABILITY_ERROR_REASON_KEY] = "mcp_tool_error"
        facts[CAPABILITY_RETRYABLE_KEY] = False
    return ToolResult(
        tool=f"mcp.{server.name}.call",
        ok=bool(result["ok"]),
        error="MCP server reported a tool error" if not result["ok"] else "",
        facts=facts,
    )


def _mcp_failure(server: MCPServerConfig, tool_name: str, exc: Exception) -> ToolResult:
    message, timed_out = describe_mcp_exception(exc)
    return ToolResult(
        tool=f"mcp.{server.name}.call" if tool_name else f"mcp.{server.name}.tools",
        ok=False,
        error=message,
        facts={
            CAPABILITY_ERROR_REASON_KEY: "mcp_timeout" if timed_out else "mcp_transport_error",
            CAPABILITY_RETRYABLE_KEY: timed_out,
            "server": server.name,
            "transport": server.transport,
            "endpoint": server.safe_endpoint,
            "mcp_tool": tool_name,
            "error_type": type(exc).__name__,
        },
    )


def _server_namespace(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_")


def _string_mapping(value: Any, *, env: dict[str, str] | None = None) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, raw in value.items():
        text = str(raw)
        if env is not None:
            match = _ENV_REFERENCE.fullmatch(text.strip())
            if match:
                text = env.get(match.group(1), "")
        if text:
            result[str(key)] = text
    return result


def _positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
