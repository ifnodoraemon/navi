# 🧭 Navi

Navi is a **local-first agent OS for a governed personal AI assistant**.

It is designed to give the model scaffolding, not to hardcode work on the model's behalf. Navi exposes declared capabilities, durable memory, approval/governance state, connector affordances, traces, and recovery records so the assistant can decide from current facts while staying auditable and bounded.

Version `1.1.2` tightens remote connector execution boundaries, restores declared environment fact tools for governed delegation, and aligns workflow docs/evals with the current runtime contract.

[中文说明](README.zh-CN.md)

---

## ✨ Core Features

*   🧠 **Governed Memory**: Typed, scoped, provenance-bearing memory for preferences, constraints, negative learnings, facts, semantic records, conflicts, and revocation.
*   🧭 **Capability-Driven Agent Runtime**: The planner chooses declared syscalls from the capability manifest instead of relying on product keyword routing.
*   ✅ **Approval, Governance, and Goal State**: Local execution moves through durable task, watch, goal, approval, verifier, and recovery records.
*   🛡️ **Defense-in-Depth Safeguards**: Permission ceilings, connector tool policies, declarative capability risk metadata, untrusted observation boundaries, trace evaluation, and safeguard failure attribution.
*   🧩 **Governed Dynamic Workflows**: The model can decide when a task needs dynamic orchestration and propose declarative plans with subagent steps, dependencies, allowed tools, approval, resumable execution, and independent verification.
*   🔄 **Reversible Evolution Ledger**: Prompt, memory, skill, workflow, governance-policy, and eval changes are recorded as reviewable, rollbackable events.
*   🔌 **Skills, Plugins, and Hooks Boundaries**: Skills teach procedures, plugins add capabilities, and hooks observe or gate lifecycle events.
*   🌐 **Multi-Surface Access**:
    *   💻 **CLI Chat and Control Plane**: Interactive local chat plus memory, approval, evolution, trace, goal, prompt, hook, diagnostic, and connector commands.
    *   🔧 **Headless Local API**: FastAPI server for local clients (`navi api`).
    *   💬 **Connector Packages**: Weixin/iLink and Telegram adapters loaded through the connector registry and governed by remote-safe tool policy.

## v1 Contract

Navi 1.0.0 stabilizes the public agent OS contract:

- capability specs, permissions, and tool-call execution;
- task, watch, goal, approval, recovery, sub-agent, and trace records;
- governed memory item shape and conflict visibility;
- CLI and local API control surfaces;
- connector-safe ingress through explicit remote tool policy;
- release validation through unit tests, evals, compile checks, and traceable docs.

Internal compatibility debt is intentionally not preserved. Obsolete internal schemas, aliases, and workflow branches should be removed rather than silently adapted unless a migration is the explicit shipped feature.

Navi 1.1.0 adds dynamic workflows as a governed v1 extension. Workflow plans are data, not executable scripts: every step must call declared capabilities through the runtime and remains subject to approval, permission ceilings, allowed tools, subagent evidence, and verifier checks.

---

## 🛠️ Quick Start

Initialize and run Navi locally in seconds:

```bash
# 1. Clone & enter the repository
cd navi

# 2. Setup your virtual environment
python -m venv .venv
source .venv/bin/activate

# 3. Install in developer mode
pip install -e ".[dev]"

# 4. Start CLI chat
navi chat
```

### Run the Headless Local API

```bash
navi api
```

The local API requires `X-API-Key`. Set `NAVI_API_KEY` or read the generated
key from `NAVI_HOME/api_key` (`.navi/api_key` by default).

### Setup & Run Weixin Connector

```bash
# Scan the QR code to authorize the account, then long-poll for messages
navi connectors setup weixin
navi connectors run weixin --once
```

---

## ⚙️ Architecture & Local-First State

Navi uses **SQLite** as its default local database core. This decision is strictly aligned with our **local-first, developer-frictionless** philosophy:
1.  **Zero Configuration**: No database servers to install, configure, or maintain.
2.  **ACID Reliability**: Offers complete transactional safety and crash protection.
3.  **Maximum Performance**: Running in-process eliminates network latency entirely.
4.  **Durable Backups**: Backed up by the **Evolution Ledger** for 100% reversible rollbacks of memory and project graphs.

All local state is structured under `.navi/` or a custom `NAVI_HOME` directory:
```text
.navi/
├── config.yaml       # User configurations
├── env               # Environment overrides
├── evolution.db      # Evolution Ledger logs
├── graph.db          # Project dependency and context graph
├── memory.db         # Active cognitive memory & session stores
├── runs.db           # Tasks, watches, approvals, and execution state
├── goals.db          # Durable goal lifecycle state
├── traces.db         # Turn and capability trace records
├── subagents.db      # Planner/executor/critic/notification role records
├── workflows.db      # Governed dynamic workflow, step, and event state
└── skills/           # Custom procedural guides (SKILL.md)
```

---

## 💬 Command Line Interface

```bash
# Diagnostics
navi status
navi doctor
navi doctor --connectivity

# Tools, hooks, and prompts
navi tools list
navi hooks list
navi prompts inspect planner

# Active Memory
navi memory list
navi memory add preference "I prefer using Python 3.12" --reason "User stated this preference" --provenance "manual"
navi memory recall "python compilation preference"
navi memory revoke <item_id>

# Reversible Self-Evolution
navi evolution list
navi evolution proposals
navi evolution show <event_id>
navi evolution rollback <event_id>

# Session Management
navi session list
navi session new [alias]
navi session show <session_id>

# Skills
navi skills

# Goals, traces, and sub-agent evidence
navi goal list
navi trace list
navi subagent list
navi workflow list
navi workflow show <workflow_id>
```

The same diagnostic checks are available from the local API at `/v1/diagnostics`.
Use `/v1/diagnostics?connectivity=true` for short live API connectivity probes.

## Evaluations

Navi keeps core-flow evals close to daily user behavior:

```bash
# Tool routing coverage
navi eval delegations --dataset evals/delegation_cases.yaml

# User-visible daily journeys
navi eval daily --dataset evals/daily_journeys.yaml

# Claw-Eval style Pass^3 task suite for Navi core flows
navi eval claw --dataset evals/claw_navi.yaml --attempts 3
```

`evals/claw_navi.yaml` is a repository-local Claw-Eval compatible subset. It preserves the task/split/rubric/Pass^3 shape while avoiding large external fixtures in the repo.

## Canonical Docs

- [Current requirements](docs/requirements.md)
- [Non-negotiable principles](docs/principles.md)
- [Agentic optimization tracker](docs/agentic-optimization-tracker.md)
- [Dynamic workflows](docs/dynamic-workflows.md)
- [Prompt architecture](docs/prompt-architecture.md)
- [Versioning contract](docs/versioning.md)
- [Release notes](docs/release-notes.md)
