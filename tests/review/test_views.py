"""Pure view-model builders of the review UI (no sockets, no fs I/O)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from tidy.analysis import events
from tidy.review.views import duplicates as duplicate_views
from tidy.review.views import events as events_views
from tidy.review.views import series as series_views
from tidy.analysis import series
from tidy.media import thumbnails
from tidy.duplicates.keeper import Discard, KeeperDecision
from tidy.duplicates.tiers import DuplicateGroup

from .helpers import (
    _event,
    _event_member,
    _series_group,
    _series_member,
)


# --- pure view-model builder --------------------------------------------

class TestBuildGroupViews:
    def test_single_group_shape(self):
        group = DuplicateGroup(size=1000, full_hash="hash1", paths=("D:/A/a.jpg", "D:/A/b.jpg"))
        decision = KeeperDecision(
            keeper="D:/A/a.jpg",
            discarded=(Discard(path="D:/A/b.jpg", reason="dup-exact"),),
        )

        views, thumb_sources, file_registry = duplicate_views.build_group_views([group], [decision])

        assert len(views) == 1
        v = views[0]
        assert v["group_id"] == "1"
        assert v["size"] == 1000
        assert v["reclaimable_bytes"] == 1000
        assert v["thumb_id"] == "hash1"
        assert v["keeper"] == {"path": "D:/A/a.jpg", "id": "1:0", "size": 1000}
        assert v["discarded"] == [
            {"path": "D:/A/b.jpg", "reason": "dup-exact", "id": "1:1", "size": 1000},
        ]
        assert thumb_sources == {"hash1": "D:/A/a.jpg"}

        # file_registry is the allowlist POST /api/apply resolves ids
        # against -- mirrors thumb_sources, keyed by the stable file id.
        assert file_registry["1:0"] == {
            "path": "D:/A/a.jpg", "group_id": "1", "size": 1000, "hash": "hash1", "reason": None,
        }
        assert file_registry["1:1"] == {
            "path": "D:/A/b.jpg", "group_id": "1", "size": 1000, "hash": "hash1", "reason": "dup-exact",
        }

    def test_keeper_and_discarded_carry_per_file_size_matching_file_registry(self):
        # Every card (keeper and each discard) needs its own size shown in
        # the UI -- sourced from the same per-file value build_group_views()
        # already puts in file_registry[id]["size"] for every file id, not
        # some group-level-only field the view models previously omitted.
        group = DuplicateGroup(size=3 * 1024 * 1024, full_hash="hash1", paths=("D:/A/a.jpg", "D:/A/b.jpg"))
        decision = KeeperDecision(
            keeper="D:/A/a.jpg",
            discarded=(Discard(path="D:/A/b.jpg", reason="dup-exact"),),
        )
        views, _, file_registry = duplicate_views.build_group_views([group], [decision])
        v = views[0]
        assert v["keeper"]["size"] == file_registry[v["keeper"]["id"]]["size"] == 3 * 1024 * 1024
        assert v["discarded"][0]["size"] == file_registry[v["discarded"][0]["id"]]["size"] == 3 * 1024 * 1024

    def test_group_ids_are_1_indexed_and_in_order(self):
        groups = [
            DuplicateGroup(size=1, full_hash="h1", paths=("a", "b")),
            DuplicateGroup(size=2, full_hash="h2", paths=("c", "d")),
        ]
        decisions = [
            KeeperDecision(keeper="a", discarded=(Discard(path="b", reason="dup-exact"),)),
            KeeperDecision(keeper="c", discarded=(Discard(path="d", reason="rule-0"),)),
        ]
        views, _, file_registry = duplicate_views.build_group_views(groups, decisions)
        assert [v["group_id"] for v in views] == ["1", "2"]
        assert [v["keeper"]["id"] for v in views] == ["1:0", "2:0"]
        assert [v["discarded"][0]["id"] for v in views] == ["1:1", "2:1"]
        assert set(file_registry) == {"1:0", "1:1", "2:0", "2:1"}

    def test_non_image_non_video_extension_gets_no_thumb_id(self):
        # .arw (RAW) is neither an image thumbnails.py decodes nor a video
        # extension -- unlike .mp4 below, it stays unsupported regardless of
        # ffmpeg availability.
        group = DuplicateGroup(size=1000, full_hash="hash1", paths=("D:/A/a.arw", "D:/A/b.arw"))
        decision = KeeperDecision(keeper="D:/A/a.arw", discarded=(Discard(path="D:/A/b.arw", reason="dup-exact"),))

        views, thumb_sources, _ = duplicate_views.build_group_views([group], [decision])

        assert views[0]["thumb_id"] is None
        assert views[0]["thumb_kind"] is None
        assert thumb_sources == {}

    def test_image_extension_gets_thumb_kind_image(self):
        group = DuplicateGroup(size=1000, full_hash="hash1", paths=("D:/A/a.jpg", "D:/A/b.jpg"))
        decision = KeeperDecision(keeper="D:/A/a.jpg", discarded=(Discard(path="D:/A/b.jpg", reason="dup-exact"),))

        views, _, _ = duplicate_views.build_group_views([group], [decision])

        assert views[0]["thumb_kind"] == "image"

    def test_video_extension_gets_thumb_kind_video(self):
        # thumb_kind is extension-only (thumbnails.kind()) -- a
        # video always reports "video" here regardless of whether ffmpeg is
        # actually available, so the UI can show a video-specific
        # placeholder even when thumb_id ends up None.
        group = DuplicateGroup(size=1000, full_hash="hash1", paths=("D:/A/a.mp4", "D:/A/b.mp4"))
        decision = KeeperDecision(keeper="D:/A/a.mp4", discarded=(Discard(path="D:/A/b.mp4", reason="dup-exact"),))

        views, _, _ = duplicate_views.build_group_views([group], [decision])

        assert views[0]["thumb_kind"] == "video"

    def test_video_without_ffmpeg_has_thumb_kind_video_but_no_thumb_id(self, monkeypatch):
        monkeypatch.setattr(thumbnails, "_ffmpeg_exe", lambda: None)
        group = DuplicateGroup(size=1000, full_hash="hash1", paths=("D:/A/a.mp4", "D:/A/b.mp4"))
        decision = KeeperDecision(keeper="D:/A/a.mp4", discarded=(Discard(path="D:/A/b.mp4", reason="dup-exact"),))

        views, thumb_sources, _ = duplicate_views.build_group_views([group], [decision])

        assert views[0]["thumb_id"] is None
        assert views[0]["thumb_kind"] == "video"
        assert thumb_sources == {}

    def test_multiple_discards_all_listed(self):
        group = DuplicateGroup(size=5, full_hash="h", paths=("k", "d1", "d2"))
        decision = KeeperDecision(
            keeper="k",
            discarded=(
                Discard(path="d1", reason="rule-0"),
                Discard(path="d2", reason="dup-exact"),
            ),
        )
        views, _, file_registry = duplicate_views.build_group_views([group], [decision])
        assert views[0]["reclaimable_bytes"] == 10
        assert [d["path"] for d in views[0]["discarded"]] == ["d1", "d2"]
        assert [d["id"] for d in views[0]["discarded"]] == ["1:1", "1:2"]
        assert file_registry["1:1"]["path"] == "d1"
        assert file_registry["1:2"]["path"] == "d2"

    def test_no_groups_returns_empty(self):
        views, thumb_sources, file_registry = duplicate_views.build_group_views([], [])
        assert views == []
        assert thumb_sources == {}
        assert file_registry == {}

    def test_strips_long_path_prefix_for_display(self):
        group = DuplicateGroup(size=1, full_hash="h", paths=("\\\\?\\D:\\A\\a.jpg", "\\\\?\\D:\\A\\b.jpg"))
        decision = KeeperDecision(
            keeper="\\\\?\\D:\\A\\a.jpg",
            discarded=(Discard(path="\\\\?\\D:\\A\\b.jpg", reason="dup-exact"),),
        )
        views, thumb_sources, file_registry = duplicate_views.build_group_views([group], [decision])
        assert views[0]["keeper"]["path"] == "D:\\A\\a.jpg"
        assert views[0]["discarded"][0]["path"] == "D:\\A\\b.jpg"
        # thumb_sources keeps the real (prefixed) path -- it's fed straight
        # to fs.open_read()/ThumbnailStore, never shown to a human.
        assert thumb_sources["h"] == "\\\\?\\D:\\A\\a.jpg"
        # file_registry likewise keeps the real (prefixed) path -- it feeds
        # applier.apply_batch()/fs.move(), never shown to a human.
        assert file_registry["1:0"]["path"] == "\\\\?\\D:\\A\\a.jpg"
        assert file_registry["1:1"]["path"] == "\\\\?\\D:\\A\\b.jpg"


class TestBuildSeriesViews:
    def test_single_group_shape(self):
        m1 = _series_member("D:/A/a.jpg", size=100)
        m2 = _series_member("D:/A/b.jpg", size=200)
        group = _series_group([m1, m2], suggested_keeper="D:/A/a.jpg")
        result = series.SeriesResult(groups=[group])

        views, thumb_sources, series_registry = series_views.build_series_views(result)

        assert len(views) == 1
        v = views[0]
        assert v["group_id"] == "s1"
        assert v["kind"] == series.KIND_BURST
        assert v["max_gap_seconds"] == 2.0
        assert v["suggested_keeper_id"] == "s1:0"
        assert len(v["entries"]) == 2

        e0 = v["entries"][0]
        for key in ("path", "size", "id", "thumb_id", "thumb_kind",
                    "width", "height", "sharpness", "has_exif"):
            assert key in e0
        assert e0["path"] == "D:/A/a.jpg"
        assert e0["size"] == 100
        assert e0["id"] == "s1:0"
        assert e0["width"] == 100
        assert e0["height"] == 200
        assert e0["sharpness"] == 5.0
        assert e0["has_exif"] is True

        assert thumb_sources[e0["thumb_id"]] == "D:/A/a.jpg"
        assert series_registry["s1:0"] == {
            "path": "D:/A/a.jpg", "group_id": "s1", "size": 100,
            "hash": "", "reason": "dup-visual",
        }
        assert series_registry["s1:1"] == {
            "path": "D:/A/b.jpg", "group_id": "s1", "size": 200,
            "hash": "", "reason": "dup-visual",
        }

    def test_suggested_keeper_id_points_at_matching_entry(self):
        m1 = _series_member("D:/A/a.jpg")
        m2 = _series_member("D:/A/b.jpg")
        group = _series_group([m1, m2], suggested_keeper="D:/A/b.jpg")
        result = series.SeriesResult(groups=[group])

        views, _, _ = series_views.build_series_views(result)

        assert views[0]["suggested_keeper_id"] == "s1:1"

    def test_group_ids_are_1_indexed_with_s_prefix(self):
        g1 = _series_group([_series_member("a")], suggested_keeper="a")
        g2 = _series_group([_series_member("b")], suggested_keeper="b")
        result = series.SeriesResult(groups=[g1, g2])

        views, _, _ = series_views.build_series_views(result)

        assert [v["group_id"] for v in views] == ["s1", "s2"]
        assert views[0]["entries"][0]["id"] == "s1:0"
        assert views[1]["entries"][0]["id"] == "s2:0"

    def test_same_path_same_thumb_id_different_path_different_thumb_id(self):
        m1 = _series_member("D:/A/a.jpg")
        m2 = _series_member("D:/A/a.jpg")
        m3 = _series_member("D:/A/other.jpg")
        group = _series_group([m1, m2, m3], suggested_keeper="D:/A/a.jpg")
        result = series.SeriesResult(groups=[group])

        views, _, _ = series_views.build_series_views(result)

        entries = views[0]["entries"]
        assert entries[0]["thumb_id"] == entries[1]["thumb_id"]
        assert entries[0]["thumb_id"] != entries[2]["thumb_id"]

    def test_thumb_id_has_p_prefix_and_never_collides_with_group_hash(self):
        m1 = _series_member("D:/A/a.jpg")
        group = _series_group([m1], suggested_keeper="D:/A/a.jpg")
        result = series.SeriesResult(groups=[group])

        views, thumb_sources, _ = series_views.build_series_views(result)
        thumb_id = views[0]["entries"][0]["thumb_id"]

        assert thumb_id.startswith("p_")
        # A plain hex full_hash (as used by build_group_views()'s
        # duplicate-group ids, e.g. "hash1" or a real hex digest) never
        # carries the "p_" prefix -- the two id spaces sharing one
        # thumb_sources dict must never collide, and "p_" is exactly the
        # guard against that.
        hex_part = thumb_id[len("p_"):]
        assert not hex_part.startswith("p_")
        assert thumb_id != hex_part
        assert re.fullmatch(r"[0-9a-f]{32}", hex_part)

    def test_unsupported_extension_gets_no_thumb_id_but_has_thumb_kind(self):
        m1 = _series_member("D:/A/a.bin")
        group = _series_group([m1], suggested_keeper="D:/A/a.bin")
        result = series.SeriesResult(groups=[group])

        views, thumb_sources, _ = series_views.build_series_views(result)

        entry = views[0]["entries"][0]
        assert entry["thumb_id"] is None
        # .bin is neither image nor video -- thumb_kind is None too, unlike
        # the video-without-ffmpeg case where kind is still known.
        assert entry["thumb_kind"] is None
        assert thumb_sources == {}

    def test_empty_result_gives_empty_lists(self):
        result = series.SeriesResult(groups=[])
        views, thumb_sources, series_registry = series_views.build_series_views(result)
        assert views == []
        assert thumb_sources == {}
        assert series_registry == {}


class TestBuildEventsViews:
    def test_single_event_shape(self):
        m1 = _event_member("D:/A/a.jpg")
        m2 = _event_member("D:/A/b.mp4")
        event = _event([m1, m2])
        result = events.EventsResult(events=[event])
        sizes = {"D:/A/a.jpg": 111, "D:/A/b.mp4": 222}

        views, thumb_sources, open_registry = events_views.build_events_views(result, sizes)

        assert len(views) == 1
        v = views[0]
        assert v["group_id"] == "e1"
        assert v["suggested_folder"] == "#Home"
        assert v["matched_existing_dir"] is False
        assert v["place_name"] == "Krakow"
        assert v["country_cc"] == "PL"
        assert v["photo_count"] == 2
        assert v["video_count"] == 0
        assert len(v["entries"]) == 2

        e0 = v["entries"][0]
        for key in ("path", "size", "id", "thumb_id", "thumb_kind", "when",
                    "timestamp_source", "camera_model", "gps_source", "is_suspicious"):
            assert key in e0
        assert e0["id"] == "e1:0"
        assert e0["size"] == 111
        assert e0["when"] == datetime(2021, 4, 25, 1, 5, 18, tzinfo=timezone.utc).isoformat()
        assert isinstance(e0["when"], str)

        assert open_registry["e1:0"] == {"path": "D:/A/a.jpg", "group_id": "e1", "size": 111}
        assert open_registry["e1:1"] == {"path": "D:/A/b.mp4", "group_id": "e1", "size": 222}
        assert thumb_sources[e0["thumb_id"]] == "D:/A/a.jpg"

    def test_group_ids_use_e_prefix(self):
        event = _event([_event_member("a.jpg")])
        result = events.EventsResult(events=[event])
        views, _, _ = events_views.build_events_views(result, {})
        assert views[0]["group_id"] == "e1"
        assert views[0]["entries"][0]["id"] == "e1:0"

    def test_missing_path_in_sizes_by_path_defaults_to_zero(self):
        event = _event([_event_member("D:/A/missing.jpg")])
        result = events.EventsResult(events=[event])

        views, _, open_registry = events_views.build_events_views(result, {})

        assert views[0]["entries"][0]["size"] == 0
        assert open_registry["e1:0"]["size"] == 0

    def test_empty_result_gives_empty_lists(self):
        result = events.EventsResult(events=[])
        views, thumb_sources, open_registry = events_views.build_events_views(result, {})
        assert views == []
        assert thumb_sources == {}
        assert open_registry == {}


class TestBuildEventsNotes:
    def test_carries_suspicious_and_camera_drift_warnings(self):
        suspicious_member = _event_member(
            "D:/A/bad.jpg",
            when=datetime(1990, 1, 1, tzinfo=timezone.utc),
            camera_model="NIKON D3100",
            is_suspicious=True,
        )
        result = events.EventsResult(
            events=[],
            suspicious=[suspicious_member],
            camera_drift_warnings=["NIKON D3100: offset applied +2h15m"],
        )

        notes = events_views.build_events_notes(result)

        assert notes["suspicious"] == [{
            "path": "D:/A/bad.jpg",
            "when": datetime(1990, 1, 1, tzinfo=timezone.utc).isoformat(),
            "camera_model": "NIKON D3100",
        }]
        assert notes["camera_drift_warnings"] == ["NIKON D3100: offset applied +2h15m"]

    def test_empty_result_gives_empty_notes(self):
        result = events.EventsResult()
        notes = events_views.build_events_notes(result)
        assert notes == {"suspicious": [], "camera_drift_warnings": []}
