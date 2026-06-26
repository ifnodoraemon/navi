from __future__ import annotations

from pathlib import Path

import pytest

from navi.memory import MemoryStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".ini"}
EXCLUDED_PARTS = {
    ".agents",
    ".codex",
    ".git",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "test_home",
}


def test_no_runtime_model_simulation_mode_residue() -> None:
    forbidden = (
        "Mo" + "ckProvider",
        "NAVI_EXECUTION_" + "MO" + "CK",
        "MODEL_GATEWAY_" + "MODE=" + "mo" + "ck",
        "provider: " + "mo" + "ck",
        "Mo" + "ck provider",
    )
    hits: list[str] = []
    for path, text in _repo_text_files():
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(PROJECT_ROOT)}:{token}")

    assert hits == []


def test_memory_store_requires_source_reason_and_provenance(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)

    with pytest.raises(ValueError, match="memory reason is required"):
        store.add_item(
            "preference",
            "Prefer concise replies",
            source="manual",
            provenance="manual",
        )
    with pytest.raises(ValueError, match="memory provenance is required"):
        store.add_item(
            "preference",
            "Prefer concise replies",
            source="manual",
            reason="User stated this preference",
        )
    with pytest.raises(ValueError, match="memory source is required"):
        store.add_item(
            "preference",
            "Prefer concise replies",
            source="",
            reason="User stated this preference",
            provenance="manual",
        )

    item = store.add_item(
        "preference",
        "Prefer concise replies",
        source="manual",
        reason="User stated this preference",
        provenance="manual",
    )

    assert item.reason == "User stated this preference"
    assert item.provenance == "manual"


def _repo_text_files() -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if path.name == "test_no_behavior_hardcoding.py":
            continue
        if any(part in EXCLUDED_PARTS for part in path.relative_to(PROJECT_ROOT).parts):
            continue
        files.append((path, path.read_text(encoding="utf-8", errors="ignore")))
    return files


def test_core_holds_no_connector_approval_affordance() -> None:
    """Principle 4: the connector-agnostic core must not declare any channel's
    approval template or command syntax. Those live only in each connector's spec
    (e.g. navi/weixin/specs/connector.yaml). With no connector matching a source,
    the core affordance must be empty so no approval prompt is rendered."""
    from navi.connector_registry import approval_surface_affordance

    assert approval_surface_affordance("source-with-no-connector") == {}
    assert approval_surface_affordance("") == {}


def test_core_specs_data_has_no_approval_syntax() -> None:
    """Principle 4: specs_data is core runtime; it must not embed approval prompt
    templates or command keywords for any channel."""
    specs_data = (PROJECT_ROOT / "src" / "navi" / "specs_data.py").read_text(encoding="utf-8")
    forbidden = ("approval_template", "approval_commands")
    hits = [token for token in forbidden if token in specs_data]
    assert hits == [], f"core specs_data must not declare connector approval syntax: {hits}"


def test_durable_constraints_reload_is_not_query_scoped(tmp_path: Path) -> None:
    """Principle 12: durable constraints must survive context compression and be
    reloaded regardless of the current message. active_constraints() returns active
    constraint memories unconditionally, unlike query-scored recall()."""
    store = MemoryStore(tmp_path)
    store.add_item(
        "constraint",
        "Never deploy to production without explicit approval",
        source="user",
        reason="User stated a hard safety rule",
        provenance="manual",
        status="active",
    )

    # A query about something completely unrelated must NOT drop the constraint.
    rendered = store.render_durable_constraints()
    assert "Never deploy to production" in rendered
    assert len(store.active_constraints()) == 1

    # recall() about an unrelated topic is query-scored and may omit it; the
    # durable-constraint reload path is what guarantees presence.
    assert store.recall("what is the weather today") == [] or all(
        r.item.type == "constraint" for r in store.recall("what is the weather today")
    )


def test_planner_turn_input_carries_durable_constraints_as_trusted_block() -> None:
    """Principle 12 + 16: reloaded constraints reach the planner as a trusted
    authoritative block (sourced from Navi's governed store), not as untrusted
    conversation text."""
    from navi.prompt_os import assemble_planner_turn_input

    assembly = assemble_planner_turn_input(
        "do something",
        tools=[],
        durable_constraints="Durable constraints:\n- Never delete user data",
    )
    constraint_blocks = [b for b in assembly.blocks if b.name == "DURABLE CONSTRAINTS"]
    assert len(constraint_blocks) == 1
    block = constraint_blocks[0]
    assert "Never delete user data" in block.content
    assert block.trusted is True

    # When there are no constraints, no block is emitted (no empty noise).
    empty = assemble_planner_turn_input("do something", tools=[], durable_constraints="")
    assert [b for b in empty.blocks if b.name == "DURABLE CONSTRAINTS"] == []


def test_recovery_runtime_records_facts_not_recommendations() -> None:
    """FP-2: completion recovery must surface verifier facts, not a runtime
    recommendation or a hardcoded list of next actions."""
    sources = [
        (PROJECT_ROOT / "src/navi/recovery.py").read_text(encoding="utf-8"),
        (PROJECT_ROOT / "src/navi/engine.py").read_text(encoding="utf-8"),
        (PROJECT_ROOT / "src/navi/trace.py").read_text(encoding="utf-8"),
    ]
    forbidden = (
        "recommended",
        "RecoveryChoice",
        "Retry the last capability",
        "Create a rollback proposal",
        "Report current status",
    )
    for source in sources:
        for token in forbidden:
            assert token not in source


def test_core_does_not_hardcode_workflow_approval_next_step() -> None:
    """FP-1/FP-6: workflow approval state is a fact. The core must not append
    fixed CLI approve/reject commands to every surface."""
    source = (PROJECT_ROOT / "src/navi/engine_approval_prompts.py").read_text(
        encoding="utf-8"
    )
    assert "navi workflow approve" not in source
    assert "navi workflow reject" not in source

    from navi.engine_approval_prompts import _render_approval_prompt

    rendered = _render_approval_prompt(
        {
            "status": "awaiting_approval",
            "workflow_id": "wf-1",
            "confirmation_required": True,
        },
        source="connector.weixin",
    )
    assert rendered == ""


def test_missing_binary_error_does_not_emit_candidate_commands(tmp_path: Path) -> None:
    """FP-2: command tools report missing-binary facts instead of suggesting a
    hardcoded substitute command."""
    from navi.core_tools import _run_command

    result = _run_command(
        ["definitely-not-a-real-binary-xyz123", "--version"],
        cwd=tmp_path,
        timeout=5,
    )
    assert result["error_reason"] == "binary_not_found"
    assert "candidate_binaries" not in result


def test_connector_failure_fallbacks_do_not_suggest_retry() -> None:
    """FP-2/FP-6: connector fallbacks should identify runtime failure facts,
    not tell the user what to do next."""
    for rel in (
        "src/navi/connector_router.py",
        "src/navi/connector_runtime.py",
        "src/navi/weixin/service.py",
    ):
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert "稍后重试" not in text
        assert "请稍后" not in text


def test_memory_write_hook_block_is_not_overridden_for_non_constraints(tmp_path: Path) -> None:
    """FP-7: hook decisions are lifecycle policy facts. The runtime must not
    silently convert a declared block into observe for non-constraint memories."""
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    (hook_dir / "memory.yaml").write_text(
        """
hooks:
  - name: block.memory
    event: before_memory_write
    decision: block
    reason: memory writes disabled by local hook
""".lstrip(),
        encoding="utf-8",
    )

    store = MemoryStore(tmp_path)
    with pytest.raises(ValueError, match="memory writes disabled by local hook"):
        store.add_item(
            "preference",
            "Prefer short answers",
            source="test",
            reason="local hook block regression",
            provenance="test",
        )


def test_connectorless_approval_reply_contains_facts_not_reply_commands() -> None:
    """FP-1/FP-6: connectorless approval output should not inject a fixed
    reply command; it should expose the approval code as a fact."""
    from navi.connector_registry import render_approval_reply

    rendered = render_approval_reply(
        "cli",
        code="ABC123",
        run_id="run-1",
        action="execute:file.write",
    )
    assert "Approval code: `ABC123`" in rendered
    assert "Task ID: `run-1`" in rendered
    assert "Reply `" not in rendered
    assert "approve ABC123" not in rendered
    assert "reject ABC123" not in rendered


def test_governance_agent_reports_state_not_auto_approval() -> None:
    source = (PROJECT_ROOT / "src/navi/governance_agent.py").read_text(encoding="utf-8")
    assert "auto-approved" not in source
    assert "Auto-approved" not in source
    assert "execution grant allowed by governance state" in source
