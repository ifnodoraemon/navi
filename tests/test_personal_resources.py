from __future__ import annotations

from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext


def _context(home: Path, sender_id: str) -> CapabilityContext:
    return CapabilityContext(
        home=home,
        source="weixin",
        peer_id="peer-a",
        sender_id=sender_id,
        session_id=f"session-{sender_id}",
        workspace=str(home),
        permission_ceiling="write",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "data"),
    [
        (
            "calendar_event",
            {"title": "Design review", "starts_at": "2026-07-22T09:00:00+08:00"},
        ),
        ("reminder", {"title": "Submit report", "due_at": "2026-07-22T17:00:00+08:00"}),
        ("contact", {"display_name": "Ada", "emails": ["ada@example.com"]}),
        (
            "mail_draft",
            {"to": ["ada@example.com"], "subject": "Review", "body": "Draft body"},
        ),
        ("attention_policy", {"channel": "weixin", "enabled": True}),
    ],
)
async def test_personal_resource_adapters_create_and_read_back(
    tmp_path: Path,
    kind: str,
    data: dict,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    registry.sensitive_approval_mode = "skip"
    context = _context(tmp_path, "user-a")

    created = await registry.invoke(
        "personal.update",
        {"operation": "create", "kind": kind, "data": data},
        permission="write",
        context=context,
    )
    queried = await registry.invoke(
        "personal.query",
        {"resource_id": created.facts["resource"]["id"]},
        permission="read",
        context=context,
    )

    assert created.ok is True
    assert created.facts["verified_after"] == created.facts["resource"]
    assert created.facts["mail_delivery_supported"] is False
    assert queried.facts["resources"][0]["kind"] == kind


@pytest.mark.asyncio
async def test_personal_resource_scope_and_version_conflict_fail_closed(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    registry.sensitive_approval_mode = "skip"
    actor_a = _context(tmp_path, "user-a")
    actor_b = _context(tmp_path, "user-b")
    created = await registry.invoke(
        "personal.update",
        {
            "operation": "create",
            "kind": "reminder",
            "data": {"title": "Private reminder"},
        },
        permission="write",
        context=actor_a,
    )
    item = created.facts["resource"]

    hidden = await registry.invoke(
        "personal.query",
        {"resource_id": item["id"]},
        permission="read",
        context=actor_b,
    )
    updated = await registry.invoke(
        "personal.update",
        {
            "operation": "update",
            "resource_id": item["id"],
            "expected_version": item["version"],
            "data": {"notes": "updated"},
        },
        permission="write",
        context=actor_a,
    )
    stale = await registry.invoke(
        "personal.update",
        {
            "operation": "delete",
            "resource_id": item["id"],
            "expected_version": item["version"],
        },
        permission="write",
        context=actor_a,
    )

    assert hidden.ok is False
    assert hidden.error_reason == "not_found"
    assert updated.ok is True
    assert updated.facts["resource"]["version"] == 2
    assert stale.ok is False
    assert stale.error_reason == "conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "data"),
    [
        (
            "calendar_event",
            {
                "title": "Invalid order",
                "starts_at": "2026-07-22T10:00:00+08:00",
                "ends_at": "2026-07-22T09:00:00+08:00",
            },
        ),
        (
            "calendar_event",
            {
                "title": "Invalid zone",
                "starts_at": "2026-07-22T10:00:00",
                "timezone": "Mars/Olympus",
            },
        ),
        (
            "mail_draft",
            {"to": ["not-an-email"], "subject": "Bad", "body": "Draft"},
        ),
        (
            "attention_policy",
            {
                "channel": "weixin",
                "quiet_hours_start": "25:00",
                "quiet_hours_end": "08:00",
            },
        ),
    ],
)
async def test_personal_resource_validation_rejects_invalid_domain_values(
    tmp_path: Path,
    kind: str,
    data: dict,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    registry.sensitive_approval_mode = "skip"

    result = await registry.invoke(
        "personal.update",
        {"operation": "create", "kind": kind, "data": data},
        permission="write",
        context=_context(tmp_path, "user-a"),
    )

    assert result.ok is False
    assert result.error_reason == "schema_mismatch"
