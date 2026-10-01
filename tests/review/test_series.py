"""Series tab over HTTP: apply, lazy computation, skips and threshold tuning."""
from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock
from tidy.analysis import events
from tidy.moves import manifest
from tidy.review import computations as review_computations
from tidy.review import server as review_server
from tidy.analysis import series
from tidy.store.cache import Cache
from fakes import FakeFileSystem
from tidy.duplicates.scanner import ScannedFile

from .helpers import (
    _get,
    _jpeg_bytes,
    _post,
    _post_json,
    _series_groups_json,
)


class TestApplySeriesHTTP:
    """POST /api/apply-series, tested the same way
    the original POST /api/apply was -- over a REAL
    socket (http.client), never mocked. series_registry is populated
    directly (mirroring TestOpenRegistrySeparationHTTP above) rather than
    by running the full series engine, since these tests are about the
    endpoint's own validation/application mechanics, not series.find_series()
    itself (already covered by tests/test_series.py)."""

    def _seed_series_group(self, state, fs, root, group_id="s1"):
        keep_path = f"{root}/g3/keep.jpg"
        discard_a = f"{root}/g3/edit.jpg"
        discard_b = f"{root}/g3/blur.jpg"
        fs.add_dir(f"{root}/g3")
        fs.add_file(keep_path, b"k" * 500, size=500)
        fs.add_file(discard_a, b"a" * 300, size=300)
        fs.add_file(discard_b, b"b" * 700, size=700)
        state.series_registry[f"{group_id}:0"] = {
            "path": keep_path, "group_id": group_id, "size": 500, "hash": "", "reason": "dup-visual",
        }
        state.series_registry[f"{group_id}:1"] = {
            "path": discard_a, "group_id": group_id, "size": 300, "hash": "", "reason": "dup-visual",
        }
        state.series_registry[f"{group_id}:2"] = {
            "path": discard_b, "group_id": group_id, "size": 700, "hash": "", "reason": "dup-visual",
        }
        return keep_path, discard_a, discard_b

    def test_unknown_id_is_rejected_with_400(self, running_server_multi):
        httpd, state, fs = running_server_multi
        status, body, _ = _post_json(
            state.port, "/api/apply-series",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": [{"group_id": "s1", "keep_ids": ["s1:0"], "discard_ids": ["s1:99"]}]},
        )
        assert status == 400
        assert "unknown id" in json.loads(body)["error"]

    def test_empty_keep_ids_is_rejected_with_400(self, running_server_multi):
        # A series group must keep at least one file -- discarding
        # every member would delete content that exists nowhere else,
        # unlike an exact-duplicate group.
        httpd, state, fs = running_server_multi
        keep_path, discard_a, discard_b = self._seed_series_group(state, fs, state.archive_root)

        status, body, _ = _post_json(
            state.port, "/api/apply-series",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": [{"group_id": "s1", "keep_ids": [], "discard_ids": ["s1:1", "s1:2"]}]},
        )

        assert status == 400
        assert "keep_ids" in json.loads(body)["error"]
        assert fs.exists(discard_a) is True
        assert fs.exists(discard_b) is True

    def test_forged_host_is_rejected_with_403(self, running_server_multi):
        httpd, state, fs = running_server_multi
        self._seed_series_group(state, fs, state.archive_root)

        status, body, _ = _post_json(
            state.port, "/api/apply-series",
            headers={"X-Dedup-Token": "test-token", "Host": "evil.example:1"},
            payload={"selections": [{"group_id": "s1", "keep_ids": ["s1:0"], "discard_ids": ["s1:1"]}]},
        )

        assert status == 403

    def test_happy_path_keeps_multiple_files_and_moves_only_discards(self, running_server_multi):
        # A series can keep more than one file -- both keep_path and
        # discard_a are kept here, only discard_b is actually removed.
        httpd, state, fs = running_server_multi
        keep_path, discard_a, discard_b = self._seed_series_group(state, fs, state.archive_root)

        status, body, _ = _post_json(
            state.port, "/api/apply-series",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": [{"group_id": "s1", "keep_ids": ["s1:0", "s1:1"], "discard_ids": ["s1:2"]}]},
        )

        assert status == 200
        result = json.loads(body)
        assert result["moved"] == 1
        assert result["failed"] == 0
        assert fs.exists(keep_path) is True
        assert fs.exists(discard_a) is True
        assert fs.exists(discard_b) is False
        assert state.applied_series_group_ids == {"s1"}

    def test_repeated_apply_of_same_group_is_rejected_with_409(self, running_server_multi):
        httpd, state, fs = running_server_multi
        self._seed_series_group(state, fs, state.archive_root)
        payload = {"selections": [{"group_id": "s1", "keep_ids": ["s1:0"], "discard_ids": ["s1:1"]}]}

        first = _post_json(state.port, "/api/apply-series", headers={"X-Dedup-Token": "test-token"}, payload=payload)
        assert first[0] == 200

        second = _post_json(state.port, "/api/apply-series", headers={"X-Dedup-Token": "test-token"}, payload=payload)
        assert second[0] == 409
        assert "already applied" in json.loads(second[1])["error"]

    def test_manifest_records_each_discards_own_size_not_a_shared_group_size(self, running_server_multi):
        # D10's HTTP-level proof: discard_a (300 bytes) and discard_b (700
        # bytes) must each land in the manifest with THEIR OWN size, not a
        # single shared group value -- the pure-function version of this
        # proof is tests/test_applier.py's TestApplyBatchSeriesPerFileSizeHash.
        httpd, state, fs = running_server_multi
        keep_path, discard_a, discard_b = self._seed_series_group(state, fs, state.archive_root)

        status, body, _ = _post_json(
            state.port, "/api/apply-series",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": [{"group_id": "s1", "keep_ids": ["s1:0"], "discard_ids": ["s1:1", "s1:2"]}]},
        )

        assert status == 200
        result = json.loads(body)
        final_rows = manifest.read_final_rows(result["manifest_path"])
        assert final_rows[discard_a].size == 300
        assert final_rows[discard_b].size == 700

    def test_id_absent_from_both_registries_rejected_by_open(self, running_server_multi, monkeypatch):
        httpd, state, fs = running_server_multi
        mock_startfile = MagicMock()
        monkeypatch.setattr(os, "startfile", mock_startfile, raising=False)

        status, body, _ = _post_json(
            state.port, "/api/open",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": "nowhere:0"},
        )
        assert status == 400
        assert "unknown id" in json.loads(body)["error"]
        mock_startfile.assert_not_called()


class TestLazySeriesEventsHTTP:
    def test_host_header_rejected_on_series_and_events(self, running_server):
        httpd, state = running_server
        for path in ("/api/series", "/api/events"):
            conn = http.client.HTTPConnection("127.0.0.1", state.port, timeout=5)
            try:
                conn.putrequest("GET", path, skip_host=True)
                conn.putheader("Host", "evil.example:1234")
                conn.endheaders()
                resp = conn.getresponse()
                assert resp.status == 403, f"{path} did not reject a spoofed Host header"
                resp.read()
            finally:
                conn.close()

    def test_localhost_alias_not_rejected_on_series(self, running_server):
        httpd, state = running_server
        conn = http.client.HTTPConnection("127.0.0.1", state.port, timeout=5)
        try:
            conn.putrequest("GET", "/api/series", skip_host=True)
            conn.putheader("Host", f"localhost:{state.port}")
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status != 403
            resp.read()
        finally:
            conn.close()

    def test_no_db_path_or_candidates_is_ready_with_empty_groups(self, running_server):
        # `running_server` leaves db_path/candidates at their defaults
        # (None / empty) -- per this step's contract that must resolve
        # straight to "ready" with an empty list, never an exception and
        # never stuck on "computing".
        httpd, state = running_server
        status, body, _ = _get(state.port, "/api/series")
        assert status == 200
        payload = json.loads(body)
        assert payload["status"] == "ready"
        assert payload["groups"] == []
        assert payload["error"] is None

        status, body, _ = _get(state.port, "/api/events")
        assert status == 200
        payload = json.loads(body)
        assert payload["status"] == "ready"
        assert payload["groups"] == []
        assert payload["error"] is None

    def test_concurrent_requests_start_exactly_one_series_computation(
        self, running_server_with_candidates, monkeypatch,
    ):
        httpd, state = running_server_with_candidates
        call_count = {"n": 0}
        started = threading.Event()
        release = threading.Event()

        def fake_find_series(fs, candidates, cache, **kwargs):
            call_count["n"] += 1
            started.set()
            release.wait(timeout=5)
            return series.SeriesResult()

        monkeypatch.setattr(series, "find_series", fake_find_series)

        results = []

        def do_request():
            results.append(_get(state.port, "/api/series"))

        t1 = threading.Thread(target=do_request)
        t1.start()
        assert started.wait(timeout=5), "first request never reached the fake engine"
        t2 = threading.Thread(target=do_request)
        t2.start()
        t2.join(timeout=5)
        release.set()
        t1.join(timeout=5)

        assert call_count["n"] == 1
        assert len(results) == 2
        for status, body, _ in results:
            assert status == 200
            assert json.loads(body)["status"] in ("computing", "ready")

    def test_computing_transitions_to_ready(self, running_server_with_candidates, monkeypatch):
        monkeypatch.setattr(
            series, "find_series",
            lambda fs, candidates, cache, **kwargs: series.SeriesResult(),
        )
        httpd, state = running_server_with_candidates

        status, body, _ = _get(state.port, "/api/series")
        assert status == 200
        assert json.loads(body)["status"] in ("computing", "ready")

        deadline = time.time() + 5
        payload = {"status": "computing"}
        while time.time() < deadline and payload["status"] != "ready":
            status, body, _ = _get(state.port, "/api/series")
            payload = json.loads(body)
            if payload["status"] != "ready":
                time.sleep(0.05)
        assert payload["status"] == "ready"
        assert payload["groups"] == []
        assert payload["error"] is None

    def test_ready_result_is_never_recomputed(self, running_server_with_candidates, monkeypatch):
        call_count = {"n": 0}

        def fake_find_series(fs, candidates, cache, **kwargs):
            call_count["n"] += 1
            return series.SeriesResult()

        monkeypatch.setattr(series, "find_series", fake_find_series)
        httpd, state = running_server_with_candidates

        deadline = time.time() + 5
        status_value = "computing"
        while time.time() < deadline and status_value != "ready":
            _, body, _ = _get(state.port, "/api/series")
            status_value = json.loads(body)["status"]
            if status_value != "ready":
                time.sleep(0.05)
        assert status_value == "ready"
        assert call_count["n"] == 1

        # A further request after "ready" must NOT trigger a second call.
        _, body, _ = _get(state.port, "/api/series")
        assert json.loads(body)["status"] == "ready"
        assert call_count["n"] == 1

    def test_engine_exception_becomes_error_status_not_500_or_hang(
        self, running_server_with_candidates, monkeypatch,
    ):
        def failing_find_series(fs, candidates, cache, **kwargs):
            raise RuntimeError("synthetic engine failure")

        monkeypatch.setattr(series, "find_series", failing_find_series)
        httpd, state = running_server_with_candidates

        deadline = time.time() + 5
        payload = {"status": "computing"}
        while time.time() < deadline and payload["status"] not in ("ready", "error"):
            status, body, _ = _get(state.port, "/api/series")
            assert status == 200  # never 500, whatever the engine does
            payload = json.loads(body)
            if payload["status"] not in ("ready", "error"):
                time.sleep(0.05)
        assert payload["status"] == "error"
        assert "synthetic engine failure" in payload["error"]

    def test_events_engine_exception_becomes_error_status(
        self, running_server_with_candidates, monkeypatch,
    ):
        def failing_find_events(fs, candidates, archive_root, **kwargs):
            raise RuntimeError("synthetic events failure")

        monkeypatch.setattr(events, "find_events", failing_find_events)
        httpd, state = running_server_with_candidates

        deadline = time.time() + 5
        payload = {"status": "computing"}
        while time.time() < deadline and payload["status"] not in ("ready", "error"):
            status, body, _ = _get(state.port, "/api/events")
            assert status == 200
            payload = json.loads(body)
            if payload["status"] not in ("ready", "error"):
                time.sleep(0.05)
        assert payload["status"] == "error"
        assert "synthetic events failure" in payload["error"]


class TestSeriesSkipPersistence:
    def test_get_series_reports_every_group_unskipped_on_a_fresh_database(self, series_skip_server):
        start, _fs, _db = series_skip_server
        state = start()
        groups = _series_groups_json(state.port)
        assert [g["group_id"] for g in groups] == ["s1", "s2"]
        assert all(g["skipped"] is False for g in groups)

    def test_skipping_a_group_marks_it_in_the_same_session(self, series_skip_server):
        start, _fs, _db = series_skip_server
        state = start()

        status, body, _ = _post_json(
            state.port, "/api/series-skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": "s1", "skip": True},
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["skipped"] is True
        assert payload["persisted"] is True
        assert payload["skipped_group_count"] == 1

        groups = {g["group_id"]: g for g in _series_groups_json(state.port)}
        assert groups["s1"]["skipped"] is True
        assert groups["s2"]["skipped"] is False

    def test_the_skip_is_still_in_effect_in_a_brand_new_server(self, series_skip_server):
        # THE test for this feature. Two independent servers over one
        # database: the second one never sees the POST, only the row it left
        # behind. This is what "nawet przy nastepnym uruchomieniu" means.
        start, _fs, _db = series_skip_server
        first = start()
        _post_json(
            first.port, "/api/series-skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": "s1", "skip": True},
        )

        second = start()
        groups = {g["group_id"]: g for g in _series_groups_json(second.port)}
        assert groups["s1"]["skipped"] is True
        assert groups["s2"]["skipped"] is False

    def test_the_decision_is_written_to_the_cache_database_itself(self, series_skip_server):
        start, _fs, db_path = series_skip_server
        state = start()
        _post_json(
            state.port, "/api/series-skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": "s1", "skip": True},
        )

        cache = Cache(db_path)
        try:
            stored = cache.list_series_skips()
        finally:
            cache.close()
        assert len(stored) == 1
        _key, paths = stored[0]
        # Stored as the group's normalized MEMBER PATHS, never as "s1" -- a
        # group_id is a position in one run's result list.
        assert paths == series.normalize_member_paths(
            [f"{state.archive_root}/a.jpg", f"{state.archive_root}/b.jpg"]
        )

    def test_unskipping_clears_it_everywhere(self, series_skip_server):
        start, _fs, db_path = series_skip_server
        state = start()
        _post_json(
            state.port, "/api/series-skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": "s1", "skip": True},
        )

        status, body, _ = _post_json(
            state.port, "/api/series-skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": "s1", "skip": False},
        )
        assert status == 200
        assert json.loads(body)["skipped"] is False
        assert json.loads(body)["skipped_group_count"] == 0

        assert all(g["skipped"] is False for g in _series_groups_json(state.port))
        cache = Cache(db_path)
        try:
            assert cache.list_series_skips() == []
        finally:
            cache.close()

        # ...and a new process agrees, which is the half that a purely
        # in-memory un-skip would get wrong.
        second = start()
        assert all(g["skipped"] is False for g in _series_groups_json(second.port))

    def test_skipping_moves_no_files_and_applies_no_group(self, series_skip_server):
        # This endpoint is behind the mutating gate because it writes to the
        # user's database, NOT because it touches the archive -- nothing on
        # this path can reach tidy/moves/applier.
        start, fs, _db = series_skip_server
        state = start()
        _post_json(
            state.port, "/api/series-skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": "s1", "skip": True},
        )
        assert fs.move_calls == []
        assert state.applied_series_group_ids == set()
        assert state.applied_group_ids == set()

    def test_unknown_group_id_is_rejected(self, series_skip_server):
        start, _fs, db_path = series_skip_server
        state = start()
        status, body, _ = _post_json(
            state.port, "/api/series-skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": "s99", "skip": True},
        )
        assert status == 400
        assert "unknown series group_id" in json.loads(body)["error"]
        cache = Cache(db_path)
        try:
            assert cache.list_series_skips() == []
        finally:
            cache.close()

    def test_a_duplicate_group_id_cannot_be_skipped_through_this_endpoint(self, series_skip_server):
        # Registry separation (module docstring): the group_id is resolved
        # through series_registry and nothing else, so an id from another
        # screen is simply unknown here.
        start, _fs, _db = series_skip_server
        state = start()
        state.file_registry["1:0"] = {
            "path": f"{state.archive_root}/a.jpg", "group_id": "1",
            "size": 100, "hash": "h", "reason": "dup-exact",
        }
        status, _body, _ = _post_json(
            state.port, "/api/series-skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": "1", "skip": True},
        )
        assert status == 400

    def test_non_boolean_skip_is_rejected(self, series_skip_server):
        start, _fs, _db = series_skip_server
        state = start()
        status, body, _ = _post_json(
            state.port, "/api/series-skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": "s1", "skip": "yes"},
        )
        assert status == 400
        assert "skip must be" in json.loads(body)["error"]

    def test_missing_group_id_is_rejected(self, series_skip_server):
        start, _fs, _db = series_skip_server
        state = start()
        status, body, _ = _post_json(
            state.port, "/api/series-skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"skip": True},
        )
        assert status == 400
        assert "group_id is required" in json.loads(body)["error"]

    def test_requires_the_token_like_every_other_mutating_post(self, series_skip_server):
        start, _fs, db_path = series_skip_server
        state = start()
        status, _body, _ = _post_json(
            state.port, "/api/series-skip",
            payload={"group_id": "s1", "skip": True},
        )
        assert status == 403
        cache = Cache(db_path)
        try:
            assert cache.list_series_skips() == []
        finally:
            cache.close()

    def test_a_session_without_a_database_skips_but_says_it_cannot_remember(
        self, running_server_multi,
    ):
        # db_path is None here (running_server_multi never passes one). The
        # skip still takes effect for this session -- reporting persisted:
        # false is how the UI knows to warn instead of silently promising
        # something it cannot deliver.
        _httpd, state, _fs = running_server_multi
        state.series_registry["s1:0"] = {
            "path": f"{state.archive_root}/g1/a.jpg", "group_id": "s1",
            "size": 100, "hash": "", "reason": "dup-visual",
        }
        status, body, _ = _post_json(
            state.port, "/api/series-skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": "s1", "skip": True},
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["skipped"] is True
        assert payload["persisted"] is False
        assert len(state.series_skips) == 1


class TestSeriesSkipFrontend:
    def _html(self, port):
        status, body, _ = _get(port, "/")
        assert status == 200
        return body.decode("utf-8")

    def _function_body(self, html, name):
        match = re.search(r'function\s+' + name + r'\([^)]*\)\s*\{', html)
        assert match, f"expected a {name}() function"
        start = match.end()
        depth = 1
        i = start
        while i < len(html) and depth > 0:
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
            i += 1
        return html[start:i - 1]

    def test_skipped_groups_are_hidden_unless_the_toggle_is_on(self, series_skip_server):
        start, _fs, _db = series_skip_server
        state = start()
        body = self._function_body(self._html(state.port), "visibleSeriesGroups")
        assert "state.showSkippedSeries" in body
        assert "g.skipped" in body

    def test_the_show_skipped_toggle_exists_and_starts_hidden_and_unchecked(self, series_skip_server):
        start, _fs, _db = series_skip_server
        state = start()
        html = self._html(state.port)
        assert 'id="show-skipped-series"' in html
        assert re.search(r'id="series-controls"[^>]*style="display:none"', html)
        assert re.search(r'showSkippedSeries:\s*false', html)

    def test_the_skip_toggle_lives_in_the_group_bar(self, series_skip_server):
        # Originally "checkbox pomin w prawym dolnym rogu grupy i 2 razy
        # wiekszym" put this in a bottom-right corner at double size via
        # transform: scale(2). A later, explicit request moved it into the
        # group bar instead -- still a prominent hit target (real box sizing
        # on the checkbox, a bordered pill on the label), just no longer
        # isolated in its own corner element.
        start, _fs, _db = series_skip_server
        state = start()
        html = self._html(state.port)
        assert ".skip-corner" not in html
        toggle = re.search(r'\.skip-toggle\s*\{([^}]*)\}', html)
        assert toggle, "expected a .skip-toggle rule"
        assert "min-height: 2rem" in toggle.group(1)
        box = re.search(r'\.skip-toggle input\[type="checkbox"\]\s*\{([^}]*)\}', html)
        assert box, "expected a .skip-toggle checkbox rule"
        assert "width: 1.15rem" in box.group(1) and "height: 1.15rem" in box.group(1)
        body = self._function_body(html, "renderSeriesScreen")
        assert re.search(r'groupBar\.appendChild\(makeSeriesSkipControl\(g,\s*sel\)\)', body), (
            "the skip toggle must render inside .group-bar"
        )

    def test_the_checkbox_posts_to_the_server_instead_of_only_setting_a_flag(
        self, series_skip_server,
    ):
        # The regression this pins: "pomin" used to be `sel.skip = checked`
        # and nothing else, which is exactly why it was forgotten on the next
        # run. The handler must go through the server.
        start, _fs, _db = series_skip_server
        state = start()
        html = self._html(state.port)
        control = self._function_body(html, "makeSeriesSkipControl")
        assert "toggleSeriesSkip(" in control
        toggle = self._function_body(html, "toggleSeriesSkip")
        assert '"/api/series-skip"' in toggle
        assert "X-Dedup-Token" in toggle
        # The local flag is set only after the response was OK -- a failed
        # write must not leave the group hidden on screen.
        assert toggle.index("resp.ok") < toggle.index("group.skipped = wantSkip")

    def test_selection_state_takes_its_skip_flag_from_the_server(self, series_skip_server):
        start, _fs, _db = series_skip_server
        state = start()
        body = self._function_body(self._html(state.port), "ensureSeriesSelection")
        assert "skip = !!g.skipped" in body


class TestSeriesTuningViaRescan:
    """The dev-mode series thresholds reach the engine ONLY through
    POST /api/rescan.

    That constraint is what makes the feature safe: a rescan already
    re-walks the disk, replaces every _LazyComputation and clears the
    applied-id sets, so files a previous apply moved into !REMOVE are simply
    not in the fresh candidate list -- no separate "never re-offer this"
    bookkeeping is needed."""

    def _server(self, tmp_path, rescan=None, **kwargs):
        fs = FakeFileSystem()
        fs.add_dir("D:/Archive")
        fs.add_dir("D:/Archive/!NEW")
        base = int(datetime(2024, 6, 1, 10, 0).timestamp() * 1_000_000_000)
        path = "D:/Archive/!NEW/a.jpg"
        fs.add_file(path, _jpeg_bytes(), volume_id=1, file_index=100, mtime_ns=base)
        candidate = ScannedFile(path=path, stat=fs.stat(path), is_duplicate_candidate=True)

        calls = []

        def default_rescan():
            calls.append(1)
            return [], [], [candidate]

        httpd, thread, state = review_server.start_server(
            fs=fs, archive_root="D:/Archive", duplicate_groups=[], decisions=[],
            thumbs_dir=str(tmp_path / "thumbs"), host="127.0.0.1", port=0,
            token="test-token", candidates=[candidate],
            db_path=str(tmp_path / "cache.sqlite3"),
            rescan=rescan or default_rescan, **kwargs,
        )
        return httpd, thread, state, calls

    def _rescan(self, state, payload):
        return _post_json(
            state.port, "/api/rescan",
            headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
            payload=payload,
        )

    def test_a_rescan_applies_the_new_thresholds_and_echoes_them_back(self, tmp_path):
        httpd, thread, state, _calls = self._server(tmp_path)
        try:
            status, body, _ = self._rescan(state, {
                "series_hamming_threshold": 10, "series_window_seconds": 60,
            })
            assert status == 200
            data = json.loads(body.decode("utf-8"))
            assert state.series_hamming_threshold == 10
            assert state.series_window_seconds == 60.0
            # The echo is what the client clears its pending marker from, so
            # it must report what took effect, not what was asked for.
            assert data["series_hamming_threshold"] == 10
            assert data["series_window_seconds"] == 60.0
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_a_rescan_carrying_no_tuning_keeps_the_current_values(self, tmp_path):
        # The pre-tuning client posts a bare "{}", and so may any other
        # caller of this endpoint.
        httpd, thread, state, _calls = self._server(tmp_path)
        try:
            state.series_hamming_threshold = 8
            state.series_window_seconds = 45.0
            status, body, _ = self._rescan(state, {})
            assert status == 200
            assert state.series_hamming_threshold == 8
            assert state.series_window_seconds == 45.0
            data = json.loads(body.decode("utf-8"))
            assert data["series_hamming_threshold"] == 8
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_a_refused_threshold_scans_nothing_and_changes_nothing(self, tmp_path):
        # Validation runs BEFORE the walk: minutes of rescanning followed by
        # "that value was invalid" would be the worst of both.
        httpd, thread, state, calls = self._server(tmp_path)
        try:
            before = (state.series_hamming_threshold, state.series_window_seconds)
            status, body, _ = self._rescan(state, {"series_hamming_threshold": 999})
            assert status == 400
            assert "series_hamming_threshold" in json.loads(body.decode("utf-8"))["error"]
            assert calls == [], "the archive must not be re-walked for a refused value"
            assert (state.series_hamming_threshold, state.series_window_seconds) == before
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_a_refused_window_scans_nothing(self, tmp_path):
        httpd, thread, state, calls = self._server(tmp_path)
        try:
            status, _body, _ = self._rescan(state, {"series_window_seconds": 86400})
            assert status == 400
            assert calls == []
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_a_non_object_body_is_refused(self, tmp_path):
        httpd, thread, state, calls = self._server(tmp_path)
        try:
            status, _body, _ = _post(
                state.port, "/api/rescan",
                headers={"X-Dedup-Token": "test-token",
                         "Host": f"127.0.0.1:{state.port}",
                         "Content-Type": "application/json"},
                body=b"[1, 2]",
            )
            assert status == 400
            assert calls == []
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_a_malformed_body_is_refused(self, tmp_path):
        httpd, thread, state, calls = self._server(tmp_path)
        try:
            status, _body, _ = _post(
                state.port, "/api/rescan",
                headers={"X-Dedup-Token": "test-token",
                         "Host": f"127.0.0.1:{state.port}",
                         "Content-Type": "application/json"},
                body=b"{not json",
            )
            assert status == 400
            assert calls == []
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_the_next_series_computation_runs_with_the_new_threshold(
        self, tmp_path, monkeypatch,
    ):
        # The point of the whole feature: the value has to arrive at
        # find_series(), not merely at ReviewState.
        httpd, thread, state, _calls = self._server(tmp_path)
        captured = {}
        called = threading.Event()

        def fake_find_series(fs, candidates, cache, **kwargs):
            captured.update(kwargs)
            called.set()
            return series.SeriesResult()

        monkeypatch.setattr(series, "find_series", fake_find_series)
        try:
            status, _body, _ = self._rescan(state, {
                "series_hamming_threshold": 12, "series_window_seconds": 30,
            })
            assert status == 200

            status, _body, _ = _get(state.port, "/api/series")
            assert status == 200
            assert called.wait(timeout=5), "find_series() was never invoked"
            assert captured["hamming_threshold"] == 12
            assert captured["window_seconds"] == 30.0
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_api_state_reports_the_values_in_force_and_the_engine_defaults(
        self, tmp_path,
    ):
        httpd, thread, state, _calls = self._server(tmp_path)
        try:
            status, body, _ = _get(state.port, "/api/state")
            assert status == 200
            data = json.loads(body.decode("utf-8"))
            assert data["series_hamming_threshold"] == series.DEFAULT_HAMMING_THRESHOLD
            assert data["series_window_seconds"] == series.DEFAULT_SERIES_WINDOW_SECONDS
            # Defaults travel separately so the client can mark "(domyslnie)"
            # without duplicating series.DEFAULT_* in JS.
            assert data["series_hamming_threshold_default"] == series.DEFAULT_HAMMING_THRESHOLD
            assert data["series_window_seconds_default"] == series.DEFAULT_SERIES_WINDOW_SECONDS
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_a_cli_supplied_threshold_is_what_the_state_reports(self, tmp_path):
        # A session started with --series-hamming-threshold 3 must show 3:
        # the select initialises from this, and a control that cannot
        # display the value in force would misdescribe the groups on screen.
        httpd, thread, state, _calls = self._server(
            tmp_path, series_hamming_threshold=3, series_window_seconds=45,
        )
        try:
            status, body, _ = _get(state.port, "/api/state")
            data = json.loads(body.decode("utf-8"))
            assert data["series_hamming_threshold"] == 3
            assert data["series_window_seconds"] == 45
            assert data["series_hamming_threshold_default"] == series.DEFAULT_HAMMING_THRESHOLD
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_the_threshold_bound_covers_the_whole_dhash_width(self):
        # 64 because dhash.py packs exactly 64 bits; a wider bound would
        # accept a threshold that can never reject anything.
        assert review_computations.SERIES_HAMMING_MIN == 0
        assert review_computations.SERIES_HAMMING_MAX == 64


class TestSeriesTuningFrontend:
    """The dev-mode-only half: the selects exist, stay hidden, never fetch,
    and reach the engine only through the Rescan button.

    There is no JS runner in this suite, so these guarantees are pinned on
    the source of the served page -- the same approach the rescan and skip
    frontend tests take."""

    def _html(self, running_server):
        _httpd, state = running_server
        status, body, _ = _get(state.port, "/")
        assert status == 200
        return body.decode("utf-8")

    def _handler(self, html, element_id):
        marker = 'document.getElementById("' + element_id + '").addEventListener'
        body = html[html.index(marker):]
        return body[:body.index("});")]

    def _function_body(self, html, name):
        body = html[html.index("function " + name + "()"):]
        return body[:body.index("\nfunction ")]

    def test_the_tuning_block_starts_hidden(self, running_server):
        html = self._html(running_server)
        match = re.search(r'<div[^>]*id="series-tuning"[^>]*>', html)
        assert match, "expected a #series-tuning block"
        assert "display:none" in match.group(0).replace(" ", "")

    def test_both_selects_are_built_from_the_server_value(self, running_server):
        html = self._html(running_server)
        # Empty in the markup on purpose: the options are built from the
        # value in force, which the server sends.
        assert '<select id="series-hamming"></select>' in html
        assert '<select id="series-window"></select>' in html
        assert "series_hamming_threshold_default" in html
        assert "series_window_seconds_default" in html

    def test_the_option_lists_never_offer_a_value_the_server_would_refuse(
        self, running_server,
    ):
        # A select offering 24h would produce a 400 on every Rescan.
        html = self._html(running_server)
        for name, bound in (
            ("SERIES_HAMMING_CHOICES", review_computations.SERIES_HAMMING_MAX),
            ("SERIES_WINDOW_CHOICES", review_computations.SERIES_WINDOW_MAX_SECONDS),
        ):
            block = html[html.index("const " + name):]
            block = block[:block.index("];")]
            values = [int(v) for v in re.findall(r"value:\s*(\d+)", block)]
            assert values, "expected choices in " + name
            assert max(values) <= bound
            assert min(values) >= 1

    def test_the_block_is_gated_on_the_series_tab_only(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "updateSeriesTuningControls")
        assert 'state.tab === "series"' in body
        assert "devMode" not in body

    def test_the_dev_toggle_reveals_the_block_too(self, running_server):
        html = self._html(running_server)
        assert "updateSeriesTuningControls();" in self._handler(html, "dev-mode-toggle")

    def test_a_tab_switch_re_evaluates_the_visibility(self, running_server):
        # updateScreenVisibility() is what every tab switch and every poll
        # tick already goes through, so the rule lives there rather than
        # only in the checkbox handler.
        html = self._html(running_server)
        body = self._function_body(html, "updateScreenVisibility")
        assert "updateSeriesTuningControls();" in body

    def test_changing_a_select_never_fetches(self, running_server):
        html = self._html(running_server)
        for element_id in ("series-hamming", "series-window"):
            handler = self._handler(html, element_id)
            assert "fetch(" not in handler
            assert "state.seriesTuning" in handler

    def test_the_rescan_request_carries_the_chosen_thresholds(self, running_server):
        html = self._html(running_server)
        rescan_handler = html.split('"rescan-btn").addEventListener')[1]
        assert "chosenSeriesTuning()" in rescan_handler
        assert "series_hamming_threshold" in rescan_handler
        assert "series_window_seconds" in rescan_handler
        assert "JSON.stringify(rescanBody)" in rescan_handler

    def test_a_successful_rescan_hands_the_selects_back_to_the_server(
        self, running_server,
    ):
        # The pick is dropped only after the server accepted it, so a
        # refused value keeps both the choice and the pending marker.
        html = self._html(running_server)
        rescan_handler = html.split('"rescan-btn").addEventListener')[1]
        refusal = rescan_handler.index("js_scan_err")
        reset = rescan_handler.index(
            "state.seriesTuning = { hamming: null, windowSeconds: null };",
        )
        assert reset > refusal

    def test_the_pending_marker_is_derived_from_the_server_value(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "seriesTuningPending")
        assert "serverSeriesTuning()" in body
        assert "chosenSeriesTuning()" in body
        assert re.search(r'#series-tuning\.pending\s*\{', html)
