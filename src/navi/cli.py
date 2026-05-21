from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import typer
import uvicorn

from .engine import HernessEngine
from .api import create_app
from .app_factory import build_runtime
from .auth import AuthInspector
from .capabilities import CapabilityContext, build_capability_registry
from .config import load_config, write_default_config
from .connector_registry import get_connector_adapter, load_connector_adapters
from .defaults import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT
from .evals import (
    load_task_eval_dataset,
    results_to_json,
    run_task_eval_dataset,
    task_eval_tools,
    validate_task_eval_dataset,
)
from .evolution import EvolutionEngine, EvolutionLedger
from .graph import GraphStore
from .memory import MemoryStore
from .paths import ensure_home
from .service import build_systemd_user_unit, install_systemd_user_unit
from .trust import TrustStore

app = typer.Typer(help="Navi local-first personal assistant")
auth_app = typer.Typer(help="CLI auth and capability checks")
connectors_app = typer.Typer(help="Connector lifecycle and status")
graph_app = typer.Typer(help="Personal graph")
trust_app = typer.Typer(help="Trust contract")
evolution_app = typer.Typer(help="Evolution ledger")
service_app = typer.Typer(help="System service helpers")
memory_app = typer.Typer(help="Typed memory control system")
session_app = typer.Typer(help="Conversation session control")
tools_app = typer.Typer(help="Unified fact tool registry")
eval_app = typer.Typer(help="Evaluation datasets")
app.add_typer(auth_app, name="auth")
app.add_typer(connectors_app, name="connectors")
app.add_typer(graph_app, name="graph")
app.add_typer(trust_app, name="trust")
app.add_typer(evolution_app, name="evolution")
app.add_typer(service_app, name="service")
app.add_typer(memory_app, name="memory")
app.add_typer(session_app, name="session")
app.add_typer(tools_app, name="tools")
app.add_typer(eval_app, name="eval")


@app.command()
def chat() -> None:
    """Start a local CLI chat."""
    home = ensure_home()
    write_default_config(home)
    runtime = build_runtime(home)
    config = load_config(home)
    agent = HernessEngine(home=home, runtime=runtime, project_dir=Path.cwd())
    session_id: str | None = None
    typer.echo("Navi chat. Type /exit to quit.")
    while True:
        text = typer.prompt("you")
        if text.strip() in {"/exit", "/quit"}:
            break
        result = asyncio.run(
            agent.handle(
                text,
                peer_id=config.runtime.local_surface,
                sender_id=config.runtime.local_surface,
                source="cli",
                session_id=session_id,
            )
        )
        session_id = result.session_id or session_id
        typer.echo(f"navi: {result.text}")


@app.command()
def web(host: str = DEFAULT_WEB_HOST, port: int = DEFAULT_WEB_PORT) -> None:
    """Run the local API and Web console."""
    home = ensure_home()
    write_default_config(home)
    typer.echo(f"Navi web: http://{host}:{port}")
    uvicorn.run(create_app(home), host=host, port=port)


@app.command("run")
def run(once: bool = False, connector: str | None = None) -> None:
    """Run the active assistant loop through a connector."""
    home = ensure_home()
    write_default_config(home)
    adapter = _select_runnable_connector(connector)
    asyncio.run(adapter.run(home, once))


@app.command()
def model() -> None:
    """Show model configuration."""
    config = load_config(ensure_home())
    typer.echo(f"provider={config.model.provider} model={config.model.model}")
    for item in config.model.fallbacks:
        typer.echo(f"fallback provider={item.provider} model={item.model}")
    for role, item in config.model.routes.items():
        typer.echo(f"route {role}: provider={item.provider} model={item.model}")


@app.command()
def skills() -> None:
    """List installed skills."""
    runtime = build_runtime(ensure_home())
    found = runtime.skills.list_skills()
    if not found:
        typer.echo("(no skills)")
        return
    for skill in found:
        typer.echo(f"{skill.name}: {skill.description}")


@memory_app.command("add")
def memory_add(
    memory_type: str,
    content: str,
    scope: str = "global",
    source: str = "manual",
    status: str = "proposed",
    confidence: float = 0.5,
) -> None:
    """Add a typed memory item."""
    item = MemoryStore(ensure_home()).add_item(
        memory_type,
        content,
        scope=scope,
        source=source,
        status=status,
        confidence=confidence,
    )
    typer.echo(item.id)


@memory_app.command("list")
def memory_list(
    memory_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> None:
    """List typed memory items as facts."""
    items = MemoryStore(ensure_home()).list_items(memory_type=memory_type, status=status, limit=limit)
    for item in items:
        typer.echo(
            f"{item.id} {item.type} {item.status} scope={item.scope} "
            f"confidence={item.confidence:.2f} source={item.source} {item.content}"
        )


@memory_app.command("recall")
def memory_recall(query: str, limit: int = 8) -> None:
    """Recall goal-relevant memory facts."""
    text = MemoryStore(ensure_home()).render_context(query, limit=limit)
    typer.echo(text or "(empty)")


@memory_app.command("revoke")
def memory_revoke(item_id: str) -> None:
    """Mark a memory item revoked."""
    item = MemoryStore(ensure_home()).set_status(item_id, "revoked")
    if item is None:
        raise typer.BadParameter("memory item not found")
    typer.echo(f"{item.id} {item.status}")


@session_app.command("new")
def session_new(alias: str | None = typer.Argument(None)) -> None:
    """Create a new conversation session, optionally bound to an alias."""
    session_id = MemoryStore(ensure_home()).create_session(alias=alias)
    typer.echo(session_id)


@session_app.command("list")
def session_list(limit: int = 50) -> None:
    """List conversation sessions and aliases as facts."""
    store = MemoryStore(ensure_home())
    aliases = {item.session_id: item.alias for item in store.list_session_aliases(limit=limit)}
    for session_id in store.list_sessions()[:limit]:
        alias = aliases.get(session_id, "-")
        typer.echo(f"{session_id} alias={alias}")


@session_app.command("aliases")
def session_aliases(limit: int = 50) -> None:
    """List active session aliases."""
    for item in MemoryStore(ensure_home()).list_session_aliases(limit=limit):
        typer.echo(f"{item.alias} -> {item.session_id}")


@session_app.command("show")
def session_show(session_id: str, limit: int = 50) -> None:
    """Show messages for one session."""
    for message in MemoryStore(ensure_home()).get_messages(session_id, limit=limit):
        typer.echo(f"{message.role}: {message.content}")


@auth_app.command("status")
def auth_status() -> None:
    """Show Codex/Gemini availability without exposing secrets."""
    for item in AuthInspector().status():
        marker = "ok" if item.installed and item.authenticated else "missing"
        typer.echo(f"{item.name}: {marker} path={item.path or '-'} version={item.version or '-'}")


@tools_app.command("list")
def tools_list(json_output: bool = False) -> None:
    """List registered tools as facts."""
    capabilities = build_capability_registry(ensure_home(), project_dir=Path.cwd())
    specs = [asdict(spec) for spec in capabilities.list_specs()]
    if json_output:
        typer.echo(json.dumps(specs, ensure_ascii=False, indent=2))
        return
    for spec in specs:
        typer.echo(
            f"{spec['name']} facts_only={spec['facts_only']} mutates={spec['mutates']} "
            f"permission={spec['permission']} source={spec['source']}"
        )


@tools_app.command("call")
def tools_call(name: str, args_json: str = "{}") -> None:
    """Invoke a registered capability with a JSON object argument."""
    try:
        args = json.loads(args_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"invalid JSON: {exc}") from exc
    if not isinstance(args, dict):
        raise typer.BadParameter("args must be a JSON object")
    home = ensure_home()
    capabilities = build_capability_registry(home, project_dir=Path.cwd())
    spec = capabilities.get(name)
    if spec is None:
        typer.echo(json.dumps({"ok": False, "error": f"capability not found: {name}"}, ensure_ascii=False, indent=2))
        raise typer.Exit(code=1)
    result = asyncio.run(
        capabilities.invoke(
            name,
            args,
            permission=spec.permission,
            context=CapabilityContext(home=home, peer_id="cli", sender_id="cli", source="cli"),
        )
    )
    typer.echo(
        json.dumps(
            {
                "ok": result.ok,
                "action": result.action,
                "observation": result.observation,
                "message": result.message,
                "task_id": result.task_id,
                "facts": result.facts or {},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not result.ok:
        raise typer.Exit(code=1)


@eval_app.command("tasks")
def eval_tasks(
    dataset: Path = Path("evals") / "task_cases.yaml",
    json_output: bool = False,
    validate_only: bool = False,
    timeout_seconds: float = 75.0,
) -> None:
    """Run the task routing eval dataset against the configured model."""
    home = ensure_home()
    if validate_only:
        errors = validate_task_eval_dataset(
            load_task_eval_dataset(dataset),
            task_eval_tools(home, project_dir=Path.cwd()),
        )
        if json_output:
            typer.echo(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
        elif errors:
            for error in errors:
                typer.echo(error)
        else:
            typer.echo("ok dataset")
        if errors:
            raise typer.Exit(code=1)
        return
    results = asyncio.run(
        run_task_eval_dataset(
            home=home,
            project_dir=Path.cwd(),
            dataset=dataset,
            timeout_seconds=timeout_seconds,
        )
    )
    if json_output:
        typer.echo(results_to_json(results))
    else:
        for result in results:
            marker = "ok" if result.ok else "fail"
            typer.echo(f"{marker} {result.id}")
            for error in result.errors:
                typer.echo(f"  {error}")
    if any(not result.ok for result in results):
        raise typer.Exit(code=1)


@graph_app.command("list")
def graph_list() -> None:
    """List personal graph nodes."""
    for node in GraphStore(ensure_home()).list():
        typer.echo(f"{node.type} {node.name}: {node.data}")


@trust_app.command("list")
def trust_list() -> None:
    """List trust contract rules."""
    for rule in TrustStore(ensure_home()).list():
        typer.echo(
            f"{rule.id} {rule.autonomy_level} {rule.name} "
            f"success={rule.success_count} failure={rule.failure_count}"
        )


@trust_app.command("set")
def trust_set(rule_id: str, level: str) -> None:
    """Set a trust rule autonomy level."""
    rule = TrustStore(ensure_home()).set_level(rule_id, level)
    if rule is None:
        raise typer.BadParameter("rule not found")
    typer.echo(f"{rule.id} -> {rule.autonomy_level}")


@evolution_app.command("list")
def evolution_list() -> None:
    """List evolution events."""
    for event in EvolutionLedger(ensure_home()).list():
        typer.echo(f"{event.id} {event.target_type} {event.target_id} task={event.task_id}")


@evolution_app.command("show")
def evolution_show(event_id: str) -> None:
    """Show an evolution event diff."""
    event = EvolutionLedger(ensure_home()).get(event_id)
    if event is None:
        raise typer.BadParameter("event not found")
    typer.echo(event.diff or "(no diff)")


@evolution_app.command("rollback")
def evolution_rollback(event_id: str) -> None:
    """Rollback a reversible evolution event."""
    event = EvolutionEngine(ensure_home()).rollback(event_id)
    if event is None:
        raise typer.BadParameter("event not found")
    typer.echo(f"rolled_back_at={event.rolled_back_at}")


@service_app.command("unit")
def service_unit() -> None:
    """Print a systemd user unit for the active assistant."""
    typer.echo(build_systemd_user_unit(project_dir=Path.cwd(), navi_home=ensure_home()))


@service_app.command("install")
def service_install() -> None:
    """Install a systemd user unit for the active assistant."""
    home = ensure_home()
    config = load_config(home)
    unit = install_systemd_user_unit(project_dir=Path.cwd(), navi_home=home, name=config.runtime.service_name)
    typer.echo(f"installed {unit.path}")
    typer.echo(f"Run: systemctl --user daemon-reload && systemctl --user enable --now {unit.name}")


@connectors_app.command("list")
def connectors_list() -> None:
    """List configured connector adapters."""
    home = ensure_home()
    for adapter in load_connector_adapters():
        marker = "enabled" if adapter.enabled(home) else "disabled"
        typer.echo(f"{adapter.name}: {marker}")


@connectors_app.command("setup")
def connector_setup(name: str, timeout_seconds: int = 480) -> None:
    """Run connector setup."""
    home = ensure_home()
    write_default_config(home)
    adapter = _require_connector(name)
    if adapter.setup is None:
        raise typer.BadParameter(f"connector does not support setup: {name}")

    def show_qr(url: str) -> None:
        typer.echo("Scan this connector QR URL:")
        typer.echo(url)

    result = asyncio.run(adapter.setup(home, timeout_seconds, show_qr))
    typer.echo(result)


@connectors_app.command("run")
def connector_run(name: str, once: bool = False) -> None:
    """Run a connector gateway."""
    home = ensure_home()
    adapter = _require_connector(name)
    if adapter.run is None:
        raise typer.BadParameter(f"connector does not support run: {name}")
    asyncio.run(adapter.run(home, once))


@connectors_app.command("status")
def connector_status(name: str) -> None:
    """Show connector status."""
    home = ensure_home()
    adapter = _require_connector(name)
    typer.echo(adapter.status(home))


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


if __name__ == "__main__":
    app()
