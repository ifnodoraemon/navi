---
name: Web Research Crawler
description: Guidance for collecting web evidence, crawling public pages responsibly, extracting structured facts, and building source-backed summaries.
permission: read
source: local
tags: [web, crawling, research, extraction]
---

# Web Research Crawler Skill

Use this skill when the user asks to gather information from public web pages, compare sources, monitor pages, extract structured data, or build a research dataset.

1. Confirm whether current information is required. If the answer may have changed, use a live source instead of memory.
2. Prefer primary sources: official docs, source repositories, standards, changelogs, papers, or owner-published pages.
3. Crawl politely and narrowly. Limit scope to the named domain or task, avoid login walls unless the user has configured a connector, and do not bypass access controls.
4. Extract structured fields with stable selectors, parsers, or documented APIs when available. Avoid brittle ad hoc scraping when a reliable API exists.
5. Track provenance for every extracted fact: URL, retrieved time, title or record id, and transformation applied.
6. For repeated monitoring, create a watch or eval around the user-visible result rather than silently running broad background crawls.
