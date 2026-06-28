from pathlib import Path
from typing import Mapping

from ..capabilities_types import (
    Capability,
    construct_capability,
    _REGISTRY,
)
from ..conversation_contract import CONVERSATION_TOOL_ASK

from .specs import ACTION_SPECS


# Spec names whose handler key differs from ``name.replace(".", "_")``.
_KEY_OVERRIDES = {CONVERSATION_TOOL_ASK: "clarify", "delegate.retry": "execution_retry"}


def get_action_handlers(
    home: Path, project_dir: Path
) -> dict[str, Capability]:
    # Importing the action modules triggers their ``@capability`` decorators,
    # which populate ``_REGISTRY``. No hand-maintained factory dict remains.
    from . import (  # noqa: F401 — imported for side effect
        approval,
        conversation,
        delegation,
        evolution,
        memory,
        session,
        trace,
        watch,
        workflow,
    )

    specs = {spec.name: spec for spec in ACTION_SPECS}
    handlers: dict[str, Capability] = {}
    for name, spec in specs.items():
        key = _KEY_OVERRIDES.get(name, name.replace(".", "_"))
        cls = _REGISTRY.get(key)
        if cls is None:
            raise ValueError(f"unknown action capability handler: {name}")
        handlers[name] = construct_capability(
            cls, spec, home=home, project_dir=project_dir
        )
    return handlers


class ActionCapabilityProvider:
    def __init__(self, *, home: Path, gateway):
        self.home = home
        self.gateway = gateway

    def capabilities(self) -> Mapping[str, Capability]:
        return get_action_handlers(self.home, self.gateway.project_dir)
