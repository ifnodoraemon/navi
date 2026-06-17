import pytest
from pathlib import Path

from navi.config import load_config
from navi.provider import build_provider
from navi.runtime import AgentRuntime
from navi.engine import HernessEngine


@pytest.mark.asyncio
@pytest.mark.live_llm
async def test_e2e_cloud_agent_run(navi_home, monkeypatch):
    """
    Run a real E2E test against the cloud LLM using the deepseek provider.
    This uses the real execution path. Skipped when no LLM credentials are set
    (see the live_llm marker handling in conftest).
    """
    # Force configuration to deepseek
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "deepseek")
    monkeypatch.setenv("NAVI_MODEL", "deepseek-v4-pro")
    
    config = load_config(navi_home)
    provider = build_provider(config.model)
    
    runtime = AgentRuntime(home=navi_home, provider=provider)
    engine = HernessEngine(
        home=navi_home,
        runtime=runtime,
        project_dir=Path.cwd(),
    )
    
    # We ask the agent to call tools.list, which should be very fast and deterministic.
    response = await engine.handle(
        "List your capabilities using the tools.list tool",
        peer_id="test",
        sender_id="test",
        source="cli"
    )
    
    # Clean shutdown
    await engine.shutdown(timeout=5)

    # Verification
    assert response is not None
    assert response.action == "tool" or "tool" in response.text or "capabilities" in response.text.lower()
