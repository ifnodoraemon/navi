from __future__ import annotations

from navi.capabilities import CapabilityRegistry
from navi.capabilities_types import CapabilityContext
from navi.effect_journal import EffectJournal


async def test_mutating_capability_replays_persisted_result_without_reexecution(tmp_path):
    registry = CapabilityRegistry(
        home=tmp_path,
        project_dir=tmp_path,
        sensitive_approval_mode="skip",
    )
    context = CapabilityContext(
        home=tmp_path,
        loop_run_id="loop-1",
        source="cli",
        peer_id="cli",
        sender_id="tester",
        workspace=str(tmp_path),
        permission_ceiling="write",
        effect_idempotency_key="effect-1",
    )
    args = {"path": "result.txt", "content": "first"}

    first = await registry.invoke("file.write", args, permission="write", context=context)
    assert first.ok is True
    (tmp_path / "result.txt").write_text("changed-after-effect", encoding="utf-8")

    replayed = await registry.invoke("file.write", args, permission="write", context=context)

    assert replayed == first
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "changed-after-effect"


def test_effect_journal_blocks_replay_after_uncertain_exception(tmp_path):
    journal = EffectJournal(tmp_path)
    first = journal.reserve(
        effect_key="effect-2",
        loop_run_id="loop-2",
        tool="shell.run",
        owner="worker-a",
    )
    assert first.acquired is True
    journal.fail("effect-2", owner="worker-a", error="connection lost after send")

    retry = journal.reserve(
        effect_key="effect-2",
        loop_run_id="loop-2",
        tool="shell.run",
        owner="worker-b",
    )

    assert retry.status == "uncertain"


def test_effect_journal_expired_active_lease_requires_reconciliation(tmp_path):
    journal = EffectJournal(tmp_path)
    first = journal.reserve(
        effect_key="effect-expired",
        loop_run_id="loop-expired",
        tool="connector.send",
        owner="worker-a",
        lease_seconds=10,
        now=100.0,
    )

    retry = journal.reserve(
        effect_key="effect-expired",
        loop_run_id="loop-expired",
        tool="connector.send",
        owner="worker-b",
        now=111.0,
    )

    assert first.acquired is True
    assert retry.status == "uncertain"
