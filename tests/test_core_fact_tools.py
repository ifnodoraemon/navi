from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from navi.core_tools import web_search as web_search_utils
from navi.loop_contracts import LockMode
from navi.tools import build_tool_gateway
from navi.workspaces import WorkspaceLockStore

# Shared constant: the DuckDuckGo HTML endpoint used by the fallback provider.
_DDG_SEARCH_URL_FROM_UTILS = web_search_utils._DDG_SEARCH_URL


@pytest.mark.asyncio
async def test_codebase_search_uses_runtime_rag_and_navi_home_cache(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("def target_workflow():\n    return 'ok'\n", encoding="utf-8")

    gateway = build_tool_gateway(home, project_dir=workspace)
    result = await gateway.call("codebase.search", {"query": "target_workflow", "limit": 1})

    assert result.ok is True
    assert result.facts["results"]
    assert (home / "codebase_rag.db").exists()
    assert not (workspace / ".navi" / "codebase_rag.db").exists()


@pytest.mark.asyncio
async def test_directory_list_returns_workspace_entries(tmp_path: Path) -> None:
    (tmp_path / "visible.txt").write_text("ok", encoding="utf-8")
    (tmp_path / ".hidden").write_text("secret", encoding="utf-8")

    gateway = build_tool_gateway(tmp_path / "home", project_dir=tmp_path)
    result = await gateway.call("directory.list", {"path": "."})

    assert result.ok is True
    names = {entry["name"] for entry in result.facts["entries"]}
    assert "visible.txt" in names
    assert ".hidden" not in names
    assert "response" not in result.facts


@pytest.mark.asyncio
async def test_file_write_uses_workspace_lock_via_gateway(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gateway = build_tool_gateway(home, project_dir=workspace)

    result = await gateway.call(
        "file.write",
        {"path": "out.txt", "content": "hello", "create_dirs": True},
    )

    assert result.ok is True
    assert result.facts["workspace_lock"]["acquired"] is True
    assert (workspace / "out.txt").read_text(encoding="utf-8") == "hello"
    assert WorkspaceLockStore(home).list_active() == ()


@pytest.mark.asyncio
async def test_file_write_blocks_on_workspace_lock_conflict(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "locked.txt"
    target.write_text("original", encoding="utf-8")
    WorkspaceLockStore(home).acquire(
        owner_run_id="other-run",
        resource="locked.txt",
        mode=LockMode.WRITE,
        ttl_seconds=60,
    )
    gateway = build_tool_gateway(home, project_dir=workspace)

    result = await gateway.call(
        "file.write",
        {"path": "locked.txt", "content": "new"},
    )

    assert result.ok is False
    assert result.error == "workspace lock conflict"
    assert result.facts["workspace_lock"]["conflicts"][0]["owner_run_id"] == "other-run"
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_file_write_can_target_shadow_workspace_and_merge_back(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("base\n", encoding="utf-8")
    gateway = build_tool_gateway(home, project_dir=workspace)

    shadow = await gateway.call("workspace.shadow.create", {"run_id": "run-shadow"})
    write = await gateway.call(
        "file.write",
        {
            "path": "app.py",
            "content": "agent\n",
            "shadow_run_id": "run-shadow",
        },
    )

    assert shadow.ok is True
    assert write.ok is True
    assert write.facts["state_transition"] == "shadow_written"
    assert target.read_text(encoding="utf-8") == "base\n"
    assert Path(write.facts["shadow_path"]).read_text(encoding="utf-8") == "agent\n"

    merged = await gateway.call("workspace.shadow.merge", {"run_id": "run-shadow"})

    assert merged.ok is True
    assert merged.facts["merge_status"] == "clean"
    assert merged.facts["completion_evidence"] is True
    assert target.read_text(encoding="utf-8") == "agent\n"


@pytest.mark.asyncio
async def test_shadow_merge_conflict_preserves_real_workspace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("base\n", encoding="utf-8")
    gateway = build_tool_gateway(home, project_dir=workspace)

    await gateway.call("workspace.shadow.create", {"run_id": "run-conflict"})
    write = await gateway.call(
        "file.write",
        {
            "path": "app.py",
            "content": "agent\n",
            "shadow_run_id": "run-conflict",
        },
    )
    target.write_text("human\n", encoding="utf-8")
    merged = await gateway.call("workspace.shadow.merge", {"run_id": "run-conflict"})

    assert write.ok is True
    assert merged.ok is True
    assert merged.facts["state_transition"] == "conflicted"
    assert merged.facts["merge_status"] == "conflicted"
    assert merged.facts["completion_evidence"] is False
    assert merged.facts["conflicts"] == ["app.py"]
    assert target.read_text(encoding="utf-8") == "human\n"
    artifact = Path(merged.facts["artifact_path"]) / "app.py"
    assert "<<<<<<< CURRENT" in artifact.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_shadow_discard_removes_shadow_without_real_change(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("base\n", encoding="utf-8")
    gateway = build_tool_gateway(home, project_dir=workspace)

    shadow = await gateway.call("workspace.shadow.create", {"run_id": "run-discard"})
    await gateway.call(
        "file.write",
        {
            "path": "app.py",
            "content": "agent\n",
            "shadow_run_id": "run-discard",
        },
    )
    discarded = await gateway.call("workspace.shadow.discard", {"run_id": "run-discard"})

    assert discarded.ok is True
    assert target.read_text(encoding="utf-8") == "base\n"
    assert not Path(shadow.facts["shadow_workspace"]).exists()


def test_web_search_uses_reachable_duckduckgo_endpoint(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}
    monkeypatch.setenv("NAVI_WEB_SEARCH_PROVIDER", "ddg")

    def fake_run(cmd, **kwargs):
        del kwargs
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                '<html><body><div class="result">'
                '<a class="result__a" '
                'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnavi&amp;rut=abc">'
                "Navi result</a>"
                '<a class="result__snippet" href="#">Search result snippet</a>'
                "</div></body></html>"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = web_search_utils._web_search({"query": "navi smoke"})

    assert result.ok is True
    assert _DDG_SEARCH_URL_FROM_UTILS in captured["cmd"]
    assert result.facts["provider"] == "duckduckgo"
    assert result.facts["results"] == [
        {
            "title": "Navi result",
            "url": "https://example.com/navi",
            "snippet": "Search result snippet",
            "engine": "duckduckgo",
        }
    ]
    assert "Search result" in result.facts["response"]["text"]


def test_web_search_uses_configured_searxng_json_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("NAVI_WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("NAVI_WEB_SEARCH_SEARXNG_URL", "https://search.example")

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self, max_bytes):
            captured["max_bytes"] = max_bytes
            return json.dumps(
                {
                    "results": [
                        {
                            "title": "Navi",
                            "url": "https://example.com/navi",
                            "content": "Search result",
                            "engine": "bing",
                            "category": "general",
                            "publishedDate": "2026-07-06",
                        }
                    ],
                    "answers": ["direct answer"],
                    "suggestions": ["navi agent"],
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["user_agent"] = request.headers["User-agent"]
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(web_search_utils, "urlopen", fake_urlopen)

    result = web_search_utils._web_search(
        {"query": "navi smoke", "limit": 3, "categories": "general", "language": "en"}
    )

    assert result.ok is True
    assert result.facts["provider"] == "searxng"
    assert result.facts["endpoint"] == "https://search.example"
    assert result.facts["answers"] == ["direct answer"]
    assert result.facts["suggestions"] == ["navi agent"]
    assert result.facts["results"] == [
        {
            "title": "Navi",
            "url": "https://example.com/navi",
            "snippet": "Search result",
            "engine": "bing",
            "category": "general",
            "published_date": "2026-07-06",
        }
    ]
    parsed = urlparse(str(captured["url"]))
    assert parsed.scheme == "https"
    assert parsed.netloc == "search.example"
    assert parsed.path == "/search"
    assert parse_qs(parsed.query) == {
        "q": ["navi smoke"],
        "format": ["json"],
        "categories": ["general"],
        "language": ["en"],
    }
    assert captured["user_agent"] == "Navi/1.0"
    assert captured["timeout"] == 15
    assert captured["max_bytes"] == 2_000_000


def test_web_search_auto_falls_back_to_duckduckgo_with_provider_error_facts(monkeypatch) -> None:
    monkeypatch.setenv("NAVI_WEB_SEARCH_PROVIDER", "auto")
    monkeypatch.setenv("NAVI_WEB_SEARCH_SEARXNG_URL", "https://search.example")

    def fake_urlopen(request, timeout):
        del request, timeout
        raise OSError("temporary search outage")

    def fake_run(cmd, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                '<html><body><div class="result">'
                '<a class="result__a" '
                'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ffallback.example">Fallback</a>'
                '<a class="result__snippet" href="#">Fallback result</a>'
                "</div></body></html>"
            ),
            stderr="",
        )

    monkeypatch.setattr(web_search_utils, "urlopen", fake_urlopen)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = web_search_utils._web_search({"query": "navi smoke"})

    assert result.ok is True
    assert result.facts["provider"] == "duckduckgo"
    assert result.facts["results"][0]["url"] == "https://fallback.example"
    assert result.facts["provider_errors"] == [
        {
            "provider": "searxng",
            "error_reason": "search_provider_error",
            "error": "temporary search outage",
            "endpoint": "https://search.example",
            "error_type": "OSError",
        }
    ]


def test_web_search_explicit_searxng_failure_does_not_use_duckduckgo(monkeypatch) -> None:
    monkeypatch.setenv("NAVI_WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("NAVI_WEB_SEARCH_SEARXNG_URL", "https://search.example")

    def fake_urlopen(request, timeout):
        del request, timeout
        raise TimeoutError("timed out")

    def fail_run(cmd, **kwargs):
        del cmd, kwargs
        raise AssertionError("explicit SearXNG mode must not call DuckDuckGo fallback")

    monkeypatch.setattr(web_search_utils, "urlopen", fake_urlopen)
    monkeypatch.setattr(subprocess, "run", fail_run)

    result = web_search_utils._web_search({"query": "navi smoke"})

    assert result.ok is False
    assert result.facts["provider"] == "searxng"
    assert result.facts["error_reason"] == "search_provider_error"
    assert result.facts["provider_errors"] == [
        {
            "provider": "searxng",
            "error_reason": "search_timeout",
            "error": "SearXNG search request timed out",
            "endpoint": "https://search.example",
        }
    ]


def test_web_search_explicit_searxng_without_endpoint_is_config_error(monkeypatch) -> None:
    monkeypatch.setenv("NAVI_WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.delenv("NAVI_WEB_SEARCH_SEARXNG_URL", raising=False)
    monkeypatch.delenv("NAVI_WEB_SEARCH_SEARXNG_URLS", raising=False)

    def fail_run(cmd, **kwargs):
        del cmd, kwargs
        raise AssertionError("explicit SearXNG mode without endpoint must not call fallback")

    monkeypatch.setattr(subprocess, "run", fail_run)

    result = web_search_utils._web_search({"query": "navi smoke"})

    assert result.ok is False
    assert result.facts["provider"] == "searxng"
    assert result.facts["error_reason"] == "search_provider_config_error"
    assert "NAVI_WEB_SEARCH_SEARXNG_URL" in result.error


def test_web_search_duckduckgo_challenge_returns_blocked_fact(monkeypatch) -> None:
    monkeypatch.setenv("NAVI_WEB_SEARCH_PROVIDER", "ddg")

    def fake_run(cmd, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                '<html><body><form id="challenge-form">'
                '<div class="anomaly-modal">Unfortunately, bots use DuckDuckGo too.</div>'
                "</form></body></html>"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = web_search_utils._web_search({"query": "navi smoke"})

    assert result.ok is False
    assert result.facts["provider"] == "duckduckgo"
    assert result.facts["error_reason"] == "search_provider_blocked"
    assert result.facts["block_reason"] == "duckduckgo_anomaly_challenge"


def test_web_search_returns_curl_transport_facts(monkeypatch) -> None:
    monkeypatch.setenv("NAVI_WEB_SEARCH_PROVIDER", "ddg")

    def fake_run(cmd, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            cmd,
            35,
            stdout="",
            stderr="SSL_ERROR_SYSCALL",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = web_search_utils._web_search({"query": "navi smoke"})

    assert result.ok is False
    assert result.facts["provider"] == "duckduckgo"
    assert result.facts["error_reason"] == "search_provider_error"
    assert result.facts["curl_exit_code"] == 35
    assert result.facts["stderr"] == "SSL_ERROR_SYSCALL"
