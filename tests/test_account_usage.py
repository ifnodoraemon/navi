from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from navi.account_usage import fetch_account_usage
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext


class _FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 8,
                    "reset_at": "2026-07-25T16:27:56Z",
                },
                "secondary_window": {
                    "used_percent": 40.5,
                    "reset_at": "2026-07-27T00:00:00Z",
                },
            },
            "credits": {"has_credits": True, "balance": 12.5},
        }


class _FakeClient:
    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, url: str, *, headers: dict[str, str]) -> _FakeResponse:
        assert url == "https://chatgpt.com/backend-api/wham/usage"
        assert headers["Authorization"] == "Bearer test-access-token"
        assert headers["User-Agent"] == "codex-cli"
        return _FakeResponse()


class _MalformedScalarResponse(_FakeResponse):
    def json(self) -> dict[str, Any]:
        return {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": ["unexpected"],
                    "reset_at": {"unexpected": True},
                }
            },
            "credits": {
                "has_credits": True,
                "balance": {"unexpected": True},
            },
        }


class _MalformedScalarClient(_FakeClient):
    def get(self, url: str, *, headers: dict[str, str]) -> _MalformedScalarResponse:
        super().get(url, headers=headers)
        return _MalformedScalarResponse()


def test_fetch_openai_codex_usage_preserves_provider_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "auth.json").write_text(
        """
        {
          "tokens": {
            "access_token": "test-access-token",
            "base_url": "https://chatgpt.com/backend-api/codex"
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr("navi.account_usage.httpx.Client", _FakeClient)

    snapshot = fetch_account_usage("openai-codex", home=tmp_path)
    facts = snapshot.to_facts()

    assert facts["available"] is True
    assert facts["provider"] == "openai-codex"
    assert facts["plan_type"] == "plus"
    assert facts["windows"][0]["window_id"] == "primary_window"
    assert facts["windows"][0]["remaining_percent"] == 92.0
    assert facts["windows"][1]["window_id"] == "secondary_window"
    assert facts["credits"] == {
        "has_credits": True,
        "unlimited": False,
        "balance": 12.5,
    }
    assert facts["evidence_contract"]["does_not_establish"] == [
        "future_request_acceptance",
        "future_usage",
        "provider_service_availability",
        "billing_state_beyond_snapshot",
    ]


def test_fetch_openai_codex_usage_treats_malformed_scalar_fields_as_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "auth.json").write_text(
        '{"tokens": {"access_token": "test-access-token"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr("navi.account_usage.httpx.Client", _MalformedScalarClient)

    facts = fetch_account_usage("openai-codex", home=tmp_path).to_facts()

    assert facts["available"] is True
    assert facts["windows"] == []
    assert facts["credits"] == {
        "has_credits": True,
        "unlimited": False,
    }


@pytest.mark.asyncio
async def test_account_usage_capability_is_visible_without_permission_filtering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "auth.json").write_text(
        '{"tokens": {"access_token": "test-access-token"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr("navi.account_usage.httpx.Client", _FakeClient)
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    assert "account.usage" in {
        spec.name for spec in registry.planner_specs()
    }
    assert "account.usage" in {
        spec.name for spec in registry.planner_specs()
    }
    result = await registry.invoke(
        "account.usage",
        {"provider": "openai-codex"},
        permission="network",
        context=CapabilityContext(home=tmp_path, permission_ceiling="network"),
    )

    assert result.ok is True
    assert result.facts["available"] is True
    assert result.facts["windows"][0]["remaining_percent"] == 92.0


@pytest.mark.asyncio
async def test_account_usage_requires_explicit_supported_provider(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    spec = registry.get("account.usage")

    assert spec is not None
    assert spec.input_schema["required"] == ["provider"]
    assert spec.input_schema["properties"]["provider"]["enum"] == ["openai-codex"]

    result = await registry.invoke(
        "account.usage",
        {},
        permission="network",
        context=CapabilityContext(home=tmp_path, permission_ceiling="network"),
    )

    assert result.ok is False
    assert result.error_reason == "schema_mismatch"


def test_account_usage_does_not_accept_provider_aliases(tmp_path: Path) -> None:
    snapshot = fetch_account_usage("codex", home=tmp_path)

    assert snapshot.available is False
    assert snapshot.provider == "codex"
    assert snapshot.unavailable_reason == "unsupported_provider"


def test_account_usage_does_not_treat_api_keys_as_codex_oauth_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "auth.json").write_text(
        '{"api_key": "must-not-be-used-as-oauth"}',
        encoding="utf-8",
    )
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    monkeypatch.setattr("navi.account_usage.Path.home", lambda: isolated_home)

    snapshot = fetch_account_usage("openai-codex", home=tmp_path)

    assert snapshot.available is False
    assert snapshot.auth_status == "not_configured"
    assert snapshot.unavailable_reason == "codex_oauth_token_not_found"
