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
