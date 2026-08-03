from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from navi.config import NaviConfig, SearchConfig, SearchProviderConfig
from navi.core_tools import web_search as web_search_utils
from navi.harness import Harness
from navi.loop_contracts import LockMode, MergeStatus
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
    assert result.facts["cache"]["kind"] == "derived_cache"
    assert result.facts["cache"]["refreshed"] is True
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
    assert result.facts == {
        "error_reason": "invalid_arguments",
        "retryable": False,
    }
    assert "array" in result.error


@pytest.mark.asyncio
async def test_shell_process_inspection_observes_host_process_table_read_only(
    tmp_path: Path,
) -> None:
    observed = subprocess.Popen(["sleep", "30"])
    try:
        gateway = build_tool_gateway(tmp_path / "home", project_dir=tmp_path)
        result = await gateway.call(
            "shell.run",
            {"command": ["ps", "-eo", "pid=,comm="]},
        )
    finally:
        observed.terminate()
        observed.wait(timeout=5)

    assert result.ok is True
    assert result.facts["required_permission"] == "read"
    assert result.facts["observation_scope"] == "host_process_table"
    assert str(observed.pid) in result.facts["stdout"]
    assert result.facts["evidence_contract"] == {
        "scope": "host_process_table",
        "establishes": ["process_presence", "sampled_process_state"],
        "does_not_establish": [
            "task_activity",
            "task_progress",
            "task_completion",
        ],
        "sampling": "single_command_execution",
    }
    assert "do not by themselves prove task progress" in result.facts[
        "observation_semantics"
    ]


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

    Harness(home=home).create_shadow_workspace(run_id="run-shadow", workspace=workspace)
    write = await gateway.call(
        "file.write",
        {
            "path": "app.py",
            "content": "agent\n",
            "shadow_run_id": "run-shadow",
        },
    )

    assert write.ok is True
    assert write.facts["state_transition"] == "shadow_written"
    assert target.read_text(encoding="utf-8") == "base\n"
    assert Path(write.facts["shadow_path"]).read_text(encoding="utf-8") == "agent\n"

    merged = Harness(home=home).merge_shadow_run("run-shadow")

    assert merged.status == MergeStatus.CLEAN
    assert target.read_text(encoding="utf-8") == "agent\n"


@pytest.mark.asyncio
async def test_shadow_merge_conflict_preserves_real_workspace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("base\n", encoding="utf-8")
    gateway = build_tool_gateway(home, project_dir=workspace)

    Harness(home=home).create_shadow_workspace(run_id="run-conflict", workspace=workspace)
    write = await gateway.call(
        "file.write",
        {
            "path": "app.py",
            "content": "agent\n",
            "shadow_run_id": "run-conflict",
        },
    )
    target.write_text("human\n", encoding="utf-8")
    merged = Harness(home=home).merge_shadow_run("run-conflict")

    assert write.ok is True
    assert merged.status == MergeStatus.CONFLICTED
    assert list(merged.conflicts) == ["app.py"]
    assert target.read_text(encoding="utf-8") == "human\n"
    artifact = Path(merged.artifact_path) / "app.py"
    assert "<<<<<<< CURRENT" in artifact.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_shadow_discard_removes_shadow_without_real_change(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("base\n", encoding="utf-8")
    gateway = build_tool_gateway(home, project_dir=workspace)

    shadow = Harness(home=home).create_shadow_workspace(
        run_id="run-discard", workspace=workspace
    )
    await gateway.call(
        "file.write",
        {
            "path": "app.py",
            "content": "agent\n",
            "shadow_run_id": "run-discard",
        },
    )
    discarded = Harness(home=home).discard_shadow_run("run-discard")

    assert discarded is True
    assert target.read_text(encoding="utf-8") == "base\n"
    assert not Path(shadow.shadow_workspace).exists()


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

    Harness(home=home).create_shadow_workspace(run_id="run-ast", workspace=workspace)
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

    assert patched.ok is True
    assert patched.facts["state_transition"] == "shadow_ast_replaced"
    assert target.read_text(encoding="utf-8") == "class Service:\n    value = 'old'\n"
    assert Path(patched.facts["shadow_path"]).read_text(encoding="utf-8") == (
        "class Service:\n    value = 'new'\n"
    )

    merged = Harness(home=home).merge_shadow_run("run-ast")

    assert merged.status == MergeStatus.CLEAN
    assert target.read_text(encoding="utf-8") == "class Service:\n    value = 'new'\n"


@pytest.mark.asyncio
async def test_web_search_uses_explicit_exa_mcp_provider(monkeypatch) -> None:
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

    result = await web_search_utils._web_search(
        {"query": "navi smoke", "provider": "exa", "limit": 3}
    )

    assert result.ok is True
    assert captured == {
        "endpoint": "https://mcp.exa.ai/mcp",
        "name": "web_search_exa",
        "arguments": {"query": "navi smoke", "numResults": 3},
    }
    assert result.facts["provider"] == "exa"
    assert result.facts["provider_kind"] == "exa_mcp"
    assert result.facts["results"] == [
        {
            "kind": "web_document",
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
        "provider_error_count": 0,
    }
    assert result.facts["evidence_contract"] == {
        "scope": "query_ranked_web_documents",
        "provider": "exa",
        "provider_kind": "exa_mcp",
        "establishes": [
            "search_result_presence",
            "source_attribution",
            "source_reported_claims",
            "document_snippets",
        ],
        "does_not_establish": [
            "claim_truth",
            "source_authority",
            "result_representativeness",
            "real_world_outcome",
        ],
        "sampling": "provider_ranked_query_results",
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

    result = await web_search_utils._web_search(
        {"query": "bounded search", "provider": "exa"}
    )

    assert result.ok is True
    assert len(result.facts["results"][0]["snippet"]) < len(long_snippet)
    assert result.facts["results"][0]["snippet"].endswith("[truncated]")
    assert "text" not in result.facts["response"]


@pytest.mark.asyncio
async def test_web_search_uses_configured_searxng_json_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}
    config = NaviConfig(
        search=SearchConfig(
            providers={
                "local": SearchProviderConfig(
                    kind="searxng",
                    endpoint="https://search.example",
                )
            }
        )
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
        {
            "query": "navi smoke",
            "provider": "local",
            "limit": 3,
            "categories": "general",
            "language": "en",
        },
        config=config,
    )

    assert result.ok is True
    assert result.facts["provider"] == "local"
    assert result.facts["provider_kind"] == "searxng"
    assert result.facts["endpoint"] == "https://search.example"
    assert result.facts["answers"] == ["direct answer"]
    assert result.facts["suggestions"] == ["navi agent"]
    assert result.facts["results"] == [
        {
            "kind": "web_document",
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
async def test_web_search_rejects_link_local_searxng_target(monkeypatch) -> None:
    config = NaviConfig(
        search=SearchConfig(
            providers={
                "local": SearchProviderConfig(
                    kind="searxng",
                    endpoint="http://metadata.invalid",
                    allow_private_network=True,
                )
            }
        )
    )
    monkeypatch.setattr(
        web_search_utils.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("169.254.169.254", 80)),
        ],
    )

    result = await web_search_utils._web_search(
        {"query": "metadata", "provider": "local"},
        config=config,
    )

    assert result.ok is False
    assert result.facts["error_reason"] == "search_target_rejected"
    assert result.facts["rejected_addresses"] == ["169.254.169.254"]


def test_web_search_safe_endpoint_removes_embedded_credentials() -> None:
    endpoint = web_search_utils._safe_http_endpoint(
        "https://user:password@search.example:8443/api?token=secret"
    )

    assert endpoint == "https://search.example:8443/api"
    assert "user" not in endpoint
    assert "password" not in endpoint


@pytest.mark.asyncio
async def test_web_search_rejects_redirect_without_following_target() -> None:
    target_hits = 0

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            nonlocal target_hits
            target_hits += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"results": []}')

        def log_message(self, *_args) -> None:
            return None

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{target.server_port}/metadata",
            )
            self.end_headers()

        def log_message(self, *_args) -> None:
            return None

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    target_thread.start()
    redirect_thread.start()
    try:
        config = NaviConfig(
            search=SearchConfig(
                providers={
                    "local": SearchProviderConfig(
                        kind="searxng",
                        endpoint=f"http://127.0.0.1:{redirect.server_port}",
                        allow_private_network=True,
                    )
                }
            )
        )
        result = await web_search_utils._web_search(
            {"query": "redirect", "provider": "local"},
            config=config,
        )
    finally:
        redirect.shutdown()
        target.shutdown()
        redirect.server_close()
        target.server_close()
        redirect_thread.join(timeout=2)
        target_thread.join(timeout=2)

    assert result.ok is False
    assert result.facts["error_reason"] == "search_provider_redirect_rejected"
    assert target_hits == 0


@pytest.mark.asyncio
async def test_web_search_rejects_auto_provider_without_calling_any_backend(monkeypatch) -> None:
    config = NaviConfig()

    def fake_urlopen(request, timeout):
        del request, timeout
        raise AssertionError("unsupported auto mode must not call SearXNG")

    async def fake_call(self, name, arguments):
        del self, name, arguments
        raise AssertionError("unsupported auto mode must not call Exa")

    monkeypatch.setattr(web_search_utils, "urlopen", fake_urlopen)
    monkeypatch.setattr(web_search_utils.MCPClient, "call_tool", fake_call)

    result = await web_search_utils._web_search(
        {"query": "navi smoke", "provider": "auto"}, config=config
    )

    assert result.ok is False
    assert result.error == "search provider is not configured: auto"
    assert result.facts["provider"] == "auto"
    assert result.facts["error_reason"] == "search_provider_not_configured"
    assert result.facts["available_providers"] == ["exa"]
    assert result.facts["retryable"] is False


@pytest.mark.asyncio
async def test_web_search_requires_model_selected_provider_without_backend_call(
    monkeypatch,
) -> None:
    def fake_urlopen(request, timeout):
        del request, timeout
        raise AssertionError("missing provider must not call an HTTP backend")

    async def fake_call(self, name, arguments):
        del self, name, arguments
        raise AssertionError("missing provider must not call an MCP backend")

    monkeypatch.setattr(web_search_utils, "urlopen", fake_urlopen)
    monkeypatch.setattr(web_search_utils.MCPClient, "call_tool", fake_call)

    result = await web_search_utils._web_search({"query": "navi smoke"})

    assert result.ok is False
    assert result.error == (
        "provider is required; the model must select one configured provider"
    )
    assert result.facts == {
        "error_reason": "missing_required_argument",
        "retryable": False,
        "provider": "",
        "missing_argument": "provider",
        "available_providers": ["exa"],
    }


@pytest.mark.asyncio
async def test_web_search_explicit_searxng_failure_does_not_use_exa(monkeypatch) -> None:
    config = NaviConfig(
        search=SearchConfig(
            providers={
                "local": SearchProviderConfig(
                    kind="searxng",
                    endpoint="https://search.example",
                )
            }
        )
    )

    def fake_urlopen(request, timeout):
        del request, timeout
        raise TimeoutError("timed out")

    async def fail_call(self, name, arguments):
        del self, name, arguments
        raise AssertionError("explicit SearXNG mode must not call Exa")

    monkeypatch.setattr(web_search_utils, "urlopen", fake_urlopen)
    monkeypatch.setattr(web_search_utils.MCPClient, "call_tool", fail_call)

    result = await web_search_utils._web_search(
        {"query": "navi smoke", "provider": "local"}, config=config
    )

    assert result.ok is False
    assert result.facts["provider"] == "local"
    assert result.facts["provider_kind"] == "searxng"
    assert result.facts["error_reason"] == "search_timeout"
    assert result.facts["retryable"] is True
    assert result.error == "SearXNG search request timed out"
    assert "provider_errors" not in result.facts


@pytest.mark.asyncio
async def test_web_search_searxng_http_error_preserves_retry_facts(monkeypatch) -> None:
    config = NaviConfig(
        search=SearchConfig(
            providers={
                "local": SearchProviderConfig(
                    kind="searxng",
                    endpoint="https://search.example",
                )
            }
        )
    )

    def fake_urlopen(request, timeout):
        del timeout
        raise HTTPError(request.full_url, 503, "unavailable", {}, None)

    monkeypatch.setattr(web_search_utils, "urlopen", fake_urlopen)

    result = await web_search_utils._web_search(
        {"query": "navi smoke", "provider": "local"}, config=config
    )

    assert result.ok is False
    assert result.error == "SearXNG returned HTTP 503"
    assert result.facts["provider"] == "local"
    assert result.facts["provider_kind"] == "searxng"
    assert result.facts["error_reason"] == "search_provider_error"
    assert result.facts["status_code"] == 503
    assert result.facts["retryable"] is True


@pytest.mark.asyncio
async def test_web_search_explicit_searxng_without_endpoint_is_config_error(monkeypatch) -> None:
    del monkeypatch
    config = NaviConfig(
        search=SearchConfig(
            providers={"local": SearchProviderConfig(kind="searxng")}
        )
    )

    result = await web_search_utils._web_search(
        {"query": "navi smoke", "provider": "local"}, config=config
    )

    assert result.ok is False
    assert result.facts["provider"] == "local"
    assert result.facts["provider_kind"] == "searxng"
    assert result.facts["error_reason"] == "search_provider_config_error"
    assert result.facts["retryable"] is False
    assert "search.providers.local.endpoint" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("results", "expected_ok", "expected_reason"),
    [
        (
            [
                {
                    "title": "Verified result",
                    "url": "https://example.com/result",
                    "content": "available",
                    "engine": "google",
                }
            ],
            True,
            "",
        ),
        ([], False, "search_provider_blocked"),
    ],
)
async def test_web_search_searxng_exposes_upstream_engine_failures(
    monkeypatch,
    results,
    expected_ok,
    expected_reason,
) -> None:
    config = NaviConfig(
        search=SearchConfig(
            providers={
                "local": SearchProviderConfig(
                    kind="searxng",
                    endpoint="https://search.example",
                )
            }
        )
    )

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self, max_bytes):
            del max_bytes
            return json.dumps(
                {
                    "results": results,
                    "unresponsive_engines": [["brave", "Too many requests"]],
                }
            ).encode()

    monkeypatch.setattr(
        web_search_utils,
        "urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = await web_search_utils._web_search(
        {"query": "navi smoke", "provider": "local"},
        config=config,
    )

    assert result.ok is expected_ok
    assert result.facts["provider_errors"] == [
        {"engine": "brave", "reason": "Too many requests"}
    ]
    if expected_ok:
        assert result.facts["response"] == {
            "result_count": 1,
            "provider_error_count": 1,
        }
    else:
        assert result.facts["error_reason"] == expected_reason
        assert result.facts["retryable"] is False


@pytest.mark.asyncio
async def test_web_search_does_not_reclassify_programming_errors_as_provider_failures(
    monkeypatch,
) -> None:
    config = NaviConfig(
        search=SearchConfig(
            providers={
                "local": SearchProviderConfig(
                    kind="searxng",
                    endpoint="https://search.example",
                )
            }
        )
    )

    def broken_urlopen(request, timeout):
        del request, timeout
        raise AssertionError("adapter bug")

    monkeypatch.setattr(web_search_utils, "urlopen", broken_urlopen)

    with pytest.raises(AssertionError, match="adapter bug"):
        await web_search_utils._web_search(
            {"query": "navi smoke", "provider": "local"},
            config=config,
        )


@pytest.mark.asyncio
async def test_web_search_x_api_normalizes_posts_without_exposing_token(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    config = NaviConfig(
        search=SearchConfig(
            providers={
                "x_live": SearchProviderConfig(
                    kind="x_api",
                    endpoint="https://api.x.com",
                    bearer_token="test-token",
                )
            }
        )
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
                    "data": [
                        {
                            "id": "123",
                            "text": "Navi search update",
                            "author_id": "42",
                            "created_at": "2026-07-30T04:00:00Z",
                            "lang": "en",
                            "conversation_id": "100",
                            "public_metrics": {"like_count": 7},
                        }
                    ],
                    "includes": {
                        "users": [
                            {
                                "id": "42",
                                "name": "OpenAI",
                                "username": "OpenAI",
                                "verified": True,
                            }
                        ]
                    },
                    "meta": {
                        "newest_id": "123",
                        "oldest_id": "123",
                        "next_token": "next",
                    },
                }
            ).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(web_search_utils, "urlopen", fake_urlopen)

    result = await web_search_utils._web_search(
        {
            "query": "from:OpenAI -is:retweet",
            "provider": "x_live",
            "limit": 3,
            "start_time": "2026-07-29T00:00:00Z",
            "sort_order": "recency",
        },
        config=config,
    )

    assert result.ok is True
    assert result.facts["provider"] == "x_live"
    assert result.facts["provider_kind"] == "x_api"
    assert result.facts["results"] == [
        {
            "kind": "social_post",
            "title": "OpenAI (@OpenAI) on X",
            "url": "https://x.com/OpenAI/status/123",
            "snippet": "Navi search update",
            "engine": "x_api",
            "source_id": "123",
            "author": {
                "id": "42",
                "name": "OpenAI",
                "username": "OpenAI",
                "verified": True,
            },
            "published_date": "2026-07-30T04:00:00Z",
            "language": "en",
            "conversation_id": "100",
            "public_metrics": {"like_count": 7},
        }
    ]
    parsed = urlparse(str(captured["url"]))
    query = parse_qs(parsed.query)
    assert parsed.path == "/2/tweets/search/recent"
    assert query["query"] == ["from:OpenAI -is:retweet"]
    assert query["max_results"] == ["10"]
    assert query["start_time"] == ["2026-07-29T00:00:00Z"]
    assert captured["authorization"] == "Bearer test-token"
    assert captured["timeout"] == 20
    assert captured["max_bytes"] == 4_000_000
    assert "test-token" not in json.dumps(result.facts)


@pytest.mark.asyncio
async def test_web_search_disabled_x_provider_does_not_call_api(monkeypatch) -> None:
    config = NaviConfig(
        search=SearchConfig(
            providers={
                "x": SearchProviderConfig(
                    kind="x_api",
                    enabled=False,
                    endpoint="https://api.x.com",
                )
            }
        )
    )

    def fake_urlopen(request, timeout):
        del request, timeout
        raise AssertionError("disabled X provider must not call the API")

    monkeypatch.setattr(web_search_utils, "urlopen", fake_urlopen)

    result = await web_search_utils._web_search(
        {"query": "navi smoke", "provider": "x"},
        config=config,
    )

    assert result.ok is False
    assert result.facts["error_reason"] == "search_provider_disabled"
    assert result.facts["retryable"] is False


@pytest.mark.asyncio
async def test_web_search_x_api_fails_closed_on_error_only_response(
    monkeypatch,
) -> None:
    config = NaviConfig(
        search=SearchConfig(
            providers={
                "x_live": SearchProviderConfig(
                    kind="x_api",
                    endpoint="https://api.x.com",
                    bearer_token="test-token",
                )
            }
        )
    )

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self, max_bytes):
            del max_bytes
            return json.dumps(
                {
                    "errors": [
                        {
                            "title": "Too Many Requests",
                            "detail": "Rate limit exceeded",
                            "status": 429,
                        }
                    ]
                }
            ).encode()

    monkeypatch.setattr(
        web_search_utils,
        "urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = await web_search_utils._web_search(
        {"query": "navi smoke", "provider": "x_live"},
        config=config,
    )

    assert result.ok is False
    assert result.facts["error_reason"] == "search_provider_blocked"
    assert result.facts["retryable"] is True
    assert result.facts["provider_errors"][0]["status"] == 429


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

    result = await web_search_utils._web_search(
        {"query": "navi smoke", "provider": "exa"}
    )

    assert result.ok is False
    assert result.facts["provider"] == "exa"
    assert result.facts["provider_kind"] == "exa_mcp"
    assert result.facts["error_reason"] == "search_provider_error"
    assert result.facts["retryable"] is False


@pytest.mark.asyncio
async def test_web_search_exa_preserves_nested_http_retry_facts(monkeypatch) -> None:
    request = httpx.Request("POST", "https://mcp.exa.ai/mcp")
    response = httpx.Response(429, request=request)

    async def fake_call(self, name, arguments):
        del self, name, arguments
        raise web_search_utils.MCPTransportError(
            ExceptionGroup(
                "task group failed",
                [
                    httpx.HTTPStatusError(
                        "arbitrary upstream wording",
                        request=request,
                        response=response,
                    )
                ],
            )
        )

    monkeypatch.setattr(web_search_utils.MCPClient, "call_tool", fake_call)

    result = await web_search_utils._web_search(
        {"query": "navi smoke", "provider": "exa"}
    )

    assert result.ok is False
    assert result.facts["error_reason"] == "search_provider_error"
    assert result.facts["status_code"] == 429
    assert result.facts["retryable"] is True


@pytest.mark.asyncio
async def test_web_search_exa_does_not_reclassify_programming_errors(
    monkeypatch,
) -> None:
    async def broken_call(self, name, arguments):
        del self, name, arguments
        raise AssertionError("exa adapter bug")

    monkeypatch.setattr(web_search_utils.MCPClient, "call_tool", broken_call)

    with pytest.raises(AssertionError, match="exa adapter bug"):
        await web_search_utils._web_search(
            {"query": "navi smoke", "provider": "exa"}
        )
