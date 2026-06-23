"""Core tool handlers."""
from __future__ import annotations
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse
from typing import Any
from ..config import load_config
from ..fact_tools import service_facts, run_facts
from ..hooks import HookRegistry
from ..memory import MemoryStore
from ..operating_context import permission_allows
from ..runs import Approval
from ..safeguards import capability_safeguard_facts
from ..skills import SkillStore
from ..tools import ALL_EXECUTION_CONTEXTS, ToolRegistry, ToolResult, ToolSpec

def _service_status(args: dict[str, Any], *, default_name: str) -> ToolResult:
    name = str(args.get("name") or default_name)
    facts = service_facts(name)
    return ToolResult(
        tool="service.status", ok=facts.exit_code == 0, facts=asdict(facts), error=facts.stderr
    )


def _run_status(home: Path, args: dict[str, Any]) -> ToolResult:
    run_id = args.get("run_id")
    facts = run_facts(home, str(run_id) if run_id else None)
    return ToolResult(
        tool="delegate.status",
        ok=facts.run is not None,
        facts={
            "run": asdict(facts.run) if facts.run else None,
            "approvals": [_approval_facts(approval) for approval in facts.approvals],
            "logs": [asdict(log) for log in facts.logs],
        },
        error="" if facts.run else "delegation run not found",
    )


def _approval_facts(approval: Approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "run_id": approval.run_id,
        "action": approval.action,
        "peer_id": approval.peer_id,
        "sender_id": approval.sender_id,
        "status": approval.status,
        "expires_at": approval.expires_at,
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
        "code_present": bool(approval.code),
    }


