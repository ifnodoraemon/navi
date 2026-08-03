from __future__ import annotations

import subprocess
import sys
import time

import pytest

from navi.harness import Harness, HarnessCommand, SecretVault
from navi.loop_contracts import LockMode, MergeStatus, TimeoutPolicy, VaultHandle
from navi.process_sandbox import bubblewrap_command, sandbox_environment


def test_harness_injects_vault_secret_without_leaking_output(tmp_path):
    handle = VaultHandle(
        uri="secret://test/api-token",
        purpose="test token",
        env_var="NAVI_TEST_SECRET",
    )
    vault = SecretVault()
    vault.put(handle, "super-secret-value")
    harness = Harness(vault=vault)

    result = harness.run_command(
        HarnessCommand(
            command=(
                sys.executable,
                "-c",
                "import os; print(os.environ['NAVI_TEST_SECRET'])",
            ),
            cwd=tmp_path,
            timeout=TimeoutPolicy(seconds=5),
            vault_handles=(handle,),
        )
    )

    assert result.ok is True
    assert "super-secret-value" not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert result.to_facts()["checker_fact"]["error_type"] == ""


def test_harness_missing_vault_handle_fails_before_execution(tmp_path):
    harness = Harness()
    handle = VaultHandle(
        uri="secret://missing/token",
        purpose="missing token",
        env_var="MISSING_TOKEN",
    )

    with pytest.raises(KeyError, match="secret handle not found"):
        harness.run_command(
            HarnessCommand(
                command=(sys.executable, "-c", "print('should not run')"),
                cwd=tmp_path,
                timeout=TimeoutPolicy(seconds=5),
                vault_handles=(handle,),
            )
        )


def test_harness_hard_timeout_returns_checker_fact(tmp_path):
    harness = Harness()

    result = harness.run_command(
        HarnessCommand(
            command=(sys.executable, "-c", "import time; time.sleep(2)"),
            cwd=tmp_path,
            timeout=TimeoutPolicy(seconds=0.1, stderr_tail_bytes=200),
        )
    )

    assert result.ok is False
    assert result.timed_out is True
    assert result.exit_code == 124
    assert result.checker_fact["error_type"] == "TimeoutError"
    assert result.checker_fact["command"].startswith(sys.executable)
    assert result.checker_fact["exit_status"] == "timed_out"


def test_harness_sandboxes_verifier_and_does_not_inherit_host_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NAVI_HOST_ONLY_SECRET", "must-not-leak")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("host data", encoding="utf-8")
    script = (
        "import os, pathlib; "
        "print(os.environ.get('NAVI_HOST_ONLY_SECRET', 'absent')); "
        f"print(pathlib.Path({str(outside)!r}).exists())"
    )

    result = Harness().run_command(
        HarnessCommand(
            command=(sys.executable, "-c", script),
            cwd=tmp_path,
            timeout=TimeoutPolicy(seconds=5),
        )
    )

    assert result.ok is True
    assert result.stdout.splitlines() == ["absent", "False"]
    assert result.to_facts()["sandboxed"] is True
    assert result.to_facts()["sandbox_backend"] == "bubblewrap"


def test_bubblewrap_constructor_clears_even_explicit_parent_environment(tmp_path) -> None:
    command, error = bubblewrap_command(
        ["printenv", "NAVI_PARENT_SECRET"],
        cwd=tmp_path,
        workspace=tmp_path,
        writable=False,
        network_allowed=False,
        path=sandbox_environment()["PATH"],
    )
    assert error == ""
    parent_env = sandbox_environment()
    parent_env["NAVI_PARENT_SECRET"] = "must-be-cleared"

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=parent_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must-be-cleared" not in result.stdout


def test_harness_timeout_kills_child_process_group(tmp_path):
    harness = Harness()

    result = harness.run_command(
        HarnessCommand(
            command=(
                sys.executable,
                "-c",
                (
                    "import subprocess, sys, time; "
                    "subprocess.Popen([sys.executable, '-c', "
                    "\"import time, pathlib; time.sleep(0.4); "
                    "pathlib.Path('child-alive.txt').write_text('alive')\"]); "
                    "time.sleep(5)"
                ),
            ),
            cwd=tmp_path,
            timeout=TimeoutPolicy(seconds=0.1),
        )
    )

    time.sleep(0.7)

    assert result.timed_out is True
    assert not (tmp_path / "child-alive.txt").exists()


def test_harness_workspace_guards_create_shadow_merge_and_lock(tmp_path):
    home = tmp_path / ".navi"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base\n", encoding="utf-8")
    harness = Harness(home=home)

    lock = harness.acquire_workspace_lock(
        owner_run_id="run-1",
        resource="app.py",
        mode=LockMode.WRITE,
        ttl_seconds=60,
    )
    blocked = harness.acquire_workspace_lock(
        owner_run_id="run-2",
        resource="app.py",
        mode=LockMode.READ,
        ttl_seconds=60,
    )
    shadow = harness.create_shadow_workspace(run_id="run-1", workspace=repo)
    (tmp_path / ".navi" / "workspace_shadows" / "run-1" / "shadow" / "app.py").write_text(
        "agent\n",
        encoding="utf-8",
    )
    merge = harness.merge_shadow_workspace(shadow)

    assert lock.acquired is True
    assert blocked.acquired is False
    assert merge.status == MergeStatus.CLEAN
    assert (repo / "app.py").read_text(encoding="utf-8") == "agent\n"
    assert harness.release_workspace_locks(owner_run_id="run-1") == 1


def test_harness_workspace_guards_require_home(tmp_path):
    harness = Harness()

    with pytest.raises(ValueError, match="workspace operations require home"):
        harness.create_shadow_workspace(run_id="run-1", workspace=tmp_path)
    with pytest.raises(ValueError, match="workspace locks require home"):
        harness.acquire_workspace_lock(owner_run_id="run-1", resource="app.py")
