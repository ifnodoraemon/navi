# Navi

Navi is a local-first personal AI assistant. The first version focuses on three entry points:

- CLI chat
- Local Web console
- Personal Weixin/WeChat connection through a QR-login, long-polling gateway shape

It intentionally does not implement a general multi-channel gateway in v1. Weixin DM is the first-class remote channel; group chat delivery depends on the upstream iLink account behavior and is not promised.

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
NAVI_WEIXIN_MOCK=true navi weixin setup
NAVI_WEIXIN_MOCK=true navi weixin run --once
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
```

Set `NAVI_HOME` to move the state directory.
