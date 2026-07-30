"""Bounded MCP client transport and result normalization."""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse, urlunparse

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .permission_contract import normalize_permission


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: str
    url: str = ""
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    timeout_seconds: float = 30.0
    tool_permissions: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    @property
    def transport_permission(self) -> str:
        return "network" if self.transport == "streamable_http" else "write"

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self.tool_permissions))

    def permission_for(self, tool: str) -> str:
        return normalize_permission(self.tool_permissions[tool])

    @property
    def safe_endpoint(self) -> str:
        if self.url:
            parsed = urlparse(self.url)
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        return self.command

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.name.strip():
            errors.append("server name is required")
        if self.transport == "streamable_http":
            parsed = urlparse(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append("streamable_http transport requires an http(s) url")
            if parsed.username or parsed.password:
                errors.append("MCP url must not contain credentials")
        elif self.transport == "stdio":
            if not self.command.strip():
                errors.append("stdio transport requires command")
        else:
            errors.append(f"unsupported transport: {self.transport}")
        if not self.tool_permissions:
            errors.append("tool_permissions must explicitly allow at least one tool")
        for tool, raw_permission in self.tool_permissions.items():
            if not str(tool).strip():
                errors.append("tool_permissions contains an empty tool name")
                continue
            try:
                permission = normalize_permission(raw_permission)
            except ValueError as exc:
                errors.append(f"tool_permissions.{tool}: {exc}")
                continue
            if self.transport == "streamable_http" and permission == "read":
                errors.append(
                    f"tool_permissions.{tool} cannot be read over a network transport"
                )
            if self.transport == "stdio" and permission != "write":
                errors.append(
                    f"tool_permissions.{tool} must be write for a local stdio process"
                )
        if self.timeout_seconds <= 0:
            errors.append("timeout_seconds must be positive")
        return tuple(errors)


@dataclass(frozen=True)
class MCPExceptionFacts:
    message: str
    timed_out: bool
    retryable: bool
    status_code: int = 0


class MCPTransportError(RuntimeError):
    """Typed boundary for failures raised while talking to an MCP server."""

    def __init__(self, cause: Exception):
        self.cause = cause
        self.facts = mcp_exception_facts(cause)
        super().__init__(self.facts.message)


class MCPClient:
    """Open one MCP session per bounded discovery or tool call."""

    def __init__(self, server: MCPServerConfig):
        errors = server.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self.server = server

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        read_timeout = timedelta(seconds=self.server.timeout_seconds)
        if self.server.transport == "streamable_http":
            timeout = httpx.Timeout(self.server.timeout_seconds)
            async with httpx.AsyncClient(
                headers=self.server.headers,
                timeout=timeout,
                follow_redirects=True,
            ) as http_client:
                async with streamable_http_client(
                    self.server.url,
                    http_client=http_client,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=read_timeout,
                    ) as session:
                        await session.initialize()
                        yield session
            return

        parameters = StdioServerParameters(
            command=self.server.command,
            args=list(self.server.args),
            env=_stdio_environment(self.server.env),
            cwd=Path(self.server.cwd).expanduser() if self.server.cwd else None,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=read_timeout,
            ) as session:
                await session.initialize()
                yield session

    async def list_tools(self) -> list[dict[str, Any]]:
        try:
            async with self._session() as session:
                result = await session.list_tools()
        except Exception as exc:
            raise MCPTransportError(exc) from exc
        return [
            {
                "name": tool.name,
                "title": tool.title or "",
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
                "output_schema": tool.outputSchema or {},
                "annotations": (
                    tool.annotations.model_dump(mode="json", by_alias=True)
                    if tool.annotations is not None
                    else {}
                ),
            }
            for tool in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._session() as session:
                result = await session.call_tool(name, arguments=arguments)
        except Exception as exc:
            raise MCPTransportError(exc) from exc
        content = [_bounded_content(block.model_dump(mode="json", by_alias=True)) for block in result.content]
        text = "\n".join(
            str(block.get("text") or "") for block in content if block.get("type") == "text"
        ).strip()
        return {
            "ok": not result.isError,
            "is_error": bool(result.isError),
            "content": content,
            "structured_content": _bounded_json(result.structuredContent),
            "text": text[:100_000],
            "truncated": len(text) > 100_000,
        }


def _stdio_environment(configured: dict[str, str]) -> dict[str, str]:
    """Build a minimal stdio-server environment without host credentials."""

    inherited = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TERM", "SYSTEMROOT")
        if os.environ.get(key)
    }
    return {
        "HOME": "/tmp/navi-mcp-home",
        "TMPDIR": "/tmp",
        **inherited,
        **configured,
    }


def mcp_exception_facts(exc: BaseException) -> MCPExceptionFacts:
    """Flatten typed SDK exception groups into bounded transport facts."""
    messages: list[str] = []
    seen: set[int] = set()
    timed_out = False
    status_codes: list[int] = []

    def visit(current: BaseException) -> None:
        nonlocal timed_out
        if id(current) in seen:
            return
        seen.add(id(current))
        if isinstance(current, (TimeoutError, httpx.TimeoutException)):
            timed_out = True
        if isinstance(current, httpx.HTTPStatusError):
            status_codes.append(int(current.response.status_code))
        message = str(current).strip()
        if message and message not in messages:
            messages.append(message)
        for nested in getattr(current, "exceptions", ()):
            if isinstance(nested, BaseException):
                visit(nested)
        if isinstance(current.__cause__, BaseException):
            visit(current.__cause__)
        if isinstance(current.__context__, BaseException):
            visit(current.__context__)

    visit(exc)
    detail = "; ".join(messages) or type(exc).__name__
    status_code = status_codes[0] if status_codes else 0
    retryable = timed_out or status_code in {408, 409, 425, 429} or status_code >= 500
    return MCPExceptionFacts(
        message=detail[:4000],
        timed_out=timed_out,
        retryable=retryable,
        status_code=status_code,
    )


def _bounded_content(block: dict[str, Any]) -> dict[str, Any]:
    bounded = dict(block)
    data = bounded.get("data")
    if isinstance(data, str):
        bounded["data_size"] = len(data)
        bounded["data_omitted"] = True
        bounded.pop("data", None)
    text = bounded.get("text")
    if isinstance(text, str) and len(text) > 100_000:
        bounded["text"] = text[:100_000]
        bounded["truncated"] = True
    return bounded


def _bounded_json(value: Any) -> Any:
    if value is None:
        return {}
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    if len(serialized) <= 100_000:
        return value
    return {"truncated": True, "json": serialized[:100_000]}
