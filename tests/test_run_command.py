from __future__ import annotations

from pathlib import Path

from navi.core_tools.codebase import _resolve_binary_error
from navi.core_tools.run_command import _run_command


def test_binary_resolution_uses_effective_execution_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "custom-tool"
    executable.write_text("#!/bin/sh\nprintf 'available'\n", encoding="utf-8")
    executable.chmod(0o755)

    assert _resolve_binary_error(["custom-tool"], path=str(bin_dir)) == ""
    assert "not found" in _resolve_binary_error(["custom-tool"], path="")


def test_pty_command_waits_for_later_output(tmp_path: Path) -> None:
    result = _run_command(
        [
            "sh",
            "-c",
            "sleep 0.2; printf ready",
        ],
        cwd=tmp_path,
        timeout=2,
        allocate_pty=True,
    )

    assert result["exit_code"] == 0
    assert "ready" in result["stdout"]
    assert result["timed_out"] is False
