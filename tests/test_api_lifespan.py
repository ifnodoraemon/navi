from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import navi.api as api_module
from navi.api_paths import api_path
from navi.config import load_config


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


@pytest.mark.asyncio
async def test_health_reports_runtime_and_connector_facts(
    tmp_path, monkeypatch, valid_runtime_config
):
    adapter = FakeConnectorAdapter()
    monkeypatch.setattr(
        adapter,
        "status",
        lambda home: {
            "status": "healthy",
            "ingress_status": "healthy",
            "egress_status": "healthy",
            "proactive_egress_status": "healthy",
        },
    )
    monkeypatch.setattr(api_module, "load_connector_adapters", lambda: [adapter])
    monkeypatch.setattr(api_module, "runtime_environment_error", lambda: "")
    app = api_module.create_app(tmp_path)
    api_key = load_config(tmp_path).api.api_key
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )

    response = await client.get(api_path("health"), headers={"X-API-Key": api_key})

    assert response.status_code == 200
    health = response.json()["data"]
    assert health["ok"] is True
    assert health["runtime"] == {"status": "healthy", "error": ""}
    assert health["connectors"]["fake"]["status"] == "healthy"
    await client.aclose()


@pytest.mark.asyncio
async def test_health_fails_when_resident_runtime_disappears(
    tmp_path, monkeypatch, valid_runtime_config
):
    adapter = FakeConnectorAdapter()
    monkeypatch.setattr(
        adapter,
        "status",
        lambda home: {
            "status": "healthy",
            "ingress_status": "healthy",
            "egress_status": "healthy",
            "proactive_egress_status": "healthy",
        },
    )
    monkeypatch.setattr(api_module, "load_connector_adapters", lambda: [adapter])
    monkeypatch.setattr(
        api_module,
        "runtime_environment_error",
        lambda: "python executable missing: /missing/python",
    )
    app = api_module.create_app(tmp_path)
    api_key = load_config(tmp_path).api.api_key
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )

    response = await client.get(api_path("health"), headers={"X-API-Key": api_key})

    assert response.status_code == 503
    health = response.json()["error"]["detail"]
    assert health["ok"] is False
    assert health["runtime"]["status"] == "unavailable"
    assert health["issues"] == [
        {
            "component": "runtime",
            "error": "python executable missing: /missing/python",
        }
    ]
    await client.aclose()
