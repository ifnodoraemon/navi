# Navi Requirements

This document is the current product and implementation source of truth for Navi. It should describe the repository as it is intended to work now, not preserve early refactoring milestones or stale compatibility expectations.

## Product Direction

Navi is a local-first agent OS for a governed personal AI assistant. It is inspired by adjacent agent systems, but it is not a clone, a chat wrapper, an enterprise RAG product, or a general multi-tenant gateway.

Non-negotiable engineering principles are captured in [principles.md](principles.md). Requirements and implementation choices must not violate those principles, especially the rules against product behavior hidden in keyword routing, prompt drift, or historical compatibility shims.

Core positioning:

- Personal assistant first.
- Local-first runtime and state.
- CLI and the headless local API are the developer/control surfaces.
- Connector packages expose remote surfaces without hardcoding channel behavior into the core runtime.
- Weixin/iLink is the first live-calibration target; Telegram exists as a connector package and test-covered adapter.
- High-risk or mutating remote actions require explicit policy, permission ceilings, approval state, trace evidence, and verifier checks.

Project names:

- Repository/package directory: `navi`
- CLI command: `navi`
- Python package: `navi`
- Distribution name: `navi-assistant`

## Explicit Scope

Current v1 includes:

- CLI chat and control plane through `navi`.
- Headless local API through `navi api`.
- Model provider abstraction with `openai-compatible`, `deepseek`, and `anthropic` provider specs.
- Persistent local state under `.navi/` or `NAVI_HOME`.
- Typed memory control system plus SQLite conversation sessions.
- Task, watch, goal, approval, execution, recovery, subagent, trace, evolution, and workflow state.
- Action/control capabilities declared in `src/navi/actions/specs.py`.
- Core fact tools and gateway-loaded tools exposed through the unified capability registry.
- Governed dynamic workflows with approval, dependency-aware execution, resumable state, step evidence, and verifier-backed completion.
- Declarative hooks loaded from built-in specs and local YAML files.
- Skill discovery from built-in skills and `.navi/skills/*/SKILL.md`.
- Connector registry with Weixin/iLink and Telegram adapters.
- Evaluation datasets for delegation routing, daily journeys, connector journeys, product acceptance, regression, and Claw-style Pass^3 flows.

Current v1 intentionally excludes:

- Team or multi-tenant permission models.
- A browser UI or hosted web control surface.
- Enterprise WeChat/WeCom as the primary channel.
- Official Account / 公众号 callback flow as a primary ingress path.
- A full RAG workbench.
- Unsupervised high-risk autonomous execution from remote connectors.
- Raw image/file/video/CDN media handling before text and transcript flows are live-calibrated.

## Connector Requirements

The core runtime must remain connector agnostic. Adding or changing a connector must be done through connector specs, adapter code, and connector-local affordances rather than core prompt rewrites.

Shared connector requirements:

- Connector packages must expose status facts without secrets.
- Connector ingress must route plain messages through the bounded observe/plan loop.
- Connector sessions must be explicit and isolated by connector/sender identity.
- Connector command syntax and approval phrasing are connector affordances, not base prompt behavior.
- Remote tool visibility must pass through `ConnectorToolPolicy` with permission ceilings, allowed tools, blocked capability classes, and audit facts.
- Remote connector ingress can propose and inspect workflows by default, but must not approve or run mutating workflows unless explicit policy enables it.

Weixin/iLink requirements:

- Provide `navi connectors setup weixin` for QR-login setup.
- Provide `navi connectors run weixin` for long-poll message processing.
- Store account credentials under `.navi/weixin/accounts/`.
- Store per-peer context tokens under `.navi/weixin/context-tokens.json`.
- Route inbound DM text messages into the agent runtime and send the response back to the same peer.
- Deduplicate inbound messages with a short TTL window.
- Support DM policies `open`, `allowlist`, `disabled`, and setup-oriented `pairing`.
- Keep group policy config, defaulted to `disabled`; ordinary WeChat group delivery is not promised unless upstream events are actually delivered.
- Support text replies, response chunking, optional typing indicators, and voice transcript text when upstream payloads include transcript text.
- Treat raw images, files, video, CDN decryption, and rich media processing as later work.

Telegram requirements:

- Provide an adapter under the connector registry with status diagnostics and connector-local approval affordances.
- Support bot-token configuration, DM policy, allowed users, and home chat id through config or environment.
- Support inbound message handling and replies.
- Keep Telegram behavior connector-local; the core prompt and router must not know Telegram-specific commands.

## Architecture Requirements

Keep the code small, explicit, and inspectable:

- `navi.config`: load `.navi/config.yaml`, `.navi/env`, and environment overrides.
- `navi.provider` and `navi.provider_specs`: model provider protocol plus declared provider defaults and structured-output policy.
- `navi.runtime`, `navi.engine`, `navi.syscalls`, `navi.prompt_os`, and `navi.prompting`: bounded turn orchestration, planner/responder prompt assembly, and provider-mediated syscall selection.
- `navi.actions.specs` and `navi.actions.*`: planner-visible action/control capability specs and handlers.
- `navi.capabilities` and `navi.capabilities_types`: unified capability registry, permission ceilings, contexts, and result envelopes.
- `navi.core_tools`, `navi.fact_tools`, and `navi.tools`: core fact tools, gateway loading, filtering, schema validation, and audit behavior.
- `navi.runs`, `navi.goals`, `navi.workflows`, `navi.subagents`, `navi.trace`, and `navi.recovery`: durable execution, goal, workflow, role, trace, and recovery stores.
- `navi.memory`: governed memory items plus SQLite conversation sessions.
- `navi.skills`: governed skill discovery and metadata.
- `navi.hooks`: declarative lifecycle hooks.
- `navi.evolution`: reviewable proposal and rollback ledger.
- `navi.connector_registry` and `navi.connector_runtime`: connector loading, status, diagnostics, and remote-safe ingress policy.
- `navi.weixin` and `navi.telegram`: connector packages that must not leak channel-specific behavior into the core prompt or router.
- `navi.api` and `navi.cli`: local API and CLI control surfaces.
- `navi.diagnostics`: local configuration, state, dependency, connector, service, auth, and capability checks.

Runtime rules:

- Missing real model credentials must fail clearly for real providers.
- Model providers must use real provider adapters; local tests may stub provider calls at the test boundary without adding runtime simulation modes.
- Structured output constraints must be passed through provider/tool schema channels; business prompts must not repeat JSON shapes, field lists, or formatting bans.
- Any action that can affect the user's machine, accounts, remote services, repository, files, credentials, or money must be traceable.
- Connector credentials should be persisted with restrictive file permissions when the OS allows it.
- Long-context operation must reload durable constraints, governance state, approvals, relevant memory, and goal/workflow state from stores before execution.
- Memory retrieval must be goal-directed and explainable; semantic similarity alone is not a sufficient recall policy.
- Connector plain messages must pass through a bounded observe/plan loop before general chat.
- High-confidence local action or schedule requests should call the relevant declared capability directly instead of asking the user to rephrase as a command.
- Tool planning must be capability-driven, not keyword-driven as product behavior.
- Deterministic parsers may only parse narrow structured facts such as ids, times, explicit command syntax, and provider/tool protocol envelopes.
- Deterministic routing must not invent missing facts such as default times, paths, service names, task ids, or permissions.
- Task and workflow goals are subordinate to user intent, durable constraints, approval state, permission ceilings, and safeguard policy.
- Skills provide promptable procedures, plugins provide installed capabilities/integrations, and hooks observe or gate lifecycle events.
- Anything with credentials, network calls, filesystem mutation, daemon behavior, providers, or connector surfaces must be a plugin/capability package rather than a skill.

## Public Interfaces

Current CLI surface:

```bash
navi chat
navi api
navi status
navi doctor [--connectivity]
navi run [--once] [--connector CONNECTOR]
navi model
navi skills

navi tools list
navi tools call TOOL_NAME --args-json JSON_ARGS

navi memory add TYPE CONTENT --reason REASON --provenance PROVENANCE
navi memory list
navi memory recall QUERY
navi memory conflicts
navi memory revoke ITEM_ID

navi session new [ALIAS]
navi session list
navi session aliases
navi session show SESSION_ID

navi auth status
navi hooks list
navi prompts inspect [planner|responder]

navi eval delegations
navi eval daily
navi eval claw
navi eval connector
navi eval acceptance

navi graph list
navi trace list
navi trace show TRACE_ID
navi trace evaluate TRACE_ID
navi trace evaluations [TRACE_ID]

navi goal list
navi goal show GOAL_ID

navi subagent list
navi subagent show SUBAGENT_ID

navi workflow propose OBJECTIVE
navi workflow list
navi workflow show WORKFLOW_ID
navi workflow approve WORKFLOW_ID
navi workflow reject WORKFLOW_ID
navi workflow run WORKFLOW_ID

navi evolution list
navi evolution targets
navi evolution proposals
navi evolution propose TARGET_TYPE TARGET_ID REASON
navi evolution apply-proposal PROPOSAL_ID
navi evolution record-evaluation PROPOSAL_ID RESULT
navi evolution show EVENT_ID
navi evolution rollback EVENT_ID

navi service unit
navi service install

navi connectors list
navi connectors setup CONNECTOR
navi connectors run CONNECTOR
navi connectors status CONNECTOR
navi connectors tail CONNECTOR
```

Current API surface:

```text
GET    /health
POST   /v1/chat
GET    /v1/sessions
POST   /v1/sessions
GET    /v1/session-aliases
GET    /v1/sessions/{session_id}
GET    /v1/memory
GET    /v1/memory/conflicts
POST   /v1/memory
GET    /v1/skills
GET    /v1/delegations
POST   /v1/delegations
PATCH  /v1/delegations/{run_id}
DELETE /v1/delegations/{run_id}
GET    /v1/approvals
GET    /v1/watches
POST   /v1/delegations/{run_id}/approve
POST   /v1/delegations/process
POST   /v1/active/delegations
POST   /v1/active/approve
POST   /v1/active/reject
POST   /v1/active/watches
POST   /v1/active/watches/process
GET    /v1/auth/status
GET    /v1/diagnostics
GET    /v1/tools
POST   /v1/tools/{tool_name}/call
GET    /v1/graph
GET    /v1/traces
GET    /v1/traces/{trace_id}
GET    /v1/trace-evaluations
POST   /v1/traces/{trace_id}/evaluate
GET    /v1/goals
GET    /v1/goals/{goal_id}
GET    /v1/subagents
GET    /v1/subagents/{subagent_id}
GET    /v1/workflows
POST   /v1/workflows
GET    /v1/workflows/{workflow_id}
POST   /v1/workflows/{workflow_id}/approve
POST   /v1/workflows/{workflow_id}/reject
POST   /v1/workflows/{workflow_id}/run
GET    /v1/evolution-events
POST   /v1/evolution-events/{event_id}/rollback
GET    /v1/evolution-targets
GET    /v1/evolution-proposals
POST   /v1/evolution-proposals
POST   /v1/evolution-proposals/{proposal_id}/apply
POST   /v1/evolution-proposals/{proposal_id}/evaluation
GET    /v1/connectors/{connector_name}/status
```

`POST /v1/memory` accepts a governed memory item shape: `type`, `content`, `scope`, `source`, `status`, `confidence`, and optional `metadata`. `GET /v1/memory` returns structured `items`; memory is not exposed as a flat text dump. `GET /v1/memory/conflicts` returns declared contradiction and supersession relationships so stale or competing facts are visible to operators and agents.

Lifecycle hooks are declared control-plane artifacts. Built-in hooks are loaded from `src/navi/specs_data.py`; local hooks can be added as YAML files under `NAVI_HOME/hooks/*.yaml` with `event`, optional `match`, `decision`, `reason`, and structured `facts`. Local hooks do not execute arbitrary code.

## Configuration

Current config shape:

```yaml
model:
  provider: openai-compatible
  model: gpt-4o
  timeout_seconds: 30.0
runtime:
  service_name: navi.service
  local_surface: local
  agent_step_budget: 8
execution:
  provider: navi
  timeout_seconds: 120.0
```

Model configs may also declare provider-specific `api_base_url`, `api_key`, `fallbacks`, and role `routes`.

Environment overrides:

```text
NAVI_HOME
NAVI_MODEL_PROVIDER
NAVI_MODEL
NAVI_MODEL_API_BASE_URL
NAVI_MODEL_API_KEY
OPENAI_API_KEY
DEEPSEEK_API_KEY
ANTHROPIC_API_KEY
NAVI_SERVICE_NAME
NAVI_LOCAL_SURFACE
NAVI_AGENT_STEP_BUDGET
NAVI_EXECUTION_PROVIDER
NAVI_EXECUTION_TIMEOUT_SECONDS
```

Weixin connector environment:

```text
NAVI_WEIXIN_ENABLED
WEIXIN_ACCOUNT_ID
WEIXIN_TOKEN
WEIXIN_BASE_URL
WEIXIN_DM_POLICY
WEIXIN_ALLOWED_USERS
WEIXIN_GROUP_POLICY
WEIXIN_GROUP_ALLOWED_USERS
WEIXIN_HOME_CHANNEL
```

Telegram connector environment:

```text
NAVI_TELEGRAM_ENABLED
TELEGRAM_BOT_TOKEN
TELEGRAM_API_BASE_URL
TELEGRAM_DM_POLICY
TELEGRAM_ALLOWED_USERS
TELEGRAM_HOME_CHAT_ID
```

## Local State

Navi stores local state under `.navi/` or `NAVI_HOME`:

```text
.navi/
├── config.yaml
├── env
├── evolution.db
├── goals.db
├── graph.db
├── memory.db
├── runs.db
├── subagents.db
├── traces.db
├── workflows.db
├── hooks/
├── skills/
├── telegram/
└── weixin/
```

## Current Implementation Status

Implemented:

- Python package scaffold and Typer CLI.
- FastAPI app for headless local API clients.
- Provider registry and provider adapters for OpenAI-compatible, DeepSeek, and Anthropic-compatible models.
- Bounded agent loop for observe/plan/read-tool/action chaining before final response.
- Unified capability registry for action specs and gateway tools.
- Core fact tools for providers, skills, tools, hooks, memory, files, shell, tests, web, service, and system facts.
- Internal execution through the structured `navi.actuator.v1` protocol; protocol actions must be capability calls and must produce capability-result evidence.
- Planner, executor, critic, and notification role executions recorded as subagent runtime records.
- Governed dynamic workflows persisted in `workflows.db` and exposed through `workflow.*`, CLI, and API surfaces.
- Local memory, session, task/watch, approval, goal, trace, evolution, hook, and graph stores.
- Connector registry plus Weixin and Telegram connector packages.
- Weixin account store, context-token store, deduplication, policy checks, HTTP client skeleton, typing indicators, chunked text replies, voice transcript extraction, and inbound-to-agent service flow.
- Telegram bot adapter with config, status diagnostics, policy checks, and inbound-to-agent service flow.
- Tests for config, runtime, memory, providers, capabilities, workflows, goals, traces, hooks, connector runtime, Weixin, Telegram, CLI coverage, API flow, and eval datasets.

Known gaps:

- Real iLink payloads/endpoints need live QR-login and DM calibration.
- `navi connectors setup weixin` should move from a single QR status poll to a clearer scan/confirm loop with timeout.
- Raw Weixin image, file, video, and CDN media handling is not implemented.
- Remote connector policy needs richer per-sender/per-surface configuration before mutating shell/file-write actuators are exposed remotely.
- MCP/plugin providers still need install-time permission manifests and policy audit before connector exposure.
- Workflow cost/token telemetry is still shallow metadata; approval UX should show concrete provider usage where available.
- Long-running goal/workflow compaction needs richer evidence preservation and replay.
- Verifier policies should grow beyond basic evidence checks into structured diffs, command-specific assertions, test result interpretation, and rollback proposals.
- Incident response CLI/API should group traces, failed safeguards, remediation proposals, and regression links.
- Browser UI is intentionally removed from this codebase.

## Next Implementation Steps

Recommended next order:

1. Live-calibrate Weixin QR setup and DM send/receive payload parsing.
2. Add richer connector liveness/status diagnostics for Weixin and Telegram production runs.
3. Add per-sender/per-surface remote tool policy configuration.
4. Add plugin/MCP provider permission manifests and install-time audit.
5. Add concrete workflow and provider usage telemetry.
6. Expand verifier policies for diffs, tests, rollback hints, and long-running compaction.
7. Add raw media handling only after text/transcript connector behavior is reliable.

## Verification Baseline

Before handoff, run:

```bash
pytest -q
PYTHONPATH=src python -m compileall src tests
NAVI_HOME=/tmp/navi-smoke PYTHONPATH=src python -c "from navi.api import create_app; app=create_app(); print(app.title, len(app.routes))"
NAVI_HOME=/tmp/navi-smoke-weixin PYTHONPATH=src python -c "from navi.paths import ensure_home; from navi.connector_registry import get_connector_adapter; home=ensure_home(); adapter=get_connector_adapter('weixin'); status=adapter.status(home); print(adapter.name, status['configured'])"
NAVI_HOME=/tmp/navi-smoke-telegram PYTHONPATH=src python -c "from navi.paths import ensure_home; from navi.connector_registry import get_connector_adapter; home=ensure_home(); adapter=get_connector_adapter('telegram'); status=adapter.status(home); print(adapter.name, status['configured'])"
```

End-to-end tests under `tests_e2e/` exercise the real model provider and run by
default; they are skipped only when no API key is set. Connector logic is
verified through injected test doubles in evals/unit tests, with no runtime fake
mode.
