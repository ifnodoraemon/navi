---
name: evidence-research
description: Research current or uncertain questions with web search, page retrieval, source comparison, and explicit citations. Use for news, technical documentation, product or market research, recommendations, fact checking, and any answer whose accuracy depends on fresh external evidence.
---

# Evidence Research

1. Define the question, decision, freshness requirement, and claims that need evidence before searching.
2. Start with one focused query. Refine with domain, date, file type, or exact-phrase constraints only when the first result set exposes a concrete gap.
3. Treat search snippets as discovery facts, not final evidence. Read the strongest result with an available fetch capability before relying on its claims.
4. Prefer primary sources: official documentation, specifications, repositories, filings, datasets, or first-party announcements. Use reputable secondary sources to add context or independent confirmation.
5. Cross-check material claims with two independent sources when practical. One authoritative primary source is sufficient for its own API contract, policy, release, or specification.
6. Track each supported claim with source URL, publication or update date when available, and the exact fact it supports. Separate source statements from your own inference.
7. For time-sensitive questions, compare publication date with the date the event occurred and search specifically for later corrections or superseding releases.
8. Stop repeating an identical capability call when its facts say `retryable: false`. Discover another configured search or MCP capability, change the query based on evidence, or report the missing provider/configuration plainly.
9. Synthesize only what the evidence supports. State disagreements, missing evidence, and confidence limits instead of filling gaps with plausible text.
10. Put citations next to the claims they support and link directly to the source page, not a search-results page.
