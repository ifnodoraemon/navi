"""Service log append proactive event detector."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import logging
from pathlib import Path
from typing import Any

from ..daemon_types import (
    MAX_LOG_PROMPT_CHARS,
    MAX_LOG_READ_BYTES,
    EventBatch,
    ProjectEventContext,
    ProactiveEvent,
)
from ..safeguards import redact_secrets

logger = logging.getLogger("navi.daemon")


class ServiceLogDetector:
    """Observes bounded new content appended to opted-in project log files."""

    async def __call__(self, context: ProjectEventContext) -> EventBatch:
        return await self.detect(context)

    async def detect(self, context: ProjectEventContext) -> EventBatch:
        events: list[ProactiveEvent] = []
        state_updates: dict[str, Any] = {}
        project_path = context.project_path
        project_data = context.project_data
        watchers = project_data.get("watchers")
        if not isinstance(watchers, dict) or watchers.get("logs") is not True:
            return events, state_updates
        log_dirs = [Path(project_path), Path(project_path) / "logs", Path(project_path) / "log"]
        for log_dir in log_dirs:
            if not log_dir.exists():
                continue
            for file_path in log_dir.glob("*.log"):
                try:
                    log_rel_path = str(file_path.relative_to(project_path))
                    current_size = file_path.stat().st_size
                    log_key = f"log_size_{log_rel_path}"
                    last_size = project_data.get(log_key, 0)
                    if current_size < last_size:
                        last_size = 0
                        state_updates[log_key] = 0

                    if current_size <= last_size:
                        continue

                    read_end = min(last_size + MAX_LOG_READ_BYTES, current_size)
                    new_content, new_last_size = await asyncio.to_thread(
                        self._read_log_diff,
                        file_path,
                        last_size,
                        read_end,
                    )
                    if not new_content:
                        state_updates[log_key] = new_last_size
                        continue

                    content_fingerprint = hashlib.sha256(new_content.encode()).hexdigest()
                    fp_key = f"last_log_fp_{log_rel_path}"
                    last_fingerprint = project_data.get(fp_key, "")
                    if content_fingerprint == last_fingerprint:
                        state_updates[log_key] = new_last_size
                        continue

                    events.append(
                        ProactiveEvent(
                            facts={
                                "detector": "log_append",
                                "kind": "log_entries_appended",
                                "project_path": project_path,
                                "log_path": log_rel_path,
                                "new_entries": new_content,
                                "evidence_contract": {
                                    "scope": "appended_log_bytes",
                                    "establishes": [
                                        "log_entries_observed",
                                        "log_file_append",
                                    ],
                                    "does_not_establish": [
                                        "error_classification",
                                        "root_cause",
                                        "service_health",
                                        "task_completion",
                                    ],
                                    "sampling": "bounded_incremental_log_read",
                                },
                            },
                            state_updates={
                                log_key: new_last_size,
                                fp_key: content_fingerprint,
                            },
                        )
                    )
                except OSError as e:
                    logger.warning("Error reading log file %s: %s", file_path, e)
        return events, state_updates

    @staticmethod
    def _read_log_diff(
        file_path: Path, last_size: int, read_end: int
    ) -> tuple[str, int]:
        chunks: list[str] = []
        total_chars = 0
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending_text = ""
        with open(file_path, "rb") as f:
            f.seek(last_size)
            while f.tell() < read_end:
                line_bytes = f.readline(64_000)
                if not line_bytes:
                    break
                pending_text += decoder.decode(line_bytes, final=False)
                lines = pending_text.splitlines(keepends=True)
                pending_text = ""
                if lines and not lines[-1].endswith(("\n", "\r")):
                    pending_text = lines.pop()
                for line in lines:
                    safe_line = redact_secrets(line)
                    total_chars = ServiceLogDetector._append_log_prompt_chunk(
                        chunks, total_chars, safe_line
                    )
            new_offset = f.tell()
        pending_text += decoder.decode(b"", final=True)
        if pending_text:
            safe_pending_text = redact_secrets(pending_text)
            total_chars = ServiceLogDetector._append_log_prompt_chunk(
                chunks, total_chars, safe_pending_text
            )
        return "".join(chunks), new_offset

    @staticmethod
    def _append_log_prompt_chunk(chunks: list[str], total_chars: int, line: str) -> int:
        # Callers redact external log content before it enters
        # the prompt-bound diff facts; this helper only enforces the text bound.
        if total_chars < MAX_LOG_PROMPT_CHARS:
            chunks.append(line)
            total_chars += len(line)
        return total_chars
