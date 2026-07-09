from navi.evolution import EvolutionEngine, EvolutionLedger, list_evolution_targets
from navi.memory.store import MemoryStore
from navi.goals import GoalStore
from navi.graph import GraphStore
import typer
import json
import time
from navi.paths import ensure_home
from ..common import _invoke_capability


goal_app = typer.Typer(help="Durable goal lifecycle")

evolution_app = typer.Typer(help="Evolution ledger")
memory_app = typer.Typer(help="Typed memory control system")
graph_app = typer.Typer(help="Personal graph")

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
    store = MemoryStore(ensure_home())
    before = store.get_item(item_id)
    item = store.set_status(item_id, "revoked")
    if item is None:
        raise typer.BadParameter("memory item not found")
    EvolutionLedger(ensure_home()).record(
        run_id=f"cli:memory:revoke:{item_id}",
        target_type="memory_item",
        target_id=item_id,
        reason="CLI memory revoke",
        before=json.dumps(before.__dict__, default=str) if before else "",
        after=json.dumps(item.__dict__, default=str),
    )
    typer.echo(f"{item.id} {item.status}")

@graph_app.command("list")
def graph_list() -> None:
    """List personal graph nodes."""
    for node in GraphStore(ensure_home()).list():
        typer.echo(f"{node.type} {node.name}: {node.data}")

@goal_app.command("list")
def goal_list(phase: str = "", limit: int = 50) -> None:
    """List durable goals as facts."""
    for goal in GoalStore(ensure_home()).list(phase=phase, limit=limit):
        task = f" task={goal.run_id}" if goal.run_id else ""
        trace = f" trace={goal.trace_id}" if goal.trace_id else ""
        typer.echo(f"{goal.id} phase={goal.phase} resolution={goal.resolution}{task}{trace} {goal.objective}")

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
    try:
        proposal = EvolutionLedger(ensure_home()).propose(
            target_type=target_type,
            target_id=target_id,
            reason=reason,
            expected_benefit=expected_benefit,
            risk=risk,
            before=before,
            after=after,
            rollback_plan=rollback_plan,
            required_approval_level=required_approval_level,
            evidence=evidence,
            source_run_id=source_run_id,
            eval_cases=[item.strip() for item in eval_cases.split(",") if item.strip()],
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(proposal.id)

@evolution_app.command("apply-proposal")
def evolution_apply_proposal(proposal_id: str) -> None:
    """Apply a proposal by recording it in the evolution ledger."""
    try:
        event = EvolutionEngine(ensure_home()).apply_proposal(proposal_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if event is None:
        raise typer.BadParameter("proposal not found")
    typer.echo(event.id)

@evolution_app.command("record-evaluation")
def evolution_record_evaluation(
    proposal_id: str,
    evaluation_result: str,
    evaluation_evidence: str = "",
) -> None:
    """Attach post-apply evaluation evidence to an evolution proposal."""
    proposal = EvolutionLedger(ensure_home()).record_proposal_evaluation(
        proposal_id,
        evaluation_result,
        evaluation_evidence=evaluation_evidence,
        approver_id="cli",
        approved_at=time.time(),
    )
    if proposal is None:
        raise typer.BadParameter("proposal not found")
    typer.echo(proposal.id)

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
