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
from navi.safeguards import (
    canonical_approval_args_json,
    redact_personal_data,
    redact_personal_data_deep,
    redact_secrets,
    redact_secrets_deep,
)
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


def test_personal_data_redaction_masks_contact_identifiers():
    text = "电话 15709610082 邮箱 ifnodoraemon@example.com"
    redacted = redact_personal_data(text)

    assert "15709610082" not in redacted
    assert "ifnodoraemon@example.com" not in redacted
    assert "[REDACTED_PHONE]" in redacted
    assert "[REDACTED_EMAIL]" in redacted


def test_personal_data_redaction_masks_separated_phone_and_nested_fields():
    payload = {
        "contact": "call 138-0013-8000 or 138 0013 8000",
        "profile": {"phone": "13800138000", "note": "mail a@example.com"},
    }

    redacted = redact_personal_data_deep(payload)

    assert "138" not in redacted["contact"]
    assert redacted["profile"]["phone"] == "[REDACTED_PERSONAL_DATA]"
    assert "a@example.com" not in redacted["profile"]["note"]


def test_verification_code_is_redacted_from_nested_audit_facts():
    redacted = redact_personal_data_deep({"verification_code": "123456"})

    assert redacted["verification_code"] == "[REDACTED]"


def test_approval_args_use_stable_hmac_for_personal_and_secret_values(tmp_path: Path):
    first = canonical_approval_args_json(
        {
            "other_peer_id": "private-peer",
            "email": "a@example.com",
            "api_key": "secret-one",
            "operation": "link",
        },
        home=tmp_path,
    )
    repeated = canonical_approval_args_json(
        {
            "other_peer_id": "private-peer",
            "email": "a@example.com",
            "api_key": "secret-one",
            "operation": "link",
        },
        home=tmp_path,
    )
    changed = canonical_approval_args_json(
        {
            "other_peer_id": "different-peer",
            "email": "b@example.com",
            "api_key": "secret-two",
            "operation": "link",
        },
        home=tmp_path,
    )

    assert first == repeated
    assert first != changed
    assert "private-peer" not in first
    assert "a@example.com" not in first
    assert "secret-one" not in first
    assert "$approval_hmac" in first




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
