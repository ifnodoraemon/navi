# External Agent Risk Watchlist

This file tracks failure modes observed in adjacent agent systems and the Navi
countermeasures we should keep explicit in design and evals.

## Hermes-Agent

Sources:

- https://github.com/NousResearch/hermes-agent/issues/29005
- https://github.com/NousResearch/hermes-agent/issues/28984
- https://github.com/NousResearch/hermes-agent/pull/29006

Observed risks:

- Connector liveness can diverge from process liveness. A gateway process may
  stay alive while a platform adapter has exhausted reconnect retries and stopped
  delivering messages.
- Config-to-runtime bindings can silently drift. Declaring a config field is not
  enough; every active consumer path must prove it reads the field.
- Path parity bugs recur when startup, restart, fallback, and model-switch paths
  implement similar logic separately.
- Hook and state payloads drift when protocol fields are added but not propagated
  through every call site.
- Fixed terminal polling creates a per-tool latency floor that compounds across
  multi-tool turns.

Navi countermeasures:

- Connector status tools must report degraded/retrying/fatal states, not only
  process health.
- Config, provider routes, connector specs, prompt layers, and tool manifests
  need typed contracts plus startup validation.
- Shared runtime entry points are preferred over copied startup/restart/fallback
  code paths.
- Hook-like extension points should pass typed payload objects instead of loose
  kwargs or dict fragments.
- Tool execution should use adaptive polling and record per-tool duration.

## OpenClaw

Sources:

- https://arxiv.org/abs/2603.27517
- https://www.pcworld.com/article/3064874/openclaw-ai-is-going-viral-dont-install-it.html
- https://www.techradar.com/pro/what-are-openclaw-skills-a-detailed-guide

Observed risks:

- Always-on local agents with messaging access and broad filesystem permissions
  have a large blast radius.
- Markdown memory and instruction files are easy to inspect, but they are also
  behavior-driving state and must be protected from prompt-injection and
  untrusted writes.
- Background heartbeat activity can turn small intent drift into repeated
  destructive actions.
- Third-party skills/plugins are supply-chain inputs. Natural-language skill
  files still define executable reach.
- Security analysis found cross-layer attack composition risks across gateway,
  node-host execution, policy, sandbox, browser, plugin, and agent/prompt layers.

Navi countermeasures:

- Keep permissions layered by surface, sender, source, tool, and autonomy level.
- Treat skills as untrusted executable capability manifests until verified.
- Scope skills and tools by workspace and agent role; avoid global capability
  loading unless explicitly intended.
- Use explicit task/watch lifecycle records and auditable execution/tool logs.
- Run background watch executions through the same governance and approval path
  as foreground requests.
- Add eval cases for prompt-injection-like, broad-permission, connector-liveness,
  and config-drift requests before changing the runtime.
