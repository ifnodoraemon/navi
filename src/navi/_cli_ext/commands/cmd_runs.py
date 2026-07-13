from navi.goals import GoalStore
from navi.memory.store import MemoryStore
from navi.diagnostics import run_diagnostics
import typer
import asyncio
from pathlib import Path
import uvicorn
from navi.app_factory import build_runtime
from navi.config import load_config
from navi.daemon import SystemDaemon
from navi.control_plane import TurnController
from navi.api import create_app
from navi.capabilities import build_capability_registry
from navi.connector_registry import load_connector_adapters
from navi.paths import ensure_home
from navi.config import write_default_config
from ..common import _run_chat_turn, _select_runnable_connector, _cli_service_active
from ...defaults import DEFAULT_API_HOST, DEFAULT_API_PORT


app = typer.Typer(help="Navi local-first personal agent OS")
session_app = typer.Typer(help="Conversation session control")

@app.command()
def chat() -> None:
    """Start a local CLI chat."""
    home = ensure_home()
    write_default_config(home)
    runtime = build_runtime(home)
    config = load_config(home)
    daemon = SystemDaemon(home, project_dir=Path.cwd())
    daemon.start()
    agent = TurnController(
        home=home,
        runtime=runtime,
        project_dir=Path.cwd(),
        event_bus=daemon.event_bus,
    )
    session_id: str | None = None
    typer.echo("Navi chat. Type /exit to quit.")

    pending_options: list[str] = []

    while True:
        if pending_options:
            import questionary

            text = questionary.select("Choice:", choices=pending_options).ask()
            if text is None:
                break
        else:
            text = typer.prompt("you")

        if text.strip() in {"/exit", "/quit"}:
            break

        result = asyncio.run(
            _run_chat_turn(
                agent,
                text,
                peer_id=config.runtime.local_surface,
                sender_id=config.runtime.local_surface,
                session_id=session_id,
            )
        )
        session_id = result.session_id or session_id

        typer.echo(f"navi: {result.surfaced_text()}")

        # Presentation is driven by the structured `options` fact, not by
        # interpreting the agent's action label (principle 4: control surfaces
        # must not encode agent action semantics).
        options = result.facts.get("options") if result.facts else None
        pending_options = options if isinstance(options, list) and options else []

@app.command()
def api(
    host: str = DEFAULT_API_HOST,
    port: int = DEFAULT_API_PORT,
    with_background: bool = typer.Option(
        False,
        "--with-background",
        help="Start API-owned background scheduling primitives.",
    ),
    with_connectors: bool = typer.Option(
        False,
        "--with-connectors",
        help="Start enabled connector polling loops inside the API process.",
    ),
) -> None:
    """Run the headless local API."""
    home = ensure_home()
    write_default_config(home)
    typer.echo(f"Navi API: http://{host}:{port}")
    uvicorn.run(
        create_app(
            home,
            start_background=with_background,
            start_connectors=with_connectors,
        ),
        host=host,
        port=port,
    )

@app.command()
def status() -> None:
    """Show a compact local assistant status summary."""
    home = ensure_home()
    write_default_config(home)
    config = load_config(home)
    tools = build_capability_registry(home, project_dir=Path.cwd()).list_specs()
    sessions = MemoryStore(home).list_sessions()
    goals = GoalStore(home).list()
    connectors = load_connector_adapters()
    typer.echo("Navi status")
    typer.echo(f"home={home}")
    typer.echo(
        f"model={config.model.provider}/{config.model.model} timeout={config.model.timeout_seconds:g}s"
    )
    typer.echo(
        f"execution={config.execution.provider} timeout={config.execution.timeout_seconds:g}s"
    )
    typer.echo(f"tools={len(tools)} sessions={len(sessions)} goals={len(goals)}")
    for adapter in connectors:
        marker = "enabled" if adapter.enabled(home) else "disabled"
        typer.echo(f"connector.{adapter.name}={marker}")

@app.command()
def doctor(connectivity: bool = False) -> None:
    """Run configuration and capability diagnostics."""
    home = ensure_home()
    write_default_config(home)
    config = load_config(home)
    checks = run_diagnostics(home, project_dir=Path.cwd(), include_connectivity=connectivity)
    typer.echo("Navi doctor")
    typer.echo(f"model: {config.model.provider}/{config.model.model}")
    for check in checks:
        if (
            check.name == "service.runtime"
            and check.status != "ok"
            and _cli_service_active(config.runtime.service_name)
        ):
            check = type(check)(
                "service.runtime", "ok", f"{config.runtime.service_name} active/running"
            )
        detail = f" {check.detail}" if check.detail else ""
        typer.echo(f"{check.name}: {check.status}{detail}")
    if any(check.status == "error" for check in checks):
        raise typer.Exit(code=1)

@app.command("run")
def run(once: bool = False, connector: str | None = None) -> None:
    """Run the active assistant loop through a connector."""
    home = ensure_home()
    write_default_config(home)
    adapter = _select_runnable_connector(connector)
    asyncio.run(adapter.run(home, Path.cwd(), once))

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
