"""Core tool handlers."""
from __future__ import annotations
import os
from urllib.parse import urlparse
from typing import Any
from .paths import _is_blocked_http_host, _is_public_http_host
from ..tools import ToolResult

def _truncate_output(value: str, *, limit: int = 12000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def _positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def _web_search(args: dict[str, Any]) -> ToolResult:
    import urllib.request
    import urllib.parse
    import json as _json

    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(tool="web.search", ok=False, error="query is required")
    base_url = (
        os.environ.get("NAVI_SEARCH_BASE_URL", "https://duckduckgo.com").rstrip("/")
    )
    encoded = urllib.parse.urlencode({"q": query, "format": "json", "no_html": "1"})
    url = f"{base_url}/?{encoded}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Navi/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
        return ToolResult(tool="web.search", ok=True, facts={"query": query, "response": data})
    except Exception as exc:
        return ToolResult(tool="web.search", ok=False, error=str(exc), facts={"query": query})


def _http_fetch(args: dict[str, Any]) -> ToolResult:
    import http.client
    import socket
    import ssl

    url = str(args.get("url") or "").strip()
    if not url:
        return ToolResult(tool="http.fetch", ok=False, error="url is required")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ToolResult(tool="http.fetch", ok=False, error="only http/https URLs allowed")
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host or not _is_public_http_host(host, port=port):
        return ToolResult(
            tool="http.fetch",
            ok=False,
            error="url host must be public; localhost, private, link-local, and metadata addresses are blocked",
            facts={"url": url},
        )
    # FP-4/L7: pin the resolved IP for the actual TCP connection to defeat
    # DNS rebinding. ``_is_public_http_host`` resolves once to verify; a
    # hostile resolver could flip the record between that check and a
    # second ``urlopen`` resolution. By resolving here and connecting to
    # that exact IP (with the Host header / SNI kept on the original
    # hostname), the two resolutions cannot diverge.
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return ToolResult(
            tool="http.fetch", ok=False, error=f"failed to resolve {host}: {exc}", facts={"url": url}
        )
    pinned_ip: str | None = None
    for _, _, _, _, sockaddr in infos:
        candidate = str(sockaddr[0])
        if not _is_blocked_http_host(candidate):
            pinned_ip = candidate
            break
    if pinned_ip is None:
        return ToolResult(
            tool="http.fetch",
            ok=False,
            error=f"host {host} resolved only to blocked addresses",
            facts={"url": url},
        )

    method = str(args.get("method") or "GET").upper()
    headers = args.get("headers") or {}
    body = args.get("body")
    max_bytes = _positive_int(args.get("max_bytes"), default=524288, maximum=2097152)

    path_query = parsed.path or "/"
    if parsed.query:
        path_query += "?" + parsed.query
    request_headers: dict[str, str] = {"Host": host, "User-Agent": "Navi/1.0"}
    request_headers.update({str(k): str(v) for k, v in headers.items()})

    conn: http.client.HTTPConnection | http.client.HTTPSConnection
    try:
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            raw_sock = socket.create_connection((pinned_ip, port), timeout=15)
            tls_sock = context.wrap_socket(raw_sock, server_hostname=host)
            conn = http.client.HTTPSConnection(pinned_ip, port, timeout=15, context=context)
            conn.sock = tls_sock
        else:
            conn = http.client.HTTPConnection(pinned_ip, port, timeout=15)
        conn.request(
            method,
            path_query,
            body=body.encode("utf-8") if body else None,
            headers=request_headers,
        )
        resp = conn.getresponse()
        content = resp.read(max_bytes).decode("utf-8", errors="replace")
        resp_headers = dict(resp.getheaders())
        status_code = resp.status
        conn.close()
        return ToolResult(
            tool="http.fetch",
            ok=True,
            facts={
                "url": url,
                "method": method,
                "status_code": status_code,
                "headers": resp_headers,
                "body": content,
                "truncated": len(content) >= max_bytes,
            },
        )
    except Exception as exc:
        return ToolResult(
            tool="http.fetch", ok=False, error=str(exc), facts={"url": url, "method": method}
        )

