"""Review server over a real 127.0.0.1 socket: core endpoints, apply, open, thumbs, registries."""
from __future__ import annotations

import http.client
import io
import json
import os
import threading
from unittest.mock import MagicMock
from PIL import Image
from tidy.analysis import events
from tidy.moves import manifest
from tidy.progress import DEFAULT_WORKERS
from tidy.review import security as review_security
from tidy.review import server as review_server
from tidy.analysis import series
from fakes import FakeFileSystem

from .helpers import (
    _get,
    _post,
    _post_json,
)


class TestReviewServerHTTP:
    def test_index_page_serves_html_with_token_embedded(self, running_server):
        httpd, state = running_server
        status, body, headers = _get(state.port, "/")
        assert status == 200
        assert "text/html" in headers["Content-Type"]
        assert "test-token" in body.decode("utf-8")

    def test_index_page_title_is_the_product_name(self, running_server):
        # title/h1 should read plain "Dedup sorter", without an internal
        # phase-name suffix leaking into the UI.
        httpd, state = running_server
        status, body, headers = _get(state.port, "/")
        assert status == 200
        text = body.decode("utf-8")
        assert "Dedup sorter" in text
        assert "Faza 4B" not in text

    def test_api_state_lists_the_one_group(self, running_server):
        httpd, state = running_server
        status, body, headers = _get(state.port, "/api/state")
        assert status == 200
        payload = json.loads(body)
        assert payload["group_count"] == 1
        # The old single `applied` flag is replaced by `all_applied`
        # (whole-dataset summary) + an append-only `applied_batches` log +
        # a per-group `applied` flag.
        assert payload["all_applied"] is False
        assert payload["applied_batches"] == []
        assert payload["groups"][0]["applied"] is False
        assert payload["groups"][0]["thumb_id"] == "hash1"
        assert payload["groups"][0]["keeper"]["path"] == f"{state.archive_root}/a.jpg"

    def test_thumbnail_served_for_known_id(self, running_server):
        httpd, state = running_server
        status, body, headers = _get(state.port, "/api/thumb/hash1")
        assert status == 200
        assert headers["Content-Type"] == "image/jpeg"
        assert body[:2] == b"\xff\xd8"  # JPEG magic bytes

    def test_thumbnail_404_for_unknown_id(self, running_server):
        httpd, state = running_server
        status, body, headers = _get(state.port, "/api/thumb/does-not-exist")
        assert status == 404

    def test_unknown_route_404(self, running_server):
        httpd, state = running_server
        status, _, _ = _get(state.port, "/nope")
        assert status == 404

    def test_apply_rejected_without_token(self, running_server):
        httpd, state = running_server
        status, body, _ = _post(state.port, "/api/apply")
        assert status == 403
        assert httpd  # server must still be up, not crashed, after a rejected request
        status2, _, _ = _get(state.port, "/api/state")
        assert status2 == 200

    def test_get_state_rejected_with_spoofed_host_header(self, running_server):
        # Regression for the CRITICAL finding: GET must be Host-checked
        # too, not just POST -- otherwise a DNS-rebinding page (same-origin
        # from the browser's view, real connection to this loopback server)
        # could read every duplicate group's paths despite never being able
        # to forge a real Host header of its own.
        httpd, state = running_server
        conn = http.client.HTTPConnection("127.0.0.1", state.port, timeout=5)
        try:
            conn.putrequest("GET", "/api/state", skip_host=True)
            conn.putheader("Host", "evil.example:1234")
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 403
            resp.read()
        finally:
            conn.close()

    def test_get_index_rejected_with_spoofed_host_header(self, running_server):
        httpd, state = running_server
        conn = http.client.HTTPConnection("127.0.0.1", state.port, timeout=5)
        try:
            conn.putrequest("GET", "/", skip_host=True)
            conn.putheader("Host", "evil.example:1234")
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 403
            resp.read()
        finally:
            conn.close()

    def test_get_state_accepted_via_localhost_alias(self, running_server):
        httpd, state = running_server
        conn = http.client.HTTPConnection("127.0.0.1", state.port, timeout=5)
        try:
            conn.putrequest("GET", "/api/state", skip_host=True)
            conn.putheader("Host", f"localhost:{state.port}")
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 200
            resp.read()
        finally:
            conn.close()

    def test_apply_rejected_with_negative_content_length(self, running_server):
        httpd, state = running_server
        conn = http.client.HTTPConnection("127.0.0.1", state.port, timeout=5)
        try:
            conn.putrequest("POST", "/api/apply", skip_host=False)
            conn.putheader("X-Dedup-Token", "test-token")
            conn.putheader("Content-Length", "-1")
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 400
            resp.read()
        finally:
            conn.close()

        # Server must still be responsive afterwards -- a negative
        # Content-Length must not hang or crash the handler thread.
        status, _, _ = _get(state.port, "/api/state")
        assert status == 200

    def test_apply_rejected_with_wrong_host_header(self, running_server):
        httpd, state = running_server
        conn = http.client.HTTPConnection("127.0.0.1", state.port, timeout=5)
        try:
            # Bypass http.client's auto Host header to send a spoofed one,
            # simulating a DNS-rebinding request.
            conn.putrequest("POST", "/api/apply", skip_host=True)
            conn.putheader("Host", "evil.example:1234")
            conn.putheader("X-Dedup-Token", "test-token")
            conn.putheader("Content-Length", "0")
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 403
            resp.read()
        finally:
            conn.close()

    def _selection_payload_for(self, state):
        # Discovers ids via GET /api/state, exactly as a real client would
        # -- never invents an id, mirroring the security model this
        # payload is meant to exercise.
        _, state_body, _ = _get(state.port, "/api/state")
        group = json.loads(state_body)["groups"][0]
        return {"selections": [{
            "group_id": group["group_id"],
            "keeper_id": group["keeper"]["id"],
            "discard_ids": [d["id"] for d in group["discarded"]],
        }]}

    def test_apply_moves_the_discarded_file_and_rejects_resubmission_of_same_group(self, running_server):
        httpd, state = running_server
        payload = self._selection_payload_for(state)
        status, body, _ = _post_json(
            state.port, "/api/apply", headers={"X-Dedup-Token": "test-token"}, payload=payload,
        )
        assert status == 200
        result = json.loads(body)
        assert result["moved"] == 1
        assert result["failed"] == 0

        # A second click resubmitting the
        # SAME group_id must be rejected server-side (409, "already
        # applied") -- the file is already gone, so silently re-running
        # apply_batch() on it would either no-op confusingly or move
        # whatever now happens to occupy that path.
        status2, body2, _ = _post_json(
            state.port, "/api/apply", headers={"X-Dedup-Token": "test-token"}, payload=payload,
        )
        assert status2 == 409
        assert "already applied" in json.loads(body2)["error"]

    def test_state_reflects_applied_after_apply(self, running_server):
        httpd, state = running_server
        payload = self._selection_payload_for(state)
        _post_json(state.port, "/api/apply", headers={"X-Dedup-Token": "test-token"}, payload=payload)
        status, body, _ = _get(state.port, "/api/state")
        payload_out = json.loads(body)
        assert payload_out["all_applied"] is True
        assert payload_out["groups"][0]["applied"] is True
        assert len(payload_out["applied_batches"]) == 1


class TestApiApplySelectionsHTTP:
    """Acceptance criteria, over a real socket."""

    def test_unknown_id_rejected_and_referenced_file_not_moved(self, running_server_multi):
        httpd, state, fs = running_server_multi
        g1_b = f"{state.archive_root}/g1/b.jpg"
        g1_c = f"{state.archive_root}/g1/c.jpg"
        payload = {"selections": [
            {"group_id": "1", "keeper_id": "1:0", "discard_ids": ["1:1", "does-not-exist"]},
        ]}
        status, body, _ = _post_json(
            state.port, "/api/apply", headers={"X-Dedup-Token": "test-token"}, payload=payload,
        )
        assert status == 400
        assert "unknown id" in json.loads(body)["error"]
        # The file that WOULD have been referenced (g1/b.jpg, id "1:1")
        # must be confirmed untouched -- and so must every other file.
        assert fs.exists(g1_b) is True
        assert fs.exists(g1_c) is True

        status2, _, _ = _get(state.port, "/api/state")
        assert status2 == 200  # server survives a rejected request

    def test_id_from_wrong_group_rejected_and_nothing_moved(self, running_server_multi):
        httpd, state, fs = running_server_multi
        payload = {"selections": [
            {"group_id": "1", "keeper_id": "1:0", "discard_ids": ["2:1"]},  # "2:1" belongs to group 2
        ]}
        status, body, _ = _post_json(
            state.port, "/api/apply", headers={"X-Dedup-Token": "test-token"}, payload=payload,
        )
        assert status == 400
        assert "does not belong" in json.loads(body)["error"]
        assert fs.exists(f"{state.archive_root}/g1/b.jpg") is True
        assert fs.exists(f"{state.archive_root}/g2/b.jpg") is True

    def test_group_without_exactly_one_keeper_rejected_before_anything_moves(self, running_server_multi):
        httpd, state, fs = running_server_multi
        payload = {"selections": [
            {"group_id": "1", "keeper_id": "", "discard_ids": ["1:1", "1:2"]},
        ]}
        status, body, _ = _post_json(
            state.port, "/api/apply", headers={"X-Dedup-Token": "test-token"}, payload=payload,
        )
        assert status == 400
        assert fs.exists(f"{state.archive_root}/g1/a.jpg") is True
        assert fs.exists(f"{state.archive_root}/g1/b.jpg") is True
        assert fs.exists(f"{state.archive_root}/g1/c.jpg") is True

    def test_spoofed_host_still_403_with_a_valid_selections_body(self, running_server_multi):
        httpd, state, fs = running_server_multi
        payload = {"selections": [{"group_id": "1", "keeper_id": "1:0", "discard_ids": ["1:1"]}]}
        body_bytes = json.dumps(payload).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", state.port, timeout=5)
        try:
            conn.putrequest("POST", "/api/apply", skip_host=True)
            conn.putheader("Host", "evil.example:1234")
            conn.putheader("X-Dedup-Token", "test-token")
            conn.putheader("Content-Length", str(len(body_bytes)))
            conn.endheaders()
            conn.send(body_bytes)
            resp = conn.getresponse()
            assert resp.status == 403
            resp.read()
        finally:
            conn.close()
        assert fs.exists(f"{state.archive_root}/g1/b.jpg") is True

    def test_missing_token_still_403_with_a_valid_selections_body(self, running_server_multi):
        httpd, state, fs = running_server_multi
        payload = {"selections": [{"group_id": "1", "keeper_id": "1:0", "discard_ids": ["1:1"]}]}
        status, body, _ = _post_json(state.port, "/api/apply", headers={}, payload=payload)
        assert status == 403
        assert fs.exists(f"{state.archive_root}/g1/b.jpg") is True

    def test_selective_apply_moves_only_the_specified_files(self, running_server_multi):
        httpd, state, fs = running_server_multi
        # Only group 1's first discard ("1:1", b.jpg) is selected: group
        # 1's second discard (c.jpg) and the whole of group 2 must stay put.
        payload = {"selections": [
            {"group_id": "1", "keeper_id": "1:0", "discard_ids": ["1:1"]},
        ]}
        status, body, _ = _post_json(
            state.port, "/api/apply", headers={"X-Dedup-Token": "test-token"}, payload=payload,
        )
        assert status == 200
        result = json.loads(body)
        assert result["moved"] == 1
        assert result["failed"] == 0

        assert fs.exists(f"{state.archive_root}/g1/a.jpg") is True  # keeper, never moves
        assert fs.exists(f"{state.archive_root}/g1/b.jpg") is False  # the one selected discard
        assert fs.exists(f"{state.archive_root}/g1/c.jpg") is True  # same group, NOT selected
        assert fs.exists(f"{state.archive_root}/g2/a.jpg") is True  # different group, untouched
        assert fs.exists(f"{state.archive_root}/g2/b.jpg") is True  # different group, untouched

        final_rows = manifest.read_final_rows(result["manifest_path"])
        assert len(final_rows) == 1
        (row,) = final_rows.values()
        assert row.status == manifest.STATUS_DONE

    def test_multiple_selections_apply_across_groups_selectively(self, running_server_multi):
        httpd, state, fs = running_server_multi
        payload = {"selections": [
            {"group_id": "1", "keeper_id": "1:0", "discard_ids": ["1:1", "1:2"]},
            {"group_id": "2", "keeper_id": "2:0", "discard_ids": ["2:1"]},
        ]}
        status, body, _ = _post_json(
            state.port, "/api/apply", headers={"X-Dedup-Token": "test-token"}, payload=payload,
        )
        assert status == 200
        result = json.loads(body)
        assert result["moved"] == 3
        assert result["failed"] == 0
        final_rows = manifest.read_final_rows(result["manifest_path"])
        assert len(final_rows) == 3

    def test_malformed_json_body_rejected_with_400_not_500(self, running_server):
        httpd, state = running_server
        status, body, _ = _post(
            state.port, "/api/apply",
            headers={"X-Dedup-Token": "test-token", "Content-Type": "application/json"},
            body=b"{not valid json",
        )
        assert status == 400
        status2, _, _ = _get(state.port, "/api/state")
        assert status2 == 200  # server survives a JSON parse error

    def test_missing_selections_key_rejected_with_400(self, running_server):
        httpd, state = running_server
        status, body, _ = _post_json(
            state.port, "/api/apply", headers={"X-Dedup-Token": "test-token"}, payload={},
        )
        assert status == 400

    def test_apply_rejected_with_body_too_large(self, running_server):
        httpd, state = running_server
        conn = http.client.HTTPConnection("127.0.0.1", state.port, timeout=5)
        try:
            conn.putrequest("POST", "/api/apply", skip_host=False)
            conn.putheader("X-Dedup-Token", "test-token")
            conn.putheader("Content-Length", str(review_security.MAX_APPLY_BODY_BYTES + 1))
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 400
            resp.read()
        finally:
            conn.close()

        # Server must still be responsive -- an oversized declared
        # Content-Length must not hang this handler thread reading a body
        # that (in this test) was never actually sent.
        status, _, _ = _get(state.port, "/api/state")
        assert status == 200


class TestApiOpenHTTP:
    """POST /api/open: {"file_id": "<id>"} opens that file with the OS
    default app via os.startfile(), gated by the exact same
    _check_mutating_request() (Host/Origin/token) as POST /api/apply, and
    with `file_id` resolved exclusively through `state.file_registry` --
    mirrors TestApiApplySelectionsHTTP's approach of driving a real socket
    to genuinely exercise the security gate, while mocking the actual
    side-effecting OS call so tests never launch a real application."""

    def _mock_startfile(self, monkeypatch):
        mock_startfile = MagicMock()
        # raising=False: os.startfile only exists on Windows, but the mock
        # must still be installable (and later asserted "not called") even
        # were this suite ever run on a non-Windows interpreter.
        monkeypatch.setattr(os, "startfile", mock_startfile, raising=False)
        return mock_startfile

    def test_open_calls_startfile_with_registry_resolved_path(self, running_server, monkeypatch):
        httpd, state = running_server
        mock_startfile = self._mock_startfile(monkeypatch)

        status, body, _ = _post_json(
            state.port, "/api/open",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": "1:0"},
        )
        assert status == 200
        assert json.loads(body) == {"opened": "1:0"}
        mock_startfile.assert_called_once_with(f"{state.archive_root}/a.jpg")

    def test_open_rejected_without_token_and_startfile_not_called(self, running_server, monkeypatch):
        httpd, state = running_server
        mock_startfile = self._mock_startfile(monkeypatch)

        status, body, _ = _post_json(
            state.port, "/api/open", headers={}, payload={"file_id": "1:0"},
        )
        assert status == 403
        mock_startfile.assert_not_called()
        status2, _, _ = _get(state.port, "/api/state")
        assert status2 == 200  # server survives a rejected request

    def test_open_rejected_with_wrong_host_header_and_startfile_not_called(self, running_server, monkeypatch):
        httpd, state = running_server
        mock_startfile = self._mock_startfile(monkeypatch)

        body_bytes = json.dumps({"file_id": "1:0"}).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", state.port, timeout=5)
        try:
            # Bypass http.client's auto Host header to send a spoofed one,
            # simulating a DNS-rebinding request (same pattern as the
            # equivalent /api/apply test above).
            conn.putrequest("POST", "/api/open", skip_host=True)
            conn.putheader("Host", "evil.example:1234")
            conn.putheader("X-Dedup-Token", "test-token")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body_bytes)))
            conn.endheaders()
            conn.send(body_bytes)
            resp = conn.getresponse()
            assert resp.status == 403
            resp.read()
        finally:
            conn.close()
        mock_startfile.assert_not_called()

    def test_open_rejected_with_wrong_origin_header_and_startfile_not_called(self, running_server, monkeypatch):
        httpd, state = running_server
        mock_startfile = self._mock_startfile(monkeypatch)

        body_bytes = json.dumps({"file_id": "1:0"}).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", state.port, timeout=5)
        try:
            # Host is legitimate here -- only Origin is spoofed, exactly
            # the DNS-rebinding scenario _check_origin() exists to catch.
            conn.putrequest("POST", "/api/open", skip_host=False)
            conn.putheader("X-Dedup-Token", "test-token")
            conn.putheader("Origin", "http://evil.example:9999")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body_bytes)))
            conn.endheaders()
            conn.send(body_bytes)
            resp = conn.getresponse()
            assert resp.status == 403
            resp.read()
        finally:
            conn.close()
        mock_startfile.assert_not_called()

    def test_open_unknown_file_id_rejected_with_400_and_startfile_not_called(self, running_server, monkeypatch):
        httpd, state = running_server
        mock_startfile = self._mock_startfile(monkeypatch)

        status, body, _ = _post_json(
            state.port, "/api/open",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": "does-not-exist"},
        )
        assert status == 400
        assert "unknown id" in json.loads(body)["error"]
        mock_startfile.assert_not_called()

    def test_open_rejects_a_raw_filesystem_path_not_in_registry(self, running_server, monkeypatch):
        # A client must never be able to make this endpoint open an
        # arbitrary path just by sending it as file_id -- even a path that
        # is genuinely valid for this server (archive_root/a.jpg, the real
        # keeper) must be rejected unless it is submitted as its
        # file_registry id ("1:0"), not the raw string.
        httpd, state = running_server
        mock_startfile = self._mock_startfile(monkeypatch)

        status, body, _ = _post_json(
            state.port, "/api/open",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": f"{state.archive_root}/a.jpg"},
        )
        assert status == 400
        mock_startfile.assert_not_called()

    def test_open_missing_file_id_rejected_with_400_and_startfile_not_called(self, running_server, monkeypatch):
        httpd, state = running_server
        mock_startfile = self._mock_startfile(monkeypatch)

        status, body, _ = _post_json(
            state.port, "/api/open", headers={"X-Dedup-Token": "test-token"}, payload={},
        )
        assert status == 400
        mock_startfile.assert_not_called()

    def test_open_rejected_via_get_not_allowed(self, running_server, monkeypatch):
        # /api/open must be POST-only, exactly like /api/apply -- launching
        # a program is a side effect, so a GET (cacheable, prefetchable,
        # triggerable by a bare <img>/link) must never reach it.
        httpd, state = running_server
        mock_startfile = self._mock_startfile(monkeypatch)

        status, body, _ = _get(state.port, "/api/open")
        assert status == 404
        mock_startfile.assert_not_called()

    def test_open_rejected_with_body_too_large(self, running_server, monkeypatch):
        # Reuses MAX_APPLY_BODY_BYTES, the exact same limit /api/apply
        # enforces -- do_POST() checks Content-Length before ever reading
        # the body, for either route.
        httpd, state = running_server
        mock_startfile = self._mock_startfile(monkeypatch)

        conn = http.client.HTTPConnection("127.0.0.1", state.port, timeout=5)
        try:
            conn.putrequest("POST", "/api/open", skip_host=False)
            conn.putheader("X-Dedup-Token", "test-token")
            conn.putheader("Content-Length", str(review_security.MAX_APPLY_BODY_BYTES + 1))
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 400
            resp.read()
        finally:
            conn.close()
        mock_startfile.assert_not_called()

        status, _, _ = _get(state.port, "/api/state")
        assert status == 200

    def test_open_malformed_json_body_rejected_with_400_not_500(self, running_server, monkeypatch):
        httpd, state = running_server
        mock_startfile = self._mock_startfile(monkeypatch)

        status, body, _ = _post(
            state.port, "/api/open",
            headers={"X-Dedup-Token": "test-token", "Content-Type": "application/json"},
            body=b"{not valid json",
        )
        assert status == 400
        mock_startfile.assert_not_called()
        status2, _, _ = _get(state.port, "/api/state")
        assert status2 == 200  # server survives a JSON parse error


class TestApiThumbRotateHTTP:
    """POST /api/thumb/rotate: {"thumb_id": "<id>"} rotates the cached
    thumbnail 90 degrees clockwise in place, gated by the same
    _check_mutating_request() as /api/apply and /api/open, with thumb_id
    resolved exclusively through state.thumb_sources -- the exact same
    allowlist GET /api/thumb/<id> already trusts, so no new authorization
    surface was introduced. running_server's one dup group uses
    full_hash "hash1" as its shared thumb_id (group.full_hash, since
    every file in an exact-duplicate group is byte-identical)."""

    def test_rotate_swaps_thumbnail_dimensions(self, running_server):
        httpd, state = running_server
        # Force generation first -- mirrors how the browser always fetches
        # the thumbnail before a user could ever click its rotate button.
        status, original_body, _ = _get(state.port, "/api/thumb/hash1")
        assert status == 200
        with Image.open(io.BytesIO(original_body)) as img:
            original_size = img.size  # (12, 8) from _jpeg_bytes() above -- non-square

        status, body, _ = _post_json(
            state.port, "/api/thumb/rotate",
            headers={"X-Dedup-Token": "test-token"}, payload={"thumb_id": "hash1"},
        )
        assert status == 200
        assert json.loads(body) == {"rotated": "hash1"}

        status, rotated_body, _ = _get(state.port, "/api/thumb/hash1")
        assert status == 200
        with Image.open(io.BytesIO(rotated_body)) as img:
            assert img.size == (original_size[1], original_size[0])

    def test_rotate_unknown_thumb_id_returns_404(self, running_server):
        httpd, state = running_server
        status, body, _ = _post_json(
            state.port, "/api/thumb/rotate",
            headers={"X-Dedup-Token": "test-token"}, payload={"thumb_id": "no-such-id"},
        )
        assert status == 404
        assert "unknown thumbnail id" in json.loads(body)["error"]

    def test_rotate_missing_thumb_id_rejected_with_400(self, running_server):
        httpd, state = running_server
        status, body, _ = _post_json(
            state.port, "/api/thumb/rotate",
            headers={"X-Dedup-Token": "test-token"}, payload={},
        )
        assert status == 400

    def test_rotate_rejected_without_token(self, running_server):
        httpd, state = running_server
        status, body, _ = _post_json(
            state.port, "/api/thumb/rotate", headers={}, payload={"thumb_id": "hash1"},
        )
        assert status == 403

    def test_rotate_rejects_a_raw_filesystem_path_not_a_registered_thumb_id(self, running_server):
        httpd, state = running_server
        status, body, _ = _post_json(
            state.port, "/api/thumb/rotate",
            headers={"X-Dedup-Token": "test-token"}, payload={"thumb_id": f"{state.archive_root}/a.jpg"},
        )
        assert status == 404


class TestKrok5MultiBatchApply:
    """Acceptance criteria, over a real socket:
    applied_group_ids/applied_batches replace the old one-shot
    applied_result lock. Each successful apply must create its own batch
    directory + manifest (never merging into a previous one), and
    resubmitting an already-applied group_id -- alone or mixed into a
    request with brand-new groups -- must be rejected server-side (409)
    without moving anything."""

    def _selection_for(self, state, group_id):
        _, body, _ = _get(state.port, "/api/state")
        group = next(g for g in json.loads(body)["groups"] if g["group_id"] == group_id)
        return {
            "group_id": group_id,
            "keeper_id": group["keeper"]["id"],
            "discard_ids": [d["id"] for d in group["discarded"]],
        }

    def _apply(self, state, selections):
        return _post_json(
            state.port, "/api/apply", headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections},
        )

    def test_two_applies_for_different_groups_get_separate_batch_dirs_and_manifests(
        self, running_server_four_groups,
    ):
        httpd, state, fs = running_server_four_groups
        g1_b = f"{state.archive_root}/g1/b.jpg"
        g2_b = f"{state.archive_root}/g2/b.jpg"

        status1, body1, _ = self._apply(state, [self._selection_for(state, "1")])
        assert status1 == 200
        result1 = json.loads(body1)

        status2, body2, _ = self._apply(state, [self._selection_for(state, "2")])
        assert status2 == 200
        result2 = json.loads(body2)

        # Acceptance: two separate batch directories and manifests, never
        # the same one reused/appended-into for a second, distinct apply.
        assert result1["batch_dir"] != result2["batch_dir"]
        assert result1["manifest_path"] != result2["manifest_path"]

        # Zero double moves, verified via the filesystem: each batch's own
        # manifest mentions only the file IT moved.
        rows1 = manifest.read_final_rows(result1["manifest_path"])
        rows2 = manifest.read_final_rows(result2["manifest_path"])
        assert set(rows1.keys()) == {g1_b}
        assert set(rows2.keys()) == {g2_b}

        # And batch 1 never touched group 2's files, nor batch 2 group 1's.
        assert fs.exists(g1_b) is False
        assert fs.exists(g2_b) is False
        assert fs.exists(f"{state.archive_root}/g1/a.jpg") is True
        assert fs.exists(f"{state.archive_root}/g2/a.jpg") is True

    def test_state_accumulates_applied_batches_and_marks_applied_groups(
        self, running_server_four_groups,
    ):
        httpd, state, fs = running_server_four_groups
        self._apply(state, [self._selection_for(state, "1")])
        self._apply(state, [self._selection_for(state, "2")])

        status, body, _ = _get(state.port, "/api/state")
        assert status == 200
        payload_out = json.loads(body)

        # applied_batches is append-only -- two successful applies means
        # two entries, never overwriting one another.
        assert len(payload_out["applied_batches"]) == 2
        assert (
            payload_out["applied_batches"][0]["batch_dir"]
            != payload_out["applied_batches"][1]["batch_dir"]
        )

        applied_flags = {g["group_id"]: g["applied"] for g in payload_out["groups"]}
        assert applied_flags == {"1": True, "2": True, "3": False, "4": False}
        assert payload_out["all_applied"] is False

    def test_resubmitting_already_applied_group_rejected_and_creates_no_second_batch(
        self, running_server_four_groups,
    ):
        httpd, state, fs = running_server_four_groups
        selection1 = self._selection_for(state, "1")
        status1, _, _ = self._apply(state, [selection1])
        assert status1 == 200

        status2, body2, _ = self._apply(state, [selection1])
        assert status2 == 409
        assert "already applied" in json.loads(body2)["error"]

        _, state_body, _ = _get(state.port, "/api/state")
        # The rejected resend created no second batch/manifest.
        assert len(json.loads(state_body)["applied_batches"]) == 1

    def test_mixing_already_applied_and_new_group_rejects_whole_request(
        self, running_server_four_groups,
    ):
        httpd, state, fs = running_server_four_groups
        selection1 = self._selection_for(state, "1")
        self._apply(state, [selection1])

        selection3 = self._selection_for(state, "3")
        status, body, _ = self._apply(state, [selection1, selection3])
        assert status == 409

        # The whole mixed request is rejected -- group 3's file, which
        # would otherwise have been perfectly valid on its own, must NOT
        # have moved just because it was submitted alongside an
        # already-applied group (same all-or-nothing principle as step 3's
        # shape validation, applied here to "already applied" conflicts).
        assert fs.exists(f"{state.archive_root}/g3/b.jpg") is True
        _, state_body, _ = _get(state.port, "/api/state")
        assert len(json.loads(state_body)["applied_batches"]) == 1

    def test_apply_page_slice_moves_only_that_pages_groups(self, running_server_four_groups):
        # Mirrors the client's currentPageGroups() arithmetic exactly
        # (pageSize=2, page=2 of 4 groups -> groups 3-4, 1-indexed
        # start=(page-1)*pageSize) to prove the server-side selective-apply
        # mechanism genuinely restricts a move to "exactly this request's
        # selections", which is what makes "Zastosuj biezaca strone" only
        # move the visible page true in practice. (The page slicing itself
        # is client-side JS -- pinned separately, structurally, in
        # TestApplyButtonsKrok5 below, since this repo has no browser/JS
        # execution harness to actually click the button and observe the
        # resulting fetch() body.)
        httpd, state, fs = running_server_four_groups
        _, body, _ = _get(state.port, "/api/state")
        all_groups = json.loads(body)["groups"]
        page_size = 2
        page = 2
        start = (page - 1) * page_size
        page_groups = all_groups[start:start + page_size]
        assert [g["group_id"] for g in page_groups] == ["3", "4"]

        selections = [
            {
                "group_id": g["group_id"],
                "keeper_id": g["keeper"]["id"],
                "discard_ids": [d["id"] for d in g["discarded"]],
            }
            for g in page_groups
        ]
        status, body, _ = self._apply(state, selections)
        assert status == 200
        result = json.loads(body)
        assert result["moved"] == 2
        assert sorted(result["group_ids"]) == ["3", "4"]

        assert fs.exists(f"{state.archive_root}/g3/b.jpg") is False
        assert fs.exists(f"{state.archive_root}/g4/b.jpg") is False
        # Groups NOT on this page must be untouched.
        assert fs.exists(f"{state.archive_root}/g1/b.jpg") is True
        assert fs.exists(f"{state.archive_root}/g2/b.jpg") is True


class TestApiStateStaysUnpaginated:
    def test_api_state_returns_all_120_groups_unpaginated(self, running_server_many_groups):
        # Acceptance: with 120 groups, pagination must be entirely
        # client-side -- /api/state's shape/contract does not change.
        httpd, state = running_server_many_groups
        status, body, _ = _get(state.port, "/api/state")
        assert status == 200
        payload = json.loads(body)
        assert payload["group_count"] == 120
        assert len(payload["groups"]) == 120


# --- full realistic session, end-to-end ---------------------------------
#
# Everything below is individually covered elsewhere in this file (pure
# view-model building, security gating, selection validation, split apply-
# all/apply-page batching) -- this class is the genuine coverage GAP: a
# single session stringing several of those pieces together over a real
# socket -- multi-page /api/state,
# a non-default keeper pick, a per-file exclude, a whole-group skip, a
# page-scoped apply, then further per-page applies for the "remaining"
# pages -- to prove they compose correctly together, not just in isolation.
class TestFullReviewFlowEndToEnd:
    PAGE_SIZE = 3

    def _all_groups(self, state):
        _, body, _ = _get(state.port, "/api/state")
        return json.loads(body)["groups"]

    def _page(self, groups, page):
        start = (page - 1) * self.PAGE_SIZE
        return groups[start:start + self.PAGE_SIZE]

    def _default_selection(self, g):
        # Mirrors buildSelectionsPayload()'s untouched-group case exactly:
        # server's own keeper id, every discarded id, nothing excluded.
        return {
            "group_id": g["group_id"],
            "keeper_id": g["keeper"]["id"],
            "discard_ids": [d["id"] for d in g["discarded"]],
        }

    def _apply(self, state, selections):
        return _post_json(
            state.port, "/api/apply", headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections},
        )

    def test_full_session_keeper_swap_exclude_skip_and_split_apply(
        self, running_server_seven_groups,
    ):
        httpd, state, fs = running_server_seven_groups

        # 1. Load state exactly as the client's loadState() would: complete,
        # unpaginated data for all 7 groups even though the UI only shows
        # PAGE_SIZE=3 at a time (pagination is client-side only).
        all_groups = self._all_groups(state)
        assert len(all_groups) == 7

        page1 = self._page(all_groups, 1)
        page2 = self._page(all_groups, 2)
        page3 = self._page(all_groups, 3)
        assert [g["group_id"] for g in page1] == ["1", "2", "3"]
        assert [g["group_id"] for g in page2] == ["4", "5", "6"]
        assert [g["group_id"] for g in page3] == ["7"]

        g1, g2, g3 = page1
        g4, g5, g6 = page2
        (g7,) = page3

        # 2. Build page 1's selection: group 1 untouched (default), group 2
        # gets a non-default keeper -- its one discard becomes the keeper
        # and the old keeper becomes the (only) thing that moves (a
        # card click) -- and group 3 excludes one of its two discards
        # individually (the per-file checkbox) while still moving the
        # other.
        g2_new_keeper_id = g2["discarded"][0]["id"]
        g2_old_keeper_id = g2["keeper"]["id"]
        g3_moved_id = g3["discarded"][0]["id"]  # b.jpg moves
        # g3["discarded"][1]["id"] (c.jpg) is deliberately left OUT of
        # discard_ids below -- the per-file "pomin ten plik" checkbox.

        page1_selections = [
            self._default_selection(g1),
            {
                "group_id": g2["group_id"],
                "keeper_id": g2_new_keeper_id,
                "discard_ids": [g2_old_keeper_id],
            },
            {
                "group_id": g3["group_id"],
                "keeper_id": g3["keeper"]["id"],
                "discard_ids": [g3_moved_id],
            },
        ]

        # 3. "Zastosuj biezaca strone" for page 1.
        status, body, _ = self._apply(state, page1_selections)
        assert status == 200
        result1 = json.loads(body)
        assert result1["moved"] == 3
        assert result1["failed"] == 0
        assert sorted(result1["group_ids"]) == ["1", "2", "3"]

        g1_root = f"{state.archive_root}/g1"
        g2_root = f"{state.archive_root}/g2"
        g3_root = f"{state.archive_root}/g3"

        # Group 1: default -- keeper stays, discard moves.
        assert fs.exists(f"{g1_root}/a.jpg") is True
        assert fs.exists(f"{g1_root}/b.jpg") is False

        # Group 2: keeper swap -- the ORIGINAL keeper (a.jpg) is the one
        # that actually moves now, and the original discard (b.jpg) stays
        # (it's the new keeper).
        assert fs.exists(f"{g2_root}/a.jpg") is False
        assert fs.exists(f"{g2_root}/b.jpg") is True

        # Group 3: only b.jpg (individually selected) moves; c.jpg, the
        # excluded file, and a.jpg, the keeper, both stay put.
        assert fs.exists(f"{g3_root}/a.jpg") is True
        assert fs.exists(f"{g3_root}/b.jpg") is False
        assert fs.exists(f"{g3_root}/c.jpg") is True

        # 4. Manifest for batch 1 has exactly the 3 moved files -- and the
        # swapped-in discard's reason falls back to "user-selected" (it
        # started life as the server's own keeper, reason=None, so the
        # generic fallback in _validate_selections() is what supplies a
        # reason for it at all).
        manifest_rows_1 = manifest.read_final_rows(result1["manifest_path"])
        assert len(manifest_rows_1) == 3
        by_src = {row.src_abs: row for row in manifest_rows_1.values()}
        assert set(by_src) == {
            f"{g1_root}/b.jpg", f"{g2_root}/a.jpg", f"{g3_root}/b.jpg",
        }
        assert all(row.status == manifest.STATUS_DONE for row in by_src.values())
        assert by_src[f"{g2_root}/a.jpg"].reason == "user-selected"
        assert by_src[f"{g2_root}/a.jpg"].keeper_abs == f"{g2_root}/b.jpg"

        # 5. State after page 1: groups 1-3 applied, 4-7 not yet.
        applied_after_page1 = {g["group_id"]: g["applied"] for g in self._all_groups(state)}
        assert applied_after_page1 == {
            "1": True, "2": True, "3": True,
            "4": False, "5": False, "6": False, "7": False,
        }

        # 6. Page 2: group 5 is skipped entirely by the user (the
        # whole-group "pomin ta grupe" checkbox) -- its selection is simply
        # omitted from the payload, exactly as buildSelectionsPayload()
        # does for a skipped group.
        page2_selections = [self._default_selection(g4), self._default_selection(g6)]
        status, body, _ = self._apply(state, page2_selections)
        assert status == 200
        result2 = json.loads(body)
        assert result2["moved"] == 2
        assert sorted(result2["group_ids"]) == ["4", "6"]

        assert fs.exists(f"{state.archive_root}/g4/b.jpg") is False
        assert fs.exists(f"{state.archive_root}/g5/a.jpg") is True  # skipped -- untouched
        assert fs.exists(f"{state.archive_root}/g5/b.jpg") is True  # skipped -- untouched
        assert fs.exists(f"{state.archive_root}/g6/b.jpg") is False

        # 7. "apply remaining pages": page 3, default selection.
        page3_selections = [self._default_selection(g7)]
        status, body, _ = self._apply(state, page3_selections)
        assert status == 200
        result3 = json.loads(body)
        assert result3["moved"] == 1
        assert result3["group_ids"] == ["7"]
        assert fs.exists(f"{state.archive_root}/g7/b.jpg") is False

        # 8. Final tally: three separate batches (one per page-apply), each
        # with its own directory/manifest, zero double moves, and a grand
        # total across all batches that matches exactly what was actually
        # selected -- 6 of 7 groups (group 5 was skipped and never
        # applied), one discard short in group 3 (the excluded file never
        # moved).
        final_state = json.loads(_get(state.port, "/api/state")[1])
        assert len(final_state["applied_batches"]) == 3
        batch_dirs = {b["batch_dir"] for b in final_state["applied_batches"]}
        assert len(batch_dirs) == 3  # every apply got its own batch directory
        manifest_paths = {b["manifest_path"] for b in final_state["applied_batches"]}
        assert len(manifest_paths) == 3

        total_moved = sum(b["moved"] for b in final_state["applied_batches"])
        assert total_moved == 6  # 3 (page1) + 2 (page2) + 1 (page3)

        applied_flags_final = {g["group_id"]: g["applied"] for g in final_state["groups"]}
        assert applied_flags_final == {
            "1": True, "2": True, "3": True, "4": True,
            "5": False, "6": True, "7": True,
        }
        # Group 5 was deliberately skipped throughout -- never applied, so
        # the dataset as a whole is correctly NOT "all applied".
        assert final_state["all_applied"] is False

        # Filesystem-level ground truth, one more time, for everything
        # that must have survived the whole session untouched.
        assert fs.exists(f"{state.archive_root}/g5/a.jpg") is True
        assert fs.exists(f"{state.archive_root}/g5/b.jpg") is True


class TestOpenRegistrySeparationHTTP:
    def test_series_registry_id_rejected_by_apply_and_nothing_moved(self, running_server_multi):
        httpd, state, fs = running_server_multi
        # Simulate a finished series computation having populated
        # series_registry (build_series_views() does exactly this) with a
        # file that is NOT part of any duplicate group in file_registry.
        extra_path = f"{state.archive_root}/g1/a.jpg"  # a REAL file on the fake fs, but not via this id
        state.series_registry["s1:0"] = {
            "path": extra_path, "group_id": "s1", "size": 100, "hash": "", "reason": "dup-visual",
        }

        status, body, _ = _post_json(
            state.port, "/api/apply",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": [{"group_id": "s1", "keeper_id": "s1:0", "discard_ids": []}]},
        )
        assert status == 400
        assert "unknown id" in json.loads(body)["error"]
        # Nothing on the fake disk moved, and no group was ever marked applied.
        assert fs.move_calls == []
        assert state.applied_group_ids == set()
        assert fs.exists(extra_path) is True

    def test_events_registry_id_rejected_by_apply_series_and_nothing_moved(self, running_server_multi):
        # The third registry separation case -- an events_registry
        # id must be un-resolvable by POST /api/apply-series too, exactly like
        # it's un-resolvable by POST /api/apply above. Simulates a finished
        # events computation having populated events_registry
        # (build_events_views() does exactly this).
        httpd, state, fs = running_server_multi
        extra_path = f"{state.archive_root}/g1/a.jpg"
        state.events_registry["e1:0"] = {"path": extra_path, "group_id": "e1", "size": 100}

        status, body, _ = _post_json(
            state.port, "/api/apply-series",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": [{"group_id": "e1", "keep_ids": ["e1:0"], "discard_ids": []}]},
        )
        assert status == 400
        assert "unknown id" in json.loads(body)["error"]
        assert fs.move_calls == []
        assert state.applied_series_group_ids == set()
        assert fs.exists(extra_path) is True

    def test_series_and_events_registry_ids_accepted_by_open(self, running_server_multi, monkeypatch):
        httpd, state, fs = running_server_multi
        mock_startfile = MagicMock()
        monkeypatch.setattr(os, "startfile", mock_startfile, raising=False)
        series_path = f"{state.archive_root}/g1/a.jpg"
        events_path = f"{state.archive_root}/g1/b.jpg"
        state.series_registry["s1:0"] = {
            "path": series_path, "group_id": "s1", "size": 100, "hash": "", "reason": "dup-visual",
        }
        state.events_registry["e1:0"] = {"path": events_path, "group_id": "e1", "size": 100}

        status, body, _ = _post_json(
            state.port, "/api/open",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": "s1:0"},
        )
        assert status == 200
        assert json.loads(body) == {"opened": "s1:0"}
        mock_startfile.assert_called_with(series_path)

        status, body, _ = _post_json(
            state.port, "/api/open",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": "e1:0"},
        )
        assert status == 200
        assert json.loads(body) == {"opened": "e1:0"}
        mock_startfile.assert_called_with(events_path)

    def test_placement_registry_id_accepted_by_open(self, running_server_multi, monkeypatch):
        # Regression: /api/open resolved file_registry/series_registry/
        # events_registry only, so a dblclick on a "Sugestie grupowania"
        # card -- makeCard() binds the same handler on every screen -- sent
        # a placement id ("p<N>:<i>") that resolved nowhere and 400'd.
        # Simulates a finished placement computation having populated
        # placement_registry (build_placement_views() does exactly this).
        httpd, state, fs = running_server_multi
        mock_startfile = MagicMock()
        monkeypatch.setattr(os, "startfile", mock_startfile, raising=False)
        placement_path = f"{state.archive_root}/g1/a.jpg"
        state.placement_registry["p1:0"] = {
            "path": placement_path, "group_id": "p1", "size": 100,
        }

        status, body, _ = _post_json(
            state.port, "/api/open",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": "p1:0"},
        )
        assert status == 200
        assert json.loads(body) == {"opened": "p1:0"}
        mock_startfile.assert_called_with(placement_path)

    def test_compress_registry_id_accepted_by_open(self, running_server_multi, monkeypatch):
        # Same fix, fifth registry: a dblclick on a "Sugestia kompresji"
        # card (makeCard() binds the same handler on every screen) sent a
        # compress id ("c1_<N>:<i>") that resolved nowhere and 400'd, same
        # as the placement regression above.
        httpd, state, fs = running_server_multi
        mock_startfile = MagicMock()
        monkeypatch.setattr(os, "startfile", mock_startfile, raising=False)
        compress_path = f"{state.archive_root}/g1/a.mp4"
        state.compress_registry["c1_1:0"] = {
            "path": compress_path, "group_id": "c1_1", "size": 100,
        }

        status, body, _ = _post_json(
            state.port, "/api/open",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": "c1_1:0"},
        )
        assert status == 200
        assert json.loads(body) == {"opened": "c1_1:0"}
        mock_startfile.assert_called_with(compress_path)

    def test_unknown_placement_id_still_rejected_by_open(self, running_server_multi, monkeypatch):
        # The fix widens the allowlist by one registry, it does not turn
        # /api/open into "open any id shaped like a placement id": an id
        # absent from every registry is still an explicit 400 and
        # os.startfile() is never reached.
        httpd, state, fs = running_server_multi
        mock_startfile = MagicMock()
        monkeypatch.setattr(os, "startfile", mock_startfile, raising=False)

        status, body, _ = _post_json(
            state.port, "/api/open",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": "p99:7"},
        )
        assert status == 400
        assert "unknown id" in json.loads(body)["error"]
        mock_startfile.assert_not_called()

    def test_placement_registry_id_rejected_by_apply_series(self, running_server_multi):
        # The registry separation the fix must not weaken: /api/open may now
        # resolve a placement id, but the series apply endpoint still must
        # not -- placement ids belong to /api/apply-placement alone.
        httpd, state, fs = running_server_multi
        extra_path = f"{state.archive_root}/g1/a.jpg"
        state.placement_registry["p1:0"] = {
            "path": extra_path, "group_id": "p1", "size": 100,
        }

        status, body, _ = _post_json(
            state.port, "/api/apply-series",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": [{"group_id": "p1", "keep_ids": ["p1:0"], "discard_ids": []}]},
        )
        assert status == 400
        assert "unknown id" in json.loads(body)["error"]
        assert fs.move_calls == []
        assert fs.exists(extra_path) is True


class TestProgressAndWorkersHTTP:
    """Progress bar + --workers: wiring
    from ReviewState.max_workers/series_progress/events_progress through
    _run_series_computation()/_run_events_computation() into
    series.find_series()/events.find_events(), and back out again via
    GET /api/series's/GET /api/events's "progress" field."""

    def test_default_max_workers_matches_progress_default(self, running_server):
        httpd, state = running_server
        assert state.max_workers == DEFAULT_WORKERS

    def test_start_server_forwards_max_workers_into_state(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_dir("D:/Archive")
        httpd, thread, state = review_server.start_server(
            fs=fs, archive_root="D:/Archive", duplicate_groups=[], decisions=[],
            thumbs_dir=str(tmp_path / "thumbs"), port=0, max_workers=7,
        )
        try:
            assert state.max_workers == 7
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_find_series_called_with_state_max_workers_and_progress(
        self, running_server_with_candidates, monkeypatch,
    ):
        httpd, state = running_server_with_candidates
        state.max_workers = 9
        captured = {}
        called = threading.Event()

        def fake_find_series(fs, candidates, cache, **kwargs):
            captured.update(kwargs)
            called.set()
            return series.SeriesResult()

        monkeypatch.setattr(series, "find_series", fake_find_series)

        status, body, _ = _get(state.port, "/api/series")
        assert status == 200
        # do_GET() only STARTS the background computation and returns
        # immediately (D6) -- the fake may not have run yet by the time the
        # HTTP response comes back, so wait for it explicitly rather than
        # racing the worker thread.
        assert called.wait(timeout=5), "find_series() was never actually invoked"

        assert captured["max_workers"] == 9
        assert captured["progress"] is state.series_progress

    def test_find_events_called_with_state_max_workers_and_progress(
        self, running_server_with_candidates, monkeypatch,
    ):
        httpd, state = running_server_with_candidates
        state.max_workers = 3
        captured = {}
        called = threading.Event()

        def fake_find_events(fs, candidates, archive_root, **kwargs):
            captured.update(kwargs)
            called.set()
            return events.EventsResult()

        monkeypatch.setattr(events, "find_events", fake_find_events)

        status, body, _ = _get(state.port, "/api/events")
        assert status == 200
        assert called.wait(timeout=5), "find_events() was never actually invoked"

        assert captured["max_workers"] == 3
        assert captured["progress"] is state.events_progress

    def test_series_response_reports_progress_while_computing(
        self, running_server_with_candidates, monkeypatch,
    ):
        started = threading.Event()
        release = threading.Event()

        def fake_find_series(fs, candidates, cache, *, progress=None, **kwargs):
            if progress is not None:
                progress.set_total(10)
                progress.increment(4)
            started.set()
            release.wait(timeout=5)
            return series.SeriesResult()

        monkeypatch.setattr(series, "find_series", fake_find_series)
        httpd, state = running_server_with_candidates

        results = []

        def do_request():
            results.append(_get(state.port, "/api/series"))

        t = threading.Thread(target=do_request)
        t.start()
        assert started.wait(timeout=5)

        # A SECOND request while the fake engine is still blocked on
        # release.wait() must see the partial progress the first call's
        # worker thread already published -- proving progress is readable
        # from a completely different thread mid-computation, not just
        # after the computation finishes.
        status, body, _ = _get(state.port, "/api/series")
        assert status == 200
        payload = json.loads(body)
        assert payload["status"] == "computing"
        assert payload["progress"] == {"done": 4, "total": 10}

        release.set()
        t.join(timeout=5)

    def test_no_candidates_reports_zero_zero_progress(self, running_server):
        httpd, state = running_server
        status, body, _ = _get(state.port, "/api/series")
        assert status == 200
        payload = json.loads(body)
        assert payload["progress"] == {"done": 0, "total": 0}

        status, body, _ = _get(state.port, "/api/events")
        assert status == 200
        payload = json.loads(body)
        assert payload["progress"] == {"done": 0, "total": 0}


class TestResponseHardeningHeaders:
    def test_html_json_and_csp_carry_frame_and_sniff_headers(self, running_server):
        httpd, state = running_server
        for path in ("/", "/api/state"):
            status, _, headers = _get(state.port, path)
            assert status == 200
            assert headers["X-Frame-Options"] == "DENY"
            assert headers["X-Content-Type-Options"] == "nosniff"
        _, _, headers = _get(state.port, "/")
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

    def test_thumbnail_bytes_carry_headers(self, running_server):
        httpd, state = running_server
        status, _, headers = _get(state.port, "/api/thumb/hash1")
        if status == 200:
            assert headers["X-Frame-Options"] == "DENY"
            assert headers["X-Content-Type-Options"] == "nosniff"


class TestApiOpenExtensionAllowlist:
    def test_disallowed_extension_is_refused_without_startfile(self, running_server, monkeypatch):
        httpd, state = running_server
        mock_startfile = MagicMock()
        monkeypatch.setattr(os, "startfile", mock_startfile, raising=False)
        state.file_registry["9:9"] = {"path": f"{state.archive_root}/evil.EXE"}

        status, body, _ = _post_json(
            state.port, "/api/open",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": "9:9"},
        )

        assert status == 400
        assert "not allowed" in json.loads(body)["error"]
        mock_startfile.assert_not_called()

    def test_uppercase_allowed_extension_is_opened(self, running_server, monkeypatch):
        httpd, state = running_server
        mock_startfile = MagicMock()
        monkeypatch.setattr(os, "startfile", mock_startfile, raising=False)
        state.file_registry["9:8"] = {"path": f"{state.archive_root}/IMG.JPG"}

        status, _, _ = _post_json(
            state.port, "/api/open",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": "9:8"},
        )

        assert status == 200
        mock_startfile.assert_called_once()
