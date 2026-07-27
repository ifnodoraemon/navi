"""Connector capability visibility and approval-boundary tests.

Connector ingress uses the same capability catalog as local CLI ingress. Source
identity scopes durable state and approvals, but does not create a second tool
allowlist. Sensitive effects are stopped by the shared capability approval gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.connector_runtime import ConnectorIngressRuntime
from navi.lifecycle import Governance, Phase
from navi.runs import RunStore
from navi.runtime import AgentRuntime


def _context(home: Path, *, source: str, permission_ceiling: str = "write") -> CapabilityContext:
    return CapabilityContext(
        home=home,
        peer_id=f"{source}-peer",
        sender_id=f"{source}-user",
        source=source,
        permission_ceiling=permission_ceiling,
        workspace=str(home),
    )


@pytest.mark.asyncio
async def test_connector_manifest_matches_local_manifest(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    assert registry.get("tools.list").context_policy == "capability_catalog"

    connector_result = await registry.invoke(
        "tools.list",
        {},
        permission="read",
        context=_context(tmp_path, source="connector.weixin"),
    )
    local_result = await registry.invoke(
        "tools.list",
        {},
        permission="read",
        context=_context(tmp_path, source="cli"),
    )

    assert connector_result.ok is True
    assert local_result.ok is True
    connector_names = {tool["name"] for tool in (connector_result.facts or {})["tools"]}
    local_names = {tool["name"] for tool in (local_result.facts or {})["tools"]}
    assert connector_names == local_names
    assert {"file.read", "shell.run", "web.search"} <= connector_names
    assert {
        "directory.list",
        "git.status",
        "service.status",
        "system.info",
        "test.run",
    }.isdisjoint(connector_names)


@pytest.mark.asyncio
async def test_tools_list_exposes_the_complete_registry_catalog(tmp_path: Path) -> None:
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        allowed_tools={"respond", "tools.list"},
    )

    result = await registry.invoke(
        "tools.list",
        {},
        permission="read",
        context=_context(tmp_path, source="cli"),
    )

    assert result.ok is True
    names = {tool["name"] for tool in result.facts["tools"]}
    assert {"respond", "tools.list", "shell.run", "file.write"} <= names


@pytest.mark.asyncio
async def test_connector_can_use_shared_nonsensitive_shell_capability(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    result = await registry.invoke(
        "shell.run",
        {"command": ["pwd"]},
        permission="read",
        context=_context(tmp_path, source="connector.weixin"),
    )

    assert result.ok is True
    assert result.facts is not None
    assert result.facts["stdout"].strip() == str(tmp_path)


@pytest.mark.asyncio
async def test_connector_sensitive_shell_requires_durable_approval(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _context(tmp_path, source="connector.weixin")
    target = tmp_path / "must-not-exist-before-approval"

    result = await registry.invoke(
        "shell.run",
        {"command": ["touch", str(target)]},
        permission="write",
        context=context,
    )

    assert result.ok is False
    assert result.action == "approval"
    assert result.error_reason == "sensitive_op_requires_approval"
    assert result.yields_control is True
    assert target.exists() is False
    approval = RunStore(tmp_path).pending_approval_for_run(result.run_id)
    assert approval is not None
    assert approval.source == "connector.weixin"
    assert approval.peer_id == context.peer_id
    assert approval.sender_id == context.sender_id
    assert approval.requested_tool == "shell.run"
    run = RunStore(tmp_path).get(result.run_id)
    assert run is not None
    assert run.phase == Phase.PAUSED
    assert run.governance == Governance.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_connector_sensitive_shell_uses_approval_instead_of_permission_ceiling(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    result = await registry.invoke(
        "shell.run",
        {"command": ["pwd"]},
        permission="write",
        context=_context(tmp_path, source="connector.weixin", permission_ceiling="read"),
    )

    assert result.ok is False
    assert result.error_reason == "sensitive_op_requires_approval"
    assert len(RunStore(tmp_path).list_approvals()) == 1


@pytest.mark.asyncio
async def test_removed_workflow_tools_remain_unavailable(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _context(tmp_path, source="connector.weixin")

    for name in ("workflow.propose", "workflow.state", "workflow.approve", "workflow.run"):
        result = await registry.invoke(name, {}, permission="prepare", context=context)
        assert result.ok is False
        assert result.error_reason == "not_found"


@pytest.mark.asyncio
async def test_connector_ingress_defaults_to_shared_write_ceiling(tmp_path: Path) -> None:
    class NoModelCalls:
        def list_roles(self) -> list[str]:
            return []

    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
    )
    try:
        assert ingress.agent.permission_ceiling == "write"
        assert ingress.agent.capabilities.allowed_tools is None
        assert ingress.agent.capabilities.disabled_tools == set()
        assert ingress.agent.capabilities.disabled_capability_classes == frozenset()
    finally:
        await ingress.event_bus.shutdown()
