from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import navi


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    surface: str
    status_tool: str
    status_description: str
    session_alias_prefix: str
    local_source: str
    approval_approve_commands: tuple[str, ...] = field(default_factory=tuple)
    approval_reject_commands: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ConnectorAdapter:
    spec: ConnectorSpec
    enabled: Callable[[Path], bool]
    status: Callable[[Path], dict[str, Any]]
    diagnostics: Callable[[Path], list[dict[str, str]]] | None
    register_tools: Callable[[Any, Path], None]
    setup: Callable[[Path, Path, int, Callable[[str], None] | None], Awaitable[str]] | None = None
    run: Callable[[Path, Path, bool], Awaitable[None]] | None = None
    load_journey_eval_dataset: Callable[[Path], dict[str, Any]] | None = None
    run_journey_eval_dataset: Callable[[Path, Path, Path, float], Awaitable[list[Any]]] | None = (
        None
    )

    @property
    def name(self) -> str:
        return self.spec.name


def load_connector_adapters() -> list[ConnectorAdapter]:
    adapters: list[ConnectorAdapter] = []
    seen: set[str] = set()
    for factory in _discover_connector_factories():
        adapter = factory()
        if adapter.name in seen:
            raise ValueError(f"duplicate connector adapter: {adapter.name}")
        seen.add(adapter.name)
        adapters.append(adapter)
    return adapters


def get_connector_adapter(name: str) -> ConnectorAdapter | None:
    for adapter in load_connector_adapters():
        if adapter.name == name:
            return adapter
    return None



def _discover_connector_factories() -> list[Callable[[], ConnectorAdapter]]:
    factories: list[Callable[[], ConnectorAdapter]] = []
    for module_info in pkgutil.iter_modules(navi.__path__, prefix="navi."):
        if not module_info.ispkg:
            continue
        try:
            module = importlib.import_module(f"{module_info.name}.connector")
        except ModuleNotFoundError as exc:
            if exc.name == f"{module_info.name}.connector":
                continue
            raise
        factory = getattr(module, "create_adapter", None)
        if callable(factory):
            factories.append(factory)
    return factories
