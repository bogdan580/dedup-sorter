"""Tests for batches.py: list_batches() --
the "Cofnij" tab's on-disk inventory of !MOVES/!REMOVE batches.

Same archive_root convention as tests/test_applier.py: `fs` is a
FakeFileSystem (no real file is ever moved by these tests), but
`archive_root` is a real pytest tmp_path-backed directory, because
manifest.py deliberately writes manifest.csv with plain open() rather than
through the `fs` seam (see manifest.py's own module docstring). Using
tmp_path here reproduces exactly the coupling production code has: the
manifest lives on the same real disk as the batch directory `fs.makedirs()`
just created inside FakeFileSystem's in-memory model.
"""
from datetime import datetime

from tidy.moves import applier
from tidy.moves import batches
from tidy import config
from tidy.moves import manifest
from fakes import FakeFileSystem
from tidy.duplicates.keeper import Discard, KeeperDecision
from tidy.duplicates.tiers import DuplicateGroup


def fixed_clock(dt=datetime(2026, 7, 26, 10, 30)):
    return lambda: dt


def _root(tmp_path) -> str:
    return str(tmp_path).replace("\\", "/")


def make_fs_with_one_duplicate_group(root):
    fs = FakeFileSystem()
    fs.add_dir(root)
    fs.add_dir(f"{root}/#Travel")
    fs.add_dir(f"{root}/!NEW")

    keeper = f"{root}/#Travel/img.jpg"
    discard = f"{root}/!NEW/img.jpg"
    fs.add_file(keeper, b"same-bytes", size=10, mtime_ns=100)
    fs.add_file(discard, b"same-bytes", size=10, mtime_ns=200)

    groups = [DuplicateGroup(size=10, full_hash="deadbeef", paths=(keeper, discard))]
    decisions = [KeeperDecision(keeper=keeper, discarded=(Discard(path=discard, reason="rule-0"),))]
    return fs, groups, decisions, keeper, discard


def make_inbox_fs_for_move(root):
    fs = FakeFileSystem()
    fs.add_dir(root)
    fs.add_dir(f"{root}/#Home")
    fs.add_dir(f"{root}/#Home/2024 Gdansk")
    fs.add_dir(f"{root}/!NEW")
    fs.add_dir(f"{root}/!NEW/Gdansk")
    fs.add_file(f"{root}/!NEW/Gdansk/IMG_0001.jpg", b"one", size=3, mtime_ns=111)
    return fs


def gdansk_move_plan(root):
    return [applier.MovePlan(
        src_abs=f"{root}/!NEW/Gdansk/IMG_0001.jpg",
        dst_abs=f"{root}/#Home/2024 Gdansk/IMG_0001.jpg",
        group_id="e1",
    )]


def _write_broken_manifest(path: str) -> None:
    """A manifest.csv whose SECOND line is malformed and is NOT the last
    line -- manifest.read_rows() only forgives a torn LAST line (its own
    crash-safety story), so this is real corruption, not a benign partial
    write, and must surface as BatchSummary.error rather than raise."""
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(";".join(manifest.MANIFEST_COLUMNS) + "\n")
        f.write("only-one-field\n")
        f.write(
            "remove;C:/a/b.jpg;b.jpg;10;100;hash;1;C:/keep.jpg;rule-0;"
            "2026-07-26T10:30:00;0.3.0;done;\n"
        )


class TestListBatches:
    def test_lists_an_op_remove_batch_with_its_done_count(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, keeper, discard = make_fs_with_one_duplicate_group(root)
        applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        result = batches.list_batches(fs, root)

        assert len(result) == 1
        assert result[0].kind == "remove"
        assert result[0].dir_name == "2026-07-26_10-30"
        assert result[0].done == 1
        assert result[0].failed == 0
        assert result[0].restorable == 1
        assert result[0].already_restored is False
        assert result[0].error == ""

    def test_lists_an_op_move_batch_with_its_done_count(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs_for_move(root)
        applier.apply_moves(fs, root, gdansk_move_plan(root), clock=fixed_clock())

        result = batches.list_batches(fs, root)

        assert len(result) == 1
        assert result[0].kind == "move"
        assert result[0].done == 1
        assert result[0].restorable == 1

    def test_a_directory_without_a_manifest_is_skipped(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, *_ = make_fs_with_one_duplicate_group(root)
        applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())
        # A stray !REMOVE subdirectory with no manifest.csv -- e.g. a batch
        # from a tool version this codebase never wrote, or one mid-cleanup.
        fs.add_dir(f"{root}/{config.REMOVE_DIR_NAME}/not-a-batch")
        fs.add_file(f"{root}/{config.REMOVE_DIR_NAME}/not-a-batch/stray.jpg", b"x")

        result = batches.list_batches(fs, root)

        assert len(result) == 1
        assert result[0].dir_name == "2026-07-26_10-30"

    def test_a_batch_already_restored_has_restorable_zero(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, *_ = make_fs_with_one_duplicate_group(root)
        apply_result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())
        applier.restore_batch(fs, apply_result.manifest_path)

        result = batches.list_batches(fs, root)

        assert result[0].done == 1
        assert result[0].restorable == 0
        assert result[0].already_restored is True

    def test_planned_and_failed_rows_do_not_count_toward_restorable(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs_for_move(root)
        fs.set_move_error(
            f"{root}/!NEW/Gdansk/IMG_0001.jpg", PermissionError("locked"),
        )
        applier.apply_moves(fs, root, gdansk_move_plan(root), clock=fixed_clock())

        result = batches.list_batches(fs, root)

        assert result[0].done == 0
        assert result[0].failed == 1
        assert result[0].restorable == 0
        # Nothing ever succeeded, so this is not "already restored" either --
        # see batches.py's own note on why the two must not be conflated.
        assert result[0].already_restored is False

    def test_a_corrupted_manifest_is_reported_as_an_error_entry_not_raised(self, tmp_path):
        root = _root(tmp_path)
        fs = FakeFileSystem()
        fs.add_dir(root)
        batch_dir = f"{root}/{config.MOVES_DIR_NAME}/2026-01-01_00-00"
        fs.add_dir(f"{root}/{config.MOVES_DIR_NAME}")
        fs.add_dir(batch_dir)
        manifest_path = f"{batch_dir}/{config.MANIFEST_FILENAME}"
        _write_broken_manifest(manifest_path)

        result = batches.list_batches(fs, root)

        assert len(result) == 1
        assert result[0].error != ""
        assert result[0].done == 0
        assert result[0].restorable == 0
        assert result[0].already_restored is False

    def test_list_batches_finds_batch_when_scandir_yields_prefixed_entry_path(self, tmp_path):
        # Regression: on the real archive,
        # platform_win.WindowsFileSystem.scandir() yields entry.path already
        # carrying the `\\?\` long-path prefix (real backslashes). Before
        # to_extended()/batches._join() were fixed to normalize/join a
        # prefixed base correctly, this exact shape made GET /api/batches
        # return `{"batches": []}` against a real 23-file batch --
        # FakeFileSystem never produced the prefix, so no test caught it.
        # tmp_path (not FakeFileSystem's own in-memory model) backs
        # manifest_path here for the same reason every other test in this
        # file does: manifest.py writes/reads manifest.csv with plain
        # open(), bypassing the fs seam entirely (see this module's own
        # docstring and manifest.py's).
        root = _root(tmp_path)
        backslash_root = str(tmp_path)
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_dir(f"{root}/{config.MOVES_DIR_NAME}")
        batch_dir = f"{root}/{config.MOVES_DIR_NAME}/2026-08-17_22-31"
        prefixed_entry_path = (
            "\\\\?\\" + f"{backslash_root}\\{config.MOVES_DIR_NAME}\\2026-08-17_22-31"
        )
        fs.add_dir(batch_dir, entry_path=prefixed_entry_path)

        # A single status=failed row -- deliberately NOT status=done, so
        # this test isolates the scandir-prefix / _join() / os.path.exists()
        # path (Defect 1's actual failure point) without also exercising
        # applier.resolve_dst()'s fs.exists() pair, which FakeFileSystem's
        # in-memory model (keyed by its own plain "/" paths, never the
        # prefixed entry_path override above) cannot resolve correctly --
        # that mismatch is a FakeFileSystem-fidelity limit, not the bug
        # under test here.
        import os
        manifest_path = f"{batch_dir}/{config.MANIFEST_FILENAME}"
        os.makedirs(batch_dir, exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(";".join(manifest.MANIFEST_COLUMNS) + "\n")
            f.write(
                "move;C:/a/b.jpg;C:/dest/b.jpg;10;100;hash;1;;rule-0;"
                "2026-08-17T22:31:00;0.3.0;failed;locked\n"
            )

        result = batches.list_batches(fs, root)

        assert len(result) == 1
        assert result[0].dir_name == "2026-08-17_22-31"
        assert result[0].kind == "move"
        assert result[0].failed == 1
        assert result[0].error == ""

    def test_newest_batch_sorts_first_across_both_kinds(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, *_ = make_fs_with_one_duplicate_group(root)
        applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock(datetime(2026, 1, 1, 9, 0)))
        fs2 = make_inbox_fs_for_move(root)
        # Same fs instance carries both fixtures forward -- reuse the first
        # fs (which already has the !REMOVE batch) rather than fs2, so both
        # batches are visible to a single list_batches() call.
        fs.add_dir(f"{root}/#Home")
        fs.add_dir(f"{root}/#Home/2024 Gdansk")
        fs.add_dir(f"{root}/!NEW/Gdansk")
        fs.add_file(f"{root}/!NEW/Gdansk/IMG_0001.jpg", b"one", size=3, mtime_ns=111)
        applier.apply_moves(
            fs, root, gdansk_move_plan(root), clock=fixed_clock(datetime(2026, 6, 1, 9, 0)),
        )

        result = batches.list_batches(fs, root)

        assert len(result) == 2
        assert result[0].kind == "move"  # 2026-06 batch, newer
        assert result[1].kind == "remove"  # 2026-01 batch, older


class TestListBatchEntries:
    """list_batch_entries() ("chce miec podglad miniaturek"
    -- the "Cofnij" tab's lazy, per-batch file list)."""

    def test_lists_only_done_rows_with_resolved_path_and_size(self, tmp_path):
        root = _root(tmp_path)
        fs, groups, decisions, keeper, discard = make_fs_with_one_duplicate_group(root)
        apply_result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())

        entries = batches.list_batch_entries(fs, apply_result.manifest_path)

        assert len(entries) == 1
        assert entries[0].size == 10
        assert entries[0].restorable is True
        # resolve_dst() for op=remove: batch_dir joined with dst_rel --
        # never the original discard path (that would defeat the whole
        # point: rendering the file where it is NOW, not where it was).
        assert entries[0].path != discard
        assert entries[0].path.startswith(apply_result.batch_dir)

    def test_planned_and_failed_rows_are_excluded(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs_for_move(root)
        fs.set_move_error(
            f"{root}/!NEW/Gdansk/IMG_0001.jpg", PermissionError("locked"),
        )
        apply_result = applier.apply_moves(fs, root, gdansk_move_plan(root), clock=fixed_clock())

        entries = batches.list_batch_entries(fs, apply_result.manifest_path)

        assert entries == []

    def test_restorable_matches_list_batches_restorable_count(self, tmp_path):
        # Same pair of fs.exists() checks as BatchSummary.restorable (module
        # docstring) -- an entry marked restorable here must never disagree
        # with the count the "Cofnij" row itself already promised.
        root = _root(tmp_path)
        fs, groups, decisions, *_ = make_fs_with_one_duplicate_group(root)
        apply_result = applier.apply_batch(fs, root, groups, decisions, clock=fixed_clock())
        applier.restore_batch(fs, apply_result.manifest_path)

        summary = batches.list_batches(fs, root)[0]
        entries = batches.list_batch_entries(fs, apply_result.manifest_path)

        assert summary.restorable == 0
        assert len(entries) == 1
        assert entries[0].restorable is False

    def test_op_move_batch_resolves_to_the_in_archive_destination(self, tmp_path):
        root = _root(tmp_path)
        fs = make_inbox_fs_for_move(root)
        apply_result = applier.apply_moves(fs, root, gdansk_move_plan(root), clock=fixed_clock())

        entries = batches.list_batch_entries(fs, apply_result.manifest_path)

        assert len(entries) == 1
        assert entries[0].path == f"{root}/#Home/2024 Gdansk/IMG_0001.jpg"
        assert entries[0].restorable is True
