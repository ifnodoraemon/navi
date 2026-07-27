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


def test_fetch_openai_codex_usage_from_credential_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "auth.json").write_text(
        """
        {
          "credential_pool": {
            "openai-codex": [
              {
                "auth_type": "oauth",
                "access_token": "test-access-token",
                "source": "manual:device_code",
                "base_url": "https://chatgpt.com/backend-api/codex"
              }
            ]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr("navi.account_usage.httpx.Client", _FakeClient)

    snapshot = fetch_account_usage("codex", home=tmp_path)
    facts = snapshot.to_facts()

    assert facts["available"] is True
    assert facts["provider"] == "openai-codex"
    assert facts["plan"] == "Plus"
    assert facts["windows"][0]["label"] == "Session"
    assert facts["windows"][0]["remaining_percent"] == 92.0
    assert facts["windows"][1]["label"] == "Weekly"
    assert facts["details"] == ["Credits balance: $12.50"]


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
        spec.name for spec in registry.planner_specs(permission_ceiling="read")
    }
    assert "account.usage" in {
        spec.name for spec in registry.planner_specs(permission_ceiling="network")
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
