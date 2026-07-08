from pathlib import Path
from typing import Any, Mapping

from ..capabilities_types import (
    Capability,
    construct_capability,
    _REGISTRY,
)

from .specs import ACTION_SPECS


# Spec names whose handler key differs from ``name.replace(".", "_")``.
_KEY_OVERRIDES = {"respond": "respond"}


def get_action_handlers(
    home: Path,
    project_dir: Path,
    *,
    runtime: Any | None = None,
    capability_registry: Any | None = None,
) -> dict[str, Capability]:
    # Importing the action modules triggers their ``@capability`` decorators,
    # which populate ``_REGISTRY``. No hand-maintained factory dict remains.
    from . import (  # noqa: F401 — imported for side effect
        approval,
        conversation,
        evolution,
        goal,
        memory,
        session,
        trace,
        watch,
    )

    specs = {spec.name: spec for spec in ACTION_SPECS}
    handlers: dict[str, Capability] = {}
    for name, spec in specs.items():
        key = _KEY_OVERRIDES.get(name, name.replace(".", "_"))
        cls = _REGISTRY.get(key)
        if cls is None:
            raise ValueError(f"unknown action capability handler: {name}")
        handlers[name] = construct_capability(
            cls,
            spec,
            home=home,
            project_dir=project_dir,
            runtime=runtime,
            capability_registry=capability_registry,
        )
    return handlers


class ActionCapabilityProvider:
    def __init__(
        self,
        *,
        home: Path,
        gateway,
        runtime: Any | None = None,
        capability_registry: Any | None = None,
    ):
        self.home = home
        self.gateway = gateway
        self.runtime = runtime
        self.capability_registry = capability_registry

    def capabilities(self) -> Mapping[str, Capability]:
        return get_action_handlers(
            self.home,
            self.gateway.project_dir,
            runtime=self.runtime,
            capability_registry=self.capability_registry,
        )
