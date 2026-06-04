from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .spec_loader import load_spec


@dataclass(frozen=True)
class HookSpec:
    name: str
    event: str
    description: str
    decision_schema: dict[str, Any]
    source: str = "built-in"
    decision: str = "observe"
    reason: str = ""
    facts: dict[str, Any] | None = None
    match: dict[str, Any] | None = None


@dataclass(frozen=True)
class HookEvent:
    event: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class HookDecision:
    hook: str
    event: str
    decision: str
    reason: str = ""
    facts: dict[str, Any] | None = None


class HookRegistry:
    """Lifecycle hook table for agent OS gates and observers."""

    def __init__(self, home: Path):
        self.home = home
        self._specs = _load_hook_specs(home)

    def list_specs(self) -> list[HookSpec]:
        return sorted(self._specs, key=lambda spec: (spec.event, spec.name))

    def list_facts(self) -> dict[str, Any]:
        specs = self.list_specs()
        return {
            "category": "hooks",
            "definition": "lifecycle gates and observers that return structured decisions or facts",
            "hooks": [asdict(spec) for spec in specs],
            "count": len(specs),
        }

    def run(self, event: HookEvent) -> list[HookDecision]:
        decisions = []
        payload_keys = sorted(event.payload)
        for spec in self._specs:
            if spec.event != event.event:
                continue
            if not _payload_matches(event.payload, spec.match or {}):
                continue
            decisions.append(
                HookDecision(
                    hook=spec.name,
                    event=event.event,
                    decision=spec.decision,
                    reason=spec.reason,
                    facts={"payload_keys": payload_keys, "source": spec.source} | (spec.facts or {}),
                )
            )
        return decisions


def _load_hook_specs(home: Path) -> list[HookSpec]:
    return _load_builtin_hook_specs() + _load_local_hook_specs(home)


def _load_builtin_hook_specs() -> list[HookSpec]:
    raw = load_spec("hooks.yaml") or []
    if not isinstance(raw, list):
        raise ValueError("hooks.yaml must contain a list")
    return [_hook_spec_from_mapping(item, default_source="built-in") for item in raw]


def _load_local_hook_specs(home: Path) -> list[HookSpec]:
    hook_dir = home / "hooks"
    if not hook_dir.exists():
        return []
    specs: list[HookSpec] = []
    for path in sorted(hook_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        entries = raw.get("hooks") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            raise ValueError(f"{path} must contain a hook list")
        source = f"local:{path.relative_to(home)}"
        specs.extend(_hook_spec_from_mapping(item, default_source=source) for item in entries)
    return specs


def _hook_spec_from_mapping(item: Any, *, default_source: str) -> HookSpec:
    if not isinstance(item, dict):
        raise ValueError("hook entries must be mappings")
    decision = str(item.get("decision") or "observe").strip().lower()
    if decision not in {"observe", "block"}:
        raise ValueError(f"unsupported hook decision: {decision}")
    facts = item.get("facts")
    if facts is not None and not isinstance(facts, dict):
        raise ValueError("hook facts must be a mapping")
    match = item.get("match")
    if match is not None and not isinstance(match, dict):
        raise ValueError("hook match must be a mapping")
    return HookSpec(
        name=str(item["name"]),
        event=str(item["event"]),
        description=str(item.get("description") or ""),
        decision_schema=dict(item.get("decision_schema") or {"type": "object"}),
        source=str(item.get("source") or default_source),
        decision=decision,
        reason=str(item.get("reason") or ""),
        facts=facts,
        match=match,
    )


def _payload_matches(payload: dict[str, Any], match: dict[str, Any]) -> bool:
    for key, expected in match.items():
        actual = payload.get(str(key))
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True
