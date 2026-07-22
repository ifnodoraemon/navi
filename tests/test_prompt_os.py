from __future__ import annotations

from pathlib import Path

from navi.prompt_os import (
    assemble_fact_response_system_prompt,
    assemble_goal_event_compaction_messages,
    assemble_memory_consolidation_messages,
    assemble_notification_system_prompt,
    assemble_semantic_checker_messages,
    assemble_summarizer_messages,
)
from navi.specs_data import PROMPT_ASSEMBLIES_SPEC


def _spec_content(assembly_name: str, block_name: str) -> str:
    assembly = PROMPT_ASSEMBLIES_SPEC[assembly_name]
    for block in assembly["blocks"]:
        if block["name"] == block_name:
            return block["content"]
    raise AssertionError(f"missing prompt spec block: {assembly_name}.{block_name}")


def test_runtime_prompt_assemblies_are_backed_by_global_specs() -> None:
    fact_response = assemble_fact_response_system_prompt()
    notification = assemble_notification_system_prompt()
    semantic_checker = assemble_semantic_checker_messages(
        objective="ship report",
        acceptance_criteria=["report delivered"],
        current_time={"iso": "2026-07-17T00:00:00+08:00"},
        trigger_facts={"source": "test"},
        task_context={"authority": "test"},
        attempt=1,
        max_attempts=2,
        last_capability={"tool": "channel.send_file"},
        observed_capability_evidence={"delivery": "requested"},
    )
    compaction = assemble_goal_event_compaction_messages(["event-a", "event-b"])
    summarizer = assemble_summarizer_messages("user: hi")
    memory_consolidation = assemble_memory_consolidation_messages(
        task_prompt="Return structured memory changes.",
        transcript=[{"role": "user", "content": "Remember that I prefer concise replies."}],
        active_memory=[],
        scope="source:test",
    )

    assert fact_response.blocks[0].content == _spec_content(
        "fact_response_system", "FACT RESPONSE BOUNDARY"
    )
    assert notification.blocks[0].content == _spec_content(
        "notification_system", "NOTIFICATION DECISION BOUNDARY"
    )
    assert semantic_checker[0].content == _spec_content(
        "semantic_checker_messages", "SEMANTIC CHECKER SYSTEM"
    )
    assert compaction[0].content == _spec_content(
        "goal_event_compaction_messages", "GOAL EVENT COMPACTION USER"
    ).format(goal_events="event-a\nevent-b")
    assert summarizer[0].content == _spec_content(
        "conversation_summarizer_messages", "CONVERSATION SUMMARIZER SYSTEM"
    )
    assert summarizer[1].content == _spec_content(
        "conversation_summarizer_messages", "CONVERSATION SUMMARIZER USER"
    ).format(transcript="user: hi")
    assert _spec_content(
        "memory_consolidation_messages", "MEMORY CONSOLIDATION BOUNDARY"
    ) in memory_consolidation[0].content
    assert "UNTRUSTED INPUT BLOCK" in memory_consolidation[1].content
    assert "source:test" in memory_consolidation[1].content


def test_stable_prompt_text_is_not_scattered_across_runtime_modules() -> None:
    root = Path(__file__).resolve().parents[1]
    src = root / "src" / "navi"
    allowed = {
        src / "specs_data.py",
        src / "evals.py",
        src / "weixin" / "evals.py",
    }
    markers = (
        "You are ",
        "Generate the user-facing reply",
        "Decide whether the verified background event",
        "Summarize the following goal events",
        "Do not invent information not present in the transcript.",
        "Only explicit durable user preferences",
    )
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker in text:
                offenders.append(f"{path.relative_to(root)} contains {marker!r}")

    assert offenders == []
