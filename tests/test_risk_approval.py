from __future__ import annotations

from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.lifecycle import Phase
from navi.runs import RunStore
from navi.safeguards import assess_capability_call


def _context(home: Path) -> CapabilityContext:
    return CapabilityContext(
        home=home,
        source="local",
        peer_id="peer-1",
        sender_id="user-1",
        workspace=str(home),
        permission_ceiling="write",
    )


@pytest.mark.asyncio
async def test_destructive_file_overwrite_waits_for_approval_then_replays(tmp_path: Path) -> None:
    target = tmp_path / "data.txt"
    target.write_text("important data\n", encoding="utf-8")
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _context(tmp_path)
    args = {"path": str(target), "content": "", "mode": "overwrite"}

    suspended = await registry.invoke("file.write", args, permission="write", context=context)

    assert suspended.ok is False
    assert suspended.yields_control is True
    assert target.read_text(encoding="utf-8") == "important data\n"
    assert suspended.facts is not None
    assert suspended.facts["risk"]["reason_code"] == (
        "destructive_file_overwrite_requires_approval"
    )
    assert suspended.facts["risk"]["evidence"]["before_size"] == 15
    approval = RunStore(tmp_path).pending_approval_for_run(suspended.run_id)
    assert approval is not None
    assert approval.reason.startswith("destructive_file_overwrite_requires_approval:")

    resolved = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=context,
    )
    assert resolved.ok is True
    approval_run = RunStore(tmp_path).get(suspended.run_id)
    assert approval_run is not None
    assert approval_run.phase == Phase.PENDING

    executed = await registry.invoke("file.write", args, permission="write", context=context)

    assert executed.ok is True
    assert target.read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_shell_binary_is_approved_instead_of_name_blocked(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    run = runs.create(
        "inspect rm version",
        kind="loop:turn",
        source="local",
        peer_id="peer-1",
        sender_id="user-1",
        workspace=str(tmp_path),
        phase=Phase.PENDING,
    )
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        governed_run_id=run.id,
    )
    context = _context(tmp_path)
    args = {"command": ["rm", "--version"]}

    suspended = await registry.invoke("shell.run", args, permission="write", context=context)

    assert suspended.ok is False
    assert suspended.facts is not None
    assert suspended.facts["risk"]["reason_code"] == "opaque_shell_effect_requires_approval"
    approval = runs.pending_approval_for_run(run.id)
    assert approval is not None

    resolved = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=context,
    )
    assert resolved.ok is True

    executed = await registry.invoke("shell.run", args, permission="write", context=context)

    assert executed.ok is True
    assert "rm (GNU coreutils)" in str((executed.facts or {}).get("stdout") or "")


@pytest.mark.asyncio
async def test_private_http_target_requires_approval_without_hard_rejection(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _context(tmp_path)

    suspended = await registry.invoke(
        "http.fetch",
        {"url": "http://10.232.18.209/v1/dashboard/usage"},
        permission="network",
        context=context,
    )

    assert suspended.ok is False
    assert suspended.yields_control is True
    assert suspended.facts is not None
    assert suspended.facts["risk"]["reason_code"] == (
        "private_network_access_requires_approval"
    )
    assert "public" not in (suspended.message or "").lower()


def test_public_read_only_http_call_is_not_high_risk(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    spec = registry.get("http.fetch")
    assert spec is not None

    risk = assess_capability_call(
        spec,
        {"url": "https://example.com/health", "method": "GET"},
        workspace=str(tmp_path),
    )

    assert risk.risk_class == "medium"
    assert risk.confirmation_required is False


def test_http_get_with_body_requires_approval(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    spec = registry.get("http.fetch")
    assert spec is not None

    risk = assess_capability_call(
        spec,
        {"url": "https://example.com/query", "method": "GET", "body": "query=x"},
        workspace=str(tmp_path),
    )

    assert risk.risk_class == "high"
    assert risk.confirmation_required is True
    assert risk.reason_code == "external_network_side_effect_requires_approval"


def test_internal_hostname_requires_approval(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    spec = registry.get("http.fetch")
    assert spec is not None

    risk = assess_capability_call(
        spec,
        {"url": "http://model-gateway.internal/v1/models"},
        workspace=str(tmp_path),
    )

    assert risk.risk_class == "high"
    assert risk.confirmation_required is True
    assert risk.reason_code == "private_network_access_requires_approval"


@pytest.mark.asyncio
async def test_external_file_delivery_requires_approval(tmp_path: Path) -> None:
    source = tmp_path / "report.txt"
    source.write_text("report", encoding="utf-8")
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    suspended = await registry.invoke(
        "channel.send_file",
        {"path": str(source)},
        permission="write",
        context=_context(tmp_path),
    )

    assert suspended.ok is False
    assert suspended.yields_control is True
    assert suspended.facts is not None
    assert suspended.facts["risk"]["reason_code"] == (
        "external_side_effect_requires_approval"
    )
