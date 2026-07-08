"""Daily journey eval dataset."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..lifecycle import Phase, Resolution
from ._utils import _eval_run_id, _safe_path_name


@dataclass(frozen=True)
class DailyJourneyResult:
    id: str
    ok: bool
    errors: list[str]
    events: list[dict[str, Any]]


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


async def run_daily_journey_eval_dataset(
    *,
    home: Path,
    project_dir: Path,
    dataset: Path,
    timeout_seconds: float = 30.0,
    provider: Any | None = None,
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


async def _run_daily_journey(
    *,
    home: Path,
    project_dir: Path,
    journey: dict[str, Any],
    provider: Any | None = None,
) -> DailyJourneyResult:
    from ..app_factory import build_runtime
    from ..control_plane import TurnController
    from ..runtime import AgentRuntime
    from ..runs import RunStore

    runtime = AgentRuntime(home=home, provider=provider) if provider is not None else build_runtime(home)
    ceiling = journey.get("permission_ceiling", "write")

    # Dynamically resolve source and disabled capability classes
    journey_id = str(journey.get("id") or "")
    if journey_id.startswith("public_"):
        source = "public_hermes"
        from ..connector_runtime import REMOTE_BLOCKED_CAPABILITY_CLASSES, REMOTE_BLOCKED_TOOLS
        disabled_capability_classes = REMOTE_BLOCKED_CAPABILITY_CLASSES
        disabled_tools = REMOTE_BLOCKED_TOOLS
    else:
        source = "cli"
        disabled_capability_classes = frozenset()
        disabled_tools = frozenset()

    from ..event_bus import EventBus
    from ..governance_agent import GovernanceAgent
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
                    processed = await _run_process_pending(engine, limit=5)
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


async def _run_daily_journey_simulator(
    *,
    journey: dict[str, Any],
    provider: Any,
    engine: Any,
    runs: Any,
) -> tuple[list[str], list[dict[str, Any]]]:
    from ..provider import ChatMessage

    simulator = journey["simulator"]
    persona = simulator.get("persona", "You are the user.")
    max_turns = int(simulator.get("max_turns", 10))
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    session_id = ""

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


async def _run_process_pending(engine: Any, *, limit: int = 5) -> list[Any]:
    """Run process_pending_once via the engine's execution service."""
    # This delegates to the engine's execution service if available
    if hasattr(engine, "execution") and engine.execution is not None:
        return await engine.execution.process_pending_once(limit=limit)
    return []


def _render_journey_text(text: str, runs: Any, *, latest_run_id: str) -> str:
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
    runs: Any,
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
            errors.append(
                f"{prefix}: run_count expected {expect['run_count']!r}, got {count!r}"
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
            errors.append(
                f"{prefix}: watch_kind expected {expect['watch_kind']!r}, got {actual!r}"
            )
    if "watch_cron" in expect:
        watches = runs.list_watches(limit=1)
        actual = watches[0].cron if watches else ""
        if actual != str(expect["watch_cron"]):
            errors.append(
                f"{prefix}: watch_cron expected {expect['watch_cron']!r}, got {actual!r}"
            )
    if "run_phase" in expect:
        run = runs.get(latest_run_id)
        actual = run.phase if run else ""
        if actual != expect["run_phase"]:
            errors.append(
                f"{prefix}: run_phase expected {expect['run_phase']!r}, got {actual!r}"
            )
    return errors


def _latest_run_id(runs: Any) -> str:
    listed = runs.list(limit=1)
    return listed[0].id if listed else ""
