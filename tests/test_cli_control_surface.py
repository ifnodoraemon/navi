from __future__ import annotations

import json

from typer.testing import CliRunner

from navi.cli import app
from navi.delivery_outbox import DeliveryEnvelope, DeliveryOutboxStore
from navi.evolution import EvolutionLedger
from navi.memory import MemoryStore
from navi.trace import TraceStore


def test_cli_memory_add_uses_control_surface_context(tmp_path):
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "memory",
            "add",
            "preference",
            "Prefer direct answers.",
            "--reason",
            "User stated the preference.",
            "--provenance",
            "test",
        ],
        env={"NAVI_HOME": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    items = MemoryStore(tmp_path).list_items()
    assert len(items) == 1
    assert items[0].type == "preference"
    assert items[0].content == "Prefer direct answers."

    revoked = runner.invoke(
        app,
        ["memory", "revoke", items[0].id],
        env={"NAVI_HOME": str(tmp_path)},
    )

    assert revoked.exit_code == 0, revoked.output
    assert MemoryStore(tmp_path).get_item(items[0].id).status == "revoked"
    assert EvolutionLedger(tmp_path).list() == []


def test_cli_session_and_trace_mutations_use_governed_surfaces(tmp_path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    session = runner.invoke(app, ["session", "new", "cli-test"], env=env)
    assert session.exit_code == 0, session.output
    session_id = session.output.strip()
    assert MemoryStore(tmp_path).current_session_id("cli-test") == session_id

    TraceStore(tmp_path).add_event(trace_id="trace-cli", phase="turn.start")
    evaluation = runner.invoke(app, ["trace", "evaluate", "trace-cli"], env=env)
    assert evaluation.exit_code == 0, evaluation.output
    assert "success none" in evaluation.output
    assert len(TraceStore(tmp_path).list_evaluations("trace-cli")) == 1


def test_cli_eval_connector_default_dataset_validates(tmp_path):
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["eval", "connector", "--validate-only"],
        env={"NAVI_HOME": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert "ok dataset journeys=" in result.output


def test_cli_skills_lists_local_catalog_without_model_runtime(tmp_path):
    skill_dir = tmp_path / "skills" / "catalog-test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: catalog-test\ndescription: local catalog fixture\n---\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["skills"],
        env={"NAVI_HOME": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert "catalog-test" in result.output


def test_cli_connector_outbox_lists_and_explicitly_requeues_failed_item(tmp_path):
    store = DeliveryOutboxStore(tmp_path)
    item = store.enqueue(
        DeliveryEnvelope(
            batch_id="operator-retry",
            channel="weixin",
            peer_id="peer",
            text="notification",
        )
    )[0]
    store.claim_ready(channel="weixin")
    store.mark_failed(item.id, error="connector_rejected")
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    listed = runner.invoke(
        app,
        ["connectors", "outbox", "weixin", "--status", "failed", "--json-output"],
        env=env,
    )
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)[0]["id"] == item.id

    retried = runner.invoke(
        app,
        ["connectors", "outbox", "weixin", "--retry-id", item.id],
        env=env,
    )
    assert retried.exit_code == 0, retried.output
    assert store.get(item.id).status == "pending"
