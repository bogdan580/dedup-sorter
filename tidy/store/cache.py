"""SQLite cache: path+size+mtime -> hash, so repeat scans are fast.

Design notes:
- The connection is opened with isolation_level=None (autocommit) so that
  schema creation and meta-table reconciliation happen as small, immediately
  committed statements, decoupled from the batched write transaction used
  for `files` rows. This keeps algo-invalidation safe to run even while a
  previous, uncommitted batch of file writes is still pending in another
  process/handle.
- Batched writes: `put()` lazily opens an explicit transaction (BEGIN) on
  the first write of a batch and commits every `batch_size` writes, or when
  `commit()`/`close()` is called explicitly. This is what makes an
  interrupted scan resumable: only fully committed rows survive a crash.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from tidy import config


@dataclass
class CachedFile:
    path: str
    display_path: str
    size: int
    mtime_ns: int
    volume_id: Optional[int]
    file_index: Optional[int]
    attributes: int
    partial_hash: Optional[str]
    full_hash: Optional[str]
    dhash: Optional[str]
    status: str
    error: Optional[str]
    scanned_at: str


_FILES_COLUMNS = (
    "path", "display_path", "size", "mtime_ns", "volume_id", "file_index",
    "attributes", "partial_hash", "full_hash", "dhash", "status", "error",
    "scanned_at",
)


class Cache:
    def __init__(
        self,
        db_path: str,
        *,
        hash_algo: str = config.DEFAULT_HASH_ALGO,
        partial_algo: str = config.DEFAULT_PARTIAL_ALGO,
        dhash_algo: str = config.DEFAULT_DHASH_ALGO,
        schema_version: int = config.CACHE_SCHEMA_VERSION,
        batch_size: int = config.CACHE_COMMIT_BATCH_SIZE,
    ) -> None:
        # sqlite3 creates the file but never its parent directory, so
        # without this the first run dies with "unable to open database
        # file". Plain os.makedirs() rather than the FileSystem seam on
        # purpose: the cache is tool-internal bookkeeping, never archive
        # content.
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._batch_size = batch_size
        self._pending = 0
        self._in_txn = False

        self._ensure_schema()
        self._ensure_timestamp_schema()
        self._ensure_series_skip_schema()
        self._ensure_placement_skip_schema()
        self._ensure_compress_schema()
        self._reconcile_meta(hash_algo, partial_algo, dhash_algo, schema_version)

    # -- schema -------------------------------------------------------
    def _ensure_schema(self) -> None:
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'"
        ).fetchone()
        if exists:
            return
        self._conn.execute(
            """
            CREATE TABLE meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                schema_version INTEGER NOT NULL,
                hash_algo TEXT NOT NULL,
                partial_algo TEXT NOT NULL,
                dhash_algo TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                display_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                volume_id INTEGER,
                file_index INTEGER,
                attributes INTEGER NOT NULL,
                partial_hash TEXT,
                full_hash TEXT,
                dhash TEXT,
                status TEXT NOT NULL,
                error TEXT,
                scanned_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX idx_files_size ON files(size)")
        self._conn.execute(
            "CREATE INDEX idx_files_volume_file_index ON files(volume_id, file_index)"
        )
        self._conn.execute("CREATE INDEX idx_files_status ON files(status)")

    def _ensure_timestamp_schema(self) -> None:
        """The photo-taken-at index, in its own table rather than a column on
        `files`: `_ensure_schema()` above returns early when `files` already
        exists, so a new column would never appear in an existing database,
        and this value is invalidated by different things than the hash
        columns (a timestamp goes stale only when the FILE does, which the
        stored (size, mtime_ns) pair already detects)."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timestamps (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                taken_at TEXT NOT NULL,
                source TEXT NOT NULL
            )
            """
        )

    def _ensure_series_skip_schema(self) -> None:
        """Stores the review UI's "skip this group" skip decisions for the
        "Serie i podobne" tab. Deliberately outside every invalidation
        `_reconcile_meta()` performs: a human verdict about photos is not a
        derived value.

        `paths` holds the group's members in series.normalize_member_paths()
        form; `group_key` is series.skip_key() over the same list.

        Details: docs/technical/components/manifest-and-db.md"""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS series_skips (
                group_key TEXT PRIMARY KEY,
                paths TEXT NOT NULL,
                member_count INTEGER NOT NULL,
                skipped_at TEXT NOT NULL
            )
            """
        )

    def _ensure_placement_skip_schema(self) -> None:
        """The review UI's "Sugestie grupowania" tab skip decisions, kept in
        its own table rather than sharing `series_skips`: a series group and
        a placement suggestion can legitimately cover the same file paths,
        and a shared table would let skipping one hide the other."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS placement_skips (
                group_key TEXT PRIMARY KEY,
                paths TEXT NOT NULL,
                member_count INTEGER NOT NULL,
                skipped_at TEXT NOT NULL
            )
            """
        )

    def _ensure_compress_schema(self) -> None:
        """Three tables for the compression feature:
        - compress_probes: cached ffprobe output keyed by (path, size, mtime_ns).
        - compress_skips: persisted user skip decisions, same shape as series_skips.
        - compress_calibration: measured encoder performance, keyed by
          (machine_id, device_key) -- see put_calibration()'s own docstring
          for why the "encoder" column name stays as-is (docs/technical/components/compression-mechanism.md)."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS compress_probes (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                codec TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                fps REAL NOT NULL,
                bitrate INTEGER NOT NULL,
                duration REAL NOT NULL,
                has_audio INTEGER NOT NULL,
                rotation INTEGER NOT NULL,
                audio_codec TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS compress_skips (
                group_key TEXT PRIMARY KEY,
                paths TEXT NOT NULL,
                member_count INTEGER NOT NULL,
                skipped_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS compress_calibration (
                machine_id TEXT NOT NULL,
                encoder TEXT NOT NULL,
                bpp REAL NOT NULL,
                mpx_s REAL NOT NULL,
                calibrated_at TEXT NOT NULL,
                PRIMARY KEY (machine_id, encoder)
            )
            """
        )
        # xpsnr_y: added by docs/technical/components/compression-mechanism.md, alongside the bpp/mpx_s a
        # completed queue job already measured -- ALTER TABLE ADD COLUMN
        # migration for a DB from before this column existed, matching the
        # project's forgiving-config convention (never abort, only extend).
        existing_columns = {
            row["name"] for row in
            self._conn.execute("PRAGMA table_info(compress_calibration)").fetchall()
        }
        if "xpsnr_y" not in existing_columns:
            self._conn.execute(
                "ALTER TABLE compress_calibration ADD COLUMN xpsnr_y REAL"
            )

    # -- meta / algo invalidation --------------------------------------
    def _reconcile_meta(
        self, hash_algo: str, partial_algo: str, dhash_algo: str, schema_version: int
    ) -> None:
        row = self._conn.execute(
            "SELECT schema_version, hash_algo, partial_algo, dhash_algo FROM meta WHERE id = 1"
        ).fetchone()

        if row is None:
            self._conn.execute(
                "INSERT INTO meta (id, schema_version, hash_algo, partial_algo, dhash_algo) "
                "VALUES (1, ?, ?, ?, ?)",
                (schema_version, hash_algo, partial_algo, dhash_algo),
            )
            return

        partial_changed = row["partial_algo"] != partial_algo
        hash_changed = row["hash_algo"] != hash_algo
        schema_changed = row["schema_version"] != schema_version
        dhash_changed = row["dhash_algo"] != dhash_algo

        if partial_changed or schema_changed:
            self._conn.execute("UPDATE files SET partial_hash = NULL, full_hash = NULL")
        elif hash_changed:
            self._conn.execute("UPDATE files SET full_hash = NULL")

        if dhash_changed or schema_changed:
            self._conn.execute("UPDATE files SET dhash = NULL")

        if partial_changed or hash_changed or dhash_changed or schema_changed:
            self._conn.execute(
                "UPDATE meta SET schema_version = ?, hash_algo = ?, partial_algo = ?, "
                "dhash_algo = ? WHERE id = 1",
                (schema_version, hash_algo, partial_algo, dhash_algo),
            )

    # -- reads ----------------------------------------------------------
    def get(self, path: str) -> Optional[CachedFile]:
        row = self._conn.execute(
            f"SELECT {', '.join(_FILES_COLUMNS)} FROM files WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return None
        return CachedFile(**{col: row[col] for col in _FILES_COLUMNS})

    def is_hit(self, path: str, size: int, mtime_ns: int) -> bool:
        row = self._conn.execute(
            "SELECT size, mtime_ns FROM files WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return False
        return row["size"] == size and row["mtime_ns"] == mtime_ns

    def get_timestamp(self, path: str, size: int, mtime_ns: int) -> Optional[Tuple[str, str]]:
        """Cached (taken_at, source) for `path`, or None when absent or
        stale. Staleness is the same (size, mtime_ns) test is_hit() uses:
        an edited or replaced file gets its date recomputed, and nothing
        else does."""
        row = self._conn.execute(
            "SELECT size, mtime_ns, taken_at, source FROM timestamps WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return None
        if row["size"] != size or row["mtime_ns"] != mtime_ns:
            return None
        return row["taken_at"], row["source"]

    # -- series skips (review UI's "skip this group") ----------------------
    #
    # Unlike put()/put_timestamp() below, these three commit IMMEDIATELY
    # rather than joining the batched scan transaction. A skip is a single
    # deliberate click that must survive an interrupted run, and immediate
    # commit is what makes it safe to call on a Cache opened purely for
    # this purpose.

    def list_series_skips(self) -> List[Tuple[str, Tuple[str, ...]]]:
        """Every stored skip as (group_key, normalized member paths). Read
        once at server start into an in-memory mirror -- the review server
        checks this on every GET /api/series, and that must never mean a
        sqlite round trip from a request thread."""
        rows = self._conn.execute(
            "SELECT group_key, paths FROM series_skips"
        ).fetchall()
        return [
            (row["group_key"], tuple(p for p in row["paths"].split("\n") if p))
            for row in rows
        ]

    def add_series_skip(
        self, group_key: str, paths: Sequence[str], skipped_at: str
    ) -> None:
        """Record (or re-record) one skipped group. INSERT OR REPLACE, so
        re-skipping the same group is idempotent and just refreshes
        skipped_at."""
        self.commit()  # never nest inside an open scan batch
        self._conn.execute(
            "INSERT OR REPLACE INTO series_skips "
            "(group_key, paths, member_count, skipped_at) VALUES (?, ?, ?, ?)",
            (group_key, "\n".join(paths), len(paths), skipped_at),
        )

    def remove_series_skips(self, group_keys: Sequence[str]) -> int:
        """Delete the named skips (un-checking "pomin" in the UI). Takes a
        LIST because one un-skip can clear more than one stored row: a group
        is hidden when its members are a SUBSET of a stored skip
        (series.group_is_skipped), so revealing it again means dropping every
        stored set that covers it -- clearing only the exactly-matching row
        would leave the group hidden by a wider one and make the checkbox
        look broken. Returns how many rows were actually deleted."""
        if not group_keys:
            return 0
        self.commit()
        placeholders = ", ".join("?" for _ in group_keys)
        cursor = self._conn.execute(
            f"DELETE FROM series_skips WHERE group_key IN ({placeholders})",
            tuple(group_keys),
        )
        return cursor.rowcount or 0

    # -- placement skips (review UI's "Sugestie grupowania" tab) -----------
    #
    # Same three-method shape and same commit-immediately reasoning as the
    # series-skip trio above (own table, see _ensure_placement_skip_schema()).

    def list_placement_skips(self) -> List[Tuple[str, Tuple[str, ...]]]:
        """Every stored placement skip as (group_key, normalized member
        paths). Read once at server start into an in-memory mirror, same
        reasoning as list_series_skips()."""
        rows = self._conn.execute(
            "SELECT group_key, paths FROM placement_skips"
        ).fetchall()
        return [
            (row["group_key"], tuple(p for p in row["paths"].split("\n") if p))
            for row in rows
        ]

    def add_placement_skip(
        self, group_key: str, paths: Sequence[str], skipped_at: str
    ) -> None:
        """Record (or re-record) one skipped placement suggestion. INSERT OR
        REPLACE, so re-skipping the same suggestion is idempotent and just
        refreshes skipped_at."""
        self.commit()  # never nest inside an open scan batch
        self._conn.execute(
            "INSERT OR REPLACE INTO placement_skips "
            "(group_key, paths, member_count, skipped_at) VALUES (?, ?, ?, ?)",
            (group_key, "\n".join(paths), len(paths), skipped_at),
        )

    def remove_placement_skips(self, group_keys: Sequence[str]) -> int:
        """Delete the named skips (un-checking "pomin" in the UI). Same
        subset-covering reasoning as remove_series_skips() -- a list, not a
        single key, because one un-skip can clear more than one stored row.
        Returns how many rows were actually deleted."""
        if not group_keys:
            return 0
        self.commit()
        placeholders = ", ".join("?" for _ in group_keys)
        cursor = self._conn.execute(
            f"DELETE FROM placement_skips WHERE group_key IN ({placeholders})",
            tuple(group_keys),
        )
        return cursor.rowcount or 0

    # -- compress probes (cached ffprobe output) --------------------------
    #
    # Immediate commit, same reasoning as skips: a probe is expensive to
    # recompute (subprocess call) and must survive an interrupted scan.

    _PROBE_COLUMNS = (
        "path", "size", "mtime_ns", "codec", "width", "height",
        "fps", "bitrate", "duration", "has_audio", "rotation", "audio_codec",
    )

    def get_compress_probe(
        self, path: str, size: int, mtime_ns: int
    ) -> Optional[dict]:
        """Cached probe for `path`, or None when absent or stale."""
        row = self._conn.execute(
            "SELECT size, mtime_ns, codec, width, height, fps, bitrate, "
            "duration, has_audio, rotation, audio_codec "
            "FROM compress_probes WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return None
        if row["size"] != size or row["mtime_ns"] != mtime_ns:
            return None
        return {
            "codec": row["codec"],
            "width": row["width"],
            "height": row["height"],
            "fps": row["fps"],
            "bitrate": row["bitrate"],
            "duration": row["duration"],
            "has_audio": bool(row["has_audio"]),
            "rotation": row["rotation"],
            "audio_codec": row["audio_codec"],
        }

    def put_compress_probe(
        self, path: str, size: int, mtime_ns: int, probe: dict
    ) -> None:
        """Store probe result. Commits immediately."""
        self.commit()
        self._conn.execute(
            "INSERT OR REPLACE INTO compress_probes "
            "(path, size, mtime_ns, codec, width, height, fps, bitrate, "
            "duration, has_audio, rotation, audio_codec) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                path, size, mtime_ns,
                probe["codec"], probe["width"], probe["height"],
                probe["fps"], probe["bitrate"], probe["duration"],
                int(probe["has_audio"]), probe["rotation"],
                probe.get("audio_codec", ""),
            ),
        )

    # -- compress skips (review UI's "skip" for compression tab) ----------
    #
    # Same three-method shape as series_skips and placement_skips.

    def list_compress_skips(self) -> List[Tuple[str, Tuple[str, ...]]]:
        """Every stored compression skip as (group_key, member paths)."""
        rows = self._conn.execute(
            "SELECT group_key, paths FROM compress_skips"
        ).fetchall()
        return [
            (row["group_key"], tuple(p for p in row["paths"].split("\n") if p))
            for row in rows
        ]

    def add_compress_skip(
        self, group_key: str, paths: Sequence[str], skipped_at: str
    ) -> None:
        """Record one skipped compression candidate."""
        self.commit()
        self._conn.execute(
            "INSERT OR REPLACE INTO compress_skips "
            "(group_key, paths, member_count, skipped_at) VALUES (?, ?, ?, ?)",
            (group_key, "\n".join(paths), len(paths), skipped_at),
        )

    def remove_compress_skips(self, group_keys: Sequence[str]) -> int:
        """Delete named compression skips. Returns count deleted."""
        if not group_keys:
            return 0
        self.commit()
        placeholders = ", ".join("?" for _ in group_keys)
        cursor = self._conn.execute(
            f"DELETE FROM compress_skips WHERE group_key IN ({placeholders})",
            tuple(group_keys),
        )
        return cursor.rowcount or 0

    # -- compress calibration ---------------------------------------------

    def get_calibration(self, machine_id: str) -> Dict[str, dict]:
        """All calibration entries for this machine, keyed by device_key
        (hwdetect.EncoderInfo.device_key: bare encoder name for CPU,
        "<encoder>@<device name>" for hardware -- docs/technical/components/compression-mechanism.md). The
        "encoder" SQL column is unrenamed on purpose: a CPU row's
        device_key already equals its encoder name, so every row written
        before device_key existed reads back correctly with no migration."""
        rows = self._conn.execute(
            "SELECT encoder, bpp, mpx_s, calibrated_at, xpsnr_y "
            "FROM compress_calibration WHERE machine_id = ?",
            (machine_id,),
        ).fetchall()
        return {
            row["encoder"]: {
                "bpp": row["bpp"],
                "mpx_s": row["mpx_s"],
                "calibrated_at": row["calibrated_at"],
                "xpsnr_y": row["xpsnr_y"],
            }
            for row in rows
        }

    def put_calibration(
        self, machine_id: str, device_key: str,
        bpp: float, mpx_s: float, calibrated_at: str,
        xpsnr_y: Optional[float] = None,
    ) -> None:
        """Store one calibration measurement, keyed by (machine_id,
        device_key) -- see get_calibration()'s own docstring for why the
        SQL column is still named "encoder". Commits immediately.
        `xpsnr_y` is None for a queue job's write-back when
        verify_xpsnr was disabled for that run; POST /api/compress/
        calibrate (E4.3) always measures and supplies one."""
        self.commit()
        self._conn.execute(
            "INSERT OR REPLACE INTO compress_calibration "
            "(machine_id, encoder, bpp, mpx_s, calibrated_at, xpsnr_y) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (machine_id, device_key, bpp, mpx_s, calibrated_at, xpsnr_y),
        )

    # -- writes (batched) -------------------------------------------------
    def put_timestamp(
        self, path: str, size: int, mtime_ns: int, taken_at: str, source: str
    ) -> None:
        """Shares put()'s transaction and batch counter deliberately: the two
        writes interleave during a scan, and giving them separate
        transactions on one autocommit connection would nest a BEGIN inside
        an open one."""
        if not self._in_txn:
            self._conn.execute("BEGIN")
            self._in_txn = True
        self._conn.execute(
            "INSERT OR REPLACE INTO timestamps (path, size, mtime_ns, taken_at, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (path, size, mtime_ns, taken_at, source),
        )
        self._pending += 1
        if self._pending >= self._batch_size:
            self.commit()

    def put(self, cached_file: CachedFile) -> None:
        if not self._in_txn:
            self._conn.execute("BEGIN")
            self._in_txn = True

        placeholders = ", ".join("?" for _ in _FILES_COLUMNS)
        values = tuple(getattr(cached_file, col) for col in _FILES_COLUMNS)
        self._conn.execute(
            f"INSERT OR REPLACE INTO files ({', '.join(_FILES_COLUMNS)}) "
            f"VALUES ({placeholders})",
            values,
        )
        self._pending += 1
        if self._pending >= self._batch_size:
            self.commit()

    def commit(self) -> None:
        if self._in_txn:
            self._conn.execute("COMMIT")
            self._in_txn = False
            self._pending = 0

    def close(self) -> None:
        self.commit()
        self._conn.close()

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
