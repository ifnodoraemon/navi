"""Evolution engine: orchestrates ledger, graph, memory, and provider."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import load_config
from ..graph import GraphStore
from ..memory import MemoryStore
from ..provider import build_provider
from ..runs import Run, RunStore
from .domain import (
    EvolutionEvent,
    EvolutionProposal,
    _SPEC_FILE_TARGETS,
    _daily_journey_eval_schema,
    _summarize_trace_events,
)
from .ledger import EvolutionLedger

logger = logging.getLogger(__name__)


class EvolutionEngine:
    def __init__(self, home: Path):
        self.home = home
        self.ledger = EvolutionLedger(home)
        self.graph = GraphStore(home)
        self.runs = RunStore(home)

        # Jarvis Memory components
        config = load_config(home)
        self.provider = build_provider(config.model)
        self.memory = MemoryStore(home)

    async def reflect_run(self, task: Run, *, success: bool) -> list[EvolutionEvent]:
        events: list[EvolutionEvent] = []
        reason = "task_reflection_success" if success else "task_reflection_failure"
        events.append(self._update_graph(task, success=success, reason=reason))

        return self.ledger.list_for_task(task.id)

    async def extract_evals_from_session(self, session_id: str, *, run_id: str = "") -> None:
        from navi.evals import load_daily_journey_eval_dataset
        from navi.provider import ChatMessage
        from navi.trace import TraceStore
        import yaml

        events = TraceStore(self.home).list_events_for_run_or_session(
            run_id=run_id,
            session_id=session_id,
            limit=200,
        )
        if not events:
            return

        trace_summary = _summarize_trace_events(events)
        if not trace_summary:
            return

        prompt = (
            "Extract one daily user journey eval from these Navi trace facts. "
            "Use only facts present in the trace. Do not invent hidden state.\n\n"
            f"{trace_summary}"
        )
        response = await self.provider.complete_for(
            "planner",
            [ChatMessage(role="user", content=prompt)],
            output_schema=_daily_journey_eval_schema(),
        )
        if not response.strip():
            return

        extracted = json.loads(response)
        if not isinstance(extracted, dict) or not extracted.get("steps"):
            return

        evals_path = self.home.parent / "evals" / "auto_captured_journeys.yaml"
        before = evals_path.read_text(encoding="utf-8") if evals_path.exists() else ""
        if evals_path.exists():
            data = load_daily_journey_eval_dataset(evals_path)
        else:
            data = {"version": 1, "journeys": []}
        data["journeys"].append(extracted)
        after = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        # FP-5: record the ledger event BEFORE the file side effect lands, so a
        # crash between the two never leaves an unaudited change. Mirrors the
        # apply_proposal ledger-before-side-effect ordering.
        self.ledger.record(
            run_id=run_id,
            target_type="eval_case",
            target_id=str(extracted.get("id") or session_id),
            reason="journey_eval_auto_captured",
            before=before,
            after=after,
        )
        evals_path.parent.mkdir(parents=True, exist_ok=True)
        evals_path.write_text(after, encoding="utf-8")
        logger.info("extracted daily eval from session %s run %s", session_id, run_id)

    def apply_proposal(self, proposal_id: str) -> EvolutionEvent | None:
        proposal = self.ledger.get_proposal(proposal_id)
        if proposal is None:
            return None
        if proposal.status == "applied" and proposal.applied_event_id:
            return self.ledger.get(proposal.applied_event_id)
        # Reuse the ledger's single authority so engine-side effects and the DB
        # transition share one gate (no drift between the two apply paths).
        self.ledger.assert_proposal_applicable(proposal)

        # Record the ledger event before performing the side effect, so the
        # change is always auditable and rollbackable, even if the file write
        # fails afterwards (principle 7/11).
        event = self.ledger.record_apply_event(proposal)
        try:
            self._write_proposal_side_effect(proposal)
        except Exception as exc:
            # FP-5/L11: the side effect failed after the apply event was
            # recorded. Record a follow-up failure event so the ledger
            # reflects that the change did not land, then surface the error.
            self.ledger.record(
                run_id=proposal.source_run_id,
                target_type=proposal.target_type,
                target_id=proposal.target_id,
                reason="proposal_apply_side_effect_failed",
                before=event.after,
                after=json.dumps({"error_type": type(exc).__name__, "error": str(exc)}, sort_keys=True),
            )
            raise
        self.ledger.mark_applied(proposal_id, event.id)
        return event

    def _write_proposal_side_effect(self, proposal: EvolutionProposal) -> None:
        if proposal.target_type == "prompt_layer":
            from ..prompting import PromptLayerStore

            PromptLayerStore(self.home).write_override(proposal.target_id, proposal.after)
            return
        spec_target = _SPEC_FILE_TARGETS.get(proposal.target_type)
        if spec_target is None:
            # Declared evolution targets that have no apply side effect must
            # fail loudly rather than recording a no-op event (principle 1.2).
            raise ValueError(
                f"proposal apply has no side-effect handler for "
                f"target_type={proposal.target_type}"
            )
        subdir, suffix = spec_target
        spec_path = self.home / subdir / f"{proposal.target_id}{suffix}"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(proposal.after, encoding="utf-8")

    def rollback(self, event_id: str) -> EvolutionEvent | None:
        event = self.ledger.get(event_id)
        if event is None or event.rolled_back_at:
            return event
        if event.target_type == "memory":
            (self.home / "memory" / "MEMORY.md").write_text(event.before, encoding="utf-8")
        elif event.target_type == "skill":
            path = Path(event.target_id)
            skills_dir = (self.home / "skills").resolve().absolute()
            try:
                resolved_path = path.resolve().absolute()
                is_safe = skills_dir == resolved_path or skills_dir in resolved_path.parents
            except Exception:
                is_safe = False
            if not is_safe:
                raise ValueError("Skill path must be within the home skills directory")

            if event.before:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(event.before, encoding="utf-8")
            elif path.exists():
                path.unlink()
        elif event.target_type == "graph_node":
            if event.before and event.before != "{}":
                self.graph.replace_data(event.target_id, json.loads(event.before))
            else:
                self.graph.delete(event.target_id)

        elif event.target_type == "memory_item":
            if not event.before:
                self.memory.delete_item(event.target_id)
            else:
                self.memory.restore_item(json.loads(event.before))
                # FP-5: a rolled-back evolution reduces confidence in the
                # affected memory item, since the change it represented was
                # rejected and should not retain its original trust level.
                self.memory.reduce_confidence(event.target_id, delta=0.2)
        elif event.target_type == "run_execution":
            if event.before:
                task_dict = json.loads(event.before)
                missing = [
                    key
                    for key in ("phase", "governance", "acceptance", "resolution")
                    if not task_dict.get(key)
                ]
                if missing:
                    raise ValueError(
                        "run_execution rollback event missing lifecycle fields: "
                        + ", ".join(missing)
                    )
                self.runs.update_run(
                    event.target_id,
                    phase=task_dict["phase"],
                    governance=task_dict["governance"],
                    acceptance=task_dict["acceptance"],
                    resolution=task_dict["resolution"],
                    result_summary=task_dict.get("result_summary", ""),
                    error=task_dict.get("error", ""),
                )
        elif event.target_type == "prompt_layer":
            from ..prompting import PromptLayerStore

            store = PromptLayerStore(self.home)
            if event.before:
                store.write_override(event.target_id, event.before)
            else:
                store.delete_override(event.target_id)
        elif event.target_type in (
            "tool_spec",
            "connector_spec",
            "workflow_policy",
            "memory_schema",
            "eval_case",
        ):
            ext = "json" if event.target_type == "eval_case" else "yaml"
            folder = "evals" if event.target_type == "eval_case" else "specs"
            spec_path = self.home / folder / f"{event.target_id}.{ext}"
            if event.before:
                spec_path.parent.mkdir(parents=True, exist_ok=True)
                spec_path.write_text(event.before, encoding="utf-8")
            elif spec_path.exists():
                spec_path.unlink()
        return self.ledger.mark_rolled_back(event_id)

    def _update_graph(self, task: Run, *, success: bool, reason: str) -> EvolutionEvent:
        name = task.workspace.strip()
        if not name:
            raise ValueError(f"Run {task.id} has no workspace")
        before_node = self.graph.get_by_name("Project", name)
        before = json.dumps(before_node.data if before_node else {}, sort_keys=True)
        node = self.graph.upsert(
            "Project",
            name,
            {
                "path": name,
                "last_run_id": task.id,
                "last_status": "success" if success else "failure",
                "last_prompt": task.prompt,
            },
        )
        after = json.dumps(node.data, sort_keys=True)
        return self.ledger.record(
            run_id=task.id,
            target_type="graph_node",
            target_id=node.id,
            reason=reason,
            before=before,
            after=after,
        )
