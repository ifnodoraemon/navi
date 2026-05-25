from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .action_tools import action_handler_keys, load_action_tool_specs
from .config import load_config
from .cron import next_cron_time, validate_cron
from .execution import ExecutionService
from .governance import GovernanceEngine
from .operating_context import permission_allows
from .graph import GraphStore
from .tasks import TaskStore
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
    task_id: str = ""
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
        project_dir: Path | None = None,
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
            ActionCapabilityProvider(home=self.home),
            ToolGatewayCapabilityProvider(self.gateway),
        )
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
        started_at = time.time()
        result = await handler.invoke(call_args, permission=permission, context=context)
        if handler.spec.mutates and not isinstance(handler, ToolCapability):
            self._audit_action_capability(handler.spec, call_args, result, started_at=started_at)
        return result

    def _build_handlers(self) -> Mapping[str, Capability]:
        handlers: dict[str, Capability] = {}
        for provider in self.providers:
            handlers.update(provider.capabilities())
        return {
            name: handler
            for name, handler in handlers.items()
            if (self.allowed_tools is None or name in self.allowed_tools)
            and name not in self.disabled_tools
            and (self.allow_sources is None or handler.spec.source in self.allow_sources)
            and permission_allows(handler.spec.permission, self.permission_ceiling)
        }

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
            "task_id": result.task_id,
            "terminal": result.terminal,
        }
        try:
            TaskStore(self.home).add_tool_call_log(
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


class ActionCapabilityProvider:
    def __init__(self, *, home: Path):
        self.home = home

    def capabilities(self) -> Mapping[str, Capability]:
        specs = {spec.name: spec for spec in load_action_tool_specs()}
        factories = {
            "final_answer": lambda spec: FinalAnswerCapability(spec),
            "clarify": lambda spec: ClarifyCapability(spec),
            "task_record": lambda spec: TaskRecordCapability(spec, home=self.home),
            "task_prepare": lambda spec: TaskPrepareCapability(spec, home=self.home),
            "approval_request": lambda spec: ApprovalRequestCapability(spec, home=self.home),
            "task_queue": lambda spec: TaskQueueCapability(spec, home=self.home),
            "watch_create": lambda spec: WatchCreateCapability(spec, home=self.home),
            "task_delete": lambda spec: TaskDeleteCapability(spec, home=self.home),
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


class TaskRecordCapability:
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
        prompt = _arg_text(args, "prompt")
        if not prompt:
            return CapabilityResult(
                ok=False,
                action="task",
                observation="task.record requires a prompt.",
                message="task.record requires a prompt.",
                terminal=True,
            )
        config = load_config(self.home)
        tasks = TaskStore(self.home)
        graph = GraphStore(self.home)
        workspace = _resolve_workspace(context.workspace)
        from .provider import build_provider

        decision = await GovernanceEngine(self.home).decide_task(
            prompt=prompt,
            sender_id=context.sender_id,
            workspace=workspace,
            provider=build_provider(config.model),
        )
        task = tasks.create(
            title=prompt[:120],
            prompt=prompt,
            kind="task",
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            provider=config.execution.provider,
            workspace=workspace,
            autonomy_level=decision.level,
            trust_rule_id=decision.rule_id,
            why_now=f"trigger=model_capability; reason={decision.why}; autonomy={decision.level}",
        )
        graph.upsert("Task", task.id, {"title": task.title, "status": task.status, "prompt": task.prompt})
        return _fact_result(
            "task",
            {
                "task_id": task.id,
                "status": task.status,
                "autonomy_level": task.autonomy_level,
                "trust_rule_id": task.trust_rule_id,
            },
            task_id=task.id,
        )


class TaskPrepareCapability:
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
        task_id = _arg_text(args, "task_id")
        task = TaskStore(self.home).get(task_id) if task_id else None
        if task is None:
            return CapabilityResult(ok=False, action="task", observation=f"task not found: {task_id}", message=f"task not found: {task_id}", terminal=True)
        planned = await ExecutionService(self.home).plan_task(task)
        return _fact_result(
            "task",
            {"task_id": planned.id, "status": planned.status, "plan_summary": planned.plan_summary},
            task_id=planned.id,
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
        task_id = _arg_text(args, "task_id")
        tasks = TaskStore(self.home)
        task = tasks.get(task_id) if task_id else None
        if task is None:
            return CapabilityResult(ok=False, action="approval", observation=f"task not found: {task_id}", message=f"task not found: {task_id}", terminal=True)
        approval = tasks.create_approval(task_id=task.id, peer_id=context.peer_id or task.peer_id, sender_id=context.sender_id or task.sender_id)
        awaiting = tasks.update_task(task.id, status="awaiting_approval") or task
        return _fact_result(
            "approval",
            {
                "task_id": awaiting.id,
                "status": awaiting.status,
                "approval": {"action": approval.action, "code": approval.code, "expires_at": approval.expires_at},
            },
            task_id=awaiting.id,
        )


class TaskQueueCapability:
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
        task_id = _arg_text(args, "task_id")
        tasks = TaskStore(self.home)
        task = tasks.get(task_id) if task_id else None
        if task is None:
            return CapabilityResult(ok=False, action="task", observation=f"task not found: {task_id}", message=f"task not found: {task_id}", terminal=True)
        execution = ExecutionService(self.home)
        if not execution.execution_allowed(task):
            return CapabilityResult(ok=False, action="task", observation="execution grant missing", message="execution grant missing", terminal=True)
        queued = tasks.update_task(task.id, status="queued") or task
        return _fact_result("task", {"task_id": queued.id, "status": queued.status}, task_id=queued.id)


class WatchCreateCapability:
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
        cron = _arg_text(args, "cron")
        prompt = _arg_text(args, "prompt")
        if not cron or not prompt:
            return CapabilityResult(
                ok=False,
                action="watch",
                observation="watch.create requires cron and prompt.",
                message="watch.create requires cron and prompt.",
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
        tasks = TaskStore(self.home)
        graph = GraphStore(self.home)
        watch = tasks.create_watch(
            cron=cron,
            prompt=prompt,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            next_run_at=next_run,
            workspace=context.workspace,
        )
        graph.upsert("Watch", watch.id, {"cron": cron, "prompt": prompt, "sender_id": context.sender_id})
        return _fact_result(
            "watch",
            {
                "watch_id": watch.id,
                "cron": watch.cron,
                "prompt": watch.prompt,
                "next_run_at": watch.next_run_at,
                "next_run_text": time.ctime(watch.next_run_at),
            },
            task_id=watch.id,
        )


class TaskDeleteCapability:
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
        task_id = _arg_text(args, "task_id")
        if not task_id:
            return self._delete_by_filter(args)
        tasks = TaskStore(self.home)
        graph = GraphStore(self.home)
        task = tasks.get(task_id)
        if task is not None and _remote_source(context.source) and task.status != "failed":
            return CapabilityResult(
                ok=False,
                action="task",
                observation="remote task.delete can only delete failed task records.",
                message="remote task.delete can only delete failed task records.",
                terminal=True,
            )
        deleted = tasks.delete_task(task_id)
        if deleted is None:
            return CapabilityResult(
                ok=False,
                action="task",
                observation=f"task not found: {task_id}",
                message=f"task not found: {task_id}",
                terminal=True,
            )
        graph.delete(deleted.id)
        return _fact_result(
            "task",
            {
                "deleted": True,
                "task_id": deleted.id,
                "title": deleted.title,
                "status": deleted.status,
            },
            task_id=deleted.id,
        )

    def _delete_by_filter(self, args: dict[str, Any]) -> CapabilityResult:
        status = _arg_text(args, "status") or "failed"
        if status != "failed":
            return CapabilityResult(
                ok=False,
                action="task",
                observation="task.delete bulk cleanup only supports status=failed.",
                message="task.delete bulk cleanup only supports status=failed.",
                terminal=True,
            )
        limit = _positive_int(args.get("limit"), default=50, maximum=500)
        source = _arg_text(args, "source")
        kind = _arg_text(args, "kind")
        tasks = TaskStore(self.home)
        graph = GraphStore(self.home)
        candidates = [
            task
            for task in tasks.list_by_status("failed", limit=limit)
            if (not source or task.source == source) and (not kind or task.kind == kind)
        ]
        deleted = []
        for task in candidates:
            removed = tasks.delete_task(task.id)
            if removed is None:
                continue
            graph.delete(removed.id)
            deleted.append(
                {
                    "task_id": removed.id,
                    "title": removed.title,
                    "source": removed.source,
                    "kind": removed.kind,
                    "updated_at": removed.updated_at,
                }
            )
        return _fact_result(
            "task",
            {
                "deleted_count": len(deleted),
                "deleted_tasks": deleted,
                "status_filter": "failed",
                "source_filter": source,
                "kind_filter": kind,
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
        tasks = TaskStore(self.home)
        graph = GraphStore(self.home)
        deleted = tasks.delete_watch(watch_id)
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
                "deleted": True,
                "watch_id": deleted.id,
                "cron": deleted.cron,
                "prompt": deleted.prompt,
            },
            task_id=deleted.id,
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
        task_id = _arg_text(args, "task_id")
        tasks = TaskStore(self.home)
        governance = GovernanceEngine(self.home)
        trust = TrustStore(self.home)
        approval = self._resolve(governance, code=code, task_id=task_id, sender_id=context.sender_id, status=status)
        if approval is None:
            facts = tasks.approval_resolution_diagnostic(code=code, task_id=task_id, sender_id=context.sender_id)
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
                observation="Approval code expired. Create a new task.",
                message="Approval code expired. Create a new task.",
                terminal=True,
            )
        if status == "approved":
            task = tasks.update_task(approval.task_id, status="queued")
            resolved_task_id = task.id if task else approval.task_id
            return _fact_result(
                "approval",
                {"task_id": resolved_task_id, "approval_status": approval.status, "task_status": "queued"},
                task_id=resolved_task_id,
            )
        task = tasks.update_task(approval.task_id, status="rejected")
        if task:
            await trust.record_failure(task)
        return _fact_result(
            "approval",
            {"task_id": approval.task_id, "approval_status": approval.status, "task_status": "rejected"},
            task_id=approval.task_id,
        )

    @staticmethod
    def _resolve(
        governance: GovernanceEngine,
        *,
        code: str,
        task_id: str,
        sender_id: str,
        status: str,
    ):
        if code:
            return governance.resolve_code(code=code, sender_id=sender_id, status=status)
        if task_id:
            return governance.resolve_task(task_id=task_id, sender_id=sender_id, status=status)
        return None


def _approval_resolution_failure_message(facts: dict[str, Any]) -> str:
    reason = str(facts.get("reason") or "")
    messages = {
        "approval_code_not_found": "Approval code was not found.",
        "sender_mismatch": "Approval exists but belongs to a different sender.",
        "approval_not_pending": f"Approval is not pending; current status is {facts.get('status') or 'unknown'}.",
        "approval_expired": "Approval is expired. Create a new approval request.",
        "task_not_found": "Task was not found for approval resolution.",
        "task_has_no_approval": "Task has no approval request.",
        "approval_identifier_missing": "approval.resolve requires code or task_id.",
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
        task_id = _arg_text(args, "task_id")
        if not task_id:
            return CapabilityResult(
                ok=False,
                action="execution",
                observation="execution.retry requires task_id.",
                message="execution.retry requires task_id.",
                terminal=True,
            )
        tasks = TaskStore(self.home)
        task = tasks.get(task_id)
        if task is None:
            return CapabilityResult(
                ok=False,
                action="execution",
                observation=f"task not found: {task_id}",
                message=f"task not found: {task_id}",
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
            "task_id": result.id,
            "status": result.status,
            "result_summary": result.result_summary,
            "error": result.error,
        }
        return _fact_result("execution", facts, task_id=result.id)


def build_capability_registry(
    home: Path,
    *,
    project_dir: Path | None = None,
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


def _fact_result(action: str, facts: dict[str, Any], *, task_id: str = "") -> CapabilityResult:
    return CapabilityResult(
        ok=True,
        action=action,
        observation=json.dumps(facts, ensure_ascii=False, sort_keys=True),
        task_id=task_id,
        facts=facts,
    )


def _arg_text(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    return str(value).strip() if value is not None else ""


def _positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def _remote_source(source: str) -> bool:
    return source.startswith("connector.") or source in {"weixin", "telegram"}


def _resolve_workspace(workspace: str) -> str:
    raw = workspace.strip() if workspace else ""
    return str(Path(raw or Path.cwd()).expanduser().resolve())
