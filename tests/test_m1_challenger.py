from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.lifecycle import Phase, Resolution
from navi.runs import RunStore
from navi.runtime import AgentRuntime
from navi.weixin.config import WeixinConfig
from navi.weixin.service import WeixinService


class NoModelCalls:
    async def complete_for(self, role: str, messages: list[Any], **kwargs: Any) -> str:
        raise AssertionError(f"unexpected model call in service initialization: {role}")

    def list_roles(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_remote_connector_prepare_allowlist_blocks_execution_and_cleanup(
    tmp_path: Path,
) -> None:
    """Remote connectors expose an explicit preparation/read allowlist.

    Execution and cleanup are not remote model syscalls by default; explicit
    approval/control paths handle those state transitions.
    """
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="weixin-peer",
        sender_id="weixin-user",
        source="connector.weixin",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    spawned = await registry.invoke(
        "delegate.spawn",
        {
            "objective": "Prepare a tracked task",
            "context": "Remote connector requested tracked work.",
            "plan": "Prepare first; execution needs approval.",
            "success_criteria": "Task is tracked and governed.",
        },
        permission="prepare",
        context=context,
    )
    assert spawned.ok is True
    assert spawned.run_id


    pending_delete = await registry.invoke(
        "delegate.delete",
        {"run_id": spawned.run_id, "reason": "remote cleanup attempt"},
        permission="write",
        context=context,
    )
    assert pending_delete.ok is False
    assert pending_delete.error_reason == "remote_tool_not_allowed"
    assert RunStore(tmp_path).get(spawned.run_id) is not None

    runs = RunStore(tmp_path)
    runs.update_run(spawned.run_id, phase=Phase.ENDED, resolution=Resolution.FAILED)
    failed_delete = await registry.invoke(
        "delegate.delete",
        {"run_id": spawned.run_id, "reason": "remove failed delegation record"},
        permission="write",
        context=context,
    )
    assert failed_delete.ok is False
    assert failed_delete.error_reason == "remote_tool_not_allowed"
    assert runs.get(spawned.run_id) is not None


@pytest.mark.asyncio
async def test_bulk_delete_requires_explicit_scope(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        source="cli",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    result = await registry.invoke(
        "delegate.delete",
        {"phase": Phase.ENDED, "reason": "cleanup failed delegation records"},
        permission="write",
        context=context,
    )

    assert result.ok is False
    assert "requires source or kind scope" in result.message


def test_weixin_service_initializes_connector_ingress_without_direct_router_call(
    tmp_path: Path,
) -> None:
    runtime = AgentRuntime(home=tmp_path, provider=NoModelCalls())
    injected_client = object()

    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=runtime,
        project_dir=tmp_path,
        client=injected_client,
    )

    assert service.client is injected_client
    assert service.active is service.daemon
    assert service.ingress.agent is not None
