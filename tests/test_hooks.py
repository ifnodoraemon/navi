from __future__ import annotations

import pytest

from navi.capabilities import CapabilityContext, build_capability_registry
from navi.hooks import HookDecision, HookEvent, HookRegistry
from navi.memory import MemoryStore
from navi.tools import build_tool_gateway


def test_hook_registry_lists_lifecycle_specs(tmp_path):
    facts = HookRegistry(tmp_path).list_facts()

    assert facts["category"] == "hooks"
    assert facts["count"] >= 3
    assert {"before_capability", "after_capability", "before_memory_write"} <= {
        item["event"] for item in facts["hooks"]
    }


@pytest.mark.asyncio
async def test_capability_invocation_runs_before_and_after_hooks(tmp_path):
    capabilities = build_capability_registry(tmp_path, project_dir=tmp_path)

    class RecordingHooks:
        def __init__(self):
            self.events: list[HookEvent] = []

        def run(self, event: HookEvent) -> list[HookDecision]:
            self.events.append(event)
            return []

    hooks = RecordingHooks()
    capabilities.hooks = hooks

    result = await capabilities.invoke(
        "provider.config",
        {},
        permission="read",
        context=CapabilityContext(home=tmp_path, source="test", sender_id="sender", workspace=str(tmp_path)),
    )

    assert result.ok is True
    assert [event.event for event in hooks.events] == ["before_capability", "after_capability"]
    assert hooks.events[0].payload["tool"] == "provider.config"
    assert hooks.events[1].payload["ok"] is True
    assert "provider" in hooks.events[1].payload["fact_keys"]


@pytest.mark.asyncio
async def test_before_capability_hook_can_block_call(tmp_path):
    capabilities = build_capability_registry(tmp_path, project_dir=tmp_path)

    class BlockingHooks:
        def run(self, event: HookEvent) -> list[HookDecision]:
            if event.event == "before_capability":
                return [
                    HookDecision(
                        hook="test.block",
                        event=event.event,
                        decision="block",
                        reason="blocked by test hook",
                    )
                ]
            raise AssertionError("after hook should not run after a block")

    capabilities.hooks = BlockingHooks()

    result = await capabilities.invoke(
        "provider.config",
        {},
        permission="read",
        context=CapabilityContext(home=tmp_path),
    )

    assert result.ok is False
    assert result.message == "blocked by test hook"
    assert result.facts["hook_decision"]["hook"] == "test.block"


def test_hooks_list_tool_returns_hook_facts(tmp_path):
    gateway = build_tool_gateway(tmp_path, project_dir=tmp_path, allow_sources={"core"})

    result = gateway.call("hooks.list", {})

    assert result.ok is True
    assert result.facts["category"] == "hooks"
    assert "hooks" in result.facts


def test_memory_write_runs_before_memory_write_hook(tmp_path, monkeypatch):
    events: list[HookEvent] = []

    class RecordingHooks:
        def __init__(self, home):
            self.home = home

        def run(self, event: HookEvent) -> list[HookDecision]:
            events.append(event)
            return []

    monkeypatch.setattr("navi.memory.HookRegistry", RecordingHooks)

    item = MemoryStore(tmp_path).add_item(
        "preference",
        "Prefer structured OS hooks.",
        source="test",
    )

    assert item.content == "Prefer structured OS hooks."
    assert [event.event for event in events] == ["before_memory_write"]
    assert events[0].payload["type"] == "preference"


def test_memory_write_hook_can_block_persistence(tmp_path, monkeypatch):
    class BlockingHooks:
        def __init__(self, home):
            self.home = home

        def run(self, event: HookEvent) -> list[HookDecision]:
            return [
                HookDecision(
                    hook="test.memory_block",
                    event=event.event,
                    decision="block",
                    reason="memory write blocked",
                )
            ]

    monkeypatch.setattr("navi.memory.HookRegistry", BlockingHooks)

    with pytest.raises(ValueError, match="memory write blocked"):
        MemoryStore(tmp_path).add_item("fact", "Blocked fact.", source="test")

    assert MemoryStore(tmp_path).list_items() == []
