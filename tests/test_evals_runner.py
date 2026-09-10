from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from navi.evals import (
    ClawEvalResult,
    DailyJourneyResult,
    _claw_error_domains,
    _claw_task_to_journey,
    _eval_run_id,
    _latest_run_id,
    _match_daily_expectation,
    _render_journey_text,
    _safe_path_name,
    _validate_eval_steps,
    claw_results_to_json,
    load_claw_eval_dataset,
    load_connector_journey_eval_dataset,
    load_daily_journey_eval_dataset,
)
from navi.goals import GoalStore
from navi.runs import RunStore
from navi.weixin.evals import (
    _CaptureWeixinClient,
    _FailingEvalProvider,
    _event_names,
    _match_expectation as _match_weixin_expectation,
    load_journey_eval_dataset as load_weixin_journey_eval_dataset,
)
from navi.weixin.service import WeixinService
from navi.weixin.config import WeixinConfig


def test_evals_helpers():
    assert _safe_path_name("Simple Journey 123!") == "Simple_Journey_123_"
    assert _safe_path_name("   ") == "journey"
    run_id = _eval_run_id()
    assert len(run_id) > 10

    res = ClawEvalResult(
        task_id="t1",
        ok=True,
        split="general",
        category="code",
        language="python",
        pass_count=1,
        attempts=1,
        error_domains=[],
        errors=[],
        attempts_detail=[],
    )
    json_str = claw_results_to_json([res])
    assert "t1" in json_str


def test_claw_error_domains_and_journey_conversion():
    assert _claw_error_domains({}, []) == []

    task = {"task_id": "c1", "rubric_dimensions": ["quality"], "journey": {"steps": []}}
    domains = _claw_error_domains(
        task,
        ["attempt[1]: run_count_delta mismatch", "some other error"],
    )
    assert "completion" in domains
    assert "safety" in domains
    assert "robustness" in domains
    assert "quality" in domains

    j = _claw_task_to_journey(task)
    assert j["id"] == "c1"


def test_render_journey_text(tmp_path: Path):
    runs = RunStore(tmp_path)
    text = "Run id is {{run_id}} and approval is {{approval_code}}"
    rendered = _render_journey_text(text, runs, latest_run_id="run-456")
    assert "run-456" in rendered
    assert "{{approval_code}}" not in rendered


def test_match_daily_expectation(tmp_path: Path):
    runs = RunStore(tmp_path)
    goals = GoalStore(tmp_path)

    err_invalid = _match_daily_expectation("step", "not_dict", event={}, runs=runs, goals=goals, latest_run_id="", before_run_count=0, before_scheduled_goal_count=0)
    assert len(err_invalid) == 1

    expect = {
        "action": "chat",
        "text_contains": "expected phrase",
        "text_contains_any": ["alpha", "beta"],
        "text_not_contains_any": ["forbidden"],
        "run_count_delta": 1,
        "run_count": 0,
        "scheduled_goal_count_delta": 0,
        "scheduled_goal_count": 0,
    }
    event = {"action": "tool", "text": "here is forbidden alpha text"}

    errors = _match_daily_expectation(
        "step[0]",
        expect,
        event=event,
        runs=runs,
        goals=goals,
        latest_run_id="",
        before_run_count=0,
        before_scheduled_goal_count=0,
    )
    assert any("action expected" in e for e in errors)
    assert any("text did not contain" in e for e in errors)
    assert any("forbidden" in e for e in errors)
    assert any("run_count_delta" in e for e in errors)


def test_eval_dataset_schema_errors(tmp_path: Path):
    bad_yaml = tmp_path / "bad.yaml"

    bad_yaml.write_text("string: content", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a journeys list"):
        load_daily_journey_eval_dataset(bad_yaml)

    bad_yaml.write_text("journeys:\n  - not_mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_daily_journey_eval_dataset(bad_yaml)

    bad_yaml.write_text("journeys:\n  - id: ''\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing id"):
        load_daily_journey_eval_dataset(bad_yaml)

    bad_yaml.write_text("journeys:\n  - id: j1\n    steps:\n      - user: hi\n  - id: j1\n    steps:\n      - user: hi\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate id"):
        load_daily_journey_eval_dataset(bad_yaml)

    with pytest.raises(ValueError, match="must be a mapping"):
        _validate_eval_steps(["not_dict"], prefix="test")

    with pytest.raises(ValueError, match="obsolete expectation keys"):
        _validate_eval_steps([{"expect": {"watch_count": 1}}], prefix="test")

    with pytest.raises(ValueError, match="unsupported action"):
        _validate_eval_steps([{"expect": {"action": "unsupported_action"}}], prefix="test")


def test_claw_dataset_schema_errors(tmp_path: Path):
    bad_yaml = tmp_path / "bad_claw.yaml"

    bad_yaml.write_text("tasks: 'not a list'", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a tasks list"):
        load_claw_eval_dataset(bad_yaml)

    bad_yaml.write_text("tasks:\n  - task_id: ''\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing task_id"):
        load_claw_eval_dataset(bad_yaml)

    bad_yaml.write_text(
        "tasks:\n  - task_id: t1\n    query: q\n    language: py\n    category: c\n    split: invalid_split\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="split must be"):
        load_claw_eval_dataset(bad_yaml)


def test_connector_dataset_schema_errors(tmp_path: Path):
    bad_yaml = tmp_path / "bad_conn.yaml"
    bad_yaml.write_text("other: key", encoding="utf-8")
    with pytest.raises(ValueError, match="must declare connector"):
        load_connector_journey_eval_dataset(bad_yaml)

    bad_yaml.write_text("connector: unknown_adapter", encoding="utf-8")
    with pytest.raises(ValueError, match="connector journey eval is not available"):
        load_connector_journey_eval_dataset(bad_yaml)


@pytest.mark.asyncio
async def test_weixin_eval_components(tmp_path: Path):
    client = _CaptureWeixinClient()
    await client.send_message(account_id="acc", peer_id="p1", text="chunk 1")
    assert len(client.sent) == 1
    assert client.sent[0]["text"] == "chunk 1"

    ticket = await client.get_typing_ticket(user_id="u1")
    assert ticket == ""

    await client.send_typing(peer_id="p1", typing_ticket="tk", status=1)
    assert len(client.typing) == 1

    failing_provider = _FailingEvalProvider()
    with pytest.raises(RuntimeError, match="eval provider failure"):
        await failing_provider.complete([])

    with pytest.raises(RuntimeError, match="eval provider failure"):
        async for _ in failing_provider.stream([]):
            pass


def test_weixin_event_names_and_matching(tmp_path: Path):
    assert _event_names(tmp_path) == []

    wx_dir = tmp_path / "weixin"
    wx_dir.mkdir()
    events_file = wx_dir / "events.jsonl"
    events_file.write_text('{"event": "inbound_msg"}\n{"event": "outbound_sent"}\ninvalid\n', encoding="utf-8")

    names = _event_names(tmp_path)
    assert "inbound_msg" in names
    assert "outbound_sent" in names

    runs = RunStore(tmp_path)
    mock_service = MagicMock()
    mock_service.client.sent = [{"text": "Hello user"}]

    expect = {
        "handled": True,
        "sent_count_delta": 1,
        "sent_contains": "Hello",
        "sent_contains_any": ["Hello", "Hi"],
        "sent_not_contains_any": ["bad"],
        "event_contains": ["inbound_msg", "missing_event"],
    }
    errors = _match_weixin_expectation(
        "step[0]",
        expect,
        event={"handled": True},
        home=tmp_path,
        service=mock_service,
        runs=runs,
        before_sent_count=0,
        before_run_count=0,
        before_scheduled_goal_count=0,
    )
    assert len(errors) == 1
    assert "missing connector events" in errors[0]


def test_load_weixin_dataset_and_helpers(tmp_path: Path):
    from navi.evals import ReplayBufferEvalReport
    from navi.weixin.evals import _safe_path_name as _safe_wx_path_name

    assert _safe_wx_path_name("Weixin Test #1") == "Weixin_Test__1"
    assert _safe_wx_path_name("   ") == "journey"

    root = Path(__file__).resolve().parents[1]
    dataset = load_weixin_journey_eval_dataset(root / "evals" / "connector_journeys.yaml")
    assert dataset["connector"] == "weixin"

    rep = ReplayBufferEvalReport(
        total_evaluated=10,
        golden_count=5,
        hard_negative_count=5,
        safeguards_retention_rate=1.0,
        golden_fidelity_rate=1.0,
        channel_breakdown={"weixin": 10},
        passed=True,
        details=[],
    )
    rep_dict = rep.to_dict()
    assert rep_dict["total_evaluated"] == 10
    assert rep_dict["passed"] is True

    runs = RunStore(tmp_path)
    assert _latest_run_id(runs) == ""

    goals = GoalStore(tmp_path)
    expect_more = {
        "scheduled_goal_status": "active",
        "cron_schedule": "* * * * *",
        "run_phase": "completed",
        "run_resolution": "success",
        "goal_phase": "completed",
    }
    errs = _match_daily_expectation(
        "step[0]",
        expect_more,
        event={},
        runs=runs,
        goals=goals,
        latest_run_id="nonexistent-run",
        before_run_count=0,
        before_scheduled_goal_count=0,
    )
    assert len(errs) >= 3


@pytest.mark.asyncio
async def test_run_eval_datasets(tmp_path: Path):
    from unittest.mock import AsyncMock, patch
    from navi.evals import (
        run_claw_eval_dataset,
        run_connector_journey_eval_dataset,
        run_daily_journey_eval_dataset,
    )

    root = Path(__file__).resolve().parents[1]
    daily_yaml = root / "evals" / "daily_journeys.yaml"
    claw_yaml = root / "evals" / "claw_fast.yaml"
    conn_yaml = root / "evals" / "connector_journeys.yaml"

    mock_res = DailyJourneyResult(id="test", ok=True, errors=[], events=[])
    with patch("navi.evals._run_daily_journey", new_callable=AsyncMock) as mock_daily:
        mock_daily.return_value = mock_res
        results = await run_daily_journey_eval_dataset(
            home=tmp_path,
            project_dir=tmp_path,
            dataset=daily_yaml,
            timeout_seconds=5.0,
        )
        assert len(results) > 0
        assert results[0].ok is True

    with patch("navi.evals._run_daily_journey", new_callable=AsyncMock) as mock_daily:
        mock_daily.side_effect = [
            DailyJourneyResult(id="t1", ok=True, errors=[], events=[]),
            TimeoutError("timeout"),
            RuntimeError("boom"),
        ]
        claw_results = await run_claw_eval_dataset(
            home=tmp_path,
            project_dir=tmp_path,
            dataset=claw_yaml,
            attempts=3,
            timeout_seconds=5.0,
        )
        assert len(claw_results) > 0
        assert claw_results[0].attempts == 3

    mock_adapter = MagicMock()
    mock_adapter.load_journey_eval_dataset.return_value = {"connector": "weixin"}
    mock_adapter.run_journey_eval_dataset = AsyncMock(return_value=["mock_journey_result"])
    with patch("navi.evals.get_connector_adapter", return_value=mock_adapter):
        conn_results = await run_connector_journey_eval_dataset(
            home=tmp_path,
            project_dir=tmp_path,
            dataset=conn_yaml,
            timeout_seconds=5.0,
        )
        assert len(conn_results) == 1


