from dataclasses import asdict
from navi.trace import TraceStore
from navi.loop import loop_decision_summary
from navi.loop import LoopPhase
from navi.provider import build_provider
from navi.acceptance import report_to_text
from navi.acceptance import load_acceptance_scenario
from navi.json_utils import json_object
import typer
import asyncio
import json
from pathlib import Path
from navi.paths import ensure_home
from navi.evals import (
    claw_results_to_json,
    delegation_eval_tools,
    load_claw_eval_dataset,
    load_connector_journey_eval_dataset,
    load_daily_journey_eval_dataset,
    load_delegation_eval_dataset,
    results_to_json,
    run_claw_eval_dataset,
    run_connector_journey_eval_dataset,
    run_daily_journey_eval_dataset,
    run_delegation_eval_dataset,
    validate_delegation_eval_dataset,
)
from navi.config import load_config
from navi.acceptance import run_product_acceptance
from ..common import _trace_evaluation_line


trace_app = typer.Typer(help="Full-flow traces and evaluations")
eval_app = typer.Typer(help="Evaluation datasets")

@eval_app.command("delegations")
def eval_delegations(
    dataset: Path = Path("evals") / "delegation_cases.yaml",
    json_output: bool = False,
    validate_only: bool = False,
    timeout_seconds: float = 75.0,
) -> None:
    """Run the delegation routing eval dataset against the configured model."""
    home = ensure_home()
    if validate_only:
        errors = validate_delegation_eval_dataset(
            load_delegation_eval_dataset(dataset),
            delegation_eval_tools(home, project_dir=Path.cwd()),
        )
        if json_output:
            typer.echo(
                json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2)
            )
        elif errors:
            for error in errors:
                typer.echo(error)
        else:
            typer.echo("ok dataset")
        if errors:
            raise typer.Exit(code=1)
        return
    results = asyncio.run(
        run_delegation_eval_dataset(
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
    dataset: Path = Path("evals") / "weixin_journeys.yaml",
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
    evaluation = TraceStore(ensure_home()).evaluate_trace(trace_id)
    typer.echo(_trace_evaluation_line(evaluation))

@trace_app.command("evaluations")
def trace_evaluations(trace_id: str = typer.Argument(""), limit: int = 50) -> None:
    """List trace evaluations as optimization evidence."""
    for evaluation in TraceStore(ensure_home()).list_evaluations(trace_id, limit=limit):
        typer.echo(f"{evaluation.trace_id} {_trace_evaluation_line(evaluation)}")
