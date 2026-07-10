"""Core tool handlers."""
from __future__ import annotations
import os
import subprocess
from pathlib import Path
from typing import Any
from .codebase import _resolve_binary_error
from .utils import _truncate_output


def _timeout_output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _run_command(
    command: list[str], *, cwd: Path, timeout: int, allocate_pty: bool = False
) -> dict[str, Any]:
    command = _normalize_argv(command)
    env = os.environ.copy()
    # Ensure common bin paths are in PATH
    home_dir = str(Path.home())
    current_path = env.get("PATH", "")
    import glob

    nvm_paths = glob.glob(f"{home_dir}/.nvm/versions/node/*/bin")
    extra_paths = [
        f"{home_dir}/.local/bin",
        f"{home_dir}/bin",
        *nvm_paths,
    ]
    env["PATH"] = os.pathsep.join(extra_paths + [current_path])
    binary_error = _resolve_binary_error(command, path=env["PATH"])
    if binary_error:
        return {
            "stdout": "",
            "stderr": binary_error,
            "exit_code": 127,
            "timed_out": False,
            "error_reason": "binary_not_found",
            "binary": command[0],
        }

    if allocate_pty:
        import pty
        import select
        import time

        master_fd, slave_fd = pty.openpty()
        try:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                text=False,
            )
            os.close(slave_fd)

            output = b""
            start_time = time.time()
            timed_out = False

            while proc.poll() is None:
                time_left = timeout - (time.time() - start_time)
                if time_left <= 0:
                    proc.kill()
                    timed_out = True
                    break

                r, _, _ = select.select([master_fd], [], [], min(0.1, time_left))
                if r:
                    try:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        output += data
                    except OSError:
                        break

            while True:
                r, _, _ = select.select([master_fd], [], [], 0)
                if r:
                    try:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        output += data
                    except OSError:
                        break
                else:
                    break

            if not timed_out:
                proc.wait(timeout=1)
            os.close(master_fd)

            text_output = output.decode("utf-8", errors="replace")

            if timed_out:
                return {
                    "stdout": _truncate_output(text_output),
                    "stderr": _truncate_output(f"command timed out after {timeout} seconds"),
                    "exit_code": 124,
                    "timed_out": True,
                }

            return {
                "stdout": _truncate_output(text_output),
                "stderr": "",
                "exit_code": proc.returncode,
                "timed_out": False,
            }
        except OSError as exc:
            try:
                os.close(master_fd)
            except OSError:
                pass
            return {"stdout": "", "stderr": str(exc), "exit_code": 127, "timed_out": False}
        except Exception:
            try:
                os.close(master_fd)
            except OSError:
                pass
            raise

    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "stdout": _truncate_output(_timeout_output_text(exc.stdout)),
            "stderr": _truncate_output(
                _timeout_output_text(exc.stderr) or f"command timed out after {timeout} seconds"
            ),
            "exit_code": 124,
            "timed_out": True,
        }
    except OSError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": 127, "timed_out": False}
    return {
        "stdout": _truncate_output(result.stdout),
        "stderr": _truncate_output(result.stderr),
        "exit_code": result.returncode,
        "timed_out": False,
    }


def _normalize_argv(command: list[str]) -> list[str]:
    replacements = {
        r"\(": "(",
        r"\)": ")",
    }
    return [replacements.get(arg, arg) for arg in command]


def _run_git(path: Path, *args: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except OSError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": 127}
    return {
        "stdout": result.stdout,
        "stderr": result.stderr.strip(),
        "exit_code": result.returncode,
    }
