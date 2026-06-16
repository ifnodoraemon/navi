from __future__ import annotations

from dataclasses import dataclass

from .specs_data import CAPABILITY_SAFEGUARDS_SPEC
from .tools import ToolSpec


@dataclass(frozen=True)
class CapabilitySafeguard:
    risk_class: str
    sensitive_contexts: tuple[str, ...]
    confirmation_required: bool
    reason: str

    def to_facts(self) -> dict:
        return {
            "risk_class": self.risk_class,
            "sensitive_contexts": list(self.sensitive_contexts),
            "confirmation_required": self.confirmation_required,
            "reason": self.reason,
        }


def classify_capability(spec: ToolSpec) -> CapabilitySafeguard:
    raw = _declared_safeguard(spec)
    return CapabilitySafeguard(
        risk_class=str(raw.get("risk_class") or "low"),
        sensitive_contexts=tuple(str(item) for item in raw.get("sensitive_contexts") or []),
        confirmation_required=bool(raw.get("confirmation_required", False)),
        reason=str(raw.get("reason") or ""),
    )


def capability_safeguard_facts(spec: ToolSpec) -> dict:
    return classify_capability(spec).to_facts()


def _declared_safeguard(spec: ToolSpec) -> dict:
    policy = CAPABILITY_SAFEGUARDS_SPEC or {}
    tools = policy.get("tools") if isinstance(policy, dict) else {}
    if isinstance(tools, dict) and isinstance(tools.get(spec.name), dict):
        return dict(tools[spec.name])
    defaults = policy.get("defaults") if isinstance(policy, dict) else {}
    default_key = "write" if spec.mutates or spec.permission == "write" else spec.permission
    if isinstance(defaults, dict) and isinstance(defaults.get(default_key), dict):
        return dict(defaults[default_key])
    return {
        "risk_class": "high" if spec.mutates else "low",
        "sensitive_contexts": ["local_state"] if spec.mutates else [],
        "confirmation_required": bool(spec.mutates),
        "reason": "Fallback safeguard for capability without declared metadata.",
    }


import re

_SECRET_PATTERNS = [
    r"(?i)(bearer\s+)[A-Za-z0-9\-\._~+/]+",
    r"(?i)(api[_-]?key[\"'\s:=]+)[A-Za-z0-9\-\._~+/]+",
    r"(?i)(password[\"'\s:=]+)[^\s&\"']+",
    r"(?i)(secret[\"'\s:=]+)[^\s&\"']+",
    r"(?i)(token[\"'\s:=]+)[A-Za-z0-9\-\._~+/]+",
]


def redact_secrets(text: str) -> str:
    if not isinstance(text, str):
        return text
    for pattern in _SECRET_PATTERNS:
        text = re.sub(pattern, r"\1[REDACTED]", text)
    return text
