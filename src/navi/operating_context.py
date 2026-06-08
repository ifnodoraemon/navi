from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .tools import ToolSpec


PERMISSION_ORDER = {
    "read": 0,
    "prepare": 1,
    "write": 2,
}


@dataclass(frozen=True)
class OperatingContext:
    """Per-surface OS context for prompt, skill, and syscall visibility."""

    home: Path
    source: str = "local"
    peer_id: str = ""
    sender_id: str = ""
    permission_ceiling: str = "write"
    skill_permission_ceiling: str = "read"
    workspace: str = ""
    role: str = ""
    objective: str = ""
    prompt_layers: tuple[str, ...] = (
        "identity",
        "runtime",
        "authorization",
        "memory",
        "skills",
        "style",
    )

    def allows_permission(self, permission: str) -> bool:
        return permission_allows(permission, self.permission_ceiling)

    def allows_prompt_layer(self, layer: str) -> bool:
        return layer in self.prompt_layers


@dataclass(frozen=True)
class PromptLayer:
    name: str
    content: str
    minimum_permission: str = "read"


def normalize_permission(value: object, *, default: str = "read") -> str:
    permission = str(value or "").strip().lower()
    return permission if permission in PERMISSION_ORDER else default


def permission_allows(required: str, ceiling: str) -> bool:
    required_level = PERMISSION_ORDER[normalize_permission(required)]
    ceiling_level = PERMISSION_ORDER[normalize_permission(ceiling)]
    return required_level <= ceiling_level


def filter_specs_by_permission(specs: Iterable[ToolSpec], ceiling: str) -> list[ToolSpec]:
    return [spec for spec in specs if permission_allows(spec.permission, ceiling)]
