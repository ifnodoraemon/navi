from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.goals import GoalStore
from navi.runs import RunStore
from navi.tools import ToolRegistry, ToolResult, ToolSpec


@pytest.mark.asyncio
async def test_mutating_gateway_tool_does_not_execute_without_audit_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry(home=tmp_path, project_dir=tmp_path)
    effect = {"executed": False}

    def mutate(_args):
        effect["executed"] = True
        return ToolResult(tool="test.mutate", ok=True, facts={"changed": True})

    registry.register(
        ToolSpec(
            name="test.mutate",
            capability_class="test",
            execution_contexts=("turn",),
            description="test mutation",
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "properties": {"changed": {"type": "boolean"}},
            },
            mutates=True,
            permission="write",
        ),
        mutate,
    )

    def unavailable(*args, **kwargs):
        raise OSError("audit db unavailable")

    monkeypatch.setattr(RunStore, "add_tool_call_log", unavailable)
    result = await registry.call("test.mutate", {})

    assert result.ok is False
    assert result.error_reason == "audit_unavailable"
    assert effect["executed"] is False


@pytest.mark.asyncio
async def test_read_only_shell_call_does_not_use_mutating_audit_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    def unavailable(*args, **kwargs):
        raise OSError("audit db unavailable")

    monkeypatch.setattr(ToolRegistry, "_reserve_mutating_audit", unavailable)
    result = await registry.invoke(
        "shell.run",
        {"command": ["pgrep", "-f", "definitely-not-a-real-navi-process"]},
        permission="read",
        context=CapabilityContext(
            home=tmp_path,
            source="local",
            peer_id="cli",
            sender_id="tester",
            workspace=str(tmp_path),
            permission_ceiling="read",
        ),
    )

    assert result.error_reason != "audit_unavailable"


@pytest.mark.asyncio
async def test_mutating_action_does_not_execute_without_audit_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
    )
    context = CapabilityContext(
        home=tmp_path,
        source="local",
        peer_id="cli",
        sender_id="tester",
        workspace=str(tmp_path),
        permission_ceiling="write",
    )

    def unavailable(*args, **kwargs):
        raise OSError("audit db unavailable")

    monkeypatch.setattr(RunStore, "add_tool_call_log", unavailable)
    result = await registry.invoke(
        "goal.open",
        {"objective": "must not be persisted", "auto_start": False},
        permission="prepare",
        context=context,
    )

    assert result.ok is False
    assert result.error_reason == "audit_unavailable"
    assert GoalStore(tmp_path).list(limit=10) == []
