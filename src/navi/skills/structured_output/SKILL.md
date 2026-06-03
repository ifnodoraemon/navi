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
4. When summarizing observations, preserve exact ids, statuses, timestamps, tool names, and error strings.
5. For model prompts that require JSON, ask for one JSON object only, no markdown fences, and include a minimal schema example.
6. Add parser or eval coverage for malformed output if a schema failure has already happened in a real run.
