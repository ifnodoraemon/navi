from __future__ import annotations

import ipaddress
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .permission_contract import PERMISSION_ORDER

if TYPE_CHECKING:
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


_LOCAL_READ_ONLY_COMMANDS = frozenset(
    {
        "arch",
        "cat",
        "df",
        "du",
        "free",
        "grep",
        "head",
        "id",
        "ls",
        "lscpu",
        "nproc",
        "pgrep",
        "pidof",
        "printenv",
        "ps",
        "pstree",
        "pwd",
        "readlink",
        "realpath",
        "rg",
        "ss",
        "stat",
        "tail",
        "uname",
        "uptime",
        "wc",
        "who",
        "whoami",
    }
)
_HOST_PROCESS_OBSERVATION_COMMANDS = frozenset({"pgrep", "pidof", "ps", "pstree"})
_GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {"blame", "diff", "grep", "log", "ls-files", "ls-tree", "rev-parse", "show", "status"}
)
_SYSTEMCTL_READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "cat",
        "is-active",
        "is-enabled",
        "is-failed",
        "list-dependencies",
        "list-unit-files",
        "list-units",
        "show",
        "status",
    }
)
_NETWORK_READ_ONLY_COMMANDS = {
    "curl": frozenset({"--head", "-I"}),
    "docker": frozenset({"images", "info", "inspect", "logs", "ps", "version"}),
    "kubectl": frozenset({"describe", "get", "logs", "top"}),
}


_DEFAULT_SAFEGUARDS = {
    "read": ("low", (), False, "default_read_safeguard"),
    "network": ("medium", ("network",), False, "default_network_safeguard"),
    "prepare": ("medium", ("task_control",), False, "default_prepare_safeguard"),
    "write": ("high", ("local_state",), True, "default_write_safeguard"),
}


def shell_call_policy(args: dict[str, Any] | None) -> dict[str, Any]:
    """Return a fail-closed effect classification for one argv-only shell call."""
    call_args = args or {}
    command = call_args.get("command")
    argv = [str(item) for item in command] if isinstance(command, list) else []
    binary = Path(argv[0]).name if argv else ""
    permission = "write"
    reason = "opaque_or_effectful_command"
    observation_scope = "workspace_sandbox"
    if argv and not bool(call_args.get("allocate_pty")):
        if binary in _LOCAL_READ_ONLY_COMMANDS:
            permission, reason = "read", "declared_local_read_only_command"
            if binary in _HOST_PROCESS_OBSERVATION_COMMANDS:
                observation_scope = "host_process_table"
        elif binary == "hostname" and not _first_positional(argv[1:]):
            permission, reason = "read", "hostname_read_only_query"
        elif binary == "find" and not _find_has_effectful_action(argv[1:]):
            permission, reason = "read", "find_without_effectful_action"
        elif (
            binary == "git"
            and _first_positional(argv[1:]) in _GIT_READ_ONLY_SUBCOMMANDS
            and not _git_has_output_file(argv[1:])
        ):
            permission, reason = "read", "declared_git_read_only_subcommand"
        elif (
            binary == "systemctl"
            and _first_positional(argv[1:]) in _SYSTEMCTL_READ_ONLY_SUBCOMMANDS
        ):
            permission, reason = "read", "declared_systemctl_read_only_subcommand"
        elif binary in {"curl", "docker", "kubectl"}:
            subcommand = _first_positional(argv[1:])
            if binary == "curl":
                method = _curl_method(argv[1:])
                if (
                    method in {"GET", "HEAD"}
                    and not _curl_has_output_file(argv[1:])
                    and not _curl_has_credentials(argv[1:])
                ):
                    permission, reason = "network", "curl_read_only_request"
            elif subcommand in _NETWORK_READ_ONLY_COMMANDS[binary]:
                permission, reason = "network", f"declared_{binary}_read_only_subcommand"
    return {
        "binary": binary,
        "argument_count": max(0, len(argv) - 1),
        "required_permission": permission,
        "effect_is_declared": permission in {"read", "network"},
        "effect_classification": reason,
        "observation_scope": observation_scope,
    }


def required_permission_for_call(spec: ToolSpec, args: dict[str, Any] | None) -> str:
    if spec.permission_policy == "shell_argv":
        return str(shell_call_policy(args)["required_permission"])
    if spec.permission_policy == "agent_operation":
        operation = str((args or {}).get("operation") or "").strip().lower()
        return "prepare" if operation in {"spawn", "message", "cancel"} else "read"
    if spec.permission_policy == "argument_map":
        selected = str((args or {}).get(spec.argument_permission_field) or "").strip()
        mapped = dict(spec.argument_permissions).get(selected, "write")
        return max((spec.permission, mapped), key=PERMISSION_ORDER.__getitem__)
    return spec.permission


def call_mutates(spec: ToolSpec, args: dict[str, Any] | None) -> bool:
    """Return whether this concrete call can change state.

    ``ToolSpec.mutates`` is a catalog-level upper bound.  Capabilities whose
    effect depends on an argument need a call-level answer so read-only
    observations do not enter the effect journal or require a mutating audit.
    """

    if not spec.mutates:
        return False
    call_args = args or {}
    if spec.permission_policy == "shell_argv":
        return not bool(shell_call_policy(call_args)["effect_is_declared"])
    if spec.permission_policy == "agent_operation":
        operation = str(call_args.get("operation") or "").strip().lower()
        return operation not in {"list", "state", "collect"}
    if spec.permission_policy == "argument_map":
        return required_permission_for_call(spec, call_args) == "write"
    return True


def prepare_capability_call(
    spec: ToolSpec,
    args: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Resolve external resources once before risk assessment and approval binding."""

    prepared = dict(args or {})
    if spec.risk_policy != "http_request":
        return prepared, None
    url = str(prepared.get("url") or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return prepared, {
            "error_reason": "invalid_network_target",
            "target_host": host,
            "retryable": False,
        }
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return prepared, {
            "error_reason": "target_resolution_failed",
            "target_host": host,
            "error_type": type(exc).__name__,
            "retryable": True,
        }
    addresses = sorted({str(item[4][0]) for item in infos if item and item[4]})
    if not addresses:
        return prepared, {
            "error_reason": "target_resolution_failed",
            "target_host": host,
            "retryable": True,
        }
    prepared["_resolved_addresses"] = addresses
    prepared["_resolved_port"] = port
    return prepared, None


def workspace_boundary_facts(
    spec: ToolSpec,
    args: dict[str, Any] | None,
    *,
    workspace: str,
) -> dict[str, Any]:
    """Evaluate declared path scope before approval or execution."""

    if spec.workspace_policy == "none":
        return {"allowed": True, "policy": "none"}
    root = Path(workspace).expanduser().resolve()
    call_args = args or {}
    fields = spec.workspace_fields
    if spec.workspace_policy == "sandbox":
        fields = fields or ("cwd",)
    checked: list[dict[str, str]] = []
    for field in fields:
        raw = str(call_args.get(field) or "").strip()
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        checked.append({"field": field, "path": str(resolved)})
        if resolved != root and root not in resolved.parents:
            return {
                "allowed": False,
                "policy": spec.workspace_policy,
                "workspace": str(root),
                "field": field,
                "path": str(resolved),
            }
    return {
        "allowed": True,
        "policy": spec.workspace_policy,
        "workspace": str(root),
        "checked": checked,
    }


def _first_positional(args: list[str]) -> str:
    return next((item for item in args if item and not item.startswith("-")), "")


def _find_has_effectful_action(args: list[str]) -> bool:
    return any(
        item
        in {
            "-delete",
            "-exec",
            "-execdir",
            "-fprint",
            "-fprint0",
            "-fprintf",
            "-fls",
            "-ok",
            "-okdir",
        }
        for item in args
    )


def _curl_method(args: list[str]) -> str:
    for index, item in enumerate(args):
        if item in {"-X", "--request"} and index + 1 < len(args):
            return args[index + 1].upper()
    if any(
        item
        in {"-d", "--data", "--data-ascii", "--data-binary", "--data-raw", "--json", "-F", "--form"}
        or item.startswith("--data-")
        for item in args
    ):
        return "POST"
    if any(item in {"-T", "--upload-file"} for item in args):
        return "PUT"
    return "HEAD" if any(item in {"-I", "--head"} for item in args) else "GET"


def _curl_has_output_file(args: list[str]) -> bool:
    return any(
        item in {"-o", "--output", "-O", "--remote-name", "--remote-header-name"} for item in args
    )


def _curl_has_credentials(args: list[str]) -> bool:
    if any(item in {"-u", "--user", "-b", "--cookie", "--oauth2-bearer"} for item in args):
        return True
    return any(
        item in {"-H", "--header"}
        and index + 1 < len(args)
        and args[index + 1]
        .strip()
        .lower()
        .startswith(("authorization:", "cookie:", "proxy-authorization:"))
        for index, item in enumerate(args)
    )


def _git_has_output_file(args: list[str]) -> bool:
    return any(item == "--output" or item.startswith("--output=") for item in args)


def classify_capability(spec: ToolSpec) -> CapabilitySafeguard:
    raw = _declared_safeguard(spec)
    return CapabilitySafeguard(
        risk_class=str(raw.get("risk_class") or "low"),
        sensitive_contexts=tuple(str(item) for item in raw.get("sensitive_contexts") or []),
        confirmation_required=bool(raw.get("confirmation_required", False)),
        reason_code=str(raw["reason_code"]),
    )


def capability_safeguard_facts(spec: ToolSpec) -> dict:
    facts = classify_capability(spec).to_facts()
    if spec.permission_policy != "static":
        facts["call_dependent_permission"] = True
        facts["static_policy"] = "unknown_or_mutating_effect_fails_closed"
    return facts


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

    if spec.risk_policy == "workspace_file_write":
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
    elif spec.risk_policy == "shell_argv":
        shell_policy = shell_call_policy(call_args)
        evidence.update(shell_policy)
        call_permission = str(shell_policy["required_permission"])
        if call_permission == "read":
            risk_class, confirmation_required, reason_code, contexts = (
                "medium",
                False,
                "declared_shell_read_only",
                ["terminal", "local_read"],
            )
        elif call_permission == "network":
            risk_class, confirmation_required, reason_code, contexts = (
                "medium",
                False,
                "declared_shell_network_read",
                ["terminal", "network"],
            )
        else:
            risk_class, confirmation_required, reason_code = (
                "high",
                True,
                "opaque_shell_effect_requires_approval",
            )
            contexts.append("opaque_process_effect")
    elif spec.risk_policy == "agent_operation":
        operation = str(call_args.get("operation") or "").strip().lower()
        evidence["operation"] = operation
        if operation in {"list", "state", "collect"}:
            risk_class = "low"
            confirmation_required = False
            reason_code = "agent_read_operation"
            contexts = ["task_control"]
        elif operation in {"spawn", "message"}:
            risk_class = "medium"
            confirmation_required = False
            reason_code = "agent_child_operation"
            contexts = ["task_control"]
        else:
            risk_class = "high"
            confirmation_required = True
            reason_code = "agent_cancel_requires_approval"
            contexts = ["task_control", "destructive_control"]
    elif spec.risk_policy == "argument_permission":
        selected = str(call_args.get(spec.argument_permission_field) or "").strip()
        required_permission = required_permission_for_call(spec, call_args)
        evidence.update(
            {
                "argument_field": spec.argument_permission_field,
                "argument_value": selected,
                "required_permission": required_permission,
            }
        )
        if required_permission == "write":
            risk_class = "high"
            confirmation_required = True
            reason_code = "argument_selected_write_requires_approval"
            contexts = ["external_side_effect"]
        elif required_permission == "prepare":
            risk_class = "medium"
            confirmation_required = False
            reason_code = "argument_selected_prepare"
            contexts = ["task_control"]
        elif required_permission == "network":
            risk_class = "medium"
            confirmation_required = False
            reason_code = "argument_selected_network_read"
            contexts = ["network"]
        else:
            risk_class = "low"
            confirmation_required = False
            reason_code = "argument_selected_read"
            contexts = []
    elif spec.risk_policy == "http_request":
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
    return {
        "path": str(resolved_path),
        "mode": mode,
        "outside_workspace": outside_workspace,
        "target_exists": exists,
        "before_size": before_size,
        "requested_size": requested_size,
        "destructive_overwrite": mode == "overwrite" and exists and requested_size < before_size,
    }


def _http_fetch_risk_facts(args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url") or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    method = str(args.get("method") or "GET").strip().upper()
    raw_headers = args.get("headers")
    headers = raw_headers if isinstance(raw_headers, dict) else {}
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
    resolved_addresses = [
        str(item) for item in args.get("_resolved_addresses", []) if str(item).strip()
    ]
    resolved_private = []
    for item in resolved_addresses:
        try:
            resolved = ipaddress.ip_address(item)
        except ValueError:
            resolved_private.append(item)
            continue
        if (
            resolved.is_private
            or resolved.is_loopback
            or resolved.is_link_local
            or resolved.is_multicast
            or resolved.is_reserved
            or resolved.is_unspecified
        ):
            resolved_private.append(item)
    private_or_local = private_or_local or bool(resolved_private)
    return {
        "url_scheme": parsed.scheme,
        "target_host": host,
        "method": method,
        "private_or_local_target": private_or_local,
        "resolved_addresses": resolved_addresses,
        "private_resolved_addresses": resolved_private,
        "writes_remote_state": method not in {"GET", "HEAD", "OPTIONS"},
        "credentialed_request": credentialed,
        "has_body": bool(args.get("body")),
    }


def _declared_safeguard(spec: ToolSpec) -> dict:
    default_key = "write" if spec.mutates or spec.permission == "write" else spec.permission
    default_risk, default_contexts, default_confirmation, default_reason = _DEFAULT_SAFEGUARDS[
        default_key
    ]
    sensitive_contexts = spec.sensitive_contexts or default_contexts
    if spec.side_effect_policy.scope == "external":
        sensitive_contexts = (*sensitive_contexts, "external_side_effect")
    return {
        "risk_class": spec.risk_class or default_risk,
        "sensitive_contexts": sensitive_contexts,
        "confirmation_required": (
            default_confirmation
            if spec.confirmation_required is None
            else spec.confirmation_required
        ),
        "reason_code": spec.risk_reason_code or default_reason,
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

_PERSONAL_DATA_PATTERNS: list[tuple[str, str]] = [
    (
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[REDACTED_EMAIL]",
    ),
    (r"(?<!\d)1[3-9](?:[\s-]?\d){9}(?!\d)", "[REDACTED_PHONE]"),
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
        "approval_code",
        "authorization",
        "bearer",
        "client_secret",
        "password",
        "private_key",
        "secret",
        "session_token",
        "token",
        "verification_code",
    }
)


def _is_secret_field_name(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in _REDACT_FIELD_NAMES or normalized.endswith(
        ("_api_key", "-api-key", "_secret", "_token")
    )


_PERSONAL_FIELD_NAMES = frozenset(
    {
        "address",
        "attendees",
        "bcc",
        "body",
        "cc",
        "display_name",
        "email",
        "email_address",
        "emails",
        "location",
        "notes",
        "other_peer_id",
        "other_sender_id",
        "peer_id",
        "phone",
        "phone_number",
        "phones",
        "recipients",
        "reply_to",
        "sender_id",
        "subject",
        "telephone",
        "title",
        "to",
    }
)

_APPROVAL_PRIVATE_FIELD_NAMES = frozenset({"content", "message", "objective", "prompt", "query"})


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
            if _is_secret_field_name(key_lower):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_secrets_deep(nested)
        return redacted
    if isinstance(value, list):
        return [redact_secrets_deep(item) for item in value]
    return value


def redact_personal_data_deep(value: Any) -> Any:
    """Recursively redact secrets and contact identifiers before audit persistence."""
    if isinstance(value, str):
        return redact_personal_data(value)
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if _is_secret_field_name(normalized):
                redacted[key] = "[REDACTED]"
            elif normalized in _PERSONAL_FIELD_NAMES and item:
                redacted[key] = "[REDACTED_PERSONAL_DATA]"
            else:
                redacted[key] = redact_personal_data_deep(item)
        return redacted
    if isinstance(value, list):
        return [redact_personal_data_deep(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_personal_data_deep(item) for item in value)
    return value


def canonical_approval_args_json(value: Any, *, home: Path) -> str:
    """Canonicalize exact approval arguments without persisting sensitive values."""
    filtered = (
        {
            key: item
            for key, item in value.items()
            if key not in {"_thought", "thought", "reasoning", "rationale"}
        }
        if isinstance(value, dict)
        else (value or {})
    )
    key = _approval_hmac_key(home)
    protected = _protect_approval_value(filtered, key=key)
    return json.dumps(protected, ensure_ascii=False, sort_keys=True)


def _protect_approval_value(value: Any, *, key: bytes, field_name: str = "") -> Any:
    normalized = field_name.strip().lower()
    if (
        _is_secret_field_name(normalized)
        or normalized in _PERSONAL_FIELD_NAMES
        or normalized in _APPROVAL_PRIVATE_FIELD_NAMES
    ):
        return {"$approval_hmac": _approval_hmac(value, key=key)}
    if isinstance(value, dict):
        return {
            str(item_key): _protect_approval_value(
                item,
                key=key,
                field_name=str(item_key),
            )
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_protect_approval_value(item, key=key) for item in value]
    if isinstance(value, str):
        if redact_personal_data(value) != value:
            return {"$approval_hmac": _approval_hmac(value, key=key)}
        return value
    return value


def _approval_hmac(value: Any, *, key: bytes) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _approval_hmac_key(home: Path) -> bytes:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "approval_hmac.key"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secrets.token_bytes(32))
    key = path.read_bytes()
    if len(key) < 32:
        raise RuntimeError("approval HMAC key is invalid")
    return key
