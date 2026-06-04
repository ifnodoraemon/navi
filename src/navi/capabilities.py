from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .action_tools import action_handler_keys, load_action_tool_specs
from .config import load_config
from .connector_registry import load_connector_adapters
from .cron import next_cron_time, validate_cron
from .execution import ExecutionService
from .goals import GoalStore
from .governance import GovernanceEngine
from .hooks import HookDecision, HookEvent, HookRegistry
from .operating_context import permission_allows
from .graph import GraphStore
from .runs import RunStore
from .tools import ToolSpec, build_tool_gateway
from .trust import TrustStore


@dataclass(frozen=True)
class CapabilityContext:
    home: Path
    peer_id: str = ""
    sender_id: str = ""
    source: str = "local"
    permission_ceiling: str = "write"
    workspace: str = ""


@dataclass(frozen=True)
class CapabilityResult:
    ok: bool
    action: str
    observation: str
    message: str = ""
    run_id: str = ""
    terminal: bool = False
    facts: dict[str, Any] | None = None


@dataclass(frozen=True)
class CapabilityNode:
    name: str
    source: str
    permission: str
    facts_only: bool
    mutates: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    provider: str
    description: str = ""


class Capability(Protocol):
    spec: ToolSpec

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        ...


class CapabilityProvider(Protocol):
    def capabilities(self) -> Mapping[str, Capability]:
        ...


class CapabilityRegistry:
    """Agent OS syscall table.

    The model sees declared capabilities and chooses one. The kernel does not
    know capability names; it only asks this registry to validate and invoke.
    """

    def __init__(
        self,
        *,
        home: Path,
        project_dir: Path,
        allow_sources: set[str] | None = None,
        allowed_tools: set[str] | None = None,
        disabled_tools: set[str] | None = None,
        permission_ceiling: str = "write",
    ):
        self.home = home
        self.allow_sources = allow_sources
        self.allowed_tools = allowed_tools
        self.disabled_tools = disabled_tools or set()
        self.permission_ceiling = permission_ceiling
        self.gateway = build_tool_gateway(
            home,
            project_dir=project_dir,
            allow_sources=allow_sources,
            allowed_tools=allowed_tools,
            disabled_tools=disabled_tools,
            permission_ceiling=permission_ceiling,
        )
        self.providers: tuple[CapabilityProvider, ...] = (
            ActionCapabilityProvider(home=self.home, project_dir=self.gateway.project_dir),
            ToolGatewayCapabilityProvider(self.gateway),
        )
        self.hooks = HookRegistry(home)
        self.handlers = self._build_handlers()

    def refresh(self) -> None:
        self.gateway.refresh()
        self.handlers = self._build_handlers()

    def planner_specs(self, *, permission_ceiling: str | None = None) -> list[ToolSpec]:
        ceiling = permission_ceiling or self.permission_ceiling
        return sorted(
            [handler.spec for handler in self.handlers.values() if permission_allows(handler.spec.permission, ceiling)],
            key=lambda spec: spec.name,
        )

    def list_specs(self) -> list[ToolSpec]:
        return self.planner_specs()

    def capability_graph(self, *, permission_ceiling: str | None = None) -> list[CapabilityNode]:
        ceiling = permission_ceiling or self.permission_ceiling
        nodes = []
        for handler in self.handlers.values():
            spec = handler.spec
            if not permission_allows(spec.permission, ceiling):
                continue
            nodes.append(
                CapabilityNode(
                    name=spec.name,
                    source=spec.source,
                    permission=spec.permission,
                    facts_only=spec.facts_only,
                    mutates=spec.mutates,
                    input_schema=spec.input_schema,
                    output_schema=spec.output_schema,
                    provider="tool_gateway" if isinstance(handler, ToolCapability) else "action",
                    description=spec.description,
                )
            )
        return sorted(nodes, key=lambda node: node.name)

    def list_sources(self) -> list[str]:
        return sorted({handler.spec.source for handler in self.handlers.values()})

    def get(self, name: str) -> ToolSpec | None:
        handler = self.handlers.get(name)
        return handler.spec if handler else None

    async def invoke(
        self,
        name: str,
        args: dict[str, Any] | None,
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        handler = self.handlers.get(name)
        if handler is None:
            return CapabilityResult(
                ok=False,
                action="capability_error",
                observation=f"capability not found: {name}",
                message=f"capability not found: {name}",
                terminal=True,
            )
        if not permission_allows(permission, context.permission_ceiling):
            return CapabilityResult(
                ok=False,
                action="capability_error",
                observation=f"permission ceiling {context.permission_ceiling} blocks requested permission {permission}",
                message=f"permission ceiling {context.permission_ceiling} blocks requested permission {permission}",
                terminal=True,
            )
        if not permission_allows(handler.spec.permission, permission):
            return CapabilityResult(
                ok=False,
                action="capability_error",
                observation=f"capability {name} is not available with permission {permission}",
                message=f"capability {name} is not available with permission {permission}",
                terminal=True,
            )
        call_args = args or {}
        before_decisions = self.hooks.run(
            HookEvent(
                event="before_capability",
                payload={
                    "tool": name,
                    "permission": permission,
                    "source": context.source,
                    "sender_id": context.sender_id,
                    "workspace": context.workspace,
                    "mutates": handler.spec.mutates,
                    "args_keys": sorted(call_args),
                },
            )
        )
        blocked = _blocking_hook(before_decisions)
        if blocked is not None:
            facts = {"hook_decision": asdict(blocked)}
            return CapabilityResult(
                ok=False,
                action="capability_error",
                observation=blocked.reason or f"hook blocked capability: {blocked.hook}",
                message=blocked.reason or f"hook blocked capability: {blocked.hook}",
                terminal=True,
                facts=facts,
            )
        started_at = time.time()
        result = await handler.invoke(call_args, permission=permission, context=context)
        self.hooks.run(
            HookEvent(
                event="after_capability",
                payload={
                    "tool": name,
                    "permission": permission,
                    "source": context.source,
                    "sender_id": context.sender_id,
                    "workspace": context.workspace,
                    "ok": result.ok,
                    "action": result.action,
                    "run_id": result.run_id,
                    "fact_keys": sorted((result.facts or {}).keys()),
                },
            )
        )
        if handler.spec.mutates and not isinstance(handler, ToolCapability):
            self._audit_action_capability(handler.spec, call_args, result, started_at=started_at)
        return result

    def _build_handlers(self) -> Mapping[str, Capability]:
        handlers: dict[str, Capability] = {}
        for provider in self.providers:
            handlers.update(provider.capabilities())
        filtered = {
            name: handler
            for name, handler in handlers.items()
            if (self.allowed_tools is None or name in self.allowed_tools)
            and name not in self.disabled_tools
            and (self.allow_sources is None or handler.spec.source in self.allow_sources)
            and permission_allows(handler.spec.permission, self.permission_ceiling)
        }
        tools_list = filtered.get("tools.list")
        if tools_list is not None:
            filtered["tools.list"] = ToolsListCapability(tools_list.spec, registry=self)
        return filtered

    def _audit_action_capability(
        self,
        spec: ToolSpec,
        args: dict[str, Any],
        result: CapabilityResult,
        *,
        started_at: float,
    ) -> None:
        facts = result.facts or {
            "action": result.action,
            "run_id": result.run_id,
            "terminal": result.terminal,
        }
        try:
            RunStore(self.home).add_tool_call_log(
                tool=spec.name,
                args_json=json.dumps(args, ensure_ascii=False, sort_keys=True),
                ok=result.ok,
                facts_json=json.dumps(facts, ensure_ascii=False, sort_keys=True),
                error="" if result.ok else result.message or result.observation,
                started_at=started_at,
                ended_at=time.time(),
            )
        except Exception:
            pass


def _blocking_hook(decisions: list[HookDecision]) -> HookDecision | None:
    return next((decision for decision in decisions if decision.decision == "block"), None)


class ActionCapabilityProvider:
    def __init__(self, *, home: Path, project_dir: Path):
        self.home = home
        self.project_dir = project_dir

    def capabilities(self) -> Mapping[str, Capability]:
        specs = {spec.name: spec for spec in load_action_tool_specs()}
        factories = {
            "final_answer": lambda spec: FinalAnswerCapability(spec),
            "clarify": lambda spec: ClarifyCapability(spec),
            "delegate_spawn": lambda spec: DelegateSpawnCapability(spec, home=self.home, project_dir=self.project_dir),
            "delegate_prepare": lambda spec: DelegatePrepareCapability(spec, home=self.home),
            "approval_request": lambda spec: ApprovalRequestCapability(spec, home=self.home),
            "delegate_run": lambda spec: DelegateRunCapability(spec, home=self.home),
            "watch_create": lambda spec: WatchCreateCapability(spec, home=self.home, project_dir=self.project_dir),
            "delegate_delete": lambda spec: DelegateDeleteCapability(spec, home=self.home),
            "watch_delete": lambda spec: WatchDeleteCapability(spec, home=self.home),
            "approval_resolve": lambda spec: ApprovalResolveCapability(spec, home=self.home),
            "execution_retry": lambda spec: ExecutionRetryCapability(spec, home=self.home),
        }
        handlers = {}
        for name, handler_key in action_handler_keys().items():
            factory = factories.get(handler_key)
            if factory is None:
                raise ValueError(f"unknown action capability handler: {handler_key}")
            handlers[name] = factory(specs[name])
        return handlers


class ToolGatewayCapabilityProvider:
    def __init__(self, gateway):
        self.gateway = gateway

    def capabilities(self) -> Mapping[str, Capability]:
        return {spec.name: ToolCapability(spec, gateway=self.gateway) for spec in self.gateway.list_specs()}


class FinalAnswerCapability:
    def __init__(self, spec: ToolSpec):
        self.spec = spec

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        message = _arg_text(args, "message")
        return CapabilityResult(ok=True, action="chat", observation=message, message=message, terminal=True)


class ClarifyCapability:
    def __init__(self, spec: ToolSpec):
        self.spec = spec

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        message = _arg_text(args, "message")
        return CapabilityResult(ok=True, action="ask", observation=message, message=message, terminal=True)


class ToolCapability:
    def __init__(self, spec: ToolSpec, *, gateway):
        self.spec = spec
        self.gateway = gateway

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        result = self.gateway.call(self.spec.name, args)
        observation = result.error if not result.ok else json.dumps(
            {"capability": self.spec.name, "facts": result.facts},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        return CapabilityResult(
            ok=result.ok,
            action="tool",
            observation=observation,
            message=observation if not result.ok else "",
            facts=result.facts,
        )


class ToolsListCapability:
    def __init__(self, spec: ToolSpec, *, registry: CapabilityRegistry):
        self.spec = spec
        self.registry = registry

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        specs = self.registry.planner_specs(permission_ceiling=context.permission_ceiling)
        facts = {
            "category": "tools",
            "definition": "callable capabilities available in the current permission and source context",
            "not_skills": True,
            "tools": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "permission": spec.permission,
                    "facts_only": spec.facts_only,
                    "mutates": spec.mutates,
                    "source": spec.source,
                    "input_properties": sorted((spec.input_schema.get("properties") or {}).keys()),
                    "required": list(spec.input_schema.get("required") or []),
                }
                for spec in specs
            ],
            "count": len(specs),
        }
        return CapabilityResult(
            ok=True,
            action="tool",
            observation=json.dumps(
                {"capability": self.spec.name, "facts": facts},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            facts=facts,
        )


class DelegateSpawnCapability:
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
        prompt = _arg_text(args, "prompt")
        if not prompt:
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation="delegate.spawn requires an objective.",
                message="delegate.spawn requires an objective.",
                terminal=True,
            )
        config = load_config(self.home)
        runs = RunStore(self.home)
        graph = GraphStore(self.home)
        workspace = _resolve_workspace(context.workspace, default=self.project_dir)
        from .provider import build_provider

        decision = await GovernanceEngine(self.home).decide_task(
            prompt=prompt,
            sender_id=context.sender_id,
            workspace=workspace,
            provider=build_provider(config.model),
        )
        task = runs.create(
            title=prompt[:120],
            prompt=prompt,
            kind="delegation",
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            provider=config.execution.provider,
            workspace=workspace,
            autonomy_level=decision.level,
            trust_rule_id=decision.rule_id,
            why_now=f"trigger=model_capability; reason={decision.why}; autonomy={decision.level}",
        )
        graph.upsert("DelegationRun", task.id, {"objective": task.title, "status": task.status, "prompt": task.prompt})
        goal = GoalStore(self.home).create(
            objective=task.prompt,
            source=task.source,
            peer_id=task.peer_id,
            sender_id=task.sender_id,
            workspace=task.workspace,
            run_id=task.id,
            evidence={"run_id": task.id, "run_status": task.status, "autonomy_level": task.autonomy_level},
        )
        return _fact_result(
            "delegation",
            {
                **_transition_facts("delegation_run", task.id, "created"),
                "goal_id": goal.id,
                "run_id": task.id,
                "status": task.status,
                "autonomy_level": task.autonomy_level,
                "trust_rule_id": task.trust_rule_id,
            },
            run_id=task.id,
        )


class DelegatePrepareCapability:
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
        run_id = _arg_text(args, "run_id") or _arg_text(args, "run_id")
        task = RunStore(self.home).get(run_id) if run_id else None
        if task is None:
            return CapabilityResult(ok=False, action="delegation", observation=f"delegation run not found: {run_id}", message=f"delegation run not found: {run_id}", terminal=True)
        planned = await ExecutionService(self.home).plan_task(task)
        GoalStore(self.home).update_for_run(planned, evidence={"run_id": planned.id, "run_status": planned.status})
        return _fact_result(
            "delegation",
            {
                **_transition_facts("delegation_run", planned.id, "updated"),
                "run_id": planned.id,
                "status": planned.status,
                "plan_summary": planned.plan_summary,
            },
            run_id=planned.id,
        )


class ApprovalRequestCapability:
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
        run_id = _arg_text(args, "run_id") or _arg_text(args, "run_id")
        runs = RunStore(self.home)
        task = runs.get(run_id) if run_id else None
        if task is None:
            return CapabilityResult(ok=False, action="approval", observation=f"delegation run not found: {run_id}", message=f"delegation run not found: {run_id}", terminal=True)
        approval = runs.create_approval(run_id=task.id, peer_id=context.peer_id or task.peer_id, sender_id=context.sender_id or task.sender_id)
        awaiting = runs.update_run(task.id, status="awaiting_approval") or task
        GoalStore(self.home).update_for_run(
            awaiting,
            evidence={"run_id": awaiting.id, "run_status": awaiting.status, "approval_status": approval.status},
        )
        return _fact_result(
            "approval",
            {
                **_transition_facts("approval_request", approval.id, "created"),
                "run_id": awaiting.id,
                "status": awaiting.status,
                "approval": {"action": approval.action, "code": approval.code, "expires_at": approval.expires_at},
            },
            run_id=awaiting.id,
        )


class DelegateRunCapability:
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
        run_id = _arg_text(args, "run_id") or _arg_text(args, "run_id")
        runs = RunStore(self.home)
        task = runs.get(run_id) if run_id else None
        if task is None:
            return CapabilityResult(ok=False, action="delegation", observation=f"delegation run not found: {run_id}", message=f"delegation run not found: {run_id}", terminal=True)
        execution = ExecutionService(self.home)
        if not execution.execution_allowed(task):
            return CapabilityResult(ok=False, action="delegation", observation="execution grant missing", message="execution grant missing", terminal=True)
        queued = runs.update_run(task.id, status="queued") or task
        GoalStore(self.home).update_for_run(queued, evidence={"run_id": queued.id, "run_status": queued.status})
        return _fact_result(
            "delegation",
            {
                **_transition_facts("delegation_run", queued.id, "updated"),
                "run_id": queued.id,
                "status": queued.status,
            },
            run_id=queued.id,
        )


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
        kind = _arg_text(args, "kind") or ("once" if args.get("run_at") is not None or run_at_text else "recurring")
        prompt = _arg_text(args, "prompt")
        if not prompt:
            return CapabilityResult(
                ok=False,
                action="watch",
                observation="watch.create requires prompt.",
                message="watch.create requires prompt.",
                terminal=True,
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
                    terminal=True,
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
                    terminal=True,
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
                    terminal=True,
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
        graph.upsert("Watch", watch.id, {"cron": cron, "prompt": prompt, "sender_id": context.sender_id, "kind": kind})
        facts = {
            **_transition_facts("watch", watch.id, "created"),
            "watch_id": watch.id,
            "cron": watch.cron,
            "kind": watch.kind,
            "prompt": watch.prompt,
            "next_run_at": watch.next_run_at,
            "next_run_text": time.ctime(watch.next_run_at),
        }
        return _fact_result("watch", facts, run_id=watch.id)


class DelegateDeleteCapability:
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
        run_id = _arg_text(args, "run_id") or _arg_text(args, "run_id")
        if not run_id:
            return self._delete_by_filter(args)
        runs = RunStore(self.home)
        graph = GraphStore(self.home)
        task = runs.get(run_id)
        if task is not None and _remote_source(context.source) and task.status != "failed":
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation="remote delegate.delete can only delete failed delegation runs.",
                message="remote delegate.delete can only delete failed delegation runs.",
                terminal=True,
            )
        deleted = runs.delete_run(run_id)
        if deleted is None:
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation=f"delegation run not found: {run_id}",
                message=f"delegation run not found: {run_id}",
                terminal=True,
            )
        graph.delete(deleted.id)
        return _fact_result(
            "delegation",
            {
                **_transition_facts("delegation_run", deleted.id, "deleted"),
                "deleted": True,
                "run_id": deleted.id,
                "title": deleted.title,
                "status": deleted.status,
            },
            run_id=deleted.id,
        )

    def _delete_by_filter(self, args: dict[str, Any]) -> CapabilityResult:
        status = _arg_text(args, "status") or "failed"
        if status != "failed":
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation="delegate.delete bulk cleanup only supports status=failed.",
                message="delegate.delete bulk cleanup only supports status=failed.",
                terminal=True,
            )
        raw_limit = args.get("limit")
        limit = _positive_int(raw_limit, default=5000, maximum=5000) if raw_limit is not None else None
        source = _arg_text(args, "source")
        kind = _arg_text(args, "kind")
        runs = RunStore(self.home)
        graph = GraphStore(self.home)
        before_count = runs.count_runs(status="failed", source=source, kind=kind)
        candidates = runs.list_by_status_filtered("failed", source=source, kind=kind, limit=limit)
        deleted = []
        for task in candidates:
            removed = runs.delete_run(task.id)
            if removed is None:
                continue
            graph.delete(removed.id)
            deleted.append(
                {
                    "run_id": removed.id,
                    "title": removed.title,
                    "source": removed.source,
                    "kind": removed.kind,
                    "updated_at": removed.updated_at,
                }
            )
        remaining_count = runs.count_runs(status="failed", source=source, kind=kind)
        return _fact_result(
            "delegation",
            {
                **_transition_facts("delegation_run", "", "deleted"),
                "entity_count": len(deleted),
                "before_count": before_count,
                "deleted_count": len(deleted),
                "deleted_runs": deleted,
                "remaining_count": remaining_count,
                "cleanup_complete": remaining_count == 0,
                "status_filter": "failed",
                "source_filter": source,
                "kind_filter": kind,
                "limit_filter": limit,
            },
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
                terminal=True,
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
                terminal=True,
            )
        graph.delete(deleted.id)
        return _fact_result(
            "watch",
            {
                **_transition_facts("watch", deleted.id, "deleted"),
                "deleted": True,
                "watch_id": deleted.id,
                "cron": deleted.cron,
                "prompt": deleted.prompt,
            },
            run_id=deleted.id,
        )


class ApprovalResolveCapability:
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
        decision = _arg_text(args, "decision").lower()
        if decision not in {"approve", "reject"}:
            return CapabilityResult(
                ok=False,
                action="approval",
                observation="approval.resolve requires decision approve or reject.",
                message="approval.resolve requires decision approve or reject.",
                terminal=True,
            )
        status = "approved" if decision == "approve" else "rejected"
        code = _arg_text(args, "code")
        run_id = _arg_text(args, "run_id") or _arg_text(args, "run_id")
        runs = RunStore(self.home)
        governance = GovernanceEngine(self.home)
        trust = TrustStore(self.home)
        approval = self._resolve(governance, code=code, run_id=run_id, sender_id=context.sender_id, status=status)
        if approval is None:
            facts = runs.approval_resolution_diagnostic(code=code, run_id=run_id, sender_id=context.sender_id)
            message = _approval_resolution_failure_message(facts)
            return CapabilityResult(
                ok=False,
                action="approval",
                observation=message,
                message=message,
                terminal=True,
                facts={"approval_resolution": facts},
            )
        if approval.status == "expired":
            return CapabilityResult(
                ok=False,
                action="approval",
                observation="Approval code expired. Create a new delegation run.",
                message="Approval code expired. Create a new delegation run.",
                terminal=True,
            )
        if status == "approved":
            task = runs.update_run(approval.run_id, status="queued")
            resolved_run_id = task.id if task else approval.run_id
            if task:
                GoalStore(self.home).update_for_run(task, evidence={"run_id": task.id, "run_status": task.status, "approval_status": approval.status})
            return _fact_result(
                "approval",
                {
                    **_transition_facts("approval_request", approval.id, "updated"),
                    "run_id": resolved_run_id,
                    "approval_status": approval.status,
                    "run_status": "queued",
                },
                run_id=resolved_run_id,
            )
        task = runs.update_run(approval.run_id, status="rejected")
        if task:
            await trust.record_failure(task)
            GoalStore(self.home).update_for_run(task, evidence={"run_id": task.id, "run_status": task.status, "approval_status": approval.status})
        return _fact_result(
            "approval",
            {
                **_transition_facts("approval_request", approval.id, "updated"),
                "run_id": approval.run_id,
                "approval_status": approval.status,
                "run_status": "rejected",
            },
            run_id=approval.run_id,
        )

    @staticmethod
    def _resolve(
        governance: GovernanceEngine,
        *,
        code: str,
        run_id: str,
        sender_id: str,
        status: str,
    ):
        if code:
            return governance.resolve_code(code=code, sender_id=sender_id, status=status)
        if run_id:
            return governance.resolve_task(run_id=run_id, sender_id=sender_id, status=status)
        return None


def _approval_resolution_failure_message(facts: dict[str, Any]) -> str:
    reason = str(facts.get("reason") or "")
    messages = {
        "approval_code_not_found": "Approval code was not found.",
        "sender_mismatch": "Approval exists but belongs to a different sender.",
        "approval_not_pending": f"Approval is not pending; current status is {facts.get('status') or 'unknown'}.",
        "approval_expired": "Approval is expired. Create a new approval request.",
        "run_not_found": "Run was not found for approval resolution.",
        "run_has_no_approval": "Run has no approval request.",
        "approval_identifier_missing": "approval.resolve requires code or run_id.",
    }
    return messages.get(reason, "Approval could not be resolved.")


class ExecutionRetryCapability:
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
        run_id = _arg_text(args, "run_id") or _arg_text(args, "run_id")
        if not run_id:
            return CapabilityResult(
                ok=False,
                action="execution",
                observation="delegate.retry requires run_id.",
                message="delegate.retry requires run_id.",
                terminal=True,
            )
        runs = RunStore(self.home)
        task = runs.get(run_id)
        if task is None:
            return CapabilityResult(
                ok=False,
                action="execution",
                observation=f"delegation run not found: {run_id}",
                message=f"delegation run not found: {run_id}",
                terminal=True,
            )
        execution = ExecutionService(self.home)
        if not execution.execution_allowed(task):
            return CapabilityResult(
                ok=False,
                action="execution",
                observation="execution grant missing: approved approval or explicit L3 trust rule required",
                message="execution grant missing: approved approval or explicit L3 trust rule required",
                terminal=True,
            )
        follow_up = _arg_text(args, "follow_up_prompt")
        retry_task = replace(task, prompt=f"{task.prompt}\n\nFollow-up execution instruction:\n{follow_up}" if follow_up else task.prompt)
        result = await execution.execute_task(retry_task)
        facts = {
            **_transition_facts("execution_attempt", result.id, "created"),
            "run_id": result.id,
            "status": result.status,
            "result_summary": result.result_summary,
            "error": result.error,
        }
        return _fact_result("execution", facts, run_id=result.id)


def build_capability_registry(
    home: Path,
    *,
    project_dir: Path,
    allow_sources: set[str] | None = None,
    allowed_tools: set[str] | None = None,
    disabled_tools: set[str] | None = None,
    permission_ceiling: str = "write",
) -> CapabilityRegistry:
    return CapabilityRegistry(
        home=home,
        project_dir=project_dir,
        allow_sources=allow_sources,
        allowed_tools=allowed_tools,
        disabled_tools=disabled_tools,
        permission_ceiling=permission_ceiling,
    )


def _fact_result(action: str, facts: dict[str, Any], *, run_id: str = "") -> CapabilityResult:
    return CapabilityResult(
        ok=True,
        action=action,
        observation=json.dumps(facts, ensure_ascii=False, sort_keys=True),
        run_id=run_id,
        facts=facts,
    )


def _transition_facts(entity_type: str, entity_id: str, transition: str) -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "state_transition": transition,
        "turn_scope": "current",
    }


def _arg_text(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    return str(value).strip() if value is not None else ""


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_one_shot_run_at(text: str, *, now: float | None = None) -> float | None:
    raw = text.strip()
    if not raw:
        return None
    base = datetime.fromtimestamp(now or time.time())
    day_offset = 1 if "\u660e\u5929" in raw else 0
    hour, minute = _parse_clock_time(raw)
    if hour is None:
        return None
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=day_offset)
    if day_offset == 0 and candidate.timestamp() <= base.timestamp():
        candidate += timedelta(days=1)
    return candidate.timestamp()


def _parse_clock_time(text: str) -> tuple[int | None, int]:
    match = re.search(r"(^|\D)([01]?\d|2[0-3])[:：]([0-5]\d)", text)
    if match:
        return int(match.group(2)), int(match.group(3))
    match = re.search(r"(\d{1,2})\s*(?:\u70b9|\u65f6)(?:\s*([0-5]?\d)\s*\u5206?)?", text)
    if not match:
        return None, 0
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if "\u4e0b\u5348" in text and hour < 12:
        hour += 12
    if "\u665a\u4e0a" in text and hour < 12:
        hour += 12
    if "\u4e2d\u5348" in text and hour < 12:
        hour += 12
    if hour > 23 or minute > 59:
        return None, 0
    return hour, minute


def _positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def _remote_source(source: str) -> bool:
    raw = source.strip()
    if not raw:
        return False
    connector_sources: set[str] = set()
    for adapter in load_connector_adapters():
        connector_sources.update({adapter.name, adapter.spec.surface, adapter.spec.local_source})
    return raw in connector_sources


def _resolve_workspace(workspace: str, *, default: Path) -> str:
    raw = workspace.strip() if workspace else ""
    return str(Path(raw).expanduser().resolve()) if raw else str(default.resolve())
