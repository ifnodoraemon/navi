from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..capabilities_types import CapabilityContext, CapabilityResult
from ..tools import ToolSpec
from .helpers import (
    arg_text as _arg_text,
    transition_facts as _transition_facts,
    fact_result as _fact_result,
    resolve_workspace as _resolve_workspace,
    float_or_none as _float_or_none,
    parse_one_shot_run_at as _parse_one_shot_run_at,
)
from ..cron import next_cron_time, validate_cron
from ..runs import RunStore
from ..graph import GraphStore


class WatchCreateCapability:
    def __init__(self, spec: ToolSpec, *, home: Path, project_dir: Path):
        self.spec = spec
        self.home = home
        self.project_dir = project_dir

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        cron = _arg_text(args, "cron")
        run_at_text = _arg_text(args, "run_at_text")
        kind = _arg_text(args, "kind") or (
            "once" if args.get("run_at") is not None or run_at_text else "recurring"
        )
        prompt = _arg_text(args, "prompt")
        if not prompt:
            return CapabilityResult(
                ok=False,
                action="watch",
                observation="watch.create requires prompt.",
                message="watch.create requires prompt.",
                terminal=False,
                error_reason="schema_mismatch",
            )
        if kind == "once":
            next_run = _float_or_none(args.get("run_at"))
            if next_run is None and run_at_text:
                next_run = _parse_one_shot_run_at(run_at_text)
            if next_run is None:
                return CapabilityResult(
                    ok=False,
                    action="watch",
                    observation="watch.create kind=once requires run_at or run_at_text.",
                    message="watch.create kind=once requires run_at or run_at_text.",
                    terminal=False,
                    error_reason="schema_mismatch",
                )
            cron = "once"
        else:
            kind = "recurring"
            if not cron:
                return CapabilityResult(
                    ok=False,
                    action="watch",
                    observation="watch.create kind=recurring requires cron.",
                    message="watch.create kind=recurring requires cron.",
                    terminal=False,
                    error_reason="schema_mismatch",
                )
            try:
                validate_cron(cron)
                next_run = next_cron_time(cron)
            except ValueError as exc:
                return CapabilityResult(
                    ok=False,
                    action="watch",
                    observation=f"Invalid cron: {exc}",
                    message=f"Invalid cron: {exc}",
                    terminal=False,
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
            "watch_id": watch.id,
            "cron": watch.cron,
            "kind": watch.kind,
            "prompt": watch.prompt,
            "next_run_at": watch.next_run_at,
            "next_run_text": time.ctime(watch.next_run_at),
        }
        return _fact_result(
            "watch",
            facts,
            run_id=watch.id,
        )


class WatchDeleteCapability:
    def __init__(self, spec: ToolSpec, *, home: Path):
        self.spec = spec
        self.home = home

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        watch_id = _arg_text(args, "watch_id")
        if not watch_id:
            return CapabilityResult(
                ok=False,
                action="watch",
                observation="watch.delete requires watch_id.",
                message="watch.delete requires watch_id.",
                terminal=False,
                error_reason="schema_mismatch",
            )
        runs = RunStore(self.home)
        graph = GraphStore(self.home)
        deleted = runs.delete_watch(watch_id)
        if deleted is None:
            return CapabilityResult(
                ok=False,
                action="watch",
                observation=f"watch not found: {watch_id}",
                message=f"watch not found: {watch_id}",
                terminal=False,
            )
        graph.delete(deleted.id)
        facts = {
            **_transition_facts("watch", deleted.id, "deleted"),
            "deleted": True,
            "watch_id": deleted.id,
            "cron": deleted.cron,
            "prompt": deleted.prompt,
        }
        return _fact_result(
            "watch",
            facts,
            run_id=deleted.id,
        )
