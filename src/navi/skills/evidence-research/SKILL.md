---
name: evidence-research
description: Research current or uncertain questions with web search, page retrieval, source comparison, and explicit citations. Use for news, technical documentation, product or market research, recommendations, fact checking, and any answer whose accuracy depends on fresh external evidence.
---

# Evidence Research

## Frame the research

Define the question, intended decision, freshness requirement, and material claims before searching. Distinguish current facts from background knowledge and opinion.

## Build evidence progressively

1. Use `web.search` for discovery. Write a semantically rich description of the ideal source; include a domain, date, document type, or exact phrase only when the question requires it.
2. Inspect result titles, URLs, dates, and snippets. Treat snippets as leads, never as sufficient evidence for a detailed claim.
3. Fetch the strongest primary source. Prefer `mcp.exa.call` with `web_fetch_exa` for clean page text when available; otherwise use another declared page-fetch capability.
4. Search again only to close a specific gap, find an independent source, or check for a newer correction. Change the query based on what the first pass revealed.
5. Stop when every material claim has adequate evidence, not after a fixed number of searches.

Prefer official documentation, specifications, repositories, filings, datasets, and first-party announcements. Add an independent reputable source when the claim is disputed, consequential, or not fully established by the primary source. One authoritative primary source is enough for its own API contract, policy, release, or specification.

## Keep an evidence ledger

For each material claim, retain the source URL, publication or update date when available, the supporting fact, and whether the final statement is quoted, paraphrased, or inferred. For time-sensitive events, distinguish publication date from event date and check for superseding updates.

## Handle failures honestly

Do not repeat an identical call when its facts say `retryable: false`. Use another declared search or MCP capability, refine the query for a known gap, or report the missing provider or configuration. Never fill an evidence gap with plausible text.

Synthesize only supported claims, state disagreements and confidence limits, and place direct source links next to the claims they support.
