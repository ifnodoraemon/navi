from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .graph import GraphStore
from .memory import MemoryStore
from .prompting import PromptLayerStore


BUILTIN_EVOLUTION_EVAL_CASES: dict[str, dict[str, Any]] = {
    "runtime.text.nonempty": {
        "id": "runtime.text.nonempty",
        "target_types": ["prompt_layer", "skill"],
        "assertions": [{"type": "nonempty"}],
    },
    "runtime.json.valid": {
        "id": "runtime.json.valid",
        "target_types": ["memory_item", "eval_case", "graph_node"],
        "assertions": [{"type": "json_valid"}],
    },
}


@dataclass(frozen=True, slots=True)
class EvolutionTargetDescriptor:
    target_type: str
    description: str
    source: str
    permissions_can_expand: bool = False


class EvolutionTargetAdapter(Protocol):
    descriptor: EvolutionTargetDescriptor

    def read(self, target_id: str) -> str: ...

    def validate(self, target_id: str, candidate: str) -> dict[str, Any]: ...

    def apply(self, target_id: str, candidate: str) -> None: ...

    def rollback(self, target_id: str, before: str) -> None: ...


class EvolutionTargetAdapterRegistry:
    def __init__(self, home: Path):
        adapters: tuple[EvolutionTargetAdapter, ...] = (
            _PromptLayerAdapter(home),
            _SkillAdapter(home),
            _MemoryItemAdapter(home),
            _EvalCaseAdapter(home),
            _GraphNodeAdapter(home),
        )
        self._adapters = {
            adapter.descriptor.target_type: adapter for adapter in adapters
        }

    def get(self, target_type: str) -> EvolutionTargetAdapter:
        try:
            return self._adapters[target_type]
        except KeyError as exc:
            raise ValueError(
                f"evolution target has no runtime adapter: {target_type}"
            ) from exc

    def descriptors(self) -> tuple[EvolutionTargetDescriptor, ...]:
        return tuple(adapter.descriptor for adapter in self._adapters.values())

    def available_eval_cases(self) -> tuple[dict[str, Any], ...]:
        adapter = self.get("eval_case")
        if not isinstance(adapter, _EvalCaseAdapter):
            return ()
        return adapter.list_available()


class _PromptLayerAdapter:
    descriptor = EvolutionTargetDescriptor(
        "prompt_layer",
        "Versioned prompt layer content loaded by the runtime prompt assembler.",
        "prompting",
    )

    def __init__(self, home: Path):
        self.store = PromptLayerStore(home)

    def read(self, target_id: str) -> str:
        return self.store.read(target_id)

    def validate(self, target_id: str, candidate: str) -> dict[str, Any]:
        self.store.override_path(target_id)
        if not self.store.is_declared(target_id):
            raise ValueError("prompt_layer target is not loaded by the runtime")
        if not candidate.strip():
            raise ValueError("prompt_layer candidate must not be empty")
        return {"loaded_by": "PromptLayerStore", "characters": len(candidate)}

    def apply(self, target_id: str, candidate: str) -> None:
        self.validate(target_id, candidate)
        self.store.write_override(target_id, candidate)

    def snapshot(self, target_id: str) -> str:
        path = self.store.override_path(target_id)
        return json.dumps(
            {
                "format": "prompt_layer_snapshot_v1",
                "override_exists": path.exists(),
                "override_content": path.read_text(encoding="utf-8")
                if path.exists()
                else "",
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def rollback_snapshot(self, target_id: str, snapshot: str) -> None:
        data = _json_object(snapshot, "prompt_layer snapshot")
        if data.get("format") != "prompt_layer_snapshot_v1":
            raise ValueError("prompt_layer rollback snapshot has an unknown format")
        if bool(data.get("override_exists")):
            self.store.write_override(
                target_id,
                str(data.get("override_content") or ""),
            )
        else:
            self.store.delete_override(target_id)

    def rollback(self, target_id: str, before: str) -> None:
        if before:
            self.store.write_override(target_id, before)
        else:
            self.store.delete_override(target_id)


class _SkillAdapter:
    descriptor = EvolutionTargetDescriptor(
        "skill",
        "Skill content loaded from the managed user skill directory.",
        "skills",
    )

    def __init__(self, home: Path):
        self.root = (home / "skills").resolve()

    def _path(self, target_id: str) -> Path:
        name = _safe_name(target_id)
        return self.root / name / "SKILL.md"

    def read(self, target_id: str) -> str:
        path = self._path(target_id)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def validate(self, target_id: str, candidate: str) -> dict[str, Any]:
        self._path(target_id)
        if not candidate.strip():
            raise ValueError("skill candidate must not be empty")
        return {"loaded_by": "SkillStore", "characters": len(candidate)}

    def apply(self, target_id: str, candidate: str) -> None:
        self.validate(target_id, candidate)
        path = self._path(target_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(candidate, encoding="utf-8")

    def rollback(self, target_id: str, before: str) -> None:
        path = self._path(target_id)
        if before:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(before, encoding="utf-8")
        elif path.exists():
            path.unlink()


class _MemoryItemAdapter:
    descriptor = EvolutionTargetDescriptor(
        "memory_item",
        "Typed durable memory records consumed by governed recall.",
        "memory",
    )

    def __init__(self, home: Path):
        self.store = MemoryStore(home)

    def read(self, target_id: str) -> str:
        item = self.store.get_item(target_id)
        return json.dumps(item.__dict__, ensure_ascii=False, sort_keys=True) if item else ""

    def validate(self, target_id: str, candidate: str) -> dict[str, Any]:
        data = _json_object(candidate, "memory_item")
        current = self.store.get_item(target_id)
        if current is None:
            raise ValueError("memory_item target must already exist")
        if str(data.get("id") or "") != target_id:
            raise ValueError("memory_item candidate id must match target_id")
        required = {"type", "status", "scope", "content", "source"}
        missing = sorted(key for key in required if not data.get(key))
        if missing:
            raise ValueError(f"memory_item candidate missing fields: {', '.join(missing)}")
        immutable = (
            "id",
            "type",
            "status",
            "scope",
            "source",
            "created_at",
            "provenance",
        )
        current_data = current.__dict__
        changed = [key for key in immutable if data.get(key) != current_data.get(key)]
        if changed:
            raise ValueError(
                "memory_item evolution cannot change authority or lifecycle fields: "
                + ", ".join(changed)
            )
        return {"loaded_by": "MemoryStore", "memory_type": str(data["type"])}

    def apply(self, target_id: str, candidate: str) -> None:
        self.validate(target_id, candidate)
        self.store.restore_item(_json_object(candidate, "memory_item"))

    def rollback(self, target_id: str, before: str) -> None:
        if before:
            self.store.restore_item(_json_object(before, "memory_item"))
        else:
            self.store.delete_item(target_id)


class _EvalCaseAdapter:
    descriptor = EvolutionTargetDescriptor(
        "eval_case",
        "Managed evaluation cases consumed by the evolution experiment runner.",
        "evals",
    )

    def __init__(self, home: Path):
        self.root = (home / "evals").resolve()

    def _path(self, target_id: str) -> Path:
        return self.root / f"{_safe_name(target_id)}.json"

    def read(self, target_id: str) -> str:
        builtin = BUILTIN_EVOLUTION_EVAL_CASES.get(target_id)
        if builtin is not None:
            return json.dumps(builtin, ensure_ascii=False, sort_keys=True)
        path = self._path(target_id)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def validate(self, target_id: str, candidate: str) -> dict[str, Any]:
        if target_id in BUILTIN_EVOLUTION_EVAL_CASES:
            raise ValueError("built-in eval cases are immutable runtime contracts")
        data = _json_object(candidate, "eval_case")
        if str(data.get("id") or target_id) != target_id:
            raise ValueError("eval_case id must match target_id")
        assertions = data.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise ValueError("eval_case requires non-empty assertions")
        return {"loaded_by": "EvolutionExperimentRunner", "assertions": len(assertions)}

    def apply(self, target_id: str, candidate: str) -> None:
        self.validate(target_id, candidate)
        path = self._path(target_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(candidate, encoding="utf-8")

    def rollback(self, target_id: str, before: str) -> None:
        if target_id in BUILTIN_EVOLUTION_EVAL_CASES:
            raise ValueError("built-in eval cases are immutable runtime contracts")
        path = self._path(target_id)
        if before:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(before, encoding="utf-8")
        elif path.exists():
            path.unlink()

    def list_available(self) -> tuple[dict[str, Any], ...]:
        cases: list[dict[str, Any]] = [
            {
                "id": case_id,
                "target_types": list(case["target_types"]),
                "assertion_types": [
                    str(assertion.get("type") or "")
                    for assertion in case["assertions"]
                    if isinstance(assertion, dict)
                ],
                "source": "runtime",
                "mutable": False,
            }
            for case_id, case in sorted(BUILTIN_EVOLUTION_EVAL_CASES.items())
        ]
        if not self.root.exists():
            return tuple(cases)
        for path in sorted(self.root.glob("*.json")):
            try:
                data = _json_object(path.read_text(encoding="utf-8"), "eval_case")
                self.validate(path.stem, json.dumps(data, ensure_ascii=False, sort_keys=True))
            except (OSError, ValueError):
                continue
            assertions = data.get("assertions")
            if not isinstance(assertions, list):
                continue
            cases.append(
                {
                    "id": path.stem,
                    "target_types": [
                        str(item)
                        for item in data.get("target_types", [])
                        if str(item).strip()
                    ],
                    "assertion_types": [
                        str(assertion.get("type") or "")
                        for assertion in assertions
                        if isinstance(assertion, dict)
                    ],
                    "source": "managed",
                    "mutable": True,
                }
            )
        return tuple(cases)


class _GraphNodeAdapter:
    descriptor = EvolutionTargetDescriptor(
        "graph_node",
        "Existing graph node data consumed by semantic graph queries.",
        "graph",
    )

    def __init__(self, home: Path):
        self.store = GraphStore(home)

    def read(self, target_id: str) -> str:
        node = self.store.get(target_id)
        return json.dumps(node.data, ensure_ascii=False, sort_keys=True) if node else ""

    def validate(self, target_id: str, candidate: str) -> dict[str, Any]:
        if self.store.get(target_id) is None:
            raise ValueError("graph_node target must already exist")
        data = _json_object(candidate, "graph_node")
        return {"loaded_by": "GraphStore", "keys": sorted(data)}

    def apply(self, target_id: str, candidate: str) -> None:
        self.validate(target_id, candidate)
        self.store.replace_data(target_id, _json_object(candidate, "graph_node"))

    def rollback(self, target_id: str, before: str) -> None:
        if before:
            self.store.replace_data(target_id, _json_object(before, "graph_node"))
        else:
            self.store.delete(target_id)


def _safe_name(value: str) -> str:
    name = value.strip()
    if (
        not name
        or name in {".", ".."}
        or ".." in name
        or "/" in name
        or "\\" in name
        or Path(name).is_absolute()
    ):
        raise ValueError("evolution target_id must be a single safe name")
    return name


def _json_object(value: str, target_type: str) -> dict[str, Any]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{target_type} candidate must be valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{target_type} candidate must be a JSON object")
    return data
