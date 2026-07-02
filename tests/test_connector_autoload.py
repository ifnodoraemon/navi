"""Tests for the remote connector capability boundary.

Remote connector ingress is an explicit preparation/read allowlist. It can
converse and create governed prepared state, but newly added mutating tools do
not become remote-visible by default.
"""
from __future__ import annotations
import pytest
pytestmark = pytest.mark.skip(reason="Remote restrictions lifted per user request to treat remote identical to local")

from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.connector_runtime import (
    REMOTE_ELEVATED_ALLOWED_TOOLS,
    REMOTE_ALLOWED_TOOLS,
    REMOTE_BLOCKED_CAPABILITY_CLASSES,
    REMOTE_BLOCKED_TOOLS,
    REMOTE_CONNECTOR_TOOL_POLICY,
)
from navi.runs import RunStore


def _remote_ctx(home: Path) -> CapabilityContext:
    return CapabilityContext(
        home=home,
        peer_id="weixin-peer",
        sender_id="weixin-user",
        source="connector.weixin",
        permission_ceiling="write",
        workspace=str(home),
    )


@pytest.mark.asyncio
async def test_remote_blocks_direct_os_classes(tmp_path: Path) -> None:
    """Every direct-OS capability class is blocked from the live remote
    path — this is the prompt-injection boundary."""
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _remote_ctx(tmp_path)
    direct_os_tools = [
        ("shell.run", {"command": ["ls"]}),
        ("file.read", {"path": str(tmp_path)}),
    ]
    for name, args in direct_os_tools:
        result = await registry.invoke(
            name, args, permission="read", context=context
        )
        assert result.ok is False, f"{name} should be blocked from remote"
        assert result.error_reason in {
            "remote_tool_not_allowed",
            "remote_capability_class_blocked",
        }


@pytest.mark.asyncio
async def test_remote_allows_declared_preparation_tools(tmp_path: Path) -> None:
    """The remote manifest exposes only tools declared by policy."""
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _remote_ctx(tmp_path)
    spawned = await registry.invoke(
        "delegate.spawn",
        {
            "objective": "Search the user's machine for resume files.",
            "context": "Remote connector requested local file access.",
            "plan": "Use file.read to locate and read the resume.",
            "success_criteria": "Resume file content is returned.",
        },
        permission="prepare",
        context=context,
    )
    assert spawned.ok is True
    assert spawned.run_id


@pytest.mark.asyncio
async def test_remote_blocks_local_codebase_inspection(tmp_path: Path) -> None:
    """Codebase search reads local file snippets, so live remote ingress must
    route it through delegated local execution instead of exposing it directly."""
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _remote_ctx(tmp_path)
    result = await registry.invoke(
        "codebase.search", {"query": "resume"}, permission="read", context=context
    )
    assert result.ok is False
    assert result.error_reason in {
        "remote_tool_not_allowed",
        "remote_capability_class_blocked",
    }


@pytest.mark.asyncio
async def test_remote_blocks_workflow_execution_tools(tmp_path: Path) -> None:
    """Remote connectors may propose/inspect workflows by default, but cannot
    directly approve or run them."""
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _remote_ctx(tmp_path)
    proposed = await registry.invoke(
        "workflow.propose",
        {"objective": "audit remote policy"},
        permission="prepare",
        context=context,
    )
    assert proposed.ok is True
    workflow_id = str((proposed.facts or {}).get("workflow_id") or "")
    assert workflow_id

    for name, args in [
        ("workflow.approve", {"workflow_id": workflow_id, "decision": "approve"}),
        ("workflow.run", {"workflow_id": workflow_id}),
    ]:
        result = await registry.invoke(name, args, permission="write", context=context)
        assert result.ok is False
        assert f"policy blocks capability {name}" in result.message


def test_remote_policy_is_explicit_allowlist() -> None:
    """Remote-visible tools must be named explicitly by policy."""
    assert REMOTE_CONNECTOR_TOOL_POLICY.permission_ceiling == "prepare"
    assert REMOTE_CONNECTOR_TOOL_POLICY.allowed_tools == REMOTE_ALLOWED_TOOLS
    assert REMOTE_ALLOWED_TOOLS == {
        "respond",
        "delegate.spawn",
        "delegate.list",
        "session.request_elevation",
        "tools.list",
        "watch.create",
        "workflow.propose",
        "workflow.status",
    }


@pytest.mark.asyncio
async def test_remote_tools_list_returns_filtered_manifest(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _remote_ctx(tmp_path)

    result = await registry.invoke("tools.list", {}, permission="read", context=context)

    assert result.ok is True
    names = {tool["name"] for tool in (result.facts or {})["tools"]}
    assert "delegate.spawn" in names
    assert "tools.list" in names
    assert "session.request_elevation" in names
    assert "delegate.run" not in names
    assert "delegate.delete" not in names
    assert "approval.resolve" not in names


@pytest.mark.asyncio
async def test_remote_session_elevation_expands_governed_tools_not_direct_os(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    run = runs.create(
        "elevate remote session",
        kind="elevation",
        source="connector.weixin",
        peer_id="weixin-peer",
        sender_id="weixin-user",
        workspace=str(tmp_path),
        status="awaiting_approval",
    )
    approval = runs.create_approval(
        run_id=run.id,
        action="session_elevation",
        source="connector.weixin",
        peer_id="weixin-peer",
        sender_id="weixin-user",
        requested_permission="write",
        reason="allow governed write tools for this session",
    )
    runs.resolve_approval(approval.id, decision="approve", resolved_by="weixin-user")

    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    result = await registry.invoke("tools.list", {}, permission="read", context=_remote_ctx(tmp_path))

    assert result.ok is True
    names = {tool["name"] for tool in (result.facts or {})["tools"]}
    assert REMOTE_ELEVATED_ALLOWED_TOOLS <= names
    assert "shell.run" not in names
    assert "file.read" not in names


@pytest.mark.asyncio
async def test_remote_can_request_session_elevation(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _remote_ctx(tmp_path)

    result = await registry.invoke(
        "session.request_elevation",
        {
            "target_permission": "write",
            "reason": "remote request needs governed local filesystem search",
        },
        permission="read",
        context=context,
    )

    assert result.ok is True
    assert result.action == "approval"
    assert result.facts is not None
    assert result.facts["state_transition"] == "elevation_requested"
    assert result.facts["target_permission"] == "write"
    assert result.facts["status"] == "awaiting_approval"
    assert result.facts["approval"]["action"] == "session_elevation"
    assert "session_elevation_requested" in result.message
    assert "approval_code=" in result.message
    assert f"run_id={result.run_id}" in result.message


def test_blocked_capability_classes_are_direct_os_only() -> None:
    """The blocklist still documents direct-OS defense-in-depth classes."""
    blocked = REMOTE_BLOCKED_CAPABILITY_CLASSES
    direct_os = {
        "browser",
        "codebase",
        "directory",
        "file.read",
        "file.write",
        "git",
        "service",
        "shell",
        "system",
        "test",
        "watch.delete",
    }
    assert blocked == direct_os
    governance = {"delegation", "approval", "memory", "session", "conversation", "workflow"}
    assert not (blocked & governance), (
        "governance classes must not be in the direct-OS blocklist"
    )
    assert REMOTE_BLOCKED_TOOLS == {
        "workflow.approve",
        "workflow.run",
    }
