from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from navi.evals import (
    load_claw_eval_dataset,
    load_daily_journey_eval_dataset,
    load_delegation_eval_cases,
    load_delegation_eval_dataset,
    match_delegation_eval_case,
    run_claw_eval_dataset,
    delegation_eval_tools,
    run_daily_journey_eval_dataset,
    run_delegation_eval_dataset,
    validate_delegation_eval_dataset,
)
from navi.provider import ChatMessage, MockProvider, ModelPool
from navi.syscalls import ModelSyscall
from navi.weixin.evals import load_journey_eval_dataset, run_journey_eval_dataset


def _dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "delegation_cases.yaml"


def _daily_dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "daily_journeys.yaml"


def _user_dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "user_journeys.yaml"


def _regression_dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "regression_journeys.yaml"


def _public_agent_dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "public_agent_journeys.yaml"


def _claw_dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "claw_navi.yaml"


def _weixin_dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "weixin_journeys.yaml"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


class ScriptedEvalProvider(MockProvider):
    def __init__(self, decisions: list[dict]):
        self.decisions = list(decisions)

    async def complete(self, messages: list[ChatMessage]) -> str:
        if messages and "model syscall planner" in messages[0].content:
            user_prompt = next((msg.content for msg in reversed(messages) if msg.role == "user"), "")
            observations = _tagged(user_prompt, "observed_facts")
            if observations:
                if '"capability": "skills.list"' in observations or '"capability": "tools.list"' in observations:
                    return await super().complete(messages)
                if not any(token in observations for token in ('"status": "pending"', '"status": "prepared"', '"watch_id":')):
                    return json.dumps(
                        _decision("final.answer", "read", {"message": observations}),
                        ensure_ascii=False,
                    )
                return await super().complete(messages)
            user_text = _tagged(user_prompt, "user_message")
            decision = self.decisions.pop(0) if self.decisions else _decision("final.answer", "read", {"message": f"Navi received: {user_text}"})
            decision = _fill_dynamic_args(decision, user_text)
            return json.dumps(decision, ensure_ascii=False)
        return await super().complete(messages)


def _tagged(text: str, tag: str) -> str:
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, flags=re.DOTALL)
    return match.group(1).strip() if match else ""


def _decision(tool: str, permission: str, args: dict | None = None) -> dict:
    return {
        "tool": tool,
        "permission": permission,
        "args": args or {},
        "model_role": "responder",
        "confidence": 1.0,
        "reason": "scripted eval decision",
    }


def _fill_dynamic_args(decision: dict, user_text: str) -> dict:
    tool = str(decision.get("tool") or "")
    args = dict(decision.get("args") or {})
    if tool == "approval.resolve":
        code = re.search(r"\b\d{6}\b", user_text)
        if code:
            args["code"] = code.group(0)
    if tool in {"delegate.status", "delegate.delete", "delegate.retry", "delegate.prepare", "delegate.run", "approval.request"}:
        run_id = re.search(r"\b[a-f0-9]{32}\b", user_text)
        if run_id:
            args["run_id"] = run_id.group(0)
    return {**decision, "args": args}


def _scripted_pool(decisions: list[dict]) -> ModelPool:
    return ModelPool(default=ScriptedEvalProvider(decisions))


def _delegation_decisions(path: Path) -> list[dict]:
    dataset = load_delegation_eval_dataset(path)
    return [
        _decision(str(case["expect"]["tool"]), str(case["expect"]["permission"]), dict(case["expect"].get("args") or {}))
        for case in dataset["cases"]
    ]


def _journey_decisions(path: Path) -> list[dict]:
    data = load_daily_journey_eval_dataset(path)
    return _decisions_for_journeys(data["journeys"])


def _claw_decisions(path: Path, *, attempts: int) -> list[dict]:
    data = load_claw_eval_dataset(path)
    decisions: list[dict] = []
    for task in data["tasks"]:
        for _ in range(attempts):
            decisions.extend(_decisions_for_journeys([task["journey"]]))
    return decisions


def _connector_decisions(path: Path) -> list[dict]:
    data = load_journey_eval_dataset(path)
    return _decisions_for_journeys(data["journeys"], inbound_key="inbound")


def _decisions_for_journeys(journeys: list[dict], *, inbound_key: str = "user") -> list[dict]:
    decisions: list[dict] = []
    for journey in journeys:
        if journey.get("provider") == "failing":
            continue
        for step in journey.get("steps") or []:
            if inbound_key == "inbound" and "inbound" not in step:
                continue
            if inbound_key == "user" and "user" not in step:
                continue
            if inbound_key == "inbound" and (step.get("expect") or {}).get("handled") is False:
                continue
            text = str((step.get("inbound") or {}).get("text") if inbound_key == "inbound" else step.get("user") or "")
            decisions.append(_decision_for_expectation(step.get("expect") or {}, text))
    return decisions


def _decision_for_expectation(expect: dict, text: str) -> dict:
    action = str(expect.get("action") or "")
    if not action:
        if "skill" in text.lower() or "工具" in text or "可以做什么" in text:
            action = "tool"
        elif "连接器" in text or "微信" in text:
            action = "tool"
        elif "哪些任务" in text:
            action = "tool"
        elif "清理" in text:
            action = "delegation"
        elif "明天" in text:
            action = "ask"
        if not action:
            if "watch_count_delta" in expect and int(expect.get("watch_count_delta") or 0) > 0:
                action = "watch"
            elif "run_count_delta" in expect and int(expect.get("run_count_delta") or 0) > 0:
                action = "approval"
            elif "failed_run_count" in expect:
                action = "delegation"
            else:
                action = "chat"
    if action == "approval":
        if "拒绝" in text:
            return _decision("approval.resolve", "write", {"decision": "reject"})
        if "批准" in text or "approve" in text.lower():
            return _decision("approval.resolve", "write", {"decision": "approve"})
        return _decision("delegate.spawn", "prepare", {"prompt": text})
    if action == "watch":
        if expect.get("watch_kind") == "once" or expect.get("watch_cron") == "once":
            return _decision("watch.create", "prepare", {"kind": "once", "run_at_text": text, "prompt": text})
        return _decision("watch.create", "prepare", {"kind": "recurring", "cron": "0 20 * * *", "prompt": text})
    if action == "ask":
        return _decision("clarify.ask", "read", {"message": "Please provide the exact recurring schedule or reminder time."})
    if action == "delegation":
        return _decision("delegate.delete", "write", {"status": "failed", "source": "watch"})
    if action == "tool":
        return _decision(_tool_for_text(text), "read", _tool_args_for_text(text))
    return _decision("final.answer", "read", {"message": f"Navi received: {text}"})


def _tool_for_text(text: str) -> str:
    lowered = text.lower()
    if "hook" in lowered or "hooks" in lowered:
        return "hooks.list"
    if "skill" in lowered:
        return "skills.list"
    if "冲突" in text and "记忆" in text:
        return "memory.conflicts"
    if "readme" in lowered:
        return "file.read"
    if "工具" in text or "可以做什么" in text:
        return "tools.list"
    if "provider" in lowered or "api key" in lowered or "模型" in text:
        return "provider.config"
    if "连接器" in text or "telegram" in lowered or "微信" in text:
        return "connector.weixin.status"
    if "为什么" in text or "没执行" in text:
        return "delegate.status"
    return "delegate.list"


def _tool_args_for_text(text: str) -> dict:
    tool = _tool_for_text(text)
    if tool == "file.read":
        return {"path": "README.md"}
    if tool == "delegate.status":
        run_id = re.search(r"\b[a-f0-9]{32}\b", text)
        return {"run_id": run_id.group(0)} if run_id else {}
    return {}


def test_task_eval_dataset_matches_capability_manifest(tmp_path):
    dataset = load_delegation_eval_dataset(_dataset())
    errors = validate_delegation_eval_dataset(dataset, delegation_eval_tools(tmp_path, project_dir=tmp_path))

    assert errors == []


def test_task_eval_dataset_has_100_percent_required_scenario_coverage():
    dataset = load_delegation_eval_dataset(_dataset())
    required = set(dataset["coverage"]["required_categories"])
    observed = {str(case["category"]) for case in dataset["cases"]}

    assert required <= observed
    assert len(required) >= 18
    assert {
        "decision_support",
        "translation_language",
        "data_analysis",
        "customer_support",
        "health_wellness_boundary",
        "finance_legal_boundary",
        "enterprise_workflow",
    } <= required


def test_task_eval_dataset_has_100_percent_required_tool_coverage(tmp_path):
    dataset = load_delegation_eval_dataset(_dataset())
    required = set(dataset["coverage"]["required_tools"])
    observed = {str(case["expect"]["tool"]) for case in dataset["cases"]}
    available = {tool.name for tool in delegation_eval_tools(tmp_path, project_dir=tmp_path)}

    assert required == available
    assert required <= observed


def test_task_eval_dataset_covers_lifecycle_regressions():
    cases = load_delegation_eval_cases(_dataset())
    ids = {str(case["id"]) for case in cases}

    assert {
        "list_delegations",
        "delete_delegation_from_recent_list",
        "delete_watch_from_recent_list",
        "hermes_connector_liveness_split",
        "hermes_provider_runtime_drift",
        "openclaw_memory_instruction_injection",
        "openclaw_broad_permission_skill_install",
        "openclaw_background_activity_guard",
        "exact_evening_watch",
        "spreadsheet_analysis_needs_delegation",
        "research_reading_from_local_file",
        "customer_support_ticket_triage_task",
        "health_wellness_general_boundary_answer",
        "finance_legal_boundary_answer",
        "approve_code",
        "reject_code",
    } <= ids


def test_task_eval_case_matcher_reports_arg_drift():
    case = {
        "id": "delete_delegation_from_recent_list",
        "expect": {
            "tool": "delegate.delete",
            "permission": "write",
            "args": {"run_id": "expected"},
        },
    }
    decision = ModelSyscall(tool="delegate.delete", permission="write", args={"run_id": "actual"})

    errors = match_delegation_eval_case(case, decision)

    assert "args.run_id expected 'expected', got 'actual'" in errors


@pytest.mark.asyncio
async def test_mock_planner_passes_delegation_eval_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("NAVI_MODEL", "mock")

    results = await run_delegation_eval_dataset(
        home=tmp_path,
        project_dir=tmp_path,
        dataset=_dataset(),
        timeout_seconds=1,
        provider=_scripted_pool(_delegation_decisions(_dataset())),
    )

    failures = [result for result in results if not result.ok]
    assert failures == []
    assert len(results) >= 48


def test_daily_journey_eval_dataset_is_user_facing():
    dataset = load_daily_journey_eval_dataset(_daily_dataset())
    ids = {str(journey["id"]) for journey in dataset["journeys"]}

    assert {
        "casual_chat_does_not_create_task",
        "local_work_request_gets_one_approval_task",
        "approval_then_background_execution_completes_goal",
        "recurring_evening_lesson_creates_watch",
        "vague_reminder_asks_clarification",
        "user_can_ask_current_task_list",
        "user_can_ask_why_task_not_executed",
        "user_can_reject_pending_task",
        "user_can_clean_failed_tasks",
        "user_can_check_model_provider_status",
        "user_can_check_connector_status",
        "writing_editing_no_task",
        "decision_support_no_task",
        "translation_language_no_task",
        "local_readme_context_tool",
        "data_analysis_report_needs_approval",
        "customer_support_triage_needs_approval",
        "privacy_redaction_needs_approval",
        "exact_one_shot_reminder_creates_once_watch",
        "broad_health_self_care_no_task",
        "finance_legal_explanation_no_task",
    } <= ids
    assert all("user_goal" in journey for journey in dataset["journeys"])
    assert len(dataset["journeys"]) >= 21


@pytest.mark.asyncio
async def test_mock_runtime_passes_daily_journey_eval_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("NAVI_MODEL", "mock")
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")

    results = await run_daily_journey_eval_dataset(
        home=tmp_path,
        project_dir=_repo_root(),
        dataset=_daily_dataset(),
        timeout_seconds=5,
        provider=_scripted_pool(_journey_decisions(_daily_dataset())),
    )

    failures = [result for result in results if not result.ok]
    assert failures == []


def test_user_journey_eval_dataset_is_extracted_from_real_dialogues():
    dataset = load_daily_journey_eval_dataset(_user_dataset())
    ids = {str(journey["id"]) for journey in dataset["journeys"]}

    assert {
        "execution_protocol_error_how_to_fix_prepares_task",
        "terse_fix_follow_up_uses_previous_error_context",
        "weixin_no_reply_checks_connector_trace",
        "contradiction_follow_up_does_not_create_task",
        "deepseek_network_claim_runs_provider_check",
    } <= ids
    assert all("source_dialogue" in journey for journey in dataset["journeys"])


@pytest.mark.asyncio
async def test_mock_runtime_passes_user_journey_eval_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("NAVI_MODEL", "mock")
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")

    results = await run_daily_journey_eval_dataset(
        home=tmp_path,
        project_dir=tmp_path,
        dataset=_user_dataset(),
        timeout_seconds=5,
        provider=_scripted_pool(_journey_decisions(_user_dataset())),
    )

    failures = [result for result in results if not result.ok]
    assert failures == []


def test_regression_journey_eval_dataset_tracks_real_incidents():
    dataset = load_daily_journey_eval_dataset(_regression_dataset())
    ids = {str(journey["id"]) for journey in dataset["journeys"]}
    incident_ids = {str(item["id"]) for item in dataset["incidents"]}

    assert {
        "weixin_hello_no_visible_reply",
        "terse_fix_lost_previous_error_context",
        "execution_protocol_evidence_must_be_list",
        "deepseek_direct_shell_for_error_repair",
        "watch_protocol_verification_must_be_object",
        "watch_created_then_delegate_status_false_failure",
        "one_shot_push_became_daily_recurring_watch",
        "provider_diagnostics_drift",
        "connector_status_drift",
    } <= incident_ids
    assert {
        "execution_protocol_error_how_to_fix_prepares_task",
        "terse_fix_follow_up_uses_previous_error_context",
        "exact_schedule_watch_creation_returns_watch_without_new_run",
        "task_list_shows_existing_watch_without_runs",
        "one_shot_time_push_creates_once_watch",
        "provider_config_check_does_not_create_task",
        "connector_status_check_does_not_create_task",
    } <= ids


@pytest.mark.asyncio
async def test_mock_runtime_passes_regression_journey_eval_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("NAVI_MODEL", "mock")
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")

    results = await run_daily_journey_eval_dataset(
        home=tmp_path,
        project_dir=tmp_path,
        dataset=_regression_dataset(),
        timeout_seconds=5,
        provider=_scripted_pool(_journey_decisions(_regression_dataset())),
    )

    failures = [result for result in results if not result.ok]
    assert failures == []


def test_public_agent_journey_eval_dataset_covers_hermes_and_openclaw_patterns():
    dataset = load_daily_journey_eval_dataset(_public_agent_dataset())
    ids = {str(journey["id"]) for journey in dataset["journeys"]}
    patterns = {str(journey.get("public_pattern") or "") for journey in dataset["journeys"]}

    assert {
        "public_hermes_pr_review_cron_to_messenger",
        "public_hermes_manual_pr_review_gets_approval",
        "public_hermes_provider_route_drift_check",
        "public_openclaw_bugfix_with_tests",
        "public_openclaw_privacy_redaction",
        "public_openclaw_memory_follow_up",
        "public_openclaw_broad_permission_boundary",
        "public_chatgpt_writing_editing",
        "public_chatgpt_decision_support",
        "public_anthropic_translation_language_learning",
        "public_enterprise_spreadsheet_automation",
        "public_enterprise_customer_support_triage",
        "public_enterprise_it_issue_resolution",
        "public_information_practices_local_identifying",
        "public_consumer_self_care_boundary",
    } <= ids
    assert any(pattern.startswith("hermes_") for pattern in patterns)
    assert any(pattern.startswith("clawbench_") for pattern in patterns)
    assert any(pattern.startswith("openai_") for pattern in patterns)
    assert any(pattern.startswith("anthropic_") for pattern in patterns)
    assert any(pattern.startswith("information_practices_") for pattern in patterns)
    assert len(dataset["journeys"]) >= 20


@pytest.mark.asyncio
async def test_mock_runtime_passes_public_agent_journey_eval_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("NAVI_MODEL", "mock")
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")

    results = await run_daily_journey_eval_dataset(
        home=tmp_path,
        project_dir=_repo_root(),
        dataset=_public_agent_dataset(),
        timeout_seconds=5,
        provider=_scripted_pool(_journey_decisions(_public_agent_dataset())),
    )

    failures = [result for result in results if not result.ok]
    assert failures == []


def test_claw_eval_dataset_is_user_task_shaped():
    dataset = load_claw_eval_dataset(_claw_dataset())
    task_ids = {str(task["task_id"]) for task in dataset["tasks"]}
    splits = {str(task["split"]) for task in dataset["tasks"]}

    assert {
        "navi_general_chat_no_task",
        "navi_multi_turn_local_task_approval_execution",
        "navi_general_task_status_query",
        "navi_general_reject_pending_task",
        "navi_general_failed_task_cleanup",
        "navi_general_exact_recurring_watch",
        "navi_general_vague_reminder_clarifies",
        "navi_general_provider_diagnostics",
        "navi_general_connector_diagnostics",
        "navi_safety_broad_permission_skill_install",
        "navi_public_hermes_pr_review_cron_to_messenger",
        "navi_public_hermes_manual_pr_review_gets_approval",
        "navi_public_hermes_provider_route_drift_check",
        "navi_public_openclaw_bugfix_with_tests",
        "navi_public_openclaw_privacy_redaction",
        "navi_public_openclaw_memory_follow_up",
        "navi_public_openclaw_broad_permission_boundary",
        "navi_public_chatgpt_writing_editing",
        "navi_public_chatgpt_decision_support",
        "navi_public_language_translation_learning",
        "navi_public_enterprise_spreadsheet_automation",
        "navi_public_enterprise_customer_support_triage",
        "navi_public_enterprise_it_issue_resolution",
        "navi_public_information_practices_local_identifying",
        "navi_public_consumer_self_care_boundary",
    } <= task_ids
    assert {"general", "multi_turn"} <= splits
    assert all("query" in task for task in dataset["tasks"])
    assert all("rubric_dimensions" in task for task in dataset["tasks"])


@pytest.mark.asyncio
async def test_mock_runtime_passes_claw_eval_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("NAVI_MODEL", "mock")
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")

    results = await run_claw_eval_dataset(
        home=tmp_path,
        project_dir=_repo_root(),
        dataset=_claw_dataset(),
        attempts=3,
        timeout_seconds=5,
        provider=_scripted_pool(_claw_decisions(_claw_dataset(), attempts=3)),
    )

    failures = [result for result in results if not result.ok]
    assert failures == []
    assert all(result.pass_count == 3 for result in results)


def test_weixin_journey_eval_dataset_is_user_visible():
    dataset = load_journey_eval_dataset(_weixin_dataset())
    ids = {str(journey["id"]) for journey in dataset["journeys"]}

    assert {
        "weixin_hello_replies_and_records_events",
        "weixin_provider_failure_returns_visible_fallback",
        "weixin_local_work_request_gets_approval",
        "weixin_execution_protocol_error_how_to_fix_gets_approval",
        "weixin_terse_fix_follow_up_uses_previous_error_context",
        "weixin_exact_schedule_creates_watch",
        "weixin_one_shot_push_creates_once_watch",
        "weixin_vague_reminder_clarifies_without_watch",
        "weixin_duplicate_message_is_ignored",
        "weixin_clean_failed_tasks",
    } <= ids
    assert all("user_goal" in journey for journey in dataset["journeys"])


@pytest.mark.asyncio
async def test_mock_runtime_passes_weixin_journey_eval_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")

    results = await run_journey_eval_dataset(
        tmp_path,
        tmp_path,
        _weixin_dataset(),
        timeout_seconds=5,
        provider=_scripted_pool(_connector_decisions(_weixin_dataset())),
    )

    failures = [result for result in results if not result.ok]
    assert failures == []
