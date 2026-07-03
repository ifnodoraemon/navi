from navi.approval_contract import APPROVAL_DECISION_REJECT
from navi.evolution.domain import list_evolution_targets
from navi.memory.store import MemoryStore
from navi.workflows.store import workflow_facts
from navi.evolution.ledger import EvolutionLedger
from navi.goals import GoalStore
from navi.graph import GraphStore
from navi.approval_contract import APPROVAL_DECISION_APPROVE
from navi.subagents import SubagentRunStore
from navi.evolution.engine import EvolutionEngine
import typer
import asyncio
import json
from pathlib import Path
from ..common import *
from ..common import _invoke_capability, _workflow_action_cli


goal_app = typer.Typer(help="Durable goal lifecycle")
subagent_app = typer.Typer(help="Sub-agent runtime records")
workflow_app = typer.Typer(help="Governed dynamic workflows")
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
def goal_list(status: str = "", limit: int = 50) -> None:
    """List durable goals as facts."""
    for goal in GoalStore(ensure_home()).list(status=status, limit=limit):
        task = f" task={goal.run_id}" if goal.run_id else ""
        trace = f" trace={goal.trace_id}" if goal.trace_id else ""
        typer.echo(f"{goal.id} {goal.phase}{task}{trace} {goal.objective}")

@goal_app.command("show")
def goal_show(goal_id: str) -> None:
    """Show one durable goal and its lifecycle events."""
    store = GoalStore(ensure_home())
    goal = store.get(goal_id)
    if goal is None:
        raise typer.BadParameter("goal not found")
    typer.echo(f"{goal.id} {goal.phase} task={goal.run_id or '-'} trace={goal.trace_id or '-'}")
    typer.echo(goal.objective)
    if goal.blocked_reason:
        typer.echo(f"blocked: {goal.blocked_reason}")
    for event in store.list_events(goal_id):
        typer.echo(
            f"- {event.event_type} {event.status} task={event.run_id or '-'} trace={event.trace_id or '-'}"
        )

@subagent_app.command("list")
def subagent_list(role: str = "", status: str = "", run_id: str = "", limit: int = 50) -> None:
    """List sub-agent runtime records as facts."""
    for item in SubagentRunStore(ensure_home()).list(
        role=role, status=status, run_id=run_id, limit=limit
    ):
        task = f" run={item.run_id}" if item.run_id else ""
        typer.echo(f"{item.id} {item.role} {item.phase} {item.status}{task}")

@subagent_app.command("show")
def subagent_show(subagent_id: str) -> None:
    """Show one sub-agent runtime record."""
    item = SubagentRunStore(ensure_home()).get(subagent_id)
    if item is None:
        raise typer.BadParameter("sub-agent run not found")
    typer.echo(f"{item.id} {item.role} {item.phase} {item.status} run={item.run_id or '-'}")
    typer.echo(f"command: {item.command}")
    if item.error:
        typer.echo(f"error: {item.error}")
    typer.echo(item.output_json)

@workflow_app.command("propose")
def workflow_propose(
    objective: str,
    steps_json: str = "[]",
    permission_ceiling: str = "read",
    max_concurrency: int = 4,
    estimated_cost: str = "",
) -> None:
    """Propose a governed dynamic workflow from declared step JSON."""
    try:
        steps = json.loads(steps_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"invalid steps_json: {exc}") from exc
    if not isinstance(steps, list):
        raise typer.BadParameter("steps_json must be a JSON array")
    home = ensure_home()
    capabilities = build_capability_registry(home, project_dir=Path.cwd())
    result = asyncio.run(
        capabilities.invoke(
            "workflow.propose",
            {
                "objective": objective,
                "steps": steps,
                "permission_ceiling": permission_ceiling,
                "max_concurrency": max_concurrency,
                "estimated_cost": estimated_cost,
            },
            permission="prepare",
            context=CapabilityContext(
                home=home, peer_id="cli", sender_id="cli", source="cli", workspace=str(Path.cwd())
            ),
        )
    )
    if not result.ok:
        raise typer.BadParameter(result.message or result.observation)
    facts = result.facts or {}
    typer.echo(f"{facts.get('workflow_id')} {facts.get('status')} steps={facts.get('step_count')}")

@workflow_app.command("list")
def workflow_list(status: str = "", limit: int = 50) -> None:
    """List dynamic workflows."""
    for workflow in WorkflowStore(ensure_home()).list(status=status, limit=limit):
        typer.echo(
            f"{workflow.id} {workflow.phase} ceiling={workflow.permission_ceiling} {workflow.objective}"
        )

@workflow_app.command("show")
def workflow_show(workflow_id: str) -> None:
    """Show one dynamic workflow with steps and events."""
    store = WorkflowStore(ensure_home())
    workflow = store.get(workflow_id)
    if workflow is None:
        raise typer.BadParameter("workflow not found")
    facts = workflow_facts(store, workflow)
    typer.echo(json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True))

@workflow_app.command("approve")
def workflow_approve(workflow_id: str) -> None:
    """Approve a proposed dynamic workflow."""
    _workflow_action_cli(
        "workflow.approve", workflow_id, {"decision": APPROVAL_DECISION_APPROVE}
    )

@workflow_app.command("reject")
def workflow_reject(workflow_id: str) -> None:
    """Reject a proposed dynamic workflow."""
    _workflow_action_cli(
        "workflow.approve", workflow_id, {"decision": APPROVAL_DECISION_REJECT}
    )

@workflow_app.command("run")
def workflow_run(workflow_id: str, resume: bool = False) -> None:
    """Run the next bounded batch of an approved dynamic workflow."""
    _workflow_action_cli("workflow.run", workflow_id, {"resume": resume})

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
def evolution_record_evaluation(proposal_id: str, evaluation_result: str) -> None:
    """Attach post-apply evaluation evidence to an evolution proposal."""
    proposal = EvolutionLedger(ensure_home()).record_proposal_evaluation(
        proposal_id, evaluation_result
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
