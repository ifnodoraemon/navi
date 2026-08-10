from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .permission_contract import normalize_permission
from .tools import ToolSpec


# ---------------------------------------------------------------------------
# Capability registry
#
# Capabilities register themselves at class-definition time via the
# ``@capability(key)`` decorator. The action registry (``actions/registry.py``)
# then constructs each registered class from its ``__init__`` signature.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type] = {}


def capability(key: str):
    """Register a capability class under ``key``.

    The decorated class is stored in :data:`_REGISTRY` so the action
    registry can look it up by spec name. Construction kwargs (``home``,
    ``project_dir``) are inferred from the ``__init__`` signature at
    build time.
    """

    def decorator(cls):
        if key in _REGISTRY:
            raise ValueError(f"capability key already registered: {key}")
        _REGISTRY[key] = cls
        return cls

    return decorator


def construct_capability(
    cls: type,
    spec: ToolSpec,
    *,
    home: Path,
    project_dir: Path,
    runtime: Any | None = None,
    capability_registry: Any | None = None,
) -> Any:
    """Construct a registered capability from its ``__init__`` signature.

    Common dependencies are conditionally passed by name. This keeps the
    registry free of per-class construction metadata while still allowing a
    capability to opt into the runtime/control-plane dependencies it needs.
    """
    params = inspect.signature(cls).parameters
    kwargs: dict[str, Any] = {}
    if "home" in params:
        kwargs["home"] = home
    if "project_dir" in params:
        kwargs["project_dir"] = project_dir
    if "runtime" in params:
        kwargs["runtime"] = runtime
    if "capability_registry" in params:
        kwargs["capability_registry"] = capability_registry
    return cls(spec, **kwargs)


@dataclass(frozen=True)
class CapabilityContext:
    home: Path
    goal_id: str = ""
    loop_run_id: str = ""
    peer_id: str = ""
    sender_id: str = ""
    source: str = "local"
    permission_ceiling: str = "write"
    skill_permission_ceiling: str = "read"
    workspace: str = ""
    session_id: str | None = None
    trace_id: str = ""
    input_text: str = ""
    event_bus: Any | None = None
    allowed_tools: frozenset[str] | None = None
    disabled_tools: frozenset[str] = frozenset()
    disabled_capability_classes: frozenset[str] = frozenset()
    runtime_facts: Mapping[str, Any] | None = None
    execution_context: str = "turn"
    effect_idempotency_key: str = ""
    approved_approval_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "permission_ceiling",
            normalize_permission(self.permission_ceiling, default="write"),
        )
        object.__setattr__(
            self,
            "skill_permission_ceiling",
            normalize_permission(self.skill_permission_ceiling, default="read"),
        )


@dataclass(frozen=True)
class CapabilityResult:
    ok: bool
    action: str
    message: str = ""
    run_id: str = ""
    terminal: bool = False
    facts: dict[str, Any] | None = None
    provenance: str = ""
    error_reason: str = ""
    yields_control: bool = False


@dataclass(frozen=True)
class CapabilityNode:
    name: str
    source: str
    permission: str
    facts_only: bool
    mutates: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    provider: str
    description: str = ""
    side_effect_policy: dict[str, Any] | None = None
    permission_policy: str = "static"
    argument_permission_field: str = ""
    argument_permissions: tuple[tuple[str, str], ...] = ()
    risk_policy: str = "declared"
    context_policy: str = "none"
    runtime_policy: str = "none"
    delegation_allowed: bool = True
    deterministic_completion_authority: bool = False
    approval_policy: str = "risk"
    workspace_policy: str = "none"
    workspace_fields: tuple[str, ...] = ()
    workspace_scope: str = "execution"


class BaseCapability:
    """Common ``__init__`` for capabilities constructed with ``(spec, *, home)``.

    Eliminates the ``self.spec = spec; self.home = home`` boilerplate repeated
    across the majority of capability classes. Subclasses needing
    ``project_dir`` override ``__init__`` and call ``super().__init__``.
    """

    def __init__(self, spec: ToolSpec, *, home: Path):
        self.spec = spec
        self.home = home

    async def preflight(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult | None:
        """Run read-only authorization checks before risk/approval handling."""
        return None

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        raise NotImplementedError


class Capability(Protocol):
    spec: ToolSpec

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult: ...


class CapabilityProvider(Protocol):
    def capabilities(self) -> Mapping[str, Capability]: ...
