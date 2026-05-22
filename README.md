# 🧭 Navi (Navi Assistant)

Navi is a premium, **local-first personal AI assistant** designed to be your private "Jarvis". Built for developers and power users, Navi runs fully on your local machine, utilizing a lightweight, zero-configuration stack with model-driven intelligence, progressive capabilities, and an active cognitive memory engine.

[中文说明](README.zh-CN.md)

---

## ✨ Core Features

*   🧠 **Cognitive Active Memory (Jarvis Memory)**: Supports typed, scoped, and provenance-bearing memory stores (`preference`, `constraint`, `negative`, `fact`, `semantic`). It automatically extracts facts and user preferences from live conversations and background task logs, and performs natural-language active consolidation (updating and revoking contradictory memories) on the fly.
*   🔄 **Reversible Evolution Ledger**: Every self-evolution step, memory consolidation, skill addition, or trust engine mutation is recorded as a fully auditable ledger event. Any change can be completely rolled back at any time via the CLI or API.
*   🔌 **progressive Skill Discovery**: Seamlessly loads custom skills and procedural guides from standard markdown files (`.navi/skills/*/SKILL.md`) directly into the prompt context based on current permission ceilings.
*   🛡️ **Trust & Autonomy Engine**: A dynamic sandbox governance system that increments/decrements autonomy levels based on historical task successes and failures, safely managing local execution.
*   🌐 **Multi-Surface Access**:
    *   💻 **CLI Chat**: Real-time interactive terminal chat (`navi chat`).
    *   🖥️ **Local Web Console**: Web UI and FastAPI server (`navi web`).
    *   💬 **Personal WeChat Connector**: Direct long-poll connection gateway to your personal WeChat account.

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

### Run the Web Console & API

```bash
navi web
```

### Setup & Run Weixin Connector

```bash
# Set mock environment for local testing
NAVI_WEIXIN_MOCK=true navi connectors setup weixin
NAVI_WEIXIN_MOCK=true navi connectors run weixin --once
```

---

## ⚙️ Architecture & Local-First Database

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
├── tasks.db          # Scheduled and queued background tasks
├── trust.db          # Trust engine states
└── skills/           # Custom procedural guides (SKILL.md)
```

---

## 💬 Command Line Interface

```bash
# Active Memory
navi memory list
navi memory add preference "I prefer using Python 3.12"
navi memory recall "python compilation preference"
navi memory revoke <item_id>

# Reversible Self-Evolution
navi evolution list
navi evolution show <event_id>
navi evolution rollback <event_id>

# Session Management
navi session list
navi session new [alias]
navi session show <session_id>

# Skills & Trust Rules
navi skills
navi trust list
navi trust set <rule_id> <autonomy_level>
```
