"""Tests for the approved-background-executor capability model:

Fix #1 — the connector ingress sandbox must NOT leak into the approved
background executor. A weixin-sourced task, once approved, can run read ops
(directory.list / file.read) that the live conversational surface blocks.

Fix #2 — sensitive (mutating) ops inside that executor are still gated: the
first one suspends the run for a fresh approval code; after approval the replay
passes the recorded grant instead of re-suspending.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.runs import RunStore


def _ctx(home: Path) -> CapabilityContext:
    return CapabilityContext(
        home=home,
        peer_id="peer",
        sender_id="sender",
        source="connector.weixin",
        permission_ceiling="write",
        workspace=str(home),
    )


@pytest.mark.asyncio
async def test_ingress_blocks_read_but_executor_allows_it(tmp_path):
    # Live connector ingress: source policy still hard-blocks local file read.
    ingress = build_capability_registry(tmp_path, project_dir=tmp_path)
    blocked = await ingress.invoke(
        "file.read", {"path": str(tmp_path / "missing.txt")},
        permission="read", context=_ctx(tmp_path),
    )
    assert blocked.ok is False
    assert "policy blocks capability" in blocked.message

    # Approved background executor: same source, but ingress sandbox is off.
    target = tmp_path / "resume.txt"
    target.write_text("name: Ada")
    runs = RunStore(tmp_path)
    task = runs.create("read file", source="connector.weixin", workspace=str(tmp_path))
    executor = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        enforce_connector_source_policy=False,
        governed_run_id=task.id,
    )
    allowed = await executor.invoke(
        "file.read", {"path": str(target)}, permission="read", context=_ctx(tmp_path)
    )
    assert allowed.ok is True  # the read op the user actually wanted


@pytest.mark.asyncio
async def test_executor_suspends_on_sensitive_write(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("write a file", source="connector.weixin", workspace=str(tmp_path))
    executor = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        enforce_connector_source_policy=False,
        governed_run_id=task.id,
    )
    res = await executor.invoke(
        "file.write",
        {"path": str(tmp_path / "x.txt"), "content": "hi"},
        permission="write",
        context=_ctx(tmp_path),
    )
    # Suspended for fresh approval rather than running unchecked.
    assert res.ok is False
    assert res.facts["reason"] == "sensitive_op_requires_approval"
    observation = json.loads(res.observation)
    assert observation["reason"] == "sensitive_op_requires_approval"
    assert "message" not in observation
    code = res.facts["approval"]["code"]
    assert len(code) == 6
    refetched = runs.get(task.id)
    assert refetched.status == "awaiting_approval"
    assert code in (refetched.result_summary or "")  # surfaced to user
    assert not (tmp_path / "x.txt").exists()  # write did NOT happen


@pytest.mark.asyncio
async def test_replay_after_approval_passes_sensitive_op(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("write a file", source="connector.weixin", workspace=str(tmp_path))
    executor = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        enforce_connector_source_policy=False,
        governed_run_id=task.id,
    )
    first = await executor.invoke(
        "file.write",
        {"path": str(tmp_path / "x.txt"), "content": "hi"},
        permission="write",
        context=_ctx(tmp_path),
    )
    code = first.facts["approval"]["code"]

    # User approves the granular code; run goes back to queued for replay.
    from navi.control import ApprovalService, SurfaceContext

    service = ApprovalService(tmp_path)
    approved = service.resolve(
        decision="approve",
        selection="explicit_code",
        context=SurfaceContext(
            home=tmp_path,
            peer_id="peer",
            sender_id="sender",
            source="connector.weixin",
            input_text=f"批准 {code}",
        ),
        code=code,
    )
    assert approved.ok is True

    # Replay: the recorded grant lets the same op through, no re-suspend.
    second = await executor.invoke(
        "file.write",
        {"path": str(tmp_path / "x.txt"), "content": "hi"},
        permission="write",
        context=_ctx(tmp_path),
    )
    assert second.ok is True
    assert (tmp_path / "x.txt").read_text() == "hi"


@pytest.mark.asyncio
async def test_executor_does_not_suspend_without_governed_run(tmp_path):
    # No governed_run_id => not an approved-execution registry => no suspend gate.
    reg = build_capability_registry(
        tmp_path, project_dir=tmp_path, enforce_connector_source_policy=False
    )
    res = await reg.invoke(
        "file.write",
        {"path": str(tmp_path / "y.txt"), "content": "hi"},
        permission="write",
        context=_ctx(tmp_path),
    )
    assert res.ok is True
    assert (tmp_path / "y.txt").exists()
