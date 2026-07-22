from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from navi.config import NaviConfig, SearchConfig
from navi.core_tools import web_search as web_search_utils
from navi.loop_contracts import LockMode
from navi.memory import MemoryStore
from navi.tools import build_tool_gateway
from navi.workspaces import WorkspaceLockStore


@pytest.mark.asyncio
async def test_codebase_search_uses_runtime_rag_and_navi_home_cache(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text(
        "def target_workflow():\n    return 'ok'\n", encoding="utf-8"
    )

    gateway = build_tool_gateway(home, project_dir=workspace)
    result = await gateway.call("codebase.search", {"query": "target_workflow", "limit": 1})

    assert result.ok is True
    assert result.facts["results"]
    assert (home / "codebase_rag.db").exists()
    assert not (workspace / ".navi" / "codebase_rag.db").exists()


@pytest.mark.asyncio
async def test_shell_list_returns_workspace_entries(tmp_path: Path) -> None:
    (tmp_path / "visible.txt").write_text("ok", encoding="utf-8")
    (tmp_path / ".hidden").write_text("secret", encoding="utf-8")

    gateway = build_tool_gateway(tmp_path / "home", project_dir=tmp_path)
    result = await gateway.call("shell.run", {"command": ["ls", "-1"], "cwd": "."})

    assert result.ok is True
    names = set(result.facts["stdout"].splitlines())
    assert "visible.txt" in names
    assert ".hidden" not in names
    assert "response" not in result.facts
    assert result.facts["command"] == ["ls", "-1"]


@pytest.mark.asyncio
async def test_shell_run_rejects_shell_strings_instead_of_guessing_argv(tmp_path: Path) -> None:
    gateway = build_tool_gateway(tmp_path / "home", project_dir=tmp_path)

    result = await gateway.call("shell.run", {"command": "ls -1 | head"})

    assert result.ok is False
    assert result.facts == {}
    assert "array" in result.error


@pytest.mark.asyncio
async def test_memory_record_activation_marks_recalled_items(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = MemoryStore(home)
    item = store.add_item(
        "preference",
        "prefer AST tools for Python code edits",
        source="test",
        status="active",
        confidence=0.8,
        reason="unit test",
        provenance="tests/test_core_fact_tools.py",
    )
    gateway = build_tool_gateway(home, project_dir=workspace)

    recall = await gateway.call("memory.recall", {"query": "Python code edits"})
    activated = await gateway.call(
        "memory.record_activation",
        {
            "item_ids": recall.facts["activation_candidate_ids"],
            "reason": "used by planner",
            "provenance": "test-trace",
        },
    )

    updated = store.get_item(item.id)
    assert recall.ok is True
    assert item.id in recall.facts["activation_candidate_ids"]
    assert activated.ok is True
    assert activated.facts["activated_count"] == 1
    assert updated is not None
    assert updated.metadata["recall_count"] == 1
    assert updated.metadata["activation_reason"] == "used by planner"
    assert updated.metadata["activation_provenance"] == "test-trace"


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


@pytest.mark.asyncio
async def test_python_ast_replace_symbol_replaces_one_function(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text(
        "def keep():\n    return 'keep'\n\ndef build():\n    return 'old'\n",
        encoding="utf-8",
    )
    gateway = build_tool_gateway(home, project_dir=workspace)

    result = await gateway.call(
        "python.ast.replace_symbol",
        {
            "path": "app.py",
            "symbol_name": "build",
            "symbol_type": "function",
            "replacement": "def build():\n    return 'new'\n",
        },
    )

    assert result.ok is True
    assert result.facts["state_transition"] == "ast_replaced"
    assert result.facts["symbol_name"] == "build"
    assert target.read_text(encoding="utf-8") == (
        "def keep():\n    return 'keep'\n\ndef build():\n    return 'new'\n"
    )


@pytest.mark.asyncio
async def test_python_ast_replace_symbol_rejects_invalid_patch_without_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    original = "def build():\n    return 'old'\n"
    target.write_text(original, encoding="utf-8")
    gateway = build_tool_gateway(home, project_dir=workspace)

    result = await gateway.call(
        "python.ast.replace_symbol",
        {
            "path": "app.py",
            "symbol_name": "build",
            "replacement": "def build(:\n    return 'broken'\n",
        },
    )

    assert result.ok is False
    assert result.error == "replacement is not valid Python"
    assert result.facts["state_transition"] == "blocked"
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_python_ast_replace_symbol_can_patch_shadow_then_merge(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("class Service:\n    value = 'old'\n", encoding="utf-8")
    gateway = build_tool_gateway(home, project_dir=workspace)

    shadow = await gateway.call("workspace.shadow.create", {"run_id": "run-ast"})
    patched = await gateway.call(
        "python.ast.replace_symbol",
        {
            "path": "app.py",
            "symbol_name": "Service",
            "symbol_type": "class",
            "replacement": "class Service:\n    value = 'new'\n",
            "shadow_run_id": "run-ast",
        },
    )

    assert shadow.ok is True
    assert patched.ok is True
    assert patched.facts["state_transition"] == "shadow_ast_replaced"
    assert target.read_text(encoding="utf-8") == "class Service:\n    value = 'old'\n"
    assert Path(patched.facts["shadow_path"]).read_text(encoding="utf-8") == (
        "class Service:\n    value = 'new'\n"
    )

    merged = await gateway.call("workspace.shadow.merge", {"run_id": "run-ast"})

    assert merged.ok is True
    assert merged.facts["completion_evidence"] is True
    assert target.read_text(encoding="utf-8") == "class Service:\n    value = 'new'\n"


@pytest.mark.asyncio
async def test_web_search_uses_exa_mcp_default(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_call(self, name, arguments):
        captured["endpoint"] = self.server.safe_endpoint
        captured["name"] = name
        captured["arguments"] = arguments
        return {
            "ok": True,
            "is_error": False,
            "content": [],
            "structured_content": {},
            "text": (
                "Title: Navi result\n"
                "URL: https://example.com/navi\n"
                "Published: 2026-07-13\n"
                "Author: Example\n"
                "Highlights:\nSearch result snippet"
            ),
            "truncated": False,
        }

    monkeypatch.setattr(web_search_utils.MCPClient, "call_tool", fake_call)

    result = await web_search_utils._web_search({"query": "navi smoke", "limit": 3})

    assert result.ok is True
    assert captured == {
        "endpoint": "https://mcp.exa.ai/mcp",
        "name": "web_search_exa",
        "arguments": {"query": "navi smoke", "numResults": 3},
    }
    assert result.facts["provider"] == "exa_mcp"
    assert result.facts["results"] == [
        {
            "title": "Navi result",
            "url": "https://example.com/navi",
            "snippet": "Search result snippet",
            "engine": "exa",
            "published_date": "2026-07-13",
            "author": "Example",
        }
    ]
    assert result.facts["response"] == {
        "text_length": len(
            "Title: Navi result\n"
            "URL: https://example.com/navi\n"
            "Published: 2026-07-13\n"
            "Author: Example\n"
            "Highlights:\nSearch result snippet"
        ),
        "truncated": False,
        "result_count": 1,
    }
    assert "text" not in result.facts["response"]


@pytest.mark.asyncio
async def test_web_search_bounds_exa_snippets_without_raw_response_duplication(
    monkeypatch,
) -> None:
    long_snippet = "fact " * 1000

    async def fake_call(self, name, arguments):
        del self, name, arguments
        return {
            "ok": True,
            "is_error": False,
            "content": [],
            "structured_content": {},
            "text": (
                "Title: Bounded result\n"
                "URL: https://example.com/bounded\n"
                "Published: N/A\n"
                "Author: N/A\n"
                f"Highlights:\n{long_snippet}"
            ),
            "truncated": False,
        }

    monkeypatch.setattr(web_search_utils.MCPClient, "call_tool", fake_call)

    result = await web_search_utils._web_search({"query": "bounded search"})

    assert result.ok is True
    assert len(result.facts["results"][0]["snippet"]) < len(long_snippet)
    assert result.facts["results"][0]["snippet"].endswith("[truncated]")
    assert "text" not in result.facts["response"]


@pytest.mark.asyncio
async def test_web_search_uses_configured_searxng_json_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}
    config = NaviConfig(
        search=SearchConfig(provider="searxng", searxng_url="https://search.example")
    )

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

    result = await web_search_utils._web_search(
        {"query": "navi smoke", "limit": 3, "categories": "general", "language": "en"},
        config=config,
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


@pytest.mark.asyncio
async def test_web_search_rejects_auto_provider_without_calling_any_backend(monkeypatch) -> None:
    config = NaviConfig(search=SearchConfig(provider="auto"))

    def fake_urlopen(request, timeout):
        del request, timeout
        raise AssertionError("unsupported auto mode must not call SearXNG")

    async def fake_call(self, name, arguments):
        del self, name, arguments
        raise AssertionError("unsupported auto mode must not call Exa")

    monkeypatch.setattr(web_search_utils, "urlopen", fake_urlopen)
    monkeypatch.setattr(web_search_utils.MCPClient, "call_tool", fake_call)

    result = await web_search_utils._web_search({"query": "navi smoke"}, config=config)

    assert result.ok is False
    assert result.error == "unsupported web search provider: auto"
    assert result.facts["provider"] == "auto"
    assert result.facts["retryable"] is False


@pytest.mark.asyncio
async def test_web_search_explicit_searxng_failure_does_not_use_exa(monkeypatch) -> None:
    config = NaviConfig(
        search=SearchConfig(provider="searxng", searxng_url="https://search.example")
    )

    def fake_urlopen(request, timeout):
        del request, timeout
        raise TimeoutError("timed out")

    async def fail_call(self, name, arguments):
        del self, name, arguments
        raise AssertionError("explicit SearXNG mode must not call Exa")

    monkeypatch.setattr(web_search_utils, "urlopen", fake_urlopen)
    monkeypatch.setattr(web_search_utils.MCPClient, "call_tool", fail_call)

    result = await web_search_utils._web_search({"query": "navi smoke"}, config=config)

    assert result.ok is False
    assert result.facts["provider"] == "searxng"
    assert result.facts["error_reason"] == "search_timeout"
    assert result.facts["retryable"] is True
    assert result.error == "SearXNG search request timed out"
    assert "provider_errors" not in result.facts


@pytest.mark.asyncio
async def test_web_search_explicit_searxng_without_endpoint_is_config_error(monkeypatch) -> None:
    config = NaviConfig(search=SearchConfig(provider="searxng"))

    result = await web_search_utils._web_search({"query": "navi smoke"}, config=config)

    assert result.ok is False
    assert result.facts["provider"] == "searxng"
    assert result.facts["error_reason"] == "search_provider_config_error"
    assert result.facts["retryable"] is False
    assert "search.searxng_url" in result.error


@pytest.mark.asyncio
async def test_web_search_exa_tool_error_is_not_retryable(monkeypatch) -> None:
    async def fake_call(self, name, arguments):
        del self, name, arguments
        return {
            "ok": False,
            "is_error": True,
            "content": [],
            "structured_content": {},
            "text": "rate limited",
            "truncated": False,
        }

    monkeypatch.setattr(web_search_utils.MCPClient, "call_tool", fake_call)

    result = await web_search_utils._web_search({"query": "navi smoke"})

    assert result.ok is False
    assert result.facts["provider"] == "exa_mcp"
    assert result.facts["error_reason"] == "search_provider_error"
    assert result.facts["retryable"] is False
