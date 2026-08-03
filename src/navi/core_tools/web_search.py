"""Model-selected structured web search providers."""

from __future__ import annotations

import asyncio
import html
import http.client
import ipaddress
import json
import re
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request

from navi.capability_contract import CAPABILITY_ERROR_REASON_KEY, CAPABILITY_RETRYABLE_KEY
from navi.config import NaviConfig, SearchProviderConfig, load_config
from navi.mcp_client import MCPClient, MCPServerConfig, MCPTransportError
from navi.mcp_tools import parse_mcp_config

from ..tools import ToolResult
from .utils import _positive_int

_SEARCH_USER_AGENT = "Navi/1.0"
_SEARCH_TITLE_MAX_CHARS = 300
_SEARCH_SNIPPET_MAX_CHARS = 1200
_SEARCH_RESPONSE_MAX_BYTES = 2_000_000
_X_RESPONSE_MAX_BYTES = 4_000_000
_FORBIDDEN_SEARCH_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection"}
)


class SearchTargetRejectedError(ValueError):
    def __init__(
        self,
        *,
        target_host: str,
        rejected_addresses: list[str],
        reason: str,
    ):
        super().__init__(f"search target rejected: {reason}")
        self.target_host = target_host
        self.rejected_addresses = tuple(rejected_addresses)
        self.reason = reason


class _PinnedHTTPResponse:
    def __init__(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ):
        self._connection = connection
        self._response = response
        self.status = int(response.status)
        self.headers = response.headers

    def read(self, max_bytes: int) -> bytes:
        return self._response.read(max_bytes)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self._connection.close()


def urlopen(request: Request, timeout: float) -> _PinnedHTTPResponse:
    """Open one pinned request without following redirects."""
    parsed = urlparse(request.full_url)
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise SearchTargetRejectedError(
            target_host=host,
            rejected_addresses=[],
            reason="invalid_http_target",
        )
    if parsed.username or parsed.password:
        raise SearchTargetRejectedError(
            target_host=host,
            rejected_addresses=[],
            reason="credentialed_target",
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = sorted({str(item[4][0]) for item in infos if item and item[4]})
    if not addresses:
        raise OSError(f"target host did not resolve: {host}")
    rejected = _rejected_search_addresses(
        addresses,
        allow_loopback=bool(getattr(request, "_navi_allow_loopback", False)),
        allow_private_network=bool(
            getattr(request, "_navi_allow_private_network", False)
        ),
    )
    rejected_set = set(rejected)
    allowed_addresses = [item for item in addresses if item not in rejected_set]
    if not allowed_addresses:
        raise SearchTargetRejectedError(
            target_host=host,
            rejected_addresses=rejected,
            reason="non_public_address",
        )
    pinned_address = allowed_addresses[0]
    headers = {
        str(key): str(value)
        for key, value in request.header_items()
        if str(key).lower() not in _FORBIDDEN_SEARCH_HEADERS
    }
    default_port = 443 if parsed.scheme == "https" else 80
    host_label = f"[{host}]" if ":" in host else host
    headers["Host"] = host_label if port == default_port else f"{host_label}:{port}"
    path_query = parsed.path or "/"
    if parsed.query:
        path_query += f"?{parsed.query}"

    connection: http.client.HTTPConnection
    if parsed.scheme == "https":
        context = ssl.create_default_context()
        raw_socket = socket.create_connection((pinned_address, port), timeout=timeout)
        tls_socket = context.wrap_socket(raw_socket, server_hostname=host)
        connection = http.client.HTTPSConnection(
            pinned_address,
            port,
            timeout=timeout,
            context=context,
        )
        connection.sock = tls_socket
    else:
        connection = http.client.HTTPConnection(pinned_address, port, timeout=timeout)
    try:
        connection.request(
            request.get_method(),
            path_query,
            body=request.data,
            headers=headers,
        )
        return _PinnedHTTPResponse(connection, connection.getresponse())
    except Exception:
        connection.close()
        raise


def _rejected_search_addresses(
    addresses: list[str],
    *,
    allow_loopback: bool,
    allow_private_network: bool,
) -> list[str]:
    rejected: list[str] = []
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            rejected.append(raw)
            continue
        if (
            address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            rejected.append(raw)
        elif address.is_loopback:
            if not allow_loopback:
                rejected.append(raw)
        elif address.is_private:
            if not allow_private_network:
                rejected.append(raw)
        elif not address.is_global:
            rejected.append(raw)
    return rejected


@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int
    categories: str = ""
    language: str = ""
    time_range: str = ""
    start_time: str = ""
    end_time: str = ""
    sort_order: str = ""
    next_token: str = ""


class SearchProviderAdapter(Protocol):
    kind: str
    requires_credentials: bool

    def validate(
        self,
        provider_id: str,
        provider: SearchProviderConfig,
        config: NaviConfig,
        *,
        home: Path | None,
    ) -> list[str]: ...

    def safe_endpoint(
        self,
        provider: SearchProviderConfig,
        config: NaviConfig,
    ) -> str: ...

    async def search(
        self,
        provider_id: str,
        provider: SearchProviderConfig,
        request: SearchRequest,
        config: NaviConfig,
        *,
        home: Path | None,
    ) -> ToolResult: ...


class SearXNGSearchProvider:
    kind = "searxng"
    requires_credentials = False

    def validate(
        self,
        provider_id: str,
        provider: SearchProviderConfig,
        config: NaviConfig,
        *,
        home: Path | None,
    ) -> list[str]:
        del config, home
        _, error = _searxng_search_url(provider.endpoint, query="validation")
        return [f"search.providers.{provider_id}.endpoint: {error}"] if error else []

    def safe_endpoint(
        self,
        provider: SearchProviderConfig,
        config: NaviConfig,
    ) -> str:
        del config
        return _safe_http_endpoint(provider.endpoint)

    async def search(
        self,
        provider_id: str,
        provider: SearchProviderConfig,
        request: SearchRequest,
        config: NaviConfig,
        *,
        home: Path | None,
    ) -> ToolResult:
        del config, home
        return await asyncio.to_thread(
            _searxng_search,
            provider_id,
            provider,
            request,
        )


class ExaMCPSearchProvider:
    kind = "exa_mcp"
    requires_credentials = False

    def validate(
        self,
        provider_id: str,
        provider: SearchProviderConfig,
        config: NaviConfig,
        *,
        home: Path | None,
    ) -> list[str]:
        _, error = _exa_server(
            provider_id,
            provider,
            config,
            home=home,
        )
        return [error] if error else []

    def safe_endpoint(
        self,
        provider: SearchProviderConfig,
        config: NaviConfig,
    ) -> str:
        server = config.mcp_servers.get(provider.mcp_server) or {}
        return _safe_http_endpoint(str(server.get("url") or ""))

    async def search(
        self,
        provider_id: str,
        provider: SearchProviderConfig,
        request: SearchRequest,
        config: NaviConfig,
        *,
        home: Path | None,
    ) -> ToolResult:
        server, error = _exa_server(
            provider_id,
            provider,
            config,
            home=home,
        )
        if server is None:
            return _provider_failure(
                provider_id=provider_id,
                provider_kind=self.kind,
                query=request.query,
                endpoint=self.safe_endpoint(provider, config),
                error=error,
                reason="search_provider_config_error",
                retryable=False,
            )
        return await _exa_mcp_search(provider_id, request, server=server)


class XAPISearchProvider:
    kind = "x_api"
    requires_credentials = True

    def validate(
        self,
        provider_id: str,
        provider: SearchProviderConfig,
        config: NaviConfig,
        *,
        home: Path | None,
    ) -> list[str]:
        del config, home
        errors: list[str] = []
        parsed = urlparse(provider.endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            errors.append(
                f"search.providers.{provider_id}.endpoint must be an https URL"
            )
        elif parsed.username or parsed.password:
            errors.append(
                f"search.providers.{provider_id}.endpoint must not include credentials"
            )
        if not provider.bearer_token:
            errors.append(
                f"search.providers.{provider_id}.bearer_token is required when enabled"
            )
        return errors

    def safe_endpoint(
        self,
        provider: SearchProviderConfig,
        config: NaviConfig,
    ) -> str:
        del config
        return _safe_http_endpoint(provider.endpoint)

    async def search(
        self,
        provider_id: str,
        provider: SearchProviderConfig,
        request: SearchRequest,
        config: NaviConfig,
        *,
        home: Path | None,
    ) -> ToolResult:
        del config, home
        return await asyncio.to_thread(
            _x_api_search,
            provider_id,
            provider,
            request,
        )


_SEARCH_PROVIDER_ADAPTERS: dict[str, SearchProviderAdapter] = {
    adapter.kind: adapter
    for adapter in (
        SearXNGSearchProvider(),
        ExaMCPSearchProvider(),
        XAPISearchProvider(),
    )
}


def validate_search_config(config: NaviConfig, home: Path) -> list[str]:
    errors: list[str] = []
    if not config.search.providers:
        return ["search.providers must configure at least one provider"]
    enabled = 0
    for provider_id, provider in sorted(config.search.providers.items()):
        adapter = _SEARCH_PROVIDER_ADAPTERS.get(provider.kind)
        if adapter is None:
            errors.append(
                f"search.providers.{provider_id}.kind '{provider.kind}' is unsupported"
            )
            continue
        if not provider.enabled:
            continue
        enabled += 1
        errors.extend(
            adapter.validate(
                provider_id,
                provider,
                config,
                home=home,
            )
        )
    if enabled == 0:
        errors.append("search.providers must enable at least one provider")
    return errors


def search_provider_catalog(
    config: NaviConfig,
) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for provider_id, provider in sorted(config.search.providers.items()):
        adapter = _SEARCH_PROVIDER_ADAPTERS.get(provider.kind)
        endpoint = adapter.safe_endpoint(provider, config) if adapter else ""
        catalog.append(
            {
                "id": provider_id,
                "kind": provider.kind,
                "enabled": provider.enabled,
                "endpoint": endpoint,
                "allow_private_network": provider.allow_private_network,
                "mcp_server": provider.mcp_server,
                "requires_credentials": bool(
                    adapter and adapter.requires_credentials
                ),
                "has_credentials": bool(provider.bearer_token),
            }
        )
    return catalog


def enabled_search_provider_ids(config: NaviConfig) -> tuple[str, ...]:
    return tuple(
        provider_id
        for provider_id, provider in sorted(config.search.providers.items())
        if provider.enabled and provider.kind in _SEARCH_PROVIDER_ADAPTERS
    )


async def _web_search(
    args: dict[str, Any],
    *,
    home: Path | None = None,
    config: NaviConfig | None = None,
) -> ToolResult:
    query = str(args.get("query") or "").strip()
    provider_id = str(args.get("provider") or "").strip()
    config = config or (load_config(home) if home is not None else NaviConfig())
    available = list(enabled_search_provider_ids(config))

    if not query:
        return _invalid_search_request(
            provider_id=provider_id,
            error="query is required",
            missing="query",
            available=available,
        )
    if not provider_id:
        return _invalid_search_request(
            provider_id="",
            error="provider is required; the model must select one configured provider",
            missing="provider",
            available=available,
        )

    provider = config.search.providers.get(provider_id)
    if provider is None:
        return _provider_failure(
            provider_id=provider_id,
            provider_kind="",
            query=query,
            endpoint="",
            error=f"search provider is not configured: {provider_id}",
            reason="search_provider_not_configured",
            retryable=False,
            extra={"available_providers": available},
        )
    if not provider.enabled:
        return _provider_failure(
            provider_id=provider_id,
            provider_kind=provider.kind,
            query=query,
            endpoint="",
            error=f"search provider is disabled: {provider_id}",
            reason="search_provider_disabled",
            retryable=False,
            extra={"available_providers": available},
        )

    adapter = _SEARCH_PROVIDER_ADAPTERS.get(provider.kind)
    if adapter is None:
        return _provider_failure(
            provider_id=provider_id,
            provider_kind=provider.kind,
            query=query,
            endpoint="",
            error=f"unsupported search provider kind: {provider.kind}",
            reason="unsupported_search_provider",
            retryable=False,
        )
    validation_errors = adapter.validate(
        provider_id,
        provider,
        config,
        home=home,
    )
    if validation_errors:
        return _provider_failure(
            provider_id=provider_id,
            provider_kind=provider.kind,
            query=query,
            endpoint=adapter.safe_endpoint(provider, config),
            error="; ".join(validation_errors),
            reason="search_provider_config_error",
            retryable=False,
        )

    request = SearchRequest(
        query=query,
        limit=_positive_int(args.get("limit"), default=5, maximum=10),
        categories=str(args.get("categories") or provider.categories).strip(),
        language=str(args.get("language") or provider.language).strip(),
        time_range=str(args.get("time_range") or provider.time_range).strip(),
        start_time=str(args.get("start_time") or "").strip(),
        end_time=str(args.get("end_time") or "").strip(),
        sort_order=str(args.get("sort_order") or "").strip(),
        next_token=str(args.get("next_token") or "").strip(),
    )
    return await adapter.search(
        provider_id,
        provider,
        request,
        config,
        home=home,
    )


def _invalid_search_request(
    *,
    provider_id: str,
    error: str,
    missing: str,
    available: list[str],
) -> ToolResult:
    return ToolResult(
        tool="web.search",
        ok=False,
        error=error,
        facts={
            CAPABILITY_ERROR_REASON_KEY: "missing_required_argument",
            CAPABILITY_RETRYABLE_KEY: False,
            "provider": provider_id,
            "missing_argument": missing,
            "available_providers": available,
        },
    )


def _provider_failure(
    *,
    provider_id: str,
    provider_kind: str,
    query: str,
    endpoint: str,
    error: str,
    reason: str,
    retryable: bool,
    extra: dict[str, Any] | None = None,
) -> ToolResult:
    facts: dict[str, Any] = {
        CAPABILITY_ERROR_REASON_KEY: reason,
        CAPABILITY_RETRYABLE_KEY: retryable,
        "query": query,
        "provider": provider_id,
        "provider_kind": provider_kind,
    }
    if endpoint:
        facts["endpoint"] = endpoint
    if extra:
        facts.update(extra)
    return ToolResult(tool="web.search", ok=False, error=error, facts=facts)


def _web_document_evidence_contract(
    *,
    provider_id: str,
    provider_kind: str,
) -> dict[str, Any]:
    return {
        "scope": "query_ranked_web_documents",
        "provider": provider_id,
        "provider_kind": provider_kind,
        "establishes": [
            "search_result_presence",
            "source_attribution",
            "source_reported_claims",
            "document_snippets",
        ],
        "does_not_establish": [
            "claim_truth",
            "source_authority",
            "result_representativeness",
            "real_world_outcome",
        ],
        "sampling": "provider_ranked_query_results",
    }


def _x_post_evidence_contract(*, provider_id: str) -> dict[str, Any]:
    return {
        "scope": "x_api_query_posts",
        "provider": provider_id,
        "provider_kind": "x_api",
        "establishes": [
            "provider_returned_post_presence",
            "post_id",
            "author_attribution",
            "provider_reported_creation_time",
            "provider_reported_public_metrics",
        ],
        "does_not_establish": [
            "query_result_completeness",
            "future_post_visibility",
            "claim_truth",
            "real_world_outcome",
        ],
        "sampling": "provider_ranked_or_recent_query_page",
    }


def _searxng_search(
    provider_id: str,
    provider: SearchProviderConfig,
    request: SearchRequest,
) -> ToolResult:
    search_url, error = _searxng_search_url(
        provider.endpoint,
        query=request.query,
        categories=request.categories,
        language=request.language,
        time_range=request.time_range,
    )
    if error:
        return _provider_failure(
            provider_id=provider_id,
            provider_kind="searxng",
            query=request.query,
            endpoint=_safe_http_endpoint(provider.endpoint),
            error=error,
            reason="search_provider_config_error",
            retryable=False,
        )

    payload, failure = _read_json_response(
        provider_id=provider_id,
        provider_kind="searxng",
        query=request.query,
        endpoint=_safe_http_endpoint(provider.endpoint),
        request=Request(search_url, headers={"User-Agent": _SEARCH_USER_AGENT}),
        timeout=15,
        max_bytes=_SEARCH_RESPONSE_MAX_BYTES,
        provider_label="SearXNG",
        allow_loopback=provider.allow_private_network,
        allow_private_network=provider.allow_private_network,
    )
    if failure is not None:
        return failure
    assert isinstance(payload, dict)

    results = _normalize_searxng_results(payload, limit=request.limit)
    provider_errors = _normalize_searxng_provider_errors(payload)
    if not results and provider_errors:
        return _provider_failure(
            provider_id=provider_id,
            provider_kind="searxng",
            query=request.query,
            endpoint=_safe_http_endpoint(provider.endpoint),
            error="SearXNG returned no results while upstream engines reported failures",
            reason="search_provider_blocked",
            retryable=any(_provider_error_retryable(item) for item in provider_errors),
            extra={
                "source_url": search_url,
                "provider_errors": provider_errors,
            },
        )

    facts = {
        "query": request.query,
        "provider": provider_id,
        "provider_kind": "searxng",
        "endpoint": _safe_http_endpoint(provider.endpoint),
        "source_url": search_url,
        "results": results,
        "answers": _as_string_list(payload.get("answers"))[:3],
        "corrections": _as_string_list(payload.get("corrections"))[:3],
        "suggestions": _as_string_list(payload.get("suggestions"))[:5],
        "infoboxes": (
            payload.get("infoboxes")
            if isinstance(payload.get("infoboxes"), list)
            else []
        ),
        "provider_errors": provider_errors,
        "response": {
            "result_count": len(results),
            "provider_error_count": len(provider_errors),
        },
        "evidence_contract": _web_document_evidence_contract(
            provider_id=provider_id,
            provider_kind="searxng",
        ),
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
        title = _bounded_search_text(item.get("title") or "", _SEARCH_TITLE_MAX_CHARS)
        content = _bounded_search_text(
            item.get("content") or item.get("snippet") or "",
            _SEARCH_SNIPPET_MAX_CHARS,
        )
        if not url and not title and not content:
            continue
        result: dict[str, Any] = {
            "kind": "web_document",
            "title": title,
            "url": url,
            "snippet": content,
            "engine": str(item.get("engine") or "").strip(),
            "category": str(item.get("category") or "").strip(),
        }
        engines = _as_string_list(item.get("engines"))
        if engines:
            result["engines"] = engines
        if item.get("publishedDate"):
            result["published_date"] = str(item.get("publishedDate"))
        results.append(result)
        if len(results) >= limit:
            break
    return results


def _normalize_searxng_provider_errors(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw_errors = payload.get("unresponsive_engines")
    if not isinstance(raw_errors, list):
        return []
    errors: list[dict[str, Any]] = []
    for item in raw_errors:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        engine = _clean_search_text(item[0])
        reason = _clean_search_text(item[1])
        if engine or reason:
            errors.append({"engine": engine, "reason": reason})
    return errors[:20]


def _provider_error_retryable(item: dict[str, Any]) -> bool:
    status = item.get("status")
    if isinstance(status, int) and not isinstance(status, bool):
        return _retryable_http_status(status)
    return False


def _exa_server(
    provider_id: str,
    provider: SearchProviderConfig,
    config: NaviConfig,
    *,
    home: Path | None,
) -> tuple[MCPServerConfig | None, str]:
    if not provider.mcp_server:
        return (
            None,
            f"search.providers.{provider_id}.mcp_server is required for exa_mcp",
        )
    report = parse_mcp_config(
        config,
        path=(home / "config.yaml") if home is not None else Path("config.yaml"),
    )
    if report.errors:
        return None, "; ".join(report.errors)
    server = next(
        (item for item in report.servers if item.name == provider.mcp_server),
        None,
    )
    if server is None:
        return (
            None,
            f"search.providers.{provider_id}.mcp_server "
            f"'{provider.mcp_server}' is not an enabled MCP server",
        )
    if "web_search_exa" not in server.allowed_tools:
        return (
            None,
            f"mcp.servers.{provider.mcp_server}.tool_permissions must include "
            "web_search_exa",
        )
    return server, ""


async def _exa_mcp_search(
    provider_id: str,
    request: SearchRequest,
    *,
    server: MCPServerConfig,
) -> ToolResult:
    try:
        result = await MCPClient(server).call_tool(
            "web_search_exa",
            {"query": request.query, "numResults": request.limit},
        )
    except MCPTransportError as exc:
        info = exc.facts
        extra: dict[str, Any] = {"error_type": type(exc.cause).__name__}
        if info.status_code:
            extra["status_code"] = info.status_code
        return _provider_failure(
            provider_id=provider_id,
            provider_kind="exa_mcp",
            query=request.query,
            endpoint=server.safe_endpoint,
            error=info.message,
            reason="search_timeout" if info.timed_out else "search_provider_error",
            retryable=info.retryable,
            extra=extra,
        )
    if not result["ok"]:
        return _provider_failure(
            provider_id=provider_id,
            provider_kind="exa_mcp",
            query=request.query,
            endpoint=server.safe_endpoint,
            error="Exa MCP reported a search tool error",
            reason="search_provider_error",
            retryable=False,
        )
    text = str(result.get("text") or "")
    results = _normalize_exa_text_results(text, limit=request.limit)
    if not results:
        return _provider_failure(
            provider_id=provider_id,
            provider_kind="exa_mcp",
            query=request.query,
            endpoint=server.safe_endpoint,
            error="Exa MCP returned no parseable search results",
            reason="search_provider_error",
            retryable=False,
            extra={"response_length": len(text)},
        )
    return ToolResult(
        tool="web.search",
        ok=True,
        facts={
            "query": request.query,
            "provider": provider_id,
            "provider_kind": "exa_mcp",
            "endpoint": server.safe_endpoint,
            "source_url": server.safe_endpoint,
            "results": results,
            "provider_errors": [],
            "response": {
                "text_length": len(text),
                "truncated": bool(result.get("truncated")),
                "result_count": len(results),
                "provider_error_count": 0,
            },
            "evidence_contract": _web_document_evidence_contract(
                provider_id=provider_id,
                provider_kind="exa_mcp",
            ),
        },
    )


def _normalize_exa_text_results(value: str, *, limit: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*---\s*\n", str(value or "")):
        fields: dict[str, str] = {}
        highlights = ""
        match = re.search(r"(?m)^Highlights:\s*\n?(.*)$", block, flags=re.DOTALL)
        if match:
            highlights = _bounded_search_text(
                match.group(1),
                _SEARCH_SNIPPET_MAX_CHARS,
            )
            header = block[: match.start()]
        else:
            header = block
        for key in ("Title", "URL", "Published", "Author"):
            field_match = re.search(rf"(?m)^{key}:\s*(.*)$", header)
            if field_match:
                fields[key.lower()] = field_match.group(1).strip()
        url = fields.get("url", "")
        title = _bounded_search_text(fields.get("title", ""), _SEARCH_TITLE_MAX_CHARS)
        if not url and not title:
            continue
        item = {
            "kind": "web_document",
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


def _x_api_search(
    provider_id: str,
    provider: SearchProviderConfig,
    request: SearchRequest,
) -> ToolResult:
    search_url, error = _x_api_search_url(provider.endpoint, request)
    endpoint = _safe_http_endpoint(provider.endpoint)
    if error:
        return _provider_failure(
            provider_id=provider_id,
            provider_kind="x_api",
            query=request.query,
            endpoint=endpoint,
            error=error,
            reason="search_provider_config_error",
            retryable=False,
        )
    payload, failure = _read_json_response(
        provider_id=provider_id,
        provider_kind="x_api",
        query=request.query,
        endpoint=endpoint,
        request=Request(
            search_url,
            headers={
                "Authorization": f"Bearer {provider.bearer_token}",
                "User-Agent": _SEARCH_USER_AGENT,
                "Accept": "application/json",
            },
        ),
        timeout=20,
        max_bytes=_X_RESPONSE_MAX_BYTES,
        provider_label="X API",
    )
    if failure is not None:
        return failure
    assert isinstance(payload, dict)

    results = _normalize_x_api_results(payload, limit=request.limit)
    provider_errors = _normalize_x_api_errors(payload)
    if not results and provider_errors:
        return _provider_failure(
            provider_id=provider_id,
            provider_kind="x_api",
            query=request.query,
            endpoint=endpoint,
            error="X API returned no posts while reporting provider errors",
            reason="search_provider_blocked",
            retryable=any(_provider_error_retryable(item) for item in provider_errors),
            extra={
                "source_url": search_url,
                "provider_errors": provider_errors,
            },
        )
    raw_meta = payload.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    response = {
        "result_count": len(results),
        "provider_error_count": len(provider_errors),
        "newest_id": str(meta.get("newest_id") or ""),
        "oldest_id": str(meta.get("oldest_id") or ""),
        "next_token": str(meta.get("next_token") or ""),
    }
    return ToolResult(
        tool="web.search",
        ok=True,
        facts={
            "query": request.query,
            "provider": provider_id,
            "provider_kind": "x_api",
            "endpoint": endpoint,
            "source_url": search_url,
            "results": results,
            "provider_errors": provider_errors,
            "response": response,
            "evidence_contract": _x_post_evidence_contract(provider_id=provider_id),
        },
    )


def _x_api_search_url(
    endpoint: str,
    request: SearchRequest,
) -> tuple[str, str]:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        return "", "X API endpoint must be an https URL"
    if parsed.username or parsed.password:
        return "", "X API endpoint must not include credentials"
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/2/tweets/search/recent"
    params = {
        "query": request.query,
        "max_results": max(10, request.limit),
        "tweet.fields": (
            "id,text,author_id,created_at,lang,public_metrics,conversation_id"
        ),
        "expansions": "author_id",
        "user.fields": "id,name,username,verified,profile_image_url",
    }
    for key, value in (
        ("start_time", request.start_time),
        ("end_time", request.end_time),
        ("sort_order", request.sort_order),
        ("next_token", request.next_token),
    ):
        if value:
            params[key] = value
    return (
        urlunparse((parsed.scheme, parsed.netloc, path, "", urlencode(params), "")),
        "",
    )


def _normalize_x_api_results(
    payload: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    raw_data = payload.get("data")
    if not isinstance(raw_data, list):
        return []
    includes = payload.get("includes")
    candidate_users = includes.get("users") if isinstance(includes, dict) else []
    raw_users: list[Any] = candidate_users if isinstance(candidate_users, list) else []
    users = {
        str(item.get("id") or ""): item
        for item in raw_users
        if isinstance(item, dict) and item.get("id")
    }
    results: list[dict[str, Any]] = []
    for post in raw_data:
        if not isinstance(post, dict):
            continue
        post_id = str(post.get("id") or "").strip()
        text = _bounded_search_text(post.get("text") or "", _SEARCH_SNIPPET_MAX_CHARS)
        if not post_id and not text:
            continue
        author_id = str(post.get("author_id") or "").strip()
        user = users.get(author_id, {})
        username = str(user.get("username") or "").strip()
        name = _bounded_search_text(user.get("name") or "", _SEARCH_TITLE_MAX_CHARS)
        title = (
            f"{name} (@{username}) on X"
            if name and username
            else f"@{username} on X"
            if username
            else f"X post {post_id}"
        )
        url = (
            f"https://x.com/{username}/status/{post_id}"
            if username and post_id
            else f"https://x.com/i/status/{post_id}"
            if post_id
            else ""
        )
        item: dict[str, Any] = {
            "kind": "social_post",
            "title": title,
            "url": url,
            "snippet": text,
            "engine": "x_api",
            "source_id": post_id,
            "author": {
                "id": author_id,
                "name": name,
                "username": username,
                "verified": bool(user.get("verified")),
            },
        }
        if post.get("created_at"):
            item["published_date"] = str(post.get("created_at"))
        if post.get("lang"):
            item["language"] = str(post.get("lang"))
        if post.get("conversation_id"):
            item["conversation_id"] = str(post.get("conversation_id"))
        metrics = post.get("public_metrics")
        if isinstance(metrics, dict):
            item["public_metrics"] = {
                str(key): value
                for key, value in metrics.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
        results.append(item)
        if len(results) >= limit:
            break
    return results


def _normalize_x_api_errors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_errors = payload.get("errors")
    if not isinstance(raw_errors, list):
        return []
    errors: list[dict[str, Any]] = []
    for item in raw_errors:
        if not isinstance(item, dict):
            continue
        error: dict[str, Any] = {
            "title": _bounded_search_text(item.get("title") or "", 300),
            "detail": _bounded_search_text(item.get("detail") or "", 1200),
            "type": str(item.get("type") or ""),
        }
        if item.get("status") is not None:
            error["status"] = item.get("status")
        errors.append(error)
    return errors[:20]


def _read_json_response(
    *,
    provider_id: str,
    provider_kind: str,
    query: str,
    endpoint: str,
    request: Request,
    timeout: float,
    max_bytes: int,
    provider_label: str,
    allow_loopback: bool = False,
    allow_private_network: bool = False,
) -> tuple[dict[str, Any] | None, ToolResult | None]:
    request._navi_allow_loopback = allow_loopback  # type: ignore[attr-defined]
    request._navi_allow_private_network = (  # type: ignore[attr-defined]
        allow_private_network
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            body = response.read(max_bytes).decode("utf-8", errors="replace")
            response_headers = getattr(response, "headers", {})
        if 300 <= status < 400:
            location = str(
                response_headers.get("location", "")
                if hasattr(response_headers, "get")
                else ""
            )
            return None, _provider_failure(
                provider_id=provider_id,
                provider_kind=provider_kind,
                query=query,
                endpoint=endpoint,
                error=f"{provider_label} redirect was rejected",
                reason="search_provider_redirect_rejected",
                retryable=False,
                extra={
                    "status_code": status,
                    "redirect_endpoint": _safe_http_endpoint(location),
                },
            )
        if status >= 400:
            return None, _provider_failure(
                provider_id=provider_id,
                provider_kind=provider_kind,
                query=query,
                endpoint=endpoint,
                error=f"{provider_label} returned HTTP {status}",
                reason="search_provider_error",
                retryable=_retryable_http_status(status),
                extra={"status_code": status},
            )
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError(f"{provider_label} returned a non-object JSON response")
        return payload, None
    except HTTPError as exc:
        status = int(exc.code)
        return None, _provider_failure(
            provider_id=provider_id,
            provider_kind=provider_kind,
            query=query,
            endpoint=endpoint,
            error=f"{provider_label} returned HTTP {status}",
            reason="search_provider_error",
            retryable=_retryable_http_status(status),
            extra={"status_code": status},
        )
    except SearchTargetRejectedError as exc:
        return None, _provider_failure(
            provider_id=provider_id,
            provider_kind=provider_kind,
            query=query,
            endpoint=endpoint,
            error=str(exc),
            reason="search_target_rejected",
            retryable=False,
            extra={
                "target_host": exc.target_host,
                "rejected_addresses": list(exc.rejected_addresses),
                "target_rejection_reason": exc.reason,
            },
        )
    except TimeoutError as exc:
        return None, _provider_failure(
            provider_id=provider_id,
            provider_kind=provider_kind,
            query=query,
            endpoint=endpoint,
            error=f"{provider_label} search request timed out",
            reason="search_timeout",
            retryable=True,
            extra={"error_type": type(exc).__name__},
        )
    except (URLError, OSError) as exc:
        return None, _provider_failure(
            provider_id=provider_id,
            provider_kind=provider_kind,
            query=query,
            endpoint=endpoint,
            error=str(exc),
            reason="search_provider_error",
            retryable=True,
            extra={"error_type": type(exc).__name__},
        )
    except (UnicodeError, ValueError) as exc:
        return None, _provider_failure(
            provider_id=provider_id,
            provider_kind=provider_kind,
            query=query,
            endpoint=endpoint,
            error=str(exc),
            reason="search_provider_error",
            retryable=False,
            extra={"error_type": type(exc).__name__},
        )


def _retryable_http_status(status: int) -> bool:
    return status in {429, 500, 502, 503, 504}


def _safe_http_endpoint(value: str) -> str:
    parsed = urlparse(str(value or ""))
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or not host:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    host_label = f"[{host}]" if ":" in host else host
    netloc = host_label if port is None else f"{host_label}:{port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def _clean_search_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _bounded_search_text(value: Any, limit: int) -> str:
    text = _clean_search_text(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()} … [truncated]"


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = _clean_search_text(item)
        if text:
            items.append(text)
    return items
