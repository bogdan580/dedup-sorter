"""Tests for cache.py: SQLite cache schema, algo-invalidation on open, and
batched/committed writes for resumability.

All databases in this file live under pytest's `tmp_path` fixture (an
OS-managed temp directory), never inside the real archive.
"""
import sqlite3

import pytest

from tidy import config
from tidy.store.cache import Cache, CachedFile


def make_file(path="C:/fake/a.jpg", **overrides):
    defaults = dict(
        path=path,
        display_path=path,
        size=100,
        mtime_ns=1_000,
        volume_id=1,
        file_index=10,
        attributes=0,
        partial_hash="partial-abc",
        full_hash="full-abc",
        dhash=None,
        status="ok",
        error=None,
        scanned_at="2026-07-26T00:00:00",
    )
    defaults.update(overrides)
    return CachedFile(**defaults)


class TestSchema:
    def test_creates_missing_parent_directory(self, tmp_path):
        """The cache lives in `.dedup\\.db\\`, which is not in git and
        which the user may delete to reclaim space. sqlite3 creates the
        database file but never its parent directory, so Cache() has to --
        otherwise the first run after a fresh clone dies with "unable to
        open database file"."""
        db_path = str(tmp_path / ".db" / "cache.sqlite3")
        cache = Cache(db_path)
        cache.close()

        assert (tmp_path / ".db" / "cache.sqlite3").is_file()

    def test_meta_and_files_tables_exist(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path)
        cache.close()

        conn = sqlite3.connect(db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "meta" in tables
            assert "files" in tables

            meta_cols = {row[1] for row in conn.execute("PRAGMA table_info(meta)")}
            assert meta_cols >= {"id", "schema_version", "hash_algo", "partial_algo", "dhash_algo"}

            files_cols = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
            assert files_cols >= {
                "path", "display_path", "size", "mtime_ns", "volume_id",
                "file_index", "attributes", "partial_hash", "full_hash",
                "dhash", "status", "error", "scanned_at",
            }
        finally:
            conn.close()

    def test_indexes_on_size_volume_file_index_and_status(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path)
        cache.close()

        conn = sqlite3.connect(db_path)
        try:
            index_sql = " ".join(
                (row[0] or "")
                for row in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index'"
                )
            )
            assert "size" in index_sql
            assert "volume_id" in index_sql and "file_index" in index_sql
            assert "status" in index_sql
        finally:
            conn.close()


class TestPutAndGet:
    def test_roundtrip(self, tmp_path):
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        f = make_file()
        cache.put(f)
        cache.commit()

        got = cache.get(f.path)
        assert got is not None
        assert got.path == f.path
        assert got.size == f.size
        assert got.mtime_ns == f.mtime_ns
        assert got.partial_hash == f.partial_hash
        assert got.full_hash == f.full_hash
        assert got.status == "ok"
        cache.close()

    def test_get_missing_path_returns_none(self, tmp_path):
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        assert cache.get("does/not/exist.jpg") is None
        cache.close()

    def test_put_replaces_existing_row_for_same_path(self, tmp_path):
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        cache.put(make_file(size=100))
        cache.put(make_file(size=200))
        cache.commit()
        got = cache.get("C:/fake/a.jpg")
        assert got.size == 200
        cache.close()


class TestCacheHitRule:
    def test_hit_requires_size_and_mtime_match(self, tmp_path):
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        cache.put(make_file(size=100, mtime_ns=1000))
        cache.commit()

        assert cache.is_hit("C:/fake/a.jpg", size=100, mtime_ns=1000) is True
        assert cache.is_hit("C:/fake/a.jpg", size=999, mtime_ns=1000) is False
        assert cache.is_hit("C:/fake/a.jpg", size=100, mtime_ns=999) is False
        assert cache.is_hit("C:/fake/a.jpg", size=999, mtime_ns=999) is False
        cache.close()

    def test_hit_false_for_unknown_path(self, tmp_path):
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        assert cache.is_hit("nope.jpg", size=1, mtime_ns=1) is False
        cache.close()


class TestAlgoInvalidationOnOpen:
    def test_unchanged_algos_preserve_hashes(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        c1 = Cache(db_path, hash_algo="blake2b", partial_algo="blake2b")
        c1.put(make_file())
        c1.close()

        c2 = Cache(db_path, hash_algo="blake2b", partial_algo="blake2b")
        got = c2.get("C:/fake/a.jpg")
        assert got.partial_hash == "partial-abc"
        assert got.full_hash == "full-abc"
        c2.close()

    def test_changing_hash_algo_nulls_full_hash_only(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        c1 = Cache(db_path, hash_algo="blake2b", partial_algo="blake2b")
        c1.put(make_file())
        c1.close()

        c2 = Cache(db_path, hash_algo="blake3-hypothetical", partial_algo="blake2b")
        got = c2.get("C:/fake/a.jpg")
        assert got.full_hash is None
        assert got.partial_hash == "partial-abc"
        c2.close()

    def test_changing_partial_algo_nulls_partial_and_full_hash(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        c1 = Cache(db_path, hash_algo="blake2b", partial_algo="blake2b")
        c1.put(make_file())
        c1.close()

        c2 = Cache(db_path, hash_algo="blake2b", partial_algo="crc32-hypothetical")
        got = c2.get("C:/fake/a.jpg")
        assert got.partial_hash is None
        assert got.full_hash is None
        c2.close()


class TestBatchedWritesResumability:
    def test_commits_periodically_by_batch_size(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path, batch_size=2)
        cache.put(make_file(path="a"))
        cache.put(make_file(path="b"))  # 2nd write should trigger auto-commit

        # A second, independent connection should already see the committed
        # rows without the first cache object calling close()/commit().
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT path FROM files").fetchall()
            assert {r[0] for r in rows} == {"a", "b"}
        finally:
            conn.close()
        cache.close()

    def test_uncommitted_tail_is_not_visible_until_committed(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path, batch_size=10)  # large batch: won't auto-commit yet
        cache.put(make_file(path="only-pending"))

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT path FROM files").fetchall()
            assert rows == []  # not committed yet, invisible to other connections
        finally:
            conn.close()
        cache.close()  # close() must flush remaining pending writes

        conn2 = sqlite3.connect(db_path)
        try:
            rows = conn2.execute("SELECT path FROM files").fetchall()
            assert {r[0] for r in rows} == {"only-pending"}
        finally:
            conn2.close()

    def test_interrupted_batch_survives_reopen_without_redoing_committed_work(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path, batch_size=2)
        cache.put(make_file(path="a"))
        cache.put(make_file(path="b"))  # committed (batch of 2)
        cache.put(make_file(path="c"))  # pending, uncommitted

        # Simulate a hard interruption (process killed before commit/close):
        # drop the raw connection without calling our own commit()/close(),
        # which triggers an implicit ROLLBACK of the pending transaction and
        # releases the file lock -- exactly what happens when a process dies
        # mid-transaction. A clean close()/commit() would defeat the point
        # of this test (it would just commit "c" instead of losing it).
        cache._conn.close()

        cache2 = Cache(db_path, batch_size=2)
        assert cache2.get("a") is not None
        assert cache2.get("b") is not None
        assert cache2.get("c") is None  # uncommitted work must be redone

        # Resuming: redo "c" through the second (post-restart) cache handle.
        cache2.put(make_file(path="c"))
        cache2.close()

        cache3 = Cache(db_path, batch_size=2)
        assert cache3.get("c") is not None
        cache3.close()


class TestSeriesSkips:
    """The review UI's persisted "pomin ta grupe" decisions.
    What makes this table worth its own class: it is the only
    thing in the cache that is a HUMAN decision rather than a derived value,
    so the invalidation rules the rest of this file pins must NOT apply to
    it."""

    def test_round_trip_through_a_reopen(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path)
        cache.add_series_skip("k1", ("d:/a/1.jpg", "d:/a/2.jpg"), "2026-08-19T10:00:00+00:00")
        cache.close()

        # A NEW process (a fresh Cache over the same file) must see it --
        # this is the whole user-visible promise: "nawet przy nastepnym
        # uruchomieniu".
        cache2 = Cache(db_path)
        assert cache2.list_series_skips() == [("k1", ("d:/a/1.jpg", "d:/a/2.jpg"))]
        cache2.close()

    def test_add_is_idempotent_and_refreshes_the_timestamp(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        with Cache(db_path) as cache:
            cache.add_series_skip("k1", ("d:/a/1.jpg",), "2026-08-19T10:00:00+00:00")
            cache.add_series_skip("k1", ("d:/a/1.jpg",), "2026-08-20T11:00:00+00:00")
            assert len(cache.list_series_skips()) == 1
            row = cache._conn.execute(
                "SELECT skipped_at, member_count FROM series_skips WHERE group_key = 'k1'"
            ).fetchone()
            assert row["skipped_at"] == "2026-08-20T11:00:00+00:00"
            assert row["member_count"] == 1

    def test_remove_deletes_only_the_named_keys(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        with Cache(db_path) as cache:
            cache.add_series_skip("k1", ("d:/a/1.jpg",), "2026-08-19T10:00:00+00:00")
            cache.add_series_skip("k2", ("d:/b/1.jpg",), "2026-08-19T10:00:00+00:00")
            assert cache.remove_series_skips(["k1"]) == 1
            assert [key for key, _ in cache.list_series_skips()] == ["k2"]

    def test_remove_with_an_empty_list_is_a_no_op(self, tmp_path):
        with Cache(str(tmp_path / "cache.sqlite3")) as cache:
            cache.add_series_skip("k1", ("d:/a/1.jpg",), "2026-08-19T10:00:00+00:00")
            assert cache.remove_series_skips([]) == 0
            assert len(cache.list_series_skips()) == 1

    def test_survives_a_dhash_algo_change_that_wipes_every_dhash(self, tmp_path):
        # The decision is about PHOTOS, not about a fingerprint: changing the
        # perceptual-hash algorithm re-derives the groups (and correctly
        # nulls the dhash column), but "these are fine, stop showing me
        # them" is not invalidated by it. A skip lost here would silently
        # re-surface every group the user ever dismissed.
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path, dhash_algo="dhash8")
        cache.put(make_file(path="a", dhash="ff00"))
        cache.add_series_skip("k1", ("d:/a/1.jpg",), "2026-08-19T10:00:00+00:00")
        cache.close()

        cache2 = Cache(db_path, dhash_algo="dhash16")
        assert cache2.get("a").dhash is None  # the derived value IS invalidated
        assert cache2.list_series_skips() == [("k1", ("d:/a/1.jpg",))]
        cache2.close()

    def test_survives_a_schema_version_bump(self, tmp_path):
        # A schema bump is the most aggressive invalidation this class does
        # (it nulls partial_hash/full_hash/dhash at once) and still must not
        # touch a human decision.
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path, schema_version=1)
        cache.add_series_skip("k1", ("d:/a/1.jpg",), "2026-08-19T10:00:00+00:00")
        cache.close()

        cache2 = Cache(db_path, schema_version=2)
        assert cache2.list_series_skips() == [("k1", ("d:/a/1.jpg",))]
        cache2.close()

    def test_table_is_created_on_a_database_that_predates_it(self, tmp_path):
        # `_ensure_schema()` returns early when `files` already exists, so a
        # pre-existing database (every user who ran this tool before this
        # feature) must still get the table -- that is why it is created by
        # its own CREATE TABLE IF NOT EXISTS rather than as a new column.
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path)
        cache.close()
        raw = sqlite3.connect(db_path)
        raw.execute("DROP TABLE series_skips")
        raw.commit()
        raw.close()

        cache2 = Cache(db_path)
        assert cache2.list_series_skips() == []
        cache2.add_series_skip("k1", ("d:/a/1.jpg",), "2026-08-19T10:00:00+00:00")
        assert len(cache2.list_series_skips()) == 1
        cache2.close()

    def test_a_skip_written_without_close_is_still_there_after_a_crash(self, tmp_path):
        # add_series_skip() commits immediately instead of joining the
        # batched scan transaction: a single deliberate click must not be
        # lost to a Ctrl+C that never flushes the batch.
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path, batch_size=100)
        cache.put(make_file(path="a"))  # opens a batch that will never commit
        cache.add_series_skip("k1", ("d:/a/1.jpg",), "2026-08-19T10:00:00+00:00")
        cache._conn.close()  # hard interruption, no commit()/close()

        cache2 = Cache(db_path)
        assert cache2.list_series_skips() == [("k1", ("d:/a/1.jpg",))]
        cache2.close()


class TestDupSkips:
    """The "Exact duplicates" tab's persisted skips: same shape as
    series_skips, but a table of their own."""

    def test_round_trip_through_a_reopen(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path)
        cache.add_dup_skip("k1", ("d:/a/1.jpg", "d:/b/1.jpg"), "2026-11-11T10:00:00+00:00")
        cache.close()

        cache2 = Cache(db_path)
        assert cache2.list_dup_skips() == [("k1", ("d:/a/1.jpg", "d:/b/1.jpg"))]
        cache2.close()

    def test_add_is_idempotent_and_refreshes_the_timestamp(self, tmp_path):
        with Cache(str(tmp_path / "cache.sqlite3")) as cache:
            cache.add_dup_skip("k1", ("d:/a/1.jpg",), "2026-11-11T10:00:00+00:00")
            cache.add_dup_skip("k1", ("d:/a/1.jpg",), "2026-11-12T10:00:00+00:00")
            assert len(cache.list_dup_skips()) == 1
            row = cache._conn.execute(
                "SELECT skipped_at FROM dup_skips WHERE group_key = 'k1'"
            ).fetchone()
            assert row["skipped_at"] == "2026-11-12T10:00:00+00:00"

    def test_remove_deletes_only_the_named_keys(self, tmp_path):
        with Cache(str(tmp_path / "cache.sqlite3")) as cache:
            cache.add_dup_skip("k1", ("d:/a/1.jpg",), "2026-11-11T10:00:00+00:00")
            cache.add_dup_skip("k2", ("d:/b/1.jpg",), "2026-11-11T10:00:00+00:00")
            assert cache.remove_dup_skips(["k1"]) == 1
            assert cache.remove_dup_skips([]) == 0
            assert [key for key, _ in cache.list_dup_skips()] == ["k2"]

    def test_is_separate_from_series_skips(self, tmp_path):
        with Cache(str(tmp_path / "cache.sqlite3")) as cache:
            cache.add_dup_skip("k1", ("d:/a/1.jpg",), "2026-11-11T10:00:00+00:00")
            assert cache.list_series_skips() == []
            cache.add_series_skip("k2", ("d:/a/1.jpg",), "2026-11-11T10:00:00+00:00")
            assert [key for key, _ in cache.list_dup_skips()] == ["k1"]

    def test_table_is_created_on_a_database_that_predates_it(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        Cache(db_path).close()
        raw = sqlite3.connect(db_path)
        raw.execute("DROP TABLE dup_skips")
        raw.commit()
        raw.close()

        cache2 = Cache(db_path)
        assert cache2.list_dup_skips() == []
        cache2.add_dup_skip("k1", ("d:/a/1.jpg",), "2026-11-11T10:00:00+00:00")
        assert len(cache2.list_dup_skips()) == 1
        cache2.close()

    def test_survives_a_schema_version_bump(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path, schema_version=1)
        cache.add_dup_skip("k1", ("d:/a/1.jpg",), "2026-11-11T10:00:00+00:00")
        cache.close()

        cache2 = Cache(db_path, schema_version=2)
        assert cache2.list_dup_skips() == [("k1", ("d:/a/1.jpg",))]
        cache2.close()


class TestCompressCalibration:
    """compress_calibration: keyed by (machine_id, device_key) --
    docs/technical/components/compression-mechanism.md. The SQL column is still named "encoder" (see
    cache.get_calibration()'s own docstring for why); these tests exercise
    the Python-facing device_key contract, not the column's literal name."""

    def test_put_and_get_round_trip(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path)
        cache.put_calibration(
            "machine-1", "hevc_nvenc@NVIDIA GeForce RTX 4060",
            0.05, 120.0, "2026-09-24T00:00:00+00:00", xpsnr_y=40.5,
        )
        result = cache.get_calibration("machine-1")
        cache.close()
        assert result == {
            "hevc_nvenc@NVIDIA GeForce RTX 4060": {
                "bpp": 0.05, "mpx_s": 120.0,
                "calibrated_at": "2026-09-24T00:00:00+00:00", "xpsnr_y": 40.5,
            },
        }

    def test_cpu_device_key_is_the_bare_encoder_name(self, tmp_path):
        # A CPU row's device_key equals its encoder name -- rows written
        # before device_key existed (keyed by bare "libx265"/"libsvtav1")
        # read back correctly with no migration (docs/technical/components/compression-mechanism.md).
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path)
        cache.put_calibration("machine-1", "libx265", 0.1, 20.9, "2026-09-24T00:00:00+00:00")
        result = cache.get_calibration("machine-1")
        cache.close()
        assert "libx265" in result
        assert result["libx265"]["xpsnr_y"] is None

    def test_different_machines_do_not_share_calibration(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path)
        cache.put_calibration("machine-1", "libx265", 0.1, 20.9, "2026-09-24T00:00:00+00:00")
        assert cache.get_calibration("machine-2") == {}
        cache.close()

    def test_put_calibration_replaces_the_previous_measurement(self, tmp_path):
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path)
        cache.put_calibration("machine-1", "libx265", 0.1, 20.9, "2026-09-24T00:00:00+00:00")
        cache.put_calibration(
            "machine-1", "libx265", 0.11, 21.5, "2026-09-25T00:00:00+00:00", xpsnr_y=39.0,
        )
        result = cache.get_calibration("machine-1")
        cache.close()
        assert result["libx265"]["bpp"] == 0.11
        assert result["libx265"]["xpsnr_y"] == 39.0

    def test_xpsnr_y_column_is_added_to_a_database_that_predates_it(self, tmp_path):
        # ALTER TABLE ADD COLUMN migration (docs/technical/components/compression-mechanism.md): a
        # compress_calibration table created before xpsnr_y existed must
        # gain the column on next open, keeping its existing rows.
        db_path = str(tmp_path / "cache.sqlite3")
        cache = Cache(db_path)
        cache.close()
        raw = sqlite3.connect(db_path)
        raw.execute(
            "CREATE TABLE compress_calibration_old AS SELECT * FROM compress_calibration"
        )
        raw.execute("DROP TABLE compress_calibration")
        raw.execute(
            """
            CREATE TABLE compress_calibration (
                machine_id TEXT NOT NULL,
                encoder TEXT NOT NULL,
                bpp REAL NOT NULL,
                mpx_s REAL NOT NULL,
                calibrated_at TEXT NOT NULL,
                PRIMARY KEY (machine_id, encoder)
            )
            """
        )
        raw.execute(
            "INSERT INTO compress_calibration (machine_id, encoder, bpp, mpx_s, calibrated_at) "
            "VALUES ('machine-1', 'libx265', 0.101, 20.9, '2026-09-12T00:00:00+00:00')"
        )
        raw.commit()
        raw.close()

        cache2 = Cache(db_path)
        result = cache2.get_calibration("machine-1")
        cache2.close()
        assert result["libx265"]["bpp"] == 0.101
        assert result["libx265"]["xpsnr_y"] is None  # pre-migration row has no measurement

        raw2 = sqlite3.connect(db_path)
        columns = {row[1] for row in raw2.execute("PRAGMA table_info(compress_calibration)")}
        raw2.close()
        assert "xpsnr_y" in columns
