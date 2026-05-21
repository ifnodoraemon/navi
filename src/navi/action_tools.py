from __future__ import annotations

from .tools import ToolSpec


def load_action_tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="final.answer",
            description="Return a direct answer to the user when no more tool call is needed.",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            output_schema={"type": "object"},
            source="action",
        ),
        ToolSpec(
            name="clarify.ask",
            description=(
                "Ask one concise clarification question when a user-owned fact is required, "
                "or when a recurring schedule has no exact time."
            ),
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            output_schema={"type": "object"},
            source="action",
        ),
        ToolSpec(
            name="task.create",
            description=(
                "Create a tracked local task for controlled local work, engineering investigation, "
                "bug diagnosis, config-to-runtime mapping, performance investigation, or code changes. "
                "The task can inspect the local project before asking for repository paths or implementation details."
            ),
            input_schema={
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
            output_schema={"type": "object"},
            facts_only=False,
            mutates=True,
            permission="prepare",
            source="action",
        ),
        ToolSpec(
            name="watch.create",
            description="Create a recurring watch only when an exact cron expression can be derived.",
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "cron": {"type": "string"},
                },
                "required": ["prompt", "cron"],
            },
            output_schema={"type": "object"},
            facts_only=False,
            mutates=True,
            permission="prepare",
            source="action",
        ),
        ToolSpec(
            name="approval.resolve",
            description="Approve or reject a pending task with an approval code or task id.",
            input_schema={
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": ["approve", "reject"]},
                    "code": {"type": "string"},
                    "task_id": {"type": "string"},
                },
                "required": ["decision"],
            },
            output_schema={"type": "object"},
            facts_only=False,
            mutates=True,
            permission="write",
            source="action",
        ),
    ]
