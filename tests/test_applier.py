"""Tests for tidy/moves/applier: apply_batch()/restore_batch()/verify_manifest().

All *archive* file state goes through FakeFileSystem (in-memory) -- no real
file is ever moved by these tests. `archive_root` is nonetheless set to a
real (pytest tmp_path-backed) directory, because manifest.py deliberately
does its own real disk I/O independent of the `fs` seam (see manifest.py's
module docstring) -- in production, apply_batch()'s manifest lives inside
the very batch directory `fs.makedirs()` just created on the *same* real
disk. Using tmp_path as `archive_root` here reproduces that same coupling
safely (an isolated pytest temp dir, never the real archive) instead of
writing manifest.csv to an unrelated real location while the "moved" files
only ever exist inside FakeFileSystem's in-memory model.
"""
import hashlib
from datetime import datetime

import pytest

from tidy.moves import applier
from tidy import config
from tidy.moves import manifest
from fakes import FakeFileSystem
from tidy.duplicates.keeper import Discard, KeeperDecision
from tidy.duplicates.tiers import DuplicateGroup


def fixed_clock(dt=datetime(2026, 7, 26, 10, 30)):
    return lambda: dt


def _root(tmp_path) -> str:
    return str(tmp_path).replace("\\", "/")


def _real_hash(content: bytes) -> str:
    # Matches hashing.full_hash()'s algorithm (BLAKE2b) so that
    # verify_manifest()'s recomputed-hash check sees a genuine match for a
    # "clean" fixture, exactly like tiers.py would have produced this value
    # for real (a placeholder string here would make verify_manifest
    # correctly report a false "mismatch").
    return hashlib.blake2b(content).hexdigest()


def make_fs_with_two_duplicate_groups(root):
    fs = FakeFileSystem()
    fs.add_dir(root)
    fs.add_dir(f"{root}/#Travel")
    fs.add_dir(f"{root}/#Travel/2023 Mountains")
    fs.add_dir(f"{root}/!NEW")
    fs.add_dir(f"{root}/!NEW/Camera")

    keeper1 = f"{root}/#Travel/2023 Mountains/img.jpg"
    discard1 = f"{root}/!NEW/Camera/img.jpg"
    fs.add_file(keeper1, b"same-bytes-1", size=12, mtime_ns=100)
    fs.add_file(discard1, b"same-bytes-1", size=12, mtime_ns=200)

    keeper2 = f"{root}/#Travel/2023 Mountains/vid.mp4"
    discard2 = f"{root}/!NEW/Camera/vid.mp4"
    fs.add_file(keeper2, b"same-bytes-2", size=12, mtime_ns=300)
    fs.add_file(discard2, b"same-bytes-2", size=12, mtime_ns=400)

    groups = [
        DuplicateGroup(size=12, full_hash=_real_hash(b"same-bytes-1"), paths=(keeper1, discard1)),
        DuplicateGroup(size=12, full_hash=_real_hash(b"same-bytes-2"), paths=(keeper2, discard2)),
    ]
    decisions = [
        KeeperDecision(keeper=keeper1, discarded=(Discard(path=discard1, reason="rule-0"),)),
        KeeperDecision(keeper=keeper2, discarded=(Discard(path=discard2, reason="rule-0"),)),
    ]
    return fs, groups, decisions, keeper1, discard1, keeper2, discard2


class TestApplyBatchHappyPath:
    def test_creates_remove_dir_and_kosz_sentinel(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, *_ = make_fs_with_two_duplicate_groups(root)

        applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        assert fs.exists(f"{root}/!REMOVE") is True
        assert fs.exists(f"{root}/!REMOVE/.dedup-kosz") is True

    def test_batch_dir_name_uses_clock_timestamp(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, *_ = make_fs_with_two_duplicate_groups(root)

        result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        assert result.batch_dir == f"{root}/!REMOVE/2026-07-26_10-30"

    def test_moves_every_discard_and_preserves_relative_structure(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, keeper1, discard1, keeper2, discard2 = make_fs_with_two_duplicate_groups(root)

        result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        assert result.moved == 2
        assert result.failed == 0
        assert fs.exists(discard1) is False
        assert fs.exists(discard2) is False
        assert fs.exists(f"{root}/!REMOVE/2026-07-26_10-30/!NEW/Camera/img.jpg") is True
        assert fs.exists(f"{root}/!REMOVE/2026-07-26_10-30/!NEW/Camera/vid.mp4") is True

    def test_keepers_are_never_touched(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, keeper1, discard1, keeper2, discard2 = make_fs_with_two_duplicate_groups(root)

        applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        assert fs.exists(keeper1) is True
        assert fs.exists(keeper2) is True

    def test_manifest_written_inside_batch_dir_by_default(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, *_ = make_fs_with_two_duplicate_groups(root)

        result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        assert result.manifest_path == f"{root}/!REMOVE/2026-07-26_10-30/manifest.csv"

    def test_manifest_rows_have_planned_then_done_for_each_discard(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, keeper1, discard1, *_ = make_fs_with_two_duplicate_groups(root)

        result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        rows = manifest.read_rows(result.manifest_path)
        discard1_rows = [r for r in rows if r.src_abs == discard1]
        assert [r.status for r in discard1_rows] == [manifest.STATUS_PLANNED, manifest.STATUS_DONE]
        assert discard1_rows[0].keeper_abs == keeper1
        assert discard1_rows[0].reason == "rule-0"
        assert discard1_rows[0].hash == _real_hash(b"same-bytes-1")
        assert discard1_rows[0].tool_version == config.TOOL_VERSION


class TestApplyBatchRepeatedClockTimestamp:
    """tidy/review's two apply buttons
    each call apply_batch() with no injected clock, so two quick clicks
    within the same clock-minute are the realistic case, not an edge case
    -- BATCH_DIR_TIMESTAMP_FORMAT is minute-resolution. Each call must still
    get its own batch directory and its own manifest; neither may touch a
    file the other one moved."""

    def test_two_calls_with_same_clock_get_distinct_batch_dirs(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, keeper1, discard1, keeper2, discard2 = make_fs_with_two_duplicate_groups(root)

        result1 = applier.apply_batch(fs, root, [groups[0]], [decisions[0]], clock=fixed_clock())
        result2 = applier.apply_batch(fs, root, [groups[1]], [decisions[1]], clock=fixed_clock())

        assert result1.batch_dir != result2.batch_dir
        assert result1.manifest_path != result2.manifest_path
        assert result1.batch_dir == f"{root}/!REMOVE/2026-07-26_10-30"
        assert result2.batch_dir == f"{root}/!REMOVE/2026-07-26_10-30_2"

    def test_second_batchs_files_land_only_under_its_own_dir(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, keeper1, discard1, keeper2, discard2 = make_fs_with_two_duplicate_groups(root)

        result1 = applier.apply_batch(fs, root, [groups[0]], [decisions[0]], clock=fixed_clock())
        result2 = applier.apply_batch(fs, root, [groups[1]], [decisions[1]], clock=fixed_clock())

        assert result1.moved == 1
        assert result2.moved == 1
        assert fs.exists(f"{result1.batch_dir}/!NEW/Camera/img.jpg") is True
        assert fs.exists(f"{result2.batch_dir}/!NEW/Camera/vid.mp4") is True
        # Batch 2's manifest must not carry batch 1's row, and vice versa.
        rows1 = manifest.read_rows(result1.manifest_path)
        rows2 = manifest.read_rows(result2.manifest_path)
        assert all(r.src_abs != discard2 for r in rows1)
        assert all(r.src_abs != discard1 for r in rows2)


class TestApplyBatchSingleFailureDoesNotAbortBatch:
    def test_one_locked_file_does_not_block_the_rest(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, keeper1, discard1, keeper2, discard2 = make_fs_with_two_duplicate_groups(root)
        fs.set_move_error(discard1, PermissionError("[WinError 32] plik uzywany"))

        result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        assert result.moved == 1
        assert result.failed == 1
        assert fs.exists(discard1) is True  # never moved
        assert fs.exists(discard2) is False  # this one succeeded

        rows = manifest.read_rows(result.manifest_path)
        discard1_rows = [r for r in rows if r.src_abs == discard1]
        assert discard1_rows[-1].status == manifest.STATUS_FAILED
        assert "plik uzywany" in discard1_rows[-1].error


class TestApplyBatchCollisionSuffixing:
    def test_same_relative_name_gets_dup_suffix(self, tmp_path):
        # Two discards whose paths are NOT under archive_root (simulating
        # a multi-root collision scenario in miniature) collide on
        # their fallback (basename-only) relative destination.
        root = _root(tmp_path)
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_dir(f"{root}/Other")
        fs.add_dir(f"{root}/Another")
        keeper_path = f"{root}/keeper.jpg"
        discard_a = f"{root}/Other/img.jpg"
        discard_b = f"{root}/Another/img.jpg"
        fs.add_file(keeper_path, b"k", size=1)
        fs.add_file(discard_a, b"a", size=1)
        fs.add_file(discard_b, b"b", size=1)

        groups = [DuplicateGroup(size=1, full_hash="h", paths=(keeper_path, discard_a, discard_b))]
        decisions = [KeeperDecision(
            keeper=keeper_path,
            discarded=(
                Discard(path=discard_a, reason="dup-exact"),
                Discard(path=discard_b, reason="dup-exact"),
            ),
        )]

        # Use an archive_root that is NOT an ancestor of either discard, so
        # both fall back to their basename-only relative destination and
        # collide -- exercising the __dup2 suffixing.
        result = applier.apply_batch(
            fs, f"{root}/unrelated-root", groups, decisions, clock=fixed_clock()
        )

        assert result.moved == 2
        assert result.failed == 0
        rows = manifest.read_rows(result.manifest_path)
        dst_rels = sorted(r.dst_rel for r in rows if r.status == manifest.STATUS_DONE)
        assert dst_rels == ["img.jpg", "img__dup2.jpg"]


class TestApplyBatchKillMidBatch:
    """Simulate a hard process kill mid-batch via an
    injected non-OSError exception on one specific move()."""

    def test_kill_mid_batch_then_verify_and_restore(self, tmp_path):
        root = _root(tmp_path)
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_dir(f"{root}/!NEW")
        keeper_dir = f"{root}/#Keep"
        fs.add_dir(keeper_dir)

        class _SimulatedKill(BaseException):
            pass

        keepers = []
        discards = []
        for i in range(3):
            k = f"{keeper_dir}/f{i}.jpg"
            d = f"{root}/!NEW/f{i}.jpg"
            fs.add_file(k, f"bytes{i}".encode(), size=6, mtime_ns=i)
            fs.add_file(d, f"bytes{i}".encode(), size=6, mtime_ns=i)
            keepers.append(k)
            discards.append(d)

        groups = [
            DuplicateGroup(size=6, full_hash=f"h{i}", paths=(keepers[i], discards[i]))
            for i in range(3)
        ]
        decisions = [
            KeeperDecision(keeper=keepers[i], discarded=(Discard(path=discards[i], reason="rule-0"),))
            for i in range(3)
        ]
        # Kill on the second discard's move -- the first must complete
        # normally, the third must never even start.
        fs.set_move_error(discards[1], _SimulatedKill())

        manifest_path_holder = {}

        def clock_and_capture():
            return datetime(2026, 7, 26, 10, 30)

        with pytest.raises(_SimulatedKill):
            applier.apply_batch(fs, root, groups, decisions, clock=clock_and_capture)

        manifest_path = f"{root}/!REMOVE/2026-07-26_10-30/manifest.csv"

        # discard 0: fully moved. discard 1: interrupted (still at source,
        # never moved -- it's a rename, not a copy). discard 2: never
        # touched, no manifest row at all.
        assert fs.exists(discards[0]) is False
        assert fs.exists(discards[1]) is True
        assert fs.exists(discards[2]) is True

        rows = manifest.read_rows(manifest_path)
        assert not any(r.src_abs == discards[2] for r in rows)

        verify = applier.verify_manifest(fs, manifest_path)
        assert verify.planned_without_move == (discards[1],)
        assert verify.orphans == ()

        restore_result = applier.restore_batch(fs, manifest_path)
        assert restore_result.refused == 0
        assert fs.exists(keepers[0]) is True
        assert fs.exists(discards[0]) is True  # restored back


class TestRestoreBatch:
    def test_restores_every_done_entry(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, keeper1, discard1, keeper2, discard2 = make_fs_with_two_duplicate_groups(root)
        result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        restore_result = applier.restore_batch(fs, result.manifest_path)

        assert restore_result.restored == 2
        assert restore_result.refused == 0
        assert fs.exists(discard1) is True
        assert fs.exists(discard2) is True
        assert fs.exists(f"{root}/!REMOVE/2026-07-26_10-30/!NEW/Camera/img.jpg") is False

    def test_collision_at_restore_refuses_that_entry_only(self, tmp_path):
        # One file was manually restored (or restore run
        # twice) -- refuse that ONE entry, restore the rest normally.
        root = _root(tmp_path)
        fs, groups, decisions, keeper1, discard1, keeper2, discard2 = make_fs_with_two_duplicate_groups(root)
        result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        # Manually place a file back at discard1's original location.
        fs.add_file(discard1, b"manually restored content", size=25)

        restore_result = applier.restore_batch(fs, result.manifest_path)

        assert restore_result.refused == 1
        assert restore_result.restored == 1
        # The manually restored file must be left untouched.
        assert fs.open_read(discard1).read() == b"manually restored content"
        # discard2 (uninvolved) still restores cleanly.
        assert fs.exists(discard2) is True


class TestVerifyManifest:
    def test_clean_batch_has_no_findings(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, *_ = make_fs_with_two_duplicate_groups(root)
        result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        verify = applier.verify_manifest(fs, result.manifest_path)

        assert verify.planned_without_move == ()
        assert verify.orphans == ()
        assert verify.mismatches == ()

    def test_orphan_file_in_batch_dir_is_reported(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, *_ = make_fs_with_two_duplicate_groups(root)
        result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())
        # Drop an untracked file straight into the batch dir.
        mystery = f"{root}/!REMOVE/2026-07-26_10-30/mystery.jpg"
        fs.add_file(mystery, b"???")

        verify = applier.verify_manifest(fs, result.manifest_path)

        assert mystery in verify.orphans

    def test_size_mismatch_at_destination_is_reported(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, keeper1, discard1, *_ = make_fs_with_two_duplicate_groups(root)
        result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())
        dst = f"{root}/!REMOVE/2026-07-26_10-30/!NEW/Camera/img.jpg"
        # Simulate corruption/truncation after the move: overwrite the fake
        # stat's recorded size directly.
        fs._stats[dst] = fs._stats[dst].__class__(
            **{**fs._stats[dst].__dict__, "size": 999999}
        )

        verify = applier.verify_manifest(fs, result.manifest_path)

        assert any(discard1 in m for m in verify.mismatches)


class TestApplyBatchSeriesPerFileSizeHash:
    """Series discards carry their own size/hash, distinct
    from each other and from the group's shared (bogus, for a series) size
    -- proving apply_batch() actually reads discard.size/discard.hash
    instead of falling back to group.size/group.full_hash. If apply_batch()
    ignored the per-discard override, verify_manifest() would report a size
    mismatch for every series row (a real series group has no single
    correct group.size to fall back to)."""

    def test_series_discards_verify_clean_despite_differing_sizes(self, tmp_path):
        root = _root(tmp_path)
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_dir(f"{root}/#Home/2024 Gdansk")

        keeper = f"{root}/#Home/2024 Gdansk/IMG_001.jpg"
        discard_a = f"{root}/#Home/2024 Gdansk/IMG_001_edit.jpg"
        discard_b = f"{root}/#Home/2024 Gdansk/IMG_001_blur.jpg"
        content_a = b"series-member-a-shorter"
        content_b = b"series-member-b-much-longer-content"
        fs.add_file(keeper, b"series-keeper", size=13, mtime_ns=100)
        fs.add_file(discard_a, content_a, size=len(content_a), mtime_ns=200)
        fs.add_file(discard_b, content_b, size=len(content_b), mtime_ns=300)

        # Deliberately bogus/unrelated shared group size+hash -- a series
        # group has no single correct value here, unlike a byte-identical
        # duplicate group. If apply_batch() fell back to these, every
        # discard row would record the wrong size and verify_manifest()
        # would flag it.
        group = DuplicateGroup(size=999999, full_hash="not-a-real-hash", paths=(keeper, discard_a, discard_b))
        decision = KeeperDecision(
            keeper=keeper,
            discarded=(
                Discard(path=discard_a, reason="dup-visual", size=len(content_a), hash=_real_hash(content_a)),
                Discard(path=discard_b, reason="dup-visual", size=len(content_b), hash=_real_hash(content_b)),
            ),
        )

        result = applier.apply_batch(fs, root, [group], [decision], clock=fixed_clock())
        verify = applier.verify_manifest(fs, result.manifest_path)

        assert result.moved == 2
        assert verify.mismatches == ()

    def test_discard_without_override_falls_back_to_group_size_and_hash(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, *_ = make_fs_with_two_duplicate_groups(root)
        # make_fs_with_two_duplicate_groups()'s Discards omit size/hash --
        # confirms the default (None/"") still resolves to the group's
        # shared values, i.e. plain duplicate-group behaviour is unchanged
        # by the per-discard override.

        result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())
        verify = applier.verify_manifest(fs, result.manifest_path)

        assert result.moved == 2
        assert verify.mismatches == ()


# --- op=move (in-archive sorting) -------------------------------------------
#
# The "Sugestie grupowania" screen can only ever become executable once a
# move *into* the archive is as reversible as a move into !REMOVE. These
# tests are that guarantee -- they lean harder on restore/verify than on the
# move itself, because a move that works but cannot be undone is the failure
# mode that actually costs the user something.

GDANSK_ONE = b"gdansk-one"
GDANSK_TWO = b"gdansk-two"


def make_inbox_fs(root):
    """An archive mid-workflow: two photos dropped in !NEW\\Gdansk, and a
    #Home\\2024 Gdansk folder that already exists -- the "przenies do juz
    istniejacego katalogu" case, which is the common one in this archive."""
    fs = FakeFileSystem()
    fs.add_dir(root)
    fs.add_dir(f"{root}/#Home")
    fs.add_dir(f"{root}/#Home/2024 Gdansk")
    fs.add_dir(f"{root}/!NEW")
    fs.add_dir(f"{root}/!NEW/Gdansk")
    fs.add_file(f"{root}/!NEW/Gdansk/IMG_0001.jpg", GDANSK_ONE, size=10, mtime_ns=111)
    fs.add_file(f"{root}/!NEW/Gdansk/IMG_0002.jpg", GDANSK_TWO, size=10, mtime_ns=222)
    return fs


def gdansk_plans(root):
    dest = f"{root}/#Home/2024 Gdansk"
    return [
        applier.MovePlan(
            src_abs=f"{root}/!NEW/Gdansk/IMG_0001.jpg",
            dst_abs=f"{dest}/IMG_0001.jpg",
            group_id="e1",
        ),
        applier.MovePlan(
            src_abs=f"{root}/!NEW/Gdansk/IMG_0002.jpg",
            dst_abs=f"{dest}/IMG_0002.jpg",
            group_id="e1",
        ),
    ]


def _delete_dir(fs, path):
    """Remove a directory from FakeFileSystem, the way a user tidying up an
    emptied !NEW subfolder in Explorer would. No public fixture method does
    this (nothing in the tool ever deletes a directory), so it reaches for
    the same internals TestVerifyManifest's size-mismatch test already uses."""
    fs._remove_from_parent_children(path)
    fs._stats.pop(path, None)
    fs._children.pop(path, None)


class TestApplyMovesHappyPath:
    def test_moves_files_into_existing_archive_folder(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs(root)

        result = applier.apply_moves(fs, root, gdansk_plans(root), clock=fixed_clock())

        assert result.moved == 2
        assert result.failed == 0
        assert fs.exists(f"{root}/!NEW/Gdansk/IMG_0001.jpg") is False
        assert fs.exists(f"{root}/#Home/2024 Gdansk/IMG_0001.jpg") is True
        assert fs.open_read(f"{root}/#Home/2024 Gdansk/IMG_0002.jpg").read() == GDANSK_TWO

    def test_creates_a_new_destination_folder_when_it_does_not_exist(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        plans = [applier.MovePlan(
            src_abs=f"{root}/!NEW/Gdansk/IMG_0001.jpg",
            dst_abs=f"{root}/#Home/2025 Sopot/IMG_0001.jpg",
        )]

        result = applier.apply_moves(fs, root, plans, clock=fixed_clock())

        assert result.moved == 1
        assert fs.exists(f"{root}/#Home/2025 Sopot") is True
        assert fs.exists(f"{root}/#Home/2025 Sopot/IMG_0001.jpg") is True

    def test_batch_dir_holds_the_manifest_and_never_the_files(self, tmp_path):
        # The structural difference from apply_batch(): !MOVES\<ts>\ is a log
        # location, not a destination. A photo showing up in there would mean
        # a move had silently behaved like a removal.
        root = _root(tmp_path)
        fs = make_inbox_fs(root)

        result = applier.apply_moves(fs, root, gdansk_plans(root), clock=fixed_clock())

        assert result.batch_dir == f"{root}/{config.MOVES_DIR_NAME}/2026-07-26_10-30"
        assert list(fs.scandir(result.batch_dir)) == []
        assert result.manifest_path == f"{result.batch_dir}/{config.MANIFEST_FILENAME}"

    def test_manifest_rows_carry_op_move_and_an_absolute_destination(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs(root)

        result = applier.apply_moves(fs, root, gdansk_plans(root), clock=fixed_clock())
        rows = manifest.read_final_rows(result.manifest_path)

        row = rows[f"{root}/!NEW/Gdansk/IMG_0001.jpg"]
        assert row.op == manifest.OP_MOVE
        assert row.status == manifest.STATUS_DONE
        # Absolute, not batch-relative -- restore has nothing but this path.
        assert row.dst_rel == f"{root}/#Home/2024 Gdansk/IMG_0001.jpg"
        assert row.reason == "event-sort"
        assert row.group_id == "e1"
        assert row.keeper_abs == ""
        assert row.size == 10

    def test_size_is_read_from_disk_when_the_caller_omits_it(self, tmp_path):
        # A placeholder 0 would turn every later --verify-manifest run into a
        # wall of false size mismatches for the whole batch.
        root = _root(tmp_path)
        fs = make_inbox_fs(root)

        result = applier.apply_moves(fs, root, gdansk_plans(root), clock=fixed_clock())
        verify = applier.verify_manifest(fs, result.manifest_path)

        assert verify.mismatches == ()


class TestApplyMovesCollisions:
    def test_existing_different_file_at_destination_is_never_overwritten(self, tmp_path):
        # Post-dedup, a name collision means two genuinely different photos
        # sharing a camera-assigned filename -- both must survive.
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        occupied = f"{root}/#Home/2024 Gdansk/IMG_0001.jpg"
        fs.add_file(occupied, b"a-different-photo", size=17, mtime_ns=999)

        result = applier.apply_moves(fs, root, gdansk_plans(root), clock=fixed_clock())

        assert result.moved == 2
        assert fs.open_read(occupied).read() == b"a-different-photo"
        assert fs.open_read(f"{root}/#Home/2024 Gdansk/IMG_0001__dup2.jpg").read() == GDANSK_ONE

    def test_two_plans_targeting_the_same_name_both_land(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        dest = f"{root}/#Home/2024 Gdansk/IMG_0001.jpg"
        plans = [
            applier.MovePlan(src_abs=f"{root}/!NEW/Gdansk/IMG_0001.jpg", dst_abs=dest),
            applier.MovePlan(src_abs=f"{root}/!NEW/Gdansk/IMG_0002.jpg", dst_abs=dest),
        ]

        result = applier.apply_moves(fs, root, plans, clock=fixed_clock())

        assert result.moved == 2
        assert fs.open_read(dest).read() == GDANSK_ONE
        assert fs.open_read(f"{root}/#Home/2024 Gdansk/IMG_0001__dup2.jpg").read() == GDANSK_TWO


class TestApplyMovesRefusals:
    """Containment invariants. apply_moves() is the layer that actually calls
    fs.move(), so it refuses these itself instead of trusting the review
    screen to have validated a user-edited folder name correctly."""

    def _refuse_one(self, tmp_path, bad_plan):
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        good = applier.MovePlan(
            src_abs=f"{root}/!NEW/Gdansk/IMG_0002.jpg",
            dst_abs=f"{root}/#Home/2024 Gdansk/IMG_0002.jpg",
        )
        result = applier.apply_moves(fs, root, [bad_plan, good], clock=fixed_clock())
        return root, fs, result

    def test_destination_outside_the_archive_is_refused(self, tmp_path):
        root = _root(tmp_path)
        bad = applier.MovePlan(
            src_abs=f"{root}/!NEW/Gdansk/IMG_0001.jpg",
            dst_abs="D:/somewhere/else/IMG_0001.jpg",
        )
        root, fs, result = self._refuse_one(tmp_path, bad)

        assert result.failed == 1
        assert result.moved == 1  # the unrelated plan still went through
        assert fs.exists(f"{root}/!NEW/Gdansk/IMG_0001.jpg") is True
        assert "outside the archive root" in result.entries[0].error

    def test_destination_inside_remove_dir_is_refused(self, tmp_path):
        root = _root(tmp_path)
        bad = applier.MovePlan(
            src_abs=f"{root}/!NEW/Gdansk/IMG_0001.jpg",
            dst_abs=f"{root}/{config.REMOVE_DIR_NAME}/2026-07-26_10-30/IMG_0001.jpg",
        )
        root, fs, result = self._refuse_one(tmp_path, bad)

        assert result.failed == 1
        assert config.REMOVE_DIR_NAME in result.entries[0].error
        assert fs.exists(f"{root}/!NEW/Gdansk/IMG_0001.jpg") is True

    def test_destination_inside_moves_dir_is_refused(self, tmp_path):
        root = _root(tmp_path)
        bad = applier.MovePlan(
            src_abs=f"{root}/!NEW/Gdansk/IMG_0001.jpg",
            dst_abs=f"{root}/{config.MOVES_DIR_NAME}/IMG_0001.jpg",
        )
        root, fs, result = self._refuse_one(tmp_path, bad)

        assert result.failed == 1
        assert config.MOVES_DIR_NAME in result.entries[0].error

    def test_source_outside_the_archive_is_refused(self, tmp_path):
        root = _root(tmp_path)
        bad = applier.MovePlan(
            src_abs="D:/somewhere/else/IMG_0001.jpg",
            dst_abs=f"{root}/#Home/2024 Gdansk/IMG_0001.jpg",
        )
        root, fs, result = self._refuse_one(tmp_path, bad)

        assert result.failed == 1
        assert "source is outside the archive root" in result.entries[0].error

    def test_no_op_move_onto_itself_is_refused(self, tmp_path):
        root = _root(tmp_path)
        same = f"{root}/!NEW/Gdansk/IMG_0001.jpg"
        bad = applier.MovePlan(src_abs=same, dst_abs=same)
        root, fs, result = self._refuse_one(tmp_path, bad)

        assert result.failed == 1
        assert "same path" in result.entries[0].error
        assert fs.exists(same) is True

    def test_refusal_is_recorded_in_the_manifest_not_silently_skipped(self, tmp_path):
        root = _root(tmp_path)
        bad = applier.MovePlan(
            src_abs=f"{root}/!NEW/Gdansk/IMG_0001.jpg",
            dst_abs="D:/somewhere/else/IMG_0001.jpg",
        )
        root, fs, result = self._refuse_one(tmp_path, bad)
        rows = manifest.read_final_rows(result.manifest_path)

        row = rows[f"{root}/!NEW/Gdansk/IMG_0001.jpg"]
        assert row.status == manifest.STATUS_FAILED
        assert "outside the archive root" in row.error

    def test_refused_plan_does_not_reserve_a_name_for_the_next_one(self, tmp_path):
        # A refused plan must not push a later, legitimate plan onto a __dup2
        # suffix by having "claimed" a destination it never used.
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        dest = f"{root}/#Home/2024 Gdansk/IMG_0001.jpg"
        plans = [
            applier.MovePlan(src_abs="D:/outside/IMG_0001.jpg", dst_abs=dest),
            applier.MovePlan(src_abs=f"{root}/!NEW/Gdansk/IMG_0001.jpg", dst_abs=dest),
        ]

        result = applier.apply_moves(fs, root, plans, clock=fixed_clock())

        assert result.failed == 1
        assert result.moved == 1
        assert fs.open_read(dest).read() == GDANSK_ONE
        assert fs.exists(f"{root}/#Home/2024 Gdansk/IMG_0001__dup2.jpg") is False


class TestApplyMovesSingleFailureDoesNotAbortBatch:
    def test_locked_file_is_recorded_failed_and_the_rest_still_moves(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        fs.set_move_error(
            f"{root}/!NEW/Gdansk/IMG_0001.jpg", PermissionError("locked by another process")
        )

        result = applier.apply_moves(fs, root, gdansk_plans(root), clock=fixed_clock())

        assert result.moved == 1
        assert result.failed == 1
        assert fs.exists(f"{root}/!NEW/Gdansk/IMG_0001.jpg") is True
        assert fs.exists(f"{root}/#Home/2024 Gdansk/IMG_0002.jpg") is True
        rows = manifest.read_final_rows(result.manifest_path)
        assert rows[f"{root}/!NEW/Gdansk/IMG_0001.jpg"].status == manifest.STATUS_FAILED


class TestRestoreMoveBatch:
    def test_restores_every_moved_file_to_its_original_location(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        result = applier.apply_moves(fs, root, gdansk_plans(root), clock=fixed_clock())

        restore_result = applier.restore_batch(fs, result.manifest_path)

        assert restore_result.restored == 2
        assert restore_result.refused == 0
        assert fs.open_read(f"{root}/!NEW/Gdansk/IMG_0001.jpg").read() == GDANSK_ONE
        assert fs.exists(f"{root}/#Home/2024 Gdansk/IMG_0001.jpg") is False

    def test_restores_even_after_the_emptied_source_folder_was_deleted(self, tmp_path):
        # Sorting a folder out of !NEW empties it, and an emptied folder is
        # exactly what a user tidies away by hand afterwards. Without the
        # parent recreation in restore_batch(), that ordinary housekeeping
        # would leave the batch permanently unrestorable.
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        result = applier.apply_moves(fs, root, gdansk_plans(root), clock=fixed_clock())
        _delete_dir(fs, f"{root}/!NEW/Gdansk")
        assert fs.exists(f"{root}/!NEW/Gdansk") is False

        restore_result = applier.restore_batch(fs, result.manifest_path)

        assert restore_result.restored == 2
        assert restore_result.refused == 0
        assert fs.open_read(f"{root}/!NEW/Gdansk/IMG_0001.jpg").read() == GDANSK_ONE

    def test_refuses_the_one_entry_whose_source_reappeared(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        result = applier.apply_moves(fs, root, gdansk_plans(root), clock=fixed_clock())
        fs.add_file(f"{root}/!NEW/Gdansk/IMG_0001.jpg", b"put back by hand", size=16)

        restore_result = applier.restore_batch(fs, result.manifest_path)

        assert restore_result.refused == 1
        assert restore_result.restored == 1
        assert fs.open_read(f"{root}/!NEW/Gdansk/IMG_0001.jpg").read() == b"put back by hand"
        # The moved copy is left where it is rather than destroyed.
        assert fs.exists(f"{root}/#Home/2024 Gdansk/IMG_0001.jpg") is True

    def test_failed_rows_are_not_restored(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        bad = applier.MovePlan(
            src_abs=f"{root}/!NEW/Gdansk/IMG_0001.jpg",
            dst_abs="D:/somewhere/else/IMG_0001.jpg",
        )
        good = applier.MovePlan(
            src_abs=f"{root}/!NEW/Gdansk/IMG_0002.jpg",
            dst_abs=f"{root}/#Home/2024 Gdansk/IMG_0002.jpg",
        )
        result = applier.apply_moves(fs, root, [bad, good], clock=fixed_clock())

        restore_result = applier.restore_batch(fs, result.manifest_path)

        assert restore_result.restored == 1
        assert restore_result.refused == 0


class TestVerifyMoveManifest:
    def test_clean_move_batch_reports_nothing(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        result = applier.apply_moves(fs, root, gdansk_plans(root), clock=fixed_clock())

        verify = applier.verify_manifest(fs, result.manifest_path)

        assert verify.planned_without_move == ()
        assert verify.mismatches == ()
        # No false orphans: an op=move destination lives out in the archive,
        # so an empty batch dir is the correct clean state here.
        assert verify.orphans == ()

    def test_size_mismatch_at_the_absolute_destination_is_reported(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        result = applier.apply_moves(fs, root, gdansk_plans(root), clock=fixed_clock())
        dst = f"{root}/#Home/2024 Gdansk/IMG_0001.jpg"
        fs._stats[dst] = fs._stats[dst].__class__(
            **{**fs._stats[dst].__dict__, "size": 999999}
        )

        verify = applier.verify_manifest(fs, result.manifest_path)

        assert any(f"{root}/!NEW/Gdansk/IMG_0001.jpg" in m for m in verify.mismatches)

    def test_destination_gone_missing_is_reported(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs(root)
        result = applier.apply_moves(fs, root, gdansk_plans(root), clock=fixed_clock())
        dst = f"{root}/#Home/2024 Gdansk/IMG_0001.jpg"
        fs.set_stat_error(dst, FileNotFoundError("deleted after the move"))

        verify = applier.verify_manifest(fs, result.manifest_path)

        assert any("destination missing" in m for m in verify.mismatches)


class TestEnsureArchiveDirs:
    def test_creates_all_missing_dirs_and_sentinel(self):
        fs = FakeFileSystem().add_dir("D:/Archive")

        created = applier.ensure_archive_dirs(fs, "D:/Archive")

        assert created == [config.NEW_DIR_NAME, config.MOVES_DIR_NAME, config.REMOVE_DIR_NAME]
        for name in created:
            assert fs.stat(f"D:/Archive/{name}").is_dir
        assert fs.exists(f"D:/Archive/!REMOVE/{config.KOSZ_SENTINEL_FILENAME}")

    def test_existing_dirs_are_left_alone(self):
        fs = FakeFileSystem().add_dir("D:/Archive").add_dir("D:/Archive/!NEW")
        fs.add_file("D:/Archive/!NEW/a.jpg", b"x")

        created = applier.ensure_archive_dirs(fs, "D:/Archive")

        assert created == [config.MOVES_DIR_NAME, config.REMOVE_DIR_NAME]
        assert fs.exists("D:/Archive/!NEW/a.jpg")
        assert applier.ensure_archive_dirs(fs, "D:/Archive") == []

    def test_missing_archive_root_creates_nothing(self):
        fs = FakeFileSystem()

        assert applier.ensure_archive_dirs(fs, "D:/Nowhere") == []
        assert not fs.exists("D:/Nowhere")
        assert fs.move_calls == []


def _write_crafted_manifest(batch_dir, rows):
    path = f"{batch_dir}/{config.MANIFEST_FILENAME}"
    for row in rows:
        manifest.append_planned(path, row)
        manifest.append_confirmation(
            path, manifest.ManifestRow(**{**row.__dict__, "status": manifest.STATUS_DONE}),
        )
    return path


def _row(op, src, dst_rel):
    return manifest.ManifestRow(
        op=op, src_abs=src, dst_rel=dst_rel, size=1, mtime=1, hash="", group_id=1,
        keeper_abs="", reason="x", ts="2026-01-01T00:00:00", tool_version="t",
        status=manifest.STATUS_PLANNED,
    )


class TestRestoreRefusesUntrustedManifestPaths:
    def _setup(self, tmp_path):
        import os
        root = _root(tmp_path)
        outside = f"{root}_outside"
        batch = f"{root}/!REMOVE/b1"
        fs = FakeFileSystem()
        for d in (root, outside, f"{root}/!REMOVE", batch, f"{root}/!NEW"):
            fs.add_dir(d)
        os.makedirs(batch, exist_ok=True)
        return root, outside, batch, fs

    def test_dotdot_in_remove_dst_rel_is_refused(self, tmp_path):
        root, outside, batch, fs = self._setup(tmp_path)
        fs.add_file(f"{outside}/x.jpg", b"secret", size=6)
        path = _write_crafted_manifest(batch, [
            _row(manifest.OP_REMOVE, f"{root}/!NEW/x.jpg", "..\\..\\tmp_outside/x.jpg"),
        ])

        result = applier.restore_batch(fs, path, archive_root=root)

        assert result.restored == 0 and result.refused == 1
        assert "unsafe" in result.entries[0].detail
        assert fs.exists(f"{outside}/x.jpg") is True
        assert fs.exists(f"{root}/!NEW/x.jpg") is False

    def test_absolute_and_drive_dst_rel_are_refused(self, tmp_path):
        root, outside, batch, fs = self._setup(tmp_path)
        path = _write_crafted_manifest(batch, [
            _row(manifest.OP_REMOVE, f"{root}/!NEW/a.jpg", "C:/Windows/a.jpg"),
            _row(manifest.OP_REMOVE, f"{root}/!NEW/b.jpg", "/etc/b.jpg"),
        ])

        result = applier.restore_batch(fs, path, archive_root=root)

        assert result.refused == 2 and result.restored == 0

    def test_src_outside_archive_is_refused(self, tmp_path):
        root, outside, batch, fs = self._setup(tmp_path)
        fs.add_file(f"{batch}/x.jpg", b"data", size=4)
        path = _write_crafted_manifest(batch, [
            _row(manifest.OP_REMOVE, f"{outside}/x.jpg", "x.jpg"),
        ])

        result = applier.restore_batch(fs, path, archive_root=root)

        assert result.refused == 1
        assert "outside the archive" in result.entries[0].detail
        assert fs.exists(f"{outside}/x.jpg") is False
        assert fs.exists(f"{batch}/x.jpg") is True

    def test_move_destination_outside_archive_is_refused(self, tmp_path):
        root, outside, batch, fs = self._setup(tmp_path)
        fs.add_file(f"{outside}/y.jpg", b"data", size=4)
        path = _write_crafted_manifest(batch, [
            _row(manifest.OP_MOVE, f"{root}/!NEW/y.jpg", f"{outside}/y.jpg"),
        ])

        result = applier.restore_batch(fs, path, archive_root=root)

        assert result.refused == 1
        assert fs.exists(f"{outside}/y.jpg") is True

    def test_dotdot_climbing_out_of_a_matching_prefix_is_refused(self, tmp_path):
        root, outside, batch, fs = self._setup(tmp_path)
        fs.add_file(f"{batch}/x.jpg", b"data", size=4)
        fs.add_file(f"{outside}/y.jpg", b"data", size=4)
        path = _write_crafted_manifest(batch, [
            _row(manifest.OP_REMOVE, f"{root}/../escaped/x.jpg", "x.jpg"),
            _row(manifest.OP_MOVE, f"{root}/!NEW/y.jpg", f"{root}/../escaped/y.jpg"),
        ])

        result = applier.restore_batch(fs, path, archive_root=root)

        assert result.refused == 2 and result.restored == 0
        assert all("outside the archive" in e.detail for e in result.entries)

    def test_verify_manifest_reports_unsafe_row(self, tmp_path):
        root, outside, batch, fs = self._setup(tmp_path)
        path = _write_crafted_manifest(batch, [
            _row(manifest.OP_REMOVE, f"{root}/!NEW/x.jpg", "../../x.jpg"),
        ])

        report = applier.verify_manifest(fs, path, archive_root=root)

        assert any("refused" in m for m in report.mismatches)

    def test_legit_batch_still_restores_with_archive_root(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, keeper1, discard1, *_ = make_fs_with_two_duplicate_groups(root)
        result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        restore_result = applier.restore_batch(fs, result.manifest_path, archive_root=root)

        assert restore_result.restored == 2 and restore_result.refused == 0
