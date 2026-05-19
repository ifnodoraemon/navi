from __future__ import annotations

import pytest

from navi.assistant import ActiveAssistant
from navi.auth import AuthInspector


@pytest.mark.asyncio
async def test_task_approval_execution_and_evolution(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    assistant = ActiveAssistant(tmp_path)

    planned = await assistant.handle_command(
        "/task create improve the navi project",
        peer_id="peer",
        sender_id="sender",
        source="weixin",
    )

    assert "Why now:" in planned.text
    task = assistant.tasks.get(planned.task_id)
    assert task is not None
    assert task.status == "awaiting_approval"
    approval = assistant.tasks.list_approvals()[0]

    approved = await assistant.handle_command(
        f"/approval approve {approval.code}",
        peer_id="peer",
        sender_id="sender",
        source="weixin",
    )
    assert "queued" in approved.text

    completed = await assistant.process_queue_once()

    assert completed[0].status == "completed"
    assert assistant.tasks.get(planned.task_id).result_summary
    assert assistant.graph.list()
    assert assistant.evolution.ledger.list()
    assert assistant.trust.list(sender_id="sender")


def test_watch_command_creates_cron_watch(tmp_path):
    assistant = ActiveAssistant(tmp_path)

    result = assistant.create_watch(
        "*/5 * * * * check the navi project",
        peer_id="peer",
        sender_id="sender",
    )

    assert "Watch" in result.text
    watches = assistant.tasks.list_watches()
    assert watches[0].cron == "*/5 * * * *"
    assert watches[0].prompt == "check the navi project"


@pytest.mark.asyncio
async def test_orthogonal_command_surface_lists_tasks_approvals_and_watches(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    assistant = ActiveAssistant(tmp_path)

    planned = await assistant.handle_command(
        "/task create run a command surface check",
        peer_id="peer",
        sender_id="sender",
        source="weixin",
    )
    watch = await assistant.handle_command(
        "/watch create 0 8 * * * check command health",
        peer_id="peer",
        sender_id="sender",
        source="weixin",
    )

    task_list = await assistant.handle_command("/task list", sender_id="sender")
    approval_list = await assistant.handle_command("/approval list", sender_id="sender")
    watch_list = await assistant.handle_command("/watch list", sender_id="sender")
    shown = await assistant.handle_command(f"/task show {planned.task_id}", sender_id="sender")

    assert "run a command surface check" in task_list.text
    assert assistant.tasks.list_approvals()[0].code in approval_list.text
    assert "check command health" in watch.text
    assert "check command health" in watch_list.text
    assert planned.task_id in shown.text


def test_watch_cron_tool_creates_watch(tmp_path):
    assistant = ActiveAssistant(tmp_path)

    result = assistant.create_watch_cron(
        "0 8 * * *",
        "进行毛选晨读",
        peer_id="peer",
        sender_id="sender",
    )

    assert "Watch" in result.text
    watches = assistant.tasks.list_watches()
    assert watches[0].cron == "0 8 * * *"
    assert watches[0].prompt == "进行毛选晨读"


def test_auth_inspector_shape():
    statuses = AuthInspector().status()

    assert {status.name for status in statuses} == {"codex", "gemini"}


@pytest.mark.asyncio
async def test_codex_plan_timeout_marks_task_failed(tmp_path, monkeypatch):
    monkeypatch.delenv("NAVI_EXECUTION_MOCK", raising=False)
    monkeypatch.setenv("NAVI_EXECUTION_TIMEOUT_SECONDS", "1")
    assistant = ActiveAssistant(tmp_path)
    task = assistant.tasks.create("timeout", status="pending")

    async def slow_plan(task):
        import asyncio

        await asyncio.sleep(2)

    monkeypatch.setattr(assistant.execution.providers["codex"], "plan", slow_plan)
    planned = await assistant.execution.plan_task(task)

    assert planned.status == "failed"
    assert "timed out" in planned.error


@pytest.mark.asyncio
async def test_evolution_rollback_restores_memory_skill_graph_and_trust(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    assistant = ActiveAssistant(tmp_path)
    planned = await assistant.handle_command(
        "/task create evolve rollback coverage",
        peer_id="peer",
        sender_id="sender",
        source="weixin",
    )
    approval = assistant.tasks.list_approvals()[0]
    assistant.approve(approval.code, sender_id="sender")
    completed = (await assistant.process_queue_once())[0]

    events = assistant.evolution.ledger.list()
    assert {event.target_type for event in events} >= {"memory", "skill", "graph_node", "trust_rule"}

    for event in events:
        rolled_back = assistant.evolution.rollback(event.id)
        assert rolled_back is not None
        assert rolled_back.rolled_back_at

    assert assistant.evolution.ledger.list()[0].rolled_back_at
    assert completed.id
