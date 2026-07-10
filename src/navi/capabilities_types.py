from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .tools import ToolSpec


# ---------------------------------------------------------------------------
# Capability registry
#
# Capabilities register themselves at class-definition time via the
# ``@capability(key)`` decorator. The action registry (``actions/registry.py``)
# then constructs each registered class from its ``__init__`` signature,
# eliminating the hand-maintained factory dict that previously mapped every
# action spec to a lambda.
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
    peer_id: str = ""
    sender_id: str = ""
    source: str = "local"
    permission_ceiling: str = "write"
    workspace: str = ""
    session_id: str | None = None
    trace_id: str = ""
    input_text: str = ""
    event_bus: Any | None = None
    allowed_tools: frozenset[str] | None = None
    disabled_tools: frozenset[str] = frozenset()
    disabled_capability_classes: frozenset[str] = frozenset()
    enforce_connector_source_policy: bool = True
    runtime_facts: Mapping[str, Any] | None = None


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


class BaseCapability:
    """Common ``__init__`` for capabilities constructed with ``(spec, *, home)``.

    Eliminates the ``self.spec = spec; self.home = home`` boilerplate repeated
    across the majority of capability classes. Subclasses needing
    ``project_dir`` override ``__init__`` and call ``super().__init__``.
    """

    def __init__(self, spec: ToolSpec, *, home: Path):
        self.spec = spec
        self.home = home


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
