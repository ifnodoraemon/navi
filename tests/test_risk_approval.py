from __future__ import annotations

import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import navi.safeguards as safeguard_module

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.lifecycle import Phase
from navi.loop_contracts import LoopTerminalState
from navi.loop_runs import LoopRunStore
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.runs import RunStore
from navi.safeguards import assess_capability_call, required_permission_for_call


def _context(home: Path) -> CapabilityContext:
    return CapabilityContext(
        home=home,
        source="local",
        peer_id="peer-1",
        sender_id="user-1",
        workspace=str(home),
        permission_ceiling="write",
    )


class _DeleteGoalProvider:
    def __init__(self, target: Path) -> None:
        self.target = target
        self.calls: list[str] = []

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        self.calls.append(role)
        assert role == "planner"
        return json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "shell.run",
                        "permission": "write",
                        "args": {
                            "command": ["rm", self.target.name],
                            "cwd": ".",
                            "timeout_seconds": 10,
                        },
                        "reason": "delete the exact requested file",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _TwoGateShellProvider:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.calls: list[str] = []

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        self.calls.append(role)
        assert role == "planner"
        command = (
            ["definitely-missing-navi-test-command"]
            if len(self.calls) == 1
            else ["another-missing-navi-test-command"]
        )
        return json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "shell.run",
                        "permission": "write",
                        "args": {
                            "command": command,
                            "cwd": ".",
                            "timeout_seconds": 1,
                        },
                        "reason": "run the requested diagnostic command",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


def _verification_command(target: Path) -> str:
    # Loop execution and verification share the same shadow-workspace envelope.
    # Keep the checker workspace-relative so it observes the staged mutation
    # before the shadow is merged back into the real workspace.
    script = f"from pathlib import Path; assert not Path({target.name!r}).exists()"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


@pytest.mark.asyncio
async def test_destructive_file_overwrite_waits_for_approval_then_replays(tmp_path: Path) -> None:
    target = tmp_path / "data.txt"
    target.write_text("important data\n", encoding="utf-8")
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _context(tmp_path)
    args = {"path": str(target), "content": "", "mode": "overwrite"}

    suspended = await registry.invoke("file.write", args, permission="write", context=context)

    assert suspended.ok is False
    assert suspended.yields_control is True
    assert target.read_text(encoding="utf-8") == "important data\n"
    assert suspended.facts is not None
    assert suspended.facts["risk"]["reason_code"] == (
        "destructive_file_overwrite_requires_approval"
    )
    assert suspended.facts["risk"]["evidence"]["before_size"] == 15
    approval = RunStore(tmp_path).pending_approval_for_run(suspended.run_id)
    assert approval is not None
    assert approval.reason.startswith("destructive_file_overwrite_requires_approval:")

    resolved = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=context,
    )
    assert resolved.ok is True
    approval_run = RunStore(tmp_path).get(suspended.run_id)
    assert approval_run is not None
    assert approval_run.phase == Phase.PENDING

    executed = await registry.invoke("file.write", args, permission="write", context=context)

    assert executed.ok is True
    assert target.read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_shell_binary_is_approved_instead_of_name_blocked(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    run = runs.create(
        "inspect rm version",
        kind="loop:turn",
        source="local",
        peer_id="peer-1",
        sender_id="user-1",
        workspace=str(tmp_path),
        phase=Phase.PENDING,
    )
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        governed_run_id=run.id,
    )
    context = _context(tmp_path)
    args = {"command": ["rm", "--version"]}

    suspended = await registry.invoke("shell.run", args, permission="write", context=context)

    assert suspended.ok is False
    assert suspended.facts is not None
    assert suspended.facts["risk"]["reason_code"] == "opaque_shell_effect_requires_approval"
    approval = runs.pending_approval_for_run(run.id)
    assert approval is not None

    resolved = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=context,
    )
    assert resolved.ok is True

    executed = await registry.invoke("shell.run", args, permission="write", context=context)

    assert executed.ok is True
    assert "rm (GNU coreutils)" in str((executed.facts or {}).get("stdout") or "")


@pytest.mark.asyncio
async def test_uptime_is_read_without_approval(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    result = await registry.invoke(
        "shell.run",
        {"command": ["uptime", "-s"]},
        permission="read",
        context=_context(tmp_path),
    )

    assert result.ok is True
    assert result.yields_control is False
    assert result.facts["exit_code"] == 0
    assert str(result.facts["stdout"]).strip()
    assert RunStore(tmp_path).list_approvals(limit=10) == []


@pytest.mark.asyncio
async def test_shell_effectful_command_cannot_hide_behind_read_permission(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    target = tmp_path / "must-not-exist.txt"
    args = {"command": ["touch", str(target)]}

    suspended = await registry.invoke(
        "shell.run",
        args,
        permission="read",
        context=_context(tmp_path),
    )
    assert suspended.ok is False
    assert suspended.yields_control is True
    assert suspended.error_reason == "sensitive_op_requires_approval"
    assert suspended.facts["requested_permission"] == "write"
    assert target.exists() is False
    approval = RunStore(tmp_path).pending_approval_for_run(suspended.run_id)
    assert approval is not None
    assert approval.requested_permission == "write"
    resolved = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=_context(tmp_path),
    )
    assert resolved.ok is True


@pytest.mark.asyncio
async def test_existing_actor_approval_is_found_beyond_global_run_noise(
    tmp_path: Path,
) -> None:
    runs = RunStore(tmp_path)
    for index in range(101):
        runs.create(
            f"unrelated approval envelope {index}",
            kind="capability_approval",
            source="telegram",
            peer_id="other-peer",
            sender_id="other-sender",
            workspace=str(tmp_path),
            phase=Phase.PAUSED,
        )

    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    args = {"command": ["touch", str(tmp_path / "approval-target.txt")]}
    first = await registry.invoke(
        "shell.run",
        args,
        permission="read",
        context=_context(tmp_path),
    )
    second = await registry.invoke(
        "shell.run",
        args,
        permission="read",
        context=_context(tmp_path),
    )

    assert first.error_reason == "sensitive_op_requires_approval"
    assert second.error_reason == "sensitive_op_requires_approval"
    assert second.facts["state_transition"] == "existing"
    assert second.facts["approval"]["id"] == first.facts["approval"]["id"]
    actor_approvals = runs.list_approvals(
        source="local",
        peer_id="peer-1",
        sender_id="user-1",
        limit=10,
    )
    assert [approval.id for approval in actor_approvals] == [first.facts["approval"]["id"]]


def test_shell_effect_classification_is_argument_sensitive(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    spec = registry.get("shell.run")
    assert spec is not None

    read_only = assess_capability_call(
        spec,
        {"command": ["find", ".", "-maxdepth", "1", "-type", "f"]},
        workspace=str(tmp_path),
    )
    destructive = assess_capability_call(
        spec,
        {"command": ["find", ".", "-name", "*.tmp", "-delete"]},
        workspace=str(tmp_path),
    )
    assert read_only.confirmation_required is False
    assert read_only.evidence["required_permission"] == "read"
    assert destructive.confirmation_required is True
    assert destructive.evidence["required_permission"] == "write"

    process_read = assess_capability_call(
        spec,
        {"command": ["pgrep", "-fa", "codex"]},
        workspace=str(tmp_path),
    )
    assert process_read.confirmation_required is False
    assert process_read.evidence["required_permission"] == "read"
    assert process_read.evidence["observation_scope"] == "host_process_table"

    direct_http_client = assess_capability_call(
        spec,
        {"command": ["curl", "http://169.254.169.254/latest/meta-data/"]},
        workspace=str(tmp_path),
    )
    assert direct_http_client.confirmation_required is True
    assert direct_http_client.evidence["required_permission"] == "write"
    assert direct_http_client.evidence["effect_classification"] == (
        "opaque_or_effectful_command"
    )


def test_call_policy_is_declared_by_contract_not_capability_name(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    shell_spec = registry.get("shell.run")
    file_spec = registry.get("file.write")
    http_spec = registry.get("http.fetch")
    assert shell_spec is not None and file_spec is not None and http_spec is not None

    renamed_shell = replace(shell_spec, name="process.inspect")
    renamed_file = replace(file_spec, name="workspace.persist")
    renamed_http = replace(http_spec, name="network.request")

    assert (
        required_permission_for_call(
            renamed_shell,
            {"command": ["find", ".", "-maxdepth", "1"]},
        )
        == "read"
    )
    assert (
        assess_capability_call(
            renamed_file,
            {"path": "../outside.txt", "mode": "overwrite"},
            workspace=str(tmp_path / "workspace"),
        ).reason_code
        == "outside_workspace_write_requires_approval"
    )
    assert (
        assess_capability_call(
            renamed_http,
            {"url": "http://model-gateway.internal/v1/models"},
            workspace=str(tmp_path),
        ).reason_code
        == "private_network_access_requires_approval"
    )


@pytest.mark.asyncio
async def test_file_read_rejects_paths_outside_current_workspace(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    result = await registry.invoke(
        "file.read",
        {"path": "../outside.txt"},
        permission="read",
        context=_context(tmp_path),
    )

    assert result.ok is False
    assert result.error_reason == "resource_scope_violation"
    assert result.facts["workspace_boundary"]["allowed"] is False


@pytest.mark.asyncio
async def test_shell_sandbox_does_not_inherit_host_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAVI_TEST_SECRET", "must-not-reach-shell")
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    result = await registry.invoke(
        "shell.run",
        {"command": ["printenv", "NAVI_TEST_SECRET"]},
        permission="read",
        context=_context(tmp_path),
    )

    assert result.ok is False
    assert "must-not-reach-shell" not in str(result.facts)
    assert result.facts["sandboxed"] is True
    assert result.facts["sandbox_backend"] == "bubblewrap"


@pytest.mark.asyncio
async def test_approval_resolve_resumes_original_shell_checkpoint(tmp_path: Path) -> None:
    target = tmp_path / "performance-report.md"
    target.write_text("important report\n", encoding="utf-8")
    provider = _DeleteGoalProvider(target)
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )
    context = _context(tmp_path)

    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "delete the performance report",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["shell.run"],
            "verification_command": _verification_command(target),
        },
        permission="prepare",
        context=context,
    )

    assert opened.facts is not None
    assert opened.facts["loop_terminal_state"] == LoopTerminalState.WAITING_APPROVAL
    assert target.exists()
    approval = RunStore(tmp_path).pending_approval_for_run(opened.run_id)
    assert approval is not None

    resolved = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="prepare",
        context=context,
    )

    assert resolved.ok is True
    assert resolved.facts is not None
    assert resolved.facts["continuation_status"] == "completed"
    assert resolved.facts["completion_evidence"] is True
    assert resolved.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert resolved.facts["continuation_result"]["ok"] is True
    assert resolved.facts["continuation_result"]["facts"]["command"][0] == "rm"
    assert resolved.facts["continuation_result"]["facts"]["exit_code"] == 0
    assert resolved.facts["continuation_checker_results"]
    assert target.exists() is False
    assert provider.calls == ["planner"]

    repeated = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="prepare",
        context=context,
    )

    assert repeated.ok is True
    assert repeated.facts is not None
    assert repeated.facts["state_transition"] == "already_approved"
    assert repeated.facts["continuation_status"] == "completed"
    assert repeated.facts["completion_evidence"] is True
    assert repeated.facts["continuation_result"]["facts"]["exit_code"] == 0
    assert provider.calls == ["planner"]

    conflicting = await registry.invoke(
        "approval.resolve",
        {"decision": "reject", "code": approval.code},
        permission="prepare",
        context=context,
    )
    assert conflicting.ok is False
    assert conflicting.facts is not None
    assert conflicting.facts["reason"] == "approval_already_approved"


@pytest.mark.asyncio
async def test_approval_resolve_separates_decision_from_distinct_next_gate(
    tmp_path: Path,
) -> None:
    provider = _TwoGateShellProvider(tmp_path)
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )
    context = _context(tmp_path)

    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "run a diagnostic that may need a revised command",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["shell.run"],
        },
        permission="prepare",
        context=context,
    )

    assert opened.facts is not None
    assert opened.facts["loop_terminal_state"] == LoopTerminalState.WAITING_APPROVAL
    first = RunStore(tmp_path).pending_approval_for_run(opened.run_id)
    assert first is not None

    resolved = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": first.code},
        permission="prepare",
        context=context,
    )

    assert resolved.ok is True
    assert resolved.facts is not None
    assert resolved.facts["decision_applied"] is True
    assert resolved.facts["approval_control_completion_evidence"] is True
    assert resolved.facts["completion_evidence"] is False
    assert resolved.facts["continuation_status"] == LoopTerminalState.WAITING_APPROVAL
    assert resolved.facts["continuation_requires_approval"] is True
    assert resolved.facts["continuation_pending_approval_is_distinct"] is True
    assert resolved.facts["pending_approval"]["id"] != first.id
    assert resolved.facts["pending_approval"]["requested_tool"] == "shell.run"
    assert provider.calls == ["planner", "planner"]


@pytest.mark.asyncio
async def test_approval_reject_cancels_original_loop_without_execution(tmp_path: Path) -> None:
    target = tmp_path / "keep-report.md"
    target.write_text("keep me\n", encoding="utf-8")
    provider = _DeleteGoalProvider(target)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
    )
    context = _context(tmp_path)
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "delete the report only if approved",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["shell.run"],
            "verification_command": _verification_command(target),
        },
        permission="prepare",
        context=context,
    )
    approval = RunStore(tmp_path).pending_approval_for_run(opened.run_id)
    assert approval is not None

    rejected = await registry.invoke(
        "approval.resolve",
        {"decision": "reject", "code": approval.code},
        permission="prepare",
        context=context,
    )

    assert rejected.ok is True
    assert rejected.facts is not None
    assert rejected.facts["continuation_status"] == "rejected"
    assert rejected.facts["completion_evidence"] is False
    assert rejected.facts["loop_terminal_state"] == LoopTerminalState.CANCELLED
    assert target.read_text(encoding="utf-8") == "keep me\n"
    loop = LoopRunStore(tmp_path).get_run(opened.facts["loop_run_id"])
    assert loop is not None
    assert loop.terminal_state == LoopTerminalState.CANCELLED

    repeated = await registry.invoke(
        "approval.resolve",
        {"decision": "reject", "code": approval.code},
        permission="prepare",
        context=context,
    )
    assert repeated.ok is True
    assert repeated.facts["state_transition"] == "already_rejected"


@pytest.mark.asyncio
async def test_private_http_target_requires_approval_without_hard_rejection(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _context(tmp_path)

    suspended = await registry.invoke(
        "http.fetch",
        {"url": "http://10.232.18.209/v1/dashboard/usage"},
        permission="network",
        context=context,
    )

    assert suspended.ok is False
    assert suspended.yields_control is True
    assert suspended.facts is not None
    assert suspended.facts["risk"]["reason_code"] == ("private_network_access_requires_approval")
    assert "public" not in (suspended.message or "").lower()


@pytest.mark.asyncio
async def test_public_hostname_resolving_to_loopback_requires_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        safeguard_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                safeguard_module.socket.AF_INET,
                safeguard_module.socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", 443),
            )
        ],
    )
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    suspended = await registry.invoke(
        "http.fetch",
        {"url": "https://public-name.example/health"},
        permission="network",
        context=_context(tmp_path),
    )

    assert suspended.ok is False
    assert suspended.yields_control is True
    assert suspended.facts["risk"]["reason_code"] == "private_network_access_requires_approval"
    assert suspended.facts["risk"]["evidence"]["private_resolved_addresses"] == ["127.0.0.1"]


@pytest.mark.asyncio
async def test_http_approval_is_bound_to_the_resolved_address_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = {"value": "127.0.0.1"}

    def resolve(*args, **kwargs):
        return [
            (
                safeguard_module.socket.AF_INET,
                safeguard_module.socket.SOCK_STREAM,
                6,
                "",
                (address["value"], 443),
            )
        ]

    monkeypatch.setattr(safeguard_module.socket, "getaddrinfo", resolve)
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _context(tmp_path)
    args = {"url": "https://changing.example/health"}

    first = await registry.invoke("http.fetch", args, permission="network", context=context)
    first_approval = RunStore(tmp_path).pending_approval_for_run(first.run_id)
    assert first_approval is not None
    approved = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": first_approval.code},
        permission="prepare",
        context=context,
    )
    assert approved.ok is True

    address["value"] = "10.0.0.8"
    second = await registry.invoke("http.fetch", args, permission="network", context=context)
    second_approval = RunStore(tmp_path).pending_approval_for_run(second.run_id)

    assert second.ok is False
    assert second.yields_control is True
    assert second_approval is not None
    assert second_approval.id != first_approval.id


def test_public_read_only_http_call_is_not_high_risk(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    spec = registry.get("http.fetch")
    assert spec is not None

    risk = assess_capability_call(
        spec,
        {"url": "https://example.com/health", "method": "GET"},
        workspace=str(tmp_path),
    )

    assert risk.risk_class == "medium"
    assert risk.confirmation_required is False


def test_http_get_with_body_requires_approval(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    spec = registry.get("http.fetch")
    assert spec is not None

    risk = assess_capability_call(
        spec,
        {"url": "https://example.com/query", "method": "GET", "body": "query=x"},
        workspace=str(tmp_path),
    )

    assert risk.risk_class == "high"
    assert risk.confirmation_required is True
    assert risk.reason_code == "external_network_side_effect_requires_approval"


def test_internal_hostname_requires_approval(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    spec = registry.get("http.fetch")
    assert spec is not None

    risk = assess_capability_call(
        spec,
        {"url": "http://model-gateway.internal/v1/models"},
        workspace=str(tmp_path),
    )

    assert risk.risk_class == "high"
    assert risk.confirmation_required is True
    assert risk.reason_code == "private_network_access_requires_approval"


@pytest.mark.asyncio
async def test_external_file_delivery_requires_approval(tmp_path: Path) -> None:
    source = tmp_path / "report.txt"
    source.write_text("report", encoding="utf-8")
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    suspended = await registry.invoke(
        "channel.send_file",
        {"path": str(source)},
        permission="write",
        context=_context(tmp_path),
    )

    assert suspended.ok is False
    assert suspended.yields_control is True
    assert suspended.facts is not None
    assert suspended.facts["risk"]["reason_code"] == ("external_side_effect_requires_approval")
