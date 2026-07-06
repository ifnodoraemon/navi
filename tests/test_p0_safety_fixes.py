"""Tests for P0 safety fixes: transaction atomicity and latent AttributeError."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from navi.control import ApprovalService, SurfaceContext
from navi.execution import ExecutionService
from navi.lifecycle import Governance, Phase, Resolution
from navi.runs import RunStore


def _make_run(
    tmp_path,
    *,
    phase=Phase.PAUSED,
    governance=Governance.AWAITING_APPROVAL,
    resolution=Resolution.BLOCKED,
    sender_id="sender-1",
):
    runs = RunStore(tmp_path)
    run = runs.create(
        "test run",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id=sender_id,
        workspace=str(tmp_path),
        phase=phase,
        governance=governance,
        resolution=resolution,
    )
    return run, runs


def test_recover_stale_runs_rejects_pending_approvals(tmp_path) -> None:
    """P0.2: recover_stale_runs must not crash with AttributeError.

    Previously it called self.runs.db.fetchall/execute, but RunStore has no
    .db attribute. This test covers the previously-untested recovery path."""
    run, runs = _make_run(tmp_path)
    approval = runs.create_approval(
        run_id=run.id,
        action="capability",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        requested_tool="connector.weixin.send_file",
        requested_permission="write",
        code="123456",
    )

    execution = ExecutionService(home=tmp_path)
    # Should not raise AttributeError
    execution.recover_stale_runs()

    # Run should be failed
    failed_run = runs.get(run.id)
    assert failed_run.phase == Phase.ENDED
    assert failed_run.resolution == Resolution.FAILED

    # Pending approval should be rejected
    rejected = runs.get_approval(approval.id)
    assert rejected.status == "rejected"


def test_recover_stale_runs_handles_running_status(tmp_path) -> None:
    """P0.2: recover_stale_runs should handle both RUNNING and AWAITING_APPROVAL."""
    run, runs = _make_run(tmp_path, phase=Phase.RUNNING, governance=Governance.NONE, resolution=Resolution.NONE)
    approval = runs.create_approval(
        run_id=run.id,
        action="capability",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        requested_tool="connector.weixin.send_file",
        requested_permission="write",
        code="654321",
    )

    execution = ExecutionService(home=tmp_path)
    execution.recover_stale_runs()

    failed_run = runs.get(run.id)
    assert failed_run.phase == Phase.ENDED
    assert failed_run.resolution == Resolution.FAILED

    rejected = runs.get_approval(approval.id)
    assert rejected.status == "rejected"


def test_resolve_atomicity_rollback_on_failure(tmp_path) -> None:
    """P0.1: if update_run_in_transaction fails after resolve_approval_in_transaction,
    both must roll back (approval stays pending, run stays awaiting_approval).

    This simulates the crash window between resolve_approval commit and
    update_run commit that existed before the single-transaction fix."""
    run, runs = _make_run(tmp_path)
    approval = runs.create_approval(
        run_id=run.id,
        action="capability",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        requested_tool="connector.weixin.send_file",
        requested_permission="write",
        code="111111",
    )

    service = ApprovalService(tmp_path)
    context = SurfaceContext(
        home=tmp_path,
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
    )

    # Patch update_run_in_transaction to raise, simulating a crash mid-transaction.
    # Because resolve + update now share a single connection, the approval UPDATE
    # should also roll back.
    original = RunStore.update_run_in_transaction

    def raising_update(self, conn, run_id, **kwargs):
        raise RuntimeError("simulated crash during update_run")

    with patch.object(RunStore, "update_run_in_transaction", raising_update):
        with pytest.raises(RuntimeError, match="simulated crash"):
            service.resolve(
                decision="approve",
                selection="",
                context=context,
                code="111111",
            )

    # Restore and verify rollback: approval should still be pending
    RunStore.update_run_in_transaction = original

    still_pending = runs.get_approval(approval.id)
    assert still_pending.status == "pending", (
        f"Expected approval to remain pending after rollback, got {still_pending.status}"
    )

    # Run should still be awaiting_approval
    still_awaiting = runs.get(run.id)
    assert still_awaiting.phase == Phase.PAUSED
    assert still_awaiting.governance == Governance.AWAITING_APPROVAL, (
        f"Expected run to remain awaiting approval after rollback, got {still_awaiting}"
    )


def test_resolve_approve_updates_both_atomically(tmp_path) -> None:
    """P0.1: successful approve should update both approval and run in one transaction."""
    run, runs = _make_run(tmp_path)
    approval = runs.create_approval(
        run_id=run.id,
        action="capability",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        requested_tool="connector.weixin.send_file",
        requested_permission="write",
        code="222222",
    )

    service = ApprovalService(tmp_path)
    context = SurfaceContext(
        home=tmp_path,
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
    )

    resolution = service.resolve(
        decision="approve",
        selection="",
        context=context,
        code="222222",
    )

    assert resolution.ok is True
    assert resolution.facts["status"] == "approved"

    # Approval should be approved
    approved = runs.get_approval(approval.id)
    assert approved.status == "approved"

    # Run should be pending (not session elevation)
    updated_run = runs.get(run.id)
    assert updated_run.phase == Phase.PENDING
    assert updated_run.governance == Governance.APPROVED
    assert updated_run.resolution == Resolution.NONE
