"""Core utility tool handlers."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

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
    if not host:
        return ToolResult(tool="http.fetch", ok=False, error="url host is required")
    # The capability approval layer classifies private/local targets and
    # credentialed or mutating requests before execution.  Once approved we
    # still pin one resolved address so the reviewed target cannot be swapped
    # by a second DNS lookup.
    prepared_addresses = [
        str(item) for item in args.get("_resolved_addresses", []) if str(item).strip()
    ]
    if prepared_addresses:
        pinned_ip = prepared_addresses[0]
    else:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            return ToolResult(
                tool="http.fetch",
                ok=False,
                error=f"failed to resolve {host}: {exc}",
                facts={"url": url},
                error_reason="target_resolution_failed",
                retryable=True,
            )
        pinned_ip = str(infos[0][4][0]) if infos else ""
    if not pinned_ip:
        return ToolResult(
            tool="http.fetch",
            ok=False,
            error=f"host {host} did not resolve to a usable address",
            facts={"url": url},
            error_reason="target_resolution_failed",
            retryable=True,
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
                "resolved_address": pinned_ip,
            },
        )
    except Exception as exc:
        return ToolResult(
            tool="http.fetch",
            ok=False,
            error=str(exc),
            facts={"url": url, "method": method, "resolved_address": pinned_ip},
            error_reason="network_request_failed",
            retryable=False,
        )
