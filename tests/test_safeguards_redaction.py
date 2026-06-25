"""Regression coverage for secret redaction (principle 13/16).

Covers the secret-bearing formats that previously slipped through the
keyword-prefix ``_SECRET_PATTERNS``: PEM private keys and connection
strings with embedded credentials. Also guards against false positives
on ordinary URLs and ``user@host`` addresses.
"""
import asyncio
from pathlib import Path

from navi.capabilities import CapabilityContext, build_capability_registry
from navi.runs import RunStore
from navi.safeguards import redact_secrets, redact_secrets_deep
from navi.tools import API_CONTEXT


def test_redacts_bearer_authorization_header():
    redacted = redact_secrets("Authorization: Bearer abc123")
    assert "abc123" not in redacted
    assert "[REDACTED]" in redacted


def test_redacts_connection_string_credentials():
    # ``scheme://user:pass@host`` must redact the password portion.
    assert redact_secrets("postgresql://user:secret@localhost:5432/db") == (
        "postgresql://user:[REDACTED]@localhost:5432/db"
    )
    assert redact_secrets("mongodb://admin:p%40ss@host:27017") == (
        "mongodb://admin:[REDACTED]@host:27017"
    )


def test_redacts_pem_private_key_block():
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANB\n"
        "-----END PRIVATE KEY-----"
    )
    redacted = redact_secrets(pem)
    assert "MIIEvQIBADANB" not in redacted
    assert "[REDACTED]" in redacted


def test_no_false_positive_on_plain_urls_and_addresses():
    safe_strings = [
        "https://example.com/path",
        "user@host",
        "a@b.c",
        "count=5",
        "hello world",
    ]
    for text in safe_strings:
        assert redact_secrets(text) == text, f"false positive: {text!r}"


def test_deep_redaction_handles_nested_connection_string():
    payload = {
        "api_key": "sk-123",
        "config": {"db": "postgres://u:p@host"},
        "name": "project",
    }
    redacted = redact_secrets_deep(payload)
    assert redacted["api_key"] == "[REDACTED]"
    assert "p@host" not in redacted["config"]["db"]
    assert redacted["name"] == "project"


def test_action_capability_audit_log_redacts_args(tmp_path: Path):
    async def run() -> str:
        registry = build_capability_registry(
            tmp_path,
            project_dir=tmp_path,
            execution_context=API_CONTEXT,
        )
        context = CapabilityContext(
            home=tmp_path,
            peer_id="local",
            sender_id="local",
            source="api",
            workspace=str(tmp_path),
        )
        await registry.invoke(
            "memory.add",
            {
                "type": "fact",
                "content": "api_key=sk-action-secret-123",
                "reason": "audit regression",
                "provenance": "test",
            },
            permission="write",
            context=context,
        )
        return RunStore(tmp_path).list_tool_call_logs(limit=1)[0].args_json

    args_json = asyncio.run(run())
    assert "sk-action-secret-123" not in args_json
    assert "api_key=[REDACTED]" in args_json
