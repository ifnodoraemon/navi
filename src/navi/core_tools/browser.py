"""Core tool handlers."""
from __future__ import annotations
import shutil
from pathlib import Path
from typing import Any
from ..tools import ToolResult
from .codebase import _project_path
from .paths import _is_browser_url
from .run_command import _run_command
from .utils import _positive_int

def _browser_screenshot(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    url = str(args.get("url") or "").strip()
    if not _is_browser_url(url):
        return ToolResult(tool="browser.screenshot", ok=False, error="url must be public http(s)")
    output, error = _project_path(args.get("path"), project_dir=project_dir)
    if error:
        return ToolResult(tool="browser.screenshot", ok=False, error=error)
    assert output is not None
    if output.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        return ToolResult(
            tool="browser.screenshot", ok=False, error="path must end with .png, .jpg, or .jpeg"
        )
    playwright = shutil.which("playwright")
    if not playwright:
        return ToolResult(
            tool="browser.screenshot",
            ok=False,
            error="playwright CLI not found",
            facts={
                "entity_type": "file",
                "entity_id": str(output),
                "state_transition": "failed",
                "turn_scope": "current",
                "url": url,
                "path": str(output),
            },
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    timeout = _positive_int(args.get("timeout_seconds"), default=30, maximum=120)
    result = _run_command(
        [playwright, "screenshot", url, str(output)], cwd=project_dir, timeout=timeout
    )
    ok = result["exit_code"] == 0 and output.exists()
    return ToolResult(
        tool="browser.screenshot",
        ok=ok,
        error="" if ok else result["stderr"],
        facts={
            "entity_type": "file",
            "entity_id": str(output),
            "state_transition": "written" if ok else "failed",
            "turn_scope": "current",
            **result,
            "url": url,
            "path": str(output),
            "exists": output.exists(),
            "size": output.stat().st_size if output.exists() else 0,
        },
    )


