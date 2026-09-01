"""Core tool handlers."""

from __future__ import annotations
from pathlib import Path
from typing import Any
from ..tools import ToolResult
from .codebase import _command_list, _project_path
from .run_command import _run_command
from .utils import _positive_int
from ..safeguards import shell_call_policy


def _connector_media_binds(
    home: Path | None, project_dir: Path
) -> list[tuple[Path, Path]] | None:
    """Bind inbound connector media read-only into the sandbox.

    Connector media lives under ``<home>/<source>/media`` (e.g. weixin file
    attachments), which is excluded from sandbox workspace shadows. Bind each
    existing media dir at its real path plus the workspace-relative path so
    shell commands can open attachments referenced in intent facts.
    """
    if home is None:
        return None
    binds: list[tuple[Path, Path]] = []
    for media_source in sorted(home.glob("*/media")):
        if not media_source.is_dir():
            continue
        resolved = media_source.resolve()
        binds.append((resolved, resolved))
        workspace_media = project_dir / ".navi" / media_source.parent.name / "media"
        if workspace_media != resolved:
            binds.append((resolved, workspace_media))
    return binds or None


def _shell_run(args: dict[str, Any], *, project_dir: Path, home: Path | None = None) -> ToolResult:
    command = _command_list(args.get("command"))
    if not command:
        return ToolResult(
            tool="shell.run", ok=False, error="command must be a non-empty string array"
        )
    cwd, error = _project_path(args.get("cwd") or str(project_dir), project_dir=project_dir)
    if error:
        return ToolResult(tool="shell.run", ok=False, error=error)
    assert cwd is not None
    if not cwd.exists() or not cwd.is_dir():
        return ToolResult(tool="shell.run", ok=False, error="cwd must be an existing directory")
    timeout = _positive_int(args.get("timeout_seconds"), default=20, maximum=600)
    allocate_pty = bool(args.get("allocate_pty"))
    shell_policy = shell_call_policy({"command": command, "allocate_pty": allocate_pty})
    requires_network = bool(shell_policy.get("requires_network"))
    result = _run_command(
        command,
        cwd=cwd,
        timeout=timeout,
        allocate_pty=allocate_pty,
        sandbox_workspace=project_dir,
        sandbox_home=None if home is None else home / "sandbox-home",
        workspace_writable=shell_policy["required_permission"] == "write",
        network_allowed=shell_policy["required_permission"] == "network" or requires_network,
        host_process_visibility=shell_policy["observation_scope"] == "host_process_table",
        read_only_binds=_connector_media_binds(home, project_dir),
    )
    observation_scope = str(shell_policy["observation_scope"])
    evidence_contract = (
        {
            "scope": "host_process_table",
            "establishes": ["process_presence", "sampled_process_state"],
            "does_not_establish": [
                "task_activity",
                "task_progress",
                "task_completion",
            ],
            "sampling": "single_command_execution",
        }
        if observation_scope == "host_process_table"
        else {
            "scope": "isolated_workspace_command",
            "establishes": ["command_result"],
            "does_not_establish": [],
            "sampling": "single_command_execution",
        }
    )
    return ToolResult(
        tool="shell.run",
        ok=result["exit_code"] == 0,
        facts={
            "entity_type": "process",
            "entity_id": " ".join(command),
            "state_transition": "executed",
            "turn_scope": "current",
            **result,
            "command": command,
            "cwd": str(cwd),
            "timeout_seconds": timeout,
            "required_permission": shell_policy["required_permission"],
            "requires_network": requires_network,
            "observation_scope": observation_scope,
            "evidence_contract": evidence_contract,
            "observation_semantics": (
                "process rows prove process presence and sampled state only; "
                "they do not by themselves prove task progress or completion"
                if observation_scope == "host_process_table"
                else "command output is scoped to the isolated workspace sandbox"
            ),
        },
        error=result["stderr"],
        error_reason=str(result.get("error_reason") or "") if result["exit_code"] != 0 else "",
    )
