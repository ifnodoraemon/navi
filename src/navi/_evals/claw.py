"""Claw-Eval style Pass^3 user task evals."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from ._utils import _eval_run_id, _safe_path_name


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


async def run_claw_eval_dataset(
    *,
    home: Path,
    project_dir: Path,
    dataset: Path,
    attempts: int = 3,
    timeout_seconds: float = 30.0,
    provider: Any | None = None,
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
                    _run_claw_journey(
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


async def _run_claw_journey(
    *,
    home: Path,
    project_dir: Path,
    journey: dict[str, Any],
    provider: Any | None = None,
) -> Any:
    """Run a claw journey by delegating to the daily journey runner."""
    from .daily_journey import _run_daily_journey

    return await _run_daily_journey(
        home=home, project_dir=project_dir, journey=journey, provider=provider
    )
