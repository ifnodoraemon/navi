from __future__ import annotations

import asyncio
from pathlib import Path

import typer
import uvicorn

from .auth import AuthInspector
from .api import create_app
from .app_factory import build_runtime
from .config import load_config, write_default_config
from .evolution import EvolutionEngine, EvolutionLedger
from .graph import GraphStore
from .paths import ensure_home
from .service import build_systemd_user_unit, install_systemd_user_unit
from .trust import TrustStore
from .weixin.service import WeixinService

app = typer.Typer(help="Navi local-first personal assistant")
weixin_app = typer.Typer(help="Personal Weixin gateway")
auth_app = typer.Typer(help="CLI auth and capability checks")
graph_app = typer.Typer(help="Personal graph")
trust_app = typer.Typer(help="Trust contract")
evolution_app = typer.Typer(help="Evolution ledger")
service_app = typer.Typer(help="System service helpers")
app.add_typer(weixin_app, name="weixin")
app.add_typer(auth_app, name="auth")
app.add_typer(graph_app, name="graph")
app.add_typer(trust_app, name="trust")
app.add_typer(evolution_app, name="evolution")
app.add_typer(service_app, name="service")


@app.command()
def chat() -> None:
    """Start a local CLI chat."""
    home = ensure_home()
    write_default_config(home)
    runtime = build_runtime(home)
    session_id: str | None = None
    typer.echo("Navi chat. Type /exit to quit.")
    while True:
        text = typer.prompt("you")
        if text.strip() in {"/exit", "/quit"}:
            break
        reply = asyncio.run(runtime.chat(text, session_id=session_id))
        session_id = reply.session_id
        typer.echo(f"navi: {reply.content}")


@app.command()
def web(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the local API and Web console."""
    home = ensure_home()
    write_default_config(home)
    typer.echo(f"Navi web: http://{host}:{port}")
    uvicorn.run(create_app(home), host=host, port=port)


@app.command("run")
def run(once: bool = False) -> None:
    """Run the active assistant loop: Weixin, watches, and queued tasks."""
    home = ensure_home()
    write_default_config(home)
    service = WeixinService(home=home, config=load_config(home).weixin, runtime=build_runtime(home))
    asyncio.run(service.run(once=once))


@app.command()
def model() -> None:
    """Show model configuration."""
    config = load_config(ensure_home())
    typer.echo(f"provider={config.model.provider} model={config.model.model}")


@app.command()
def memory(text: str | None = None) -> None:
    """Read or append persistent memory."""
    runtime = build_runtime(ensure_home())
    if text:
        runtime.memory.append_memory(text)
        typer.echo("memory appended")
    else:
        typer.echo(runtime.memory.read_memory() or "(empty)")


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


@auth_app.command("status")
def auth_status() -> None:
    """Show Codex/Gemini availability without exposing secrets."""
    for item in AuthInspector().status():
        marker = "ok" if item.installed and item.authenticated else "missing"
        typer.echo(f"{item.name}: {marker} path={item.path or '-'} version={item.version or '-'}")


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
    unit = install_systemd_user_unit(project_dir=Path.cwd(), navi_home=ensure_home())
    typer.echo(f"installed {unit.path}")
    typer.echo("Run: systemctl --user daemon-reload && systemctl --user enable --now navi.service")


@weixin_app.command("setup")
def weixin_setup(timeout_seconds: int = 480) -> None:
    """Start Weixin QR setup."""
    home = ensure_home()
    write_default_config(home)
    service = WeixinService(home=home, config=load_config(home).weixin, runtime=build_runtime(home))

    def show_qr(url: str) -> None:
        typer.echo("Scan this Weixin QR URL with WeChat:")
        typer.echo(url)

    result = asyncio.run(service.setup(timeout_seconds=timeout_seconds, on_qr=show_qr))
    typer.echo(result)


@weixin_app.command("run")
def weixin_run(once: bool = False) -> None:
    """Run the Weixin long-poll gateway."""
    home = ensure_home()
    service = WeixinService(home=home, config=load_config(home).weixin, runtime=build_runtime(home))
    asyncio.run(service.run(once=once))


@weixin_app.command("status")
def weixin_status() -> None:
    """Show Weixin status."""
    home = ensure_home()
    service = WeixinService(home=home, config=load_config(home).weixin, runtime=build_runtime(home))
    typer.echo(service.status())


if __name__ == "__main__":
    app()
