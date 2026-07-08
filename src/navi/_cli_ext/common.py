from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import typer

from ..control_plane import TurnController
from ..capabilities import CapabilityContext, build_capability_registry
from ..connector_registry import get_connector_adapter, load_connector_adapters
from ..paths import ensure_home
from ..tools import API_CONTEXT



def _invoke_capability(name: str, args: dict, *, execution_context: str = API_CONTEXT) -> dict:
    """Invoke a capability through the unified registry (same path the API
    takes), so CLI writes go through hook gates and schema validation."""
    home = ensure_home()
    capabilities = build_capability_registry(
        home, project_dir=Path.cwd(), execution_context=execution_context
    )
    spec = capabilities.get(name)
    if spec is None:
        raise typer.BadParameter(f"capability not found: {name}")
    result = asyncio.run(
        capabilities.invoke(
            name,
            args,
            permission=spec.permission,
            context=CapabilityContext(home=home, peer_id="cli", sender_id="cli", source="cli"),
        )
    )
    if not result.ok:
        raise typer.BadParameter(result.message or "capability failed")
    return result.facts or {}


async def _run_chat_turn(
    agent: TurnController,
    text: str,
    *,
    peer_id: str,
    sender_id: str,
    session_id: str | None,
):
    result = await agent.handle(
        text,
        peer_id=peer_id,
        sender_id=sender_id,
        source="cli",
        session_id=session_id,
    )
    await agent.shutdown(timeout=10.0)
    return result


def _trace_evaluation_line(evaluation) -> str:
    evidence = json.loads(evaluation.evidence_json or "{}")
    rule = str(evidence.get("evaluation_rule") or "").strip()
    suffix = f" rule={rule}" if rule else ""
    return f"{evaluation.outcome} {evaluation.failure_domain}{suffix}"


def _require_connector(name: str):
    adapter = get_connector_adapter(name)
    if adapter is None:
        raise typer.BadParameter(f"unknown connector: {name}")
    return adapter


def _select_runnable_connector(name: str | None):
    adapters = load_connector_adapters()
    if name:
        adapter = _require_connector(name)
        if adapter.run is None:
            raise typer.BadParameter(f"connector does not support run: {name}")
        return adapter
    runnable = [adapter for adapter in adapters if adapter.run is not None]
    if not runnable:
        raise typer.BadParameter("no runnable connectors configured")
    enabled = [adapter for adapter in runnable if adapter.enabled(ensure_home())]
    return (enabled or runnable)[0]


def _tail_connector_events(home: Path, name: str, *, limit: int) -> list[dict]:
    path = home / name / "events.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-max(1, limit) :]
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _cli_service_active(name: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode == 0 and result.stdout.strip() == "active":
        return True
    try:
        status = subprocess.run(
            ["systemctl", "--user", "status", name, "--no-pager"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "Active: active (running)" in status.stdout
