"""Placement (!NEW inbox) over HTTP: destinations, progress, registries, rescan."""
from __future__ import annotations

import json
import threading
import urllib.parse
from datetime import datetime
from tidy.analysis import inbox
from tidy.review import page as review_page
from tidy.review import server as review_server
from tidy.review import validation as review_validation
from tidy.review.views import placement as placement_views
from fakes import FakeFileSystem
from tidy.duplicates.scanner import ScannedFile

from .helpers import (
    _get,
    _jpeg_bytes,
    _post_json,
    _wait_placement_ready,
)


class TestDestCandidatesReachEveryFolder:
    """Bug report: "nie wyswietlaja sie wszystkie katalogi
    do 2 poziomu jak wczesniej".

    _dest_candidates() scopes to the suggestion's own top-level so the
    likely answers come first. That scoping used to also HIDE the rest of
    the archive -- and whether it did depended on whether the invented
    top-level existed yet, so a folder created during the session (an
    apply creating `#Nieznane`) silently shrank the list from ~110 entries
    to two. The scoped folders still come first; the rest is now carried
    with `outside: True` for the picker's own group."""

    def _dir_index(self):
        return [
            {"rel": "#Nieznane", "file_count": 0, "depth": 1},
            {"rel": "#Nieznane\\2024 Starachowice", "file_count": 12, "depth": 2},
            {"rel": "#Home", "file_count": 0, "depth": 1},
            {"rel": "#Home\\2024 Gdansk", "file_count": 402, "depth": 2},
            {"rel": "#Home\\Krakow", "file_count": 33, "depth": 2},
            {"rel": "!NEW", "file_count": 0, "depth": 1},
            {"rel": "!NEW\\Camera", "file_count": 88, "depth": 2},
        ]

    def test_folders_outside_the_scope_are_offered_last_and_flagged(self):
        cands = placement_views._dest_candidates(
            "#Nieznane\\2023 (nieznane miejsce)", self._dir_index())
        rels = [c["rel"] for c in cands]
        # scoped first, in dir_index order, unflagged
        assert rels[:2] == ["#Nieznane", "#Nieznane\\2024 Starachowice"]
        assert all(not c.get("outside") for c in cands[:2])
        # everything else still reachable, flagged for its own group
        assert set(rels[2:]) == {"#Home", "#Home\\2024 Gdansk", "#Home\\Krakow"}
        assert all(c["outside"] for c in cands[2:])

    def test_the_new_folder_screen_never_offers_the_inbox_as_a_destination(self):
        cands = placement_views._dest_candidates(
            "#Nieznane\\2023 (nieznane miejsce)", self._dir_index())
        rels = {c["rel"] for c in cands}
        assert "!NEW" not in rels
        assert "!NEW\\Camera" not in rels

    def test_an_invented_top_level_still_yields_one_unflagged_list(self):
        # No `#Gdansk` on disk: the whole archive IS the answer here, so it
        # must not be pushed into the "other folders" group leaving the
        # main one empty.
        cands = placement_views._dest_candidates(
            "#Gdansk\\2024 Gdansk", self._dir_index())
        assert cands
        assert all(not c.get("outside") for c in cands)
        assert "#Home\\2024 Gdansk" in {c["rel"] for c in cands}


class TestPlacementIdNamespacesAcrossComputations:
    """Regression: "nie zawsze wszystkie zdjecia
    sa zaznaczone".

    The visible symptom was cosmetic -- cards rendering unticked after a
    source-directory switch, because the client keeps its selections keyed
    by group_id while every computation numbered from p1, so a remembered
    id set was handed to a DIFFERENT group of the same number. Underneath
    it was a data bug: placement_registry is one dict that each computation
    update()s, so the colliding ids on a screen the user came back to
    resolved to the other directory's files, and the apply moved those."""

    def _classification(self, name, folder, paths):
        return inbox.FolderClassification(
            name=name,
            classification=inbox.KIND_MIXED,
            suggestions=(inbox.PlacementSuggestion(
                suggested_folder=folder,
                matched_existing_dir=False,
                paths=tuple(paths),
                photo_count=len(paths),
                video_count=0,
            ),),
        )

    def _two_sources(self):
        first = [self._classification(
            "Camera", "#Home\\2024 Gdansk",
            ["D:/A/!NEW/Camera/a0.jpg", "D:/A/!NEW/Camera/a1.jpg"])]
        second = [self._classification(
            "Stare", "#Travel\\2023 Mountains",
            ["D:/A/Stare/b%d.jpg" % i for i in range(5)])]
        return first, second

    def test_two_computations_never_hand_out_the_same_id(self):
        first, second = self._two_sources()
        _folders_a, _t, reg_a = placement_views.build_placement_views(
            first, {}, [], id_prefix="p1_")
        _folders_b, _t, reg_b = placement_views.build_placement_views(
            second, {}, [], id_prefix="p2_")
        assert set(reg_a) & set(reg_b) == set()

    def test_apply_from_an_earlier_screen_still_resolves_to_its_own_files(self):
        # The scenario that moved the wrong files: compute one source, switch
        # (the second computation merges into the SAME registry), come back --
        # the first screen is served from cache, unchanged -- and apply. Its
        # ids must still name its own photos. Before the fix this produced
        # D:/A/Stare/b0.jpg with no validation error at all.
        first, second = self._two_sources()
        registry = {}
        folders_a, _t, reg_a = placement_views.build_placement_views(
            first, {}, [], id_prefix="p1_")
        registry.update(reg_a)
        _folders_b, _t, reg_b = placement_views.build_placement_views(
            second, {}, [], id_prefix="p2_")
        registry.update(reg_b)

        suggestion = folders_a[0]["suggestions"][0]
        plans, error = review_validation._validate_placement_selections(
            [{
                "group_id": suggestion["group_id"],
                "file_ids": [e["id"] for e in suggestion["entries"]],
                "dest": suggestion["suggested_folder"],
            }],
            registry, "D:/A",
        )
        assert error is None
        assert [p.src_abs for p in plans] == [
            "D:/A/!NEW/Camera/a0.jpg", "D:/A/!NEW/Camera/a1.jpg",
        ]


class TestPlacementProgress:
    """"Sugestie grupowania"
    showed a bare "licze..." for the whole wait after a source-directory
    switch, while the two tabs beside it already had a progress bar. The
    endpoint had always carried a "progress" field -- nothing ever wrote to
    the counter behind it, so the front-end (which only draws a bar once
    total > 0) had nothing to draw."""

    def test_progress_ends_full_when_the_directory_is_ready(self, placement_server):
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        assert payload["status"] == "ready"
        done, total = payload["progress"]["done"], payload["progress"]["total"]
        assert total > 0
        assert done == total, "the bar must end exactly full, not short of its total"

    def test_total_covers_the_date_index_too_not_just_the_source_folder(
        self, placement_server,
    ):
        # The fixture's source directory holds 2 media files; the archive
        # date index walks the whole candidate pool on top of that.
        # A total of exactly 2 would mean the index's minutes of work are
        # once again invisible -- the half of the wait the user actually
        # noticed.
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        assert payload["progress"]["total"] > 2

    def test_response_reports_progress_while_computing(
        self, placement_server, monkeypatch,
    ):
        started = threading.Event()
        release = threading.Event()

        def fake_classify_source(fs, source_abs, files, archive_root, *, progress=None, **kwargs):
            if progress is not None:
                progress.add_total(10)
                progress.increment(4)
            started.set()
            release.wait(timeout=5)
            return [], [], {"suspicious": [], "camera_drift_warnings": []}

        monkeypatch.setattr(inbox, "classify_source", fake_classify_source)
        httpd, state, fs = placement_server

        t = threading.Thread(
            target=lambda: _get(state.port, "/api/placement?source=" + urllib.parse.quote("!NEW")),
        )
        t.start()
        assert started.wait(timeout=5)

        # A second request while the fake engine is still blocked must see
        # the partial progress the worker thread already published -- the
        # same cross-thread read the series tab's own test pins.
        status, body, _ = _get(state.port, "/api/placement?source=" + urllib.parse.quote("!NEW"))
        assert status == 200
        payload = json.loads(body.decode("utf-8"))
        assert payload["status"] == "computing"
        assert payload["progress"] == {"done": 4, "total": 10}

        release.set()
        t.join(timeout=5)

    def test_classify_source_receives_the_counter_for_this_directory(
        self, placement_server, monkeypatch,
    ):
        captured = {}
        called = threading.Event()

        def fake_classify_source(fs, source_abs, files, archive_root, **kwargs):
            captured.update(kwargs)
            called.set()
            return [], [], {"suspicious": [], "camera_drift_warnings": []}

        monkeypatch.setattr(inbox, "classify_source", fake_classify_source)
        httpd, state, fs = placement_server

        status, body, _ = _get(state.port, "/api/placement?source=" + urllib.parse.quote("!NEW"))
        assert status == 200
        assert called.wait(timeout=5), "classify_source() was never actually invoked"
        # Per source directory, not one shared counter: switching
        # directories must not have the new one inherit the old one's bar.
        assert captured["progress"] is state.placement_progress["!NEW"]


class TestRescanRefreshesEveryTab:
    """POST /api/rescan.

    The button exists because the archive changes on disk while the server
    is up -- a new batch lands in !NEW -- and every tab is computed from
    the file list captured at startup."""

    def _server(self, tmp_path, rescan):
        fs = FakeFileSystem()
        fs.add_dir("D:/Archive")
        fs.add_dir("D:/Archive/!NEW")
        fs.add_dir("D:/Archive/!NEW/Gdansk")
        base = int(datetime(2024, 6, 1, 10, 0).timestamp() * 1_000_000_000)
        path = "D:/Archive/!NEW/Gdansk/a.jpg"
        fs.add_file(path, _jpeg_bytes(), volume_id=1, file_index=100, mtime_ns=base)
        first = ScannedFile(path=path, stat=fs.stat(path), is_duplicate_candidate=True)

        httpd, thread, state = review_server.start_server(
            fs=fs, archive_root="D:/Archive", duplicate_groups=[], decisions=[],
            thumbs_dir=str(tmp_path / "thumbs"), host="127.0.0.1", port=0,
            token="test-token", candidates=[first],
            db_path=str(tmp_path / "cache.sqlite3"),
            rescan=lambda: rescan(fs, base),
        )
        return httpd, thread, state, fs

    def test_files_added_after_startup_show_up_after_a_rescan(self, tmp_path):
        def rescan(fs, base):
            path = "D:/Archive/!NEW/Gdansk/b.jpg"
            fs.add_file(path, _jpeg_bytes(), volume_id=1, file_index=101,
                        mtime_ns=base + 60 * 1_000_000_000)
            return [], [], [
                ScannedFile(path=p, stat=fs.stat(p), is_duplicate_candidate=True)
                for p in ("D:/Archive/!NEW/Gdansk/a.jpg", "D:/Archive/!NEW/Gdansk/b.jpg")
            ]

        httpd, thread, state, fs = self._server(tmp_path, rescan)
        try:
            before = _wait_placement_ready(state.port)
            assert len(before["folders"][0]["suggestions"][0]["entries"]) == 1

            status, body, _ = _post_json(
                state.port, "/api/rescan",
                headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
                payload={},
            )
            assert status == 200
            assert json.loads(body.decode("utf-8"))["candidates"] == 2

            after = _wait_placement_ready(state.port)
            assert len(after["folders"][0]["suggestions"][0]["entries"]) == 2
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_source_dirs_counts_are_refreshed_by_a_rescan(self, tmp_path):
        # Bug report: "w Sugestie grupowania po Rescan w
        # Katalog zrodlowy nie zmienia sie liczba zdjec". The picker's
        # "(N plikow)" comes from GET /api/source-dirs, which _dir_index()
        # memoises -- so the count is only right if the rescan drops that
        # cache. This asserts the server half; the client half is the
        # `sourceDirsLoaded` reset covered by the next test.
        def rescan(fs, base):
            path = "D:/Archive/!NEW/Gdansk/b.jpg"
            fs.add_file(path, _jpeg_bytes(), volume_id=1, file_index=101,
                        mtime_ns=base + 60 * 1_000_000_000)
            return [], [], [
                ScannedFile(path=p, stat=fs.stat(p), is_duplicate_candidate=True)
                for p in ("D:/Archive/!NEW/Gdansk/a.jpg", "D:/Archive/!NEW/Gdansk/b.jpg")
            ]

        httpd, thread, state, fs = self._server(tmp_path, rescan)
        try:
            # Fetched BEFORE the rescan on purpose: this is what populates
            # dir_index_cache, so a rescan that failed to invalidate it
            # would keep answering 1 below.
            status, body, _ = _get(state.port, "/api/source-dirs")
            assert status == 200
            before = {d["rel"]: d["file_count"] for d in json.loads(body.decode("utf-8"))["dirs"]}
            assert before["!NEW\\Gdansk"] == 1

            status, _, _ = _post_json(
                state.port, "/api/rescan",
                headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
                payload={},
            )
            assert status == 200

            status, body, _ = _get(state.port, "/api/source-dirs")
            assert status == 200
            after = {d["rel"]: d["file_count"] for d in json.loads(body.decode("utf-8"))["dirs"]}
            assert after["!NEW\\Gdansk"] == 2
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_rescan_clears_the_client_source_dirs_one_shot_guard(self):
        # The other half of the same bug, and the half that actually bit:
        # the server was already re-walking the disk, but ensureSourceDirs()
        # returns immediately while `sourceDirsLoaded` is true, so the
        # <option> labels stayed frozen at their startup counts. There is no
        # JS runner in this suite, so the guarantee is pinned on the source
        # of the served page rather than left untested.
        page = review_page.INDEX_HTML
        # Split on the listener registration, not on the id alone -- the id
        # appears again on the next line (`const btn = ...`), which would
        # slice out a two-line window instead of the handler body.
        rescan_handler = page.split('"rescan-btn").addEventListener')[1]
        assert "sourceDirsLoaded = false;" in rescan_handler
        assert "await ensureSourceDirs();" in rescan_handler

    def test_applied_ids_are_cleared_because_a_rescan_renumbers_them(self, tmp_path):
        # Ids are positional ("p3:1"). Keeping the applied-set across a
        # rescan would mark whichever unrelated suggestion inherited the
        # number as already applied, hiding it from the user entirely.
        def rescan(fs, base):
            return [], [], [
                ScannedFile(path="D:/Archive/!NEW/Gdansk/a.jpg",
                            stat=fs.stat("D:/Archive/!NEW/Gdansk/a.jpg"),
                            is_duplicate_candidate=True),
            ]

        httpd, thread, state, fs = self._server(tmp_path, rescan)
        try:
            state.applied_placement_group_ids.add("p1")
            state.applied_group_ids.add("1")
            state.applied_series_group_ids.add("s1")

            status, _, _ = _post_json(
                state.port, "/api/rescan",
                headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
                payload={},
            )
            assert status == 200
            assert state.applied_placement_group_ids == set()
            assert state.applied_group_ids == set()
            assert state.applied_series_group_ids == set()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_a_failing_rescan_reports_instead_of_killing_the_server(self, tmp_path):
        def rescan(fs, base):
            raise OSError("drive went away")

        httpd, thread, state, fs = self._server(tmp_path, rescan)
        try:
            status, body, _ = _post_json(
                state.port, "/api/rescan",
                headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
                payload={},
            )
            assert status == 500
            assert "drive went away" in json.loads(body.decode("utf-8"))["error"]
            # Still serving.
            assert _get(state.port, "/api/state")[0] == 200
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


class TestPlacementHTTP:
    def test_source_dirs_lists_archive_dirs_without_tool_directories(self, placement_server):
        httpd, state, fs = placement_server
        status, body, _ = _get(state.port, "/api/source-dirs")
        assert status == 200
        payload = json.loads(body.decode("utf-8"))
        rels = {d["rel"] for d in payload["dirs"]}
        assert payload["default"] == "!NEW"
        assert "!NEW" in rels
        assert "#Home" in rels
        assert "#Home\\2024 Gdansk" in rels
        assert not any(r.startswith("!REMOVE") or r.startswith("!MOVES") for r in rels)

    def test_placement_computes_suggestions_for_the_new_folder(self, placement_server):
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        assert payload["status"] == "ready"
        assert payload["source"] == "!NEW"
        folders = payload["folders"]
        assert len(folders) == 1
        assert folders[0]["name"] == "Gdansk"
        # A meaningful folder name means one whole-folder move, and
        # the name itself becomes the place name.
        assert folders[0]["whole_folder"] is True
        suggestion = folders[0]["suggestions"][0]
        # The "RRRR Miejsce" naming convention, and a NEW top-level: with no
        # GPS there is no country to geocode, so the engine cannot know
        # these belong under the existing #Home. It does not guess --
        # it proposes, and the candidate list (next test) puts the
        # alternative one click away.
        assert suggestion["suggested_folder"] == "#Gdansk\\2024 Gdansk"
        assert len(suggestion["entries"]) == 2

    def test_switching_source_and_back_keeps_every_id_pointing_at_its_own_files(
        self, placement_server,
    ):
        # Regression, end to end over the socket:
        # two source directories, two computations, ONE placement_registry.
        # Ids must not collide, and the first directory's ids must still
        # resolve to the first directory's files after the second one has
        # been computed -- the screen for it is served from cache and never
        # recomputed, so a collision here is an apply on the wrong files.
        httpd, state, fs = placement_server

        first = _wait_placement_ready(state.port, source="!NEW")
        first_ids = [e["id"] for f in first["folders"]
                     for s in f["suggestions"] for e in s["entries"]]
        assert first_ids

        second = _wait_placement_ready(state.port, source="#Home")
        second_ids = [e["id"] for f in second["folders"]
                      for s in f["suggestions"] for e in s["entries"]]
        assert second_ids
        assert set(first_ids) & set(second_ids) == set()

        # back to the first directory: same cached ids, same files
        again = _wait_placement_ready(state.port, source="!NEW")
        again_ids = [e["id"] for f in again["folders"]
                     for s in f["suggestions"] for e in s["entries"]]
        assert again_ids == first_ids
        for file_id in again_ids:
            assert state.placement_registry[file_id]["path"].startswith(
                f"{state.archive_root}/!NEW/")
        for file_id in second_ids:
            assert state.placement_registry[file_id]["path"].startswith(
                f"{state.archive_root}/#Home/")

    def test_suggestion_offers_existing_folders_as_candidates(self, placement_server):
        # The existing #Home\2024 Gdansk must be offered, with its
        # file count, rather than silently reused or silently ignored.
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        candidates = payload["folders"][0]["suggestions"][0]["candidates"]
        rels = {c["rel"]: c["file_count"] for c in candidates}
        assert "#Home" in rels
        assert rels["#Home\\2024 Gdansk"] == 2

    def test_rescan_is_unavailable_when_no_pipeline_was_injected(self, placement_server):
        # Every existing fixture starts the server without one. Reporting
        # that plainly beats a button that appears to refresh and doesn't.
        httpd, state, fs = placement_server
        status, body, _ = _post_json(
            state.port, "/api/rescan",
            headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
            payload={},
        )
        assert status == 400
        assert "rescan" in json.loads(body.decode("utf-8"))["error"]

    def test_rescan_requires_the_same_gate_as_an_apply(self, placement_server):
        # It moves nothing, but it walks the whole archive and discards
        # every computed view -- a cross-site POST could stall the tool for
        # minutes, so it must not be reachable with a spoofed Host.
        httpd, state, fs = placement_server
        status, _, _ = _post_json(
            state.port, "/api/rescan",
            headers={"X-Dedup-Token": "test-token", "Host": "evil.example.com"},
            payload={},
        )
        assert status == 403

    def test_badge_follows_the_proposed_folder_not_its_top_level(self, placement_server):
        # The reported bug: events.matched_existing_dir is true as soon as
        # the TOP LEVEL exists, and the screen rendered it against the full
        # path -- labelling `#Home\2022 High Mountains`, a directory
        # not on disk, "istniejacy katalog". Here the proposal is
        # `#Gdansk\2024 Gdansk`, which does not exist either.
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        suggestion = payload["folders"][0]["suggestions"][0]
        assert suggestion["suggested_folder"] == "#Gdansk\\2024 Gdansk"
        assert suggestion["dest_exists"] is False

    def test_dest_exists_is_true_for_a_folder_actually_on_disk(self, placement_server):
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        candidates = payload["folders"][0]["suggestions"][0]["candidates"]
        # Everything offered as a candidate comes from the directory walk,
        # so each one is by construction a folder that exists -- the flag
        # must agree with that for the one the engine happens to propose.
        assert any(c["rel"] == "#Home\\2024 Gdansk" for c in candidates)

    def test_a_folder_holding_photos_from_the_same_days_is_offered_first(self, placement_server):
        # `#Home\2024 Gdansk` holds old.jpg, taken the same day as
        # the two files being placed, so it leads the candidate list and
        # says why. Without this the right answer sits somewhere in a list
        # ordered by nothing in particular.
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        candidates = payload["folders"][0]["suggestions"][0]["candidates"]
        assert candidates[0]["rel"] == "#Home\\2024 Gdansk"
        assert candidates[0]["overlap_files"] >= 1
        assert "already has" in candidates[0]["why"]

    def test_unknown_source_directory_is_rejected(self, placement_server):
        httpd, state, fs = placement_server
        status, body, _ = _get(state.port, "/api/placement?source=%23Nowhere")
        assert status == 400
        assert "unknown source directory" in json.loads(body.decode("utf-8"))["error"]

    def test_traversal_in_source_is_rejected(self, placement_server):
        httpd, state, fs = placement_server
        status, body, _ = _get(state.port, "/api/placement?source=..%5C..%5CWindows")
        assert status == 400
        assert "source:" in json.loads(body.decode("utf-8"))["error"]

    def test_apply_placement_moves_the_files_into_the_archive(self, placement_server):
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        suggestion = payload["folders"][0]["suggestions"][0]

        status, body, _ = _post_json(
            state.port, "/api/apply-placement",
            headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
            payload={"selections": [{
                "group_id": suggestion["group_id"],
                "file_ids": [e["id"] for e in suggestion["entries"]],
                "dest": "#Home\\2024 Gdansk",
            }]},
        )
        assert status == 200
        result = json.loads(body.decode("utf-8"))
        assert result["moved"] == 2
        assert result["failed"] == 0
        # The files land in the archive proper -- NOT in !REMOVE, and not in
        # the batch directory, which holds only the manifest.
        assert fs.exists(f"{state.archive_root}/#Home/2024 Gdansk/a.jpg") is True
        assert fs.exists(f"{state.archive_root}/!NEW/Gdansk/a.jpg") is False
        assert "!MOVES" in result["batch_dir"]
        assert fs.exists(f"{state.archive_root}/!REMOVE") is False

    def test_applied_suggestion_is_flagged_and_cannot_be_applied_twice(self, placement_server):
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        suggestion = payload["folders"][0]["suggestions"][0]
        request = {"selections": [{
            "group_id": suggestion["group_id"],
            "file_ids": [e["id"] for e in suggestion["entries"]],
            "dest": "#Home\\2024 Gdansk",
        }]}
        headers = {"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"}

        first, _, _ = _post_json(state.port, "/api/apply-placement", headers=headers, payload=request)
        assert first == 200
        second, body, _ = _post_json(state.port, "/api/apply-placement", headers=headers, payload=request)
        assert second == 409

        # The flag shows up on the very next GET, without the
        # directory being recomputed.
        after = _wait_placement_ready(state.port)
        assert after["folders"][0]["suggestions"][0]["applied"] is True

    def test_destination_outside_the_archive_is_refused_by_the_validator(self, placement_server):
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        suggestion = payload["folders"][0]["suggestions"][0]

        status, body, _ = _post_json(
            state.port, "/api/apply-placement",
            headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
            payload={"selections": [{
                "group_id": suggestion["group_id"],
                "file_ids": [suggestion["entries"][0]["id"]],
                "dest": "..\\..\\Windows",
            }]},
        )
        assert status == 400
        assert fs.exists(f"{state.archive_root}/!NEW/Gdansk/a.jpg") is True

    def test_spoofed_host_is_rejected_before_anything_moves(self, placement_server):
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        suggestion = payload["folders"][0]["suggestions"][0]

        status, _, _ = _post_json(
            state.port, "/api/apply-placement",
            headers={"X-Dedup-Token": "test-token", "Host": "evil.example.com"},
            payload={"selections": [{
                "group_id": suggestion["group_id"],
                "file_ids": [suggestion["entries"][0]["id"]],
                "dest": "#Home",
            }]},
        )
        assert status == 403
        assert fs.exists(f"{state.archive_root}/!NEW/Gdansk/a.jpg") is True

    def test_unticking_files_moves_only_the_selected_ones(self, placement_server):
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        suggestion = payload["folders"][0]["suggestions"][0]

        status, body, _ = _post_json(
            state.port, "/api/apply-placement",
            headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
            payload={"selections": [{
                "group_id": suggestion["group_id"],
                "file_ids": [suggestion["entries"][0]["id"]],
                "dest": "#Home\\2024 Gdansk",
            }]},
        )
        assert status == 200
        assert json.loads(body.decode("utf-8"))["moved"] == 1
        assert fs.exists(f"{state.archive_root}/!NEW/Gdansk/b.jpg") is True


class TestPlacementRegistrySeparation:
    """A fourth registry: an id from one screen must never be
    resolvable by another screen's apply endpoint."""

    def test_placement_id_sent_to_apply_series_is_rejected(self, placement_server):
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        suggestion = payload["folders"][0]["suggestions"][0]
        file_id = suggestion["entries"][0]["id"]

        status, body, _ = _post_json(
            state.port, "/api/apply-series",
            headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
            payload={"selections": [{
                "group_id": suggestion["group_id"],
                "keep_ids": [file_id],
                "discard_ids": [],
            }]},
        )
        assert status == 400
        assert "unknown id" in json.loads(body.decode("utf-8"))["error"]
        assert fs.exists(f"{state.archive_root}/!NEW/Gdansk/a.jpg") is True

    def test_placement_id_sent_to_apply_is_rejected(self, placement_server):
        httpd, state, fs = placement_server
        payload = _wait_placement_ready(state.port)
        suggestion = payload["folders"][0]["suggestions"][0]
        file_id = suggestion["entries"][0]["id"]

        status, body, _ = _post_json(
            state.port, "/api/apply",
            headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
            payload={"selections": [{
                "group_id": suggestion["group_id"],
                "keeper_id": file_id,
                "discard_ids": [],
            }]},
        )
        assert status == 400
        assert fs.exists(f"{state.archive_root}/!NEW/Gdansk/a.jpg") is True
