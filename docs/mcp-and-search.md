# MCP and web search

## Web search

`web.search` is a stable Navi capability. In the default `auto` mode it tries configured
SearXNG JSON endpoints first, then uses Exa's official Streamable HTTP MCP service. The old
DuckDuckGo HTML scraper is not a production fallback: bot challenges made it unreliable and
its unchanged failures could consume an entire retry budget.

Put configuration and secrets in `.navi/env` (the Navi home `env` file) or the process
environment:

```dotenv
NAVI_WEB_SEARCH_PROVIDER=auto
NAVI_WEB_SEARCH_SEARXNG_URL=https://search.example
NAVI_EXA_API_KEY=optional-production-key
```

Supported provider modes are `auto`, `searxng`, and `exa`. Exa's hosted MCP has a free tier
without a key; `NAVI_EXA_API_KEY` or `EXA_API_KEY` raises its production limits. Override the
hosted endpoint with `NAVI_WEB_SEARCH_EXA_MCP_URL` only when deliberately routing through a
compatible deployment.

Run `navi doctor` to inspect the effective provider without exposing secrets. Run
`navi doctor --connectivity` for a real one-result search probe.

Search failures include an `error_reason` and a generic `retryable` fact. A false value means
the same provider call should not be repeated unchanged. The durable loop still exposes the
failure to the planner, which may choose another capability or arguments, request missing
configuration, or explain the blocker within the remaining governed budget. Repeated unchanged
calls are bounded by the runtime's no-progress gate.

## MCP servers

Configure additional MCP servers in `.navi/mcp.json` using the common `mcpServers` shape.
Streamable HTTP and stdio are transports only; both enter the same capability registry and
the same permission, approval, audit, and redaction path.

```json
{
  "mcpServers": {
    "exa": {
      "url": "https://mcp.exa.ai/mcp",
      "headers": {"x-api-key": "${EXA_API_KEY}"},
      "permission": "network",
      "allowed_tools": ["web_search_exa", "web_fetch_exa"]
    },
    "workspace": {
      "command": "workspace-mcp-server",
      "args": ["--stdio"],
      "permission": "write"
    }
  }
}
```

Each enabled server exposes two governed capabilities:

- `mcp.<server>.tools` initializes the server and returns its current tool schemas.
- `mcp.<server>.call` calls one discovered tool with an arguments object.

The default permission for an MCP call is `write`, so an unclassified server cannot bypass
sensitive-operation approval. Lowering `permission` to `network` or `read` requires an
explicit `allowed_tools` list; later server additions remain unavailable until reviewed.
Server-supplied annotations remain discovery metadata and never lower Navi's configured
permission.

MCP prompts, resources, sampling, elicitation, and server-driven permission changes are not
enabled. This first client boundary intentionally supports tool discovery and tool calls only.
