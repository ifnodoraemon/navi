"""Execution protocol: dataclasses, parsing, and schema."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..runs import Run

INTERNAL_EXECUTION_PROVIDER = "navi"
EXECUTION_PROTOCOL_VERSION = "navi.actuator.v1"
SUBAGENT_PLANNER_ROLE = "planner"
SUBAGENT_EXECUTOR_ROLE = "executor"
SUBAGENT_NOTIFICATION_ROLE = "notification"


@dataclass(frozen=True)
class ExecutionProtocol:
    version: str = EXECUTION_PROTOCOL_VERSION
    phase: str = ""
    run_id: str = ""
    plan_id: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    completion: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return str(self.completion.get("summary") or "").strip()[:1600]

    @property
    def actions(self) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for step in self.steps:
            raw_actions = step.get("actions") if isinstance(step, dict) else None
            if isinstance(raw_actions, list):
                actions.extend(action for action in raw_actions if isinstance(action, dict))
        return actions

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "phase": self.phase,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "steps": self.steps,
            "evidence": self.evidence,
            "verification": self.verification,
            "completion": self.completion,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_model_output(cls, *, run_id: str, phase: str, text: str) -> "ExecutionProtocol":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("execution protocol output must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("execution protocol missing JSON object")
        payload = (
            parsed.get("navi_execution")
            if isinstance(parsed.get("navi_execution"), dict)
            else parsed
        )
        version = str(payload.get("version") or "")
        if version != EXECUTION_PROTOCOL_VERSION:
            raise ValueError(f"execution protocol version must be {EXECUTION_PROTOCOL_VERSION}")
        payload_phase = str(payload.get("phase") or phase)
        if payload_phase != phase:
            raise ValueError(f"execution protocol phase must be {phase}")
        payload_run_id = str(payload.get("run_id") or run_id)
        if run_id and payload_run_id != run_id:
            raise ValueError("execution protocol run_id does not match task")
        steps = _protocol_steps(payload)
        completion = _protocol_dict(payload, "completion")
        summary = str(completion.get("summary") or "").strip()
        if not summary:
            raise ValueError("execution protocol completion.summary is required")
        return cls(
            version=version,
            phase=payload_phase,
            run_id=payload_run_id,
            plan_id=str(payload.get("plan_id") or f"{phase}:{payload_run_id or 'watch'}"),
            steps=steps,
            evidence=_protocol_dict_list(payload.get("evidence")),
            verification=_protocol_dict(payload, "verification"),
            completion=completion,
        )

    @classmethod
    def internal_status(
        cls,
        *,
        run_id: str,
        phase: str,
        status: str,
        summary: str,
        reason_code: str,
        action_kind: str,
    ) -> "ExecutionProtocol":
        return cls(
            phase=phase,
            run_id=run_id,
            plan_id=f"{phase}:{run_id or 'internal'}",
            steps=[
                {
                    "id": "internal",
                    "actions": [
                        {
                            "kind": action_kind,
                            "target": run_id,
                            "status": status,
                            "summary": summary,
                        }
                    ],
                    "verification": {"checks": [], "reason_code": reason_code},
                    "status": status,
                }
            ],
            evidence=[{"kind": "internal_state", "summary": summary}],
            verification={"status": status, "checks": [], "reason_code": reason_code},
            completion={"status": status, "summary": summary},
        )


@dataclass(frozen=True)
class ExecutionResult:
    provider: str
    phase: str
    command: list[str]
    stdout: str
    stderr: str
    exit_code: int
    started_at: float
    ended_at: float
    protocol: ExecutionProtocol | None = None
    model_role: str = ""

    @property
    def summary(self) -> str:
        if self.protocol and self.protocol.summary:
            return self.protocol.summary
        text = (self.stdout or self.stderr).strip()
        return text[:1600] if text else f"{self.phase} exited with {self.exit_code}"


def _protocol_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if isinstance(value, dict):
        return dict(value)
    if key == "verification":
        return {"status": "proposed", "checks": [], "reason_code": "verification_omitted"}
    raise ValueError(f"execution protocol {key} must be an object")


def _protocol_dict_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("execution protocol evidence must be a list")
    return [dict(item) for item in value[:20] if isinstance(item, dict)]


def _protocol_steps(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("execution protocol steps must be a non-empty list")
    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise ValueError("execution protocol step entries must be objects")
        raw_actions = raw_step.get("actions")
        actions = raw_actions if isinstance(raw_actions, list) else []
        if not actions:
            raise ValueError("execution protocol step actions must be a non-empty list")
        verification = raw_step.get("verification")
        steps.append(
            {
                "id": str(raw_step.get("id") or f"step-{index + 1}"),
                "description": str(raw_step.get("description") or ""),
                "actions": [dict(action) for action in actions if isinstance(action, dict)],
                "verification": dict(verification) if isinstance(verification, dict) else {},
                "on_failure": str(raw_step.get("on_failure") or "stop"),
            }
        )
    return steps


def execution_protocol_instruction(phase: str) -> str:
    return (
        f"Return a {phase} protocol with the steps needed for the current phase. "
        "Each step action must name a declared capability tool, permission, and args. "
        "The protocol is declarative evidence for Navi; do not claim side effects that did not run."
    )


def execution_output_schema(phase: str) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "navi_execution": {
                "type": "object",
                "properties": {
                    "version": {"type": "string", "enum": [EXECUTION_PROTOCOL_VERSION]},
                    "phase": {"type": "string", "enum": [phase]},
                    "run_id": {"type": "string"},
                    "plan_id": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "description": {"type": "string"},
                                "actions": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "tool": {"type": "string"},
                                            "permission": {
                                                "type": "string",
                                                "enum": ["read", "prepare", "write"],
                                            },
                                            "args": {"type": "object"},
                                        },
                                        "required": ["tool", "permission", "args"],
                                        "additionalProperties": True,
                                    },
                                },
                                "verification": {
                                    "type": "object",
                                    "properties": {
                                        "checks": {"type": "array"},
                                        "reason_code": {"type": "string"},
                                    },
                                    "required": ["checks", "reason_code"],
                                    "additionalProperties": True,
                                },
                                "on_failure": {
                                    "type": "string",
                                    "enum": ["stop", "continue", "retry_once"],
                                },
                            },
                            "required": ["id", "actions", "verification"],
                            "additionalProperties": True,
                        },
                    },
                    "evidence": {"type": "array", "items": {"type": "object"}},
                    "verification": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string"},
                            "checks": {"type": "array"},
                            "reason_code": {"type": "string"},
                        },
                        "required": ["status", "checks", "reason_code"],
                        "additionalProperties": True,
                    },
                    "completion": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                        "required": ["status", "summary"],
                        "additionalProperties": True,
                    },
                },
                "required": [
                    "version",
                    "phase",
                    "run_id",
                    "plan_id",
                    "steps",
                    "evidence",
                    "verification",
                    "completion",
                ],
                "additionalProperties": False,
            }
        },
        "required": ["navi_execution"],
        "additionalProperties": False,
    }
    return {"name": f"navi_{phase}_execution", "strict": False, "schema": schema}


def require_workspace_value(workspace: str, *, run_id: str = "") -> str:
    value = workspace.strip()
    if not value:
        suffix = f" for run {run_id}" if run_id else ""
        raise ValueError(f"workspace is required{suffix}")
    return value


def task_workspace(task: Run) -> Path:
    return Path(require_workspace_value(task.workspace, run_id=task.id))
