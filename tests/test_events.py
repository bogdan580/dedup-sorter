"""Tests for events.py (time/GPS event splitting, offline place
naming, folder-name suggestion, camera clock-offset correction).

Pure-logic pieces (_split_by_time_gap, _split_by_gps_jump, _inherit_gps,
_suggest_top_level, parse_camera_offset) are tested directly against
hand-built data, mirroring test_series.py's convention. find_events()
itself is exercised end-to-end against FakeFileSystem with real
Pillow-encoded JPEG bytes carrying real (nested-IFD) EXIF DateTimeOriginal/
GPS/Model tags -- the same SubIFD lesson test_timeline.py/test_gps.py
fixtures already learned: a fixture that wrote these flat at the top level
would validate a bug, not the real structure real cameras produce.
"""
import io
import json
from datetime import datetime, timedelta

from PIL import ExifTags, Image

from tidy.analysis import events
from fakes import FakeFileSystem, make_stat
from tidy.progress import DEFAULT_WORKERS, ProgressCounter
from tidy.duplicates.scanner import ScannedFile


# --- helpers -----------------------------------------------------------------

def _scanned(path, *, mtime_ns=0):
    return ScannedFile(path=path, stat=make_stat(size=1000, mtime_ns=mtime_ns), is_duplicate_candidate=True)


def _jpeg_bytes(when=None, gps=None, camera_model=None, size=(20, 15)):
    """Builds a real JPEG with EXIF written the way a real camera/phone
    would: DateTimeOriginal and GPS in their real nested SubIFDs, Model at
    the flat IFD0 top level (verified empirically -- see events.py's
    _CAMERA_MODEL_TAG comment)."""
    buf = io.BytesIO()
    image = Image.new("RGB", size, (10, 20, 30))
    exif = Image.Exif()
    wrote_anything = False
    if when is not None:
        exif[ExifTags.IFD.Exif] = {36867: when}
        wrote_anything = True
    if gps is not None:
        lat, lon = gps
        exif[ExifTags.IFD.GPSInfo] = {
            1: "N" if lat >= 0 else "S", 2: (abs(lat), 0, 0.0),
            3: "E" if lon >= 0 else "W", 4: (abs(lon), 0, 0.0),
        }
        wrote_anything = True
    if camera_model is not None:
        exif[0x0110] = camera_model
        wrote_anything = True
    if wrote_anything:
        image.save(buf, "JPEG", exif=exif.tobytes())
    else:
        image.save(buf, "JPEG")
    return buf.getvalue()


def _entry(path, when, *, gps=None, suspicious=False, camera_model=None, source="exif"):
    return events._RawEntry(
        scanned=_scanned(path), when=when, timestamp_source=source,
        camera_model=camera_model, own_gps=gps, is_suspicious=suspicious,
    )


# --- _split_by_time_gap -------------------------------------------------------

class TestSplitByTimeGap:
    def test_entries_within_gap_form_one_group(self):
        a = _entry("a.jpg", datetime(2024, 1, 1, 10, 0))
        b = _entry("b.jpg", datetime(2024, 1, 1, 14, 0))  # 4h later
        groups = events._split_by_time_gap([a, b], gap_hours=8.0)
        assert len(groups) == 1
        assert [e.scanned.path for e in groups[0]] == ["a.jpg", "b.jpg"]

    def test_entries_beyond_gap_split_into_separate_groups(self):
        a = _entry("a.jpg", datetime(2024, 1, 1, 10, 0))
        b = _entry("b.jpg", datetime(2024, 1, 2, 20, 0))  # >8h later
        groups = events._split_by_time_gap([a, b], gap_hours=8.0)
        assert len(groups) == 2

    def test_single_entry_is_its_own_group(self):
        a = _entry("a.jpg", datetime(2024, 1, 1, 10, 0))
        groups = events._split_by_time_gap([a], gap_hours=8.0)
        assert len(groups) == 1
        assert len(groups[0]) == 1

    def test_empty_input_returns_no_groups(self):
        assert events._split_by_time_gap([], gap_hours=8.0) == []

    def test_long_chain_splits_at_every_gap_over_threshold(self):
        entries = [
            _entry("a.jpg", datetime(2024, 1, 1, 10, 0)),
            _entry("b.jpg", datetime(2024, 1, 1, 12, 0)),
            _entry("c.jpg", datetime(2024, 1, 2, 22, 0)),  # gap from b > 8h
            _entry("d.jpg", datetime(2024, 1, 2, 23, 0)),
        ]
        groups = events._split_by_time_gap(entries, gap_hours=8.0)
        assert [[e.scanned.path for e in g] for g in groups] == [["a.jpg", "b.jpg"], ["c.jpg", "d.jpg"]]


# --- _split_by_gps_jump --------------------------------------------------------

class TestSplitByGpsJump:
    def test_no_jump_keeps_one_group(self):
        entries = [
            _entry("a.jpg", datetime(2024, 1, 1, 10, 0), gps=(50.0, 19.0)),
            _entry("b.jpg", datetime(2024, 1, 1, 11, 0), gps=(50.01, 19.01)),
        ]
        groups = events._split_by_gps_jump(entries, jump_km=25.0)
        assert len(groups) == 1

    def test_large_jump_splits_into_two_groups(self):
        # Krakow vs Warsaw -- roughly 250km apart.
        entries = [
            _entry("a.jpg", datetime(2024, 1, 1, 10, 0), gps=(50.06, 19.94)),
            _entry("b.jpg", datetime(2024, 1, 1, 14, 0), gps=(52.23, 21.01)),
        ]
        groups = events._split_by_gps_jump(entries, jump_km=25.0)
        assert len(groups) == 2
        assert [e.scanned.path for e in groups[0]] == ["a.jpg"]
        assert [e.scanned.path for e in groups[1]] == ["b.jpg"]

    def test_no_gps_members_stay_in_one_group(self):
        entries = [
            _entry("a.jpg", datetime(2024, 1, 1, 10, 0)),
            _entry("b.jpg", datetime(2024, 1, 1, 11, 0)),
        ]
        assert len(events._split_by_gps_jump(entries, jump_km=25.0)) == 1

    def test_non_gps_member_between_two_gps_fixes_assigned_by_time_midpoint(self):
        entries = [
            _entry("a.jpg", datetime(2024, 1, 1, 10, 0), gps=(50.06, 19.94)),
            _entry("mid_before.jpg", datetime(2024, 1, 1, 11, 0)),  # closer to a
            _entry("mid_after.jpg", datetime(2024, 1, 1, 13, 0)),  # closer to b
            _entry("b.jpg", datetime(2024, 1, 1, 14, 0), gps=(52.23, 21.01)),
        ]
        groups = events._split_by_gps_jump(entries, jump_km=25.0)
        assert len(groups) == 2
        assert [e.scanned.path for e in groups[0]] == ["a.jpg", "mid_before.jpg"]
        assert [e.scanned.path for e in groups[1]] == ["mid_after.jpg", "b.jpg"]

    def test_tied_timestamp_donors_still_split(self):
        # Regression: two GPS-jump-worthy donors sharing the exact same
        # timestamp (e.g. both fell back to a coarse mtime, or a batch copy
        # stamped identical mtimes on files from two different places)
        # used to collapse the midpoint cut onto that single shared
        # instant, and a plain `>=` comparison put both tied donors in the
        # same bucket -- silently defeating the split for exactly the pair
        # that triggered it.
        same_time = datetime(2024, 1, 1, 10, 0)
        entries = [
            _entry("a.jpg", same_time, gps=(50.06, 19.94)),
            _entry("b.jpg", same_time, gps=(52.23, 21.01)),  # ~250km away, same instant
        ]
        groups = events._split_by_gps_jump(entries, jump_km=25.0)
        assert len(groups) == 2
        assert [e.scanned.path for e in groups[0]] == ["a.jpg"]
        assert [e.scanned.path for e in groups[1]] == ["b.jpg"]

    def test_suspicious_gps_member_is_not_used_as_a_jump_donor(self):
        entries = [
            _entry("a.jpg", datetime(2024, 1, 1, 10, 0), gps=(50.06, 19.94)),
            _entry("bad.jpg", datetime(1990, 1, 1, 10, 0), gps=(52.23, 21.01), suspicious=True),
            _entry("c.jpg", datetime(2024, 1, 1, 12, 0), gps=(50.07, 19.95)),
        ]
        groups = events._split_by_gps_jump(entries, jump_km=25.0)
        assert len(groups) == 1  # the suspicious jump is ignored entirely


# --- _inherit_gps --------------------------------------------------------------

class TestInheritGps:
    def test_own_gps_is_kept(self):
        entries = [_entry("a.jpg", datetime(2024, 1, 1), gps=(50.0, 19.0))]
        members = events._inherit_gps(entries)
        assert members[0].gps == (50.0, 19.0)
        assert members[0].gps_source == events.GPS_SOURCE_OWN

    def test_missing_gps_inherits_nearest_time_neighbor(self):
        entries = [
            _entry("a.jpg", datetime(2024, 1, 1, 10, 0), gps=(50.0, 19.0)),
            _entry("b.jpg", datetime(2024, 1, 1, 10, 1)),  # no own GPS, closer to a
            _entry("c.jpg", datetime(2024, 1, 1, 15, 0), gps=(51.0, 20.0)),
        ]
        members = events._inherit_gps(entries)
        by_path = {m.path: m for m in members}
        assert by_path["b.jpg"].gps == (50.0, 19.0)
        assert by_path["b.jpg"].gps_source == events.GPS_SOURCE_INHERITED

    def test_no_donors_leaves_gps_none(self):
        entries = [_entry("a.jpg", datetime(2024, 1, 1))]
        members = events._inherit_gps(entries)
        assert members[0].gps is None
        assert members[0].gps_source == events.GPS_SOURCE_NONE

    def test_suspicious_member_never_inherits_and_never_donates(self):
        entries = [
            _entry("bad.jpg", datetime(1990, 1, 1), suspicious=True),
            _entry("a.jpg", datetime(2024, 1, 1), gps=(50.0, 19.0)),
        ]
        members = events._inherit_gps(entries)
        by_path = {m.path: m for m in members}
        assert by_path["bad.jpg"].gps is None
        assert by_path["bad.jpg"].gps_source == events.GPS_SOURCE_SUSPICIOUS


# --- parse_camera_offset / load_camera_offsets --------------------------------

class TestParseCameraOffset:
    def test_hours_and_minutes(self):
        assert events.parse_camera_offset("+2h15m") == timedelta(hours=2, minutes=15)

    def test_negative_hours_only(self):
        assert events.parse_camera_offset("-1h") == timedelta(hours=-1)

    def test_minutes_only(self):
        assert events.parse_camera_offset("+45m") == timedelta(minutes=45)

    def test_invalid_format_raises(self):
        import pytest
        with pytest.raises(ValueError):
            events.parse_camera_offset("banana")


class TestLoadCameraOffsets:
    def test_loads_valid_offsets(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"camera_offsets": {"NIKON D3100": "+2h15m"}}), encoding="utf-8")

        offsets = events.load_camera_offsets(str(config_path))

        assert offsets == {"NIKON D3100": timedelta(hours=2, minutes=15)}

    def test_missing_file_returns_empty(self, tmp_path):
        assert events.load_camera_offsets(str(tmp_path / "missing.json")) == {}

    def test_none_path_returns_empty(self):
        assert events.load_camera_offsets(None) == {}

    def test_malformed_json_returns_empty(self, tmp_path, capsys):
        config_path = tmp_path / "config.json"
        config_path.write_text("{not valid json", encoding="utf-8")

        assert events.load_camera_offsets(str(config_path)) == {}
        assert "invalid JSON" in capsys.readouterr().out

    def test_bad_offset_value_is_skipped_with_warning(self, tmp_path, capsys):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"camera_offsets": {"NIKON D3100": "banana", "Canon EOS 550D": "-1h"}}),
            encoding="utf-8",
        )

        offsets = events.load_camera_offsets(str(config_path))

        assert offsets == {"Canon EOS 550D": timedelta(hours=-1)}
        assert "NIKON D3100" in capsys.readouterr().out

    def test_missing_camera_offsets_key_returns_empty(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"priority_dirs": ["#Home"]}), encoding="utf-8")

        assert events.load_camera_offsets(str(config_path)) == {}


# --- _suggest_top_level --------------------------------------------------------

class TestSuggestTopLevel:
    def test_matches_existing_dir_by_city_name(self):
        existing = {"#odesa": "#Odesa"}
        name, matched = events._suggest_top_level(existing, "Odesa", "UA")
        assert (name, matched) == ("#Odesa", True)

    def test_matches_existing_dir_by_country_display_name(self):
        existing = {"#poland": "#Poland"}
        name, matched = events._suggest_top_level(existing, "Krakow", "PL")
        assert (name, matched) == ("#Poland", True)

    def test_no_match_proposes_new_country_folder(self):
        name, matched = events._suggest_top_level({}, "Krakow", "PL")
        assert (name, matched) == ("#Poland", False)

    def test_unknown_country_code_falls_back_to_raw_code(self):
        name, matched = events._suggest_top_level({}, "Somewhere", "ZZ")
        assert (name, matched) == ("#ZZ", False)

    def test_no_place_at_all_falls_back_to_nieznane(self):
        assert events._suggest_top_level({}, None, None) == ("#Nieznane", False)


# --- find_events() end-to-end --------------------------------------------------

class TestFindEventsEndToEnd:
    def test_splits_two_far_apart_photos_into_separate_events(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/Archive")
        fs.add_file("D:/Archive/a.jpg", _jpeg_bytes(when="2024:01:01 10:00:00", gps=(50.06, 19.94)))
        fs.add_file("D:/Archive/b.jpg", _jpeg_bytes(when="2024:06:01 10:00:00", gps=(52.23, 21.01)))
        candidates = [_scanned("D:/Archive/a.jpg"), _scanned("D:/Archive/b.jpg")]

        result = events.find_events(fs, candidates, "D:/Archive", now=datetime(2024, 7, 1))

        assert len(result.events) == 2
        places = {e.place_name for e in result.events}
        assert "Krakow" in places
        assert "Warsaw" in places

    def test_prefers_existing_top_level_directory(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/Archive")
        fs.add_dir("D:/Archive/#Poland")
        fs.add_file("D:/Archive/a.jpg", _jpeg_bytes(when="2024:01:01 10:00:00", gps=(50.06, 19.94)))
        candidates = [_scanned("D:/Archive/a.jpg")]

        result = events.find_events(fs, candidates, "D:/Archive", now=datetime(2024, 7, 1))

        assert len(result.events) == 1
        assert result.events[0].suggested_folder.startswith("#Poland\\")
        assert result.events[0].matched_existing_dir is True

    def test_suspicious_date_reported_separately_and_excluded_from_donation(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/Archive")
        fs.add_file("D:/Archive/good.jpg", _jpeg_bytes(when="2024:01:01 10:00:00", gps=(50.06, 19.94)))
        fs.add_file("D:/Archive/bad.jpg", _jpeg_bytes(when="1990:01:01 10:00:00"))
        candidates = [_scanned("D:/Archive/good.jpg"), _scanned("D:/Archive/bad.jpg")]

        result = events.find_events(fs, candidates, "D:/Archive", now=datetime(2024, 7, 1))

        assert len(result.suspicious) == 1
        assert result.suspicious[0].path == "D:/Archive/bad.jpg"

    def test_camera_offset_moves_photo_into_correct_event(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/Archive")
        # Without correction this looks 3h earlier than its real burst-mates
        # (still within the same 8h event either way here) -- use a larger
        # gap to actually prove the offset changes which event a photo
        # lands in.
        fs.add_file(
            "D:/Archive/drifted.jpg",
            _jpeg_bytes(when="2024:01:01 04:00:00", camera_model="NIKON D3100"),
        )
        fs.add_file("D:/Archive/anchor.jpg", _jpeg_bytes(when="2024:01:01 18:00:00"))
        candidates = [_scanned("D:/Archive/drifted.jpg"), _scanned("D:/Archive/anchor.jpg")]
        offsets = {"NIKON D3100": timedelta(hours=9)}  # corrected: 2024-01-01 13:00 (5h before anchor)

        result = events.find_events(
            fs, candidates, "D:/Archive", now=datetime(2024, 7, 1), camera_offsets=offsets,
        )

        assert len(result.events) == 1  # corrected timestamp now falls within 8h of anchor

    def test_camera_drift_warning_for_unconfigured_model_with_suspicious_dates(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/Archive")
        fs.add_file(
            "D:/Archive/bad.jpg",
            _jpeg_bytes(when="1990:01:01 10:00:00", camera_model="NIKON D3100"),
        )
        candidates = [_scanned("D:/Archive/bad.jpg")]

        result = events.find_events(fs, candidates, "D:/Archive", now=datetime(2024, 7, 1))

        assert any("NIKON D3100" in w for w in result.camera_drift_warnings)

    def test_no_gps_at_all_still_produces_an_unnamed_event(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/Archive")
        fs.add_file("D:/Archive/a.jpg", _jpeg_bytes(when="2024:01:01 10:00:00"))
        candidates = [_scanned("D:/Archive/a.jpg")]

        result = events.find_events(fs, candidates, "D:/Archive", now=datetime(2024, 7, 1))

        assert len(result.events) == 1
        assert result.events[0].place_name is None
        assert "nieznane miejsce" in result.events[0].suggested_folder


class TestFindEventsParallelWorkers:
    """find_events()'s max_workers
    parallelizes _build_raw_entries()'s per-candidate metadata extraction
    via ThreadPoolExecutor, but must produce the same EventsResult as the
    sequential path (max_workers<=1) for the same input."""

    def _mixed_fs_and_candidates(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/Archive")
        fs.add_file("D:/Archive/a.jpg", _jpeg_bytes(when="2024:01:01 10:00:00", gps=(50.06, 19.94)))
        fs.add_file("D:/Archive/b.jpg", _jpeg_bytes(when="2024:01:01 11:00:00", gps=(50.07, 19.95)))
        fs.add_file("D:/Archive/c.jpg", _jpeg_bytes(when="2024:06:01 10:00:00", gps=(52.23, 21.01)))
        candidates = [
            _scanned("D:/Archive/a.jpg"), _scanned("D:/Archive/b.jpg"), _scanned("D:/Archive/c.jpg"),
        ]
        return fs, candidates

    def _result_shape(self, result: events.EventsResult):
        return [
            (e.place_name, e.country_cc, e.suggested_folder, e.matched_existing_dir,
             tuple(sorted(m.path for m in e.members)))
            for e in result.events
        ]

    def test_parallel_and_sequential_agree(self):
        fs, candidates = self._mixed_fs_and_candidates()

        sequential = events.find_events(
            fs, candidates, "D:/Archive", now=datetime(2024, 7, 1), max_workers=1,
        )
        parallel = events.find_events(
            fs, candidates, "D:/Archive", now=datetime(2024, 7, 1), max_workers=4,
        )

        assert self._result_shape(sequential) == self._result_shape(parallel)
        assert len(parallel.events) == 2  # sanity: fixture forms 2 events

    def test_progress_counter_reaches_done_equals_total(self):
        fs, candidates = self._mixed_fs_and_candidates()
        counter = ProgressCounter()

        events.find_events(
            fs, candidates, "D:/Archive", now=datetime(2024, 7, 1),
            max_workers=4, progress=counter,
        )

        assert counter.snapshot() == (3, 3)

    def test_default_max_workers_matches_progress_default_workers(self):
        import inspect
        sig = inspect.signature(events.find_events)
        assert sig.parameters["max_workers"].default == DEFAULT_WORKERS


# --- filter_new_locations ------------------------------------------------------

class TestFilterNewLocations:
    def _event(self, *, matched, when):
        member = events.EventMember(
            path="x.jpg", when=when, timestamp_source="exif", camera_model=None,
            gps=None, gps_source=events.GPS_SOURCE_NONE, is_suspicious=False,
        )
        return events.Event(
            members=(member,), place_name="X", country_cc="PL",
            suggested_folder="#X\\2024-01 X (1 zdjec, 0 filmow)",
            matched_existing_dir=matched, photo_count=1, video_count=0,
        )

    def test_excludes_events_matching_an_existing_folder(self):
        now = datetime(2024, 1, 10)
        result = events.filter_new_locations(
            [self._event(matched=True, when=datetime(2024, 1, 9))], timedelta(days=7), now=now,
        )
        assert result == []

    def test_excludes_events_older_than_since(self):
        now = datetime(2024, 1, 10)
        result = events.filter_new_locations(
            [self._event(matched=False, when=datetime(2023, 1, 1))], timedelta(days=7), now=now,
        )
        assert result == []

    def test_includes_recent_new_location_event(self):
        now = datetime(2024, 1, 10)
        event = self._event(matched=False, when=datetime(2024, 1, 9))
        result = events.filter_new_locations([event], timedelta(days=7), now=now)
        assert result == [event]


# --- CSV export + console summary ---------------------------------------------

class TestWriteEventsCsv:
    def test_writes_one_row_per_member(self, tmp_path):
        member = events.EventMember(
            path="D:/Archive/a.jpg", when=datetime(2024, 1, 1, 10, 0), timestamp_source="exif",
            camera_model="NIKON D3100", gps=(50.0, 19.0), gps_source=events.GPS_SOURCE_OWN,
            is_suspicious=False,
        )
        event = events.Event(
            members=(member,), place_name="Krakow", country_cc="PL",
            suggested_folder="#Home\\2024-01 Krakow (1 zdjec, 0 filmow)",
            matched_existing_dir=False, photo_count=1, video_count=0,
        )
        result = events.EventsResult(events=[event])
        out_path = tmp_path / "raport_wydarzenia.csv"

        events.write_events_csv(str(out_path), result)

        with open(out_path, "r", encoding="utf-8-sig", newline="") as f:
            import csv
            rows = list(csv.reader(f, delimiter=";"))
        assert rows[0] == list(events._CSV_COLUMNS)
        assert rows[1][3] == "D:/Archive/a.jpg"


class TestFormatEventsSummary:
    def test_includes_event_count_and_folder_suggestion(self):
        member = events.EventMember(
            path="a.jpg", when=datetime(2024, 1, 1), timestamp_source="exif",
            camera_model=None, gps=None, gps_source=events.GPS_SOURCE_NONE, is_suspicious=False,
        )
        event = events.Event(
            members=(member,), place_name=None, country_cc=None,
            suggested_folder="#Nieznane\\2024-01 (nieznane miejsce, 1 zdjec, 0 filmow)",
            matched_existing_dir=False, photo_count=1, video_count=0,
        )
        summary = events.format_events_summary(events.EventsResult(events=[event]))
        assert "Events: 1" in summary
        assert "#Nieznane" in summary
