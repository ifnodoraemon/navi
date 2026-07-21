"""Core tool handlers."""
from __future__ import annotations
import ast
from pathlib import Path
import textwrap
from typing import Any
from ..harness import Harness
from ..loop_contracts import LockMode, MergeStatus
from ..tools import ToolResult
from .codebase import _project_path
from .utils import _positive_int


def _file_read(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    path, error = _project_path(args.get("path"), project_dir=project_dir)
    if error:
        return ToolResult(tool="file.read", ok=False, error=error)
    assert path is not None
    limit = _positive_int(args.get("max_bytes"), default=200000, maximum=1000000)
    facts = {"path": str(path), "max_bytes": limit, "truncated": False, "content": ""}
    if not path.exists():
        return ToolResult(tool="file.read", ok=False, facts=facts, error="path not found")
    if not path.is_file():
        return ToolResult(tool="file.read", ok=False, facts=facts, error="path is not a file")
    try:
        data = path.read_bytes()
    except OSError as exc:
        return ToolResult(tool="file.read", ok=False, facts=facts, error=str(exc))
    truncated = len(data) > limit
    chunk = data[:limit]
    facts.update(
        {
            "size": len(data),
            "truncated": truncated,
            "content": chunk.decode("utf-8", errors="replace"),
        }
    )
    return ToolResult(tool="file.read", ok=True, facts=facts)


def _file_write(args: dict[str, Any], *, project_dir: Path, home: Path | None = None) -> ToolResult:
    path, error = _project_path(args.get("path"), project_dir=project_dir)
    if error:
        return ToolResult(tool="file.write", ok=False, error=error)
    assert path is not None
    content = str(args.get("content") or "")
    mode = str(args.get("mode") or "overwrite").strip().lower()
    shadow_run_id = str(args.get("shadow_run_id") or "").strip()
    if mode not in {"overwrite", "append"}:
        return ToolResult(tool="file.write", ok=False, error="mode must be overwrite or append")
    if not shadow_run_id and path.exists() and path.is_dir():
        return ToolResult(tool="file.write", ok=False, error="path is a directory")
    parent = path.parent
    if not shadow_run_id and not parent.exists():
        if bool(args.get("create_dirs")):
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return ToolResult(tool="file.write", ok=False, error=str(exc))
        else:
            return ToolResult(tool="file.write", ok=False, error="parent directory does not exist")
    # Gap G: snapshot before high-risk overwrite so the engine can
    # backtrack. Appends are additive and low-risk, so they skip the
    # snapshot to avoid stash churn.
    checkpoint_id: str | None = None
    if not shadow_run_id and mode == "overwrite" and bool(args.get("checkpoint")):
        checkpoint_id = _checkpoint_store(project_dir).snapshot(
            path=path,
            reason=str(args.get("checkpoint_reason") or "file.write overwrite"),
        )
    lock = None
    harness = Harness(home=home) if home is not None else None
    target_path = path
    shadow_path = ""
    if shadow_run_id:
        if harness is None:
            return ToolResult(tool="file.write", ok=False, error="shadow writes require home")
        shadow = harness.get_shadow_workspace(shadow_run_id)
        if shadow is None or shadow.status != "active":
            return ToolResult(tool="file.write", ok=False, error="active shadow workspace not found")
        rel_path = _lock_resource(path, project_dir=project_dir)
        target_path = Path(shadow.shadow_workspace) / rel_path
        shadow_path = str(target_path)
        parent = target_path.parent
        if not parent.exists():
            if bool(args.get("create_dirs")):
                try:
                    parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    return ToolResult(tool="file.write", ok=False, error=str(exc))
            else:
                return ToolResult(tool="file.write", ok=False, error="parent directory does not exist")
        if target_path.exists() and target_path.is_dir():
            return ToolResult(tool="file.write", ok=False, error="path is a directory")
    if harness is not None:
        lock = harness.acquire_workspace_lock(
            owner_run_id=shadow_run_id or str(args.get("run_id") or "tool:file.write"),
            resource=_lock_resource(path, project_dir=project_dir),
            mode=LockMode.WRITE,
        )
        if not lock.acquired:
            return ToolResult(
                tool="file.write",
                ok=False,
                facts={
                    "entity_type": "file",
                    "entity_id": str(path),
                    "state_transition": "blocked",
                    "turn_scope": "current",
                    "path": str(path),
                    "workspace_lock": lock.to_dict(),
                },
                error="workspace lock conflict",
            )
    try:
        try:
            before_size = target_path.stat().st_size if target_path.exists() else 0
            if mode == "append":
                with target_path.open("a", encoding="utf-8") as handle:
                    handle.write(content)
            else:
                target_path.write_text(content, encoding="utf-8")
            after_size = target_path.stat().st_size
        except OSError as exc:
            return ToolResult(tool="file.write", ok=False, error=str(exc))
        return ToolResult(
            tool="file.write",
            ok=True,
            facts={
                "entity_type": "file",
                "entity_id": str(path),
                "state_transition": _write_transition(mode, shadow=bool(shadow_run_id)),
                "turn_scope": "current",
                "path": str(path),
                "shadow_path": shadow_path,
                "shadow_run_id": shadow_run_id,
                "mode": mode,
                "bytes_written": len(content.encode("utf-8")),
                "before_size": before_size,
                "after_size": after_size,
                "checkpoint_id": checkpoint_id or "",
                "workspace_lock": lock.to_dict() if lock is not None else {},
            },
        )
    finally:
        if harness is not None and lock is not None and lock.acquired:
            harness.release_workspace_locks(
                owner_run_id=shadow_run_id or str(args.get("run_id") or "tool:file.write"),
                resource=_lock_resource(path, project_dir=project_dir),
            )


def _python_ast_replace_symbol(
    args: dict[str, Any], *, project_dir: Path, home: Path | None = None
) -> ToolResult:
    path, error = _project_path(args.get("path"), project_dir=project_dir)
    if error:
        return ToolResult(tool="python.ast.replace_symbol", ok=False, error=error)
    assert path is not None
    symbol_name = str(args.get("symbol_name") or "").strip()
    symbol_type = str(args.get("symbol_type") or "any").strip().lower()
    replacement = str(args.get("replacement") or "")
    shadow_run_id = str(args.get("shadow_run_id") or "").strip()
    if not symbol_name:
        return ToolResult(
            tool="python.ast.replace_symbol",
            ok=False,
            error="symbol_name is required",
        )
    if symbol_type not in {"any", "function", "class"}:
        return ToolResult(
            tool="python.ast.replace_symbol",
            ok=False,
            error="symbol_type must be any, function, or class",
        )
    if path.suffix != ".py":
        return ToolResult(
            tool="python.ast.replace_symbol",
            ok=False,
            error="python.ast.replace_symbol requires a .py file",
        )
    harness = Harness(home=home) if home is not None else None
    target_path = path
    shadow_path = ""
    if shadow_run_id:
        if harness is None:
            return ToolResult(
                tool="python.ast.replace_symbol",
                ok=False,
                error="shadow AST patches require home",
            )
        shadow = harness.get_shadow_workspace(shadow_run_id)
        if shadow is None or shadow.status != "active":
            return ToolResult(
                tool="python.ast.replace_symbol",
                ok=False,
                error="active shadow workspace not found",
            )
        target_path = Path(shadow.shadow_workspace) / _lock_resource(path, project_dir=project_dir)
        shadow_path = str(target_path)
    if not target_path.exists():
        return ToolResult(tool="python.ast.replace_symbol", ok=False, error="path not found")
    if not target_path.is_file():
        return ToolResult(tool="python.ast.replace_symbol", ok=False, error="path is not a file")
    try:
        source = target_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult(tool="python.ast.replace_symbol", ok=False, error=str(exc))
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return ToolResult(
            tool="python.ast.replace_symbol",
            ok=False,
            error="existing file is not valid Python",
            facts=_ast_error_facts(path, shadow_path, exc),
        )
    dedented_replacement = textwrap.dedent(replacement).strip("\n")
    if not dedented_replacement.strip():
        return ToolResult(tool="python.ast.replace_symbol", ok=False, error="replacement is required")
    try:
        replacement_tree = ast.parse(dedented_replacement + "\n")
    except SyntaxError as exc:
        return ToolResult(
            tool="python.ast.replace_symbol",
            ok=False,
            error="replacement is not valid Python",
            facts=_ast_error_facts(path, shadow_path, exc),
        )
    replacement_node = _single_replacement_node(
        replacement_tree,
        symbol_name=symbol_name,
        symbol_type=symbol_type,
    )
    if isinstance(replacement_node, str):
        return ToolResult(tool="python.ast.replace_symbol", ok=False, error=replacement_node)
    matches = [
        node
        for node in ast.walk(tree)
        if _symbol_matches(node, symbol_name=symbol_name, symbol_type=symbol_type)
    ]
    if len(matches) != 1:
        return ToolResult(
            tool="python.ast.replace_symbol",
            ok=False,
            error=f"expected exactly one matching symbol, found {len(matches)}",
            facts={
                "entity_type": "python_symbol",
                "entity_id": f"{path}:{symbol_name}",
                "state_transition": "blocked",
                "turn_scope": "current",
                "path": str(path),
                "shadow_path": shadow_path,
                "symbol_name": symbol_name,
                "symbol_type": symbol_type,
                "match_count": len(matches),
            },
        )
    target_node = matches[0]
    start_line = _symbol_start_line(target_node)
    end_line = int(getattr(target_node, "end_lineno", 0) or 0)
    if start_line <= 0 or end_line < start_line:
        return ToolResult(
            tool="python.ast.replace_symbol",
            ok=False,
            error="target symbol does not expose stable source locations",
        )
    source_lines = source.splitlines(keepends=True)
    target_line = source_lines[start_line - 1]
    indent = target_line[: len(target_line) - len(target_line.lstrip())]
    replacement_lines = _indent_replacement(dedented_replacement, indent)
    candidate = "".join(
        [
            *source_lines[: start_line - 1],
            replacement_lines,
            *source_lines[end_line:],
        ]
    )
    try:
        ast.parse(candidate)
    except SyntaxError as exc:
        return ToolResult(
            tool="python.ast.replace_symbol",
            ok=False,
            error="patched file would not be valid Python",
            facts=_ast_error_facts(path, shadow_path, exc),
        )
    lock = None
    if harness is not None:
        lock = harness.acquire_workspace_lock(
            owner_run_id=shadow_run_id
            or str(args.get("run_id") or "tool:python.ast.replace_symbol"),
            resource=_lock_resource(path, project_dir=project_dir),
            mode=LockMode.WRITE,
        )
        if not lock.acquired:
            return ToolResult(
                tool="python.ast.replace_symbol",
                ok=False,
                facts={
                    "entity_type": "python_symbol",
                    "entity_id": f"{path}:{symbol_name}",
                    "state_transition": "blocked",
                    "turn_scope": "current",
                    "path": str(path),
                    "shadow_path": shadow_path,
                    "workspace_lock": lock.to_dict(),
                },
                error="workspace lock conflict",
            )
    try:
        try:
            current_source = target_path.read_text(encoding="utf-8")
            if current_source != source:
                return ToolResult(
                    tool="python.ast.replace_symbol",
                    ok=False,
                    facts={
                        "entity_type": "python_symbol",
                        "entity_id": f"{path}:{symbol_name}",
                        "state_transition": "blocked",
                        "turn_scope": "current",
                        "path": str(path),
                        "shadow_path": shadow_path,
                        "workspace_lock": lock.to_dict() if lock is not None else {},
                    },
                    error="file changed before AST patch write",
                )
            before_size = target_path.stat().st_size
            target_path.write_text(candidate, encoding="utf-8")
            after_size = target_path.stat().st_size
        except OSError as exc:
            return ToolResult(tool="python.ast.replace_symbol", ok=False, error=str(exc))
        return ToolResult(
            tool="python.ast.replace_symbol",
            ok=True,
            facts={
                "entity_type": "python_symbol",
                "entity_id": f"{path}:{symbol_name}",
                "state_transition": "shadow_ast_replaced" if shadow_run_id else "ast_replaced",
                "turn_scope": "current",
                "path": str(path),
                "shadow_path": shadow_path,
                "shadow_run_id": shadow_run_id,
                "symbol_name": symbol_name,
                "symbol_type": _node_symbol_type(replacement_node),
                "start_line": start_line,
                "end_line": end_line,
                "before_size": before_size,
                "after_size": after_size,
                "workspace_lock": lock.to_dict() if lock is not None else {},
            },
        )
    finally:
        if harness is not None and lock is not None and lock.acquired:
            harness.release_workspace_locks(
                owner_run_id=shadow_run_id
                or str(args.get("run_id") or "tool:python.ast.replace_symbol"),
                resource=_lock_resource(path, project_dir=project_dir),
            )


def _single_replacement_node(
    tree: ast.Module, *, symbol_name: str, symbol_type: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | str:
    matches = [
        node
        for node in tree.body
        if _symbol_matches(node, symbol_name=symbol_name, symbol_type=symbol_type)
    ]
    if len(matches) != 1:
        return f"replacement must define exactly one matching top-level symbol, found {len(matches)}"
    if len(tree.body) != 1:
        return "replacement must contain only the replacement symbol"
    match = matches[0]
    if isinstance(match, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return match
    return "replacement matched an unsupported top-level symbol"


def _symbol_matches(node: ast.AST, *, symbol_name: str, symbol_type: str) -> bool:
    if symbol_type in {"any", "function"} and isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return node.name == symbol_name
    if symbol_type in {"any", "class"} and isinstance(node, ast.ClassDef):
        return node.name == symbol_name
    return False


def _node_symbol_type(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "function"
    if isinstance(node, ast.ClassDef):
        return "class"
    return "unknown"


def _symbol_start_line(node: ast.AST) -> int:
    lines = [int(getattr(node, "lineno", 0) or 0)]
    decorators = getattr(node, "decorator_list", ())
    lines.extend(int(getattr(item, "lineno", 0) or 0) for item in decorators)
    positive = [line for line in lines if line > 0]
    return min(positive) if positive else 0


def _indent_replacement(replacement: str, indent: str) -> str:
    lines = replacement.splitlines()
    return "".join(f"{indent}{line}\n" if line else "\n" for line in lines)


def _ast_error_facts(path: Path, shadow_path: str, exc: SyntaxError) -> dict[str, Any]:
    return {
        "entity_type": "python_symbol",
        "entity_id": str(path),
        "state_transition": "blocked",
        "turn_scope": "current",
        "path": str(path),
        "shadow_path": shadow_path,
        "error_type": type(exc).__name__,
        "line": exc.lineno or 0,
        "offset": exc.offset or 0,
        "message": exc.msg,
    }


def _workspace_shadow_create(args: dict[str, Any], *, project_dir: Path, home: Path) -> ToolResult:
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        return ToolResult(tool="workspace.shadow.create", ok=False, error="run_id is required")
    try:
        shadow = Harness(home=home).create_shadow_workspace(run_id=run_id, workspace=project_dir)
    except (OSError, ValueError) as exc:
        return ToolResult(tool="workspace.shadow.create", ok=False, error=str(exc))
    return ToolResult(
        tool="workspace.shadow.create",
        ok=True,
        facts={
            "entity_type": "shadow_workspace",
            "entity_id": run_id,
            "state_transition": "created",
            "turn_scope": "current",
            "run_id": run_id,
            "real_workspace": shadow.real_workspace,
            "baseline_workspace": shadow.baseline_workspace,
            "shadow_workspace": shadow.shadow_workspace,
            "baseline_fingerprint": shadow.baseline_fingerprint.digest,
        },
    )


def _workspace_shadow_merge(args: dict[str, Any], *, home: Path) -> ToolResult:
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        return ToolResult(tool="workspace.shadow.merge", ok=False, error="run_id is required")
    try:
        result = Harness(home=home).merge_shadow_run(run_id)
    except (KeyError, OSError, ValueError) as exc:
        return ToolResult(tool="workspace.shadow.merge", ok=False, error=str(exc))
    status = str(result.status)
    return ToolResult(
        tool="workspace.shadow.merge",
        ok=True,
        facts={
            "entity_type": "shadow_workspace",
            "entity_id": run_id,
            "state_transition": "conflicted" if status == str(MergeStatus.CONFLICTED) else "merged",
            "turn_scope": "current",
            "run_id": run_id,
            "merge_status": status,
            "conflicts": list(result.conflicts),
            "artifact_path": result.artifact_path,
            "completion_evidence": status in {str(MergeStatus.CLEAN), str(MergeStatus.NO_OP)},
        },
    )


def _workspace_shadow_discard(args: dict[str, Any], *, home: Path) -> ToolResult:
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        return ToolResult(tool="workspace.shadow.discard", ok=False, error="run_id is required")
    discarded = Harness(home=home).discard_shadow_run(run_id)
    return ToolResult(
        tool="workspace.shadow.discard",
        ok=discarded,
        error="" if discarded else "shadow workspace not found",
        facts={
            "entity_type": "shadow_workspace",
            "entity_id": run_id,
            "state_transition": "discarded" if discarded else "not_found",
            "turn_scope": "current",
            "run_id": run_id,
            "discarded": discarded,
        },
    )


class _CheckpointStore:
    """Minimal git-stash-backed snapshot store (Gap G).

    Before a high-risk overwrite, the engine calls ``snapshot()`` which
    runs ``git stash create`` (no branch switch, no working-tree
    destruction) and records the stash ref. ``restore()`` reapplies the
    stash to roll back to the snapshotted state.

    The store is intentionally tiny: it does not manage branching,
    commits, or reflogs. It only knows how to create and apply a stash
    ref. Metadata (stash ref, reason, timestamp) lives in a sidecar
    JSON file so it survives process restarts and does not rely on git
    reflog (which is gc'd).
    """

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir

    def snapshot(self, *, path: Path, reason: str) -> str:
        """Create a git stash checkpoint and return the checkpoint id."""
        import json
        import time
        import uuid

        from .run_command import _run_git

        cwd = path.parent if path.is_file() else path
        # ``git stash create`` returns a stash ref without touching the
        # working tree or switching branches. If there is nothing to
        # stash (clean tree), it returns empty — we still record a
        # checkpoint id so callers have a stable handle.
        stash = _run_git(cwd, "stash", "create")
        stash_ref = stash["stdout"].strip() if stash["stdout"] else ""
        checkpoint_id = uuid.uuid4().hex
        meta = {
            "checkpoint_id": checkpoint_id,
            "stash_ref": stash_ref,
            "reason": reason,
            "path": str(path),
            "created_at": time.time(),
        }
        sidecar = cwd / ".navi-checkpoints.json"
        existing: list[dict] = []
        if sidecar.exists():
            existing = json.loads(sidecar.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                raise ValueError(f"checkpoint metadata must be a list: {sidecar}")
        existing.append(meta)
        # Keep at most 50 checkpoints to avoid unbounded growth.
        existing = existing[-50:]
        sidecar.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return checkpoint_id

    def restore(self, checkpoint_id: str) -> bool:
        """Restore file contents from a checkpoint (Gap G backtrack)."""
        import json

        from .run_command import _run_git

        sidecar = self._project_dir / ".navi-checkpoints.json"
        if not sidecar.exists():
            return False
        entries = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError(f"checkpoint metadata must be a list: {sidecar}")
        meta = next(
            (e for e in entries if e.get("checkpoint_id") == checkpoint_id),
            None,
        )
        if meta is None:
            return False
        stash_ref = meta.get("stash_ref", "")
        if not stash_ref:
            return False
        # ``git stash apply`` reapplies the stashed changes without
        # dropping the stash. This restores file contents to the
        # snapshotted state.
        result = _run_git(self._project_dir, "stash", "apply", stash_ref)
        return result["exit_code"] == 0


def _checkpoint_store(project_dir: Path) -> _CheckpointStore:
    return _CheckpointStore(project_dir)


def _lock_resource(path: Path, *, project_dir: Path) -> str:
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _write_transition(mode: str, *, shadow: bool) -> str:
    if shadow:
        return "shadow_appended" if mode == "append" else "shadow_written"
    return "appended" if mode == "append" else "written"
