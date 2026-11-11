"""Route table of the review handler: the POST table is the only whitelist."""
from __future__ import annotations

import json

from .helpers import _get, _post

from tidy.review.handler.routes import GET_PREFIX_ROUTES, GET_ROUTES, POST_ROUTES


class TestRouteTable:
    def test_table_sizes(self):
        assert len(GET_ROUTES) == 11
        assert len(GET_PREFIX_ROUTES) == 1
        assert len(POST_ROUTES) == 15

    def test_every_post_route_rejected_without_token(self, running_server):
        httpd, _state = running_server
        port = httpd.server_address[1]
        for path in POST_ROUTES:
            status, body, _ = _post(port, path, headers={"Host": f"127.0.0.1:{port}"}, body=b"{}")
            assert status == 403, path
            assert "error" in json.loads(body), path

    def test_every_post_route_rejected_with_wrong_token(self, running_server):
        httpd, _state = running_server
        port = httpd.server_address[1]
        for path in POST_ROUTES:
            status, _, _ = _post(
                port, path,
                headers={"Host": f"127.0.0.1:{port}", "X-Dedup-Token": "wrong"}, body=b"{}",
            )
            assert status == 403, path

    def test_unknown_post_path_is_404_even_without_token(self, running_server):
        httpd, _state = running_server
        port = httpd.server_address[1]
        status, body, _ = _post(port, "/api/nope", body=b"{}")
        assert status == 404
        assert json.loads(body) == {"error": "not found"}

    def test_unknown_get_path_is_404(self, running_server):
        httpd, _state = running_server
        port = httpd.server_address[1]
        status, body, _ = _get(port, "/api/nope")
        assert status == 404
        assert json.loads(body) == {"error": "not found"}

    def test_get_thumb_prefix_unknown_id_is_404(self, running_server):
        httpd, _state = running_server
        port = httpd.server_address[1]
        status, body, _ = _get(port, "/api/thumb/rotate")
        assert status == 404
        assert json.loads(body) == {"error": "unknown thumbnail id"}

    def test_get_query_string_does_not_change_route(self, running_server):
        httpd, _state = running_server
        port = httpd.server_address[1]
        status, _, _ = _get(port, "/api/state?x=1")
        assert status == 200
