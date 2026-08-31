"""Shared fail-closed process sandbox construction."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from pathlib import Path


def sandbox_environment() -> dict[str, str]:
    env = {
        "HOME": "/tmp/navi-home",
        "PATH": "/tmp/navi-home/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
        "TERM": os.environ.get("TERM", "dumb"),
        "TMPDIR": "/tmp",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }
    return {key: value for key, value in env.items() if value}


def sandbox_environment_fd(environment: dict[str, str]) -> int | None:
    """Place explicitly selected child variables in an anonymous in-memory file."""
    if not environment:
        return None
    lines: list[str] = []
    for key, value in sorted(environment.items()):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
            raise ValueError(f"invalid sandbox environment variable: {key}")
        text = str(value)
        if "\x00" in text:
            raise ValueError(f"sandbox environment variable contains NUL: {key}")
        lines.append(f"export {key}={shlex.quote(text)}\n")
    descriptor = os.memfd_create("navi-sandbox-environment", flags=os.MFD_CLOEXEC)
    try:
        os.write(descriptor, "".join(lines).encode("utf-8"))
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def bubblewrap_command(
    command: list[str],
    *,
    cwd: Path,
    workspace: Path,
    writable: bool,
    network_allowed: bool,
    path: str,
    host_process_visibility: bool = False,
    environment_fd: int | None = None,
    sandbox_home: Path | None = None,
    read_only_binds: list[tuple[Path, Path]] | None = None,
) -> tuple[list[str], str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return [], "bubblewrap is required for process workspace isolation"
    root = workspace.expanduser().resolve()
    working_dir = cwd.expanduser().resolve()
    if working_dir != root and root not in working_dir.parents:
        return [], "process cwd must stay inside the current project workspace"

    executable = _resolve_executable(command[0], cwd=working_dir, path=path)
    in_sandbox_executable = None
    persistent_home = (
        sandbox_home.expanduser().resolve() if sandbox_home is not None else None
    )
    if executable is None and persistent_home is not None:
        # The binary may live only inside the persistent sandbox HOME (pipx/pip
        # --user installs).  On the host it is a symlink into /tmp/navi-home
        # that does not resolve, so run it at its in-sandbox path instead.
        in_sandbox_executable = _resolve_in_sandbox_executable(command[0], persistent_home)
        if in_sandbox_executable is None:
            return [], f"command not found: {command[0]}"
    elif executable is None:
        return [], f"command not found: {command[0]}"

    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--unshare-pid",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/etc",
        "--dir",
        "/run",
    ]
    if persistent_home is not None:
        # A persistent writable HOME lets pip/pipx installs survive across
        # separate shell.run invocations while staying inside the project's
        # .navi/sandbox-home (never the real ~).  It must be mounted after the
        # /tmp tmpfs so it overlays /tmp/navi-home.
        persistent_home.mkdir(parents=True, exist_ok=True)
        argv.extend(("--dir", "/tmp/navi-home"))
        argv.extend(("--bind", str(persistent_home), "/tmp/navi-home"))
    else:
        argv.extend(("--dir", "/tmp/navi-home"))
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

    for source, destination in read_only_binds or []:
        # Read-only overlays such as inbound connector media live outside the
        # mounted workspace root; expose them at their advertised paths so a
        # command referencing the real path sees the same bytes.
        bind_source = source.expanduser().resolve()
        bind_destination = destination.expanduser().resolve()
        if not bind_source.is_dir() or bind_source == root or bind_destination == root:
            continue
        if root in bind_source.parents:
            # Already visible through the workspace mount itself.
            continue
        if root in bind_destination.parents and not writable:
            # The mountpoint cannot be created inside a read-only workspace
            # bind; the caller's real-path bind still covers the media.
            continue
        for parent in reversed(bind_destination.parents[:-1]):
            if parent == root or parent in created_dirs:
                continue
            argv.extend(("--dir", str(parent)))
            created_dirs.add(parent)
        argv.extend(("--ro-bind", str(bind_source), str(bind_destination)))

    sandbox_executable = in_sandbox_executable or executable
    if in_sandbox_executable is not None:
        # The persistent sandbox HOME is already bind-mounted (writable) at
        # /tmp/navi-home, where this executable's symlinks and venv interpreter
        # resolve.  Nothing extra needs binding.
        pass
    elif (
        executable is not None
        and executable != root
        and root not in executable.parents
        and not str(executable).startswith("/usr/")
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
    if environment_fd is not None:
        environment_path = "/run/navi-command-environment"
        argv.extend(("--file", str(environment_fd), environment_path))
        argv.extend(
            (
                "--chdir",
                str(working_dir),
                "--",
                "/bin/sh",
                "-c",
                f'. {environment_path}\nexec "$@"',
                "navi-sandbox-environment",
                str(sandbox_executable),
                *command[1:],
            )
        )
    else:
        argv.extend(
            ("--chdir", str(working_dir), "--", str(sandbox_executable), *command[1:])
        )
    return argv, ""


def _resolve_executable(value: str, *, cwd: Path, path: str) -> Path | None:
    raw = Path(value).expanduser()
    if "/" in value:
        candidate = raw if raw.is_absolute() else cwd / raw
        resolved = candidate.resolve()
        return resolved if resolved.is_file() and os.access(resolved, os.X_OK) else None
    found = shutil.which(value, path=path)
    return Path(found).resolve() if found else None


def _resolve_in_sandbox_executable(value: str, persistent_home: Path) -> Path | None:
    """Locate a binary inside the persistent sandbox HOME.

    pipx/pip --user installs place a symlink at ``$HOME/.local/bin/<name>``
    pointing into ``/tmp/navi-home/...`` inside the sandbox; on the host that
    target does not exist, so the normal resolver rejects it.  Here we check the
    host-side copy of the persistent home and return the in-sandbox path (under
    /tmp/navi-home) that bubblewrap executes after mounting the home.
    """
    host_bin = persistent_home / ".local" / "bin" / value
    if not (host_bin.exists() or host_bin.is_symlink()):
        return None
    return Path("/tmp/navi-home") / ".local" / "bin" / value
