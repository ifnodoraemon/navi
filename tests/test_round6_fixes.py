"""Tests for round-6 fixes derived from production log analysis.

Covers:
- approval.resolve idempotency (already-approved returns ok=True)
- reissue_approval dedup against existing pending approval
- governance capabilities exempt from second-level approval suspension
- FallbackProvider always wraps single-provider configs (transport retry)
- pre-flight binary validation in _run_command (python -> python3 hint)
- pending/prepared runs deletable from remote (stuck-run cleanup)
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.control import ApprovalService, SurfaceContext
from navi.core_tools import _resolve_binary_error, _run_command
from navi.provider import (
    AnthropicCompatibleProvider,
    ChatMessage,
    FallbackProvider,
    OpenAICompatibleProvider,
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
    assert second.facts.get("state_transition") == "already_resolved"


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


def test_resolve_binary_error_suggests_python3_for_python() -> None:
    """When 'python' is not on PATH but 'python3' is, the pre-flight check
    must surface a structured hint instead of letting subprocess raise a
    confusing '[Errno 2] No such file or directory: 'python''."""
    error = _resolve_binary_error(["python", "-m", "pytest"])
    # On systems where 'python' exists, no error. On systems where only
    # 'python3' exists (the production failure), a suggestion is returned.
    if shutil.which("python"):
        assert error == ""
    else:
        assert "python3" in error


def test_resolve_binary_error_returns_empty_for_known_binary() -> None:
    error = _resolve_binary_error(["ls"])
    assert error == ""


def test_resolve_binary_error_returns_empty_for_path_binary() -> None:
    error = _resolve_binary_error(["/usr/bin/ls"])
    assert error == ""


def test_run_command_returns_structured_error_for_missing_binary(
    tmp_path: Path,
) -> None:
    """A missing binary must return a structured 127-error with a clear hint,
    not a confusing '[Errno 2] No such file or directory'."""
    result = _run_command(
        ["definitely-not-a-real-binary-xyz123", "--version"],
        cwd=tmp_path,
        timeout=5,
    )
    assert result["exit_code"] == 127
    assert "not found" in result["stderr"].lower()


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
