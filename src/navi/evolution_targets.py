from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .graph import GraphStore
from .memory import MemoryStore
from .prompting import PromptLayerStore
from .runs import RunStore


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
            _RunExecutionAdapter(home),
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
        if not candidate.strip():
            raise ValueError("prompt_layer candidate must not be empty")
        return {"loaded_by": "PromptLayerStore", "characters": len(candidate)}

    def apply(self, target_id: str, candidate: str) -> None:
        self.validate(target_id, candidate)
        self.store.write_override(target_id, candidate)

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
        if str(data.get("id") or "") != target_id:
            raise ValueError("memory_item candidate id must match target_id")
        required = {"type", "status", "scope", "content", "source"}
        missing = sorted(key for key in required if not data.get(key))
        if missing:
            raise ValueError(f"memory_item candidate missing fields: {', '.join(missing)}")
        return {"loaded_by": "MemoryStore", "memory_type": str(data["type"])}

    def apply(self, target_id: str, candidate: str) -> None:
        self.validate(target_id, candidate)
        self.store.restore_item(_json_object(candidate, "memory_item"))

    def rollback(self, target_id: str, before: str) -> None:
        if before:
            self.store.restore_item(_json_object(before, "memory_item"))
            self.store.reduce_confidence(target_id, delta=0.2)
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
        path = self._path(target_id)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def validate(self, target_id: str, candidate: str) -> dict[str, Any]:
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
        path = self._path(target_id)
        if before:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(before, encoding="utf-8")
        elif path.exists():
            path.unlink()


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


class _RunExecutionAdapter:
    descriptor = EvolutionTargetDescriptor(
        "run_execution",
        "Existing run lifecycle fields consumed by the control plane.",
        "execution",
    )

    def __init__(self, home: Path):
        self.store = RunStore(home)

    def read(self, target_id: str) -> str:
        run = self.store.get(target_id)
        if run is None:
            return ""
        data = {
            key: getattr(run, key)
            for key in (
                "phase",
                "governance",
                "acceptance",
                "resolution",
                "result_summary",
                "error",
            )
        }
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    def validate(self, target_id: str, candidate: str) -> dict[str, Any]:
        if self.store.get(target_id) is None:
            raise ValueError("run_execution target must already exist")
        data = _json_object(candidate, "run_execution")
        required = ("phase", "governance", "acceptance", "resolution")
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise ValueError(
                "run_execution candidate missing lifecycle fields: " + ", ".join(missing)
            )
        return {"loaded_by": "RunStore", "lifecycle_fields": list(required)}

    def apply(self, target_id: str, candidate: str) -> None:
        self.validate(target_id, candidate)
        data = _json_object(candidate, "run_execution")
        self.store.update_run(
            target_id,
            phase=data["phase"],
            governance=data["governance"],
            acceptance=data["acceptance"],
            resolution=data["resolution"],
            result_summary=str(data.get("result_summary") or ""),
            error=str(data.get("error") or ""),
        )

    def rollback(self, target_id: str, before: str) -> None:
        if before:
            self.apply(target_id, before)


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
