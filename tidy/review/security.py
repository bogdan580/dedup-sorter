"""Request-level security checks: token header, Host/Origin validation, body limits."""
from __future__ import annotations

import secrets
import urllib.parse
from typing import Mapping, Optional


TOKEN_HEADER = "X-Dedup-Token"


# See: docs/technical/security.md
MAX_APPLY_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB


_CSP_HEADER = (
    "default-src 'self'; img-src 'self'; style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'none'; object-src 'none'"
)


# See: docs/technical/security.md
_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost"})


def _check_host(headers: Mapping[str, str], *, port: int) -> Optional[str]:
    host_header = headers.get("Host")
    if host_header is None:
        return "missing Host header"
    hostname, sep, port_str = host_header.rpartition(":")
    if not sep or hostname.casefold() not in _LOOPBACK_HOSTNAMES or port_str != str(port):
        return f"invalid Host header: {host_header!r}"
    return None


def _check_origin(headers: Mapping[str, str], *, port: int) -> Optional[str]:
    """Origin is optional (absent on a plain same-origin GET/navigation)
    but, when present, must name this server -- accepting either loopback
    alias (see _check_host) so a page loaded via "http://localhost:<port>/"
    isn't rejected for a legitimate same-origin POST."""
    origin = headers.get("Origin")
    if origin is None:
        return None
    parsed = urllib.parse.urlsplit(origin)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "http" or hostname not in _LOOPBACK_HOSTNAMES or parsed.port != port:
        return f"invalid Origin header: {origin!r}"
    return None


def _check_mutating_request(
    headers: Mapping[str, str],
    *,
    port: int,
    expected_token: str,
) -> Optional[str]:
    """Return None if `headers` passes every check required for a mutating
    request (POST /api/apply), else a human-readable rejection reason.

    See: docs/technical/security.md"""
    token = headers.get(TOKEN_HEADER)
    if token is None or not secrets.compare_digest(
        token.encode("utf-8"), expected_token.encode("utf-8"),
    ):
        return "missing or invalid X-Dedup-Token header"
    error = _check_host(headers, port=port)
    if error is not None:
        return error
    return _check_origin(headers, port=port)
