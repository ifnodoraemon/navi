from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from navi.engine import HernessEngine
from navi.action_tools import load_action_tool_specs
from navi.app_factory import build_runtime
from navi.config import load_config
from navi.syscalls import ModelSyscall, ModelSyscallPlanner
from navi.tools import build_tool_gateway


pytestmark = pytest.mark.live_llm


def _live_enabled() -> bool:
    return os.environ.get("NAVI_LIVE_LLM_TESTS", "").lower() in {"1", "true", "yes", "on"}


def _home() -> Path:
    return Path(os.environ.get("NAVI_LIVE_HOME", ".navi"))


def _cases() -> dict[str, Any]:
    path = Path(__file__).with_name("live_llm_cases.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise AssertionError("live_llm_cases.yaml must contain a mapping")
    return data


def _require_live_provider() -> Path:
    if not _live_enabled():
        pytest.skip("set NAVI_LIVE_LLM_TESTS=1 to run real model provider tests")
    home = _home()
    config = load_config(home)
    if config.model.provider == "mock":
        pytest.skip("live LLM tests require a non-mock provider")
    if not config.model.api_key:
        pytest.skip(f"live LLM tests require an API key for provider {config.model.provider}")
    return home


async def _select(home: Path, message: str) -> ModelSyscall:
    runtime = build_runtime(home)
    planner = ModelSyscallPlanner(runtime.provider)
    tools = [*load_action_tool_specs(), *build_tool_gateway(home, project_dir=Path.cwd()).list_specs()]
    return await asyncio.wait_for(planner.plan(message, tools=tools), timeout=75)


@pytest.mark.parametrize("case", _cases().get("syscall_cases", []), ids=lambda item: item["id"])
async def test_live_llm_routes_real_user_issue(case: dict[str, Any]) -> None:
    home = _require_live_provider()

    decision = await _select(home, str(case["message"]))

    expected = case["expect"]
    assert decision.tool == expected["tool"], _format_failure(case, decision)
    assert decision.permission == expected["permission"], _format_failure(case, decision)
    for key, value in (expected.get("args") or {}).items():
        assert str(decision.args.get(key, "")).lower() == str(value).lower(), _format_failure(case, decision)


@pytest.mark.parametrize("case", _cases().get("response_cases", []), ids=lambda item: item["id"])
async def test_live_llm_responses_do_not_regress_to_command_instructions(case: dict[str, Any]) -> None:
    home = _require_live_provider()
    runtime = build_runtime(home)
    router = HernessEngine(home=home, runtime=runtime, project_dir=Path.cwd())

    result = await asyncio.wait_for(
        router.handle(
            str(case["message"]),
            peer_id="live-eval",
            sender_id="live-eval",
            source="live-eval",
        ),
        timeout=120,
    )

    for banned in case.get("banned") or []:
        assert banned not in result.text, f"{case['id']} leaked {banned!r} in response:\n{result.text}"


def _format_failure(case: dict[str, Any], decision: ModelSyscall) -> str:
    return (
        f"case={case['id']} source={case.get('source', '-')}\n"
        f"message={case['message']}\n"
        f"expected={case['expect']}\n"
        f"actual={decision}"
    )
