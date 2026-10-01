"""HTTP request handler for the review server.

Gate order in do_POST (preserve): Content-Length validity (400) -> body-size cap
(400) -> body read -> path whitelist = POST_ROUTES keys (404) ->
_check_mutating_request: Host/Origin/token (403) -> route function.
do_GET: _check_host (403) -> exact route -> prefix route -> 404.
"""
from __future__ import annotations

import urllib.parse
from http import HTTPStatus

from tidy.review.handler.base import ResponseHandler, read_body
from tidy.review.handler.routes import GET_PREFIX_ROUTES, GET_ROUTES, POST_ROUTES
from tidy.review.security import _check_host, _check_mutating_request
from tidy.review.state import ReviewState


def _make_handler_class(state: ReviewState):
    class Handler(ResponseHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            host_error = _check_host(self.headers, port=state.port)
            if host_error is not None:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": host_error})
                return

            parts = urllib.parse.urlsplit(self.path)
            route = GET_ROUTES.get(parts.path)
            if route is None:
                for prefix, prefix_route in GET_PREFIX_ROUTES.items():
                    if parts.path.startswith(prefix):
                        route = prefix_route
                        break
            if route is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            route(self, state, parts)

        def do_POST(self) -> None:  # noqa: N802 - stdlib method name
            body = read_body(self)
            if body is None:
                return

            parsed_path = urllib.parse.urlsplit(self.path).path
            route = POST_ROUTES.get(parsed_path)
            if route is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            error = _check_mutating_request(
                self.headers, port=state.port, expected_token=state.token,
            )
            if error is not None:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": error})
                return

            route(self, state, body)

    return Handler


make_handler_class = _make_handler_class
