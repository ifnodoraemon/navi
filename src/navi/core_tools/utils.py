"""Core tool handlers."""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from navi.capability_contract import CAPABILITY_ERROR_REASON_KEY

from .paths import _is_blocked_http_host, _is_public_http_host
from ..tools import ToolResult

# Caller-supplied http.fetch headers may not override these — they control
# request addressing/framing; overriding them enables smuggling/vhost confusion.
_FORBIDDEN_FETCH_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection"}
)


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
    import re
    import subprocess
    import urllib.parse

    query = args.get("query")
    if not query:
        return ToolResult(
            tool="web.search",
            ok=False,
            error="query is required",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "missing_required_argument",
                "provider": "curl_bing",
            },
        )

    encoded = urllib.parse.quote(query)
    url = f"https://www.bing.com/search?q={encoded}"

    try:
        result = subprocess.run(
            [
                "curl",
                "-sL",
                "-A",
                (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                url,
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
                    "provider": "curl_bing",
                    "curl_exit_code": result.returncode,
                    "stderr": stderr,
                },
            )
        html = result.stdout

        html = re.sub(
            r"<(script|style|svg|symbol|use|path).*?>.*?</\1>",
            " ",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()

        facts = {
            "query": query,
            "provider": "curl_bing",
            "response": {"text": text[:15000]},
        }

        return ToolResult(tool="web.search", ok=True, facts=facts)
    except subprocess.TimeoutExpired:
        return ToolResult(
            tool="web.search",
            ok=False,
            error="search request timed out",
            facts={
                CAPABILITY_ERROR_REASON_KEY: "search_timeout",
                "query": query,
                "provider": "curl_bing",
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
                "provider": "curl_bing",
                "error_type": type(exc).__name__,
            },
        )


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
    # Caller headers may not override addressing/framing headers: overriding
    # Host or Content-Length/Transfer-Encoding against the pinned IP enables
    # request smuggling and vhost confusion.
    request_headers.update(
        {
            str(k): str(v)
            for k, v in headers.items()
            if str(k).lower() not in _FORBIDDEN_FETCH_HEADERS
        }
    )

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
