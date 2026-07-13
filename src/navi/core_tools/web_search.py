"""Web search provider handlers."""
from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from navi.capability_contract import CAPABILITY_ERROR_REASON_KEY, CAPABILITY_RETRYABLE_KEY
from navi.config import load_runtime_env
from navi.mcp_client import (
    DEFAULT_EXA_MCP_URL,
    MCPClient,
    MCPServerConfig,
    describe_mcp_exception,
)

from ..tools import ToolResult
from .utils import _positive_int, _truncate_output

_SEARCH_USER_AGENT = "Navi/1.0"
_WEB_SEARCH_PROVIDER_SEARXNG = "searxng"
_WEB_SEARCH_PROVIDER_EXA_MCP = "exa_mcp"


async def _web_search(args: dict[str, Any], *, home: Path | None = None) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(
            tool="web.search",
            ok=False,
            error="query is required",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "missing_required_argument",
                CAPABILITY_RETRYABLE_KEY: False,
                "provider": "web.search",
            },
        )

    limit = _positive_int(args.get("limit"), default=5, maximum=10)
    env = load_runtime_env(home) if home is not None else dict(os.environ)
    provider = str(env.get("NAVI_WEB_SEARCH_PROVIDER") or "auto").strip().lower()
    provider_errors: list[dict[str, Any]] = []

    if provider in {"auto", _WEB_SEARCH_PROVIDER_SEARXNG, "searxng_json"}:
        searxng_endpoints = _searxng_endpoints(env)
        if not searxng_endpoints and provider in {_WEB_SEARCH_PROVIDER_SEARXNG, "searxng_json"}:
            return ToolResult(
                tool="web.search",
                ok=False,
                error="NAVI_WEB_SEARCH_SEARXNG_URL or NAVI_WEB_SEARCH_SEARXNG_URLS is required",
                facts={
                    CAPABILITY_ERROR_REASON_KEY: "search_provider_config_error",
                    CAPABILITY_RETRYABLE_KEY: False,
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
                    args.get("categories") or env.get("NAVI_WEB_SEARCH_CATEGORIES") or ""
                ).strip(),
                language=str(
                    args.get("language") or env.get("NAVI_WEB_SEARCH_LANGUAGE") or ""
                ).strip(),
                time_range=str(
                    args.get("time_range") or env.get("NAVI_WEB_SEARCH_TIME_RANGE") or ""
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

    if provider not in {"auto", "exa", "exa_mcp", "mcp"}:
        return ToolResult(
            tool="web.search",
            ok=False,
            error=f"unsupported web search provider: {provider}",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "unsupported_search_provider",
                CAPABILITY_RETRYABLE_KEY: False,
                "query": query,
                "provider": provider,
            },
        )

    exa_result = await _exa_mcp_search(query, limit=limit, env=env)
    if provider_errors:
        exa_result.facts["provider_errors"] = provider_errors
    return exa_result


def _searxng_endpoints(env: dict[str, str] | None = None) -> tuple[str, ...]:
    env = env or dict(os.environ)
    raw = ",".join(
        value
        for value in (
            env.get("NAVI_WEB_SEARCH_SEARXNG_URLS", ""),
            env.get("NAVI_WEB_SEARCH_SEARXNG_URL", ""),
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


async def _exa_mcp_search(
    query: str,
    *,
    limit: int,
    env: dict[str, str] | None = None,
) -> ToolResult:
    env = env or dict(os.environ)
    endpoint = str(
        env.get("NAVI_WEB_SEARCH_EXA_MCP_URL") or DEFAULT_EXA_MCP_URL
    ).strip()
    api_key = str(env.get("NAVI_EXA_API_KEY") or env.get("EXA_API_KEY") or "")
    headers = {"x-api-key": api_key} if api_key else {}
    server = MCPServerConfig(
        name="exa",
        transport="streamable_http",
        url=endpoint,
        headers=headers,
        timeout_seconds=30,
        permission="network",
        allowed_tools=("web_search_exa",),
    )
    try:
        result = await MCPClient(server).call_tool(
            "web_search_exa",
            {"query": query, "numResults": limit},
        )
    except Exception as exc:
        error, timed_out = describe_mcp_exception(exc)
        return ToolResult(
            tool="web.search",
            ok=False,
            error=error,
            facts={
                CAPABILITY_ERROR_REASON_KEY: "search_timeout" if timed_out else "search_provider_error",
                CAPABILITY_RETRYABLE_KEY: timed_out,
                "query": query,
                "provider": _WEB_SEARCH_PROVIDER_EXA_MCP,
                "endpoint": server.safe_endpoint,
                "error_type": type(exc).__name__,
            },
        )
    if not result["ok"]:
        return ToolResult(
            tool="web.search",
            ok=False,
            error="Exa MCP reported a search tool error",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "search_provider_error",
                CAPABILITY_RETRYABLE_KEY: False,
                "query": query,
                "provider": _WEB_SEARCH_PROVIDER_EXA_MCP,
                "endpoint": server.safe_endpoint,
            },
        )
    text = str(result.get("text") or "")
    results = _normalize_exa_text_results(text, limit=limit)
    if not results:
        return ToolResult(
            tool="web.search",
            ok=False,
            error="Exa MCP returned no parseable search results",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "search_provider_error",
                CAPABILITY_RETRYABLE_KEY: False,
                "query": query,
                "provider": _WEB_SEARCH_PROVIDER_EXA_MCP,
                "endpoint": server.safe_endpoint,
                "response_length": len(text),
            },
        )
    return ToolResult(
        tool="web.search",
        ok=True,
        facts={
            "query": query,
            "provider": _WEB_SEARCH_PROVIDER_EXA_MCP,
            "endpoint": server.safe_endpoint,
            "results": results,
            "response": {
                "text": text[:100_000],
                "truncated": len(text) > 100_000,
                "result_count": len(results),
            },
        },
    )


def _normalize_exa_text_results(value: str, *, limit: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*---\s*\n", str(value or "")):
        fields: dict[str, str] = {}
        highlights = ""
        match = re.search(r"(?m)^Highlights:\s*\n?(.*)$", block, flags=re.DOTALL)
        if match:
            highlights = _clean_search_text(match.group(1))
            header = block[: match.start()]
        else:
            header = block
        for key in ("Title", "URL", "Published", "Author"):
            field_match = re.search(rf"(?m)^{key}:\s*(.*)$", header)
            if field_match:
                fields[key.lower()] = field_match.group(1).strip()
        url = fields.get("url", "")
        title = fields.get("title", "")
        if not url and not title:
            continue
        item = {
            "title": title,
            "url": url,
            "snippet": highlights,
            "engine": "exa",
        }
        published = fields.get("published", "")
        author = fields.get("author", "")
        if published and published != "N/A":
            item["published_date"] = published
        if author and author != "N/A":
            item["author"] = author
        results.append(item)
        if len(results) >= limit:
            break
    return results


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
            CAPABILITY_RETRYABLE_KEY: False,
            "query": query,
            "provider": provider,
            "provider_errors": provider_errors,
        },
    )
