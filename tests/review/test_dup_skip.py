"""Persisted "skip this group" on the Exact duplicates tab: POST /api/dup-skip,
the `skipped` flag on GET /api/state, and the front-end that hides skipped
groups behind "Show skipped groups"."""
from __future__ import annotations

import json
import re

from tidy.analysis import series
from tidy.store.cache import Cache

from .helpers import _get, _post_json

TOKEN = {"X-Dedup-Token": "test-token"}


def _dup_groups_json(port):
    status, body, _ = _get(port, "/api/state")
    assert status == 200
    return {g["group_id"]: g for g in json.loads(body)["groups"]}


def _skip(port, group_id, skip):
    return _post_json(port, "/api/dup-skip", headers=TOKEN,
                      payload={"group_id": group_id, "skip": skip})


class TestDupSkipPersistence:
    def test_every_group_is_unskipped_on_a_fresh_database(self, dup_skip_server):
        start, _fs, _db = dup_skip_server
        state = start()
        groups = _dup_groups_json(state.port)
        assert sorted(groups) == ["1", "2"]
        assert all(g["skipped"] is False for g in groups.values())

    def test_skipping_a_group_marks_it_in_the_same_session(self, dup_skip_server):
        start, _fs, _db = dup_skip_server
        state = start()
        status, body, _ = _skip(state.port, "1", True)
        assert status == 200
        payload = json.loads(body)
        assert payload["skipped"] is True
        assert payload["persisted"] is True
        assert payload["skipped_group_count"] == 1

        groups = _dup_groups_json(state.port)
        assert groups["1"]["skipped"] is True
        assert groups["2"]["skipped"] is False

    def test_the_skip_is_still_in_effect_in_a_brand_new_server(self, dup_skip_server):
        start, _fs, _db = dup_skip_server
        first = start()
        _skip(first.port, "1", True)

        second = start()
        groups = _dup_groups_json(second.port)
        assert groups["1"]["skipped"] is True
        assert groups["2"]["skipped"] is False

    def test_the_decision_is_stored_by_member_paths_in_its_own_table(self, dup_skip_server):
        start, _fs, db_path = dup_skip_server
        state = start()
        _skip(state.port, "1", True)

        cache = Cache(db_path)
        try:
            stored = cache.list_dup_skips()
            assert cache.list_series_skips() == []
        finally:
            cache.close()
        assert len(stored) == 1
        assert stored[0][1] == series.normalize_member_paths(
            [f"{state.archive_root}/a.bin", f"{state.archive_root}/b.bin"]
        )

    def test_unskipping_clears_it_everywhere(self, dup_skip_server):
        start, _fs, db_path = dup_skip_server
        state = start()
        _skip(state.port, "1", True)
        status, body, _ = _skip(state.port, "1", False)
        assert status == 200
        assert json.loads(body)["skipped"] is False
        assert json.loads(body)["skipped_group_count"] == 0

        assert all(g["skipped"] is False for g in _dup_groups_json(state.port).values())
        cache = Cache(db_path)
        try:
            assert cache.list_dup_skips() == []
        finally:
            cache.close()

        second = start()
        assert all(g["skipped"] is False for g in _dup_groups_json(second.port).values())

    def test_skipping_moves_no_files_and_applies_no_group(self, dup_skip_server):
        start, fs, _db = dup_skip_server
        state = start()
        _skip(state.port, "1", True)
        assert fs.move_calls == []
        assert state.applied_group_ids == set()

    def test_unknown_group_id_is_rejected(self, dup_skip_server):
        start, _fs, db_path = dup_skip_server
        state = start()
        status, body, _ = _skip(state.port, "99", True)
        assert status == 400
        assert "unknown duplicate group_id" in json.loads(body)["error"]
        cache = Cache(db_path)
        try:
            assert cache.list_dup_skips() == []
        finally:
            cache.close()

    def test_a_series_group_id_cannot_be_skipped_through_this_endpoint(self, dup_skip_server):
        start, _fs, _db = dup_skip_server
        state = start()
        state.series_registry["s1:0"] = {
            "path": f"{state.archive_root}/a.bin", "group_id": "s1",
        }
        status, _body, _ = _skip(state.port, "s1", True)
        assert status == 400

    def test_non_boolean_skip_is_rejected(self, dup_skip_server):
        start, _fs, _db = dup_skip_server
        state = start()
        status, body, _ = _skip(state.port, "1", "yes")
        assert status == 400
        assert "skip must be" in json.loads(body)["error"]

    def test_missing_group_id_is_rejected(self, dup_skip_server):
        start, _fs, _db = dup_skip_server
        state = start()
        status, body, _ = _post_json(state.port, "/api/dup-skip", headers=TOKEN,
                                     payload={"skip": True})
        assert status == 400
        assert "group_id is required" in json.loads(body)["error"]

    def test_requires_the_token(self, dup_skip_server):
        start, _fs, db_path = dup_skip_server
        state = start()
        status, _body, _ = _post_json(state.port, "/api/dup-skip",
                                      payload={"group_id": "1", "skip": True})
        assert status == 403
        assert state.dup_skips == {}

    def test_a_shrunk_skipped_group_stays_hidden(self, dup_skip_server):
        # Skip stored over a wider set (e.g. a third copy existed then) still
        # covers today's two-member group -- the subset rule.
        start, _fs, db_path = dup_skip_server
        first = start()
        wider = [f"{first.archive_root}/{n}" for n in ("a.bin", "b.bin", "z.bin")]
        cache = Cache(db_path)
        try:
            cache.add_dup_skip(series.skip_key(wider), series.normalize_member_paths(wider),
                               "2026-11-11T10:00:00+00:00")
        finally:
            cache.close()

        second = start()
        assert _dup_groups_json(second.port)["1"]["skipped"] is True

    def test_skips_survive_a_session_without_a_database(self, running_server):
        _httpd, state = running_server
        status, body, _ = _skip(state.port, "1", True)
        assert status == 200
        assert json.loads(body)["persisted"] is False
        assert _dup_groups_json(state.port)["1"]["skipped"] is True


class TestDupSkipFrontend:
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

    def test_skipped_groups_are_hidden_unless_the_toggle_is_on(self, dup_skip_server):
        start, _fs, _db = dup_skip_server
        body = self._function_body(self._html(start().port), "visibleDupGroups")
        assert "state.showSkippedDup" in body
        assert "g.skipped" in body

    def test_the_show_skipped_toggle_exists_and_starts_hidden_and_unchecked(self, dup_skip_server):
        start, _fs, _db = dup_skip_server
        html = self._html(start().port)
        assert 'id="show-skipped-dup"' in html
        assert re.search(r'id="dup-controls"[^>]*style="display:none"', html)
        assert re.search(r'showSkippedDup:\s*false', html)
        listener = re.search(
            r'getElementById\("show-skipped-dup"\)\.addEventListener\("change", \(e\) => \{(.*?)\}\);',
            html, re.DOTALL,
        )
        assert listener
        assert "state.showSkippedDup = e.target.checked" in listener.group(1)

    def test_the_toggle_is_shown_only_on_the_dup_tab_with_a_hidden_count(self, dup_skip_server):
        start, _fs, _db = dup_skip_server
        body = self._function_body(self._html(start().port), "updateScreenVisibility")
        assert re.search(
            r'getElementById\("dup-controls"\)\.style\.display\s*=\s*state\.tab === "dup"', body,
        )
        assert "skippedDupCount()" in body

    def test_the_checkbox_posts_to_the_server_and_flips_only_after_ok(self, dup_skip_server):
        start, _fs, _db = dup_skip_server
        html = self._html(start().port)
        assert "makeDupSkipControl(g, sel)" in self._function_body(html, "render")
        assert "toggleDupSkip(" in self._function_body(html, "makeDupSkipControl")
        toggle = self._function_body(html, "toggleDupSkip")
        assert '"/api/dup-skip"' in toggle
        assert "X-Dedup-Token" in toggle
        assert toggle.index("resp.ok") < toggle.index("group.skipped = wantSkip")

    def test_render_pages_and_applies_only_visible_groups(self, dup_skip_server):
        start, _fs, _db = dup_skip_server
        html = self._html(start().port)
        render = self._function_body(html, "render")
        assert "visibleDupGroups()" in render
        assert "visibleDupGroups()" in self._function_body(html, "groupsForCurrentTab")
        assert "visibleDupGroups()" in self._function_body(html, "updateSummary")
        assert re.search(
            r'nonAppliedGroups\(\s*currentPageGroups\(\s*visibleDupGroups\(\)\s*\)\s*\)', html,
        )
        assert re.search(r'nonAppliedGroups\(\s*visibleDupGroups\(\)\s*\)', html)

    def test_selection_takes_its_skip_flag_from_the_server(self, dup_skip_server):
        start, _fs, _db = dup_skip_server
        body = self._function_body(self._html(start().port), "initSelection")
        assert "skip: !!g.skipped" in body
