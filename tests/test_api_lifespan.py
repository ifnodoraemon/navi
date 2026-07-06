from __future__ import annotations

from fastapi.testclient import TestClient

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


def test_api_lifespan_is_headless_by_default(tmp_path, monkeypatch):
    daemon_starts = []
    adapter = FakeConnectorAdapter()

    def fake_daemon_start(self):
        daemon_starts.append(self.home)

    monkeypatch.setattr(api_module.SystemDaemon, "start", fake_daemon_start)
    monkeypatch.setattr(api_module, "load_connector_adapters", lambda: [adapter])

    app = api_module.create_app(tmp_path)
    with TestClient(app):
        pass

    assert daemon_starts == []
    assert adapter.setup_calls == 0
    assert adapter.run_calls == 0
