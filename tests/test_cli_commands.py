from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from navi.cli import app
from navi.config import write_default_config
from navi.goals import GoalStore
from navi.memory import MemoryStore
from navi.trace import TraceStore


def test_cli_root_status_and_config(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    res_cfg = runner.invoke(app, ["config"], env=env)
    assert res_cfg.exit_code == 0, res_cfg.output
    cfg_data = json.loads(res_cfg.output)
    assert "config" in cfg_data

    res_status = runner.invoke(app, ["status"], env=env)
    assert res_status.exit_code == 0, res_status.output
    assert "Navi status" in res_status.output


def test_cli_metrics_and_model(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    res_m = runner.invoke(app, ["metrics"], env=env)
    assert res_m.exit_code == 0, res_m.output
    assert "Navi metrics" in res_m.output

    res_m_json = runner.invoke(app, ["metrics", "--json-output"], env=env)
    assert res_m_json.exit_code == 0, res_m_json.output
    parsed = json.loads(res_m_json.output)
    assert "overall_status" in parsed

    res_model = runner.invoke(app, ["model"], env=env)
    assert res_model.exit_code == 0, res_model.output
    assert "provider=" in res_model.output


def test_cli_skills_and_tools(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    res_skills = runner.invoke(app, ["skills"], env=env)
    assert res_skills.exit_code == 0, res_skills.output

    res_tools = runner.invoke(app, ["tools", "list"], env=env)
    assert res_tools.exit_code == 0, res_tools.output
    assert "account.usage" in res_tools.output or "file.read" in res_tools.output


def test_cli_hooks_and_prompts(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    res_hooks = runner.invoke(app, ["hooks", "list"], env=env)
    assert res_hooks.exit_code == 0, res_hooks.output

    res_prompts = runner.invoke(app, ["prompts", "inspect"], env=env)
    assert res_prompts.exit_code == 0, res_prompts.output
    assert "planner" in res_prompts.output.lower()


def test_cli_doctor(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    with patch("navi.cli.run_diagnostics", return_value=[]):
        res = runner.invoke(app, ["doctor"], env=env)
        assert res.exit_code == 0, res.output
        assert "Navi doctor" in res.output


def test_cli_service_units(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    res_unit = runner.invoke(app, ["service", "unit"], env=env)
    assert res_unit.exit_code == 0, res_unit.output
    assert "[Unit]" in res_unit.output

    res_api_unit = runner.invoke(app, ["service", "api-unit"], env=env)
    assert res_api_unit.exit_code == 0, res_api_unit.output
    assert "[Unit]" in res_api_unit.output


def test_cli_sessions(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    res_new = runner.invoke(app, ["session", "new", "test_chat"], env=env)
    assert res_new.exit_code == 0, res_new.output

    res_list = runner.invoke(app, ["session", "list"], env=env)
    assert res_list.exit_code == 0, res_list.output

    res_aliases = runner.invoke(app, ["session", "aliases"], env=env)
    assert res_aliases.exit_code == 0, res_aliases.output


def test_cli_connectors(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    res_list = runner.invoke(app, ["connectors", "list"], env=env)
    assert res_list.exit_code == 0, res_list.output
    assert "weixin" in res_list.output

    res_status = runner.invoke(app, ["connectors", "status", "weixin"], env=env)
    assert res_status.exit_code == 0, res_status.output

    res_tail = runner.invoke(app, ["connectors", "tail", "weixin"], env=env)
    assert res_tail.exit_code == 0, res_tail.output

    res_outbox = runner.invoke(app, ["connectors", "outbox", "weixin"], env=env)
    assert res_outbox.exit_code == 0, res_outbox.output


def test_cli_trace_commands(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}
    write_default_config(tmp_path)

    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="t1",
        phase="start",
        session_id="s1",
        run_id="r1",
    )

    res_list = runner.invoke(app, ["trace", "list"], env=env)
    assert res_list.exit_code == 0, res_list.output

    res_decisions = runner.invoke(app, ["trace", "decisions", "t1"], env=env)
    assert res_decisions.exit_code == 0, res_decisions.output

    res_runs = runner.invoke(app, ["trace", "runs", "t1"], env=env)
    assert res_runs.exit_code == 0, res_runs.output


def test_cli_goal_commands(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path), "OPENAI_API_KEY": "sk-dummy"}
    cfg_path = write_default_config(tmp_path)
    cfg_text = cfg_path.read_text(encoding="utf-8")
    cfg_path.write_text(cfg_text + "\nmodel:\n  api_key: dummy-token\n", encoding="utf-8")

    res_list = runner.invoke(app, ["goal", "list"], env=env)
    assert res_list.exit_code == 0, res_list.output

    with patch("navi.cli._invoke_capability", return_value={"goal_id": "g-123"}):
        res_open = runner.invoke(
            app,
            ["goal", "open", "Build feature X"],
            env=env,
        )
        assert res_open.exit_code == 0, res_open.output


def test_cli_evolution_commands(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}
    write_default_config(tmp_path)

    res_list = runner.invoke(app, ["evolution", "list"], env=env)
    assert res_list.exit_code == 0, res_list.output

    res_targets = runner.invoke(app, ["evolution", "targets"], env=env)
    assert res_targets.exit_code == 0, res_targets.output

    res_prop = runner.invoke(app, ["evolution", "proposals"], env=env)
    assert res_prop.exit_code == 0, res_prop.output


def test_cli_auth_and_graph(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    res_auth = runner.invoke(app, ["auth", "status"], env=env)
    assert res_auth.exit_code == 0, res_auth.output

    res_graph = runner.invoke(app, ["graph", "list"], env=env)
    assert res_graph.exit_code == 0, res_graph.output


def test_cli_eval_datasets(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    res_daily = runner.invoke(app, ["eval", "daily", "--validate-only", "--dataset", "evals/daily_journeys.yaml"], env=env)
    assert res_daily.exit_code == 0, res_daily.output
    assert "ok dataset" in res_daily.output

    res_claw = runner.invoke(app, ["eval", "claw", "--validate-only", "--dataset", "evals/claw_navi.yaml"], env=env)
    assert res_claw.exit_code == 0, res_claw.output
    assert "ok dataset" in res_claw.output


def test_cli_goal_lifecycle(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}
    write_default_config(tmp_path)

    store = GoalStore(tmp_path)
    goal = store.create(objective="Complete project audit", workspace=str(tmp_path))

    res_show = runner.invoke(app, ["goal", "show", goal.id], env=env)
    assert res_show.exit_code == 0, res_show.output
    assert "Complete project audit" in res_show.output

    with patch("navi.cli._invoke_capability", return_value={"status": "ok"}):
        res_state = runner.invoke(app, ["goal", "state", goal.id], env=env)
        assert res_state.exit_code == 0, res_state.output

        res_resume = runner.invoke(app, ["goal", "resume", goal.id], env=env)
        assert res_resume.exit_code == 0, res_resume.output

        res_cancel = runner.invoke(app, ["goal", "cancel", goal.id, "--reason", "obsolete"], env=env)
        assert res_cancel.exit_code == 0, res_cancel.output


def test_cli_evolution_lifecycle(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}
    write_default_config(tmp_path)

    with patch("navi.cli._invoke_capability", return_value={"event": {"rolled_back_at": 12345}}):
        res_rb = runner.invoke(app, ["evolution", "rollback", "ev-1"], env=env)
        assert res_rb.exit_code == 0, res_rb.output
        assert "rolled_back_at=12345" in res_rb.output


def test_cli_memory_queries_and_session_show(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    mem = MemoryStore(tmp_path)
    mem.add_item(
        "fact",
        "System is operating normally.",
        source="setup",
        reason="Initial status check",
        provenance="test",
    )
    mem.add_message(session_id="s1", role="user", content="hello navi")

    res_list = runner.invoke(app, ["memory", "list"], env=env)
    assert res_list.exit_code == 0, res_list.output
    assert "operating normally" in res_list.output

    res_recall = runner.invoke(app, ["memory", "recall", "system"], env=env)
    assert res_recall.exit_code == 0, res_recall.output

    res_conflicts = runner.invoke(app, ["memory", "conflicts"], env=env)
    assert res_conflicts.exit_code == 0, res_conflicts.output

    res_show = runner.invoke(app, ["session", "show", "s1"], env=env)
    assert res_show.exit_code == 0, res_show.output
    assert "hello navi" in res_show.output


def test_cli_tools_call_and_trace_show(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    res_bad_json = runner.invoke(app, ["tools", "call", "account.usage", "{invalid"], env=env)
    assert res_bad_json.exit_code != 0

    res_not_dict = runner.invoke(app, ["tools", "call", "account.usage", "[]"], env=env)
    assert res_not_dict.exit_code != 0

    res_not_found = runner.invoke(app, ["tools", "call", "nonexistent.tool", "{}"], env=env)
    assert res_not_found.exit_code != 0

    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="t_show",
        phase="start",
        session_id="s1",
        run_id="r1",
        tool="test_tool",
        message="test message",
    )
    res_trace_show = runner.invoke(app, ["trace", "show", "t_show"], env=env)
    assert res_trace_show.exit_code == 0, res_trace_show.output
    assert "start" in res_trace_show.output


def test_cli_evolution_full_cycle_and_connectors(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    with patch("navi.cli._invoke_capability") as mock_invoke:
        mock_invoke.return_value = {"proposal_id": "prop-1", "event_id": "ev-1", "activation": {"ok": True}}

        res_cand = runner.invoke(app, ["evolution", "candidates"], env=env)
        assert res_cand.exit_code == 0, res_cand.output

        res_prop = runner.invoke(
            app,
            [
                "evolution",
                "propose",
                "prompt",
                "planner",
                "improve defense",
                "new content",
                "higher pass rate",
                "restore old",
                "test1,test2",
            ],
            env=env,
        )
        assert res_prop.exit_code == 0, res_prop.output

        res_apply = runner.invoke(app, ["evolution", "apply-proposal", "prop-1"], env=env)
        assert res_apply.exit_code == 0, res_apply.output

        res_exp = runner.invoke(app, ["evolution", "experiment", "prop-1"], env=env)
        assert res_exp.exit_code == 0, res_exp.output

        res_rec = runner.invoke(app, ["evolution", "record-evaluation", "prop-1", "success"], env=env)
        assert res_rec.exit_code == 0, res_rec.output

        res_obs = runner.invoke(app, ["evolution", "observe", "ev-1", "--successes", "5"], env=env)
        assert res_obs.exit_code == 0, res_obs.output

    res_mem_bad_json = runner.invoke(
        app,
        ["memory", "add", "fact", "content", "--reason", "why", "--metadata-json", "invalid"],
        env=env,
    )
    assert res_mem_bad_json.exit_code != 0


def test_cli_trace_evaluate_and_evaluations(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}
    write_default_config(tmp_path)

    store = TraceStore(tmp_path)
    store.add_event(trace_id="t_eval", phase="start", session_id="s1", run_id="r1")
    store.record_evaluation(
        trace_id="t_eval",
        outcome="failure",
        failure_domain="prompt",
        evidence={"evaluation_rule": "safety_check"},
    )

    res_list = runner.invoke(app, ["trace", "evaluations", "t_eval"], env=env)
    assert res_list.exit_code == 0, res_list.output
    assert "safety_check" in res_list.output

    with patch("navi.cli._invoke_capability") as mock_inv:
        mock_inv.return_value = {
            "evaluation": {
                "outcome": "success",
                "failure_domain": "none",
                "evidence": {"evaluation_rule": "pass_rule"},
            }
        }
        res_ev = runner.invoke(app, ["trace", "evaluate", "t_eval"], env=env)
        assert res_ev.exit_code == 0, res_ev.output
        assert "pass_rule" in res_ev.output


def test_cli_service_installs(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}
    write_default_config(tmp_path)

    with patch("navi.cli.install_systemd_user_unit") as mock_inst, \
         patch("navi.cli.install_systemd_api_user_unit") as mock_inst_api:
        mock_inst.return_value = MagicMock(path=tmp_path / "u.service", name="u.service")
        mock_inst_api.return_value = MagicMock(path=tmp_path / "a.service", name="a.service")

        res1 = runner.invoke(app, ["service", "install"], env=env)
        assert res1.exit_code == 0, res1.output
        assert "installed" in res1.output

        res2 = runner.invoke(app, ["service", "install-api"], env=env)
        assert res2.exit_code == 0, res2.output
        assert "installed" in res2.output


def test_cli_api_cmd(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}
    cfg_path = write_default_config(tmp_path)
    cfg_text = cfg_path.read_text(encoding="utf-8")
    cfg_path.write_text(cfg_text + "\nmodel:\n  api_key: dummy-token\n", encoding="utf-8")

    with patch("uvicorn.run") as mock_run:
        res = runner.invoke(app, ["api", "--host", "127.0.0.1", "--port", "9876"], env=env)
        assert res.exit_code == 0, res.output
        assert "http://127.0.0.1:9876" in res.output
        mock_run.assert_called_once()


def test_cli_connectors_setup_and_run(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}
    write_default_config(tmp_path)

    with patch("navi.cli.get_connector_adapter") as mock_get:
        mock_adapter = MagicMock()
        mock_adapter.setup = MagicMock(return_value=asyncio.sleep(0, result="setup ok"))
        mock_adapter.run = MagicMock(return_value=asyncio.sleep(0))
        mock_adapter.status = MagicMock(return_value={"status": "healthy"})
        mock_get.return_value = mock_adapter

        res_setup = runner.invoke(app, ["connectors", "setup", "weixin"], env=env)
        assert res_setup.exit_code == 0, res_setup.output

        res_run = runner.invoke(app, ["connectors", "run", "weixin", "--once"], env=env)
        assert res_run.exit_code == 0, res_run.output


def test_cli_evolution_self_play_and_replay_and_parameters(tmp_path: Path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}
    write_default_config(tmp_path)

    # 1. parameters: list
    res_p_list = runner.invoke(app, ["evolution", "parameters"], env=env)
    assert res_p_list.exit_code == 0, res_p_list.output
    assert "Dynamic Parameters" in res_p_list.output
    assert "provider_retry_after_seconds" in res_p_list.output

    # 2. parameters: set
    res_p_set = runner.invoke(app, ["evolution", "parameters", "provider_retry_after_seconds", "--set", "40.0"], env=env)
    assert res_p_set.exit_code == 0, res_p_set.output
    assert "Updated provider_retry_after_seconds to 40.0" in res_p_set.output

    # 3. parameters: rollback
    res_p_rb = runner.invoke(app, ["evolution", "parameters", "provider_retry_after_seconds", "--rollback"], env=env)
    assert res_p_rb.exit_code == 0, res_p_rb.output
    assert "Rolled back provider_retry_after_seconds to 15.0" in res_p_rb.output

    # 4. parameters: reset
    res_p_res = runner.invoke(app, ["evolution", "parameters", "provider_retry_after_seconds", "--reset"], env=env)
    assert res_p_res.exit_code == 0, res_p_res.output
    assert "Reset provider_retry_after_seconds to default 15.0" in res_p_res.output

    # 5. replay-buffer: inspect
    res_rb = runner.invoke(app, ["evolution", "replay-buffer"], env=env)
    assert res_rb.exit_code == 0, res_rb.output
    assert "Experience Replay Buffer" in res_rb.output

    # 6. self-play: list (empty initially)
    res_sp_list = runner.invoke(app, ["evolution", "self-play", "--list"], env=env)
    assert res_sp_list.exit_code == 0, res_sp_list.output
    assert "No shadow trials found." in res_sp_list.output

    # 7. self-play: run cycle
    res_sp_cycle = runner.invoke(app, ["evolution", "self-play", "-n", "1", "--no-auto-promote"], env=env)
    assert res_sp_cycle.exit_code == 0, res_sp_cycle.output
    assert "Completed self-play cycle" in res_sp_cycle.output

    # 8. self-play: list after cycle
    res_sp_list2 = runner.invoke(app, ["evolution", "self-play", "--list"], env=env)
    assert res_sp_list2.exit_code == 0, res_sp_list2.output
    assert "trial=" in res_sp_list2.output



