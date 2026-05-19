# Navi Requirements

This document captures the current product and implementation decisions so the next development session can continue without re-litigating the basics.

## Product Direction

Navi is a local-first personal AI assistant, inspired by Hermes and OpenClaw, but it is not a clone of either project.

Non-negotiable engineering principles are captured in [principles.md](principles.md). Requirements and implementation choices must not violate those principles.

Core positioning:

- Personal assistant first, not an enterprise RAG product.
- Local-first runtime and state.
- CLI and local Web are the developer/control surfaces.
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
- Local Web/API: `navi web`
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

- Provide `navi weixin setup` for QR-login setup.
- Provide `navi weixin run` for long-poll message processing.
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
- `navi.api`: FastAPI local API and Web entry.
- `navi.cli`: Typer CLI entrypoint.
- `navi.weixin`: Weixin client, store, models, and service.

Runtime rules:

- Missing real model credentials should fail clearly for real providers.
- Mock provider is allowed for local development and tests.
- The browser/local Web should not hold secrets.
- Weixin credentials should be persisted with restrictive file permissions when the OS allows it.
- Any future dangerous tools, especially shell/file write tools, must require an approval policy before being available to remote Weixin messages.
- Long-context operation must reload durable constraints, trust state, approvals, and relevant memory from stores before execution; it must not rely only on the current prompt window or a lossy summary.
- Memory implementation should evolve toward typed, scoped, provenance-bearing stores: working, constraint, episodic, semantic, procedural, preference, negative, and skill memory.
- Memory retrieval must be goal-directed and explainable; semantic similarity alone is not a sufficient recall policy.
- Conversation sessions must be explicit state. Long-running connectors need a way to start a fresh session without deleting old transcripts, otherwise topic drift and stale local context will pollute future answers.
- Connector plain messages must pass through intent/tool selection before general chat. High-confidence local action or schedule intents should call the relevant task/watch/fact tool directly instead of asking the user to rephrase as a command.
- Intent selection must be capability-driven, not keyword-driven as product behavior. Deterministic parsers are acceptable only for narrow structured facts such as ids, times, and explicit command syntax.
- Deterministic routing must not invent missing facts such as default times, paths, service names, task ids, or permissions. If a capability can be used but required slots are missing, the agent should ask a concise clarification.
- Slash commands are explicit structured syntax, not the primary agent behavior. Their grammar must stay orthogonal: `/task create|show|list`, `/watch create|list`, `/approval approve|reject|list`, and connector-local `/session new|current`.
- Extension boundaries must be explicit: skills provide promptable procedures, plugins provide installed capabilities/integrations, and hooks observe or gate lifecycle events.
- Anything with credentials, network calls, filesystem mutation, daemon behavior, providers, or connector surfaces must be a plugin rather than a skill.
- Anything that runs at task/message/tool/approval/memory lifecycle boundaries must be a hook rather than hidden inline logic.

## Public Interfaces

Current CLI surface:

```bash
navi chat
navi web
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
navi evolution list
navi evolution show EVENT_ID
navi evolution rollback EVENT_ID
navi service unit
navi service install
navi weixin setup
navi weixin run
navi weixin status
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
POST /v1/memory
GET  /v1/skills
GET  /v1/tasks
POST /v1/tasks
PATCH /v1/tasks/{task_id}
GET  /v1/approvals
GET  /v1/watches
POST /v1/tasks/{task_id}/approve
POST /v1/tasks/process
POST /v1/active/tasks
POST /v1/active/approve
POST /v1/active/reject
POST /v1/active/watches
POST /v1/active/watches/process
GET  /v1/auth/status
GET  /v1/tools
POST /v1/tools/{tool_name}/call
GET  /v1/graph
GET  /v1/trust-rules
GET  /v1/evolution-events
POST /v1/evolution-events/{event_id}/rollback
GET  /v1/weixin/status
GET  /
```

Current config shape:

```yaml
model:
  provider: mock
  model: mock
  api_base_url: https://api.openai.com/v1
  api_key: ""
weixin:
  enabled: false
  account_id: ""
  token: ""
  base_url: https://ilinkai.weixin.qq.com
  dm_policy: open
  allowed_users: []
  group_policy: disabled
  group_allowed_users: []
  home_channel: ""
runtime:
  service_name: navi.service
  web_url: ""
```

Environment overrides:

```text
NAVI_HOME
NAVI_MODEL_PROVIDER
NAVI_MODEL
NAVI_MODEL_API_BASE_URL
NAVI_MODEL_API_KEY
NAVI_WEIXIN_ENABLED
WEIXIN_ACCOUNT_ID
WEIXIN_TOKEN
WEIXIN_BASE_URL
WEIXIN_DM_POLICY
WEIXIN_ALLOWED_USERS
WEIXIN_GROUP_POLICY
WEIXIN_GROUP_ALLOWED_USERS
WEIXIN_HOME_CHANNEL
NAVI_SERVICE_NAME
NAVI_WEB_URL
NAVI_WEIXIN_MOCK
NAVI_WEIXIN_MOCK_MESSAGE
```

## Current Implementation Status

Implemented:

- Python package scaffold and CLI.
- FastAPI app and simple local Web console.
- Mock and OpenAI-compatible provider shape.
- Local memory/session/task stores.
- Skill discovery.
- Weixin account store, context-token store, deduplication, policy checks, mock client, HTTP client skeleton, and inbound-to-agent service flow.
- Tests for config, runtime, memory, Weixin policy, deduplication, and context persistence.

Known gaps:

- Real iLink payloads/endpoints need calibration during a live QR-login test.
- `navi weixin setup` currently polls QR status once; a production setup should loop with timeout and clearer scan/confirm states.
- Weixin media support is not implemented.
- Weixin typing indicators are not implemented.
- No remote-message tool approval policy exists yet.
- Web UI is intentionally minimal.

## Next Implementation Steps

Recommended next order:

1. Run a live `navi weixin setup` against iLink and adjust QR/status response parsing.
2. Run `navi weixin run` with a test DM and adjust `getupdates`/`sendmessage` payload parsing.
3. Add structured logging and visible diagnostics for Weixin connection states.
4. Add a remote-safe tool policy before enabling shell/file-write tools from Weixin.
5. Improve local Web to show sessions, Weixin status, memory, and task list.
6. Add text chunking for long Weixin responses.
7. Add optional media handling after text DM is reliable.

## Verification Baseline

Before handoff, run:

```bash
cd navi
pytest -q
PYTHONPATH=src python -m compileall src tests
NAVI_HOME=/tmp/navi-smoke PYTHONPATH=src python -c "from navi.api import create_app; app=create_app(); print(app.title, len(app.routes))"
NAVI_HOME=/tmp/navi-smoke-weixin NAVI_WEIXIN_MOCK=true PYTHONPATH=src python -c "import asyncio; from navi.paths import ensure_home; from navi.config import load_config; from navi.app_factory import build_runtime; from navi.weixin.service import WeixinService; home=ensure_home(); svc=WeixinService(home=home, config=load_config(home).weixin, runtime=build_runtime(home)); print(asyncio.run(svc.setup()).splitlines()[0])"
```
