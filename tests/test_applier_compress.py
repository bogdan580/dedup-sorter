"""Tests for tidy/moves/applier's op=compress support: apply_compressions() and
restore_batch()'s op=compress branch.

Same double-tracking pattern as test_applier.py: archive content lives in
FakeFileSystem, but `archive_root` is a real tmp_path-backed directory
because manifest.py writes manifest.csv with plain open() independent of
the `fs` seam.
"""
import hashlib
from datetime import datetime

from tidy.moves import applier
from tidy import config
from tidy.moves import manifest
from fakes import FakeFileSystem


def fixed_clock(dt=datetime(2026, 7, 26, 10, 30)):
    return lambda: dt


def _root(tmp_path) -> str:
    return str(tmp_path).replace("\\", "/")


def _real_hash(content: bytes) -> str:
    return hashlib.blake2b(content).hexdigest()


def _make_fs(root, *, original_content=b"original-bytes", staging_content=b"compressed-bytes"):
    fs = FakeFileSystem()
    fs.add_dir(root)
    fs.add_dir(f"{root}/#Home/2024 Gdansk")
    fs.add_dir(f"{root}/.dedup/.compress")

    original = f"{root}/#Home/2024 Gdansk/clip.mp4"
    staging = f"{root}/.dedup/.compress/job1.mp4"
    fs.add_file(original, original_content, size=len(original_content), mtime_ns=100)
    fs.add_file(staging, staging_content, size=len(staging_content), mtime_ns=200)
    return fs, original, staging


class TestApplyCompressions:
    def test_moves_staging_to_archive_leaves_original_untouched(self, tmp_path):
        root = _root(tmp_path)
        fs, original, staging = _make_fs(root)
        dst = f"{root}/#Home/2024 Gdansk/clip_compressed.mp4"

        plan = applier.CompressPlan(
            original_abs=original, staging_abs=staging, dst_abs=dst,
            group_id="1", reason="libx265 crf21 slow",
        )
        result = applier.apply_compressions(fs, root, [plan], clock=fixed_clock())

        assert result.moved == 1
        assert result.failed == 0
        assert fs.exists(original) is True          # never touched
        assert fs.exists(staging) is False           # moved away from staging
        assert fs.exists(dst) is True                # now lives in the archive

        rows = manifest.read_rows(result.manifest_path)
        assert len(rows) == 2  # planned + done
        done = rows[-1]
        assert done.op == manifest.OP_COMPRESS
        assert done.status == manifest.STATUS_DONE
        assert done.src_abs == original
        assert done.dst_rel == dst  # absolute, like op=move
        assert done.keeper_abs == original
        assert done.size == len(b"compressed-bytes")
        assert done.hash == _real_hash(b"compressed-bytes")
        assert done.reason == "libx265 crf21 slow"

    def test_manifest_lives_under_moves_dir(self, tmp_path):
        root = _root(tmp_path)
        fs, original, staging = _make_fs(root)
        dst = f"{root}/#Home/2024 Gdansk/clip_compressed.mp4"
        plan = applier.CompressPlan(original_abs=original, staging_abs=staging, dst_abs=dst)
        result = applier.apply_compressions(fs, root, [plan], clock=fixed_clock())
        assert f"{root}/{config.MOVES_DIR_NAME}/" in result.manifest_path.replace("\\", "/")

    def test_collision_gets_dup_suffix(self, tmp_path):
        root = _root(tmp_path)
        fs, original, staging = _make_fs(root)
        dst = f"{root}/#Home/2024 Gdansk/clip_compressed.mp4"
        fs.add_file(dst, b"already-here", size=12, mtime_ns=50)  # pre-existing collision

        plan = applier.CompressPlan(original_abs=original, staging_abs=staging, dst_abs=dst)
        result = applier.apply_compressions(fs, root, [plan], clock=fixed_clock())

        assert result.moved == 1
        assert fs.exists(dst) is True  # untouched pre-existing file
        dup = f"{root}/#Home/2024 Gdansk/clip_compressed__dup2.mp4"
        assert fs.exists(dup) is True

    def test_refuses_destination_outside_archive_root(self, tmp_path):
        root = _root(tmp_path)
        fs, original, staging = _make_fs(root)
        outside = "C:/Windows/clip_compressed.mp4"
        plan = applier.CompressPlan(original_abs=original, staging_abs=staging, dst_abs=outside)

        result = applier.apply_compressions(fs, root, [plan], clock=fixed_clock())

        assert result.moved == 0
        assert result.failed == 1
        assert fs.exists(staging) is True  # refused before ever touching the file
        assert fs.exists(original) is True

    def test_refuses_destination_inside_remove_dir(self, tmp_path):
        root = _root(tmp_path)
        fs, original, staging = _make_fs(root)
        dst = f"{root}/{config.REMOVE_DIR_NAME}/2026-01-01_00-00/clip_compressed.mp4"
        plan = applier.CompressPlan(original_abs=original, staging_abs=staging, dst_abs=dst)

        result = applier.apply_compressions(fs, root, [plan], clock=fixed_clock())

        assert result.moved == 0
        assert result.failed == 1
        assert fs.exists(staging) is True


class TestRestoreCompressBatch:
    def test_restore_moves_compressed_result_to_remove_leaves_original(self, tmp_path):
        root = _root(tmp_path)
        fs, original, staging = _make_fs(root)
        dst = f"{root}/#Home/2024 Gdansk/clip_compressed.mp4"
        plan = applier.CompressPlan(
            original_abs=original, staging_abs=staging, dst_abs=dst,
            group_id="1", reason="libx265 crf21 slow",
        )
        apply_result = applier.apply_compressions(fs, root, [plan], clock=fixed_clock())

        restore_result = applier.restore_batch(
            fs, apply_result.manifest_path, archive_root=root, clock=fixed_clock(),
        )

        assert restore_result.restored == 1
        assert restore_result.refused == 0
        assert fs.exists(original) is True   # original was never touched, still there
        assert fs.exists(dst) is False        # _compressed result moved out of the archive

        remove_root = f"{root}/{config.REMOVE_DIR_NAME}"
        assert fs.exists(f"{remove_root}/.dedup-kosz") is True
        moved_entry = restore_result.entries[0]
        assert moved_entry.status == "restored"
        assert fs.exists(moved_entry.dst_abs) is True
        assert remove_root in moved_entry.dst_abs.replace("\\", "/")

        # The undo itself is a normal, independently restorable op=remove batch.
        undo_rows = manifest.read_rows(_undo_manifest_path(moved_entry.dst_abs, remove_root))
        assert undo_rows[-1].op == manifest.OP_REMOVE
        assert undo_rows[-1].status == manifest.STATUS_DONE

    def test_restore_refuses_when_compressed_result_already_gone(self, tmp_path):
        # Hand-construct a "done" op=compress manifest whose dst_rel was
        # never actually created in `fs` -- simulates the user having
        # already deleted the compressed file by hand before restoring.
        root = _root(tmp_path)
        fs, original, staging = _make_fs(root)
        fs.add_dir(f"{root}/{config.MOVES_DIR_NAME}/2026-07-26_10-30")
        manifest_path = f"{root}/{config.MOVES_DIR_NAME}/2026-07-26_10-30/{config.MANIFEST_FILENAME}"
        dst = f"{root}/#Home/2024 Gdansk/clip_compressed.mp4"  # never added to fs

        row = manifest.ManifestRow(
            op=manifest.OP_COMPRESS, src_abs=original, dst_rel=dst, size=100, mtime=0,
            hash="deadbeef", group_id="1", keeper_abs=original, reason="libx265 crf21 slow",
            ts="2026-07-26T10:30:00", tool_version=config.TOOL_VERSION,
            status=manifest.STATUS_DONE,
        )
        manifest.append_planned(manifest_path, row)
        manifest.append_confirmation(manifest_path, row)

        restore_result = applier.restore_batch(
            fs, manifest_path, archive_root=root, clock=fixed_clock(),
        )
        assert restore_result.refused == 1
        assert restore_result.restored == 0
        assert fs.exists(original) is True

    def test_restore_without_archive_root_raises_for_compress_manifest(self, tmp_path):
        root = _root(tmp_path)
        fs, original, staging = _make_fs(root)
        dst = f"{root}/#Home/2024 Gdansk/clip_compressed.mp4"
        plan = applier.CompressPlan(original_abs=original, staging_abs=staging, dst_abs=dst)
        apply_result = applier.apply_compressions(fs, root, [plan], clock=fixed_clock())

        try:
            applier.restore_batch(fs, apply_result.manifest_path)
        except ValueError:
            pass
        else:
            assert False, "expected ValueError when archive_root is missing"


class TestApplyCompressRemovals:
    """applier.apply_compress_removals() -- a thin overlay on apply_batch()
    that moves an original whose _compressed result is already confirmed
    and living in the archive into a fresh !REMOVE batch, separate from the
    !MOVES batch apply_compressions() used (docs/technical/components/manifest-and-db.md)."""

    def _make_removal_fs(self, root, *, original_size=100, compressed_size=40):
        fs = FakeFileSystem()
        fs.add_dir(root)
        original = f"{root}/#Home/2024 Gdansk/clip.mp4"
        compressed = f"{root}/#Home/2024 Gdansk/clip_compressed.mp4"
        fs.add_file(original, b"o" * original_size, size=original_size, mtime_ns=100)
        fs.add_file(compressed, b"c" * compressed_size, size=compressed_size, mtime_ns=200)
        return fs, original, compressed

    def test_moves_original_to_remove_when_result_is_smaller(self, tmp_path):
        root = _root(tmp_path)
        fs, original, compressed = self._make_removal_fs(root)
        removal = applier.CompressRemoval(original_abs=original, compressed_abs=compressed)

        result = applier.apply_compress_removals(fs, root, [removal], clock=fixed_clock())

        assert result.moved == 1
        assert result.failed == 0
        assert fs.exists(original) is False       # original is gone
        assert fs.exists(compressed) is True       # compressed result untouched, still in archive
        assert f"{root}/{config.REMOVE_DIR_NAME}/" in result.manifest_path.replace("\\", "/")

    def test_manifest_row_has_compress_remove_reason(self, tmp_path):
        root = _root(tmp_path)
        fs, original, compressed = self._make_removal_fs(root)
        removal = applier.CompressRemoval(original_abs=original, compressed_abs=compressed)

        result = applier.apply_compress_removals(fs, root, [removal], clock=fixed_clock())

        rows = manifest.read_rows(result.manifest_path)
        done = rows[-1]
        assert done.op == manifest.OP_REMOVE
        assert done.status == manifest.STATUS_DONE
        assert done.reason == "compress-remove-original"
        assert done.keeper_abs == compressed

    def test_bigger_result_never_removes_original(self, tmp_path):
        # Second safety net (docs/business/rules/compression.md): a compressed result that is
        # not strictly smaller than the original is never removed, no
        # matter what the caller asked for.
        root = _root(tmp_path)
        fs, original, compressed = self._make_removal_fs(
            root, original_size=40, compressed_size=100,
        )
        removal = applier.CompressRemoval(original_abs=original, compressed_abs=compressed)

        result = applier.apply_compress_removals(fs, root, [removal], clock=fixed_clock())

        assert result.moved == 0
        assert result.failed == 1
        assert fs.exists(original) is True
        assert fs.exists(compressed) is True

    def test_equal_size_result_never_removes_original(self, tmp_path):
        root = _root(tmp_path)
        fs, original, compressed = self._make_removal_fs(
            root, original_size=50, compressed_size=50,
        )
        removal = applier.CompressRemoval(original_abs=original, compressed_abs=compressed)

        result = applier.apply_compress_removals(fs, root, [removal], clock=fixed_clock())

        assert result.moved == 0
        assert result.failed == 1
        assert fs.exists(original) is True

    def test_manifest_lives_under_remove_dir_separate_from_moves_dir(self, tmp_path):
        root = _root(tmp_path)
        fs, original, compressed = self._make_removal_fs(root)
        removal = applier.CompressRemoval(original_abs=original, compressed_abs=compressed)

        result = applier.apply_compress_removals(fs, root, [removal], clock=fixed_clock())

        manifest_path_norm = result.manifest_path.replace("\\", "/")
        assert f"{root}/{config.REMOVE_DIR_NAME}/" in manifest_path_norm
        assert f"{root}/{config.MOVES_DIR_NAME}/" not in manifest_path_norm

    def test_restore_of_removal_batch_brings_original_back(self, tmp_path):
        root = _root(tmp_path)
        fs, original, compressed = self._make_removal_fs(root)
        removal = applier.CompressRemoval(original_abs=original, compressed_abs=compressed)
        apply_result = applier.apply_compress_removals(fs, root, [removal], clock=fixed_clock())

        restore_result = applier.restore_batch(fs, apply_result.manifest_path, clock=fixed_clock())

        assert restore_result.restored == 1
        assert fs.exists(original) is True
        assert fs.exists(compressed) is True  # never touched by this removal batch


def _undo_manifest_path(moved_dst_abs: str, remove_root: str) -> str:
    """The undo batch's manifest.csv sits next to the moved file, one level
    up (files are moved into <remove_root>/<ts>/<relative...>)."""
    norm = moved_dst_abs.replace("\\", "/")
    remove_root_norm = remove_root.replace("\\", "/").rstrip("/")
    rel = norm[len(remove_root_norm) + 1:]
    ts_dir = rel.split("/", 1)[0]
    return f"{remove_root_norm}/{ts_dir}/{config.MANIFEST_FILENAME}"


class TestSharedQueueBatch:
    """reserve_batch_dir() + batch_dir=: the review UI's compress queue
    lands every job, one call each, into ONE !MOVES and ONE !REMOVE batch
    named after the queue's start."""

    def _make_two_jobs_fs(self, root):
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_dir(f"{root}/.dedup/.compress")
        jobs = []
        for n in (1, 2):
            original = f"{root}/#Home/2024 Gdansk/clip{n}.mp4"
            staging = f"{root}/.dedup/.compress/{n}.mp4"
            fs.add_file(original, b"o" * 100, size=100, mtime_ns=100)
            fs.add_file(staging, b"c" * 40, size=40, mtime_ns=200)
            jobs.append((original, staging, f"{root}/#Home/2024 Gdansk/clip{n}_compressed.mp4"))
        return fs, jobs

    def test_reserve_names_the_batch_after_the_start_time(self, tmp_path):
        root = _root(tmp_path)
        fs = FakeFileSystem()
        fs.add_dir(root)
        started = datetime(2026, 7, 26, 9, 5)

        batch_dir = applier.reserve_batch_dir(fs, root, config.REMOVE_DIR_NAME, started)

        expected_name = started.strftime(config.BATCH_DIR_TIMESTAMP_FORMAT)
        assert batch_dir == f"{root}/{config.REMOVE_DIR_NAME}/{expected_name}"
        assert fs.exists(batch_dir)
        assert fs.exists(f"{root}/{config.REMOVE_DIR_NAME}/{config.KOSZ_SENTINEL_FILENAME}")

    def test_reserve_never_reuses_an_existing_batch_dir(self, tmp_path):
        root = _root(tmp_path)
        fs = FakeFileSystem()
        fs.add_dir(root)
        started = datetime(2026, 7, 26, 9, 5)

        first = applier.reserve_batch_dir(fs, root, config.MOVES_DIR_NAME, started)
        second = applier.reserve_batch_dir(fs, root, config.MOVES_DIR_NAME, started)

        assert first != second
        assert second == f"{first}_2"

    def test_separate_calls_share_one_moves_and_one_remove_manifest(self, tmp_path):
        root = _root(tmp_path)
        fs, jobs = self._make_two_jobs_fs(root)
        started = datetime(2026, 7, 26, 9, 5)
        moves_dir = applier.reserve_batch_dir(fs, root, config.MOVES_DIR_NAME, started)
        remove_dir = applier.reserve_batch_dir(fs, root, config.REMOVE_DIR_NAME, started)

        manifests = set()
        for original, staging, dst in jobs:
            c = applier.apply_compressions(
                fs, root, [applier.CompressPlan(original_abs=original, staging_abs=staging, dst_abs=dst)],
                clock=fixed_clock(), batch_dir=moves_dir,
            )
            r = applier.apply_compress_removals(
                fs, root, [applier.CompressRemoval(original_abs=original, compressed_abs=dst)],
                clock=fixed_clock(), batch_dir=remove_dir,
            )
            assert c.batch_dir == moves_dir and r.batch_dir == remove_dir
            manifests.update({c.manifest_path, r.manifest_path})

        assert manifests == {
            f"{moves_dir}/{config.MANIFEST_FILENAME}",
            f"{remove_dir}/{config.MANIFEST_FILENAME}",
        }
        moves_rows = manifest.read_final_rows(f"{moves_dir}/{config.MANIFEST_FILENAME}")
        remove_rows = manifest.read_final_rows(f"{remove_dir}/{config.MANIFEST_FILENAME}")
        assert {r.status for r in moves_rows.values()} == {manifest.STATUS_DONE}
        assert {r.status for r in remove_rows.values()} == {manifest.STATUS_DONE}
        assert len(moves_rows) == 2 and len(remove_rows) == 2
        for original, _staging, dst in jobs:
            assert not fs.exists(original)
            assert fs.exists(dst)

    def test_restore_of_shared_remove_batch_brings_every_original_back(self, tmp_path):
        root = _root(tmp_path)
        fs, jobs = self._make_two_jobs_fs(root)
        remove_dir = applier.reserve_batch_dir(
            fs, root, config.REMOVE_DIR_NAME, datetime(2026, 7, 26, 9, 5),
        )
        for original, staging, dst in jobs:
            fs.move(staging, dst)
            applier.apply_compress_removals(
                fs, root, [applier.CompressRemoval(original_abs=original, compressed_abs=dst)],
                clock=fixed_clock(), batch_dir=remove_dir,
            )

        result = applier.restore_batch(fs, f"{remove_dir}/{config.MANIFEST_FILENAME}")

        assert result.restored == 2
        for original, _staging, _dst in jobs:
            assert fs.exists(original)

    def test_reused_batch_dir_never_overwrites_an_earlier_calls_file(self, tmp_path):
        # Two different originals mapping to the same batch-relative path
        # cannot happen in a real queue, but apply_batch() must still dodge
        # a file an earlier call already put in the reused directory.
        root = _root(tmp_path)
        fs = FakeFileSystem()
        fs.add_dir(root)
        original = f"{root}/a/clip.mp4"
        compressed = f"{root}/a/clip_compressed.mp4"
        fs.add_file(original, b"o" * 100, size=100, mtime_ns=100)
        fs.add_file(compressed, b"c" * 40, size=40, mtime_ns=200)
        remove_dir = applier.reserve_batch_dir(
            fs, root, config.REMOVE_DIR_NAME, datetime(2026, 7, 26, 9, 5),
        )
        fs.add_file(f"{remove_dir}/a/clip.mp4", b"x", size=1, mtime_ns=1)

        result = applier.apply_compress_removals(
            fs, root, [applier.CompressRemoval(original_abs=original, compressed_abs=compressed)],
            clock=fixed_clock(), batch_dir=remove_dir,
        )

        assert result.moved == 1
        assert result.entries[0].dst_abs == f"{remove_dir}/a/clip__dup2.mp4"
        assert fs.exists(f"{remove_dir}/a/clip.mp4")
