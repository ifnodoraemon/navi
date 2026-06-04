from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .spec_loader import load_spec


@dataclass(frozen=True)
class HookSpec:
    name: str
    event: str
    description: str
    decision_schema: dict[str, Any]
    source: str = "built-in"


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
        self._specs = _load_hook_specs()

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
            decisions.append(
                HookDecision(
                    hook=spec.name,
                    event=event.event,
                    decision="observe",
                    facts={"payload_keys": payload_keys},
                )
            )
        return decisions


def _load_hook_specs() -> list[HookSpec]:
    raw = load_spec("hooks.yaml") or []
    if not isinstance(raw, list):
        raise ValueError("hooks.yaml must contain a list")
    specs = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("hooks.yaml entries must be mappings")
        specs.append(
            HookSpec(
                name=str(item["name"]),
                event=str(item["event"]),
                description=str(item.get("description") or ""),
                decision_schema=dict(item.get("decision_schema") or {"type": "object"}),
                source=str(item.get("source") or "built-in"),
            )
        )
    return specs
