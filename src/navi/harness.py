from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .loop_contracts import LockMode, MergeResult, TimeoutEvidence, TimeoutPolicy, VaultHandle
from .process_sandbox import (
    bubblewrap_command,
    sandbox_environment,
    sandbox_environment_fd,
)
from .safeguards import redact_secrets
from .workspaces import (
    LockAcquireResult,
    ShadowWorkspace,
    ShadowWorkspaceRecord,
    ShadowWorkspaceManager,
    WorkspaceLockStore,
)


@dataclass
class SecretVault:
    _secrets: dict[str, str] = field(default_factory=dict)

    def put(self, handle: VaultHandle, value: str) -> None:
        handle.validate()
        self._secrets[handle.uri] = value

    def resolve_env(self, handles: tuple[VaultHandle, ...]) -> tuple[dict[str, str], tuple[str, ...]]:
        env: dict[str, str] = {}
        secret_values: list[str] = []
        for handle in handles:
            handle.validate()
            if not handle.env_var:
                raise ValueError("VaultHandle.env_var is required for command injection")
            if handle.uri not in self._secrets:
                raise KeyError(f"secret handle not found: {handle.uri}")
            value = self._secrets[handle.uri]
            env[handle.env_var] = value
            secret_values.append(value)
        return env, tuple(secret_values)


class VaultResolver(Protocol):
    def resolve_env(
        self,
        handles: tuple[VaultHandle, ...],
    ) -> tuple[dict[str, str], tuple[str, ...]]: ...


@dataclass(frozen=True)
class HarnessCommand:
    command: tuple[str, ...]
    cwd: Path
    timeout: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    env: dict[str, str] = field(default_factory=dict)
    vault_handles: tuple[VaultHandle, ...] = ()


@dataclass(frozen=True)
class HarnessResult:
    ok: bool
    command: tuple[str, ...]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    timeout: TimeoutPolicy
    sandboxed: bool = True
    sandbox_backend: str = "bubblewrap"
    checker_fact: dict[str, Any] = field(default_factory=dict)

    def to_facts(self) -> dict[str, Any]:
        return {
            "entity_type": "process",
            "entity_id": " ".join(self.command),
            "state_transition": "executed",
            "turn_scope": "current",
            "command": list(self.command),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
            "timeout": self.timeout.to_dict(),
            "sandboxed": self.sandboxed,
            "sandbox_backend": self.sandbox_backend,
            "checker_fact": dict(self.checker_fact),
        }


class Harness:
    def __init__(self, *, home: Path | None = None, vault: VaultResolver | None = None):
        self.home = home
        resolved_vault: VaultResolver
        if vault is not None:
            resolved_vault = vault
        elif home is not None:
            from .vault import VaultStore

            resolved_vault = VaultStore(home)
        else:
            resolved_vault = SecretVault()
        self.vault = resolved_vault
        self.shadow_workspaces = ShadowWorkspaceManager(home) if home is not None else None
        self.workspace_locks = WorkspaceLockStore(home) if home is not None else None

    def run_command(self, command: HarnessCommand) -> HarnessResult:
        command.timeout.validate()
        if not command.command:
            raise ValueError("HarnessCommand.command is required")
        if not command.cwd.exists() or not command.cwd.is_dir():
            raise ValueError("HarnessCommand.cwd must be an existing directory")

        injected_env, secret_values = self.vault.resolve_env(command.vault_handles)
        resolution_env = sandbox_environment()
        resolution_env.update(command.env)
        resolution_env.update(injected_env)
        explicit_env = {**command.env, **injected_env}
        environment_fd = sandbox_environment_fd(explicit_env)
        started = time.time()
        sandbox_command, sandbox_error = bubblewrap_command(
            list(command.command),
            cwd=command.cwd,
            workspace=command.cwd,
            writable=True,
            network_allowed=False,
            path=resolution_env["PATH"],
            environment_fd=environment_fd,
        )
        if sandbox_error:
            if environment_fd is not None:
                os.close(environment_fd)
            return HarnessResult(
                ok=False,
                command=command.command,
                cwd=str(command.cwd),
                exit_code=126,
                stdout="",
                stderr=sandbox_error,
                timed_out=False,
                duration_seconds=time.time() - started,
                timeout=command.timeout,
                sandboxed=False,
                checker_fact={
                    "error_type": "SandboxUnavailable",
                    "message": sandbox_error,
                },
            )
        process: subprocess.Popen[str] | None = None
        stdout_text = ""
        stderr_text = ""
        try:
            try:
                process = subprocess.Popen(
                    sandbox_command,
                    cwd=command.cwd,
                    env=sandbox_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    pass_fds=((environment_fd,) if environment_fd is not None else ()),
                )
            finally:
                if environment_fd is not None:
                    os.close(environment_fd)
            stdout_text, stderr_text = process.communicate(timeout=command.timeout.seconds)
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                _kill_process_tree(process)
                stdout_text, stderr_text = process.communicate()
            else:
                stdout_text = _timeout_text(exc.stdout)
                stderr_text = _timeout_text(exc.stderr)
            duration = time.time() - started
            stdout = _redact(_tail(stdout_text, command.timeout.stdout_tail_bytes), secret_values)
            stderr = _redact(
                _tail(
                    stderr_text
                    or f"command timed out after {command.timeout.seconds:g} seconds",
                    command.timeout.stderr_tail_bytes,
                ),
                secret_values,
            )
            evidence = TimeoutEvidence(
                command=" ".join(command.command),
                duration_seconds=duration,
                timeout_seconds=command.timeout.seconds,
                stdout_tail=stdout,
                stderr_tail=stderr,
            )
            return HarnessResult(
                ok=False,
                command=command.command,
                cwd=str(command.cwd),
                exit_code=124,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                duration_seconds=duration,
                timeout=command.timeout,
                checker_fact=evidence.to_checker_fact(),
            )
        except OSError as exc:
            duration = time.time() - started
            stderr = _redact(str(exc), secret_values)
            return HarnessResult(
                ok=False,
                command=command.command,
                cwd=str(command.cwd),
                exit_code=127,
                stdout="",
                stderr=stderr,
                timed_out=False,
                duration_seconds=duration,
                timeout=command.timeout,
                checker_fact={"error_type": "OSError", "message": stderr},
            )

        duration = time.time() - started
        returncode = process.returncode if process is not None else 127
        stdout = _redact(_tail(stdout_text, command.timeout.stdout_tail_bytes), secret_values)
        stderr = _redact(_tail(stderr_text, command.timeout.stderr_tail_bytes), secret_values)
        return HarnessResult(
            ok=returncode == 0,
            command=command.command,
            cwd=str(command.cwd),
            exit_code=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            duration_seconds=duration,
            timeout=command.timeout,
            checker_fact={
                "error_type": "" if returncode == 0 else "CommandFailed",
                "exit_code": returncode,
            },
        )

    def create_shadow_workspace(self, *, run_id: str, workspace: Path) -> ShadowWorkspace:
        if self.shadow_workspaces is None:
            raise ValueError("Harness workspace operations require home")
        return self.shadow_workspaces.create_shadow(run_id=run_id, workspace=workspace)

    def merge_shadow_workspace(self, shadow: ShadowWorkspace) -> MergeResult:
        if self.shadow_workspaces is None:
            raise ValueError("Harness workspace operations require home")
        return self.shadow_workspaces.merge_back(shadow)

    def get_shadow_workspace(self, run_id: str) -> ShadowWorkspaceRecord | None:
        if self.shadow_workspaces is None:
            raise ValueError("Harness workspace operations require home")
        return self.shadow_workspaces.get_shadow(run_id)

    def merge_shadow_run(self, run_id: str) -> MergeResult:
        if self.shadow_workspaces is None:
            raise ValueError("Harness workspace operations require home")
        return self.shadow_workspaces.merge_run(run_id)

    def discard_shadow_run(self, run_id: str) -> bool:
        if self.shadow_workspaces is None:
            raise ValueError("Harness workspace operations require home")
        return self.shadow_workspaces.discard_run(run_id)

    def acquire_workspace_lock(
        self,
        *,
        owner_run_id: str,
        resource: str,
        mode: LockMode | str = LockMode.WRITE,
        ttl_seconds: float = 900,
    ) -> LockAcquireResult:
        if self.workspace_locks is None:
            raise ValueError("Harness workspace locks require home")
        return self.workspace_locks.acquire(
            owner_run_id=owner_run_id,
            resource=resource,
            mode=mode,
            ttl_seconds=ttl_seconds,
        )

    def release_workspace_locks(self, *, owner_run_id: str, resource: str = "") -> int:
        if self.workspace_locks is None:
            raise ValueError("Harness workspace locks require home")
        return self.workspace_locks.release(owner_run_id=owner_run_id, resource=resource)


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()


def _tail(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text[-limit:]


def _redact(text: str, secret_values: tuple[str, ...]) -> str:
    redacted = text
    for value in secret_values:
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redact_secrets(redacted)
