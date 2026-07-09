from __future__ import annotations

from pathlib import Path

import pytest

from navi.daemon import SystemDaemon
from navi.detectors import ServiceLogDetector
from navi.graph import GraphStore
from navi.memory.store import MemoryStore
from navi.trace import TraceStore


def test_read_log_diff_redacts_secrets_in_diff_and_error_lines(tmp_path: Path) -> None:
    """Principle 13/16: external log content is untrusted and may contain secrets.
    Both the prompt-bound diff and the matched error-line facts must be redacted
    before they can reach the agent."""
    log = tmp_path / "service.log"
    body = (
        "info: starting up with api_key=sk-supersecretvalue123\n"
        "FATAL: auth failed using Bearer abcDEF123tokenvalue\n"
    )
    log.write_text(body, encoding="utf-8")

    diff, error_lines, offset = ServiceLogDetector._read_log_diff(
        log, last_size=0, read_end=len(body.encode("utf-8"))
    )

    # No raw secret survives in either the diff body or the error facts.
    assert "sk-supersecretvalue123" not in diff
    assert "abcDEF123tokenvalue" not in diff
    assert "[REDACTED]" in diff

    # The FATAL line is collected as an error fact and is also redacted.
    assert any("FATAL" in line for line in error_lines)
    assert all("abcDEF123tokenvalue" not in line for line in error_lines)
    assert offset == len(body.encode("utf-8"))


def test_read_log_diff_without_secrets_is_unchanged(tmp_path: Path) -> None:
    log = tmp_path / "clean.log"
    body = "info: request handled in 12ms\ninfo: cache warm\n"
    log.write_text(body, encoding="utf-8")

    diff, error_lines, _ = ServiceLogDetector._read_log_diff(
        log, last_size=0, read_end=len(body.encode("utf-8"))
    )

    assert diff == body
    assert error_lines == []


def test_daemon_mutation_trace_is_evaluated(tmp_path: Path) -> None:
    daemon = SystemDaemon(tmp_path, project_dir=tmp_path)

    trace_id = daemon._record_project_graph_mutation(
        str(tmp_path),
        {"path": str(tmp_path), "last_git_status_hash": "abc"},
    )

    evaluations = TraceStore(tmp_path).list_evaluations(trace_id)
    assert len(evaluations) == 1
    assert evaluations[0].outcome == "success"
    assert evaluations[0].failure_domain == "none"


@pytest.mark.asyncio
async def test_daemon_memory_maintenance_syncs_semantic_graph(tmp_path: Path) -> None:
    daemon = SystemDaemon(tmp_path, project_dir=tmp_path)
    item = MemoryStore(tmp_path).add_item(
        "fact",
        "semantic graph maintenance fact",
        source="test",
        status="active",
        confidence=0.8,
        reason="unit test",
        provenance="tests/test_daemon.py",
    )

    facts = await daemon.process_memory_maintenance_once()

    node = GraphStore(tmp_path).get_by_name("MemoryItem", item.id)
    assert facts["ok"] is True
    assert facts["semantic_graph"]["synced_count"] == 1
    assert node is not None
    assert node.data["memory_id"] == item.id
