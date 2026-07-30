"""Configuration and governed tool surfaces for MCP servers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capability_contract import CAPABILITY_ERROR_REASON_KEY, CAPABILITY_RETRYABLE_KEY
from .config import NaviConfig, load_config
from .mcp_client import (
    MCPClient,
    MCPServerConfig,
    MCPTransportError,
)
from .tools import ALL_EXECUTION_CONTEXTS, ToolRegistry, ToolResult, ToolSpec

_ENV_REFERENCE = re.compile(r"^\$\{[A-Z_][A-Z0-9_]*\}$")
_MCP_SERVER_FIELDS = {
    "transport",
    "url",
    "command",
    "args",
    "env",
    "headers",
    "cwd",
    "timeout_seconds",
    "tool_permissions",
    "enabled",
}


@dataclass(frozen=True)
class MCPConfigReport:
    path: Path
    servers: tuple[MCPServerConfig, ...]
    errors: tuple[str, ...]


def load_mcp_config(home: Path) -> MCPConfigReport:
    return parse_mcp_config(load_config(home), path=home / "config.yaml")


def parse_mcp_config(config: NaviConfig, *, path: Path) -> MCPConfigReport:
    raw_servers = config.mcp_servers

    servers: list[MCPServerConfig] = []
    errors: list[str] = []
    namespaces: set[str] = set()
    for name, item in raw_servers.items():
        item_path = f"mcp.servers.{name}"
        raw_name = str(name)
        namespace = _server_namespace(raw_name)
        if not namespace:
            errors.append(f"{item_path} has no usable name")
            continue
        if namespace != raw_name:
            errors.append(
                f"{item_path} name must use only lowercase letters, digits, and underscores"
            )
            continue
        if namespace in namespaces:
            errors.append(f"{item_path} collides with namespace {namespace}")
            continue
        namespaces.add(namespace)
        unknown_fields = sorted(set(item) - _MCP_SERVER_FIELDS)
        if unknown_fields:
            errors.append(f"{item_path} has unsupported fields: {', '.join(unknown_fields)}")
            continue
        url = str(item.get("url") or "").strip()
        transport = str(item.get("transport") or ("streamable_http" if url else "stdio"))
        try:
            server = MCPServerConfig(
                name=namespace,
                transport=transport.replace("-", "_").lower(),
                url=url,
                command=str(item.get("command") or "").strip(),
                args=_string_tuple(item.get("args"), f"{item_path}.args"),
                env=_string_mapping(item.get("env"), f"{item_path}.env"),
                headers=_string_mapping(item.get("headers"), f"{item_path}.headers"),
                cwd=str(item.get("cwd") or "").strip(),
                timeout_seconds=_positive_float(
                    item.get("timeout_seconds", 30.0), f"{item_path}.timeout_seconds"
                ),
                tool_permissions=_permission_mapping(
                    item.get("tool_permissions"), f"{item_path}.tool_permissions"
                ),
                enabled=_boolean(item.get("enabled", True), f"{item_path}.enabled"),
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        validation_errors = server.validate()
        if validation_errors:
            errors.extend(f"{item_path}: {error}" for error in validation_errors)
            continue
        if server.enabled:
            servers.append(server)
    return MCPConfigReport(path=path, servers=tuple(servers), errors=tuple(errors))


def register_mcp_tools(registry: ToolRegistry, *, home: Path) -> None:
    report = load_mcp_config(home)
    if report.errors:
        raise ValueError("invalid MCP configuration: " + "; ".join(report.errors))
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
                    "endpoint": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "object"}},
                    "count": {"type": "integer"},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}},
                    "tool_permissions": {"type": "object"},
                    "annotations_trusted_for_permission": {"type": "boolean"},
                },
            },
            permission=server.transport_permission,
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
                    "tool": {"type": "string", "enum": list(server.allowed_tools)},
                    "arguments": {"type": "object", "default": {}},
                },
                "required": ["tool"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "transport": {"type": "string"},
                    "endpoint": {"type": "string"},
                    "mcp_tool": {"type": "string"},
                    "content": {"type": "array", "items": {"type": "object"}},
                    "structured_content": {"type": "object"},
                    "response": {"type": "object"},
                },
            },
            permission=server.transport_permission,
            permission_policy="argument_map",
            argument_permission_field="tool",
            argument_permissions=tuple(sorted(server.tool_permissions.items())),
            risk_policy="argument_permission",
            mutates=any(
                permission == "write" for permission in server.tool_permissions.values()
            ),
            source=source,
        ),
        call_server_tool,
    )


async def _list_server_tools(server: MCPServerConfig) -> ToolResult:
    try:
        tools = await MCPClient(server).list_tools()
    except MCPTransportError as exc:
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
            "tool_permissions": dict(sorted(server.tool_permissions.items())),
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
    except MCPTransportError as exc:
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


def _mcp_failure(
    server: MCPServerConfig,
    tool_name: str,
    exc: MCPTransportError,
) -> ToolResult:
    info = exc.facts
    reason = (
        "mcp_timeout"
        if info.timed_out
        else "mcp_http_error"
        if info.status_code
        else "mcp_transport_error"
    )
    facts: dict[str, Any] = {
        CAPABILITY_ERROR_REASON_KEY: reason,
        CAPABILITY_RETRYABLE_KEY: info.retryable,
        "server": server.name,
        "transport": server.transport,
        "endpoint": server.safe_endpoint,
        "mcp_tool": tool_name,
        "error_type": type(exc.cause).__name__,
    }
    if info.status_code:
        facts["status_code"] = info.status_code
    return ToolResult(
        tool=f"mcp.{server.name}.call" if tool_name else f"mcp.{server.name}.tools",
        ok=False,
        error=info.message,
        facts=facts,
    )


def _server_namespace(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_")


def _string_mapping(value: Any, path: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping of strings")
    result: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(raw, str):
            raise ValueError(f"{path}.{key} must be a string")
        text = raw.strip()
        if _ENV_REFERENCE.fullmatch(text):
            raise ValueError(
                f"{path}.{key} must contain its value directly; environment references are unsupported"
            )
        if text:
            result[str(key)] = text
    return result


def _permission_mapping(value: Any, path: str) -> dict[str, str]:
    return {
        tool: permission.strip().lower()
        for tool, permission in _string_mapping(value, path).items()
    }


def _positive_float(value: Any, path: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{path} must be a number") from None
    if parsed <= 0:
        raise ValueError(f"{path} must be greater than zero")
    return parsed


def _string_tuple(value: Any, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{path} must be a list of strings")
    return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    return value
