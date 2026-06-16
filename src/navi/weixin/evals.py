from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from navi.provider import ChatMessage, ModelPool
from navi.runtime import AgentRuntime
from navi.runs import RunStore

from .client import FakeWeixinClient
from .config import WeixinConfig
from .models import WeixinAccount, WeixinUpdate
from .service import WeixinService


@dataclass(frozen=True)
class WeixinJourneyResult:
    id: str
    ok: bool
    errors: list[str]
    events: list[dict[str, Any]]


class _FailingEvalProvider():
    async def complete(self, messages: list[ChatMessage]) -> str:
        raise RuntimeError("eval provider failure")


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
    for index, journey in enumerate(journeys):
        if not isinstance(journey, dict):
            raise ValueError(f"journey {index} must be a mapping")
        steps = journey.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"journey {journey.get('id') or index} must contain non-empty steps")
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
    runtime = AgentRuntime(home=home, provider=model_provider or ModelPool(default=()))
    service = WeixinService(
        home=home, config=WeixinConfig(dm_policy="open"), runtime=runtime, project_dir=project_dir
    )
    service.client = FakeWeixinClient()
    account = WeixinAccount(account_id="eval-account", token="eval-token", base_url="fake://ilink")
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
        before_watches = len(runs.list_watches(limit=500))
        expect = step.get("expect") or {}
        if "seed_failed_run" in step:
            seed = step.get("seed_failed_run") or {}
            run = runs.create(
                str(seed.get("title") or "failed connector eval task"),
                prompt=str(seed.get("prompt") or seed.get("title") or "failed connector eval task"),
                status="failed",
                source=str(seed.get("source") or "watch"),
                kind=str(seed.get("kind") or "delegation"),
                peer_id="connector-eval-peer",
                sender_id="connector-eval-sender",
                workspace=str(project_dir),
            )
            event = {"kind": "seed_failed_run", "run_id": run.id}
        elif "inbound" in step:
            inbound = step.get("inbound") or {}
            message_index += 1
            update = WeixinUpdate(
                message_id=str(inbound.get("message_id") or f"msg-{message_index}"),
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
            errors.append(f"step[{index}]: missing inbound or seed_failed_run")
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
                before_watch_count=before_watches,
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
    before_watch_count: int,
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
    if "watch_count_delta" in expect:
        delta = len(runs.list_watches(limit=500)) - before_watch_count
        if delta != int(expect["watch_count_delta"]):
            errors.append(
                f"{prefix}: watch_count_delta expected {expect['watch_count_delta']!r}, got {delta!r}"
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
    if "failed_run_count" in expect:
        count = runs.count_runs(status="failed")
        if count != int(expect["failed_run_count"]):
            errors.append(
                f"{prefix}: failed_run_count expected {expect['failed_run_count']!r}, got {count!r}"
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
