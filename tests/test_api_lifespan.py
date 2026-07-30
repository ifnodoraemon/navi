from __future__ import annotations

from pathlib import Path

import pytest

import navi.api as api_module


class FakeConnectorAdapter:
    name = "fake"
    setup_calls = 0
    run_calls = 0

    def enabled(self, home):
        return True

    def status(self, home):
        return {"configured": True}

    async def setup(self, home, project_dir, timeout, on_qr):
        self.setup_calls += 1

    async def run(self, home, project_dir, once):
        self.run_calls += 1


@pytest.mark.asyncio
async def test_api_lifespan_is_headless_by_default(
    tmp_path, monkeypatch, valid_runtime_config
):
    background_calls = []
    adapter = FakeConnectorAdapter()

    async def fake_process_background(self):
        background_calls.append(("maintenance", self.home))
        return []

    async def fake_process_queue(self):
        background_calls.append(("queue", self.home))
        return []

    monkeypatch.setattr(
        api_module.SystemDaemon,
        "process_background_once",
        fake_process_background,
    )
    monkeypatch.setattr(api_module.SystemDaemon, "process_queue_once", fake_process_queue)
    monkeypatch.setattr(api_module, "load_connector_adapters", lambda: [adapter])

    app = api_module.create_app(tmp_path)
    async with app.router.lifespan_context(app):
        pass

    assert background_calls == []
    assert adapter.setup_calls == 0
    assert adapter.run_calls == 0


def test_trace_ui_is_served_from_packaged_assets(tmp_path, valid_runtime_config):
    app = api_module.create_app(tmp_path)

    static_route = next(
        route for route in app.routes if getattr(route, "path", "") == "/ui/trace"
    )
    index = Path(static_route.app.directory) / "index.html"
    body = index.read_text(encoding="utf-8")

    assert static_route.app.html is True
    assert "Navi Trace Explorer" in body
    assert "/ui/trace/assets/" in body
