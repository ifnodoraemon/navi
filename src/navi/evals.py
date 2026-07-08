from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .app_factory import build_runtime
from .capabilities import build_capability_registry
from .connector_registry import get_connector_adapter
from .control_plane import TurnController
from .execution import ExecutionService
from .goals import GoalStore
from .lifecycle import Phase, Resolution
from .provider import ModelPool
from .runtime import AgentRuntime
from .runs import RunStore
from .syscalls import ModelSyscall, ModelSyscallPlanner
from .tools import ToolSpec


@dataclass(frozen=True)
class EvalResult:
    id: str
    ok: bool
    expected: dict[str, Any]
    actual: dict[str, Any]
    errors: list[str]


@dataclass(frozen=True)
class DailyJourneyResult:
    id: str
    ok: bool
    errors: list[str]
    events: list[dict[str, Any]]


@dataclass(frozen=True)
class ClawEvalResult:
    task_id: str
    ok: bool
    split: str
    category: str
    language: str
    pass_count: int
    attempts: int
    error_domains: list[str]
    errors: list[str]
    attempts_detail: list[dict[str, Any]]


def load_delegation_eval_cases(path: Path) -> list[dict[str, Any]]:
    return load_delegation_eval_dataset(path)["cases"]


def load_delegation_eval_dataset(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = {} if loaded is None else loaded
    if not isinstance(data, dict):
        raise ValueError("eval dataset must be a mapping")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError("eval dataset must contain a cases list")
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"case {index} must be a mapping")
    return data


def load_daily_journey_eval_dataset(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = {} if loaded is None else loaded
    if not isinstance(data, dict):
        raise ValueError("daily journey eval dataset must be a mapping")
    journeys = data.get("journeys")
    if not isinstance(journeys, list):
        raise ValueError("daily journey eval dataset must contain a journeys list")
    for index, journey in enumerate(journeys):
        if not isinstance(journey, dict):
            raise ValueError(f"journey {index} must be a mapping")
        if "simulator" not in journey:
            steps = journey.get("steps")
            if not isinstance(steps, list) or not steps:
                raise ValueError(
                    f"journey {journey.get('id') or index} must contain non-empty steps or a simulator"
                )
    return data


def load_claw_eval_dataset(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = {} if loaded is None else loaded
    if not isinstance(data, dict):
        raise ValueError("claw eval dataset must be a mapping")
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("claw eval dataset must contain a tasks list")
    seen: set[str] = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"task {index} must be a mapping")
        task_id = str(task.get("task_id") or "").strip()
        prefix = task_id or f"task[{index}]"
        if not task_id:
            raise ValueError(f"{prefix}: missing task_id")
        if task_id in seen:
            raise ValueError(f"{prefix}: duplicate task_id")
        seen.add(task_id)
        for key in ("query", "language", "category", "split"):
            if not str(task.get(key) or "").strip():
                raise ValueError(f"{prefix}: missing {key}")
        if str(task.get("split")) not in {"general", "multimodal", "multi_turn"}:
            raise ValueError(f"{prefix}: split must be general, multimodal, or multi_turn")
        journey = task.get("journey")
        if not isinstance(journey, dict):
            raise ValueError(f"{prefix}: missing journey mapping")
        steps = journey.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"{prefix}: journey must contain non-empty steps")
    return data


def load_connector_journey_eval_dataset(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = {} if loaded is None else loaded
    if not isinstance(data, dict):
        raise ValueError("connector journey eval dataset must be a mapping")
    connector = str(data.get("connector") or "").strip()
    if not connector:
        raise ValueError("connector journey eval dataset must declare connector")
    adapter = get_connector_adapter(connector)
    if adapter is None or adapter.load_journey_eval_dataset is None:
        raise ValueError(f"connector journey eval is not available for {connector!r}")
    return adapter.load_journey_eval_dataset(path)


async def run_connector_journey_eval_dataset(
    *,
    home: Path,
    project_dir: Path,
    dataset: Path,
    timeout_seconds: float = 30.0,
) -> list[Any]:
    loaded = load_connector_journey_eval_dataset(dataset)
    connector = str(loaded.get("connector") or "").strip()
    adapter = get_connector_adapter(connector)
    if adapter is None or adapter.run_journey_eval_dataset is None:
        raise ValueError(f"connector journey eval is not available for {connector!r}")
    return await adapter.run_journey_eval_dataset(home, project_dir, dataset, timeout_seconds)


async def run_daily_journey_eval_dataset(
    *,
    home: Path,
    project_dir: Path,
    dataset: Path,
    timeout_seconds: float = 30.0,
    provider: ModelPool | None = None,
) -> list[DailyJourneyResult]:
    loaded = load_daily_journey_eval_dataset(dataset)
    results: list[DailyJourneyResult] = []
    run_root = home / "daily_journeys" / _eval_run_id()
    for journey in loaded["journeys"]:
        journey_home = run_root / _safe_path_name(str(journey.get("id") or "journey"))
        result = await asyncio.wait_for(
            _run_daily_journey(
                home=journey_home, project_dir=project_dir, journey=journey, provider=provider
            ),
            timeout=timeout_seconds,
        )
        results.append(result)
    return results


async def run_claw_eval_dataset(
    *,
    home: Path,
    project_dir: Path,
    dataset: Path,
    attempts: int = 3,
    timeout_seconds: float = 30.0,
    provider: ModelPool | None = None,
) -> list[ClawEvalResult]:
    loaded = load_claw_eval_dataset(dataset)
    run_attempts = attempts if attempts > 0 else int(loaded.get("pass_at") or 3)
    results: list[ClawEvalResult] = []
    run_root = home / "claw_eval" / _eval_run_id()
    for task in loaded["tasks"]:
        task_id = str(task["task_id"])
        attempt_details: list[dict[str, Any]] = []
        errors: list[str] = []
        for attempt in range(1, run_attempts + 1):
            attempt_home = run_root / _safe_path_name(task_id) / f"attempt_{attempt}"
            journey = _claw_task_to_journey(task)
            try:
                result = await asyncio.wait_for(
                    _run_daily_journey(
                        home=attempt_home,
                        project_dir=project_dir,
                        journey=journey,
                        provider=provider,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                attempt_details.append(
                    {
                        "attempt": attempt,
                        "ok": False,
                        "errors": [f"timed out after {timeout_seconds:g}s"],
                    }
                )
                errors.append(f"attempt[{attempt}]: timed out after {timeout_seconds:g}s")
                continue
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                attempt_details.append({"attempt": attempt, "ok": False, "errors": [message]})
                errors.append(f"attempt[{attempt}]: {message}")
                continue
            attempt_errors = [f"attempt[{attempt}]: {error}" for error in result.errors]
            errors.extend(attempt_errors)
            attempt_details.append(
                {
                    "attempt": attempt,
                    "ok": result.ok,
                    "errors": result.errors,
                    "events": result.events,
                }
            )
        pass_count = sum(1 for item in attempt_details if item.get("ok") is True)
        results.append(
            ClawEvalResult(
                task_id=task_id,
                ok=pass_count == run_attempts,
                split=str(task["split"]),
                category=str(task["category"]),
                language=str(task["language"]),
                pass_count=pass_count,
                attempts=run_attempts,
                error_domains=_claw_error_domains(task, errors),
                errors=errors,
                attempts_detail=attempt_details,
            )
        )
    return results


def _claw_task_to_journey(task: dict[str, Any]) -> dict[str, Any]:
    journey = dict(task["journey"])
    journey.setdefault("id", task["task_id"])
    journey.setdefault("user_goal", task.get("query") or task["task_id"])
    return journey


def _claw_error_domains(task: dict[str, Any], errors: list[str]) -> list[str]:
    if not errors:
        return []
    domains: set[str] = set()
    dimensions = task.get("rubric_dimensions") or []
    if isinstance(dimensions, list):
        domains.update(str(item) for item in dimensions if str(item))
    if errors:
        domains.add("completion")
    if any("run_count_delta" in error or "watch_count_delta" in error for error in errors):
        domains.add("safety")
    if any(error.startswith("attempt[") for error in errors):
        domains.add("robustness")
    return sorted(domains)


async def _run_daily_journey_simulator(
    *,
    journey: dict[str, Any],
    provider: ModelPool,
    engine: TurnController,
    runs: RunStore,
) -> tuple[list[str], list[dict[str, Any]]]:
    simulator = journey["simulator"]
    persona = simulator.get("persona", "You are the user.")
    max_turns = int(simulator.get("max_turns", 10))
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    session_id = ""

    from navi.provider import ChatMessage

    messages = [
        ChatMessage(role="system", content=persona),
        ChatMessage(
            role="user",
            content="You are starting the conversation with Navi. State your initial request based on your persona. Provide your request in a natural, conversational way. Do not explain your persona to Navi.",
        ),
    ]

    for turn_idx in range(max_turns):
        user_message = await provider.complete_for("default", messages)
        user_message = user_message.strip()
        messages.append(ChatMessage(role="assistant", content=user_message))

        if user_message == "/exit" or user_message.lower() == "exit":
            break

        turn = await engine.handle(
            user_message,
            peer_id="daily-eval-sim",
            sender_id="daily-eval-sim",
            source="cli",
            session_id=session_id or None,
        )
        session_id = turn.session_id
        events.append(
            {
                "kind": "user",
                "message": user_message,
                "action": turn.action,
                "run_id": turn.run_id,
                "text": turn.text,
            }
        )

        if turn.action in {"delegation", "approval"}:
            messages.append(
                ChatMessage(
                    role="user",
                    content=f"Navi created a background task (action={turn.action}). Navi said: {turn.text}\nIf you consider the task complete or are satisfied, reply with /exit. Otherwise, continue.",
                )
            )
        else:
            messages.append(
                ChatMessage(
                    role="user",
                    content=f"Navi replied: {turn.text}\nPlease reply to Navi naturally. If your goal is fully accomplished, reply with /exit.",
                )
            )

    return errors, events


async def _run_daily_journey(
    *,
    home: Path,
    project_dir: Path,
    journey: dict[str, Any],
    provider: ModelPool | None = None,
) -> DailyJourneyResult:
    runtime = (
        AgentRuntime(home=home, provider=provider) if provider is not None else build_runtime(home)
    )
    ceiling = journey.get("permission_ceiling", "write")
    
    # Dynamically resolve source and disabled capability classes
    journey_id = str(journey.get("id") or "")
    if journey_id.startswith("public_"):
        source = "public_hermes"
        from .connector_runtime import REMOTE_BLOCKED_CAPABILITY_CLASSES, REMOTE_BLOCKED_TOOLS
        disabled_capability_classes = REMOTE_BLOCKED_CAPABILITY_CLASSES
        disabled_tools = REMOTE_BLOCKED_TOOLS
    else:
        source = "cli"
        disabled_capability_classes = frozenset()
        disabled_tools = frozenset()

    from .event_bus import EventBus
    from .governance_agent import GovernanceAgent
    event_bus = EventBus()
    GovernanceAgent(home, event_bus)

    engine = TurnController(
        home=home,
        runtime=runtime,
        project_dir=project_dir,
        permission_ceiling=ceiling,
        disabled_tools=set(disabled_tools),
        disabled_capability_classes=disabled_capability_classes,
        event_bus=event_bus,
    )
    runs = RunStore(home)
    goals = GoalStore(home)
    execution = ExecutionService(home)
    session_id = ""
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    latest_run_id = ""
    try:
        if "simulator" in journey:
            # Add a Provider check just in case
            if provider is None:
                errors.append("Simulator requires a ModelPool provider")
            else:
                sim_errors, sim_events = await _run_daily_journey_simulator(
                    journey=journey,
                    provider=provider,
                    engine=engine,
                    runs=runs,
                )
                errors.extend(sim_errors)
                events.extend(sim_events)
        else:
            for index, step in enumerate(journey["steps"]):
                before_runs = runs.list(limit=500)
                before_watches = runs.list_watches(limit=500)
                expect = step.get("expect") or {}
                if not isinstance(step, dict):
                    errors.append(f"step[{index}]: step must be a mapping")
                    continue
                if "user" in step:
                    message = _render_journey_text(
                        str(step["user"]), runs, latest_run_id=latest_run_id
                    )
                    turn = await engine.handle(
                        message,
                        peer_id="daily-eval",
                        sender_id="daily-eval",
                        source=source,
                        session_id=session_id or None,
                    )
                    session_id = turn.session_id
                    latest_run_id = turn.run_id or latest_run_id or _latest_run_id(runs)
                    event: dict[str, Any] = {
                        "kind": "user",
                        "message": message,
                        "action": turn.action,
                        "run_id": turn.run_id,
                        "text": turn.text,
                    }
                elif step.get("process_pending"):
                    processed = await execution.process_pending_once(limit=5)
                    if processed:
                        latest_run_id = processed[-1].id
                    event = {
                        "kind": "process_pending",
                        "processed": [item.__dict__ for item in processed],
                    }
                elif "seed_failed_run" in step:
                    seed = step.get("seed_failed_run") or {}
                    title = str(seed.get("title") or "failed daily eval task")
                    run = runs.create(
                        title,
                        prompt=str(seed.get("prompt") or title),
                        phase=Phase.ENDED,
                        resolution=Resolution.FAILED,
                        source=str(seed.get("source") or "watch"),
                        kind=str(seed.get("kind") or "delegation"),
                        peer_id="daily-eval",
                        sender_id="daily-eval",
                        workspace=str(project_dir),
                    )
                    latest_run_id = run.id
                    event = {"kind": "seed_failed_run", "run_id": run.id}
                else:
                    errors.append(
                        f"step[{index}]: missing user, process_pending, or seed_failed_run"
                    )
                    continue
                events.append(event)
                errors.extend(
                    _match_daily_expectation(
                        f"step[{index}]",
                        expect,
                        event=event,
                        runs=runs,
                        goals=goals,
                        latest_run_id=latest_run_id,
                        before_run_count=len(before_runs),
                        before_watch_count=len(before_watches),
                    )
                )
    finally:
        await engine.shutdown(timeout=1)
    return DailyJourneyResult(
        id=str(journey.get("id") or ""), ok=not errors, errors=errors, events=events
    )


def _render_journey_text(text: str, runs: RunStore, *, latest_run_id: str) -> str:
    if "{{approval_code}}" in text:
        approvals = runs.list_approvals(limit=20)
        code = approvals[0].code if approvals else ""
        text = text.replace("{{approval_code}}", code)
    if "{{run_id}}" in text:
        text = text.replace("{{run_id}}", latest_run_id)
    return text


def _match_daily_expectation(
    prefix: str,
    expect: dict[str, Any],
    *,
    event: dict[str, Any],
    runs: RunStore,
    goals: GoalStore,
    latest_run_id: str,
    before_run_count: int,
    before_watch_count: int,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(expect, dict):
        return [f"{prefix}: expect must be a mapping"]
    if "action" in expect and event.get("action") != expect["action"]:
        errors.append(
            f"{prefix}: action expected {expect['action']!r}, got {event.get('action')!r}"
        )
    if "text_contains" in expect and str(expect["text_contains"]) not in str(
        event.get("text") or ""
    ):
        errors.append(f"{prefix}: text did not contain {expect['text_contains']!r}")
    if "text_contains_any" in expect:
        expected_any = [str(item) for item in expect["text_contains_any"]]
        text = str(event.get("text") or "")
        if not any(item in text for item in expected_any):
            errors.append(f"{prefix}: text did not contain any of {expected_any!r}")
    if "text_not_contains_any" in expect:
        forbidden = [str(item) for item in expect["text_not_contains_any"]]
        text = str(event.get("text") or "")
        found = [item for item in forbidden if item in text]
        if found:
            errors.append(f"{prefix}: text contained forbidden items {found!r}")
    if "run_count_delta" in expect:
        delta = len(runs.list(limit=500)) - before_run_count
        if delta != int(expect["run_count_delta"]):
            errors.append(
                f"{prefix}: run_count_delta expected {expect['run_count_delta']!r}, got {delta!r}"
            )
    if "run_count" in expect:
        count = len(runs.list(limit=500))
        if count != int(expect["run_count"]):
            errors.append(f"{prefix}: run_count expected {expect['run_count']!r}, got {count!r}")
    if "failed_run_count" in expect:
        count = len(
            [
                run
                for run in runs.list_by_phase(Phase.ENDED, limit=500)
                if run.resolution == Resolution.FAILED
            ]
        )
        if count != int(expect["failed_run_count"]):
            errors.append(
                f"{prefix}: failed_run_count expected {expect['failed_run_count']!r}, got {count!r}"
            )
    if "watch_count_delta" in expect:
        delta = len(runs.list_watches(limit=500)) - before_watch_count
        if delta != int(expect["watch_count_delta"]):
            errors.append(
                f"{prefix}: watch_count_delta expected {expect['watch_count_delta']!r}, got {delta!r}"
            )
    if "watch_count" in expect:
        count = len(runs.list_watches(limit=500))
        if count != int(expect["watch_count"]):
            errors.append(
                f"{prefix}: watch_count expected {expect['watch_count']!r}, got {count!r}"
            )
    if "watch_kind" in expect:
        watches = runs.list_watches(limit=1)
        actual = watches[0].kind if watches else ""
        if actual != str(expect["watch_kind"]):
            errors.append(f"{prefix}: watch_kind expected {expect['watch_kind']!r}, got {actual!r}")
    if "watch_cron" in expect:
        watches = runs.list_watches(limit=1)
        actual = watches[0].cron if watches else ""
        if actual != str(expect["watch_cron"]):
            errors.append(f"{prefix}: watch_cron expected {expect['watch_cron']!r}, got {actual!r}")
    if "run_phase" in expect:
        run = runs.get(latest_run_id)
        actual = run.phase if run else ""
        if actual != expect["run_phase"]:
            errors.append(f"{prefix}: run_phase expected {expect['run_phase']!r}, got {actual!r}")
    if "run_resolution" in expect:
        run = runs.get(latest_run_id)
        actual = run.resolution if run else ""
        if actual != expect["run_resolution"]:
            errors.append(
                f"{prefix}: run_resolution expected {expect['run_resolution']!r}, got {actual!r}"
            )
    if "goal_phase" in expect:
        goal = goals.get_by_run(latest_run_id)
        actual = goal.phase if goal else ""
        if actual != expect["goal_phase"]:
            errors.append(
                f"{prefix}: goal_phase expected {expect['goal_phase']!r}, got {actual!r}"
            )
    return errors


def _latest_run_id(runs: RunStore) -> str:
    listed = runs.list(limit=1)
    return listed[0].id if listed else ""


def _safe_path_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return safe or "journey"


def _eval_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]


def validate_delegation_eval_cases(cases: list[dict[str, Any]], tools: list[ToolSpec]) -> list[str]:
    return validate_delegation_eval_dataset({"cases": cases}, tools)


def validate_delegation_eval_dataset(dataset: dict[str, Any], tools: list[ToolSpec]) -> list[str]:
    errors: list[str] = []
    by_name = {tool.name: tool for tool in tools}
    seen: set[str] = set()
    cases = dataset.get("cases")
    if not isinstance(cases, list):
        return ["dataset: missing cases list"]
    required_categories = _required_categories(dataset)
    required_tools = _required_tools(dataset)
    unknown_required_tools = required_tools - set(by_name)
    for tool_name in sorted(unknown_required_tools):
        errors.append(f"dataset: unknown required tool {tool_name!r}")
    categories_seen: set[str] = set()
    tools_seen: set[str] = set()
    for index, case in enumerate(cases):
        case_id = str(case.get("id") or "")
        prefix = case_id or f"case[{index}]"
        if not case_id:
            errors.append(f"{prefix}: missing id")
        if case_id in seen:
            errors.append(f"{prefix}: duplicate id")
        seen.add(case_id)
        if not str(case.get("message") or "").strip():
            errors.append(f"{prefix}: missing message")
        category = str(case.get("category") or "").strip()
        if required_categories:
            if not category:
                errors.append(f"{prefix}: missing category")
            elif category not in required_categories:
                errors.append(f"{prefix}: unknown category {category!r}")
            else:
                categories_seen.add(category)
        expected = case.get("expect")
        if not isinstance(expected, dict):
            errors.append(f"{prefix}: missing expect mapping")
            continue
        tool_name = str(expected.get("tool") or "")
        tool = by_name.get(tool_name)
        if tool is None:
            errors.append(f"{prefix}: unknown expected tool {tool_name!r}")
            continue
        for allowed_tool in expected.get("allowed_tools") or []:
            allowed_name = str(allowed_tool or "")
            if allowed_name not in by_name:
                errors.append(f"{prefix}: unknown allowed tool {allowed_name!r}")
        for option in expected.get("allowed_decisions") or []:
            if not isinstance(option, dict):
                errors.append(f"{prefix}: allowed_decisions entries must be mappings")
                continue
            allowed_name = str(option.get("tool") or "")
            allowed = by_name.get(allowed_name)
            if allowed is None:
                errors.append(f"{prefix}: unknown allowed decision tool {allowed_name!r}")
                continue
            allowed_permission = str(option.get("permission") or "")
            if allowed_permission and allowed_permission != allowed.permission:
                errors.append(
                    f"{prefix}: allowed decision {allowed_name!r} permission {allowed_permission!r} does not match {allowed.permission!r}"
                )
        if tool_name in required_tools:
            tools_seen.add(tool_name)
        permission = str(expected.get("permission") or "")
        if permission != tool.permission:
            errors.append(
                f"{prefix}: expected permission {permission!r} does not match {tool.permission!r}"
            )
        _validate_expected_args(prefix, expected.get("args") or {}, tool, errors)
        _validate_expected_args(prefix, expected.get("args_contains") or {}, tool, errors)
    for category in sorted(required_categories - categories_seen):
        errors.append(f"dataset: missing required category {category!r}")
    for tool_name in sorted(required_tools - tools_seen - unknown_required_tools):
        errors.append(f"dataset: missing required tool {tool_name!r}")
    return errors


async def run_delegation_eval_dataset(
    *,
    home: Path,
    project_dir: Path,
    dataset: Path,
    timeout_seconds: float = 75.0,
    provider: ModelPool | None = None,
) -> list[EvalResult]:
    loaded = load_delegation_eval_dataset(dataset)
    cases = loaded["cases"]
    tools = delegation_eval_tools(home, project_dir=project_dir)
    validation_errors = validate_delegation_eval_dataset(loaded, tools)
    if validation_errors:
        return [
            EvalResult(
                id="dataset",
                ok=False,
                expected={},
                actual={},
                errors=validation_errors,
            )
        ]
    runtime_provider = provider or build_runtime(home).provider
    planner = ModelSyscallPlanner(runtime_provider)
    results: list[EvalResult] = []
    for case in cases:
        try:
            decision = await asyncio.wait_for(
                planner.plan(
                    str(case["message"]),
                    tools=tools,
                    conversation_context=_case_conversation_context(case),
                    permission_ceiling=_case_permission_ceiling(case),
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            results.append(
                EvalResult(
                    id=str(case["id"]),
                    ok=False,
                    expected=dict(case["expect"]),
                    actual={},
                    errors=[f"planner timed out after {timeout_seconds:g}s"],
                )
            )
            continue
        except Exception as exc:
            results.append(
                EvalResult(
                    id=str(case["id"]),
                    ok=False,
                    expected=dict(case["expect"]),
                    actual={},
                    errors=[f"planner error: {type(exc).__name__}: {exc}"],
                )
            )
            continue
        errors = match_delegation_eval_case(case, decision)
        results.append(
            EvalResult(
                id=str(case["id"]),
                ok=not errors,
                expected=dict(case["expect"]),
                actual=asdict(decision),
                errors=errors,
            )
        )
    return results


def delegation_eval_tools(home: Path, *, project_dir: Path) -> list[ToolSpec]:
    return build_capability_registry(home, project_dir=project_dir).list_specs()


def match_delegation_eval_case(case: dict[str, Any], decision: ModelSyscall) -> list[str]:
    expected = case.get("expect") or {}
    errors: list[str] = []
    expected_tool = str(expected.get("tool") or "")
    expected_permission = str(expected.get("permission") or "")
    candidates: list[dict[str, Any]] = [
        {
            "tool": expected_tool,
            "permission": expected_permission,
            "args": expected.get("args") or {},
            "args_contains": expected.get("args_contains") or {},
        }
    ]
    for option in expected.get("allowed_decisions") or []:
        if not isinstance(option, dict):
            continue
        candidates.append(
            {
                "tool": str(option.get("tool") or ""),
                "permission": str(option.get("permission") or ""),
                "args": option.get("args") or {},
                "args_contains": option.get("args_contains") or {},
            }
        )
    allowed_tools = {
        str(item)
        for item in (expected.get("allowed_tools") or [])
        if isinstance(item, str) and item.strip()
    }
    candidates.extend(
        {"tool": tool, "permission": expected_permission, "args": {}, "args_contains": {}}
        for tool in sorted(allowed_tools)
    )
    matched = next(
        (
            candidate
            for candidate in candidates
            if decision.tool == candidate["tool"]
            and (
                not candidate["permission"]
                or decision.permission == candidate["permission"]
            )
        ),
        None,
    )
    if matched is None:
        expected_summary = [
            f"{candidate['tool']}:{candidate['permission'] or '*'}"
            for candidate in candidates
            if candidate["tool"]
        ]
        errors.append(
            f"decision expected one of {expected_summary!r}, got {decision.tool!r}:{decision.permission!r}"
        )
        return errors
    for key, value in (matched.get("args") or {}).items():
        actual = decision.args.get(str(key))
        if str(actual).lower() != str(value).lower():
            errors.append(f"args.{key} expected {value!r}, got {actual!r}")
    for key, value in (matched.get("args_contains") or {}).items():
        actual = str(decision.args.get(str(key)) or "").lower()
        expected_part = str(value).lower()
        if expected_part not in actual:
            errors.append(f"args.{key} expected to contain {value!r}, got {actual!r}")
    return errors


def _case_conversation_context(case: dict[str, Any]) -> str:
    parts = []
    context = str(case.get("conversation_context") or "").strip()
    scenario = str(case.get("scenario") or "").strip()
    if context:
        parts.append(context)
    if scenario:
        parts.append(f"Scenario facts:\n{scenario}")
    return "\n\n".join(parts)


def _case_permission_ceiling(case: dict[str, Any]) -> str:
    explicit = str(case.get("permission_ceiling") or "").strip()
    if explicit:
        return explicit
    scenario = str(case.get("scenario") or "")
    match = re.search(r"permission_ceiling:\s*(read|prepare|write)\b", scenario)
    return match.group(1) if match else "write"


def results_to_json(results: list[EvalResult]) -> str:
    return json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2)


def claw_results_to_json(results: list[ClawEvalResult]) -> str:
    return json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2)


def _validate_expected_args(
    prefix: str,
    expected_args: dict[str, Any],
    tool: ToolSpec,
    errors: list[str],
) -> None:
    if not isinstance(expected_args, dict):
        errors.append(f"{prefix}: expect.args must be a mapping")
        return
    properties = tool.input_schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    for key in expected_args:
        if str(key) not in properties:
            errors.append(f"{prefix}: args.{key} is not declared by {tool.name}")


def _required_categories(dataset: dict[str, Any]) -> set[str]:
    coverage = dataset.get("coverage") or {}
    if not isinstance(coverage, dict):
        return set()
    raw = coverage.get("required_categories") or []
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def _required_tools(dataset: dict[str, Any]) -> set[str]:
    coverage = dataset.get("coverage") or {}
    if not isinstance(coverage, dict):
        return set()
    raw = coverage.get("required_tools") or []
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}
