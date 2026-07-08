"""Web search provider handlers."""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from navi.capability_contract import CAPABILITY_ERROR_REASON_KEY

from ..tools import ToolResult
from .utils import _positive_int, _truncate_output

_SEARCH_USER_AGENT = "Navi/1.0"
_WEB_SEARCH_PROVIDER_SEARXNG = "searxng"
_WEB_SEARCH_PROVIDER_DDG = "duckduckgo"
_DDG_SEARCH_URL = "https://html.duckduckgo.com/html/"
_DDG_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _web_search(args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(
            tool="web.search",
            ok=False,
            error="query is required",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "missing_required_argument",
                "provider": "web.search",
            },
        )

    limit = _positive_int(args.get("limit"), default=5, maximum=10)
    provider = str(os.environ.get("NAVI_WEB_SEARCH_PROVIDER") or "auto").strip().lower()
    provider_errors: list[dict[str, Any]] = []

    if provider in {"auto", _WEB_SEARCH_PROVIDER_SEARXNG, "searxng_json"}:
        searxng_endpoints = _searxng_endpoints()
        if not searxng_endpoints and provider in {_WEB_SEARCH_PROVIDER_SEARXNG, "searxng_json"}:
            return ToolResult(
                tool="web.search",
                ok=False,
                error="NAVI_WEB_SEARCH_SEARXNG_URL or NAVI_WEB_SEARCH_SEARXNG_URLS is required",
                facts={
                    CAPABILITY_ERROR_REASON_KEY: "search_provider_config_error",
                    "query": query,
                    "provider": _WEB_SEARCH_PROVIDER_SEARXNG,
                },
            )
        for endpoint in searxng_endpoints:
            searxng_result = _searxng_search(
                query,
                limit=limit,
                endpoint=endpoint,
                categories=str(
                    args.get("categories") or os.environ.get("NAVI_WEB_SEARCH_CATEGORIES") or ""
                ).strip(),
                language=str(
                    args.get("language") or os.environ.get("NAVI_WEB_SEARCH_LANGUAGE") or ""
                ).strip(),
                time_range=str(
                    args.get("time_range") or os.environ.get("NAVI_WEB_SEARCH_TIME_RANGE") or ""
                ).strip(),
            )
            if searxng_result.ok:
                return searxng_result
            provider_errors.append(_search_error_fact(searxng_result))
        if provider in {_WEB_SEARCH_PROVIDER_SEARXNG, "searxng_json"}:
            return _combined_search_failure(
                query=query,
                provider=_WEB_SEARCH_PROVIDER_SEARXNG,
                provider_errors=provider_errors,
            )

    if provider not in {"auto", _WEB_SEARCH_PROVIDER_DDG, "ddg", "duckduckgo"}:
        return ToolResult(
            tool="web.search",
            ok=False,
            error=f"unsupported web search provider: {provider}",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "unsupported_search_provider",
                "query": query,
                "provider": provider,
            },
        )

    ddg_result = _duckduckgo_html_search(query, limit=limit)
    if provider_errors:
        ddg_result.facts["provider_errors"] = provider_errors
    return ddg_result


def _searxng_endpoints() -> tuple[str, ...]:
    raw = ",".join(
        value
        for value in (
            os.environ.get("NAVI_WEB_SEARCH_SEARXNG_URLS", ""),
            os.environ.get("NAVI_WEB_SEARCH_SEARXNG_URL", ""),
        )
        if value
    )
    endpoints: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        endpoint = item.strip().rstrip("/")
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        endpoints.append(endpoint)
    return tuple(endpoints)


def _searxng_search(
    query: str,
    *,
    limit: int,
    endpoint: str,
    categories: str = "",
    language: str = "",
    time_range: str = "",
) -> ToolResult:
    search_url, error = _searxng_search_url(
        endpoint,
        query=query,
        categories=categories,
        language=language,
        time_range=time_range,
    )
    if error:
        return ToolResult(
            tool="web.search",
            ok=False,
            error=error,
            facts={
                CAPABILITY_ERROR_REASON_KEY: "search_provider_config_error",
                "query": query,
                "provider": _WEB_SEARCH_PROVIDER_SEARXNG,
                "endpoint": endpoint,
            },
        )

    try:
        request = Request(search_url, headers={"User-Agent": _SEARCH_USER_AGENT})
        with urlopen(request, timeout=15) as response:
            status = int(getattr(response, "status", 200))
            body = response.read(2_000_000).decode("utf-8", errors="replace")
        if status >= 400:
            return ToolResult(
                tool="web.search",
                ok=False,
                error=f"SearXNG returned HTTP {status}",
                facts={
                    CAPABILITY_ERROR_REASON_KEY: "search_provider_error",
                    "query": query,
                    "provider": _WEB_SEARCH_PROVIDER_SEARXNG,
                    "endpoint": endpoint,
                    "status_code": status,
                },
            )
        payload = json.loads(body)
    except TimeoutError:
        return ToolResult(
            tool="web.search",
            ok=False,
            error="SearXNG search request timed out",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "search_timeout",
                "query": query,
                "provider": _WEB_SEARCH_PROVIDER_SEARXNG,
                "endpoint": endpoint,
            },
        )
    except Exception as exc:
        return ToolResult(
            tool="web.search",
            ok=False,
            error=str(exc),
            facts={
                CAPABILITY_ERROR_REASON_KEY: "search_provider_error",
                "query": query,
                "provider": _WEB_SEARCH_PROVIDER_SEARXNG,
                "endpoint": endpoint,
                "error_type": type(exc).__name__,
            },
        )

    results = _normalize_searxng_results(payload, limit=limit)
    facts = {
        "query": query,
        "provider": _WEB_SEARCH_PROVIDER_SEARXNG,
        "endpoint": endpoint,
        "results": results,
        "answers": _as_string_list(payload.get("answers"))[:3],
        "corrections": _as_string_list(payload.get("corrections"))[:3],
        "suggestions": _as_string_list(payload.get("suggestions"))[:5],
        "infoboxes": payload.get("infoboxes") if isinstance(payload.get("infoboxes"), list) else [],
        "response": {"result_count": len(results)},
    }
    return ToolResult(tool="web.search", ok=True, facts=facts)


def _searxng_search_url(
    endpoint: str,
    *,
    query: str,
    categories: str = "",
    language: str = "",
    time_range: str = "",
) -> tuple[str, str]:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "", "SearXNG endpoint must be an http(s) URL"
    if parsed.username or parsed.password:
        return "", "SearXNG endpoint must not include credentials"
    path = parsed.path.rstrip("/")
    if not path.endswith("/search"):
        path = f"{path}/search" if path else "/search"
    params = {"q": query, "format": "json"}
    if categories:
        params["categories"] = categories
    if language:
        params["language"] = language
    if time_range:
        params["time_range"] = time_range
    return (
        urlunparse((parsed.scheme, parsed.netloc, path, "", urlencode(params), "")),
        "",
    )


def _normalize_searxng_results(payload: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    results: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = _clean_search_text(item.get("title") or "")
        content = _clean_search_text(item.get("content") or item.get("snippet") or "")
        if not url and not title and not content:
            continue
        result = {
            "title": title,
            "url": url,
            "snippet": content,
            "engine": str(item.get("engine") or "").strip(),
            "category": str(item.get("category") or "").strip(),
        }
        if item.get("publishedDate"):
            result["published_date"] = str(item.get("publishedDate"))
        results.append(result)
        if len(results) >= limit:
            break
    return results


def _duckduckgo_html_search(query: str, *, limit: int) -> ToolResult:
    try:
        result = subprocess.run(
            [
                "curl",
                "-sL",
                "-X",
                "POST",
                "-A",
                _DDG_BROWSER_UA,
                "-H",
                "Content-Type: application/x-www-form-urlencoded",
                "--data",
                urlencode({"q": query}),
                _DDG_SEARCH_URL,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if result.returncode != 0:
            stderr = _truncate_output(result.stderr.strip(), limit=2000)
            return ToolResult(
                tool="web.search",
                ok=False,
                error=stderr or f"curl exited with status {result.returncode}",
                facts={
                    CAPABILITY_ERROR_REASON_KEY: "search_provider_error",
                    "query": query,
                    "provider": _WEB_SEARCH_PROVIDER_DDG,
                    "curl_exit_code": result.returncode,
                    "stderr": stderr,
                },
            )
        raw_html = result.stdout
        block_reason = _search_block_reason(raw_html)
        if block_reason:
            return ToolResult(
                tool="web.search",
                ok=False,
                error=f"DuckDuckGo returned a bot challenge page: {block_reason}",
                facts={
                    CAPABILITY_ERROR_REASON_KEY: "search_provider_blocked",
                    "query": query,
                    "provider": _WEB_SEARCH_PROVIDER_DDG,
                    "source_url": _DDG_SEARCH_URL,
                    "block_reason": block_reason,
                    "response_length": len(raw_html),
                },
            )
        results = _extract_duckduckgo_results(raw_html, limit=limit)
        if not results:
            return ToolResult(
                tool="web.search",
                ok=False,
                error="DuckDuckGo returned no parseable results",
                facts={
                    CAPABILITY_ERROR_REASON_KEY: "search_provider_error",
                    "query": query,
                    "provider": _WEB_SEARCH_PROVIDER_DDG,
                    "response_length": len(raw_html),
                },
            )
        stripped_text = _html_to_text(raw_html)
        return ToolResult(
            tool="web.search",
            ok=True,
            facts={
                "query": query,
                "provider": _WEB_SEARCH_PROVIDER_DDG,
                "source_url": _DDG_SEARCH_URL,
                "results": results,
                "response": {"text": stripped_text[:15000], "result_count": len(results)},
            },
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            tool="web.search",
            ok=False,
            error="search request timed out",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "search_timeout",
                "query": query,
                "provider": _WEB_SEARCH_PROVIDER_DDG,
            },
        )
    except Exception as exc:
        return ToolResult(
            tool="web.search",
            ok=False,
            error=str(exc),
            facts={
                CAPABILITY_ERROR_REASON_KEY: "search_provider_error",
                "query": query,
                "provider": _WEB_SEARCH_PROVIDER_DDG,
                "error_type": type(exc).__name__,
            },
        )


def _extract_duckduckgo_results(raw_html: str, *, limit: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    blocks = re.findall(
        r'<div[^>]+class="[^"]*\bresult\b[^"]*"[^>]*>(.*?)(?=<div[^>]+class="[^"]*\bresult\b|</body>)',
        raw_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for block in blocks:
        title_match = re.search(
            r'<a[^>]+class="[^"]*\bresult__a\b[^"]*"[^>]*>(.*?)</a>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not title_match:
            continue
        title = _html_to_text(title_match.group(1))
        href_match = re.search(r'href=["\']([^"\']+)["\']', title_match.group(0), re.IGNORECASE)
        url = ""
        if href_match:
            raw_href = html.unescape(href_match.group(1)).strip()
            url = _resolve_ddg_redirect(raw_href)
        snippet_match = re.search(
            r'<a[^>]+class="[^"]*\bresult__snippet\b[^"]*"[^>]*>(.*?)</a>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        snippet = _html_to_text(snippet_match.group(1)) if snippet_match else ""
        if title or url or snippet:
            results.append({"title": title, "url": url, "snippet": snippet, "engine": "duckduckgo"})
        if len(results) >= limit:
            break
    return results


def _resolve_ddg_redirect(href: str) -> str:
    cleaned = href.strip()
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned
    parsed = urlparse(cleaned)
    if "duckduckgo.com" not in parsed.netloc:
        return cleaned
    query = parse_qs(parsed.query)
    uddg = query.get("uddg", [""])[0]
    return html.unescape(uddg) if uddg else cleaned


def _html_to_text(value: str) -> str:
    text = re.sub(
        r"<(script|style|svg|symbol|use|path).*?>.*?</\1>",
        " ",
        str(value or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _search_block_reason(raw_html: str) -> str:
    lowered = str(raw_html or "").lower()
    challenge_markers = {
        "anomaly-modal": "duckduckgo_anomaly_challenge",
        "unfortunately, bots use duckduckgo too": "duckduckgo_bot_challenge",
        "challenge-form": "search_challenge_form",
        "cloudflarehandlecaptcha": "cloudflare_captcha",
        "turnstile": "turnstile",
        "captcha": "captcha",
    }
    for marker, reason in challenge_markers.items():
        if marker in lowered:
            return reason
    return ""


def _clean_search_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = _clean_search_text(item)
        if text:
            items.append(text)
    return items


def _search_error_fact(result: ToolResult) -> dict[str, Any]:
    return {
        "provider": str(result.facts.get("provider") or result.tool),
        "error_reason": str(
            result.facts.get(CAPABILITY_ERROR_REASON_KEY) or "search_provider_error"
        ),
        "error": _truncate_output(result.error, limit=1000),
        **{
            key: value
            for key, value in result.facts.items()
            if key
            in {
                "endpoint",
                "status_code",
                "curl_exit_code",
                "error_type",
                "block_reason",
                "source_url",
            }
        },
    }


def _combined_search_failure(
    *,
    query: str,
    provider: str,
    provider_errors: list[dict[str, Any]],
) -> ToolResult:
    return ToolResult(
        tool="web.search",
        ok=False,
        error="all configured web search providers failed",
        facts={
            CAPABILITY_ERROR_REASON_KEY: "search_provider_error",
            "query": query,
            "provider": provider,
            "provider_errors": provider_errors,
        },
    )
