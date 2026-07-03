"""Git mutation proactive event detector."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from pathlib import Path

from ..daemon_types import (
    MAX_GIT_STATUS_PROMPT_CHARS,
    EventBatch,
    ProjectEventContext,
    ProactiveEvent,
)
from ..text_utils import truncate_middle

logger = logging.getLogger("navi.daemon")


class GitMutationDetector:
    """Detects filesystem mutations in a git working tree."""

    async def __call__(self, context: ProjectEventContext) -> EventBatch:
        return await self.detect(context)

    async def detect(self, context: ProjectEventContext) -> EventBatch:
        events: list[ProactiveEvent] = []
        project_path = context.project_path
        project_data = context.project_data
        git_dir = Path(project_path) / ".git"
        if not git_dir.exists():
            return events, {}
        if shutil.which("git") is None:
            logger.warning("Skipping git proactive detector because git is not on PATH")
            return events, {}

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "status",
                "--porcelain",
                cwd=project_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.warning("Git status timed out for %s", project_path)
                return events, {}
            status_text = stdout.decode(errors="replace").strip()
            if not status_text:
                return events, {}

            current_hash = hashlib.sha256(status_text.encode()).hexdigest()
            last_hash = project_data.get("last_git_status_hash", "")
            if current_hash == last_hash:
                return events, {}

            status_text = truncate_middle(status_text, MAX_GIT_STATUS_PROMPT_CHARS)
            events.append(
                ProactiveEvent(
                    source="event_git",
                    message=f"Git filesystem mutation detected in {project_path}.",
                    facts={
                        "kind": "git_status_changed",
                        "project_path": project_path,
                        "changed_files": status_text.splitlines(),
                    },
                    state_updates={"last_git_status_hash": current_hash},
                    suppressed_state_updates={"last_git_status_hash": current_hash},
                )
            )
        except (OSError, asyncio.SubprocessError) as e:
            logger.warning("Error checking git status for %s: %s", project_path, e)
        return events, {}
