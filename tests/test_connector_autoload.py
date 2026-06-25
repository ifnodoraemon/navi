"""Tests for the auto-loaded remote connector policy.

The remote connector no longer uses a hand-maintained per-tool allowlist
(``REMOTE_SAFE_TOOLS``). Instead, the policy is a stable *blocklist* of
direct-OS capability classes. New governance / read tools auto-load into
the remote manifest without a central list edit; only direct-OS classes
(file, shell, browser) are blocked from the live remote path, since they
would let a prompt-injected message run shell or read local files without
the delegate.spawn → approval gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.connector_runtime import REMOTE_BLOCKED_CAPABILITY_CLASSES, REMOTE_BLOCKED_TOOLS


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
        assert "policy blocks capability class" in result.message, (
            f"{name} blocked message should name the class"
        )


@pytest.mark.asyncio
async def test_remote_autoloads_governance_tools(tmp_path: Path) -> None:
    """Governance tools (delegate.spawn) auto-load into the remote manifest
    without being hand-listed. A newly declared governance tool would
    similarly auto-appear."""
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
    assert "policy blocks capability class codebase" in result.message


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


def test_blocked_capability_classes_are_direct_os_only() -> None:
    """The blocklist contains only direct-OS classes — the stable
    prompt-injection boundary. It must not contain governance classes
    (delegation, approval, workflow, memory, session, etc.)."""
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
    governance = {"delegation", "approval", "memory", "session", "conversation"}
    assert not (blocked & governance), (
        "governance classes must not be in the direct-OS blocklist"
    )
    assert REMOTE_BLOCKED_TOOLS == {
        "workflow.approve",
        "workflow.run",
    }
