from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


@dataclass(frozen=True)
class AccountUsageWindow:
    label: str
    used_percent: float | None = None
    reset_at: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        remaining_percent = (
            None
            if self.used_percent is None
            else max(0.0, min(100.0, 100.0 - float(self.used_percent)))
        )
        return {
            "label": self.label,
            "used_percent": self.used_percent,
            "remaining_percent": remaining_percent,
            "reset_at": self.reset_at,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AccountUsageSnapshot:
    provider: str
    source: str
    fetched_at: str
    plan: str = ""
    windows: tuple[AccountUsageWindow, ...] = ()
    details: tuple[str, ...] = ()
    unavailable_reason: str = ""
    auth_status: str = ""

    @property
    def available(self) -> bool:
        return not self.unavailable_reason and bool(self.windows or self.details)

    def to_facts(self) -> dict[str, Any]:
        return {
            "entity_type": "account_usage",
            "entity_id": self.provider,
            "state_transition": "retrieved" if self.available else "unavailable",
            "turn_scope": "current",
            "provider": self.provider,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "available": self.available,
            "plan": self.plan,
            "auth_status": self.auth_status,
            "windows": [window.to_dict() for window in self.windows],
            "details": list(self.details),
            "unavailable_reason": self.unavailable_reason,
        }


def fetch_account_usage(
    provider: str,
    *,
    home: Path,
    timeout_seconds: float = 15.0,
) -> AccountUsageSnapshot:
    normalized = str(provider or "").strip().lower().replace("_", "-")
    if normalized in {"codex", "openai-codex", "openai-codex-oauth"}:
        return _fetch_codex_account_usage(home=home, timeout_seconds=timeout_seconds)
    return AccountUsageSnapshot(
        provider=normalized or "unknown",
        source="account_usage",
        fetched_at=_utc_now_iso(),
        unavailable_reason="unsupported_provider",
    )


def _fetch_codex_account_usage(*, home: Path, timeout_seconds: float) -> AccountUsageSnapshot:
    auth = _resolve_codex_credentials(home)
    if not auth.get("access_token"):
        return AccountUsageSnapshot(
            provider="openai-codex",
            source=str(auth.get("source") or "codex_oauth"),
            fetched_at=_utc_now_iso(),
            auth_status="not_configured",
            unavailable_reason=str(auth.get("unavailable_reason") or "codex_oauth_not_configured"),
        )

    headers = {
        "Authorization": f"Bearer {auth['access_token']}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    account_id = str(auth.get("account_id") or "").strip()
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    url = _codex_usage_url(str(auth.get("base_url") or DEFAULT_CODEX_BASE_URL))
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json() or {}
    except httpx.HTTPStatusError as exc:
        return AccountUsageSnapshot(
            provider="openai-codex",
            source=str(auth.get("source") or "codex_oauth"),
            fetched_at=_utc_now_iso(),
            auth_status="configured",
            unavailable_reason=f"http_status_{exc.response.status_code}",
        )
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return AccountUsageSnapshot(
            provider="openai-codex",
            source=str(auth.get("source") or "codex_oauth"),
            fetched_at=_utc_now_iso(),
            auth_status="configured",
            unavailable_reason=type(exc).__name__,
        )

    windows: list[AccountUsageWindow] = []
    rate_limit = payload.get("rate_limit") if isinstance(payload, dict) else {}
    if isinstance(rate_limit, dict):
        for key, label in (("primary_window", "Session"), ("secondary_window", "Weekly")):
            window = rate_limit.get(key)
            if not isinstance(window, dict):
                continue
            used = _float_or_none(window.get("used_percent"))
            if used is None:
                continue
            windows.append(
                AccountUsageWindow(
                    label=label,
                    used_percent=used,
                    reset_at=_iso_from_any(window.get("reset_at")),
                )
            )

    details: list[str] = []
    credits = payload.get("credits") if isinstance(payload, dict) else {}
    if isinstance(credits, dict) and credits.get("has_credits"):
        balance = credits.get("balance")
        if isinstance(balance, (int, float)) and not isinstance(balance, bool):
            details.append(f"Credits balance: ${float(balance):.2f}")
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")

    return AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=_utc_now_iso(),
        plan=_title_case(payload.get("plan_type") if isinstance(payload, dict) else ""),
        windows=tuple(windows),
        details=tuple(details),
        auth_status="configured",
        unavailable_reason="" if windows or details else "usage_payload_empty",
    )


def _resolve_codex_credentials(home: Path) -> dict[str, Any]:
    candidates = [
        home / "auth.json",
        Path.home() / ".codex" / "auth.json",
        Path.home() / ".hermes" / "auth.json",
    ]
    for path in candidates:
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        resolved = _extract_codex_token(data)
        if resolved.get("access_token"):
            return {
                **resolved,
                "source": str(path),
                "base_url": resolved.get("base_url") or DEFAULT_CODEX_BASE_URL,
            }
    return {"unavailable_reason": "codex_oauth_token_not_found"}


def _extract_codex_token(data: dict[str, Any]) -> dict[str, Any]:
    direct = _token_from_mapping(data)
    if direct:
        return direct

    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        found = _token_from_mapping(tokens)
        if found:
            return found

    providers = data.get("providers")
    if isinstance(providers, dict):
        for key in ("openai-codex", "codex", "openai_codex"):
            value = providers.get(key)
            if isinstance(value, dict):
                found = _token_from_mapping(value)
                if found:
                    return found

    credentials = data.get("credentials")
    if isinstance(credentials, dict):
        for key in ("openai-codex", "codex", "openai_codex"):
            value = credentials.get(key)
            if isinstance(value, dict):
                found = _token_from_mapping(value)
                if found:
                    return found
    if isinstance(credentials, list):
        for item in credentials:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or item.get("name") or "").lower()
            if provider not in {"openai-codex", "codex", "openai_codex"}:
                continue
            found = _token_from_mapping(item)
            if found:
                return found

    credential_pool = data.get("credential_pool")
    if isinstance(credential_pool, dict):
        for key in ("openai-codex", "codex", "openai_codex"):
            value = credential_pool.get(key)
            if isinstance(value, dict):
                found = _token_from_mapping(value)
                if found:
                    return found
            if isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    found = _token_from_mapping(item)
                    if found:
                        return found

    return {}


def _token_from_mapping(data: dict[str, Any]) -> dict[str, Any]:
    token = (
        data.get("access_token")
        or data.get("api_key")
        or data.get("runtime_api_key")
        or data.get("token")
    )
    token_text = str(token or "").strip()
    if not token_text:
        return {}
    return {
        "access_token": token_text,
        "account_id": str(data.get("account_id") or data.get("chatgpt_account_id") or "").strip(),
        "base_url": str(data.get("base_url") or data.get("runtime_base_url") or "").strip(),
        "expires_at": data.get("expires_at"),
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _codex_usage_url(base_url: str) -> str:
    normalized = (base_url or DEFAULT_CODEX_BASE_URL).strip().rstrip("/")
    if normalized.endswith("/codex"):
        normalized = normalized[: -len("/codex")]
    if "/backend-api" in normalized:
        return normalized + "/wham/usage"
    return normalized + "/api/codex/usage"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_any(value: Any) -> str:
    if value in {None, ""}:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    text = str(value).strip()
    if not text:
        return ""
    return text[:-1] + "+00:00" if text.endswith("Z") else text


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _title_case(value: Any) -> str:
    text = str(value or "").strip()
    return text.replace("_", " ").replace("-", " ").title() if text else ""
