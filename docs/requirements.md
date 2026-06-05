# Navi Requirements

This document captures the current product and implementation decisions so the next development session can continue without re-litigating the basics.

## Product Direction

Navi is a local-first personal AI assistant, inspired by Hermes and OpenClaw, but it is not a clone of either project.

Non-negotiable engineering principles are captured in [principles.md](principles.md). Requirements and implementation choices must not violate those principles.

Core positioning:

- Personal assistant first, not an enterprise RAG product.
- Local-first runtime and state.
- CLI and the headless local API are the developer/control surfaces.
- Personal Weixin/WeChat is the first remote channel.
- Do not build a general multi-channel gateway in v1.

The project name is `Navi`.

- Repository/package directory: `navi`
- CLI command: `navi`
- Python package: `navi`
- Distribution name: `navi-assistant`

## Explicit Scope

v1 should include:

- CLI chat: `navi chat`
- Headless local API: `navi api`
- Model provider abstraction, with mock and OpenAI-compatible providers.
- Persistent local state under `.navi/` or `NAVI_HOME`.
- Typed memory control system plus SQLite session history.
- Skill discovery from `.navi/skills/*/SKILL.md`.
- Minimal task store for future scheduled/async work.
- Personal Weixin setup and long-poll gateway shape.

v1 should not include:

- Telegram, Slack, WhatsApp, Discord, or other remote channels.
- Enterprise WeChat / WeCom as the primary channel.
- Official Account / 公众号 callback flow.
- Team/multi-tenant permission model.
- Full RAG workbench integration.
- Complex autonomous tool execution before permissions and tracing are tightened.

## Weixin Requirements

The initial Weixin integration targets personal WeChat via QR login and long polling, following the Hermes Weixin/iLink design.

Required behavior:

- Provide `navi connectors setup weixin` for QR-login setup.
- Provide `navi connectors run weixin` for long-poll message processing.
- Store account credentials under `.navi/weixin/accounts/`.
- Store per-peer context tokens under `.navi/weixin/context-tokens.json`.
- Route inbound DM text messages into the agent runtime.
- Send the agent response back to the same peer.
- Deduplicate inbound messages with a short TTL window.
- Support DM access policy:
  - `open`
  - `allowlist`
  - `disabled`
  - `pairing` as a setup-oriented mode
- Keep group policy config, but default it to `disabled`.

Important limitation:

- v1 only promises DM behavior. Ordinary WeChat group delivery may not work because iLink bot identities often do not receive normal group events. If the upstream does not deliver group events, Navi should diagnose/log that situation rather than pretending group chat is supported.

v1 media policy:

- Text is required.
- Images, files, voice, video, CDN encryption/decryption, typing indicators, and advanced markdown chunking are later work.
- The current code should remain structured so those can be added without rewriting the service boundary.

## Architecture Requirements

Keep the code small and explicit:

- `navi.config`: load `.navi/config.yaml`, `.navi/env`, and environment overrides.
- `navi.provider`: model provider protocol plus mock/OpenAI-compatible implementations.
- `navi.memory`: Markdown memory and SQLite session store.
- `navi.skills`: progressive skill discovery from `SKILL.md`.
- `navi.runtime`: agent turn orchestration.
- `navi.model_tools`: model-driven next-tool-call planning; it is not an intent classifier.
- `navi.action_tools`: planner-visible action/control tool specs such as answer, clarify, task, watch, and approval.
- `navi.core_tools`: core fact tool specs and handlers.
- `navi.tools`: tool registry and gateway loading mechanism.
- `navi.api`: FastAPI local API.
- `navi.cli`: Typer CLI entrypoint.
- `navi.connector_registry`: loads connector adapters from declarative connector specs.
- `navi.weixin`: Weixin connector implementation; it must not leak channel details into the core prompt or router.

Runtime rules:

- Missing real model credentials should fail clearly for real providers.
- Mock provider is allowed for local development and tests.
- Browser-based control surfaces are out of scope; local API clients must not expose long-lived secrets.
- Connector credentials should be persisted with restrictive file permissions when the OS allows it.
- Any future dangerous tools, especially shell/file write tools, must require an approval policy before being available to remote connector messages.
- Long-context operation must reload durable constraints, trust state, approvals, and relevant memory from stores before execution; it must not rely only on the current prompt window or a lossy summary.
- Memory implementation should evolve toward typed, scoped, provenance-bearing stores: working, constraint, episodic, semantic, procedural, preference, negative, and skill memory.
- Memory retrieval must be goal-directed and explainable; semantic similarity alone is not a sufficient recall policy.
- Conversation sessions must be explicit state. Long-running connectors need a way to start a fresh session without deleting old transcripts, otherwise topic drift and stale local context will pollute future answers.
- Connector plain messages must pass through a bounded observe/plan loop before general chat. The loop may call multiple read fact tools in one turn before answering, then must stop, answer, ask a clarification, or prepare a managed action.
- High-confidence local action or schedule requests should call the relevant task/watch/fact capability directly instead of asking the user to rephrase as a command.
- Tool planning must be capability-driven, not keyword-driven as product behavior. Deterministic parsers are acceptable only for narrow structured facts such as ids, times, and explicit command syntax.
- Source code must not define ordinary natural-language behavior with keyword lists such as question markers, action verbs, time-of-day words, or channel-specific phrasing. Natural language interpretation belongs to the model with declared tools.
- Deterministic routing must not invent missing facts such as default times, paths, service names, task ids, or permissions. If a capability can be used but required slots are missing, the agent should ask a concise clarification.
- Command-like text from users is ordinary model input unless a specific management CLI/API endpoint handles it. User-facing task, watch, approval, and session behavior must be decided from natural language plus declared tools.
- Task goals must be subordinate to user intent, durable constraints, approval state, permission ceilings, and safeguard policy. Model replacement, shutdown, scope reduction, and failed goal completion are operating states, not threats to resist.
- Extension boundaries must be explicit: skills provide promptable procedures, plugins provide installed capabilities/integrations, and hooks observe or gate lifecycle events.
- Anything with credentials, network calls, filesystem mutation, daemon behavior, providers, or connector surfaces must be a plugin rather than a skill.
- Anything that runs at task/message/tool/approval/memory lifecycle boundaries must be a hook rather than hidden inline logic.

## Public Interfaces

Current CLI surface:

```bash
navi chat
navi api
navi run
navi model
navi tools list
navi tools call TOOL_NAME --args-json JSON_ARGS
navi memory add TYPE CONTENT
navi memory list
navi memory recall QUERY
navi memory revoke ITEM_ID
navi session new [ALIAS]
navi session list
navi session aliases
navi session show SESSION_ID
navi skills
navi auth status
navi graph list
navi trust list
navi trust set RULE_ID LEVEL
navi workflow propose OBJECTIVE
navi workflow list
navi workflow show WORKFLOW_ID
navi workflow approve WORKFLOW_ID
navi workflow reject WORKFLOW_ID
navi workflow run WORKFLOW_ID
navi workflow resume WORKFLOW_ID
navi workflow verify WORKFLOW_ID
navi evolution list
navi evolution show EVENT_ID
navi evolution rollback EVENT_ID
navi service unit
navi service install
navi connectors list
navi connectors setup CONNECTOR
navi connectors run CONNECTOR
navi connectors status CONNECTOR
```

Current API surface:

```text
GET  /health
POST /v1/chat
GET  /v1/sessions
POST /v1/sessions
GET  /v1/session-aliases
GET  /v1/sessions/{session_id}
GET  /v1/memory
GET  /v1/memory/conflicts
POST /v1/memory
GET  /v1/skills
GET  /v1/delegations
POST /v1/delegations
PATCH /v1/delegations/{run_id}
GET  /v1/approvals
GET  /v1/watches
POST /v1/delegations/{run_id}/approve
POST /v1/delegations/process
POST /v1/active/delegations
POST /v1/active/approve
POST /v1/active/reject
POST /v1/active/watches
POST /v1/active/watches/process
GET  /v1/auth/status
GET  /v1/tools
POST /v1/tools/{tool_name}/call
GET  /v1/graph
GET  /v1/trust-rules
GET  /v1/traces
GET  /v1/traces/{trace_id}
GET  /v1/trace-evaluations
POST /v1/traces/{trace_id}/evaluate
GET  /v1/goals
GET  /v1/goals/{goal_id}
GET  /v1/subagents
GET  /v1/subagents/{subagent_id}
GET  /v1/workflows
POST /v1/workflows
GET  /v1/workflows/{workflow_id}
POST /v1/workflows/{workflow_id}/approve
POST /v1/workflows/{workflow_id}/reject
POST /v1/workflows/{workflow_id}/run
POST /v1/workflows/{workflow_id}/resume
POST /v1/workflows/{workflow_id}/verify
GET  /v1/evolution-events
POST /v1/evolution-events/{event_id}/rollback
GET  /v1/evolution-targets
GET  /v1/evolution-proposals
POST /v1/evolution-proposals
POST /v1/evolution-proposals/{proposal_id}/apply
POST /v1/evolution-proposals/{proposal_id}/evaluation
GET  /v1/connectors/weixin/status
```

`POST /v1/memory` accepts a governed memory item shape: `type`, `content`,
`scope`, `source`, `status`, `confidence`, and optional `metadata`. `GET
/v1/memory` returns structured `items`; memory is not exposed as a flat text
dump. `GET /v1/memory/conflicts` returns declared contradiction and
supersession relationships so stale or competing facts are visible to
operators and agents.

Lifecycle hooks are declared control-plane artifacts. Built-in hooks are loaded
from `src/navi/specs/hooks.yaml`; local hooks can be added as YAML files under
`NAVI_HOME/hooks/*.yaml` with `event`, optional `match`, `decision`, `reason`,
and structured `facts`. Local hooks do not execute arbitrary code.

Current config shape:

```yaml
model:
  provider: mock
  model: mock
  api_base_url: https://api.openai.com/v1
  api_key: ""
runtime:
  service_name: navi.service
  local_surface: local
  agent_step_budget: 8
execution:
  provider: navi
  timeout_seconds: 120.0
  mock: false
```

Environment overrides:

```text
NAVI_HOME
NAVI_MODEL_PROVIDER
NAVI_MODEL
NAVI_MODEL_API_BASE_URL
NAVI_MODEL_API_KEY
NAVI_SERVICE_NAME
NAVI_EXECUTION_TIMEOUT_SECONDS
NAVI_EXECUTION_MOCK
```

Connector-specific environment is owned by each connector adapter. The Weixin adapter currently accepts:

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
NAVI_WEIXIN_MOCK
NAVI_WEIXIN_MOCK_MESSAGE
```

## Current Implementation Status

Implemented:

- Python package scaffold and CLI.
- FastAPI app for headless local API clients.
- Mock and OpenAI-compatible provider shape.
- Bounded agent loop for observe/plan/read-tool chaining before final response.
- Tool Gateway abstraction with provider sources, refresh, filtering, and audit logs.
- Internal execution requires the structured `navi.actuator.v1` protocol. Protocol actions must be capability calls (`tool`, `permission`, `args`), are executed through `CapabilityRegistry`, and produce actual capability-result evidence; free-form execution output or non-capability actions are failed executions. Structured output constraints must be passed through provider/tool schema channels and must not be repeated as JSON shapes or field lists in business prompts. Completed execution is treated as a completion candidate until the critic gate verifies non-empty actuator evidence, successful verification status, and independent checks for mutating actions.
- Planner, executor, critic, and notification role executions are recorded as sub-agent runtime records with status and evidence separate from delegation rows. CLI and API consumers can inspect these records through `navi subagent list/show` and `/v1/subagents`.
- Governed dynamic workflows persist declarative orchestration plans, subagent steps, dependency-aware execution state, approval state, verifier evidence, and lifecycle events in `workflows.db`; they are exposed through `workflow.*` capabilities, `navi workflow ...`, and `/v1/workflows`.
- Local memory, session, and task stores.
- Skill discovery.
- Connector registry plus Weixin account store, context-token store, deduplication, policy checks, mock client, HTTP client skeleton, and inbound-to-agent service flow.
- Tests for config, runtime, memory, Weixin policy, deduplication, and context persistence.

Known gaps:

- Real iLink payloads/endpoints need calibration during a live QR-login test.
- `navi connectors setup weixin` currently polls QR status once; a production setup should loop with timeout and clearer scan/confirm states.
- Weixin media support is not implemented.
- Weixin typing indicators are not implemented.
- Remote connector tool visibility uses an inspectable connector tool policy with a permission ceiling, allowed tools, blocked capability classes, and audit facts. Richer per-sender/per-surface policy configuration is still future work.
- MCP servers are not connected yet; Tool Gateway is ready for an MCP provider. Action/control and gateway tools are exposed through the unified capability registry.
- Execution protocol is a bounded step plan. Each step contains capability-backed actions and verification checks; Navi keeps runtime budgets internal, can trigger bounded recovery for prepare-level follow-up work, supports retry-once/continue/stop failure policy, records workspace dirty-state before/after execution, emits rollback hints on failed dirty runs, and can check expected files, file contents, git status, and test results before marking execution complete. Provider policy must distinguish schema-enforced structured outputs, JSON-only modes, and strict function/tool calling; JSON-only modes may use only centralized provider-adapter compatibility hints and still require local validation plus bounded repair. Richer remote policy controls are still needed before exposing mutating actuators to connectors.
- Browser UI is intentionally removed from this codebase.

## Next Implementation Steps

Recommended next order:

1. Run a live `navi connectors setup weixin` against iLink and adjust QR/status response parsing.
2. Run `navi connectors run weixin` with a test DM and adjust `getupdates`/`sendmessage` payload parsing.
3. Add structured logging and visible diagnostics for Weixin connection states.
4. Add richer verifier policies for structured diffs, command-specific assertions, and automatic rollback proposals.
5. Add per-sender/per-surface remote tool policy configuration before enabling shell/file-write tools from Weixin.
6. Add workflow cost telemetry and richer long-running compaction.
7. Improve headless API observability for sessions, connector status, memory, and task list.
8. Add text chunking for long Weixin responses.
9. Add optional media handling after text DM is reliable.

## Verification Baseline

Before handoff, run:

```bash
cd navi
pytest -q
PYTHONPATH=src python -m compileall src tests
NAVI_HOME=/tmp/navi-smoke PYTHONPATH=src python -c "from navi.api import create_app; app=create_app(); print(app.title, len(app.routes))"
NAVI_HOME=/tmp/navi-smoke-weixin NAVI_WEIXIN_MOCK=true PYTHONPATH=src python -c "import asyncio; from navi.paths import ensure_home; from navi.connector_registry import get_connector_adapter; home=ensure_home(); adapter=get_connector_adapter('weixin'); print(asyncio.run(adapter.setup(home, 10, None)).splitlines()[0])"
```
