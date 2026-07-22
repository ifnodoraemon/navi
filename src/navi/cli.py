from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import typer
import uvicorn

from .control_plane import TurnController
from .api import create_app
from .app_factory import build_runtime
from .acceptance import load_acceptance_scenario, report_to_text, run_product_acceptance
from .auth import AuthInspector
from .capabilities import CapabilityContext, build_capability_registry
from .config import load_config, write_default_config
from .connector_registry import get_connector_adapter, load_connector_adapters
from .diagnostics import run_diagnostics
from .daemon import SystemDaemon
from .evals import (
    claw_results_to_json,
    load_claw_eval_dataset,
    load_connector_journey_eval_dataset,
    load_daily_journey_eval_dataset,
    run_claw_eval_dataset,
    run_connector_journey_eval_dataset,
    run_daily_journey_eval_dataset,
)
from .evolution import EvolutionLedger, list_evolution_targets
from .goals import GoalStore
from .graph import GraphStore
from .hooks import HookRegistry
from .json_utils import json_object
from .loop import LoopPhase, loop_decision_summary
from .memory import MemoryStore
from .metrics import MetricsProjector
from .paths import ensure_home
from .prompt_os import assemble_planner_system_prompt
from .prompting import build_system_prompt_assembly
from .provider import build_provider
from .service import build_systemd_user_unit, install_systemd_user_unit
from .safeguards import redact_secrets_deep
from .trace import TraceStore
from .tools import API_CONTEXT


def _invoke_capability(name: str, args: dict, *, execution_context: str = API_CONTEXT) -> dict:
    """Invoke a capability through the unified registry (same path the API
    takes), so CLI writes go through hook gates and schema validation."""
    home = ensure_home()
    capabilities = build_capability_registry(
        home,
        project_dir=Path.cwd(),
        execution_context=execution_context,
    )
    spec = capabilities.get(name)
    if spec is None:
        raise typer.BadParameter(f"capability not found: {name}")
    needs_runtime = spec.runtime_policy == "required" or (
        spec.runtime_policy == "when_auto_start" and bool(args.get("auto_start", True))
    )
    runtime = build_runtime(home) if needs_runtime else None
    if runtime is not None:
        capabilities = build_capability_registry(
            home,
            project_dir=Path.cwd(),
            execution_context=execution_context,
            runtime=runtime,
        )
    result = asyncio.run(
        capabilities.invoke(
            name,
            args,
            permission=spec.permission,
            context=CapabilityContext(home=home, peer_id="cli", sender_id="cli", source="cli"),
        )
    )
    if not result.ok:
        approval = (result.facts or {}).get("approval")
        if isinstance(approval, dict) and approval.get("id"):
            raise typer.BadParameter(
                "approval required: "
                f"approval_id={approval['id']} code={approval.get('code', '')}"
            )
        raise typer.BadParameter(result.message or "capability failed")
    return result.facts or {}


app = typer.Typer(help="Navi local-first personal agent OS")
auth_app = typer.Typer(help="CLI auth and capability checks")
connectors_app = typer.Typer(help="Connector lifecycle and status")
graph_app = typer.Typer(help="Personal graph")
evolution_app = typer.Typer(help="Evolution ledger")
trace_app = typer.Typer(help="Full-flow traces and evaluations")
goal_app = typer.Typer(help="Durable goal lifecycle")
service_app = typer.Typer(help="System service helpers")
memory_app = typer.Typer(help="Typed memory control system")
session_app = typer.Typer(help="Conversation session control")
tools_app = typer.Typer(help="Unified capability registry")
hooks_app = typer.Typer(help="Lifecycle hooks")
prompts_app = typer.Typer(help="Prompt operating system")
eval_app = typer.Typer(help="Evaluation datasets")
app.add_typer(auth_app, name="auth")
app.add_typer(connectors_app, name="connectors")
app.add_typer(graph_app, name="graph")
app.add_typer(evolution_app, name="evolution")
app.add_typer(trace_app, name="trace")
app.add_typer(goal_app, name="goal")
app.add_typer(service_app, name="service")
app.add_typer(memory_app, name="memory")
app.add_typer(session_app, name="session")
app.add_typer(tools_app, name="tools")
app.add_typer(hooks_app, name="hooks")
app.add_typer(prompts_app, name="prompts")
app.add_typer(eval_app, name="eval")


@app.command()
def chat() -> None:
    """Start a local CLI chat."""
    home = ensure_home()
    write_default_config(home)
    runtime = build_runtime(home)
    config = load_config(home)
    daemon = SystemDaemon(home, project_dir=Path.cwd())
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


@app.command()
def api(
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
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
    config = load_config(home)
    host = config.api.host if host is None else host
    port = config.api.port if port is None else port
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


@app.command("config")
def config_show() -> None:
    """Show the effective global configuration with secrets redacted."""
    home = ensure_home()
    write_default_config(home)
    config = load_config(home)
    payload = {
        "path": str(home / "config.yaml"),
        "bootstrap_environment": ["NAVI_HOME"],
        "config": redact_secrets_deep(asdict(config)),
    }
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command()
def status() -> None:
    """Show a compact local assistant status summary."""
    home = ensure_home()
    write_default_config(home)
    config = load_config(home)
    tools = build_capability_registry(home, project_dir=Path.cwd()).list_specs()
    sessions = MemoryStore(home).list_sessions()
    goal_store = GoalStore(home)
    goals = goal_store.count_scoped()
    connectors = load_connector_adapters()
    typer.echo("Navi status")
    typer.echo(f"home={home}")
    typer.echo(
        f"model={config.model.provider}/{config.model.model} timeout={config.model.timeout_seconds:g}s"
    )
    typer.echo(
        f"execution={config.execution.provider} timeout={config.execution.timeout_seconds:g}s"
    )
    typer.echo(f"tools={len(tools)} sessions={len(sessions)} goals={goals}")
    for adapter in connectors:
        marker = "enabled" if adapter.enabled(home) else "disabled"
        typer.echo(f"connector.{adapter.name}={marker}")


@app.command()
def metrics(json_output: bool = False) -> None:
    """Show event-derived runtime metrics and SLO status."""
    snapshot = MetricsProjector(ensure_home()).snapshot().to_dict()
    if json_output:
        typer.echo(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return
    typer.echo(f"Navi metrics overall={snapshot['overall_status']}")
    for slo in snapshot["slos"]:
        typer.echo(
            f"{slo['name']}: {slo['status']} actual={slo['actual']:.4g} "
            f"target={slo['target']} samples={slo['samples']}"
        )


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
    reason: str = typer.Option(..., "--reason", help="Why this memory should be stored."),
    provenance: str = typer.Option(
        "manual", "--provenance", help="Source event or evidence for this memory."
    ),
    scope: str = "global",
    source: str = "manual",
    status: str = "proposed",
    confidence: float = 0.5,
    metadata_json: str = "",
) -> None:
    """Add a typed memory item."""
    metadata: dict = {}
    if metadata_json:
        try:
            parsed = json.loads(metadata_json)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"invalid metadata JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise typer.BadParameter("metadata JSON must be an object")
        metadata = parsed
    result = _invoke_capability(
        "memory.add",
        {
            "type": memory_type,
            "content": content,
            "scope": scope,
            "source": source,
            "status": status,
            "confidence": confidence,
            "metadata": metadata,
            "reason": reason,
            "provenance": provenance,
        },
    )
    typer.echo(result.get("memory_id", ""))


@memory_app.command("list")
def memory_list(
    memory_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> None:
    """List typed memory items as facts."""
    items = MemoryStore(ensure_home()).list_items(
        memory_type=memory_type, status=status, limit=limit
    )
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


@memory_app.command("conflicts")
def memory_conflicts(limit: int = 50) -> None:
    """List declared memory conflicts."""
    conflicts = MemoryStore(ensure_home()).list_conflicts(limit=limit)
    if not conflicts:
        typer.echo("(empty)")
        return
    for conflict in conflicts:
        typer.echo(
            f"{conflict.item.id} {conflict.relation} {conflict.conflicting_item_id} "
            f"status={conflict.status} reason={conflict.reason}"
        )


@memory_app.command("revoke")
def memory_revoke(item_id: str) -> None:
    """Mark a memory item revoked."""
    result = _invoke_capability(
        "memory.add",
        {
            "operation": "revoke",
            "memory_id": item_id,
            "reason": "CLI memory revoke",
            "provenance": "cli",
        },
    )
    item = result.get("item") or {}
    typer.echo(f"{item.get('id', item_id)} {item.get('status', 'revoked')}")


@session_app.command("new")
def session_new(alias: str | None = typer.Argument(None)) -> None:
    """Create a new conversation session, optionally bound to an alias."""
    facts = _invoke_capability("session.create", {"alias": alias} if alias else {})
    typer.echo(str(facts.get("session_id") or ""))


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


@eval_app.command("daily")
def eval_daily(
    dataset: Path = Path("evals") / "daily_journeys.yaml",
    json_output: bool = False,
    validate_only: bool = False,
    timeout_seconds: float = 30.0,
) -> None:
    """Run user-facing daily journey evals against the local runtime."""
    if validate_only:
        load_daily_journey_eval_dataset(dataset)
        if json_output:
            typer.echo(json.dumps({"ok": True, "errors": []}, ensure_ascii=False, indent=2))
        else:
            typer.echo("ok dataset")
        return
    home_path = ensure_home()
    results = asyncio.run(
        run_daily_journey_eval_dataset(
            home=home_path,
            project_dir=Path.cwd(),
            dataset=dataset,
            timeout_seconds=timeout_seconds,
            provider=build_provider(load_config(home_path).model),
        )
    )
    if json_output:
        typer.echo(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            marker = "ok" if result.ok else "fail"
            typer.echo(f"{marker} {result.id}")
            for error in result.errors:
                typer.echo(f"  {error}")
    if any(not result.ok for result in results):
        raise typer.Exit(code=1)


@eval_app.command("claw")
def eval_claw(
    dataset: Path = Path("evals") / "claw_navi.yaml",
    json_output: bool = False,
    validate_only: bool = False,
    attempts: int = 3,
    timeout_seconds: float = 30.0,
) -> None:
    """Run Claw-Eval style Pass^3 user task evals against Navi core flows."""
    if validate_only:
        loaded = load_claw_eval_dataset(dataset)
        if json_output:
            typer.echo(
                json.dumps(
                    {"ok": True, "tasks": len(loaded["tasks"]), "errors": []},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            typer.echo(f"ok dataset tasks={len(loaded['tasks'])}")
        return
    home_path = ensure_home()
    results = asyncio.run(
        run_claw_eval_dataset(
            home=home_path,
            project_dir=Path.cwd(),
            dataset=dataset,
            attempts=attempts,
            timeout_seconds=timeout_seconds,
            provider=build_provider(load_config(home_path).model),
        )
    )
    if json_output:
        typer.echo(claw_results_to_json(results))
    else:
        for result in results:
            marker = "ok" if result.ok else "fail"
            domains = ",".join(result.error_domains) if result.error_domains else "-"
            typer.echo(
                f"{marker} {result.task_id} pass={result.pass_count}/{result.attempts} domains={domains}"
            )
            for error in result.errors:
                typer.echo(f"  {error}")
    if any(not result.ok for result in results):
        raise typer.Exit(code=1)


@eval_app.command("connector")
def eval_connector(
    dataset: Path = Path("evals") / "connector_journeys.yaml",
    json_output: bool = False,
    validate_only: bool = False,
    timeout_seconds: float = 30.0,
) -> None:
    """Run connector journey evals against a local runtime."""
    if validate_only:
        loaded = load_connector_journey_eval_dataset(dataset)
        if json_output:
            typer.echo(
                json.dumps(
                    {"ok": True, "journeys": len(loaded["journeys"]), "errors": []},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            typer.echo(f"ok dataset journeys={len(loaded['journeys'])}")
        return
    results = asyncio.run(
        run_connector_journey_eval_dataset(
            home=ensure_home(),
            project_dir=Path.cwd(),
            dataset=dataset,
            timeout_seconds=timeout_seconds,
        )
    )
    if json_output:
        typer.echo(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            marker = "ok" if result.ok else "fail"
            typer.echo(f"{marker} {result.id}")
            for error in result.errors:
                typer.echo(f"  {error}")
    if any(not result.ok for result in results):
        raise typer.Exit(code=1)


@eval_app.command("acceptance")
def eval_acceptance(
    scenario: Path = Path("evals") / "product_acceptance.yaml",
    workspace: Path | None = None,
    json_output: bool = False,
    no_auto_approve: bool = False,
) -> None:
    """Run a product acceptance loop against the real assistant path."""
    home = ensure_home()
    loaded = load_acceptance_scenario(scenario)
    report = asyncio.run(
        run_product_acceptance(
            home=home,
            project_dir=workspace,
            scenario=loaded,
            auto_approve=not no_auto_approve,
        )
    )
    if json_output:
        typer.echo(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(report_to_text(report))
    if not report.accepted:
        raise typer.Exit(code=1)


@graph_app.command("list")
def graph_list() -> None:
    """List personal graph nodes."""
    for node in GraphStore(ensure_home()).list():
        typer.echo(f"{node.type} {node.name}: {node.data}")


@trace_app.command("list")
def trace_list() -> None:
    """List recent full-flow trace ids."""
    for trace_id in TraceStore(ensure_home()).list_trace_ids():
        typer.echo(trace_id)


@trace_app.command("show")
def trace_show(trace_id: str) -> None:
    """Show events for one full-flow trace."""
    for event in TraceStore(ensure_home()).list_events(trace_id):
        marker = "ok" if event.ok else "fail"
        typer.echo(
            f"{event.phase} {marker} tool={event.tool or '-'} role={event.model_role or '-'}"
        )
        if event.phase == LoopPhase.DECISION:
            output = json_object(event.output_json)
            summary = loop_decision_summary(
                output,
                event_tool=event.tool,
                event_run_id=event.run_id,
            )
            typer.echo(
                "  "
                + " ".join(
                    part
                    for part in (
                        f"decision={summary.decision or '-'}",
                        f"reason={summary.reason or '-'}",
                    )
                    if part
                )
            )
            failed = [*summary.failed_checkers, *summary.failed_gates]
            if failed:
                typer.echo(f"  failed={', '.join(failed)}")
        if event.message:
            typer.echo(f"  {event.message[:240]}")


@trace_app.command("decisions")
def trace_decisions(trace_id: str) -> None:
    """Show loop decisions for one full-flow trace."""
    for event in TraceStore(ensure_home()).list_loop_decisions(trace_id):
        output = json_object(event.output_json)
        summary = loop_decision_summary(
            output,
            event_tool=event.tool,
            event_run_id=event.run_id,
        )
        typer.echo(
            " ".join(
                (
                    summary.decision or "-",
                    summary.phase or "-",
                    f"tool={summary.tool or '-'}",
                    f"reason={summary.reason or '-'}",
                )
            )
        )
        failed = [*summary.failed_checkers, *summary.failed_gates]
        if failed:
            typer.echo(f"  failed={', '.join(failed)}")


@trace_app.command("runs")
def trace_runs(trace_id: str) -> None:
    """Show LangSmith-style run/span projection for one trace."""
    for run in TraceStore(ensure_home()).list_run_views(trace_id):
        parent = f" parent={run.parent_run_id}" if run.parent_run_id else ""
        typer.echo(f"{run.id} {run.run_type} {run.status} {run.name}{parent}")


@trace_app.command("evaluate")
def trace_evaluate(trace_id: str) -> None:
    """Evaluate a trace to identify the likely optimization target."""
    facts = _invoke_capability("trace.evaluate", {"trace_id": trace_id})
    evaluation = facts.get("evaluation") or {}
    typer.echo(_trace_evaluation_line(evaluation))


@trace_app.command("evaluations")
def trace_evaluations(trace_id: str = typer.Argument(""), limit: int = 50) -> None:
    """List trace evaluations as optimization evidence."""
    for evaluation in TraceStore(ensure_home()).list_evaluations(trace_id, limit=limit):
        typer.echo(f"{evaluation.trace_id} {_trace_evaluation_line(evaluation)}")


def _trace_evaluation_line(evaluation) -> str:
    if isinstance(evaluation, dict):
        evidence = evaluation.get("evidence") or {}
        outcome = str(evaluation.get("outcome") or "")
        failure_domain = str(evaluation.get("failure_domain") or "")
    else:
        evidence = json.loads(evaluation.evidence_json or "{}")
        outcome = evaluation.outcome
        failure_domain = evaluation.failure_domain
    rule = str(evidence.get("evaluation_rule") or "").strip()
    suffix = f" rule={rule}" if rule else ""
    return f"{outcome} {failure_domain}{suffix}"


@goal_app.command("list")
def goal_list(phase: str = "", limit: int = 50) -> None:
    """List durable goals as facts."""
    for goal in GoalStore(ensure_home()).list(phase=phase, limit=limit):
        task = f" task={goal.run_id}" if goal.run_id else ""
        trace = f" trace={goal.trace_id}" if goal.trace_id else ""
        typer.echo(
            f"{goal.id} phase={goal.phase} governance={goal.governance} resolution={goal.resolution}{task}{trace} {goal.objective}"
        )


@goal_app.command("open")
def goal_open(
    objective: str,
    workspace: str = typer.Option("", "--workspace", help="Workspace path for the goal."),
    verification_command: str = typer.Option(
        "", "--verification-command", help="Deterministic checker command."
    ),
    cron_schedule: str = typer.Option(
        "", "--cron-schedule", help="Cron expression to run the goal periodically."
    ),
    allowed_capability: list[str] | None = typer.Option(
        None,
        "--allowed-capability",
        help="Capability allowed in the LoopSpec. Repeat to allow multiple capabilities.",
    ),
    timeout_seconds: int = typer.Option(120, "--timeout-seconds"),
    auto_start: bool = typer.Option(True, "--auto-start/--no-auto-start"),
) -> None:
    """Create a durable Goal and LoopRun through the Loop control service."""
    args: dict[str, object] = {
        "objective": objective,
        "timeout_seconds": timeout_seconds,
        "auto_start": auto_start,
    }
    if workspace:
        args["workspace"] = workspace
    if verification_command:
        args["verification_command"] = verification_command
    if cron_schedule:
        args["cron_schedule"] = cron_schedule
    if allowed_capability:
        args["allowed_capabilities"] = allowed_capability
    typer.echo(
        json.dumps(
            _invoke_capability("goal.open", args),
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@goal_app.command("show")
def goal_show(goal_id: str) -> None:
    """Show one durable goal and its lifecycle events."""
    store = GoalStore(ensure_home())
    goal = store.get(goal_id)
    if goal is None:
        raise typer.BadParameter("goal not found")
    typer.echo(
        f"{goal.id} phase={goal.phase} governance={goal.governance} resolution={goal.resolution} "
        f"task={goal.run_id or '-'} trace={goal.trace_id or '-'}"
    )
    typer.echo(goal.objective)
    if goal.blocked_reason:
        typer.echo(f"blocked: {goal.blocked_reason}")
    for event in store.list_events(goal_id):
        typer.echo(
            f"- {event.event_type} phase={event.phase} governance={event.governance} "
            f"resolution={event.resolution} task={event.run_id or '-'} trace={event.trace_id or '-'}"
        )


@goal_app.command("state")
def goal_state(
    goal_id: str = typer.Argument("", help="Goal id to inspect."),
    loop_run_id: str = typer.Option("", "--loop-run-id", help="LoopRun id to inspect."),
    limit: int = 20,
) -> None:
    """Show durable Goal/LoopRun control state as JSON facts."""
    args: dict[str, object] = {"limit": limit}
    if goal_id:
        args["goal_id"] = goal_id
    if loop_run_id:
        args["loop_run_id"] = loop_run_id
    typer.echo(
        json.dumps(
            _invoke_capability("goal.state", args),
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@goal_app.command("resume")
def goal_resume(
    goal_id: str = typer.Argument("", help="Goal id to resume."),
    loop_run_id: str = typer.Option("", "--loop-run-id", help="LoopRun id to resume."),
    workspace: str = typer.Option("", "--workspace", help="Workspace path."),
) -> None:
    """Resume a durable Goal or LoopRun from its checkpoint."""
    args: dict[str, object] = {}
    if goal_id:
        args["goal_id"] = goal_id
    if loop_run_id:
        args["loop_run_id"] = loop_run_id
    if workspace:
        args["workspace"] = workspace
    typer.echo(
        json.dumps(
            _invoke_capability("goal.resume", args),
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@goal_app.command("cancel")
def goal_cancel(
    goal_id: str = typer.Argument("", help="Goal id to cancel."),
    loop_run_id: str = typer.Option("", "--loop-run-id", help="LoopRun id to cancel."),
    reason: str = typer.Option("", "--reason", help="Cancellation reason."),
) -> None:
    """Cancel an active durable Goal or LoopRun through the StateGraph control edge."""
    args: dict[str, object] = {}
    if goal_id:
        args["goal_id"] = goal_id
    if loop_run_id:
        args["loop_run_id"] = loop_run_id
    if reason:
        args["reason"] = reason
    typer.echo(
        json.dumps(
            _invoke_capability("goal.cancel", args),
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@evolution_app.command("list")
def evolution_list() -> None:
    """List evolution events."""
    for event in EvolutionLedger(ensure_home()).list():
        typer.echo(f"{event.id} {event.target_type} {event.target_id} task={event.run_id}")


@evolution_app.command("targets")
def evolution_targets() -> None:
    """List evolvable behavior target types."""
    for target in list_evolution_targets():
        marker = "permissioned" if target["permissions_can_expand"] else "content"
        typer.echo(
            f"{target['target_type']} source={target['source']} kind={marker} {target['description']}"
        )


@evolution_app.command("proposals")
def evolution_proposals(status: str | None = None) -> None:
    """List pending or applied evolution proposals."""
    for proposal in EvolutionLedger(ensure_home()).list_proposals(status=status):
        typer.echo(f"{proposal.id} {proposal.status} {proposal.target_type} {proposal.target_id}")


@evolution_app.command("propose")
def evolution_propose(
    target_type: str,
    target_id: str,
    reason: str,
    after: str,
    before: str = "",
    expected_benefit: str = "",
    risk: str = "",
    rollback_plan: str = "",
    required_approval_level: str = "L2",
    evidence: str = "",
    source_run_id: str = "",
    eval_cases: str = "",
) -> None:
    """Create a reviewable evolution proposal without mutating the target."""
    facts = _invoke_capability(
        "evolution.propose",
        {
            "target_type": target_type,
            "target_id": target_id,
            "reason": reason,
            "expected_benefit": expected_benefit,
            "risk": risk,
            "before": before,
            "after": after,
            "rollback_plan": rollback_plan,
            "required_approval_level": required_approval_level,
            "evidence": evidence,
            "source_run_id": source_run_id,
            "eval_cases": [item.strip() for item in eval_cases.split(",") if item.strip()],
        },
    )
    typer.echo(str(facts.get("proposal_id") or ""))


@evolution_app.command("apply-proposal")
def evolution_apply_proposal(proposal_id: str) -> None:
    """Apply an evaluated proposal through the governed capability boundary."""
    facts = _invoke_capability("evolution.apply", {"proposal_id": proposal_id})
    typer.echo(str(facts.get("event_id") or ""))


@evolution_app.command("experiment")
def evolution_experiment(proposal_id: str) -> None:
    """Run and persist the proposal's declared evaluation cases."""
    facts = _invoke_capability("evolution.experiment", {"proposal_id": proposal_id})
    typer.echo(json.dumps(facts.get("experiment") or {}, ensure_ascii=False, sort_keys=True))


@evolution_app.command("record-evaluation")
def evolution_record_evaluation(
    proposal_id: str,
    evaluation_result: str,
    evaluation_evidence: str = "",
    approval_id: str = "",
) -> None:
    """Attach post-apply evaluation evidence to an evolution proposal."""
    facts = _invoke_capability(
        "evolution.record_evaluation",
        {
            "proposal_id": proposal_id,
            "evaluation_result": evaluation_result,
            "evaluation_evidence": evaluation_evidence,
            "approval_id": approval_id,
        },
    )
    typer.echo(str(facts.get("proposal_id") or ""))


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
    facts = _invoke_capability("evolution.rollback", {"event_id": event_id})
    event = facts.get("event") or {}
    typer.echo(f"rolled_back_at={event.get('rolled_back_at', 0)}")


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


if __name__ == "__main__":
    app()
