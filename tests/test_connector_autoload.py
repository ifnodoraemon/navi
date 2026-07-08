"""Tests for the remote connector capability boundary.

Remote connector ingress is an explicit preparation/read allowlist. It can
converse and create governed prepared state, but newly added mutating tools do
not become remote-visible by default.
"""
from __future__ import annotations

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
from navi.lifecycle import Governance, Phase, Resolution
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
        "goal.open",
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
async def test_removed_workflow_tools_are_not_available(tmp_path: Path) -> None:
    """Workflow capabilities were removed; remote ingress must not rediscover
    them through either tools.list or direct invocation."""
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _remote_ctx(tmp_path)
    manifest = await registry.invoke("tools.list", {}, permission="read", context=context)
    assert manifest.ok is True
    names = {tool["name"] for tool in (manifest.facts or {})["tools"]}
    assert not {name for name in names if name.startswith("workflow.")}

    for name in ("workflow.propose", "workflow.state", "workflow.approve", "workflow.run"):
        result = await registry.invoke(name, {}, permission="prepare", context=context)
        assert result.ok is False
        assert result.error_reason == "not_found"


def test_remote_policy_is_explicit_allowlist() -> None:
    """Remote-visible tools must be named explicitly by policy."""
    assert REMOTE_CONNECTOR_TOOL_POLICY.permission_ceiling == "prepare"
    assert REMOTE_CONNECTOR_TOOL_POLICY.allowed_tools == REMOTE_ALLOWED_TOOLS
    assert REMOTE_ALLOWED_TOOLS == {
        "respond",
        "approval.resolve",
        "goal.open",
        "goal.state",
        "session.request_elevation",
        "tools.list",
    }


@pytest.mark.asyncio
async def test_remote_tools_list_returns_filtered_manifest(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _remote_ctx(tmp_path)

    result = await registry.invoke("tools.list", {}, permission="read", context=context)

    assert result.ok is True
    names = {tool["name"] for tool in (result.facts or {})["tools"]}
    assert "goal.open" in names
    assert "tools.list" in names
    assert "approval.resolve" in names
    assert "goal.state" in names
    assert "session.request_elevation" in names


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
        phase=Phase.PAUSED,
        governance=Governance.AWAITING_APPROVAL,
        resolution=Resolution.BLOCKED,
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

    assert result.ok is False
    assert result.action == "approval"
    assert result.yields_control is True
    assert result.error_reason == "session_elevation_requested"
    assert result.message == ""
    assert result.facts is not None
    assert result.facts["state_transition"] == "elevation_requested"
    assert result.facts["target_permission"] == "write"
    assert result.facts["phase"] == Phase.PAUSED
    assert result.facts["governance"] == Governance.AWAITING_APPROVAL
    assert result.facts["resolution"] == Resolution.BLOCKED
    assert result.facts["approval"]["action"] == "session_elevation"
    assert result.facts["run_id"] == result.run_id


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
    }
    assert blocked == direct_os
    governance = {"delegation", "approval", "memory", "session", "conversation"}
    assert not (blocked & governance), (
        "governance classes must not be in the direct-OS blocklist"
    )
    assert REMOTE_BLOCKED_TOOLS == frozenset()
