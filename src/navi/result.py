"""Result type and error hierarchy for structured error handling.

Replaces ad-hoc ``try/except`` blocks that return ``CapabilityResult(ok=False)``
or ``ToolResult(ok=False)`` with a uniform pattern:

    @guarded
    async def invoke(self, args, *, permission, context) -> CapabilityResult:
        item = store.get(args["id"])  # may raise NotFound
        ...

The decorator catches :class:`NaviError` subclasses and converts them to
``CapabilityResult`` failures with the correct ``error_reason``.
"""

from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

logger = logging.getLogger("navi.result")

T = TypeVar("T")


# ----------------------------------------------------------------- error tree


class NaviError(Exception):
    """Base class for all Navi domain errors."""

    #: The ``error_reason`` string written to ``CapabilityResult``.
    reason: str = "error"

    #: Whether the error is terminal (stops the agent turn).
    terminal: bool = False


class SchemaMismatch(NaviError):
    """Input does not match the expected schema."""

    reason = "schema_mismatch"


class NotFound(NaviError):
    """A referenced entity (run, goal, workflow) does not exist."""

    reason = "not_found"


class PermissionDenied(NaviError):
    """The caller lacks the permission to perform the action."""

    reason = "permission_denied"
    terminal = True


class Conflict(NaviError):
    """The action conflicts with existing state (duplicate, wrong status)."""

    reason = "conflict"


class InternalError(NaviError):
    """An unexpected internal failure."""

    reason = "internal_error"


# --------------------------------------------------------------- Result monad


@dataclass(frozen=True, slots=True)
class Result(Generic[T]):
    """A simple Result type.

    Usage::

        def find_item(item_id: str) -> Result[Item]:
            item = store.get(item_id)
            if item is None:
                return Result.fail(NotFound(f"item {item_id} not found"))
            return Result.success(item)

    The ``error`` field is a :class:`NaviError` instance on failure, or
    ``None`` on success.
    """

    value: T | None
    error: NaviError | None
    ok: bool

    @staticmethod
    def success(value: T) -> Result[T]:
        return Result(value=value, error=None, ok=True)

    @staticmethod
    def fail(error: NaviError) -> Result[Any]:
        return Result(value=None, error=error, ok=False)

    def map(self, fn: Callable[[T], Any]) -> Result[Any]:
        if not self.ok:
            return self
        return Result.success(fn(self.value))

    def unwrap(self) -> T:
        if self.ok:
            return self.value  # type: ignore[return-value]
        raise self.error or RuntimeError("result failed without error")

    def unwrap_or(self, default: T) -> T:
        return self.value if self.ok else default


# ----------------------------------------------------------------- @guarded


def _error_observation(*, error_reason: str, error_type: str) -> tuple[str, dict[str, Any]]:
    facts = {"error_reason": error_reason, "error_type": error_type}
    return json.dumps(facts, ensure_ascii=False, sort_keys=True), facts


def guarded(fn: Callable) -> Callable:
    """Decorator for ``Capability.invoke`` methods.

    Catches :class:`NaviError` and generic :class:`Exception`, converting
    them to ``CapabilityResult`` failures. This replaces the 22+ inline
    ``try/except`` blocks scattered across ``actions/*.py``.

    The decorated function must return a :class:`CapabilityResult`.
    """

    @functools.wraps(fn)
    async def wrapper(self, args, *, permission, context):
        try:
            return await fn(self, args, permission=permission, context=context)
        except NaviError as exc:
            logger.debug("capability %s failed: %s", getattr(self, "spec", ""), exc)
            from .capabilities_types import CapabilityResult

            observation, facts = _error_observation(
                error_reason=exc.reason,
                error_type=exc.__class__.__name__,
            )
            return CapabilityResult(
                ok=False,
                action="error",
                observation=observation,
                message=str(exc),
                terminal=exc.terminal,
                facts=facts,
                error_reason=exc.reason,
            )
        except Exception as exc:
            logger.exception(
                "capability %s raised unexpected error", getattr(self, "spec", "")
            )
            from .capabilities_types import CapabilityResult

            observation, facts = _error_observation(
                error_reason="internal_error",
                error_type=exc.__class__.__name__,
            )
            return CapabilityResult(
                ok=False,
                action="error",
                observation=observation,
                message=f"internal error: {exc}",
                terminal=False,
                facts=facts,
                error_reason="internal_error",
            )

    return wrapper
