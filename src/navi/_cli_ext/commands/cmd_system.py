from navi.prompt_os import assemble_planner_system_prompt
from dataclasses import asdict
from navi.hooks import HookRegistry
from navi.service import install_systemd_user_unit
from navi.prompting import build_system_prompt_assembly
from navi.auth import AuthInspector
from navi.service import build_systemd_user_unit
import typer
import asyncio
import json
from pathlib import Path
from navi.capabilities import build_capability_registry, CapabilityContext
from navi.config import load_config, write_default_config
from navi.connector_registry import load_connector_adapters
from navi.paths import ensure_home
from ..common import _require_connector, _tail_connector_events


auth_app = typer.Typer(help="CLI auth and capability checks")
connectors_app = typer.Typer(help="Connector lifecycle and status")
service_app = typer.Typer(help="System service helpers")
tools_app = typer.Typer(help="Unified capability registry")
hooks_app = typer.Typer(help="Lifecycle hooks")
prompts_app = typer.Typer(help="Prompt operating system")

@auth_app.command("status")
def auth_status() -> None:
    """Show external auth providers without exposing secrets."""
    for item in AuthInspector().status():
        marker = "ok" if item.installed and item.authenticated else "missing"
        typer.echo(f"{item.name}: {marker} path={item.path or '-'} version={item.version or '-'}")

@tools_app.command("list")
def tools_list(json_output: bool = False) -> None:
    """List registered capabilities as facts."""
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
        typer.echo(
            json.dumps(
                {"ok": False, "error": f"capability not found: {name}"},
                ensure_ascii=False,
                indent=2,
            )
        )
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
                "run_id": result.run_id,
                "facts": result.facts or {},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not result.ok:
        raise typer.Exit(code=1)

@hooks_app.command("list")
def hooks_list(json_output: bool = False) -> None:
    """List lifecycle hooks as facts."""
    facts = HookRegistry(ensure_home()).list_facts()
    if json_output:
        typer.echo(json.dumps(facts, ensure_ascii=False, indent=2))
        return
    for hook in facts["hooks"]:
        typer.echo(f"{hook['name']} event={hook['event']} source={hook['source']}")

@prompts_app.command("inspect")
def prompts_inspect(target: str = typer.Argument("planner"), json_output: bool = False) -> None:
    """Inspect prompt OS block manifests."""
    home = ensure_home()
    if target == "planner":
        assembly = assemble_planner_system_prompt()
    elif target == "responder":
        assembly = build_system_prompt_assembly(home=home)
    else:
        raise typer.BadParameter("target must be planner or responder")

    manifest = assembly.manifest()
    if json_output:
        typer.echo(json.dumps(manifest, ensure_ascii=False, indent=2))
        return
    typer.echo(f"{manifest['name']} digest={manifest['digest']}")
    for block in manifest["blocks"]:
        typer.echo(
            f"{block['name']} tier={block['tier']} source={block['source']} "
            f"trusted={block['trusted']} mutable={block['mutable']} digest={block['digest']} chars={block['chars']}"
        )

@service_app.command("unit")
def service_unit() -> None:
    """Print a systemd user unit for the active assistant."""
    typer.echo(build_systemd_user_unit(project_dir=Path.cwd(), navi_home=ensure_home()))

@service_app.command("install")
def service_install() -> None:
    """Install a systemd user unit for the active assistant."""
    home = ensure_home()
    config = load_config(home)
    unit = install_systemd_user_unit(
        project_dir=Path.cwd(), navi_home=home, name=config.runtime.service_name
    )
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

    result = asyncio.run(adapter.setup(home, Path.cwd(), timeout_seconds, show_qr))
    typer.echo(result)

@connectors_app.command("run")
def connector_run(name: str, once: bool = False) -> None:
    """Run a connector gateway."""
    home = ensure_home()
    adapter = _require_connector(name)
    if adapter.run is None:
        raise typer.BadParameter(f"connector does not support run: {name}")
    asyncio.run(adapter.run(home, Path.cwd(), once))

@connectors_app.command("status")
def connector_status(name: str) -> None:
    """Show connector status."""
    home = ensure_home()
    adapter = _require_connector(name)
    typer.echo(adapter.status(home))

@connectors_app.command("tail")
def connector_tail(name: str, limit: int = 20, json_output: bool = False) -> None:
    """Show recent connector ingress and delivery events."""
    home = ensure_home()
    _require_connector(name)
    events = _tail_connector_events(home, name, limit=limit)
    if json_output:
        typer.echo(json.dumps(events, ensure_ascii=False, indent=2))
        return
    if not events:
        typer.echo("(no events)")
        return
    for event in events:
        ts = event.get("ts", "-")
        kind = event.get("event", "-")
        facts = " ".join(
            f"{key}={value}" for key, value in event.items() if key not in {"ts", "event"}
        )
        typer.echo(f"{ts} {kind} {facts}".rstrip())
