from __future__ import annotations

import pytest

from navi.provider import MockProvider
from navi.runtime import AgentRuntime


@pytest.mark.asyncio
async def test_runtime_persists_session_messages(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=MockProvider())

    reply = await runtime.chat("hello")

    assert reply.session_id
    assert reply.content == "Navi received: hello"
    messages = runtime.memory.get_messages(reply.session_id)
    assert [message.role for message in messages] == ["user", "assistant"]


def test_memory_append_and_read(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=MockProvider())

    runtime.memory.append_memory("Prefers concise replies")

    assert "Prefers concise replies" in runtime.memory.read_memory()
