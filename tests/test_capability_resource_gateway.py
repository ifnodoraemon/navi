from __future__ import annotations

from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.resource_gateway import ResourceRequest


def _context(home: Path) -> CapabilityContext:
    return CapabilityContext(
        home=home,
        source="cli",
        peer_id="cli",
        sender_id="tester",
        permission_ceiling="write",
        workspace=str(home),
    )


@pytest.mark.asyncio
async def test_capability_registry_blocks_when_resource_gateway_pauses(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    held = registry.resource_gateway.request(ResourceRequest(kind="held-capability"))
    assert held.allowed is True

    result = await registry.invoke(
        "respond",
        {"message": "hello"},
        permission="read",
        context=_context(tmp_path),
    )

    assert result.ok is False
    assert result.error_reason == "resource_pause"
    assert result.facts is not None
    assert result.facts["resource_grant"]["reason"] == "concurrency_limit"


@pytest.mark.asyncio
async def test_capability_registry_releases_resource_gateway_after_call(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    first = await registry.invoke(
        "respond",
        {"message": "one"},
        permission="read",
        context=_context(tmp_path),
    )
    second = await registry.invoke(
        "respond",
        {"message": "two"},
        permission="read",
        context=_context(tmp_path),
    )

    assert first.ok is True
    assert second.ok is True
