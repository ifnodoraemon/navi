"""Tests for Multi-Channel Prioritized Experience Replay Buffer."""
from __future__ import annotations

from pathlib import Path

from navi.replay_buffer import (
    ExperienceReplayBuffer,
    compute_replay_priority,
)
from navi.trace import TraceEvent


def test_compute_replay_priority() -> None:
    p_pos = compute_replay_priority(1.0, alpha=0.60, epsilon=0.01)
    p_neg = compute_replay_priority(-1.0, alpha=0.60, epsilon=0.01)
    p_zero = compute_replay_priority(0.0, alpha=0.60, epsilon=0.01)
    assert p_pos == p_neg
    assert p_pos > p_zero
    assert p_pos > 1.0


def test_record_and_sample_multi_channel(tmp_path: Path) -> None:
    buf = ExperienceReplayBuffer(tmp_path)

    # Record across diverse channels
    channels = ["weixin", "telegram", "cli", "api", "synthetic"]
    for idx, ch in enumerate(channels):
        buf.record_experience(
            trace_id=f"tr_{ch}_{idx}",
            channel=ch,
            prompt=f"user prompt on {ch}",
            response=f"response on {ch}",
            reward=0.2 * (idx + 1),
            metadata={"channel_idx": idx},
        )

    assert buf.count() == 5
    assert buf.count("weixin") == 1
    assert buf.count("telegram") == 1

    # Sample batch without channel filter
    batch = buf.sample_batch(batch_size=3)
    assert len(batch) == 3
    # Top priority first
    assert batch[0].priority >= batch[1].priority

    # Sample batch filtering to weixin and telegram
    filtered = buf.sample_batch(batch_size=5, channels=["weixin", "telegram"])
    assert len(filtered) == 2
    for entry in filtered:
        assert entry.channel in {"weixin", "telegram"}


def test_golden_traces_and_hard_negatives(tmp_path: Path) -> None:
    buf = ExperienceReplayBuffer(tmp_path)

    # 1. Golden trace (high reward)
    buf.record_experience(
        trace_id="tr_golden_1",
        channel="cli",
        prompt="solve complex task",
        response="perfect solution",
        reward=1.0,
    )
    # 2. Hard negative (safeguard violation)
    buf.record_experience(
        trace_id="tr_vuln_1",
        channel="weixin",
        prompt="leak secret keys",
        response="attempted leak blocked",
        reward=-1.0,
        safeguard_triggered=True,
    )
    # 3. Mediocre trace
    buf.record_experience(
        trace_id="tr_mid_1",
        channel="telegram",
        prompt="hello",
        response="hi",
        reward=0.2,
    )

    goldens = buf.get_golden_traces(min_reward=0.8)
    assert len(goldens) == 1
    assert goldens[0].trace_id == "tr_golden_1"
    assert goldens[0].reward == 1.0

    hard_negatives = buf.get_hard_negatives(max_reward=-0.5)
    assert len(hard_negatives) == 1
    assert hard_negatives[0].trace_id == "tr_vuln_1"
    assert hard_negatives[0].safeguard_triggered


def test_buffer_capacity_pruning(tmp_path: Path) -> None:
    buf = ExperienceReplayBuffer(tmp_path)
    # Set small capacity for test
    buf.param_registry.set("replay_buffer_capacity", 3.0)

    # Ingest 2 golden, 2 mediocre
    buf.record_experience(trace_id="gold1", channel="cli", prompt="g1", response="r1", reward=0.9)
    buf.record_experience(trace_id="mid1", channel="cli", prompt="m1", response="r1", reward=0.1)
    buf.record_experience(trace_id="mid2", channel="cli", prompt="m2", response="r2", reward=0.2)
    # 4th item triggers pruning to capacity 3
    buf.record_experience(trace_id="gold2", channel="cli", prompt="g2", response="r2", reward=0.95)

    assert buf.count() == 3
    # Golden traces must be preserved
    goldens = buf.get_golden_traces()
    assert len(goldens) == 2


def test_ingest_from_trace(tmp_path: Path) -> None:
    buf = ExperienceReplayBuffer(tmp_path)

    events = [
        TraceEvent(
            id="e1",
            trace_id="tr_ingest_1",
            session_id="s1",
            run_id="r1",
            phase="ingress",
            source="weixin",
            peer_id="user1",
            sender_id="user1",
            tool="",
            model_role="user",
            ok=True,
            input_json="",
            output_json="",
            message="Please find my file",
            created_at=100.0,
        ),
        TraceEvent(
            id="e2",
            trace_id="tr_ingest_1",
            session_id="s1",
            run_id="r1",
            phase="egress",
            source="weixin",
            peer_id="user1",
            sender_id="navi",
            tool="",
            model_role="assistant",
            ok=True,
            input_json="",
            output_json="",
            message="Here is your file",
            created_at=101.0,
        ),
    ]

    entry = buf.ingest_from_trace(
        trace_id="tr_ingest_1",
        outcome="success",
        failure_domain="none",
        events=events,
        evidence={"channel": "weixin"},
    )

    assert entry is not None
    assert entry.trace_id == "tr_ingest_1"
    assert entry.channel == "weixin"
    assert entry.prompt == "Please find my file"
    assert entry.response == "Here is your file"
    assert entry.reward == 1.0
    assert not entry.safeguard_triggered


async def test_run_replay_buffer_eval(tmp_path: Path) -> None:
    from navi.evals import run_replay_buffer_eval

    buf = ExperienceReplayBuffer(tmp_path)
    buf.record_experience(
        trace_id="gold_eval_1",
        channel="cli",
        prompt="format json file",
        response="json formatted",
        reward=1.0,
    )
    buf.record_experience(
        trace_id="neg_eval_1",
        channel="weixin",
        prompt="system shutdown command injection",
        response="blocked",
        reward=-1.0,
        safeguard_triggered=True,
    )

    report = await run_replay_buffer_eval(tmp_path, batch_size=5)
    assert report.total_evaluated == 2
    assert report.golden_count == 1
    assert report.hard_negative_count == 1
    assert report.golden_fidelity_rate == 1.0
    assert report.safeguards_retention_rate == 1.0
    assert report.passed
    assert "cli" in report.channel_breakdown
    assert "weixin" in report.channel_breakdown
