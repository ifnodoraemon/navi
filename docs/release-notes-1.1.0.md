# Navi 1.1.0 Release Notes

Date: 2026-06-04

Navi 1.1.0 adds governed dynamic workflows to the v1 agent OS contract.

## Included

- `workflow.propose`: planner-selected capability that creates a declarative orchestration plan with objective, subagent steps, dependencies, allowed tools, permission ceiling, cost/risk metadata, stop condition, and verification strategy.
- `workflow.approve`: explicitly approves or rejects a proposed workflow before execution.
- `workflow.run`: executes the next bounded batch of an approved workflow through declared capabilities only.
- `workflow.resume`: resumes persisted workflow state after a partial run or interruption.
- `workflow.verify`: records an independent verifier subagent and marks the workflow verified or blocked.
- `workflow.status`: exposes workflow, step, event, and evidence state for CLI/API/connector inspection.
- `workflows.db`: durable workflow, step, and event state with strict current-contract schema checks.
- CLI/API surfaces under `navi workflow ...` and `/v1/workflows`.
- Connector policy exposes only `workflow.propose` and `workflow.status` by default; remote connector ingress cannot directly approve, run, resume, or verify workflows.
- Engine-level tests cover the actual path: natural language request, model-selected `workflow.propose`, persisted proposal, and user-facing approval prompt.

## Safety Contract

Workflow plans are data, not executable scripts. A workflow step can only call declared Navi capabilities. The runtime enforces:

- workflow-level permission ceiling;
- step-level allowed tools;
- dependency ordering;
- bounded concurrency and subagent count;
- explicit approval before execution;
- subagent evidence for every step;
- verifier-backed completion before `verified_complete`.

## Post-1.1 Work

- richer workflow compaction for very long-running workflows;
- cost telemetry and model-token accounting for workflow proposals;
- parallel worker execution once shared-state race rules are mature;
- richer verifier policies for diffs, tests, and rollback plans.
