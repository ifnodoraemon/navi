"""Round-7 concurrency-guard regressions.

Two coupled approval races, found by cross-checking the execution and
persistence layers:

1. Approval state UPDATEs were unconditional. Two surfaces (daemon, API,
   connector) racing to resolve the same code could last-writer-win, letting a
   late reject clobber an approve. Fix: every pending->X transition is now
   ``WHERE ... AND status = 'pending'``.
2. ``reissue_approval`` rewrote run status unconditionally. A code expiring at
   the same moment the run was approved could pull an already-queued run back
   into awaiting_approval (and orphan a fresh code). Fix: re-issue only applies
   to a run still in awaiting_approval/expired.
"""

from __future__ import annotations

from pathlib import Path

from navi.runs import RunStore


def _run(runs: RunStore, tmp_path: Path, status: str):
    return runs.create(
        "t",
        prompt="p",
        source="connector.weixin",
        peer_id="p",
        sender_id="s",
        workspace=str(tmp_path),
        kind="delegation",
        status=status,
    )


def test_reissue_refuses_when_run_already_advanced(tmp_path: Path) -> None:
    """A run approved/advanced concurrently must not be pulled back into
    awaiting_approval by an expired-code re-issue, nor leave an orphan code."""
    runs = RunStore(tmp_path)
    run = _run(runs, tmp_path, "queued")  # already approved & queued
    # An expired approval lingers from before the run was approved.
    runs.create_approval(run_id=run.id, peer_id="p", sender_id="s", ttl_seconds=-1)

    result = runs.reissue_approval(run_id=run.id, peer_id="p", sender_id="s")

    assert result is None  # re-issue refused
    assert runs.get(run.id).status == "queued"  # not pulled back
    # No fresh pending code was minted (no orphan).
    assert runs.pending_approval_for_run(run.id, sender_id="s") is None


def test_reissue_resurrects_expired_run(tmp_path: Path) -> None:
    """A run in 'expired' (archived) state IS re-issuable — sanity that the
    guard does not over-block the legitimate recovery path."""
    runs = RunStore(tmp_path)
    run = _run(runs, tmp_path, "awaiting_approval")
    runs.create_approval(run_id=run.id, peer_id="p", sender_id="s", ttl_seconds=-1)
    runs.archive_expired_approvals()  # run -> expired
    assert runs.get(run.id).status == "expired"

    fresh = runs.reissue_approval(run_id=run.id, peer_id="p", sender_id="s")

    assert fresh is not None
    assert runs.get(run.id).status == "awaiting_approval"


def test_resolve_approval_late_reject_cannot_clobber_approve(tmp_path: Path) -> None:
    """resolve_approval must not transition an approval that is no longer
    pending. A racing reject arriving after an approve is a no-op, not
    last-writer-wins."""
    runs = RunStore(tmp_path)
    run = _run(runs, tmp_path, "awaiting_approval")
    approval = runs.create_approval(run_id=run.id, peer_id="p", sender_id="s")

    first = runs.resolve_approval(approval.code, "s", "approved", peer_id="p")
    assert first is not None and first.status == "approved"

    # Late reject on the same code: rejected because no longer pending.
    second = runs.resolve_approval(approval.code, "s", "rejected", peer_id="p")
    assert second is None
    assert runs.get_approval(approval.code).status == "approved"
