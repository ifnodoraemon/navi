from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
    approval_template: str = ""
    approval_commands: dict[str, list[str]] | None = None


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


def approval_surface_affordance(source: str) -> dict[str, Any]:
    source = source.strip()
    for adapter in load_connector_adapters():
        spec = adapter.spec
        if source in {spec.name, spec.surface, spec.local_source}:
            return _approval_affordance_from_spec(spec)
    # Principle 4: no core default. A source with no matching connector has no
    # approval affordance, and the caller renders no approval prompt.
    return {}


def _approval_affordance_from_spec(spec: ConnectorSpec) -> dict[str, Any]:
    return {
        "approval_template": spec.approval_template,
        "approval_commands": spec.approval_commands or {},
    }


def _first_command(commands: dict[str, Any], key: str, fallback: str) -> str:
    raw = commands.get(key)
    if isinstance(raw, list) and raw:
        return str(raw[0])
    return fallback


def render_approval_reply(
    source: str,
    *,
    code: str,
    run_id: str = "",
    action: str = "",
    expires_at: float = 0.0,
) -> str:
    """Render a connector-sourced approval prompt.

    FP-6: the core must not hardcode channel-specific approval verbs (``批准`` /
    ``拒绝``). The connector affordance provides ``approval_template`` and
    ``approval_commands``; this helper formats them. When no connector matches
    the source (CLI/local API), it returns a connector-agnostic prompt that
    names only the code and run id."""
    affordance = approval_surface_affordance(source)
    commands = (
        affordance.get("approval_commands")
        if isinstance(affordance.get("approval_commands"), dict)
        else {}
    )
    approve_command = _first_command(commands, "approve", "approve")
    reject_command = _first_command(commands, "reject", "reject")
    task_line = f"Task ID: `{run_id}`" if run_id else ""
    if expires_at:
        try:
            minutes = max(0, round((float(expires_at) - 0.0) / 60))
        except (TypeError, ValueError):
            minutes = 0
        expiry = f"Approval expires in ~{minutes} minutes." if minutes else ""
    else:
        expiry = ""
    template = str(affordance.get("approval_template") or "")
    if template:
        return template.format(
            task_line=task_line,
            code=code,
            expiry=expiry,
            approve_command=approve_command,
            reject_command=reject_command,
        ).strip()
    head = f"Approval required for `{action}`." if action else "Approval required."
    return (
        f"{head}\n{task_line}\nApproval code: `{code}`\n"
        f"Reply `{approve_command} {code}` to execute, or `{reject_command} {code}` to cancel."
    ).strip()


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
