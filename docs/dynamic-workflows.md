# Governed Dynamic Workflows

Navi dynamic workflows are model-proposed orchestration plans that the runtime stores, gates, executes, resumes through the run path, and verifies automatically when runnable work is complete.

The planner decides when to call `workflow.propose` from ordinary user language. Users should not need to switch modes or manually request a workflow for normal use. CLI and API commands are operator control surfaces for inspection, approval, execution, recovery, and verification after a workflow has been proposed.

They are intentionally not executable scripts. A workflow is declarative data:

- objective;
- permission ceiling;
- max concurrency;
- total subagent limit;
- cost/risk metadata;
- stop condition;
- verification strategy;
- ordered subagent steps;
- optional dependency ids;
- allowed tools for each step;
- optional declared capability-call intents, stored as model-visible facts rather than replayable script instructions.

## Lifecycle

1. `workflow.propose` creates a workflow with `phase=paused`, `governance=awaiting_approval`, and `resolution=none`.
2. `workflow.approve` changes governance to `approved`, or ends the workflow with rejected governance and failed resolution.
3. `workflow.run` starts the next dependency-ready batch as model-owned step loops constrained by each step's `allowed_tools`.
4. `workflow.run` with `resume=true` continues from persisted state after interruption.
5. When no pending work remains, `workflow.run` applies the verifier and records terminal lifecycle facts such as `phase=ended`, `acceptance=accepted`, and `resolution=success` or `blocked`.
6. `workflow.state` returns workflow, step, event, and evidence facts.

## Safety Rules

- A workflow step cannot call `workflow.*` recursively.
- A step may only call its `allowed_tools`, plus the terminal conversation syscall `respond`.
- Every tool call must request a permission at or below the workflow permission ceiling.
- Stored `tool_calls` are proposal facts for the model, not an execution script for the runner to replay.
- Remote connector ingress can propose and inspect workflows, but cannot approve or run them by default.
- Ending without verifier evidence must not be treated as accepted success; acceptance and resolution remain separate lifecycle facts.

## CLI

```bash
navi workflow propose "Audit provider facts" --steps-json '[{"id":"provider","role":"auditor","objective":"Inspect provider config","allowed_tools":["provider.config"],"tool_calls":[{"tool":"provider.config","permission":"read","args":{}}]}]'
navi workflow approve <workflow_id>
navi workflow run <workflow_id>
navi workflow run <workflow_id> --resume
navi workflow show <workflow_id>
```
