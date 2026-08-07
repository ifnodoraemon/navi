from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator

import yaml

from navi.provider import ChatMessage, ModelPool, ProviderUsage
from navi.goals import GoalStore
from navi.runtime import AgentRuntime
from navi.runs import RunStore

from .client import split_text_for_weixin
from .config import WeixinConfig
from .models import WeixinAccount, WeixinUpdate
from .service import WeixinService


class _CaptureWeixinClient:
    """Eval-only test double that captures outbound weixin traffic.

    Lives in the eval module (a test boundary), not in the runtime client, so
    the production transport has no simulation mode. It records sent chunks so
    journey assertions can inspect what would have been delivered, without
    contacting a real weixin account.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.typing: list[dict[str, str | int]] = []

    async def send_message(
        self, *, account_id: str, peer_id: str, text: str, context_token: str = ""
    ) -> None:
        for chunk in split_text_for_weixin(text):
            self.sent.append(
                {
                    "account_id": account_id,
                    "peer_id": peer_id,
                    "text": chunk,
                    "context_token": context_token,
                }
            )

    async def get_typing_ticket(self, *, user_id: str, context_token: str = "") -> str:
        return ""

    async def send_typing(self, *, peer_id: str, typing_ticket: str, status: int) -> None:
        self.typing.append({"peer_id": peer_id, "typing_ticket": typing_ticket, "status": status})


@dataclass(frozen=True)
class WeixinJourneyResult:
    id: str
    ok: bool
    errors: list[str]
    events: list[dict[str, Any]]


class _FailingEvalProvider:
    last_usage: ProviderUsage | None = None

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        output_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        raise RuntimeError("eval provider failure")

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        result = await self.complete(messages, temperature=temperature, max_tokens=max_tokens)
        yield result


def load_journey_eval_dataset(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = {} if loaded is None else loaded
    if not isinstance(data, dict):
        raise ValueError("connector journey eval dataset must be a mapping")
    if str(data.get("connector") or "").strip() != "weixin":
        raise ValueError("connector journey eval dataset targets a different connector")
    journeys = data.get("journeys")
    if not isinstance(journeys, list):
        raise ValueError("connector journey eval dataset must contain a journeys list")
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
        steps = journey.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"journey {journey.get('id') or index} must contain non-empty steps")
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"journey {journey_id} step[{step_index}] must be a mapping")
            expect = step.get("expect")
            if expect is not None and not isinstance(expect, dict):
                raise ValueError(
                    f"journey {journey_id} step[{step_index}].expect must be a mapping"
                )
            if not isinstance(expect, dict):
                continue
            obsolete = sorted(
                {"watch_count", "watch_count_delta", "watch_cron", "watch_kind"}.intersection(
                    expect
                )
            )
            if obsolete:
                raise ValueError(
                    f"journey {journey_id} step[{step_index}] uses obsolete "
                    f"expectation keys: {obsolete}"
                )
            if expect.get("cron_schedule") == "once":
                raise ValueError(
                    f"journey {journey_id} step[{step_index}] uses removed "
                    "one-shot watch sentinel"
                )
    return data


async def run_journey_eval_dataset(
    home: Path,
    project_dir: Path,
    dataset: Path,
    timeout_seconds: float = 30.0,
    provider: ModelPool | None = None,
) -> list[WeixinJourneyResult]:
    loaded = load_journey_eval_dataset(dataset)
    results: list[WeixinJourneyResult] = []
    run_root = home / "connector_journeys" / _eval_run_id()
    for journey in loaded["journeys"]:
        journey_home = run_root / _safe_path_name(str(journey.get("id") or "journey"))
        result = await asyncio.wait_for(
            _run_journey(
                home=journey_home, project_dir=project_dir, journey=journey, provider=provider
            ),
            timeout=timeout_seconds,
        )
        results.append(result)
    return results


async def _run_journey(
    *,
    home: Path,
    project_dir: Path,
    journey: dict[str, Any],
    provider: ModelPool | None = None,
) -> WeixinJourneyResult:
    model_provider = (
        ModelPool(default=_FailingEvalProvider())
        if journey.get("provider") == "failing"
        else provider
    )
    runtime = AgentRuntime(
        home=home,
        provider=model_provider or ModelPool(default=_FailingEvalProvider()),
    )
    service = WeixinService(
        home=home,
        config=WeixinConfig(dm_policy="open"),
        runtime=runtime,
        project_dir=project_dir,
        client=_CaptureWeixinClient(),
    )
    account = WeixinAccount(account_id="eval-account", token="eval-token", base_url="ilink")
    runs = RunStore(home)
    errors: list[str] = []
    events: list[dict[str, Any]] = []
    message_index = 0
    for index, step in enumerate(journey["steps"]):
        if not isinstance(step, dict):
            errors.append(f"step[{index}]: step must be a mapping")
            continue
        before_sent = len(getattr(service.client, "sent", []))
        before_runs = len(runs.list(limit=500))
        before_scheduled_goals = len(GoalStore(home).list_cron_goals())
        expect = step.get("expect") or {}
        if "inbound" in step:
            inbound = step.get("inbound") or {}
            message_index += 1
            update = WeixinUpdate(
                message_id=str(
                    inbound.get("message_id")
                    or f"synthetic:msg-{message_index}"
                ),
                peer_id=str(inbound.get("peer_id") or "connector-eval-peer"),
                sender_id=str(inbound.get("sender_id") or "connector-eval-sender"),
                text=str(inbound.get("text") or ""),
                context_token=str(inbound.get("context_token") or "eval-context"),
                is_group=bool(inbound.get("is_group", False)),
            )
            handled = await service.handle_update(account, update)
            event = {
                "kind": "inbound",
                "handled": handled,
                "text": update.text,
                "sent": list(getattr(service.client, "sent", [])),
            }
        else:
            errors.append(f"step[{index}]: missing inbound")
            continue
        events.append(event)
        errors.extend(
            _match_expectation(
                f"step[{index}]",
                expect,
                event=event,
                home=home,
                service=service,
                runs=runs,
                before_sent_count=before_sent,
                before_run_count=before_runs,
                before_scheduled_goal_count=before_scheduled_goals,
            )
        )
    return WeixinJourneyResult(
        id=str(journey.get("id") or ""), ok=not errors, errors=errors, events=events
    )


def _match_expectation(
    prefix: str,
    expect: dict[str, Any],
    *,
    event: dict[str, Any],
    home: Path,
    service: WeixinService,
    runs: RunStore,
    before_sent_count: int,
    before_run_count: int,
    before_scheduled_goal_count: int,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(expect, dict):
        return [f"{prefix}: expect must be a mapping"]
    sent = list(getattr(service.client, "sent", []))
    if "handled" in expect and bool(event.get("handled")) is not bool(expect["handled"]):
        errors.append(
            f"{prefix}: handled expected {expect['handled']!r}, got {event.get('handled')!r}"
        )
    if "sent_count_delta" in expect:
        delta = len(sent) - before_sent_count
        if delta != int(expect["sent_count_delta"]):
            errors.append(
                f"{prefix}: sent_count_delta expected {expect['sent_count_delta']!r}, got {delta!r}"
            )
    if "sent_contains" in expect:
        text = sent[-1]["text"] if sent else ""
        if str(expect["sent_contains"]) not in text:
            errors.append(f"{prefix}: sent text did not contain {expect['sent_contains']!r}")
    if "sent_contains_any" in expect:
        text = sent[-1]["text"] if sent else ""
        expected_any = [str(item) for item in expect["sent_contains_any"]]
        if not any(item in text for item in expected_any):
            errors.append(f"{prefix}: sent text did not contain any of {expected_any!r}")
    if "sent_not_contains_any" in expect:
        text = sent[-1]["text"] if sent else ""
        forbidden = [str(item) for item in expect["sent_not_contains_any"]]
        found = [item for item in forbidden if item in text]
        if found:
            errors.append(f"{prefix}: sent text contained forbidden items {found!r}")
    if "run_count_delta" in expect:
        delta = len(runs.list(limit=500)) - before_run_count
        if delta != int(expect["run_count_delta"]):
            errors.append(
                f"{prefix}: run_count_delta expected {expect['run_count_delta']!r}, got {delta!r}"
            )
    if "scheduled_goal_count_delta" in expect:
        delta = len(GoalStore(home).list_cron_goals()) - before_scheduled_goal_count
        if delta != int(expect["scheduled_goal_count_delta"]):
            errors.append(
                f"{prefix}: scheduled_goal_count_delta expected "
                f"{expect['scheduled_goal_count_delta']!r}, got {delta!r}"
            )
    if "scheduled_goal_status" in expect:
        scheduled_goals = GoalStore(home).list_cron_goals()
        actual = scheduled_goals[0].task_status if scheduled_goals else ""
        if actual != str(expect["scheduled_goal_status"]):
            errors.append(
                f"{prefix}: scheduled_goal_status expected "
                f"{expect['scheduled_goal_status']!r}, got {actual!r}"
            )
    if "cron_schedule" in expect:
        scheduled_goals = GoalStore(home).list_cron_goals()
        actual = scheduled_goals[0].cron_schedule if scheduled_goals else ""
        if actual != str(expect["cron_schedule"]):
            errors.append(
                f"{prefix}: cron_schedule expected {expect['cron_schedule']!r}, got {actual!r}"
            )
    if "event_contains" in expect:
        event_names = _event_names(home)
        expected_events = [str(item) for item in expect["event_contains"]]
        missing = [name for name in expected_events if name not in event_names]
        if missing:
            errors.append(f"{prefix}: missing connector events {missing!r}")
    return errors


def _event_names(home: Path) -> list[str]:
    path = home / "weixin" / "events.jsonl"
    if not path.exists():
        return []
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            names.append(str(event.get("event") or ""))
    return names


def _safe_path_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return safe or "journey"


def _eval_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
