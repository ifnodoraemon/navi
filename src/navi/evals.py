from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .app_factory import build_runtime
from .connector_registry import get_connector_adapter
from .control_plane import TurnController
from .goals import GoalStore
from .provider import ModelPool
from .runtime import AgentRuntime
from .runs import RunStore


_CURRENT_EVAL_ACTIONS = {"approval", "ask", "chat", "connector_outbound", "goal", "tool"}
_OBSOLETE_EXPECTATION_KEYS = {
    "watch_count",
    "watch_count_delta",
    "watch_cron",
    "watch_kind",
}


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


def load_daily_journey_eval_dataset(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = {} if loaded is None else loaded
    if not isinstance(data, dict):
        raise ValueError("daily journey eval dataset must be a mapping")
    journeys = data.get("journeys")
    if not isinstance(journeys, list):
        raise ValueError("daily journey eval dataset must contain a journeys list")
    seen: set[str] = set()
    for index, journey in enumerate(journeys):
        if not isinstance(journey, dict):
            raise ValueError(f"journey {index} must be a mapping")
        journey_id = str(journey.get("id") or "").strip()
        if not journey_id:
            raise ValueError(f"journey {index} is missing id")
        if journey_id in seen:
            raise ValueError(f"journey {journey_id}: duplicate id")
        seen.add(journey_id)
        if "simulator" not in journey:
            steps = journey.get("steps")
            if not isinstance(steps, list) or not steps:
                raise ValueError(
                    f"journey {journey.get('id') or index} must contain non-empty steps or a simulator"
                )
            _validate_eval_steps(steps, prefix=f"journey {journey_id}")
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
        _validate_eval_steps(steps, prefix=prefix)
    return data


def _validate_eval_steps(steps: list[Any], *, prefix: str) -> None:
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"{prefix} step[{index}] must be a mapping")
        expect = step.get("expect")
        if expect is None:
            continue
        if not isinstance(expect, dict):
            raise ValueError(f"{prefix} step[{index}].expect must be a mapping")
        obsolete = sorted(_OBSOLETE_EXPECTATION_KEYS.intersection(expect))
        if obsolete:
            raise ValueError(
                f"{prefix} step[{index}] uses obsolete expectation keys: {obsolete}"
            )
        action = str(expect.get("action") or "").strip()
        if action and action not in _CURRENT_EVAL_ACTIONS:
            raise ValueError(f"{prefix} step[{index}] uses unsupported action: {action}")
        if expect.get("cron_schedule") == "once":
            raise ValueError(
                f"{prefix} step[{index}] uses removed one-shot watch sentinel"
            )


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
    if any(
        "run_count_delta" in error or "scheduled_goal_count_delta" in error
        for error in errors
    ):
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

        if turn.action in {"goal", "approval"}:
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
    
    journey_id = str(journey.get("id") or "")
    source = "public_hermes" if journey_id.startswith("public_") else "cli"

    from .event_bus import EventBus
    event_bus = EventBus()

    engine = TurnController(
        home=home,
        runtime=runtime,
        project_dir=project_dir,
        permission_ceiling=ceiling,
        event_bus=event_bus,
    )
    runs = RunStore(home)
    goals = GoalStore(home)
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
                before_scheduled_goals = goals.list_cron_goals()
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
                    processed = await _run_process_pending(engine, limit=5)
                    if processed:
                        latest_run_id = processed[-1].id
                    event = {
                        "kind": "process_pending",
                        "processed": [item.__dict__ for item in processed],
                    }
                else:
                    errors.append(f"step[{index}]: missing user or process_pending")
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
                        before_scheduled_goal_count=len(before_scheduled_goals),
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


async def _run_process_pending(engine: TurnController, *, limit: int = 5) -> list[Any]:
    from .daemon import SystemDaemon

    processed = await SystemDaemon(
        engine.home,
        project_dir=engine.project_dir,
    ).process_queue_once()
    return processed[:limit]


def _match_daily_expectation(
    prefix: str,
    expect: dict[str, Any],
    *,
    event: dict[str, Any],
    runs: RunStore,
    goals: GoalStore,
    latest_run_id: str,
    before_run_count: int,
    before_scheduled_goal_count: int,
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
    if "scheduled_goal_count_delta" in expect:
        delta = len(goals.list_cron_goals()) - before_scheduled_goal_count
        if delta != int(expect["scheduled_goal_count_delta"]):
            errors.append(
                f"{prefix}: scheduled_goal_count_delta expected "
                f"{expect['scheduled_goal_count_delta']!r}, got {delta!r}"
            )
    if "scheduled_goal_count" in expect:
        count = len(goals.list_cron_goals())
        if count != int(expect["scheduled_goal_count"]):
            errors.append(
                f"{prefix}: scheduled_goal_count expected "
                f"{expect['scheduled_goal_count']!r}, got {count!r}"
            )
    if "scheduled_goal_status" in expect:
        scheduled_goals = goals.list_cron_goals()
        actual = scheduled_goals[0].task_status if scheduled_goals else ""
        if actual != str(expect["scheduled_goal_status"]):
            errors.append(
                f"{prefix}: scheduled_goal_status expected "
                f"{expect['scheduled_goal_status']!r}, got {actual!r}"
            )
    if "cron_schedule" in expect:
        scheduled_goals = goals.list_cron_goals()
        actual = scheduled_goals[0].cron_schedule if scheduled_goals else ""
        if actual != str(expect["cron_schedule"]):
            errors.append(
                f"{prefix}: cron_schedule expected {expect['cron_schedule']!r}, got {actual!r}"
            )
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


def claw_results_to_json(results: list[ClawEvalResult]) -> str:
    return json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2)
