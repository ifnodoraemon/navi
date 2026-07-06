"""Service log error proactive event detector."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import logging
from pathlib import Path
from typing import Any

from ..daemon_types import (
    LOG_ERROR_KEYWORDS,
    MAX_LOG_PROMPT_CHARS,
    MAX_LOG_READ_BYTES,
    EventBatch,
    ProjectEventContext,
    ProactiveEvent,
)
from ..safeguards import redact_secrets

logger = logging.getLogger("navi.daemon")


class ServiceLogDetector:
    """Detects new exception/error lines appended to project log files."""

    async def __call__(self, context: ProjectEventContext) -> EventBatch:
        return await self.detect(context)

    async def detect(self, context: ProjectEventContext) -> EventBatch:
        events: list[ProactiveEvent] = []
        state_updates: dict[str, Any] = {}
        project_path = context.project_path
        project_data = context.project_data
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
                    new_content, error_lines, new_last_size = await asyncio.to_thread(
                        self._read_log_diff,
                        file_path,
                        last_size,
                        read_end,
                    )
                    if not error_lines:
                        state_updates[log_key] = new_last_size
                        continue

                    error_fingerprint = hashlib.sha256(
                        "\n".join(error_lines).encode()
                    ).hexdigest()
                    fp_key = f"last_err_fp_{log_rel_path}"
                    last_fingerprint = project_data.get(fp_key, "")
                    if error_fingerprint == last_fingerprint:
                        state_updates[log_key] = new_last_size
                        continue

                    events.append(
                        ProactiveEvent(
                            source="event_log",
                            message=f"Exception detected in log {log_rel_path}.",
                            facts={
                                "kind": "log_error_detected",
                                "project_path": project_path,
                                "log_path": log_rel_path,
                                "new_entries": new_content,
                                "matched_error_lines": error_lines,
                            },
                            state_updates={
                                log_key: new_last_size,
                                fp_key: error_fingerprint,
                            },
                            suppressed_state_updates={
                                log_key: new_last_size,
                                fp_key: error_fingerprint,
                            },
                        )
                    )
                except OSError as e:
                    logger.warning("Error reading log file %s: %s", file_path, e)
        return events, state_updates

    @staticmethod
    def _read_log_diff(
        file_path: Path, last_size: int, read_end: int
    ) -> tuple[str, list[str], int]:
        chunks: list[str] = []
        error_lines: list[str] = []
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
                    total_chars = ServiceLogDetector._append_log_prompt_chunk(
                        chunks, total_chars, line
                    )
                    if any(keyword in line.lower() for keyword in LOG_ERROR_KEYWORDS):
                        error_lines.append(redact_secrets(line.strip()))
            new_offset = f.tell()
        pending_text += decoder.decode(b"", final=True)
        if pending_text:
            total_chars = ServiceLogDetector._append_log_prompt_chunk(
                chunks, total_chars, pending_text
            )
            if any(keyword in pending_text.lower() for keyword in LOG_ERROR_KEYWORDS):
                error_lines.append(redact_secrets(pending_text.strip()))
        return "".join(chunks), error_lines, new_offset

    @staticmethod
    def _append_log_prompt_chunk(chunks: list[str], total_chars: int, line: str) -> int:
        # Principle 13/16: external log content is untrusted and may contain
        # secrets; redact before it enters the prompt-bound diff or error facts.
        if total_chars < MAX_LOG_PROMPT_CHARS:
            chunks.append(redact_secrets(line))
            total_chars += len(line)
        return total_chars
