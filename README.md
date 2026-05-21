# Navi

Navi is a local-first personal AI assistant. The current version focuses on three entry points:

- CLI chat
- Local Web console
- Personal Weixin/WeChat connection through a connector adapter

Connectors are transport surfaces. The core runtime receives user text, loads declared tools, and lets the model plan the next tool call without channel-specific prompt behavior.

## Requirements

The product decisions, v1 scope, Weixin requirements, public interfaces, known gaps, and next implementation steps are documented in [docs/requirements.md](docs/requirements.md).

## Quick Start

```bash
cd navi
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
navi chat
```

Run the local API and Web console:

```bash
navi web
```

Mock a Weixin QR setup without calling the real iLink API:

```bash
NAVI_WEIXIN_MOCK=true navi connectors setup weixin
NAVI_WEIXIN_MOCK=true navi connectors run weixin --once
```

## Config

Navi stores local state under `.navi/` in the current workspace by default.

```yaml
model:
  provider: mock
  model: mock
weixin:
  enabled: true
  base_url: https://ilinkai.weixin.qq.com
  dm_policy: open
  group_policy: disabled
runtime:
  service_name: navi.service
  web_url: ""
execution:
  provider: codex
  timeout_seconds: 120.0
  mock: false
```

Set `NAVI_HOME` to move the state directory.
