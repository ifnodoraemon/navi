"""Shared fail-closed process sandbox construction."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def sandbox_environment() -> dict[str, str]:
    env = {
        "HOME": "/tmp/navi-home",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
        "TERM": os.environ.get("TERM", "dumb"),
        "TMPDIR": "/tmp",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }
    return {key: value for key, value in env.items() if value}


def bubblewrap_command(
    command: list[str],
    *,
    cwd: Path,
    workspace: Path,
    writable: bool,
    network_allowed: bool,
    path: str,
    host_process_visibility: bool = False,
) -> tuple[list[str], str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return [], "bubblewrap is required for process workspace isolation"
    root = workspace.expanduser().resolve()
    working_dir = cwd.expanduser().resolve()
    if working_dir != root and root not in working_dir.parents:
        return [], "process cwd must stay inside the current project workspace"

    executable = _resolve_executable(command[0], cwd=working_dir, path=path)
    if executable is None:
        return [], f"command not found: {command[0]}"

    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/tmp/navi-home",
        "--dir",
        "/etc",
        "--dir",
        "/run",
    ]
    if host_process_visibility:
        # Keep the filesystem, environment, network, and session isolated while
        # allowing declared read-only process-inspection argv (ps/pgrep/etc.) to
        # observe the host process table.  A private procfs would otherwise make
        # these commands report only the sandbox wrapper and create false facts.
        argv.extend(("--ro-bind", "/proc", "/proc"))
    else:
        argv.extend(("--proc", "/proc"))
    if not network_allowed:
        argv.append("--unshare-net")
    for source in (Path("/usr"), Path("/sys")):
        if source.exists():
            argv.extend(("--ro-bind", str(source), str(source)))
    for link, target in (
        ("/bin", "usr/bin"),
        ("/sbin", "usr/sbin"),
        ("/lib", "usr/lib"),
        ("/lib64", "usr/lib64"),
    ):
        argv.extend(("--symlink", target, link))
    for source in (
        Path("/etc/ld.so.cache"),
        Path("/etc/ssl"),
        Path("/etc/ca-certificates"),
        Path("/etc/resolv.conf"),
        Path("/etc/hosts"),
        Path("/etc/nsswitch.conf"),
        Path("/etc/passwd"),
        Path("/etc/group"),
        Path("/etc/localtime"),
    ):
        if source.exists():
            argv.extend(("--ro-bind", str(source), str(source)))

    ancestors = list(reversed(root.parents[:-1]))
    created_dirs = {Path("/tmp"), Path("/tmp/navi-home"), Path("/etc"), Path("/run")}
    for parent in ancestors:
        argv.extend(("--dir", str(parent)))
        created_dirs.add(parent)
    argv.extend(("--bind" if writable else "--ro-bind", str(root), str(root)))

    sandbox_executable = executable
    if executable != root and root not in executable.parents and not str(executable).startswith(
        "/usr/"
    ):
        runtime_prefix = Path(sys.base_prefix).resolve()
        if executable == runtime_prefix or runtime_prefix in executable.parents:
            for parent in reversed(runtime_prefix.parents[:-1]):
                if parent not in created_dirs:
                    argv.extend(("--dir", str(parent)))
                    created_dirs.add(parent)
            argv.extend(("--ro-bind", str(runtime_prefix), str(runtime_prefix)))
        else:
            argv.extend(("--dir", "/run/navi-bin"))
            sandbox_executable = Path("/run/navi-bin") / executable.name
            argv.extend(("--ro-bind", str(executable), str(sandbox_executable)))

    for key, value in sandbox_environment().items():
        argv.extend(("--setenv", key, value))
    argv.extend(("--chdir", str(working_dir), "--", str(sandbox_executable), *command[1:]))
    return argv, ""


def _resolve_executable(value: str, *, cwd: Path, path: str) -> Path | None:
    raw = Path(value).expanduser()
    if "/" in value:
        candidate = raw if raw.is_absolute() else cwd / raw
        resolved = candidate.resolve()
        return resolved if resolved.is_file() and os.access(resolved, os.X_OK) else None
    found = shutil.which(value, path=path)
    return Path(found).resolve() if found else None
