from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..result import NotFound, SchemaMismatch, guarded
from ..tools import ToolSpec
from .helpers import (
    arg_text as _arg_text,
    transition_facts as _transition_facts,
    fact_result as _fact_result,
    failure_result as _failure_result,
    resolve_workspace as _resolve_workspace,
    float_or_none as _float_or_none,
)
from ..cron import next_cron_time, validate_cron
from ..runs import RunStore
from ..graph import GraphStore


@capability("watch_create")
class WatchCreateCapability(BaseCapability):
    def __init__(self, spec: ToolSpec, *, home: Path, project_dir: Path):
        super().__init__(spec, home=home)
        self.project_dir = project_dir

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        cron = _arg_text(args, "cron")
        kind = _arg_text(args, "kind") or (
            "once" if args.get("run_at") is not None else "recurring"
        )
        prompt = _arg_text(args, "prompt")
        if not prompt:
            raise SchemaMismatch("watch.create requires prompt.")
        if kind == "once":
            next_run = _float_or_none(args.get("run_at"))
            if next_run is None:
                raise SchemaMismatch("watch.create kind=once requires run_at.")
            now = time.time()
            if next_run <= now:
                return _failure_result(
                    "watch",
                    "watch.create run_at is not in the future.",
                    error_reason="schema_mismatch",
                    facts={
                        "invalid_field": "run_at",
                        "invalid_reason": "run_at_not_future",
                        "provided_run_at": next_run,
                        "provided_run_text": time.ctime(next_run),
                        "now": now,
                        "now_text": time.ctime(now),
                    },
                )
            cron = "once"
        else:
            kind = "recurring"
            if not cron:
                raise SchemaMismatch("watch.create kind=recurring requires cron.")
            try:
                validate_cron(cron)
                next_run = next_cron_time(cron)
            except ValueError as exc:
                return _failure_result(
                    "watch",
                    f"Invalid cron: {exc}",
                    error_reason="schema_mismatch",
                    facts={
                        "invalid_field": "cron",
                        "invalid_reason": str(exc),
                        "provided_cron": cron,
                        "provided_field_count": len(cron.split()),
                        "expected_format": "5-field cron: minute hour day month weekday",
                    },
                )
        runs = RunStore(self.home)
        graph = GraphStore(self.home)
        workspace = _resolve_workspace(context.workspace, default=self.project_dir)
        watch = runs.create_watch(
            cron=cron,
            prompt=prompt,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            next_run_at=next_run,
            workspace=workspace,
            kind=kind,
        )
        graph.upsert(
            "Watch",
            watch.id,
            {"cron": cron, "prompt": prompt, "sender_id": context.sender_id, "kind": kind},
        )
        facts = {
            **_transition_facts("watch", watch.id, "created"),
            "completion_evidence": True,
            "watch_id": watch.id,
            "cron": watch.cron,
            "kind": watch.kind,
            "prompt": watch.prompt,
            "next_run_at": watch.next_run_at,
            "next_run_text": time.ctime(watch.next_run_at),
        }
        facts["surface_message"] = _watch_created_surface_message(facts)
        return _fact_result(
            "watch",
            facts,
            run_id=watch.id,
        )


@capability("watch_delete")
class WatchDeleteCapability(BaseCapability):

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        watch_id = _arg_text(args, "watch_id")
        reason = _arg_text(args, "reason")
        if not watch_id:
            raise SchemaMismatch("watch.delete requires watch_id.")
        runs = RunStore(self.home)
        graph = GraphStore(self.home)
        deleted = runs.delete_watch(watch_id)
        if deleted is None:
            raise NotFound(f"watch not found: {watch_id}")
        graph.delete(deleted.id)
        facts = {
            **_transition_facts("watch", deleted.id, "deleted"),
            "completion_evidence": True,
            "deleted": True,
            "watch_id": deleted.id,
            "cron": deleted.cron,
            "prompt": deleted.prompt,
            "reason": reason,
            "surface_message": f"Watch deleted: {deleted.id}.",
        }
        return _fact_result(
            "watch",
            facts,
            run_id=deleted.id,
        )


def _watch_created_surface_message(facts: dict[str, Any]) -> str:
    next_run_text = str(facts.get("next_run_text") or "").strip()
    kind = str(facts.get("kind") or "").strip()
    cron = str(facts.get("cron") or "").strip()
    if kind == "recurring":
        return f"Recurring watch created. Next run: {next_run_text}. Cron: {cron}."
    return f"Watch created. Run at: {next_run_text}."
