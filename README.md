# Navi

Navi is a local-first agent OS for a governed personal AI assistant.

It gives the model current facts and declared capabilities instead of encoding
product workflows in prompts. The model owns semantic decisions; the runtime
enforces schemas, permissions, approvals, workspace boundaries, side-effect
policy, persistence, and audit evidence.

[中文说明](README.zh-CN.md)

## Current Capabilities

- Capability-driven planning through a declared, schema-validated manifest.
- Durable goals, runs, approvals, loop checkpoints, and trace evidence.
- Typed memory with scope, provenance, confidence, conflicts, and revocation.
- Permission ceilings, durable approvals, hooks, resource gates, and shadow
  workspaces.
- CLI and authenticated local FastAPI surfaces.
- Weixin/iLink and Telegram adapters discovered through the connector registry.
- Prompt manifests and an evolution ledger for inspectable, reviewable changes.
- Scoped calendar, reminder, contact, mail-draft, and attention resources.
- Event-derived metrics/SLOs plus experiment and activation rollback evidence.

The current architecture and known boundary deviations are documented in
[Architecture](docs/architecture.md). Do not infer shipped behavior from old
plans or release narratives.

## Quick Start

Navi requires Python 3.13 or newer. On Linux, command execution and verifier
capabilities also require Bubblewrap (`bwrap`) so the OS sandbox can fail closed:

```bash
sudo apt-get install bubblewrap
```

Ubuntu 24.04 and newer restrict unprivileged user namespaces through AppArmor.
Install and load Ubuntu's Bubblewrap profile when that restriction is enabled:

```bash
sudo apt-get install apparmor-profiles
sudo apparmor_parser --replace /usr/share/apparmor/extra-profiles/bwrap-userns-restrict
```

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
navi chat
```

Run the authenticated local API:

```bash
navi api
```

API requests require `X-API-Key`. The key is `api.api_key` in
`NAVI_HOME/config.yaml` (`.navi/config.yaml` by default).

## Configuration

`.navi/config.yaml` is the only Navi runtime configuration file. `NAVI_HOME` is
the only bootstrap environment variable; it selects the directory and does not
override values in the file. Runtime, API, and connector startup fail closed
when the active configuration is invalid; diagnostic commands remain available
to report the exact validation facts. Inspect the effective configuration
without revealing secrets:

```bash
navi config
```

The global parameter groups are:

- `model`: `provider`, `model`, `api_base_url`, `api_key`, `kind`,
  `timeout_seconds`, explicit `response_transport` (`json` or `sse`), governed
  `request_options`, `routes`, and per-role `role_params`;
- `runtime`: `service_name`, `local_surface`;
- `execution`: `provider`, `timeout_seconds`;
- `api`: `host`, `port`, `api_key`;
- `search.providers.<name>`: `kind` (`exa_mcp`, `searxng`, or `x_api`),
  `enabled`, `endpoint`, `allow_private_network`, `mcp_server`,
  `bearer_token`, `categories`, `language`, `time_range`. The default
  instances are `exa`, `searxng`, and `x`; the planner selects an enabled
  instance on every `web.search` call;
- `connectors.telegram`: `enabled`, `bot_token`, `api_base_url`, `dm_policy`,
  `allowed_users`, `home_chat_id`;
- `connectors.weixin`: `enabled`, `account_id`, `token`, `base_url`,
  `cdn_base_url`, `dm_policy`, `allowed_users`, `group_policy`,
  `group_allowed_users`, `home_channel`;
- `mcp.servers.<name>`: `transport`, `url`, `command`, `args`, `env`, `headers`,
  `cwd`, `timeout_seconds`, `tool_permissions`, `enabled`. `tool_permissions`
  is the local allowlist and permission map for each exposed MCP tool.

The file is created with owner-only permissions. Legacy `.navi/env`,
`.navi/mcp.json`, and `.navi/api_key` files are rejected rather than merged.

## CLI

```bash
# Runtime and diagnostics
navi chat
navi status
navi metrics --json-output
navi doctor
navi doctor --connectivity
navi config
navi model

# Capabilities and prompts
navi tools list
navi hooks list
navi prompts inspect planner --json-output
navi skills

# Durable state
navi goal list
navi trace list
navi memory list
navi memory jobs --status dead_letter
navi memory retry-jobs <exact-job-id> --reason "root cause repaired and verified"
navi session list
navi evolution list

# Connectors and service operation
navi connectors list
navi connectors status weixin
navi service unit

# Evaluation datasets
navi eval claw --validate-only --dataset evals/claw_navi.yaml
```

Use `navi COMMAND --help` for the current subcommand contract.

## Connectors

Set up and run Weixin/iLink with:

```bash
navi connectors setup weixin
navi connectors run weixin --once
```

Live connector behavior depends on upstream payloads and credentials. Connector
tests use injected test doubles; Navi does not expose a runtime fake mode.

## Local State

Navi stores local state under `.navi/` or a custom `NAVI_HOME`. Separate SQLite
stores currently hold runs and approvals, goals, loop checkpoints, traces,
memory, graph data, evolution records, and connector state. See
[Architecture](docs/architecture.md) for consistency boundaries and known gaps.

## Verification

```bash
pytest -q
ruff check src tests
python -m compileall -q src/navi
python -m build
```

Live model tests are opt-in and require a configured provider credential.

## Canonical Docs

Docs are intentionally limited to the long-lived contract set below. Current
CLI/API behavior comes from code and `--help`; completed plans, incident notes,
and topic checklists should be folded into these files or deleted.

- [Product requirements](docs/requirements.md)
- [Non-negotiable principles](docs/principles.md)
- [Current architecture](docs/architecture.md)
