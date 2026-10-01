"""Response helpers and request-body reading shared by every route module."""
from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Optional

from tidy.review.security import MAX_APPLY_BODY_BYTES, _CSP_HEADER


class ResponseHandler(BaseHTTPRequestHandler):
    server_version = "DedupReview/0.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # keep console output focused on this tool's own summaries

    def end_headers(self) -> None:
        # See: docs/technical/security.md
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, body_text: str) -> None:
        body = body_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", _CSP_HEADER)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def read_body(handler) -> Optional[bytes]:
    """Read the POST body, or answer 400 and return None.

    A malformed (non-numeric) or negative Content-Length is rejected outright,
    and an oversize one on the declared size alone, before rfile.read().
    """
    raw_content_length = handler.headers.get("Content-Length")
    content_length = 0
    if raw_content_length is not None:
        try:
            content_length = int(raw_content_length)
        except ValueError:
            content_length = -1
        if content_length < 0:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
            return None
    if content_length > MAX_APPLY_BODY_BYTES:
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "request body too large"})
        handler.close_connection = True
        return None
    return handler.rfile.read(content_length) if content_length else b""
