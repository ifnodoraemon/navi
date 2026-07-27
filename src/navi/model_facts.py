"""Bounded, redacted facts for model-facing prompt blocks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .safeguards import redact_secrets, redact_secrets_deep
from .text_utils import truncate_middle


DEFAULT_MODEL_FACT_CHARS = 48_000
DEFAULT_MODEL_FACT_STRING_CHARS = 4_000
DEFAULT_MODEL_FACT_DEPTH = 5
DEFAULT_MODEL_FACT_ITEMS = 30


@dataclass
class _ProjectionBudget:
    remaining: int

    def take(self, value: str, *, limit: int) -> str:
        allowed = max(0, min(limit, self.remaining))
        if not allowed:
            return ""
        projected = truncate_middle(value, allowed)
        self.remaining = max(0, self.remaining - len(projected))
        return projected


def project_model_facts(
    value: Any,
    *,
    max_characters: int = DEFAULT_MODEL_FACT_CHARS,
    max_string_characters: int = DEFAULT_MODEL_FACT_STRING_CHARS,
    max_depth: int = DEFAULT_MODEL_FACT_DEPTH,
    max_items: int = DEFAULT_MODEL_FACT_ITEMS,
) -> Any:
    """Return a bounded, secret-redacted projection without mutating durable facts.

    Runtime evidence can include large file bodies, tool output, or external
    payloads.  This is the sole model-input projection boundary: callers keep
    the original evidence for verification and audit, while prompts receive a
    finite representation with the same shape where practical.
    """
    return _project(
        redact_secrets_deep(value),
        budget=_ProjectionBudget(max(0, max_characters)),
        depth=0,
        max_string_characters=max(1, max_string_characters),
        max_depth=max(1, max_depth),
        max_items=max(1, max_items),
    )


def _project(
    value: Any,
    *,
    budget: _ProjectionBudget,
    depth: int,
    max_string_characters: int,
    max_depth: int,
    max_items: int,
) -> Any:
    if isinstance(value, str):
        return budget.take(redact_secrets(value), limit=max_string_characters)
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= max_depth:
        return {"truncated": True, "type": type(value).__name__}
    if isinstance(value, dict):
        return {
            str(key): _project(
                nested,
                budget=budget,
                depth=depth + 1,
                max_string_characters=max_string_characters,
                max_depth=max_depth,
                max_items=max_items,
            )
            for key, nested in list(value.items())[:max_items]
        }
    if isinstance(value, list | tuple):
        return [
            _project(
                item,
                budget=budget,
                depth=depth + 1,
                max_string_characters=max_string_characters,
                max_depth=max_depth,
                max_items=max_items,
            )
            for item in list(value)[:max_items]
        ]
    if isinstance(value, set | frozenset):
        return [
            _project(
                item,
                budget=budget,
                depth=depth + 1,
                max_string_characters=max_string_characters,
                max_depth=max_depth,
                max_items=max_items,
            )
            for item in sorted(value, key=str)[:max_items]
        ]
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value), "content_omitted": True}
    return budget.take(str(value), limit=max_string_characters)
