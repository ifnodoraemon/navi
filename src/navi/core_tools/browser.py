"""Core tool handlers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..tools import ToolAvailability, ToolResult
from .codebase import _project_path
from .run_command import _run_command
from .utils import _positive_int


PLAYWRIGHT_CACHE_ROOTS: tuple[Path, ...] = (
    Path.home() / ".cache" / "ms-playwright",
    Path.home() / ".cache" / "ms-playwright-go",
)


def browser_screenshot_availability() -> ToolAvailability:
    playwright = shutil.which("playwright")
    if not playwright:
        return ToolAvailability(
            available=False,
            reason_code="missing_runtime_dependency",
            detail="playwright CLI not found",
            requirements=("executable:playwright", "playwright-browser:chromium"),
        )
    chromium = playwright_browser_executable(PLAYWRIGHT_CACHE_ROOTS)
    if chromium is None:
        return ToolAvailability(
            available=False,
            reason_code="missing_runtime_dependency",
            detail="playwright Chromium browser cache not found",
            requirements=("executable:playwright", "playwright-browser:chromium"),
        )
    return ToolAvailability(
        available=True,
        detail="browser screenshot runtime is available",
        requirements=("executable:playwright", "playwright-browser:chromium"),
    )


_PLAYWRIGHT_EXECUTABLE_CACHE: dict[tuple[Path, ...], Path] = {}


def playwright_browser_executable(cache_roots: tuple[Path, ...]) -> Path | None:
    cached = _PLAYWRIGHT_EXECUTABLE_CACHE.get(cache_roots)
    if cached is not None and cached.is_file():
        return cached
    executable_names = {
        "chrome",
        "chrome-headless-shell",
        "chromium",
        "headless_shell",
    }
    for root in cache_roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if (
                path.name in executable_names
                and path.is_file()
                and os.access(path, os.X_OK)
            ):
                _PLAYWRIGHT_EXECUTABLE_CACHE[cache_roots] = path
                return path
    return None


def _browser_screenshot(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    url = str(args.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ToolResult(tool="browser.screenshot", ok=False, error="url must be http(s)")
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
        [playwright, "screenshot", url, str(output)],
        cwd=project_dir,
        timeout=timeout,
        sandbox_workspace=project_dir,
        workspace_writable=True,
        network_allowed=True,
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
