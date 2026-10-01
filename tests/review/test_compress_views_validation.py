"""Compress tab: view builders, selection validation and read endpoints."""
from __future__ import annotations

import json
from tidy.compression import compress
from tidy.compression import hwdetect
from tidy.review import validation as review_validation
from tidy.review.views import compress as compress_views
from tidy.media import thumbnails

from .helpers import (
    _get,
    _probe_result,
    _wait_compress_ready,
)


class TestBuildCompressViews:
    def test_candidate_id_and_registry_shape(self):
        candidates = [compress.CompressCandidate(
            path="D:/A/!NEW/clip.mp4", size=180_000_000,
            probe=_probe_result(), reason="bpp 0.448 > threshold 0.100 for hevc",
        )]
        candidates_json, skipped_json, _thumb_sources, registry = compress_views.build_compress_views(
            candidates, [], {}, compress.CompressConfig(), id_prefix="c1_",
        )
        assert skipped_json == []
        assert len(candidates_json) == 1
        row = candidates_json[0]
        assert row["id"] == "c1_1:0"
        assert row["group_id"] == "c1_1"
        assert row["path"] == "D:/A/!NEW/clip.mp4"
        assert row["size"] == 180_000_000
        assert row["reason"] == "bpp 0.448 > threshold 0.100 for hevc"
        assert registry == {
            "c1_1:0": {"path": "D:/A/!NEW/clip.mp4", "group_id": "c1_1", "size": 180_000_000},
        }

    def test_skipped_rows_carry_path_and_reason_only(self):
        skipped = [compress.CompressSkipped(
            path="D:/A/!NEW/old.mp4", reason="bpp 0.05 <= threshold 0.10 for hevc",
        )]
        candidates_json, skipped_json, thumb_sources, registry = compress_views.build_compress_views(
            [], skipped, {}, compress.CompressConfig(),
        )
        assert candidates_json == []
        assert registry == {}
        assert thumb_sources == {}
        assert skipped_json == [{
            "path": "D:/A/!NEW/old.mp4", "reason": "bpp 0.05 <= threshold 0.10 for hevc",
        }]

    def test_estimates_cover_every_configured_profile(self):
        candidates = [compress.CompressCandidate(
            path="D:/A/clip.mp4", size=180_000_000, probe=_probe_result(), reason="r",
        )]
        candidates_json, _s, _t, _r = compress_views.build_compress_views(
            candidates, [], {}, compress.CompressConfig(), id_prefix="c1_",
        )
        estimates = candidates_json[0]["estimates"]
        names = {e["profile_name"] for e in estimates}
        # No available_encoders restriction given -- every configured
        # profile gets an estimate row, hardware or not (filtering by what
        # this machine can actually run is build_compress_views()'s
        # available_encoders parameter, exercised separately below).
        assert names == set(compress.CompressConfig().profiles)
        seeded_profiles = {"x265", "x265_medium", "x265_high", "av1"}
        for e in estimates:
            # No calibration table was passed in -- every estimate must
            # come from config.py's seed values, never claim a measurement
            # it doesn't have (docs/technical/components/compression-mechanism.md).
            assert e["calibrated"] is False
            if e["profile_name"] in seeded_profiles:
                assert e["estimated_size"] > 0
                assert e["estimated_seconds"] > 0
            else:
                # Hardware profile with no SEED_CALIBRATION entry (docs/technical/components/compression-mechanism.md
                # E3.2: "Zadnych zmyslonych nasion") -- "?" in the UI, never a
                # guessed number.
                assert e["estimated_size"] == 0
                assert e["estimated_seconds"] == 0.0

    def test_available_encoders_hides_unrunnable_hardware_profiles(self):
        # docs/technical/components/compression-mechanism.md: a hardware profile whose encoder this machine
        # doesn't actually have is never shown, let alone later accepted by
        # POST /api/apply-compress.
        candidates = [compress.CompressCandidate(
            path="D:/A/clip.mp4", size=180_000_000, probe=_probe_result(), reason="r",
        )]
        candidates_json, _s, _t, _r = compress_views.build_compress_views(
            candidates, [], {}, compress.CompressConfig(), id_prefix="c1_",
            available_encoders={"libx265", "libsvtav1", "hevc_qsv"},
        )
        names = {e["profile_name"] for e in candidates_json[0]["estimates"]}
        assert names == {"x265", "x265_medium", "x265_high", "av1", "hevc_qsv"}

    def test_two_computations_never_hand_out_the_same_id(self):
        # Same regression build_placement_views() guards against
        # (TestPlacementIdNamespacesAcrossComputations): compress_registry
        # is one dict every computation update()s, so two source
        # directories' ids must be disjoint by construction.
        first = [compress.CompressCandidate(
            path="D:/A/first.mp4", size=1, probe=_probe_result(), reason="r")]
        second = [compress.CompressCandidate(
            path="D:/A/second.mp4", size=1, probe=_probe_result(), reason="r")]
        _j1, _s1, _t1, reg_a = compress_views.build_compress_views(
            first, [], {}, compress.CompressConfig(), id_prefix="c1_")
        _j2, _s2, _t2, reg_b = compress_views.build_compress_views(
            second, [], {}, compress.CompressConfig(), id_prefix="c2_")
        assert set(reg_a) & set(reg_b) == set()

    def test_thumb_sources_populated_only_when_thumbnails_supports_the_path(self, monkeypatch):
        monkeypatch.setattr(thumbnails, "supports", lambda path: True)
        candidates = [compress.CompressCandidate(
            path="D:/A/clip.mp4", size=10, probe=_probe_result(), reason="r")]
        candidates_json, _s, thumb_sources, _r = compress_views.build_compress_views(
            candidates, [], {}, compress.CompressConfig(), id_prefix="c1_",
        )
        thumb_id = candidates_json[0]["thumb_id"]
        assert thumb_id is not None
        assert thumb_sources[thumb_id] == "D:/A/clip.mp4"

    def test_thumb_id_is_none_when_thumbnails_does_not_support_the_path(self, monkeypatch):
        monkeypatch.setattr(thumbnails, "supports", lambda path: False)
        candidates = [compress.CompressCandidate(
            path="D:/A/clip.mp4", size=10, probe=_probe_result(), reason="r")]
        candidates_json, _s, thumb_sources, _r = compress_views.build_compress_views(
            candidates, [], {}, compress.CompressConfig(), id_prefix="c1_",
        )
        assert candidates_json[0]["thumb_id"] is None
        assert thumb_sources == {}


class TestCompressHTTP:
    def test_unknown_source_directory_is_rejected(self, compress_server):
        httpd, state, fs = compress_server
        status, body, _ = _get(state.port, "/api/compress?source=%23Nowhere")
        assert status == 400
        assert "unknown source directory" in json.loads(body.decode("utf-8"))["error"]

    def test_traversal_in_source_is_rejected_and_starts_no_computation(self, compress_server):
        # Contract test #4 (docs/technical/development/conventions-and-tests.md), adapted from the
        # placement screen's own traversal test: gate 1
        # (sanitize_dest_folder()) rejects before _compress_slots() is ever
        # reached, so no computation is created for this key at all.
        httpd, state, fs = compress_server
        status, body, _ = _get(state.port, "/api/compress?source=..%5C..%5CWindows")
        assert status == 400
        assert "source:" in json.loads(body.decode("utf-8"))["error"]
        assert state.compress_computations == {}

    def test_real_scan_finds_and_estimates_a_video_candidate(self, compress_server):
        httpd, state, fs = compress_server
        payload = _wait_compress_ready(state.port)
        assert payload["status"] == "ready"
        assert payload["source"] == "!NEW"
        assert len(payload["candidates"]) == 1
        candidate = payload["candidates"][0]
        assert candidate["path"].endswith("clip.avi")
        # No apply endpoint exists yet in this step -- every row must still
        # report both flags so a later step adding one is a pure addition.
        assert candidate["applied"] is False
        assert candidate["skipped"] is False
        names = {e["profile_name"] for e in candidate["estimates"]}
        # Only profiles whose encoder this real machine actually has pass
        # build_compress_views()'s available_encoders filter (docs/technical/components/compression-mechanism.md
        # E3.3) -- computed fresh here rather than assumed static, since
        # which hardware encoders a given machine exposes varies.
        hw = hwdetect.detect(None)
        available = {e.name for e in hw.encoders} if hw is not None else set()
        expected_names = {
            name for name, p in compress.CompressConfig().profiles.items()
            if p["encoder"] in available
        }
        assert names == expected_names
        assert payload["skipped"] == []

    def test_progress_ends_full_when_the_directory_is_ready(self, compress_server):
        httpd, state, fs = compress_server
        payload = _wait_compress_ready(state.port)
        done, total = payload["progress"]["done"], payload["progress"]["total"]
        assert total > 0
        assert done == total, "the bar must end exactly full, not short of its total"


class TestCompressEncodersHTTP:
    def test_returns_available_encoders_from_the_bundled_binary(self, compress_server):
        httpd, state, fs = compress_server
        status, body, _ = _get(state.port, "/api/compress/encoders")
        assert status == 200
        payload = json.loads(body.decode("utf-8"))
        assert payload["available"] is True
        names = {e["name"] for e in payload["encoders"]}
        # libx265 is always in the bundled GPL build's -encoders list and
        # needs no hardware init test (hwdetect.py's CPU-encoder branch),
        # unlike NVENC/QSV which depend on what is actually installed.
        assert "libx265" in names
        for e in payload["encoders"]:
            # No compression has run on this fresh test DB yet -- nothing
            # to be calibrated against (docs/technical/components/compression-mechanism.md: real work only).
            assert e["calibrated"] is False
            assert e["device_key"]  # never empty, CPU or hardware
        by_name = {e["name"]: e for e in payload["encoders"]}
        assert by_name["libx265"]["device_key"] == "libx265"
        assert payload["machine_id"]
        assert payload["ffmpeg_version"]

    def test_degrades_gracefully_when_ffmpeg_is_unavailable(self, compress_server, monkeypatch):
        httpd, state, fs = compress_server
        monkeypatch.setattr(hwdetect, "detect", lambda *_a, **_kw: None)
        status, body, _ = _get(state.port, "/api/compress/encoders")
        assert status == 200
        payload = json.loads(body.decode("utf-8"))
        assert payload == {
            "available": False, "encoders": [], "machine_id": None, "ffmpeg_version": None,
            "unavailable_reason": None,
        }

    def test_detection_is_memoised_not_reprobed_per_request(self, compress_server, monkeypatch):
        httpd, state, fs = compress_server
        real_detect = hwdetect.detect
        calls = []

        def _counting_detect(path):
            calls.append(path)
            return real_detect(path)

        monkeypatch.setattr(hwdetect, "detect", _counting_detect)
        status1, _b1, _h1 = _get(state.port, "/api/compress/encoders")
        status2, _b2, _h2 = _get(state.port, "/api/compress/encoders")
        assert status1 == 200 and status2 == 200
        assert len(calls) == 1


# --- compress queue, apply endpoints, skip, rescan -------------------------

class TestValidateCompressSelections:
    def test_resolves_entries_through_registry(self):
        registry = {"c1_1:0": {"path": "D:/A/x.mp4", "group_id": "c1_1", "size": 10}}
        entries, error = review_validation._validate_compress_selections(
            [{"group_id": "c1_1", "file_ids": ["c1_1:0"]}], registry,
        )
        assert error is None
        assert entries == [
            {"file_id": "c1_1:0", "group_id": "c1_1", "path": "D:/A/x.mp4", "size": 10},
        ]

    def test_unknown_id_is_rejected(self):
        entries, error = review_validation._validate_compress_selections(
            [{"group_id": "c1_1", "file_ids": ["c1_1:0"]}], {},
        )
        assert entries == []
        assert "unknown id" in error

    def test_empty_selections_is_rejected(self):
        entries, error = review_validation._validate_compress_selections([], {})
        assert entries == []
        assert error

    def test_duplicate_group_id_is_rejected(self):
        registry = {
            "c1_1:0": {"path": "a", "group_id": "c1_1", "size": 1},
            "c1_2:0": {"path": "b", "group_id": "c1_2", "size": 1},
        }
        selections = [
            {"group_id": "c1_1", "file_ids": ["c1_1:0"]},
            {"group_id": "c1_1", "file_ids": ["c1_2:0"]},
        ]
        entries, error = review_validation._validate_compress_selections(selections, registry)
        assert entries == []
        assert "submitted more than once" in error

    def test_id_not_belonging_to_its_declared_group_is_rejected(self):
        registry = {"c1_1:0": {"path": "a", "group_id": "c1_1", "size": 1}}
        entries, error = review_validation._validate_compress_selections(
            [{"group_id": "c1_2", "file_ids": ["c1_1:0"]}], registry,
        )
        assert entries == []
        assert "does not belong to group" in error


class TestValidateCompressRemoveSelections:
    def test_requires_compressed_path_on_the_registry_entry(self):
        # Structural "no checkbox for an unverified row" contract
        # (docs/technical/components/review-ui.md) expressed server-side: a candidate row from
        # GET /api/compress never has a compressed_path.
        registry = {"c1_1:0": {"path": "D:/A/x.mp4", "group_id": "c1_1", "size": 10}}
        removals, error = review_validation._validate_compress_remove_selections(
            [{"group_id": "c1_1", "file_ids": ["c1_1:0"]}], registry,
        )
        assert removals == []
        assert "no verified compressed result" in error

    def test_resolves_removal_plans_when_compressed_path_present(self):
        registry = {
            "c9_1:0": {
                "path": "D:/A/x.mp4", "compressed_path": "D:/A/x_compressed.mp4",
                "group_id": "c9_1", "size": 10,
            },
        }
        removals, error = review_validation._validate_compress_remove_selections(
            [{"group_id": "c9_1", "file_ids": ["c9_1:0"]}], registry,
        )
        assert error is None
        assert len(removals) == 1
        assert removals[0].original_abs == "D:/A/x.mp4"
        assert removals[0].compressed_abs == "D:/A/x_compressed.mp4"
        assert removals[0].group_id == "c9_1"
