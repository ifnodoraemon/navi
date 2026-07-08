---
name: Structured Output
description: Practices for producing and validating strict JSON, schemas, tool arguments, and machine-readable plans.
permission: read
source: local
tags: [json, schema, tool-calling]
---

# Structured Output Skill

Use this skill when a task depends on valid JSON, typed arguments, schemas, execution plans, eval datasets, or tool-call contracts.

1. Treat the declared schema as authoritative. Do not add undeclared keys unless the schema explicitly allows extension data.
2. Prefer small objects with stable field names over free-form prose that downstream code must parse.
3. Validate required fields, enum values, and permission levels before calling a tool or writing a dataset.
4. When summarizing facts, preserve exact ids, statuses, timestamps, tool names, and error strings.
5. For model calls that require machine-readable output, pass the schema through the provider-native output channel: OpenAI `response_format` JSON schema, Anthropic forced tool `input_schema`, or another provider's equivalent tool/function schema; keep business prompts semantic and do not add JSON shapes, field lists, or markdown-fence formatting rules.
6. When a provider only supports JSON syntax mode, keep any API-required compatibility hint centralized in the provider adapter, then validate against Navi's schema and use bounded repair or a visible failure.
7. Add parser or eval coverage for malformed output if a schema failure has already happened in a real run.
