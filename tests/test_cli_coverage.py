from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from navi.cli import app
from navi.memory import MemoryStore


runner = CliRunner()


def _env(tmp_path: Path) -> dict[str, str]:
    return {"NAVI_HOME": str(tmp_path)}


def test_cli_memory_and_session_commands(tmp_path):
    env = _env(tmp_path)

    added = runner.invoke(
        app,
        [
            "memory",
            "add",
            "preference",
            "Prefers coverage reports",
            "--status",
            "active",
            "--confidence",
            "0.8",
        ],
        env=env,
    )
    assert added.exit_code == 0
    item_id = added.output.strip()

    listed = runner.invoke(app, ["memory", "list", "--status", "active"], env=env)
    assert listed.exit_code == 0
    assert "Prefers coverage reports" in listed.output

    recalled = runner.invoke(app, ["memory", "recall", "coverage reports"], env=env)
    assert recalled.exit_code == 0
    assert "Memory recall:" in recalled.output

    revoked = runner.invoke(app, ["memory", "revoke", item_id], env=env)
    assert revoked.exit_code == 0
    assert "revoked" in revoked.output

    session = runner.invoke(app, ["session", "new", "cli:test"], env=env)
    assert session.exit_code == 0
    session_id = session.output.strip()
    MemoryStore(tmp_path).add_message(session_id, "user", "hello from cli")

    aliases = runner.invoke(app, ["session", "aliases"], env=env)
    assert aliases.exit_code == 0
    assert "cli:test" in aliases.output

    sessions = runner.invoke(app, ["session", "list"], env=env)
    assert sessions.exit_code == 0
    assert session_id in sessions.output

    shown = runner.invoke(app, ["session", "show", session_id], env=env)
    assert shown.exit_code == 0
    assert "hello from cli" in shown.output


def test_cli_tool_eval_model_and_skill_commands(tmp_path):
    env = _env(tmp_path)

    model = runner.invoke(app, ["model"], env=env)
    assert model.exit_code == 0
    assert "provider=" in model.output

    skills = runner.invoke(app, ["skills"], env=env)
    assert skills.exit_code == 0
    assert "(no skills)" in skills.output

    tools = runner.invoke(app, ["tools", "list", "--json-output"], env=env)
    assert tools.exit_code == 0
    assert '"task.list"' in tools.output

    provider = runner.invoke(app, ["tools", "call", "provider.config"], env=env)
    assert provider.exit_code == 0
    assert '"tool": "provider.config"' in provider.output

    bad_json = runner.invoke(app, ["tools", "call", "provider.config", "--args-json", "["], env=env)
    assert bad_json.exit_code != 0
    assert "invalid JSON" in bad_json.output

    dataset = Path(__file__).resolve().parents[1] / "evals" / "task_cases.yaml"
    eval_result = runner.invoke(app, ["eval", "tasks", "--validate-only", "--dataset", str(dataset)], env=env)
    assert eval_result.exit_code == 0
    assert "ok dataset" in eval_result.output


def test_cli_graph_trust_evolution_and_connector_status(tmp_path):
    env = _env(tmp_path)

    assert runner.invoke(app, ["graph", "list"], env=env).exit_code == 0
    assert runner.invoke(app, ["trust", "list"], env=env).exit_code == 0
    assert runner.invoke(app, ["evolution", "list"], env=env).exit_code == 0

    missing_evolution = runner.invoke(app, ["evolution", "show", "missing"], env=env)
    assert missing_evolution.exit_code != 0
    assert "event not found" in missing_evolution.output

    connectors = runner.invoke(app, ["connectors", "list"], env=env)
    assert connectors.exit_code == 0
    assert "weixin:" in connectors.output
    assert "telegram:" in connectors.output

    unknown = runner.invoke(app, ["connectors", "status", "missing"], env=env)
    assert unknown.exit_code != 0
    assert "unknown connector" in unknown.output
