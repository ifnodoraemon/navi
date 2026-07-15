from pathlib import Path

import pytest

from navi.config import load_config
from navi.control_plane import TurnController
from navi.provider import build_provider
from navi.runtime import AgentRuntime


@pytest.mark.asyncio
@pytest.mark.live_llm
async def test_e2e_cloud_agent_run(navi_home: Path) -> None:
    """Exercise the configured provider through the current turn controller."""
    config = load_config(navi_home)
    runtime = AgentRuntime(home=navi_home, provider=build_provider(config.model))
    controller = TurnController(
        home=navi_home,
        runtime=runtime,
        project_dir=Path.cwd(),
    )

    response = await controller.handle(
        "Use tools.list, then briefly report which capabilities are available.",
        peer_id="test",
        sender_id="test",
        source="cli",
    )
    await controller.shutdown(timeout=5)

    assert response is not None
    assert response.surfaced_text().strip()
    assert response.action in {"chat", "tool"}
