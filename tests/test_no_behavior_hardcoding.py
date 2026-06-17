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
