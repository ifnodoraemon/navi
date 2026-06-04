# Governed Dynamic Workflows

Navi dynamic workflows are model-proposed orchestration plans that the runtime stores, gates, executes, resumes, and verifies.

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
- optional allowed tools;
- optional capability calls.

## Lifecycle

1. `workflow.propose` creates a workflow in `awaiting_approval`.
2. `workflow.approve` moves it to `approved` or `rejected`.
3. `workflow.run` executes the next dependency-ready batch through declared capabilities.
4. `workflow.resume` continues from persisted state after interruption.
5. `workflow.verify` records a critic subagent and marks the workflow `verified_complete` or `blocked`.
6. `workflow.status` returns workflow, step, event, and evidence facts.

## Safety Rules

- A workflow step cannot call `workflow.*` recursively.
- A step with `allowed_tools` may only call those tools.
- Every tool call must request a permission at or below the workflow permission ceiling.
- Remote connector ingress can propose and inspect workflows, but cannot approve or run them by default.
- Completion without verifier evidence is only `completed`, not `verified_complete`.

## CLI

```bash
navi workflow propose "Audit provider facts" --steps-json '[{"id":"provider","role":"auditor","objective":"Inspect provider config","allowed_tools":["provider.config"],"tool_calls":[{"tool":"provider.config","permission":"read","args":{}}]}]'
navi workflow approve <workflow_id>
navi workflow run <workflow_id>
navi workflow resume <workflow_id>
navi workflow verify <workflow_id>
navi workflow show <workflow_id>
```
