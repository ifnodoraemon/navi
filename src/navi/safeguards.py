from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

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


_SECRET_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)(bearer\s+)[A-Za-z0-9\-\._~+/]+", r"\1[REDACTED]"),
    (r"(?i)(api[_-]?key[\"'\s:=]+)[A-Za-z0-9\-\._~+/]+", r"\1[REDACTED]"),
    (r"(?i)(password[\"'\s:=]+)[^\s&\"']+", r"\1[REDACTED]"),
    (r"(?i)(secret[\"'\s:=]+)[^\s&\"']+", r"\1[REDACTED]"),
    (r"(?i)(token[\"'\s:=]+)[A-Za-z0-9\-\._~+/]+", r"\1[REDACTED]"),
    # Generic ``Authorization: <scheme> <value>`` header.
    (r"(?i)(authorization:\s*(bearer\s+)?)[A-Za-z0-9\-\._~+/=]+", r"\1[REDACTED]"),
    # PEM-encoded private keys (RSA, EC, OPENSSH, ...). Defense in depth
    # (principle 13/16): these are well-known secret formats that must not
    # leak through tool args/facts/audit logs even without a keyword prefix.
    (
        r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        "[REDACTED]",
    ),
    # Connection strings with embedded credentials (RFC-3986 userinfo):
    # ``scheme://user:pass@host``. The password sits between ``:`` and ``@``
    # so lookarounds redact only the credential, preserving delimiters.
    (r"(?<=:)[^@\s:]+(?=@)", "[REDACTED]"),
]


def redact_secrets(text: str) -> str:
    if not isinstance(text, str):
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text


# FP-4: secret-bearing field names whose values must be redacted regardless of
# where they appear in a nested structure (args, facts, HTTP bodies). This is a
# value-level allowlist complement to the keyword-prefix ``_SECRET_PATTERNS``.
_REDACT_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "client_secret",
        "password",
        "private_key",
        "secret",
        "session_token",
        "token",
    }
)


def redact_secrets_deep(value: Any) -> Any:
    """Recursively redact secrets inside nested dicts/lists/strings.

    FP-4/L8: ``redact_secrets`` runs against a flattened JSON string, so
    secrets in nested objects whose keys don't match a keyword-prefix pattern
    slip through. This walker redacts at the value level: any string leaf is
    passed through ``redact_secrets``, and any dict value whose lowercased key
    is a known secret-bearing field name is fully redacted."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_lower = str(key).lower()
            if key_lower in _REDACT_FIELD_NAMES:
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_secrets_deep(nested)
        return redacted
    if isinstance(value, list):
        return [redact_secrets_deep(item) for item in value]
    return value
