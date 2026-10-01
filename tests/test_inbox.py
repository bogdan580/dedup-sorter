"""Tests for inbox.py (!NEW inbox triage -- dedup vs archive,
series within !NEW, and coherent/mixed folder placement classification).

Mirrors test_events.py/test_series.py's convention: pure-logic pieces
(_new_relative_segments, _group_new_files_by_top_folder, _classify_group)
tested directly on hand-built data; build_inbox_report() exercised
end-to-end against FakeFileSystem via a real scanner.scan() walk, exactly
like tests/cli/test_run_dedup.py's TestKeeperWiringInRun. Media files use plain
(non-image) bytes with a controlled mtime_ns: events.find_events()'s
timestamp extraction falls back to mtime for undecodable content
(timeline.py's EXIF->filename->mtime chain), so real Pillow-encoded
fixtures are only needed where GPS/camera-model extraction actually
matters -- not exercised here (see test_events.py for that coverage).
"""
from __future__ import annotations

from datetime import datetime

from tidy.analysis import inbox
from tidy.duplicates import keeper
from tidy.duplicates import tiers
from tidy.store.cache import Cache
from fakes import FakeFileSystem, make_stat
from tidy.progress import ProgressCounter
from tidy.duplicates.scanner import ScannedFile, scan


def _mtime_ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


def _scanned(path, *, mtime_ns=0, size=1000, is_duplicate_candidate=True):
    return ScannedFile(
        path=path, stat=make_stat(size=size, mtime_ns=mtime_ns),
        is_duplicate_candidate=is_duplicate_candidate,
    )


def _add_scanned(fs: FakeFileSystem, path: str, *, mtime_ns: int = 0) -> ScannedFile:
    """Register `path` with real (empty-image-incapable, i.e. plain) bytes
    in `fs` and return a matching ScannedFile -- needed wherever the
    ScannedFile actually flows into events.find_events()/timeline.py,
    which calls fs.open_read() to try (and, for this plain content, fail)
    EXIF extraction before falling back to mtime."""
    fs.add_file(path, b"plain-bytes-not-a-real-image", mtime_ns=mtime_ns)
    return ScannedFile(path=path, stat=fs.stat(path), is_duplicate_candidate=True)


# --- _new_relative_segments ---------------------------------------------------

class TestNewRelativeSegments:
    def test_path_under_new_subfolder(self):
        assert inbox._new_relative_segments("D:/Archive/!NEW/Camera/a.jpg") == ["Camera", "a.jpg"]

    def test_loose_file_directly_in_new(self):
        assert inbox._new_relative_segments("D:/Archive/!NEW/a.jpg") == ["a.jpg"]

    def test_no_new_segment_returns_none(self):
        assert inbox._new_relative_segments("D:/Archive/#Home/a.jpg") is None

    def test_case_insensitive_match(self):
        assert inbox._new_relative_segments("D:/Archive/!new/a.jpg") == ["a.jpg"]


class TestGroupByTopFolder:
    def test_groups_by_first_segment_after_new(self):
        files = [
            _scanned("D:/A/!NEW/Camera/a.jpg"),
            _scanned("D:/A/!NEW/Camera/b.jpg"),
            _scanned("D:/A/!NEW/Travel/c.jpg"),
        ]
        groups = inbox._group_new_files_by_top_folder(files)
        assert set(groups.keys()) == {"Camera", "Travel"}
        assert len(groups["Camera"]) == 2

    def test_loose_files_use_loose_key(self):
        files = [_scanned("D:/A/!NEW/loose.jpg")]
        groups = inbox._group_new_files_by_top_folder(files)
        assert list(groups.keys()) == [inbox.LOOSE_KEY]

    def test_non_new_paths_are_skipped(self):
        files = [_scanned("D:/A/#Home/a.jpg")]
        assert inbox._group_new_files_by_top_folder(files) == {}


class TestGroupFilesBySubfolder:
    """Unlike its sentinel-based predecessor
    this one compares a source path against file paths, so it is exposed to
    the one asymmetry that shipped the screen broken: WindowsFileSystem
    hands out long-path-prefixed paths, while `source_abs` is built from a
    plain archive-root string. The mismatch fails silently -- no exception,
    just zero groups -- and the screen then reports every subfolder as
    empty for a directory holding 1500 photos."""

    def test_groups_by_immediate_subfolder(self):
        files = [
            _scanned("D:/A/!NEW/Camera/a.jpg"),
            _scanned("D:/A/!NEW/Camera/b.jpg"),
            _scanned("D:/A/!NEW/Travel/c.jpg"),
            _scanned("D:/A/!NEW/loose.jpg"),
        ]
        groups = inbox.group_files_by_subfolder(files, "D:/A/!NEW")
        assert set(groups.keys()) == {"Camera", "Travel", inbox.LOOSE_KEY}
        assert len(groups["Camera"]) == 2

    def test_files_outside_the_source_are_skipped(self):
        files = [_scanned("D:/A/#Home/a.jpg")]
        assert inbox.group_files_by_subfolder(files, "D:/A/!NEW") == {}

    def test_extended_length_prefixed_files_match_a_plain_source(self):
        files = [
            _scanned("\\\\?\\D:\\A\\!NEW\\Camera\\a.jpg"),
            _scanned("\\\\?\\D:\\A\\!NEW\\loose.jpg"),
        ]
        groups = inbox.group_files_by_subfolder(files, "D:\\A\\!NEW")
        assert set(groups.keys()) == {"Camera", inbox.LOOSE_KEY}

    def test_plain_files_match_an_extended_length_prefixed_source(self):
        files = [_scanned("D:\\A\\!NEW\\Camera\\a.jpg")]
        groups = inbox.group_files_by_subfolder(files, "\\\\?\\D:\\A\\!NEW")
        assert set(groups.keys()) == {"Camera"}

    def test_grouped_files_keep_their_original_path(self):
        # The apply path moves ScannedFile.path verbatim, so normalising for
        # comparison must not leak into what comes back out.
        prefixed = "\\\\?\\D:\\A\\!NEW\\Camera\\a.jpg"
        groups = inbox.group_files_by_subfolder([_scanned(prefixed)], "D:\\A\\!NEW")
        assert groups["Camera"][0].path == prefixed


# --- _is_media -----------------------------------------------------------------

class TestIsMedia:
    def test_photo_extension_is_media(self):
        assert inbox._is_media("a.jpg") is True

    def test_video_extension_is_media(self):
        assert inbox._is_media("a.mp4") is True

    def test_raw_extension_is_not_media(self):
        assert inbox._is_media("a.arw") is False

    def test_text_file_is_not_media(self):
        assert inbox._is_media("notes.txt") is False


# --- _classify_group -----------------------------------------------------------

class TestClassifyGroup:
    def _fs(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/A")
        return fs

    def test_single_event_is_coherent(self):
        fs = self._fs()
        files = [
            _add_scanned(fs, "D:/A/!NEW/Gdansk/a.jpg", mtime_ns=_mtime_ns(datetime(2024, 6, 1, 10, 0))),
            _add_scanned(fs, "D:/A/!NEW/Gdansk/b.jpg", mtime_ns=_mtime_ns(datetime(2024, 6, 1, 12, 0))),
        ]
        classification, non_media, _events = inbox._classify_group(
            fs, "Gdansk", files, "D:/A", camera_offsets={}, event_gap_hours=8.0, gps_jump_km=25.0,
        )
        assert classification.classification == inbox.KIND_COHERENT
        assert len(classification.suggestions) == 1
        assert len(classification.suggestions[0].paths) == 2
        assert non_media == []

    def test_multiple_events_is_mixed_not_whole_folder(self):
        fs = self._fs()
        files = [
            _add_scanned(fs, "D:/A/!NEW/Camera/a.jpg", mtime_ns=_mtime_ns(datetime(2020, 1, 1, 10, 0))),
            _add_scanned(fs, "D:/A/!NEW/Camera/b.jpg", mtime_ns=_mtime_ns(datetime(2024, 6, 1, 10, 0))),
        ]
        classification, _, _events = inbox._classify_group(
            fs, "Camera", files, "D:/A", camera_offsets={}, event_gap_hours=8.0, gps_jump_km=25.0,
        )
        assert classification.classification == inbox.KIND_MIXED
        assert len(classification.suggestions) == 2
        all_paths = {p for s in classification.suggestions for p in s.paths}
        assert all_paths == {"D:/A/!NEW/Camera/a.jpg", "D:/A/!NEW/Camera/b.jpg"}

    def test_loose_files_are_never_a_whole_folder_suggestion(self):
        """Even when loose files form exactly one event, LOOSE_KEY must
        never be classified KIND_COHERENT -- there's no folder to move as
        a unit (loose files are always treated like a mixed
        folder's contents)."""
        fs = self._fs()
        files = [_add_scanned(fs, "D:/A/!NEW/a.jpg", mtime_ns=_mtime_ns(datetime(2024, 6, 1, 10, 0)))]
        classification, _, _events = inbox._classify_group(
            fs, inbox.LOOSE_KEY, files, "D:/A", camera_offsets={}, event_gap_hours=8.0, gps_jump_km=25.0,
        )
        assert classification.classification == inbox.KIND_LOOSE
        assert len(classification.suggestions) == 1

    def test_no_media_files_is_reported_without_suggestions(self):
        fs = self._fs()
        files = [_scanned("D:/A/!NEW/Camera/notes.txt")]
        classification, non_media, _events = inbox._classify_group(
            fs, "Camera", files, "D:/A", camera_offsets={}, event_gap_hours=8.0, gps_jump_km=25.0,
        )
        assert classification.classification == inbox.KIND_NO_MEDIA
        assert classification.suggestions == ()
        assert non_media == ["D:/A/!NEW/Camera/notes.txt"]

    def test_non_media_file_alongside_media_is_reported_separately(self):
        # Folder name is a meaningful one ("Gdansk"), not a dump name: this
        # test is about non-media routing, and "Camera" would now pin the
        # classification to "mixed" for an unrelated reason (dump-folder
        # name matching).
        fs = self._fs()
        files = [
            _add_scanned(fs, "D:/A/!NEW/Gdansk/a.jpg", mtime_ns=_mtime_ns(datetime(2024, 6, 1, 10, 0))),
            _scanned("D:/A/!NEW/Gdansk/readme.txt"),
        ]
        classification, non_media, _events = inbox._classify_group(
            fs, "Gdansk", files, "D:/A", camera_offsets={}, event_gap_hours=8.0, gps_jump_km=25.0,
        )
        assert classification.classification == inbox.KIND_COHERENT
        assert non_media == ["D:/A/!NEW/Gdansk/readme.txt"]


class TestDumpFolderNames:
    """A dump folder is never offered as one move, and its name
    is never used as a place name."""

    def _fs(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/A")
        fs.add_dir("D:/A/!NEW")
        return fs

    def _one_event_files(self, fs, folder):
        # Two photos minutes apart -- unambiguously a single event,
        # so the classification can only differ because of the folder NAME.
        return [
            _add_scanned(fs, f"D:/A/!NEW/{folder}/a.jpg", mtime_ns=_mtime_ns(datetime(2024, 6, 1, 10, 0))),
            _add_scanned(fs, f"D:/A/!NEW/{folder}/b.jpg", mtime_ns=_mtime_ns(datetime(2024, 6, 1, 10, 5))),
        ]

    def test_meaningful_folder_with_one_event_is_offered_as_a_whole(self):
        fs = self._fs()
        classification, _, _events = inbox._classify_group(
            fs, "Gdansk", self._one_event_files(fs, "Gdansk"), "D:/A",
            camera_offsets={}, event_gap_hours=8.0, gps_jump_km=25.0,
        )
        assert classification.classification == inbox.KIND_COHERENT

    def test_dump_folder_with_one_event_is_never_offered_as_a_whole(self):
        # Identical contents to the test above -- only the name differs.
        # This is the case the content-based check alone cannot catch: a
        # single weekend dumped off a phone really does form one event.
        fs = self._fs()
        classification, _, _events = inbox._classify_group(
            fs, "Baseus", self._one_event_files(fs, "Baseus"), "D:/A",
            camera_offsets={}, event_gap_hours=8.0, gps_jump_km=25.0,
        )
        assert classification.classification == inbox.KIND_MIXED
        assert len(classification.suggestions) == 1  # still suggested, just per-event

    def test_dump_name_matching_is_case_insensitive(self):
        fs = self._fs()
        classification, _, _events = inbox._classify_group(
            fs, "camera", self._one_event_files(fs, "camera"), "D:/A",
            camera_offsets={}, event_gap_hours=8.0, gps_jump_km=25.0,
        )
        assert classification.classification == inbox.KIND_MIXED

    def test_meaningful_folder_name_becomes_the_suggested_place_name(self):
        # The user's own label wins
        # over whatever the geocoder makes of the GPS (here: no GPS at all).
        fs = self._fs()
        classification, _, _events = inbox._classify_group(
            fs, "Gdansk", self._one_event_files(fs, "Gdansk"), "D:/A",
            camera_offsets={}, event_gap_hours=8.0, gps_jump_km=25.0,
        )
        assert classification.suggestions[0].suggested_folder.endswith("2024 Gdansk")

    def test_dump_folder_name_is_never_used_as_a_place_name(self):
        fs = self._fs()
        classification, _, _events = inbox._classify_group(
            fs, "Baseus", self._one_event_files(fs, "Baseus"), "D:/A",
            camera_offsets={}, event_gap_hours=8.0, gps_jump_km=25.0,
        )
        assert "Baseus" not in classification.suggestions[0].suggested_folder

    def test_custom_list_from_config_replaces_the_default(self):
        fs = self._fs()
        classification, _, _events = inbox._classify_group(
            fs, "Gdansk", self._one_event_files(fs, "Gdansk"), "D:/A",
            camera_offsets={}, event_gap_hours=8.0, gps_jump_km=25.0,
            dump_folder_names=("Gdansk",),
        )
        assert classification.classification == inbox.KIND_MIXED


class TestLoadDumpFolderNames:
    def test_missing_path_uses_defaults(self):
        assert inbox.load_dump_folder_names(None) == inbox.DEFAULT_DUMP_FOLDER_NAMES

    def test_missing_file_uses_defaults(self, tmp_path):
        missing = str(tmp_path / "nope.json")
        assert inbox.load_dump_folder_names(missing) == inbox.DEFAULT_DUMP_FOLDER_NAMES

    def test_list_from_config_is_used(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text('{"dump_folder_names": ["Zrzut", "Telefon"]}', encoding="utf-8")
        assert inbox.load_dump_folder_names(str(path)) == ("Zrzut", "Telefon")

    def test_wrong_type_falls_back_to_defaults_with_a_warning(self, tmp_path, capsys):
        path = tmp_path / "config.json"
        path.write_text('{"dump_folder_names": "Camera"}', encoding="utf-8")

        result = inbox.load_dump_folder_names(str(path))

        assert result == inbox.DEFAULT_DUMP_FOLDER_NAMES
        assert "dump_folder_names" in capsys.readouterr().out

    def test_malformed_json_falls_back_to_defaults_with_a_warning(self, tmp_path, capsys):
        path = tmp_path / "config.json"
        path.write_text("{not json", encoding="utf-8")

        result = inbox.load_dump_folder_names(str(path))

        assert result == inbox.DEFAULT_DUMP_FOLDER_NAMES
        assert "JSON" in capsys.readouterr().out


# --- build_inbox_report end-to-end ----------------------------------------------

class TestBuildInboxReportEndToEnd:
    def _build(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_dir("D:/A")
        fs.add_dir("D:/A/#Travel")
        fs.add_dir("D:/A/#Home")
        fs.add_dir("D:/A/!NEW")
        fs.add_dir("D:/A/!NEW/Travel")
        fs.add_dir("D:/A/!NEW/Camera")
        fs.add_dir("D:/A/!NEW/WillEmpty")
        fs.add_dir("D:/A/!NEW/Empty")

        # Rule-0 duplicate: same content in archive and !NEW.
        fs.add_file("D:/A/#Travel/dup.jpg", b"same-content-1", file_index=1,
                    mtime_ns=_mtime_ns(datetime(2022, 8, 30, 10, 0)))
        fs.add_file("D:/A/!NEW/Travel/dup.jpg", b"same-content-1", file_index=2,
                    mtime_ns=_mtime_ns(datetime(2022, 8, 30, 10, 0)))
        # Two unique files in the same event -> Travel should end up coherent.
        fs.add_file("D:/A/!NEW/Travel/unique1.jpg", b"unique-1", file_index=3,
                    mtime_ns=_mtime_ns(datetime(2022, 8, 30, 11, 0)))
        fs.add_file("D:/A/!NEW/Travel/unique2.jpg", b"unique-2", file_index=4,
                    mtime_ns=_mtime_ns(datetime(2022, 8, 30, 12, 0)))

        # Camera: two files 4+ years apart -> mixed.
        fs.add_file("D:/A/!NEW/Camera/old.jpg", b"old-content", file_index=5,
                    mtime_ns=_mtime_ns(datetime(2020, 1, 1, 10, 0)))
        fs.add_file("D:/A/!NEW/Camera/new.jpg", b"new-content", file_index=6,
                    mtime_ns=_mtime_ns(datetime(2024, 6, 1, 10, 0)))

        # WillEmpty: its only file duplicates an archive file -> folder
        # should end up in will_be_empty_after_dedup, not empty_folders
        # (it currently has a file; dedup would remove it).
        fs.add_file("D:/A/#Home/dup2.jpg", b"same-content-2", file_index=7,
                    mtime_ns=_mtime_ns(datetime(2024, 1, 1, 10, 0)))
        fs.add_file("D:/A/!NEW/WillEmpty/dup2.jpg", b"same-content-2", file_index=8,
                    mtime_ns=_mtime_ns(datetime(2024, 1, 1, 10, 0)))

        # Loose files directly in !NEW: one photo, one non-media.
        fs.add_file("D:/A/!NEW/loose.jpg", b"loose-content", file_index=9,
                    mtime_ns=_mtime_ns(datetime(2023, 5, 1, 10, 0)))
        fs.add_file("D:/A/!NEW/notes.txt", b"some notes", file_index=10,
                    mtime_ns=_mtime_ns(datetime(2023, 5, 1, 10, 0)))

        scan_result = scan(fs, ["D:/A"])
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            tier_result = tiers.find_duplicates(fs, scan_result.files, cache, confirm=lambda msg: True)
        finally:
            cache.close()
        mtimes = {f.path: f.stat.mtime_ns for f in scan_result.files}
        decisions = [keeper.select_keeper(g.paths, mtimes) for g in tier_result.duplicate_groups]

        report_cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            report = inbox.build_inbox_report(
                fs, scan_result, tier_result, decisions, "D:/A", "D:/A/!NEW", report_cache,
            )
        finally:
            report_cache.close()
        return report

    def test_rule_0_duplicate_reported_in_duplicates_section(self, tmp_path):
        report = self._build(tmp_path)
        assert len(report.duplicate_groups) == 2  # dup.jpg pair + dup2.jpg pair
        discarded_paths = {
            d.path for _, decision in report.duplicate_groups for d in decision.discarded
        }
        assert "D:/A/!NEW/Travel/dup.jpg" in discarded_paths
        assert "D:/A/!NEW/WillEmpty/dup2.jpg" in discarded_paths

    def test_slovakia_folder_is_coherent_after_dedup_removes_the_duplicate(self, tmp_path):
        report = self._build(tmp_path)
        slovakia = next(f for f in report.folders if f.name == "Travel")
        assert slovakia.classification == inbox.KIND_COHERENT
        paths = slovakia.suggestions[0].paths
        assert "D:/A/!NEW/Travel/dup.jpg" not in paths  # already removed by dedup
        assert set(paths) == {"D:/A/!NEW/Travel/unique1.jpg", "D:/A/!NEW/Travel/unique2.jpg"}

    def test_camera_folder_is_mixed(self, tmp_path):
        report = self._build(tmp_path)
        camera = next(f for f in report.folders if f.name == "Camera")
        assert camera.classification == inbox.KIND_MIXED
        assert len(camera.suggestions) == 2

    def test_loose_files_grouped_and_never_whole_folder(self, tmp_path):
        report = self._build(tmp_path)
        loose = next(f for f in report.folders if f.name == inbox.LOOSE_KEY)
        assert loose.classification == inbox.KIND_LOOSE
        assert "D:/A/!NEW/loose.jpg" in loose.suggestions[0].paths

    def test_non_media_file_is_skipped_not_suggested(self, tmp_path):
        report = self._build(tmp_path)
        assert "D:/A/!NEW/notes.txt" in report.skipped_non_media

    def test_already_empty_folder_is_reported(self, tmp_path):
        report = self._build(tmp_path)
        assert "Empty" in report.empty_folders
        assert "Empty" not in report.will_be_empty_after_dedup

    def test_folder_emptied_by_dedup_is_reported_separately(self, tmp_path):
        report = self._build(tmp_path)
        assert "WillEmpty" in report.will_be_empty_after_dedup
        assert "WillEmpty" not in report.empty_folders
        # And it must not appear as a folder classification -- nothing is
        # left in it to suggest a placement for.
        assert all(f.name != "WillEmpty" for f in report.folders)


# --- non-candidate files (zero-byte/offline/hardlink/inventory-only) -----------

class TestNonCandidateFilesUnderNew:
    """Regression coverage for a code-review HIGH finding: files
    scanner.py excludes from duplicate-candidacy entirely (inventory-only
    like Thumbs.db, zero-byte, offline, hardlink-of) must never be silently
    invisible from every section of the report, and must never cause a
    folder to be misreported as "will empty once you accept the dedup
    suggestions" when no dedup group actually touches it -- the original
    bug conflated "not a duplicate-candidate" with "removed by dedup"."""

    def _build(self, tmp_path, fs):
        scan_result = scan(fs, ["D:/A"])
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            tier_result = tiers.find_duplicates(fs, scan_result.files, cache, confirm=lambda msg: True)
        finally:
            cache.close()
        mtimes = {f.path: f.stat.mtime_ns for f in scan_result.files}
        decisions = [keeper.select_keeper(g.paths, mtimes) for g in tier_result.duplicate_groups]
        report_cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            return inbox.build_inbox_report(
                fs, scan_result, tier_result, decisions, "D:/A", "D:/A/!NEW", report_cache,
            )
        finally:
            report_cache.close()

    def test_inventory_only_file_alone_in_folder_is_not_a_false_will_be_empty(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_dir("D:/A")
        fs.add_dir("D:/A/!NEW")
        fs.add_dir("D:/A/!NEW/JunkOnly")
        fs.add_file("D:/A/!NEW/JunkOnly/Thumbs.db", b"junk", file_index=1)

        report = self._build(tmp_path, fs)

        assert "JunkOnly" not in report.empty_folders
        assert "JunkOnly" not in report.will_be_empty_after_dedup
        assert "D:/A/!NEW/JunkOnly/Thumbs.db" in report.skipped_non_media
        assert all(f.name != "JunkOnly" for f in report.folders)

    def test_zero_byte_file_alone_in_folder_is_not_a_false_will_be_empty(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_dir("D:/A")
        fs.add_dir("D:/A/!NEW")
        fs.add_dir("D:/A/!NEW/ZeroOnly")
        fs.add_file("D:/A/!NEW/ZeroOnly/broken.jpg", b"", file_index=1)

        report = self._build(tmp_path, fs)

        assert "ZeroOnly" not in report.empty_folders
        assert "ZeroOnly" not in report.will_be_empty_after_dedup
        assert "D:/A/!NEW/ZeroOnly/broken.jpg" in report.skipped_non_media


# --- --min-size/--ext narrowing (mirrors --series/--events) ---------------------

class TestFiltersNarrowSeriesAndPlacement:
    """Regression coverage for a code-review MEDIUM finding: --min-size/
    --ext must narrow the series/placement candidate pool the same way
    they already narrow tier_result's own duplicate detection, not just
    the duplicates section of the report."""

    def _report_for(self, tmp_path, fs, **kwargs):
        scan_result = scan(fs, ["D:/A"])
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            tier_result = tiers.find_duplicates(fs, scan_result.files, cache, confirm=lambda msg: True)
        finally:
            cache.close()
        report_cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            return inbox.build_inbox_report(
                fs, scan_result, tier_result, [], "D:/A", "D:/A/!NEW", report_cache, **kwargs,
            )
        finally:
            report_cache.close()

    def test_extensions_casefold_excludes_files_from_classification(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_dir("D:/A")
        fs.add_dir("D:/A/!NEW")
        fs.add_dir("D:/A/!NEW/Trip")
        fs.add_file("D:/A/!NEW/Trip/a.jpg", b"plain-a", mtime_ns=1)
        fs.add_file("D:/A/!NEW/Trip/b.mp4", b"plain-b", mtime_ns=2)

        report = self._report_for(tmp_path, fs, extensions_casefold=frozenset({".jpg"}))

        trip = next(f for f in report.folders if f.name == "Trip")
        all_paths = {p for s in trip.suggestions for p in s.paths}
        assert all_paths == {"D:/A/!NEW/Trip/a.jpg"}

    def test_min_size_excludes_small_files_from_classification(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_dir("D:/A")
        fs.add_dir("D:/A/!NEW")
        fs.add_dir("D:/A/!NEW/Trip")
        fs.add_file("D:/A/!NEW/Trip/big.jpg", b"x" * 2000, mtime_ns=1)
        fs.add_file("D:/A/!NEW/Trip/small.jpg", b"x" * 10, mtime_ns=2)

        report = self._report_for(tmp_path, fs, min_size=1000)

        trip = next(f for f in report.folders if f.name == "Trip")
        all_paths = {p for s in trip.suggestions for p in s.paths}
        assert all_paths == {"D:/A/!NEW/Trip/big.jpg"}

    def test_filters_never_affect_empty_folder_detection(self, tmp_path):
        """A file excluded from placement by --ext must still count as
        "the folder isn't empty" -- these are two independent concerns."""
        fs = FakeFileSystem()
        fs.add_dir("D:/A")
        fs.add_dir("D:/A/!NEW")
        fs.add_dir("D:/A/!NEW/Trip")
        fs.add_file("D:/A/!NEW/Trip/a.mp4", b"plain-a", mtime_ns=1)

        report = self._report_for(tmp_path, fs, extensions_casefold=frozenset({".jpg"}))

        assert "Trip" not in report.empty_folders
        assert "Trip" not in report.will_be_empty_after_dedup


# --- HTML report + console summary ---------------------------------------------

class TestWriteInboxHtml:
    def test_writes_three_sections_and_escapes_html(self, tmp_path):
        group = tiers.DuplicateGroup(size=10, full_hash="abc", paths=("a.jpg", "b.jpg"))
        decision = keeper.select_keeper(["a.jpg", "b.jpg"], {"a.jpg": 0, "b.jpg": 0})
        report = inbox.InboxReport(
            duplicate_groups=[(group, decision)],
            skipped_non_media=["<script>evil</script>.txt"],
        )
        out_path = str(tmp_path / "raport.html")
        inbox.write_inbox_html(out_path, report)

        content = open(out_path, "r", encoding="utf-8").read()
        assert "<h2>1. Duplicates" in content
        assert "<h2>2. Serie" in content
        assert "<h2>3. Grouping suggestions" in content
        assert "<script>evil</script>" not in content
        assert "&lt;script&gt;" in content


class TestClassifySourceProgress:
    """The progress bar on "Sugestie
    grupowania": the review screen's counter, fed folder by folder. The
    property that matters is that the total is declared ONCE for the whole
    source directory and ends exactly full -- a bar that re-scales per
    subfolder, or stops at 40/120, is worse than no bar."""

    def _fs_with_two_folders(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/A")
        fs.add_dir("D:/A/!NEW")
        fs.add_dir("D:/A/!NEW/Trip")
        fs.add_dir("D:/A/!NEW/Camera")
        files = [
            _add_scanned(fs, "D:/A/!NEW/Trip/a.jpg", mtime_ns=_mtime_ns(datetime(2024, 5, 1, 10, 0))),
            _add_scanned(fs, "D:/A/!NEW/Trip/b.jpg", mtime_ns=_mtime_ns(datetime(2024, 5, 1, 10, 5))),
            _add_scanned(fs, "D:/A/!NEW/Camera/c.jpg", mtime_ns=_mtime_ns(datetime(2024, 8, 3, 9, 0))),
        ]
        return fs, files

    def test_total_is_the_whole_directory_and_ends_full(self):
        fs, files = self._fs_with_two_folders()
        counter = ProgressCounter()

        inbox.classify_source(fs, "D:/A/!NEW", files, "D:/A", progress=counter)

        assert counter.snapshot() == (3, 3)

    def test_total_is_declared_before_any_folder_is_decoded(self, monkeypatch):
        """The whole point of declaring up front: the first poll the browser
        makes already knows how big the job is, so the bar does not grow
        under the user while folders are processed."""
        fs, files = self._fs_with_two_folders()
        counter = ProgressCounter()
        totals_seen = []

        original = inbox.events_module.find_events

        def spy(*args, **kwargs):
            totals_seen.append(counter.snapshot()[1])
            return original(*args, **kwargs)

        monkeypatch.setattr(inbox.events_module, "find_events", spy)
        inbox.classify_source(fs, "D:/A/!NEW", files, "D:/A", progress=counter)

        assert totals_seen == [3, 3]

    def test_non_media_files_are_not_counted(self):
        # They are never opened (no event detection runs on them), so
        # counting them would leave the bar permanently short of its total.
        fs, files = self._fs_with_two_folders()
        fs.add_file("D:/A/!NEW/Trip/notes.txt", b"hello", mtime_ns=1)
        files.append(_scanned("D:/A/!NEW/Trip/notes.txt"))
        counter = ProgressCounter()

        inbox.classify_source(fs, "D:/A/!NEW", files, "D:/A", progress=counter)

        assert counter.snapshot() == (3, 3)

    def test_no_progress_argument_still_works(self):
        fs, files = self._fs_with_two_folders()
        classifications, non_media, notes = inbox.classify_source(
            fs, "D:/A/!NEW", files, "D:/A",
        )
        assert {c.name for c in classifications} == {"Trip", "Camera"}


class TestFormatInboxSummary:
    def test_summary_mentions_counts(self):
        report = inbox.InboxReport()
        text = inbox.format_inbox_summary(report)
        assert "!NEW inbox" in text
        assert "Duplicate groups involving !NEW: 0" in text
