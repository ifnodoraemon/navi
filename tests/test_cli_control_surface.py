from __future__ import annotations

from typer.testing import CliRunner

from navi.cli import app
from navi.memory import MemoryStore


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


def test_cli_eval_connector_default_dataset_validates(tmp_path):
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["eval", "connector", "--validate-only"],
        env={"NAVI_HOME": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert "ok dataset journeys=" in result.output
