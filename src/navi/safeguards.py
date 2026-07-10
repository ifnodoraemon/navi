from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .specs_data import CAPABILITY_SAFEGUARDS_SPEC
from .tools import ToolSpec


@dataclass(frozen=True)
class CapabilitySafeguard:
    risk_class: str
    sensitive_contexts: tuple[str, ...]
    confirmation_required: bool
    reason_code: str

    def to_facts(self) -> dict:
        return {
            "risk_class": self.risk_class,
            "sensitive_contexts": list(self.sensitive_contexts),
            "confirmation_required": self.confirmation_required,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class CapabilityRiskAssessment:
    """Risk facts for one concrete capability call.

    Static tool metadata declares the default boundary.  The call assessment
    adds the actual effect and scope so approval is tied to what will happen,
    rather than to a binary-name denylist.
    """

    risk_class: str
    sensitive_contexts: tuple[str, ...]
    confirmation_required: bool
    reason_code: str
    evidence: dict[str, Any]

    def to_facts(self) -> dict[str, Any]:
        return {
            "risk_class": self.risk_class,
            "sensitive_contexts": list(self.sensitive_contexts),
            "confirmation_required": self.confirmation_required,
            "reason_code": self.reason_code,
            "evidence": dict(self.evidence),
        }


def classify_capability(spec: ToolSpec) -> CapabilitySafeguard:
    raw = _declared_safeguard(spec)
    return CapabilitySafeguard(
        risk_class=str(raw.get("risk_class") or "low"),
        sensitive_contexts=tuple(str(item) for item in raw.get("sensitive_contexts") or []),
        confirmation_required=bool(raw.get("confirmation_required", False)),
        reason_code=str(raw.get("reason_code") or _safeguard_reason_code(spec)),
    )


def capability_safeguard_facts(spec: ToolSpec) -> dict:
    return classify_capability(spec).to_facts()


def assess_capability_call(
    spec: ToolSpec,
    args: dict[str, Any] | None,
    *,
    workspace: str = "",
) -> CapabilityRiskAssessment:
    """Classify a concrete call without permanently blocking the operation."""

    declared = classify_capability(spec)
    call_args = args or {}
    evidence: dict[str, Any] = {
        "tool": spec.name,
        "side_effect_scope": spec.side_effect_policy.scope,
        "side_effect_mode": spec.side_effect_policy.mode,
    }
    risk_class = declared.risk_class
    confirmation_required = declared.confirmation_required
    reason_code = declared.reason_code
    contexts = list(declared.sensitive_contexts)

    if spec.side_effect_policy.scope == "external" and declared.confirmation_required:
        risk_class = "high"
        confirmation_required = True
        reason_code = "external_side_effect_requires_approval"
        contexts.append("external_side_effect")
        evidence["external_effect"] = True

    if spec.name == "file.write":
        path_facts = _file_write_risk_facts(call_args, workspace=workspace)
        evidence.update(path_facts)
        if path_facts["outside_workspace"]:
            risk_class = "high"
            confirmation_required = True
            reason_code = "outside_workspace_write_requires_approval"
            contexts.append("outside_workspace")
        if path_facts["destructive_overwrite"]:
            risk_class = "high"
            confirmation_required = True
            reason_code = "destructive_file_overwrite_requires_approval"
            contexts.append("destructive_overwrite")
    elif spec.name == "shell.run":
        command = call_args.get("command")
        argv = [str(item) for item in command] if isinstance(command, list) else []
        evidence.update(
            {
                "binary": Path(argv[0]).name if argv else "",
                "argument_count": max(0, len(argv) - 1),
                "effect_is_declared": False,
            }
        )
        risk_class = "high"
        confirmation_required = True
        reason_code = "opaque_shell_effect_requires_approval"
        contexts.append("opaque_process_effect")
    elif spec.name == "http.fetch":
        network_facts = _http_fetch_risk_facts(call_args)
        evidence.update(network_facts)
        if network_facts["private_or_local_target"]:
            risk_class = "high"
            confirmation_required = True
            reason_code = "private_network_access_requires_approval"
            contexts.append("private_network")
        elif (
            network_facts["writes_remote_state"]
            or network_facts["credentialed_request"]
            or network_facts["has_body"]
        ):
            risk_class = "high"
            confirmation_required = True
            reason_code = "external_network_side_effect_requires_approval"
            contexts.append("external_side_effect")

    return CapabilityRiskAssessment(
        risk_class=risk_class,
        sensitive_contexts=tuple(dict.fromkeys(contexts)),
        confirmation_required=confirmation_required,
        reason_code=reason_code,
        evidence=evidence,
    )


def _file_write_risk_facts(args: dict[str, Any], *, workspace: str) -> dict[str, Any]:
    raw_path = str(args.get("path") or "").strip()
    requested_path = Path(raw_path).expanduser() if raw_path else Path(".")
    workspace_path = Path(workspace).expanduser().resolve() if workspace else None
    if not requested_path.is_absolute() and workspace_path is not None:
        requested_path = workspace_path / requested_path
    resolved_path = requested_path.resolve()
    outside_workspace = bool(
        workspace_path is not None
        and resolved_path != workspace_path
        and workspace_path not in resolved_path.parents
    )
    exists = resolved_path.is_file()
    before_size = resolved_path.stat().st_size if exists else 0
    requested_size = len(str(args.get("content") or "").encode("utf-8"))
    mode = str(args.get("mode") or "overwrite").strip().lower()
    destructive_overwrite = mode == "overwrite" and exists and requested_size < before_size
    return {
        "path": str(resolved_path),
        "mode": mode,
        "outside_workspace": outside_workspace,
        "target_exists": exists,
        "before_size": before_size,
        "requested_size": requested_size,
        "destructive_overwrite": destructive_overwrite,
    }


def _http_fetch_risk_facts(args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url") or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    method = str(args.get("method") or "GET").strip().upper()
    headers = args.get("headers") if isinstance(args.get("headers"), dict) else {}
    credentialed = any(
        str(key).strip().lower() in {"authorization", "cookie", "proxy-authorization"}
        for key in headers
    )
    private_or_local = (
        host in {"localhost", "localhost.localdomain", "metadata.google.internal"}
        or host.endswith((".localhost", ".local", ".internal"))
        or (bool(host) and "." not in host)
    )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        private_or_local = bool(
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
    return {
        "url_scheme": parsed.scheme,
        "target_host": host,
        "method": method,
        "private_or_local_target": private_or_local,
        "writes_remote_state": method not in {"GET", "HEAD", "OPTIONS"},
        "credentialed_request": credentialed,
        "has_body": bool(args.get("body")),
    }


def _declared_safeguard(spec: ToolSpec) -> dict:
    policy = CAPABILITY_SAFEGUARDS_SPEC or {}
    tools = policy.get("tools") if isinstance(policy, dict) else {}
    if isinstance(tools, dict) and isinstance(tools.get(spec.name), dict):
        return dict(tools[spec.name])
    defaults = policy.get("defaults") if isinstance(policy, dict) else {}
    default_key = "write" if spec.mutates or spec.permission == "write" else spec.permission
    if isinstance(defaults, dict) and isinstance(defaults.get(default_key), dict):
        return dict(defaults[default_key])
    sensitive_contexts = ["local_state"] if spec.mutates else []
    if spec.side_effect_policy.scope == "external":
        sensitive_contexts = [*sensitive_contexts, "external_side_effect"]
    return {
        "risk_class": "high" if spec.mutates else "low",
        "sensitive_contexts": sensitive_contexts,
        "confirmation_required": bool(spec.mutates),
    }


def _safeguard_reason_code(spec: ToolSpec) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", spec.name.lower()).strip("_")
    return f"capability_safeguard_{normalized or 'default'}"


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

_PERSONAL_DATA_PATTERNS: list[tuple[str, str]] = [
    (
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[REDACTED_EMAIL]",
    ),
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "[REDACTED_PHONE]"),
]


def redact_secrets(text: str) -> str:
    if not isinstance(text, str):
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text


def redact_personal_data(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = redact_secrets(text)
    for pattern, replacement in _PERSONAL_DATA_PATTERNS:
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
