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
- Permission ceilings, connector allowlists, hooks, resource gates, and shadow
  workspaces.
- CLI and authenticated local FastAPI surfaces.
- Weixin/iLink and Telegram adapters discovered through the connector registry.
- Prompt manifests and an evolution ledger for inspectable, reviewable changes.

The current architecture and known boundary deviations are documented in
[Architecture](docs/architecture.md). Do not infer shipped behavior from old
plans or release narratives.

## Quick Start

Navi requires Python 3.13 or newer.

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

API requests require `X-API-Key`. Set `NAVI_API_KEY` or read the generated key
from `NAVI_HOME/api_key` (`.navi/api_key` by default).

## CLI

```bash
# Runtime and diagnostics
navi chat
navi status
navi doctor
navi doctor --connectivity
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

- [Product requirements](docs/requirements.md)
- [Non-negotiable principles](docs/principles.md)
- [Current architecture](docs/architecture.md)
- [Prompt architecture](docs/prompt-architecture.md)
