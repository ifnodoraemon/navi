from __future__ import annotations

import pytest

from navi.assistant import ActiveAssistant
from navi.auth import AuthInspector


@pytest.mark.asyncio
async def test_task_approval_execution_and_evolution(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_CODEX_MOCK", "true")
    assistant = ActiveAssistant(tmp_path)

    planned = await assistant.handle_weixin_command(
        "/task improve the navi project",
        peer_id="peer",
        sender_id="sender",
    )

    assert "Why now:" in planned.text
    task = assistant.tasks.get(planned.task_id)
    assert task is not None
    assert task.status == "awaiting_approval"
    approval = assistant.tasks.list_approvals()[0]

    approved = await assistant.handle_weixin_command(
        f"/approve {approval.code}",
        peer_id="peer",
        sender_id="sender",
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


def test_auth_inspector_shape():
    statuses = AuthInspector().status()

    assert {status.name for status in statuses} == {"codex", "gemini"}


@pytest.mark.asyncio
async def test_evolution_rollback_restores_memory_skill_graph_and_trust(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_CODEX_MOCK", "true")
    assistant = ActiveAssistant(tmp_path)
    planned = await assistant.handle_weixin_command(
        "/task evolve rollback coverage",
        peer_id="peer",
        sender_id="sender",
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
