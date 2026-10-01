"""Tests for dateindex.py (which existing archive folder
already holds photos from these days).

The scenario the whole module exists for is pinned in TestTheMountainsCase
below, with the real numbers measured off this archive: name-based
matching sends `!NEW\\Mountains 2021 trip` to a folder with the wrong year,
while the calendar sends it to the right one.
"""
from __future__ import annotations

from datetime import date, datetime

from tidy.analysis import dateindex
from tidy.store.cache import Cache
from fakes import FakeFileSystem
from tidy.duplicates.scanner import ScannedFile

ROOT = "D:/Archive"


def _mtime_for(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12, 0, 0).timestamp() * 1_000_000_000)


def _add(fs: FakeFileSystem, rel: str, day: date) -> ScannedFile:
    """A file whose only readable date is its mtime -- enough for this
    module, which asks timeline.py for a day and does not care which link
    of its chain produced it (that chain is test_timeline.py's subject)."""
    path = f"{ROOT}/{rel}"
    fs.add_file(path, b"not-an-image", mtime_ns=_mtime_for(day))
    return ScannedFile(path=path, stat=fs.stat(path), is_duplicate_candidate=True)


class TestRelativeDir:
    def test_directory_relative_to_the_archive_root(self):
        assert dateindex._relative_dir(
            "D:/Archive/#Home/2021 MOUNTAINS/a.jpg", ROOT,
        ) == "#Home\\2021 MOUNTAINS"

    def test_file_directly_in_the_root_has_no_folder(self):
        assert dateindex._relative_dir("D:/Archive/a.jpg", ROOT) == ""

    def test_path_outside_the_root_is_none(self):
        assert dateindex._relative_dir("D:/Elsewhere/a.jpg", ROOT) is None

    def test_long_path_prefixed_file_matches_a_plain_root(self):
        # The same asymmetry that silently emptied the placement screen:
        # scanned paths carry the prefix, archive_root does not.
        assert dateindex._relative_dir(
            "\\\\?\\D:\\Archive\\#Home\\2021 MOUNTAINS\\a.jpg", "D:\\Archive",
        ) == "#Home\\2021 MOUNTAINS"


class TestBuildFolderDates:
    def test_counts_files_per_day_per_folder(self):
        fs = FakeFileSystem()
        files = [
            _add(fs, "#Home/2021 MOUNTAINS/a.jpg", date(2021, 2, 14)),
            _add(fs, "#Home/2021 MOUNTAINS/b.jpg", date(2021, 2, 14)),
            _add(fs, "#Home/2021 MOUNTAINS/c.jpg", date(2021, 2, 15)),
            _add(fs, "#Home/Kielce/d.jpg", date(2020, 1, 1)),
        ]
        index = dateindex.build_folder_dates(fs, ROOT, files, max_workers=1)

        gory = index["#Home\\2021 MOUNTAINS"]
        assert gory.days == {date(2021, 2, 14): 2, date(2021, 2, 15): 1}
        assert gory.total == 3
        assert index["#Home\\Kielce"].total == 1

    def test_the_inbox_is_not_indexed(self):
        # !NEW holds the files being placed. Indexing it would offer the
        # user the folder they are trying to empty.
        fs = FakeFileSystem()
        files = [_add(fs, "!NEW/Camera/a.jpg", date(2021, 2, 14))]
        assert dateindex.build_folder_dates(fs, ROOT, files, max_workers=1) == {}

    def test_tool_directories_are_not_indexed(self):
        fs = FakeFileSystem()
        files = [
            _add(fs, "!REMOVE/2021/a.jpg", date(2021, 2, 14)),
            _add(fs, "!MOVES/2021/b.jpg", date(2021, 2, 14)),
        ]
        assert dateindex.build_folder_dates(fs, ROOT, files, max_workers=1) == {}


class TestExtractDaysCache:
    def test_second_run_reads_from_the_cache_instead_of_the_files(self, tmp_path):
        fs = FakeFileSystem()
        files = [_add(fs, "#Home/2021 MOUNTAINS/a.jpg", date(2021, 2, 14))]
        db = str(tmp_path / "cache.sqlite3")

        with Cache(db) as cache:
            first = dateindex.extract_days(fs, files, cache=cache, max_workers=1)

        # Reading is now refused outright: anything the second run returns
        # can only have come from the cache.
        fs.set_open_error(files[0].path, PermissionError("locked"))
        with Cache(db) as cache:
            second = dateindex.extract_days(fs, files, cache=cache, max_workers=1)

        assert second == first

    def test_a_changed_file_is_recomputed_not_trusted(self, tmp_path):
        fs = FakeFileSystem()
        files = [_add(fs, "#Home/2021 MOUNTAINS/a.jpg", date(2021, 2, 14))]
        db = str(tmp_path / "cache.sqlite3")
        with Cache(db) as cache:
            dateindex.extract_days(fs, files, cache=cache, max_workers=1)

        moved = [_add(fs, "#Home/2021 MOUNTAINS/a.jpg", date(2019, 7, 1))]
        with Cache(db) as cache:
            again = dateindex.extract_days(fs, moved, cache=cache, max_workers=1)

        assert again[moved[0].path] == date(2019, 7, 1)


class TestRankByDateOverlap:
    def _index(self, fs, layout):
        files = []
        for rel, days in layout.items():
            for i, day in enumerate(days):
                files.append(_add(fs, f"{rel}/f{i}.jpg", day))
        return dateindex.build_folder_dates(fs, ROOT, files, max_workers=1)

    def test_folder_covering_more_of_the_source_days_wins(self):
        fs = FakeFileSystem()
        index = self._index(fs, {
            "#Home/Both": [date(2021, 2, 14), date(2021, 2, 15)],
            "#Home/OneDayManyFiles": [date(2021, 2, 14)] * 20,
        })
        ranked = dateindex.rank_by_date_overlap(
            index, {date(2021, 2, 14), date(2021, 2, 15)}, min_overlap_files=1,
        )
        assert [m.rel for m in ranked][0] == "#Home\\Both"

    def test_a_neighbouring_day_still_counts(self):
        # Timezones and midnight: a photo an hour either side of the day
        # boundary belongs to the same trip.
        fs = FakeFileSystem()
        index = self._index(fs, {"#Home/Trip": [date(2021, 2, 16), date(2021, 2, 16)]})
        ranked = dateindex.rank_by_date_overlap(index, {date(2021, 2, 15)})
        assert [m.rel for m in ranked] == ["#Home\\Trip"]

    def test_a_distant_day_does_not_count(self):
        fs = FakeFileSystem()
        index = self._index(fs, {"#Home/Other": [date(2021, 6, 1), date(2021, 6, 2)]})
        assert dateindex.rank_by_date_overlap(index, {date(2021, 2, 15)}) == []

    def test_a_single_coincidental_overlap_is_not_reported(self):
        fs = FakeFileSystem()
        index = self._index(fs, {"#Home/Big": [date(2021, 2, 15)] + [date(2019, 1, 1)] * 50})
        assert dateindex.rank_by_date_overlap(index, {date(2021, 2, 15)}) == []

    def test_excluded_folder_is_never_offered(self):
        fs = FakeFileSystem()
        index = self._index(fs, {"#Home/Trip": [date(2021, 2, 15), date(2021, 2, 15)]})
        ranked = dateindex.rank_by_date_overlap(
            index, {date(2021, 2, 15)}, exclude={"#home\\trip"},
        )
        assert ranked == []

    def test_no_source_days_means_no_guessing(self):
        fs = FakeFileSystem()
        index = self._index(fs, {"#Home/Trip": [date(2021, 2, 15), date(2021, 2, 15)]})
        assert dateindex.rank_by_date_overlap(index, set()) == []

    def test_ordering_is_stable_between_identical_runs(self):
        # The list sits under the user's cursor and re-renders on every
        # poll tick; an unstable sort would reshuffle it mid-click.
        fs = FakeFileSystem()
        index = self._index(fs, {
            "#Home/A": [date(2021, 2, 15)] * 3,
            "#Home/B": [date(2021, 2, 15)] * 3,
            "#Home/C": [date(2021, 2, 15)] * 3,
        })
        first = [m.rel for m in dateindex.rank_by_date_overlap(index, {date(2021, 2, 15)})]
        second = [m.rel for m in dateindex.rank_by_date_overlap(index, {date(2021, 2, 15)})]
        assert first == second


class TestTheMountainsCase:
    """The case the user reported, with this archive's real shape.

    `!NEW\\Mountains 2021 trip` holds 23 files from 2021-02-14/15. Three
    folders carry a similar NAME; only one holds photos from those days,
    and it is none of them -- it is `#Home\\2021 MOUNTAINS`, which shares no
    word with the source at all."""

    def test_date_overlap_finds_the_folder_the_name_never_would(self):
        fs = FakeFileSystem()
        files = []
        for i in range(37):
            files.append(_add(fs, f"#Home/2021 MOUNTAINS/g{i}.jpg", date(2021, 2, 14)))
        for i in range(26):
            files.append(_add(fs, f"#Home/2021 MOUNTAINS/h{i}.jpg", date(2021, 2, 15)))
        # Same folder, unrelated contents -- an old trip, and a batch whose
        # only timestamp is the day it was copied. A min/max date RANGE
        # would be wrecked by these; a day histogram ignores them.
        for i in range(11):
            files.append(_add(fs, f"#Home/2021 MOUNTAINS/old{i}.jpg", date(2016, 1, 8)))
        for i in range(16):
            files.append(_add(fs, f"#Home/2021 MOUNTAINS/copied{i}.jpg", date(2026, 8, 16)))
        # The name-alike decoys, each from a different year.
        for i in range(221):
            files.append(_add(fs, f"#Home/2022 Mountains + spa/b{i}.jpg", date(2022, 1, 18)))
        for i in range(160):
            files.append(
                _add(fs, f"#Home/2020 Mountain Lodge/z{i}.jpg", date(2020, 2, 10))
            )

        index = dateindex.build_folder_dates(fs, ROOT, files, max_workers=1)
        source_days = {date(2021, 2, 14), date(2021, 2, 15)}
        ranked = dateindex.rank_by_date_overlap(index, source_days)

        assert [m.rel for m in ranked] == ["#Home\\2021 MOUNTAINS"]
        best = ranked[0]
        assert best.overlap_days == 2
        assert best.overlap_files == 63
        assert best.describe() == "already has 63 files from 2021-02-14..2021-02-15"
