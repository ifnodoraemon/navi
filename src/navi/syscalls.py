from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from .json_utils import json_schema_errors
from .operating_context import PERMISSION_ORDER
from .provider import (
    ChatMessage,
    ModelPool,
    ProviderHTTPError,
    ProviderResponseError,
    StructuredOutputError,
)
from .prompt_os import (
    assemble_planner_system_prompt,
    assemble_planner_tool_manifest,
    assemble_planner_turn_input,
)
from .text_utils import truncate_middle
from .tools import ToolSpec

PROVIDER_TRANSPORT_RETRY_AFTER_SECONDS = 15.0
PROVIDER_ERROR_MAX_CHARS = 1_000


@dataclass(frozen=True)
class ModelSyscall:
    tool: str
    permission: str = "read"
    args: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    confidence: float = 0.0
    reason: str = ""
    used_memory_ids: tuple[str, ...] = ()
    used_evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "permission": self.permission,
            "args": dict(self.args),
            "message": self.message,
            "confidence": self.confidence,
            "reason": self.reason,
            "used_memory_ids": list(self.used_memory_ids),
            "used_evidence_ids": list(self.used_evidence_ids),
        }


class ModelSyscallPlanner:
    def __init__(self, provider: ModelPool):
        self.provider = provider

    async def plan(
        self,
        text: str,
        *,
        tools: list[ToolSpec],
        conversation_context: str = "",
        runtime_facts: dict[str, Any] | None = None,
        permission_ceiling: str = "write",
        durable_constraints: str = "",
        memory_context: str = "",
    ) -> list[ModelSyscall]:
        turn_input = assemble_planner_turn_input(
            text,
            conversation_context=conversation_context,
            runtime_facts=runtime_facts,
            permission_ceiling=permission_ceiling,
            durable_constraints=durable_constraints,
            memory_context=memory_context,
        )
        response = await self.provider.complete_for(
            "planner",
            [
                ChatMessage(
                    "system",
                    "\n\n".join(
                        (
                            assemble_planner_system_prompt().render(),
                            assemble_planner_tool_manifest(tools).render(),
                        )
                    ),
                ),
                ChatMessage("user", turn_input.render()),
            ],
            output_schema=_syscall_output_schema(),
        )
        syscalls = self._parse_syscalls(response)
        if len(syscalls) != 1:
            raise StructuredOutputError(
                f"planner_must_return_exactly_one_syscall: count={len(syscalls)}"
            )
        for syscall in syscalls:
            matching_spec = next((spec for spec in tools if spec.name == syscall.tool), None)
            if matching_spec:
                if syscall.permission not in PERMISSION_ORDER:
                    raise StructuredOutputError(
                        f"planner selected an unknown permission: {syscall.permission}"
                    )
                schema_errors = json_schema_errors(syscall.args, matching_spec.input_schema)
                if schema_errors:
                    raise StructuredOutputError(
                        f"planner capability arguments schema mismatch: {'; '.join(schema_errors[:5])}"
                    )
        return syscalls

    @staticmethod
    def _parse_syscalls(response: str) -> list[ModelSyscall]:
        """Parse planner output into a list of syscalls.

        The provider's structured-output channel already validates the JSON
        envelope against ``_syscall_output_schema``.  This method unpacks the
        validated payload into ``ModelSyscall`` objects.  Any parse failure
        here is a provider contract violation and propagates as a hard error.
        """
        data = json.loads(response)
        raw_list = data["syscalls"]
        syscalls: list[ModelSyscall] = []
        for item in raw_list:
            parsed = ModelSyscallPlanner._parse_single(item)
            syscalls.append(parsed)
        if not syscalls:
            raise StructuredOutputError("planner 'syscalls' list was empty after parsing")
        return syscalls

    @staticmethod
    def _parse_single(data: Any) -> ModelSyscall:
        if not isinstance(data, dict):
            raise StructuredOutputError(
                "planner syscall entry was not an object despite the declared schema"
            )
        for key in ("tool", "permission", "args"):
            if key not in data:
                raise StructuredOutputError(
                    f"planner syscall entry is missing required field {key!r}"
                )
        tool = str(data["tool"]).strip()
        if not tool:
            raise StructuredOutputError("planner syscall entry has an empty 'tool'")
        args = dict(data["args"])
        message = str(args.get("message") or "")
        return ModelSyscall(
            tool=tool,
            permission=str(data["permission"]).strip(),
            args=args,
            message=message,
            confidence=_confidence(data.get("confidence")),
            reason=str(data.get("reason") or ""),
            used_memory_ids=_string_tuple(data.get("used_memory_ids")),
            used_evidence_ids=_string_tuple(data.get("used_evidence_ids")),
        )


def _syscall_output_schema() -> dict[str, Any]:
    return {
        "name": "planner_decision",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "syscalls": {
                    "type": "array",
                    "description": "Exactly one capability call selected from the current manifest.",
                    "minItems": 1,
                    "maxItems": 1,
                    "items": _single_syscall_schema(),
                },
            },
            "required": ["syscalls"],
            "additionalProperties": False,
        },
    }


def _single_syscall_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "tool": {
                "type": "string",
                "minLength": 1,
                "description": "Selected capability name from the current manifest.",
            },
            "permission": {"type": "string", "enum": ["read", "network", "prepare", "write"]},
            "args": {"type": "object", "description": "Arguments for the selected capability."},
            "confidence": {"type": "number"},
            "reason": {
                "type": "string",
                "description": "Optional model rationale for audit only; runtime does not consume it for routing.",
            },
            "used_memory_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Recalled memory ids this syscall decision actually depends on.",
            },
            "used_evidence_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Context evidence ids this syscall decision actually depends on.",
            },
        },
        "required": ["tool", "permission", "args"],
        "additionalProperties": False,
    }


def provider_failure_facts(exc: Exception) -> dict[str, Any]:
    """Project a provider failure into bounded transport-recovery facts."""
    structured_output_failure = isinstance(exc, StructuredOutputError)
    provider_call_failure = isinstance(
        exc,
        (ProviderHTTPError, ProviderResponseError, httpx.TransportError),
    )
    retryable = isinstance(exc, httpx.TransportError)
    retry_after_seconds = (
        PROVIDER_TRANSPORT_RETRY_AFTER_SECONDS if retryable else 0.0
    )
    status_code = 0
    if isinstance(exc, ProviderHTTPError):
        status_code = exc.status_code
        retryable = status_code in {408, 409, 425, 429} or status_code >= 500
        retry_after_seconds = (
            exc.retry_after_seconds
            if retryable and exc.retry_after_seconds > 0
            else PROVIDER_TRANSPORT_RETRY_AFTER_SECONDS if retryable else 0.0
        )
    error_text = str(exc).strip()
    if not error_text:
        error_text = repr(exc)
    error_text = str(exc).strip()
    if not error_text:
        # httpx transport errors (e.g. ReadError) frequently have an empty
        # ``str(exc)``; fall back to a non-empty representation so downstream
        # evidence, logs, and the planner all receive a usable reason.
        error_text = repr(exc).strip()
    facts: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "error": truncate_middle(error_text, PROVIDER_ERROR_MAX_CHARS),
        "provider_call_failure": provider_call_failure,
        "structured_output_failure": structured_output_failure,
        "retryable": retryable,
        "retry_after_seconds": retry_after_seconds,
    }
    if status_code:
        facts["status_code"] = status_code
    return facts


def _confidence(value: object) -> float:
    if not isinstance(value, (str, int, float)):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(parsed, 1.0))


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())
