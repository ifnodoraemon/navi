import os
import pytest
from pathlib import Path

from navi.config import load_config, ModelConfig
from navi.provider import build_provider
from navi.runtime import AgentRuntime
from navi.engine import HernessEngine
from navi.event_bus import EventBus


@pytest.mark.asyncio
@pytest.mark.skipif("DEEPSEEK_API_KEY" not in os.environ, reason="DEEPSEEK_API_KEY not set")
async def test_e2e_cloud_agent_run(navi_home, monkeypatch):
    """
    Run a real E2E test against the cloud LLM using the deepseek provider.
    This uses the real execution path.
    """
    # Force configuration to deepseek
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "deepseek")
    monkeypatch.setenv("NAVI_MODEL", "deepseek-v4-pro")
    
    bus = EventBus()
    config = load_config(navi_home)
    provider = build_provider(config.model)
    
    runtime = AgentRuntime(home=navi_home, provider=provider)
    engine = HernessEngine(
        home=navi_home,
        runtime=runtime,
        project_dir=Path.cwd(),
        event_bus=bus,
    )
    
    # We ask the agent to call tools.list, which should be very fast and deterministic.
    response = await engine.handle(
        "List your capabilities using the tools.list tool",
        peer_id="test",
        sender_id="test",
        source="cli"
    )
    
    # Clean shutdown
    await engine.shutdown(5)
    await bus.drain()
    
    # Verification
    assert response is not None
    assert response.action == "tool" or "tool" in response.text or "capabilities" in response.text.lower()
