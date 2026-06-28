"""Tests for round-6 fixes derived from production log analysis.

Covers:
- approval.resolve idempotency (already-approved returns ok=True)
- reissue_approval dedup against existing pending approval
- governance capabilities exempt from second-level approval suspension
- FallbackProvider always wraps single-provider configs (transport retry)
- pre-flight binary validation in _run_command (missing binary facts)
- pending/prepared runs deletable from remote (stuck-run cleanup)
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.control import ApprovalService, CurrentStateBuilder, SurfaceContext, current_state_facts
from navi.core_tools import _resolve_binary_error, _run_command
from navi.provider import (
    ChatMessage,
    FallbackProvider,
    _build_fallback_chain,
)
from navi.runs import RunStore


# ---------------------------------------------------------------------------
# Fix 1: approval.resolve idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_resolve_already_approved_is_idempotent(
    tmp_path: Path,
) -> None:
    runs = RunStore(tmp_path)
    run = runs.create(
        "t",
        prompt="p",
        source="connector.weixin",
        peer_id="weixin-peer",
        sender_id="weixin-user",
        workspace=str(tmp_path),
        kind="delegation",
    )
    approval = runs.create_approval(
        run_id=run.id,
        peer_id="weixin-peer",
        sender_id="weixin-user",
    )
    # First resolution approves the run.
    service = ApprovalService(tmp_path)
    first = service.resolve(
        decision="approve",
        selection="explicit_code",
        context=SurfaceContext(
            home=tmp_path,
            source="connector.weixin",
            peer_id="weixin-peer",
            sender_id="weixin-user",
        ),
        code=approval.code,
    )
    assert first.ok is True
    assert first.facts.get("completion_evidence") is True

    # Re-submitting the same code must not fail — the planner would otherwise
    # loop on "Approval is not pending; current status is approved."
    second = service.resolve(
        decision="approve",
        selection="explicit_code",
        context=SurfaceContext(
            home=tmp_path,
            source="connector.weixin",
            peer_id="weixin-peer",
            sender_id="weixin-user",
        ),
        code=approval.code,
    )
    assert second.ok is True
    assert "do not" not in second.message.lower()
    assert "re-resolve" not in second.message.lower()
    assert second.facts.get("state_transition") == "already_resolved"
    assert second.facts.get("completion_evidence") is True


# ---------------------------------------------------------------------------
# Fix 2: reissue_approval dedup
# ---------------------------------------------------------------------------


def test_reissue_approval_returns_existing_pending_approval(
    tmp_path: Path,
) -> None:
    runs = RunStore(tmp_path)
    run = runs.create(
        "t",
        prompt="p",
        source="connector.weixin",
        peer_id="weixin-peer",
        sender_id="weixin-user",
        workspace=str(tmp_path),
        kind="delegation",
    )
    first = runs.create_approval(
        run_id=run.id,
        peer_id="weixin-peer",
        sender_id="weixin-user",
    )
    # The first pending approval exists. Re-issuing must return the SAME
    # approval (same code), not mint a new one — the observed "4-code storm"
    # happened because each re-submission minted a fresh code.
    reissued = runs.reissue_approval(
        run_id=run.id,
        peer_id="weixin-peer",
        sender_id="weixin-user",
    )
    assert reissued.code == first.code


def test_approval_resolve_accepts_visible_batch_id(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    context = SurfaceContext(
        home=tmp_path,
        source="connector.weixin",
        peer_id="weixin-peer",
        sender_id="weixin-user",
    )
    for title in ("a", "b"):
        run = runs.create(
            title,
            prompt=title,
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=str(tmp_path),
            kind="delegation",
        )
        runs.create_approval(
            run_id=run.id,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
        )

    state = CurrentStateBuilder(tmp_path).build(context)
    facts = current_state_facts(state)
    batch_id = facts["visible_approval_batches"][0]["batch_id"]

    result = ApprovalService(tmp_path).resolve(
        decision="approve",
        selection="batch_id",
        context=context,
        batch_id=batch_id,
    )

    assert result.ok is True
    assert result.facts["selection"] == "batch_id"
    assert result.facts["batch_id"] == batch_id
    assert result.facts["resolved_count"] == 2
    assert all(approval.status == "approved" for approval in runs.list_approvals(limit=10))


# ---------------------------------------------------------------------------
# Fix 3: governance capabilities exempt from second-level approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_governance_capabilities_not_suspended_in_governed_run(
    tmp_path: Path,
) -> None:
    """approval.resolve / approval.request must not trigger a second-level
    approval suspension inside a governed background run — otherwise resolving
    an approval requires another approval (infinite loop)."""
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    request_spec = registry.get("approval.request")
    resolve_spec = registry.get("approval.resolve")
    assert request_spec is not None and request_spec.governance_exempt is True
    assert resolve_spec is not None and resolve_spec.governance_exempt is True

    runs = RunStore(tmp_path)
    run = runs.create(
        "t",
        prompt="p",
        source="connector.weixin",
        peer_id="weixin-peer",
        sender_id="weixin-user",
        workspace=str(tmp_path),
        kind="delegation",
    )
    # Simulate a governed background run: any mutating capability would be
    # suspended for a second-level approval here.
    registry.governed_run_id = run.id

    context = CapabilityContext(
        home=tmp_path,
        peer_id="weixin-peer",
        sender_id="weixin-user",
        source="connector.weixin",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )

    # approval.request must NOT be suspended for a second-level approval.
    result = await registry.invoke(
        "approval.request",
        {"run_id": run.id},
        permission="write",
        context=context,
    )
    assert result.ok is True
    assert "sensitive_op_requires_approval" not in str(result.facts)


@pytest.mark.asyncio
async def test_approval_resolve_rejects_unseen_code_as_fact_only_error(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    runs = RunStore(tmp_path)
    run = runs.create(
        "t",
        prompt="p",
        source="connector.weixin",
        peer_id="weixin-peer",
        sender_id="weixin-user",
        workspace=str(tmp_path),
        kind="delegation",
    )
    approval = runs.create_approval(
        run_id=run.id,
        peer_id="weixin-peer",
        sender_id="weixin-user",
    )
    context = CapabilityContext(
        home=tmp_path,
        peer_id="weixin-peer",
        sender_id="weixin-user",
        source="connector.weixin",
        permission_ceiling="write",
        workspace=str(tmp_path),
        input_text="全部批准",
    )

    result = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=context,
    )

    assert result.ok is False
    assert "hallucinate" not in result.observation.lower()
    assert "do not" not in result.observation.lower()
    assert result.error_reason == "schema_mismatch"
    assert result.facts["reason"] == "approval_code_not_in_user_input"
    assert result.facts["selection"] == "explicit_code"
    assert result.facts["code_present_in_current_user_input"] is False
    assert json.loads(result.observation)["error_reason"] == "schema_mismatch"
    assert "approval code was not present" not in result.observation.lower()


@pytest.mark.asyncio
async def test_session_elevation_returns_state_facts_without_approval_instruction(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="weixin-peer",
        sender_id="weixin-user",
        source="connector.weixin",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )

    result = await registry.invoke(
        "session.request_elevation",
        {"target_permission": "write", "reason": "needs local edit"},
        permission="read",
        context=context,
    )

    assert result.ok is True
    assert result.facts["state_transition"] == "elevation_requested"
    assert result.facts["target_permission"] == "write"
    assert result.facts["reason"] == "needs local edit"
    assert "message" not in result.facts
    assert "please approve" not in result.observation.lower()


@pytest.mark.asyncio
async def test_capability_registry_returns_fact_only_not_found_observation(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(home=tmp_path, workspace=str(tmp_path))

    result = await registry.invoke(
        "missing.capability",
        {},
        permission="read",
        context=context,
    )

    assert result.ok is False
    assert result.error_reason == "not_found"
    assert json.loads(result.observation) == {
        "error_reason": "not_found",
        "tool": "missing.capability",
    }
    assert "capability not found" not in result.observation


@pytest.mark.asyncio
async def test_remote_policy_failure_observation_is_structured_facts(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="weixin-peer",
        sender_id="weixin-user",
        source="connector.weixin",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )

    result = await registry.invoke(
        "file.read",
        {"path": "README.md"},
        permission="read",
        context=context,
    )

    assert result.ok is False
    assert result.error_reason == "remote_capability_class_blocked"
    observation = json.loads(result.observation)
    assert observation["tool"] == "file.read"
    assert observation["capability_class"] == "file.read"
    assert observation["policy"] == "remote_connector_default"
    assert "policy blocks" not in result.observation


@pytest.mark.asyncio
async def test_capability_input_schema_failure_observation_uses_schema_reason(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        permission_ceiling="write",
        workspace=str(tmp_path),
    )

    result = await registry.invoke("shell.run", {}, permission="write", context=context)

    assert result.ok is False
    assert result.error_reason == "schema_mismatch"
    observation = json.loads(result.observation)
    assert observation["error_reason"] == "schema_mismatch"
    assert observation["tool"] == "shell.run"
    assert observation["schema_errors"] == ["$.command is required"]
    assert "error" not in observation
    assert "Invalid arguments" not in result.observation


@pytest.mark.asyncio
async def test_tool_gateway_preserves_structured_tool_error_reason(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        permission_ceiling="write",
        workspace=str(tmp_path),
    )

    result = await registry.invoke(
        "shell.run",
        {"command": ["rm", "-rf", "target"], "cwd": str(tmp_path)},
        permission="write",
        context=context,
    )

    assert result.ok is False
    assert result.error_reason == "binary_denied"
    observation = json.loads(result.observation)
    assert observation["error_reason"] == "binary_denied"
    assert observation["facts"]["error_reason"] == "binary_denied"
    assert "error" not in observation
    assert "use " not in result.observation.lower()
    assert "instead" not in result.observation.lower()


@pytest.mark.asyncio
async def test_guarded_action_failure_observation_is_structured_facts(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        permission_ceiling="write",
        workspace=str(tmp_path),
    )

    result = await registry.invoke("watch.create", {}, permission="prepare", context=context)

    assert result.ok is False
    assert result.error_reason == "schema_mismatch"
    observation = json.loads(result.observation)
    assert observation["error_reason"] == "schema_mismatch"
    assert observation["tool"] == "watch.create"
    assert observation["schema_errors"] == ["$.prompt is required"]
    assert "requires prompt" not in result.observation.lower()
    assert "input schema mismatch" in result.message.lower()


@pytest.mark.asyncio
async def test_direct_action_failure_observation_uses_failure_facts(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        permission_ceiling="write",
        workspace=str(tmp_path),
    )

    result = await registry.invoke(
        "delegate.delete",
        {"status": "failed", "reason": "cleanup"},
        permission="write",
        context=context,
    )

    assert result.ok is False
    assert result.error_reason == "scope_required"
    observation = json.loads(result.observation)
    assert observation["error_reason"] == "scope_required"
    assert observation["status"] == "failed"
    assert "requires source or kind" not in result.observation.lower()


# ---------------------------------------------------------------------------
# Fix 4: FallbackProvider always wraps single-provider configs
# ---------------------------------------------------------------------------


class _Config:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def test_build_fallback_chain_wraps_single_provider_in_fallback() -> None:
    """A single-provider config must still be wrapped in FallbackProvider so
    transport-layer failures (httpx.RemoteProtocolError, httpx.ReadError,
    httpx.ConnectError, httpx.ReadTimeout) get retried with backoff instead
    of becoming terminal planner failures."""
    from navi.config import ModelConfig

    config = ModelConfig(
        provider="openai-compatible",
        model="test-model",
        api_base_url="https://example.com",
        api_key="test-key",
        kind="openai-compatible",
        timeout_seconds=30,
    )
    chain = _build_fallback_chain(config)
    assert isinstance(chain, FallbackProvider), (
        "single-provider configs must be wrapped in FallbackProvider for "
        "transport retry"
    )


@pytest.mark.asyncio
async def test_fallback_provider_retries_on_remote_protocol_error(
    tmp_path: Path,
) -> None:
    """httpx.RemoteProtocolError ('peer closed connection without sending
    complete message body (incomplete chunked read)') must be retried by
    FallbackProvider, not surface as a terminal failure."""

    class _FailingProvider:
        def __init__(self) -> None:
            self.call_count = 0

        async def complete(
            self, messages: list[ChatMessage], *, output_schema: Any = None
        ) -> str:
            self.call_count += 1
            if self.call_count < 3:
                raise httpx.RemoteProtocolError(
                    "peer closed connection without sending complete message "
                    "body (incomplete chunked read)",
                    request=httpx.Request("POST", "https://example.com"),
                )
            return "recovered"

    failing = _FailingProvider()
    provider = FallbackProvider([failing])
    # Patch sleep to avoid real delays.
    original_sleep = asyncio.sleep

    async def _fast_sleep(seconds: float) -> None:
        await original_sleep(0)

    asyncio.sleep = _fast_sleep  # type: ignore[assignment]
    try:
        result = await provider.complete([ChatMessage("user", "hi")])
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]
    assert result == "recovered"
    assert failing.call_count == 3


# ---------------------------------------------------------------------------
# Fix 5: pre-flight binary validation in _run_command
# ---------------------------------------------------------------------------


def test_resolve_binary_error_reports_missing_binary_without_advice() -> None:
    """When 'python' is not on PATH but 'python3' is, the pre-flight check
    must surface a factual error instead of letting subprocess raise a
    confusing '[Errno 2] No such file or directory: 'python''."""
    error = _resolve_binary_error(["python", "-m", "pytest"])
    # On systems where 'python' exists, no error. On systems where only
    # 'python3' exists (the production failure), a factual error is returned.
    if shutil.which("python"):
        assert error == ""
    else:
        assert error == "binary 'python' not found on PATH."
        assert "Try " not in error


def test_resolve_binary_error_returns_empty_for_known_binary() -> None:
    error = _resolve_binary_error(["ls"])
    assert error == ""


def test_resolve_binary_error_returns_empty_for_path_binary() -> None:
    error = _resolve_binary_error(["/usr/bin/ls"])
    assert error == ""


def test_run_command_returns_structured_error_for_missing_binary(
    tmp_path: Path,
) -> None:
    """A missing binary must return a structured 127-error with facts,
    not a confusing '[Errno 2] No such file or directory'."""
    result = _run_command(
        ["definitely-not-a-real-binary-xyz123", "--version"],
        cwd=tmp_path,
        timeout=5,
    )
    assert result["exit_code"] == 127
    assert "not found" in result["stderr"].lower()
    assert result["error_reason"] == "binary_not_found"
    assert "candidate_binaries" not in result


def test_run_command_returns_fact_only_error_for_denied_binary(
    tmp_path: Path,
) -> None:
    result = _run_command(["rm", "-rf", "target"], cwd=tmp_path, timeout=5)

    assert result["exit_code"] == 127
    assert result["error_reason"] == "binary_denied"
    assert result["binary"] == "rm"
    assert "use " not in result["stderr"].lower()
    assert "instead" not in result["stderr"].lower()


# ---------------------------------------------------------------------------
# Fix 6: pending/prepared runs deletable from remote
# (Also covered by the updated test_remote_connector_cannot_run_but_can_cleanup_stuck_delegation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_can_delete_prepared_delegation(tmp_path: Path) -> None:
    """A run stuck in 'prepared' must be deletable from a remote surface."""
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="weixin-peer",
        sender_id="weixin-user",
        source="connector.weixin",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    spawned = await registry.invoke(
        "delegate.spawn",
        {
            "objective": "Prepare a tracked task",
            "context": "Remote connector requested tracked work.",
            "plan": "Prepare first; execution needs approval.",
            "success_criteria": "Task is tracked and governed.",
        },
        permission="prepare",
        context=context,
    )
    runs = RunStore(tmp_path)
    runs.update_run(spawned.run_id, status="prepared")

    delete_result = await registry.invoke(
        "delegate.delete",
        {"run_id": spawned.run_id, "reason": "cleanup stuck prepared run"},
        permission="write",
        context=context,
    )
    assert delete_result.ok is True
    assert runs.get(spawned.run_id) is None
