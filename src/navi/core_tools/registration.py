"""Core tool handlers."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from ..tools import ALL_EXECUTION_CONTEXTS, ToolRegistry, ToolSpec
from .browser import _browser_screenshot
from .codebase import _codebase_search
from .files import (
    _file_read,
    _file_write,
    _python_ast_replace_symbol,
    _workspace_shadow_create,
    _workspace_shadow_discard,
    _workspace_shadow_merge,
)
from .hooks import _hooks_list
from .context import _context_search
from .memory import _memory_conflicts, _memory_list, _memory_recall, _memory_record_activation
from .provider import _provider_config
from .shell import _shell_run
from .skills import _skills_list, _skills_view
from .tools_list import _tools_list
from .utils import _http_fetch
from .web_search import _web_search
from ..connector_delivery import register_connector_delivery_tool


def _core_tool_spec(**kwargs: Any) -> ToolSpec:
    return ToolSpec(execution_contexts=ALL_EXECUTION_CONTEXTS, **kwargs)


def _output_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties}


def _array_of_objects() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "object"}}


def register_core_tools(registry: ToolRegistry, *, home: Path) -> None:
    register_connector_delivery_tool(registry, home=home)
    registry.register(
        _core_tool_spec(
            name="provider.config",
            capability_class="provider",
            description="Return configured model provider facts without secrets.",
            input_schema={"type": "object", "properties": {}},
            output_schema=_output_schema(
                {
                    "provider": {"type": "string"},
                    "kind": {"type": "string"},
                    "model": {"type": "string"},
                    "api_base_url": {"type": "string"},
                    "has_api_key": {"type": "boolean"},
                    "fallbacks": _array_of_objects(),
                    "routes": {"type": "object"},
                    "validation_errors": {"type": "array", "items": {"type": "string"}},
                }
            ),
        ),
        lambda args: _provider_config(home),
    )
    registry.register(
        _core_tool_spec(
            name="skills.list",
            capability_class="skills",
            description="Return installed procedural skill facts.",
            input_schema={"type": "object", "properties": {}},
            output_schema=_output_schema(
                {
                    "category": {"type": "string"},
                    "definition": {"type": "string"},
                    "not_tools": {"type": "boolean"},
                    "prompt_permission_ceiling": {"type": "string"},
                    "skills": _array_of_objects(),
                    "count": {"type": "integer"},
                }
            ),
        ),
        lambda args: _skills_list(home, workspace=registry.project_dir),
    )
    registry.register(
        _core_tool_spec(
            name="skills.view",
            capability_class="skills",
            description="Return one installed skill's full instructions or a safe supporting file by skill name.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "relative_path": {"type": "string", "default": "SKILL.md"},
                    "max_bytes": {"type": "integer", "default": 50000},
                },
                "required": ["name"],
            },
            output_schema=_output_schema(
                {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "permission": {"type": "string"},
                    "injectable_with_read_ceiling": {"type": "boolean"},
                    "path": {"type": "string"},
                    "relative_path": {"type": "string"},
                    "size": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                    "content": {"type": "string"},
                }
            ),
        ),
        lambda args: _skills_view(home, args, workspace=registry.project_dir),
    )
    registry.register(
        _core_tool_spec(
            name="tools.list",
            capability_class="tools",
            description="Return callable capability facts.",
            input_schema={"type": "object", "properties": {}},
            output_schema=_output_schema(
                {
                    "category": {"type": "string"},
                    "definition": {"type": "string"},
                    "not_skills": {"type": "boolean"},
                    "tools": _array_of_objects(),
                    "count": {"type": "integer"},
                }
            ),
        ),
        lambda args: _tools_list(registry),
    )
    registry.register(
        _core_tool_spec(
            name="hooks.list",
            capability_class="hooks",
            description="Return lifecycle hook facts.",
            input_schema={"type": "object", "properties": {}},
            output_schema=_output_schema(
                {
                    "category": {"type": "string"},
                    "definition": {"type": "string"},
                    "hooks": _array_of_objects(),
                    "count": {"type": "integer"},
                }
            ),
        ),
        lambda args: _hooks_list(home),
    )
    registry.register(
        _core_tool_spec(
            name="context.search",
            capability_class="context",
            context_policy="actor_memory",
            risk_class="medium",
            sensitive_contexts=("memory",),
            confirmation_required=False,
            risk_reason_code="capability_safeguard_context_search",
            description=(
                "Return deterministic, source-attributed context evidence from the "
                "current conversation and governed memory. The model decides how to use it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "need": {"type": "array", "items": {"type": "string"}},
                    "terms": {"type": "array", "items": {"type": "string"}},
                    "time_hint": {"type": "string"},
                    "scope_hint": {"type": "string"},
                    "max_items": {"type": "integer", "default": 8},
                },
            },
            output_schema=_output_schema(
                {
                    "policy": {"type": "string"},
                    "query": {"type": "string"},
                    "terms": {"type": "array", "items": {"type": "string"}},
                    "need": {"type": "array", "items": {"type": "string"}},
                    "time_hint": {"type": "string"},
                    "scope_hint": {"type": "string"},
                    "identity": {"type": "object"},
                    "allowed_scopes": {"type": "array", "items": {"type": "string"}},
                    "evidence": _array_of_objects(),
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "count": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "selection_policy": {"type": "string"},
                    "model_decides_usage": {"type": "boolean"},
                }
            ),
        ),
        lambda args: _context_search(home, args),
    )
    registry.register(
        _core_tool_spec(
            name="memory.list",
            capability_class="memory",
            context_policy="actor_memory",
            risk_class="medium",
            sensitive_contexts=("memory",),
            confirmation_required=False,
            risk_reason_code="capability_safeguard_memory_list",
            description="Return typed memory item facts from Navi's local memory store.",
            input_schema={
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
            output_schema=_output_schema(
                {
                    "items": _array_of_objects(),
                    "count": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "type": {"type": "string"},
                    "status": {"type": "string"},
                }
            ),
        ),
        lambda args: _memory_list(home, args),
    )
    registry.register(
        _core_tool_spec(
            name="memory.recall",
            capability_class="memory",
            context_policy="actor_memory",
            risk_class="medium",
            sensitive_contexts=("memory",),
            confirmation_required=False,
            risk_reason_code="capability_safeguard_memory_recall",
            description="Recall goal-relevant memory facts from Navi's local memory store.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "goal": {"type": "string"},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
            output_schema=_output_schema(
                {
                    "query": {"type": "string"},
                    "goal": {"type": "string"},
                    "items": _array_of_objects(),
                    "activation_candidate_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "count": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "rendered": {"type": "string"},
                }
            ),
        ),
        lambda args: _memory_recall(home, args),
    )
    registry.register(
        _core_tool_spec(
            name="memory.record_activation",
            capability_class="memory",
            context_policy="actor_memory",
            description=(
                "Record that specific recalled memory items were used, so "
                "maintenance can distinguish active knowledge from stale memory."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                    "provenance": {"type": "string"},
                },
                "required": ["item_ids", "reason", "provenance"],
            },
            output_schema=_output_schema(
                {
                    "entity_type": {"type": "string"},
                    "entity_id": {"type": "string"},
                    "state_transition": {"type": "string"},
                    "turn_scope": {"type": "string"},
                    "activated_items": _array_of_objects(),
                    "activated_count": {"type": "integer"},
                    "missing_item_ids": {"type": "array", "items": {"type": "string"}},
                    "missing_count": {"type": "integer"},
                    "reason": {"type": "string"},
                    "provenance": {"type": "string"},
                }
            ),
            facts_only=True,
            mutates=True,
            permission="write",
        ),
        lambda args: _memory_record_activation(home, args),
    )
    registry.register(
        _core_tool_spec(
            name="memory.conflicts",
            capability_class="memory",
            context_policy="actor_memory",
            risk_class="medium",
            sensitive_contexts=("memory",),
            confirmation_required=False,
            risk_reason_code="capability_safeguard_memory_conflicts",
            description="Return declared memory conflict facts from Navi's local memory store.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
            output_schema=_output_schema(
                {
                    "conflicts": _array_of_objects(),
                    "count": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "unresolved_count": {"type": "integer"},
                }
            ),
        ),
        lambda args: _memory_conflicts(home, args),
    )
    registry.register(
        _core_tool_spec(
            name="browser.screenshot",
            capability_class="browser",
            risk_class="high",
            sensitive_contexts=("browser", "untrusted_web", "artifact_write"),
            confirmation_required=True,
            risk_reason_code="capability_safeguard_browser_screenshot",
            description="Capture a browser screenshot artifact for a reachable page.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "path": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "default": 30},
                },
                "required": ["url", "path"],
            },
            output_schema=_output_schema(
                {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "timed_out": {"type": "boolean"},
                    "url": {"type": "string"},
                    "path": {"type": "string"},
                    "exists": {"type": "boolean"},
                    "size": {"type": "integer"},
                }
            ),
            facts_only=True,
            mutates=True,
            permission="write",
        ),
        lambda args: _browser_screenshot(args, project_dir=registry.project_dir),
    )
    registry.register(
        _core_tool_spec(
            name="file.read",
            capability_class="file.read",
            risk_class="medium",
            sensitive_contexts=("filesystem", "untrusted_local_content"),
            confirmation_required=False,
            risk_reason_code="capability_safeguard_file_read",
            description="Read a UTF-8 text file inside the current project workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_bytes": {"type": "integer", "default": 200000},
                },
                "required": ["path"],
            },
            output_schema=_output_schema(
                {
                    "path": {"type": "string"},
                    "max_bytes": {"type": "integer"},
                    "size": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                    "content": {"type": "string"},
                }
            ),
        ),
        lambda args: _file_read(args, project_dir=registry.project_dir),
    )
    registry.register(
        _core_tool_spec(
            name="file.write",
            capability_class="file.write",
            risk_class="high",
            sensitive_contexts=("filesystem", "local_state"),
            confirmation_required=True,
            risk_reason_code="capability_safeguard_file_write",
            description="Write UTF-8 text to a file inside the current project workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {"type": "string", "default": "overwrite"},
                    "create_dirs": {"type": "boolean", "default": False},
                    "checkpoint": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true and mode is overwrite, create a git-stash checkpoint before writing so the engine can backtrack (Gap G).",
                    },
                    "checkpoint_reason": {
                        "type": "string",
                        "default": "",
                        "description": "Optional reason recorded with the checkpoint.",
                    },
                    "shadow_run_id": {
                        "type": "string",
                        "default": "",
                        "description": "If set, write to that run's active shadow workspace instead of the real workspace.",
                    },
                },
                "required": ["path", "content"],
            },
            output_schema=_output_schema(
                {
                    "path": {"type": "string"},
                    "shadow_path": {"type": "string"},
                    "shadow_run_id": {"type": "string"},
                    "mode": {"type": "string"},
                    "bytes_written": {"type": "integer"},
                    "before_size": {"type": "integer"},
                    "after_size": {"type": "integer"},
                    "checkpoint_id": {"type": "string"},
                    "workspace_lock": {"type": "object"},
                }
            ),
            facts_only=True,
            mutates=True,
            permission="write",
            risk_policy="workspace_file_write",
        ),
        lambda args: _file_write(args, project_dir=registry.project_dir, home=home),
    )
    registry.register(
        _core_tool_spec(
            name="python.ast.replace_symbol",
            capability_class="file.write",
            description=(
                "Replace one Python function or class definition after parsing both "
                "the existing file and replacement with Python AST validation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "symbol_name": {"type": "string"},
                    "symbol_type": {
                        "type": "string",
                        "enum": ["any", "function", "class"],
                        "default": "any",
                    },
                    "replacement": {"type": "string"},
                    "shadow_run_id": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "If set, patch that run's active shadow workspace "
                            "instead of the real workspace."
                        ),
                    },
                },
                "required": ["path", "symbol_name", "replacement"],
            },
            output_schema=_output_schema(
                {
                    "entity_type": {"type": "string"},
                    "entity_id": {"type": "string"},
                    "state_transition": {"type": "string"},
                    "turn_scope": {"type": "string"},
                    "path": {"type": "string"},
                    "shadow_path": {"type": "string"},
                    "shadow_run_id": {"type": "string"},
                    "symbol_name": {"type": "string"},
                    "symbol_type": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "before_size": {"type": "integer"},
                    "after_size": {"type": "integer"},
                    "workspace_lock": {"type": "object"},
                }
            ),
            facts_only=True,
            mutates=True,
            permission="write",
        ),
        lambda args: _python_ast_replace_symbol(args, project_dir=registry.project_dir, home=home),
    )
    registry.register(
        _core_tool_spec(
            name="workspace.shadow.create",
            capability_class="workspace",
            description="Create or return a persistent shadow workspace for a run.",
            input_schema={
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
            output_schema=_output_schema(
                {
                    "entity_type": {"type": "string"},
                    "entity_id": {"type": "string"},
                    "state_transition": {"type": "string"},
                    "turn_scope": {"type": "string"},
                    "run_id": {"type": "string"},
                    "real_workspace": {"type": "string"},
                    "baseline_workspace": {"type": "string"},
                    "shadow_workspace": {"type": "string"},
                    "baseline_fingerprint": {"type": "string"},
                }
            ),
            facts_only=True,
            mutates=True,
            permission="prepare",
        ),
        lambda args: _workspace_shadow_create(args, project_dir=registry.project_dir, home=home),
    )
    registry.register(
        _core_tool_spec(
            name="workspace.shadow.merge",
            capability_class="workspace",
            description="Merge a run's shadow workspace back to the real workspace with conflict artifacts.",
            input_schema={
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
            output_schema=_output_schema(
                {
                    "entity_type": {"type": "string"},
                    "entity_id": {"type": "string"},
                    "state_transition": {"type": "string"},
                    "turn_scope": {"type": "string"},
                    "run_id": {"type": "string"},
                    "merge_status": {"type": "string"},
                    "conflicts": {"type": "array", "items": {"type": "string"}},
                    "artifact_path": {"type": "string"},
                    "completion_evidence": {"type": "boolean"},
                }
            ),
            facts_only=True,
            mutates=True,
            permission="write",
        ),
        lambda args: _workspace_shadow_merge(args, home=home),
    )
    registry.register(
        _core_tool_spec(
            name="workspace.shadow.discard",
            capability_class="workspace",
            description="Discard a run's shadow workspace without changing the real workspace.",
            input_schema={
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
            output_schema=_output_schema(
                {
                    "entity_type": {"type": "string"},
                    "entity_id": {"type": "string"},
                    "state_transition": {"type": "string"},
                    "turn_scope": {"type": "string"},
                    "run_id": {"type": "string"},
                    "discarded": {"type": "boolean"},
                }
            ),
            facts_only=True,
            mutates=True,
            permission="write",
        ),
        lambda args: _workspace_shadow_discard(args, home=home),
    )
    registry.register(
        _core_tool_spec(
            name="codebase.search",
            capability_class="codebase",
            description="Perform a fast, semantic-like search across the entire project codebase.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The concept or code to search for.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            output_schema=_output_schema({"results": _array_of_objects()}),
        ),
        lambda args: _codebase_search(args, project_dir=registry.project_dir, home=home),
    )
    registry.register(
        _core_tool_spec(
            name="shell.run",
            capability_class="shell",
            risk_class="high",
            sensitive_contexts=("terminal", "local_state"),
            confirmation_required=True,
            risk_reason_code="capability_safeguard_shell_run",
            description=(
                "Run one argv-only command in the project workspace and return bounded "
                "stdout/stderr facts. Declared read-only commands require read permission, "
                "network reads require network permission, and unknown or effectful commands "
                "fail closed to write permission and approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string", "default": str(registry.project_dir)},
                    "timeout_seconds": {
                        "type": "integer",
                        "default": 20,
                        "maximum": 600,
                    },
                    "allocate_pty": {
                        "type": "boolean",
                        "default": False,
                        "description": "Allocate a pseudo-terminal. It is required by commands that reject non-TTY stdin. When enabled, stdout may contain ANSI escape codes and stderr is merged into stdout.",
                    },
                },
                "required": ["command"],
            },
            output_schema=_output_schema(
                {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "timed_out": {"type": "boolean"},
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                    "timeout_seconds": {"type": "integer"},
                }
            ),
            facts_only=True,
            mutates=True,
            permission="read",
            permission_policy="shell_argv",
            risk_policy="shell_argv",
        ),
        lambda args: _shell_run(args, project_dir=registry.project_dir),
    )
    registry.register(
        _core_tool_spec(
            name="web.search",
            capability_class="web",
            risk_class="low",
            sensitive_contexts=("web",),
            confirmation_required=False,
            risk_reason_code="capability_safeguard_web_search",
            description=(
                "Search the web and return structured result facts. Uses configured "
                "SearXNG JSON endpoints first and the official Exa MCP search service "
                "as the default reliable fallback."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "limit": {
                        "type": "integer",
                        "description": "Maximum result count, capped at 10.",
                        "default": 5,
                    },
                    "categories": {
                        "type": "string",
                        "description": "Optional SearXNG categories parameter.",
                    },
                    "language": {
                        "type": "string",
                        "description": "Optional SearXNG language parameter.",
                    },
                    "time_range": {
                        "type": "string",
                        "description": "Optional SearXNG time_range parameter.",
                    },
                },
                "required": ["query"],
            },
            output_schema=_output_schema(
                {
                    "query": {"type": "string"},
                    "provider": {"type": "string"},
                    "endpoint": {"type": "string"},
                    "source_url": {"type": "string"},
                    "results": _array_of_objects(),
                    "answers": {"type": "array", "items": {"type": "string"}},
                    "corrections": {"type": "array", "items": {"type": "string"}},
                    "suggestions": {"type": "array", "items": {"type": "string"}},
                    "infoboxes": _array_of_objects(),
                    "provider_errors": _array_of_objects(),
                    "response": {"type": "object"},
                }
            ),
            permission="network",
        ),
        lambda args: _web_search(args, home=home),
    )
    registry.register(
        _core_tool_spec(
            name="http.fetch",
            capability_class="web",
            risk_class="medium",
            sensitive_contexts=("web", "untrusted_web"),
            confirmation_required=False,
            risk_reason_code="capability_safeguard_http_fetch",
            description="Make an HTTP request and return the response. Supports GET/POST with custom headers.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string", "default": "GET"},
                    "headers": {"type": "object", "default": {}},
                    "body": {"type": "string"},
                    "max_bytes": {"type": "integer", "default": 524288},
                },
                "required": ["url"],
            },
            output_schema=_output_schema(
                {
                    "url": {"type": "string"},
                    "method": {"type": "string"},
                    "status_code": {"type": "integer"},
                    "headers": {"type": "object"},
                    "body": {"type": "string"},
                    "truncated": {"type": "boolean"},
                }
            ),
            permission="network",
            risk_policy="http_request",
        ),
        _http_fetch,
    )
