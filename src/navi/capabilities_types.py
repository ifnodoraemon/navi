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
    cls: type, spec: ToolSpec, *, home: Path, project_dir: Path
) -> Any:
    """Construct a registered capability from its ``__init__`` signature.

    Only ``home`` and ``project_dir`` are conditionally passed — everything
    else (``spec``) is positional. This keeps the registry free of per-class
    construction metadata.
    """
    params = inspect.signature(cls.__init__).parameters
    kwargs: dict[str, Any] = {}
    if "home" in params:
        kwargs["home"] = home
    if "project_dir" in params:
        kwargs["project_dir"] = project_dir
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
    input_text: str = ""
    event_bus: Any | None = None


@dataclass(frozen=True)
class CapabilityResult:
    ok: bool
    action: str
    observation: str
    message: str = ""
    run_id: str = ""
    terminal: bool = False
    facts: dict[str, Any] | None = None
    provenance: str = ""
    error_reason: str = "unknown"


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
