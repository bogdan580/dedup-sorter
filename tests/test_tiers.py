"""Tests for tiers.py: three-tier duplicate detection, cache-backed
resumability, and the pre-hashing confirmation gate.

All I/O goes through FakeFileSystem and a real (but tmp_path-scoped)
SQLite Cache -- never the real archive.
"""
from datetime import datetime, timezone

import pytest

from tidy import config
from tidy.duplicates import tiers
from tidy.store.cache import Cache
from fakes import FakeFileSystem
from tidy.duplicates.scanner import ScannedFile
from fakes import make_stat


def candidate(path, content, fs, **stat_overrides):
    stat_overrides.setdefault("volume_id", 1)
    stat_overrides.setdefault("file_index", abs(hash(path)) % (2 ** 31))
    fs.add_file(path, content, **stat_overrides)
    return ScannedFile(path=path, stat=fs.stat(path), is_duplicate_candidate=True)


def always_yes(message: str) -> bool:
    return True


def always_no(message: str) -> bool:
    return False


class TestEstimateWork:
    def test_unique_size_excluded_from_estimate(self):
        fs = FakeFileSystem()
        c1 = candidate("a.jpg", b"x" * 10, fs)
        c2 = candidate("b.jpg", b"y" * 20, fs)  # different size, unique
        est = tiers.estimate_work([c1, c2])
        assert est.file_count == 0
        assert est.total_bytes == 0

    def test_same_size_group_counted(self):
        fs = FakeFileSystem()
        c1 = candidate("a.jpg", b"x" * 10, fs)
        c2 = candidate("b.jpg", b"y" * 10, fs)
        c3 = candidate("c.jpg", b"z" * 999, fs)  # unique size, excluded
        est = tiers.estimate_work([c1, c2, c3])
        assert est.file_count == 2
        assert est.total_bytes == 20
        assert "2" in est.message


class TestConfirmationGate:
    def test_declining_skips_all_hashing(self, tmp_path):
        fs = FakeFileSystem()
        c1 = candidate("a.jpg", b"same", fs)
        c2 = candidate("b.jpg", b"same", fs)
        cache = Cache(str(tmp_path / "cache.sqlite3"))

        result = tiers.find_duplicates(fs, [c1, c2], cache, confirm=always_no)

        assert result.confirmed is False
        assert result.duplicate_groups == []
        assert fs.open_read_calls == []
        cache.close()

    def test_accepting_proceeds_with_hashing(self, tmp_path):
        fs = FakeFileSystem()
        c1 = candidate("a.jpg", b"same", fs)
        c2 = candidate("b.jpg", b"same", fs)
        cache = Cache(str(tmp_path / "cache.sqlite3"))

        result = tiers.find_duplicates(fs, [c1, c2], cache, confirm=always_yes)

        assert result.confirmed is True
        assert len(result.duplicate_groups) == 1
        cache.close()


class TestThreeTierDetection:
    def test_unique_size_dropped_at_tier1_no_read(self, tmp_path):
        fs = FakeFileSystem()
        c1 = candidate("a.jpg", b"x" * 10, fs)
        c2 = candidate("b.jpg", b"y" * 999, fs)
        cache = Cache(str(tmp_path / "cache.sqlite3"))

        result = tiers.find_duplicates(fs, [c1, c2], cache, confirm=always_yes)

        assert result.duplicate_groups == []
        assert result.tier1_unique_count == 2
        assert fs.open_read_calls == []
        cache.close()

    def test_same_size_different_head_tail_dropped_at_tier2(self, tmp_path):
        window = 16
        fs = FakeFileSystem()
        data_a = b"H" * window + b"middle-a" + b"T" * window
        data_b = b"X" * window + b"middle-b" + b"Y" * window
        assert len(data_a) == len(data_b)
        c1 = candidate("a.jpg", data_a, fs)
        c2 = candidate("b.jpg", data_b, fs)
        cache = Cache(str(tmp_path / "cache.sqlite3"))

        result = tiers.find_duplicates(
            fs, [c1, c2], cache, confirm=always_yes, window_bytes=window
        )

        assert result.duplicate_groups == []
        assert result.tier2_dropped_count == 2
        cache.close()

    def test_same_partial_hash_different_full_content_not_duplicate(self, tmp_path):
        window = 16
        fs = FakeFileSystem()
        head = b"H" * window
        tail = b"T" * window
        data_a = head + b"AAAA" * 50 + tail
        data_b = head + b"BBBB" * 50 + tail
        assert len(data_a) == len(data_b)
        c1 = candidate("a.jpg", data_a, fs)
        c2 = candidate("b.jpg", data_b, fs)
        cache = Cache(str(tmp_path / "cache.sqlite3"))

        result = tiers.find_duplicates(
            fs, [c1, c2], cache, confirm=always_yes, window_bytes=window
        )

        assert result.duplicate_groups == []
        assert result.tier3_not_duplicate_count == 2
        cache.close()

    def test_identical_files_confirmed_duplicate_group(self, tmp_path):
        fs = FakeFileSystem()
        c1 = candidate("a.jpg", b"identical-bytes", fs)
        c2 = candidate("b.jpg", b"identical-bytes", fs)
        cache = Cache(str(tmp_path / "cache.sqlite3"))

        result = tiers.find_duplicates(fs, [c1, c2], cache, confirm=always_yes)

        assert len(result.duplicate_groups) == 1
        group = result.duplicate_groups[0]
        assert set(group.paths) == {"a.jpg", "b.jpg"}
        cache.close()

    def test_small_files_reuse_tier2_hash_open_read_called_once_each(self, tmp_path):
        # Files well under 128KB: partial_hash already covers the whole file,
        # so tier-3 must reuse it instead of opening the file a second time.
        fs = FakeFileSystem()
        c1 = candidate("a.jpg", b"same-small-content", fs)
        c2 = candidate("b.jpg", b"same-small-content", fs)
        cache = Cache(str(tmp_path / "cache.sqlite3"))

        result = tiers.find_duplicates(fs, [c1, c2], cache, confirm=always_yes)

        assert len(result.duplicate_groups) == 1
        assert fs.open_read_calls.count("a.jpg") == 1
        assert fs.open_read_calls.count("b.jpg") == 1
        cache.close()


class TestResumability:
    def test_cache_hit_skips_open_read_on_second_run(self, tmp_path):
        fs = FakeFileSystem()
        c1 = candidate("a.jpg", b"same-bytes", fs)
        c2 = candidate("b.jpg", b"same-bytes", fs)
        db_path = str(tmp_path / "cache.sqlite3")

        cache1 = Cache(db_path)
        result1 = tiers.find_duplicates(fs, [c1, c2], cache1, confirm=always_yes)
        assert len(result1.duplicate_groups) == 1
        assert len(fs.open_read_calls) == 2
        cache1.close()

        fs.open_read_calls.clear()
        cache2 = Cache(db_path)
        result2 = tiers.find_duplicates(fs, [c1, c2], cache2, confirm=always_yes)

        assert len(result2.duplicate_groups) == 1
        assert fs.open_read_calls == []  # nothing changed -> no re-reads
        cache2.close()

    def test_changed_file_is_rehashed_unchanged_file_is_not(self, tmp_path):
        fs = FakeFileSystem()
        c1 = candidate("a.jpg", b"same-bytes", fs, mtime_ns=1)
        c2 = candidate("b.jpg", b"same-bytes", fs, mtime_ns=1)
        db_path = str(tmp_path / "cache.sqlite3")

        cache1 = Cache(db_path)
        tiers.find_duplicates(fs, [c1, c2], cache1, confirm=always_yes)
        cache1.close()

        # "a.jpg" changes on disk (new mtime + new content, same size so it
        # stays in the same tier-1 size group), "b.jpg" doesn't.
        assert len(b"diff-bytes") == len(b"same-bytes")
        fs.add_file("a.jpg", b"diff-bytes", volume_id=1, file_index=c1.stat.file_index, mtime_ns=2)
        c1_updated = ScannedFile(path="a.jpg", stat=fs.stat("a.jpg"), is_duplicate_candidate=True)

        fs.open_read_calls.clear()
        cache2 = Cache(db_path)
        tiers.find_duplicates(fs, [c1_updated, c2], cache2, confirm=always_yes)

        assert "a.jpg" in fs.open_read_calls
        assert "b.jpg" not in fs.open_read_calls
        cache2.close()

    def test_preserves_existing_dhash_when_writing_partial_and_full_hash(self, tmp_path):
        # Regression: series.py caches a dhash for a path via the
        # same Cache/--db. Cache.put() is INSERT OR REPLACE over every
        # column, so writing partial_hash/full_hash here for that same
        # path must not silently null the dhash column back out.
        fs = FakeFileSystem()
        c1 = candidate("a.jpg", b"same-bytes", fs)
        c2 = candidate("b.jpg", b"same-bytes", fs)
        db_path = str(tmp_path / "cache.sqlite3")

        cache = Cache(db_path)
        from tidy.store.cache import CachedFile
        cache.put(CachedFile(
            path="a.jpg", display_path="a.jpg", size=c1.stat.size, mtime_ns=c1.stat.mtime_ns,
            volume_id=c1.stat.volume_id, file_index=c1.stat.file_index, attributes=0,
            partial_hash=None, full_hash=None, dhash="00000000000001ff",
            status="ok", error=None, scanned_at="2026-01-01T00:00:00",
        ))
        cache.commit()

        tiers.find_duplicates(fs, [c1, c2], cache, confirm=always_yes)

        row = cache.get("a.jpg")
        assert row.dhash == "00000000000001ff"
        assert row.partial_hash is not None  # confirms the write path was actually exercised
        cache.close()


class TestErrorHandling:
    def test_open_read_error_recorded_and_scan_continues(self, tmp_path):
        fs = FakeFileSystem()
        c1 = candidate("a.jpg", b"same-bytes", fs)
        c2 = candidate("b.jpg", b"same-bytes", fs)
        c3 = candidate("c.jpg", b"other-size-content", fs)
        fs.set_open_error("a.jpg", PermissionError("locked by another process"))
        cache = Cache(str(tmp_path / "cache.sqlite3"))

        result = tiers.find_duplicates(fs, [c1, c2, c3], cache, confirm=always_yes)

        assert any(e.path == "a.jpg" for e in result.errors)
        # b.jpg alone (a.jpg errored out) is no longer part of a duplicate pair.
        assert result.duplicate_groups == []
        cache.close()
