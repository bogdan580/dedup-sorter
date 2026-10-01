"""Tests for scanner.py: recursive walk, exclusion/pruning, reparse-point,
hard-link and offline classification, and top-level error resilience.

All filesystem interaction goes through FakeFileSystem (tests/fakes.py) --
no real path on this machine, let alone the real archive, is ever touched.
"""
import pytest

from tidy import config
from tidy.duplicates import scanner
from fakes import FakeFileSystem


def paths(scanned_files):
    return {f.path for f in scanned_files}


def candidates(result):
    return {f.path for f in result.files if f.is_duplicate_candidate}


class TestPathHandling:
    def test_cyrillic_and_long_paths_scan_correctly(self):
        fs = FakeFileSystem()
        root = "C:/Photo Archive"
        fs.add_dir(root)
        long_name = "a" * 300 + ".jpg"
        long_path = f"{root}/{long_name}"
        fs.add_file(long_path, b"data", volume_id=1, file_index=1)
        cyr_path = f"{root}/#Місто/2019 Місто-Море/фото.jpg"
        fs.add_dir(f"{root}/#Місто")
        fs.add_dir(f"{root}/#Місто/2019 Місто-Море")
        fs.add_file(cyr_path, b"data2", volume_id=1, file_index=2)

        result = scanner.scan(fs, [root])

        found = paths(result.files)
        assert long_path in found
        assert cyr_path in found
        assert not result.errors


class TestExclusionAndPruning:
    def test_remove_dir_refused_even_when_explicitly_targeted(self):
        fs = FakeFileSystem()
        fs.add_dir("!REMOVE")
        fs.add_file("!REMOVE/2026-01-01/x.jpg", b"x")

        with pytest.raises(scanner.RemoveDirRefusedError):
            scanner.scan(fs, ["!REMOVE"])

    def test_remove_dir_pruned_during_walk(self):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_dir(f"{root}/!REMOVE")
        fs.add_file(f"{root}/!REMOVE/2026-01-01/x.jpg", b"x")
        fs.add_file(f"{root}/keep.jpg", b"keep")

        result = scanner.scan(fs, [root])

        found = paths(result.files)
        assert f"{root}/keep.jpg" in found
        assert not any(p.startswith(f"{root}/!REMOVE") for p in found)

    def test_kosz_sentinel_blocks_entry_even_if_dir_renamed(self):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        renamed = f"{root}/old_trash_renamed"
        fs.add_dir(renamed)
        fs.add_file(f"{renamed}/{config.KOSZ_SENTINEL_FILENAME}", b"")
        fs.add_file(f"{renamed}/secret.jpg", b"secret")
        fs.add_file(f"{root}/keep.jpg", b"keep")

        result = scanner.scan(fs, [root])

        found = paths(result.files)
        assert f"{root}/keep.jpg" in found
        assert f"{renamed}/secret.jpg" not in found

    def test_dedup_and_video_ts_dirs_pruned(self):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_dir(f"{root}/.dedup")
        fs.add_file(f"{root}/.dedup/cache.sqlite3", b"db")
        fs.add_dir(f"{root}/Movie/VIDEO_TS")
        fs.add_dir(f"{root}/Movie")
        fs.add_file(f"{root}/Movie/VIDEO_TS/VTS_01_0.IFO", b"ifo")
        fs.add_file(f"{root}/keep.jpg", b"keep")

        result = scanner.scan(fs, [root])

        found = paths(result.files)
        assert f"{root}/keep.jpg" in found
        assert f"{root}/.dedup/cache.sqlite3" not in found
        assert f"{root}/Movie/VIDEO_TS/VTS_01_0.IFO" not in found

    @pytest.mark.parametrize("name", ["desktop.ini", "Thumbs.db", "notes.db", "photo.AAE", "photo.xmp"])
    def test_inventory_only_files_excluded_from_duplicate_candidates(self, name):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_file(f"{root}/{name}", b"content")

        result = scanner.scan(fs, [root])

        matching = [f for f in result.files if f.path == f"{root}/{name}"]
        assert len(matching) == 1
        assert matching[0].is_duplicate_candidate is False
        assert matching[0].exclusion_reason == "inventory-only"

    def test_zero_byte_files_are_report_only(self):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_file(f"{root}/empty.jpg", b"")

        result = scanner.scan(fs, [root])

        matching = [f for f in result.files if f.path == f"{root}/empty.jpg"]
        assert len(matching) == 1
        assert matching[0].is_duplicate_candidate is False
        assert matching[0].exclusion_reason == "zero-byte"


class TestReparsePoints:
    def test_reparse_dir_not_descended_and_recorded(self):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_dir(f"{root}/junction", is_reparse_point=True)
        fs.add_file(f"{root}/junction/hidden.jpg", b"hidden")
        fs.add_file(f"{root}/keep.jpg", b"keep")

        result = scanner.scan(fs, [root])

        found = paths(result.files)
        assert f"{root}/keep.jpg" in found
        assert f"{root}/junction/hidden.jpg" not in found
        assert f"{root}/junction" in result.skipped_reparse


class TestHardLinks:
    def test_hardlinks_collapse_to_one_representative_never_duplicates(self):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_file(f"{root}/a.jpg", b"same-bytes", volume_id=1, file_index=99, nlink=2)
        fs.add_file(f"{root}/b.jpg", b"same-bytes", volume_id=1, file_index=99, nlink=2)

        result = scanner.scan(fs, [root])

        cand = candidates(result)
        # Exactly one of the two hard-linked names is a duplicate candidate.
        assert len(cand & {f"{root}/a.jpg", f"{root}/b.jpg"}) == 1

        non_candidate = [
            f for f in result.files
            if f.path in (f"{root}/a.jpg", f"{root}/b.jpg") and not f.is_duplicate_candidate
        ]
        assert len(non_candidate) == 1
        assert non_candidate[0].exclusion_reason == "hardlink-of"
        assert non_candidate[0].hardlink_of in (f"{root}/a.jpg", f"{root}/b.jpg")


class TestOfflineFiles:
    def test_offline_file_never_passed_to_open_read_and_recorded(self):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_file(f"{root}/cloud.jpg", b"", attributes=0x1000)
        del fs._contents[f"{root}/cloud.jpg"]  # ensure open_read would blow up if called

        result = scanner.scan(fs, [root])

        assert f"{root}/cloud.jpg" in result.skipped_offline
        assert fs.open_read_calls == []
        matching = [f for f in result.files if f.path == f"{root}/cloud.jpg"]
        assert len(matching) == 1
        assert matching[0].is_duplicate_candidate is False
        assert matching[0].exclusion_reason == "offline"


class TestErrorHandling:
    def test_scandir_error_on_a_directory_does_not_abort_whole_scan(self):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_dir(f"{root}/locked")
        fs.add_file(f"{root}/keep.jpg", b"keep")

        # Simulate scandir() itself failing for a locked directory by
        # monkeypatching just that one path's behavior.
        original_scandir = fs.scandir

        def flaky_scandir(path):
            if path == f"{root}/locked":
                raise PermissionError("Access is denied")
            return original_scandir(path)

        fs.scandir = flaky_scandir  # type: ignore[assignment]

        result = scanner.scan(fs, [root])

        found = paths(result.files)
        assert f"{root}/keep.jpg" in found
        assert any(e.path == f"{root}/locked" for e in result.errors)

    def test_root_stat_error_is_recorded_not_raised(self):
        fs = FakeFileSystem()
        fs.set_stat_error("D:/GHOST", FileNotFoundError("no such root"))

        result = scanner.scan(fs, ["D:/GHOST"])

        assert any(e.path == "D:/GHOST" for e in result.errors)
        assert result.files == []

    def test_nothing_raises_out_of_top_level_scan(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/ARCHIVE")
        fs.set_stat_error("D:/ARCHIVE", OSError("boom"))
        # Should not raise -- errors are captured in the result.
        result = scanner.scan(fs, ["D:/ARCHIVE"])
        assert isinstance(result, scanner.ScanResult)


class TestOverlappingRootsRegression:
    """Regression coverage for the review finding: overlapping --roots
    walked a nested subtree twice, producing two ScannedFile entries for
    the identical path -- and downstream, a fabricated duplicate group
    containing that file paired with itself."""

    def test_root_nested_inside_another_root_is_scanned_only_once(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/ARCHIVE")
        fs.add_dir("D:/ARCHIVE/Travel")
        fs.add_file("D:/ARCHIVE/Travel/unique_photo.jpg", b"data", volume_id=1, file_index=1)

        result = scanner.scan(fs, ["D:/ARCHIVE", "D:/ARCHIVE/Travel"])

        matching = [f for f in result.files if f.path == "D:/ARCHIVE/Travel/unique_photo.jpg"]
        assert len(matching) == 1

    def test_order_of_overlapping_roots_does_not_matter(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/ARCHIVE")
        fs.add_dir("D:/ARCHIVE/Travel")
        fs.add_file("D:/ARCHIVE/Travel/unique_photo.jpg", b"data", volume_id=1, file_index=1)

        result = scanner.scan(fs, ["D:/ARCHIVE/Travel", "D:/ARCHIVE"])

        matching = [f for f in result.files if f.path == "D:/ARCHIVE/Travel/unique_photo.jpg"]
        assert len(matching) == 1

    def test_duplicate_root_passed_twice_is_scanned_once(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/ARCHIVE")
        fs.add_file("D:/ARCHIVE/photo.jpg", b"data", volume_id=1, file_index=1)

        result = scanner.scan(fs, ["D:/ARCHIVE", "D:/ARCHIVE"])

        assert len([f for f in result.files if f.path == "D:/ARCHIVE/photo.jpg"]) == 1

    def test_sibling_non_overlapping_roots_are_both_scanned(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/ARCHIVE")
        fs.add_dir("D:/ARCHIVE/A")
        fs.add_dir("D:/ARCHIVE/B")
        fs.add_file("D:/ARCHIVE/A/one.jpg", b"a", volume_id=1, file_index=1)
        fs.add_file("D:/ARCHIVE/B/two.jpg", b"b", volume_id=1, file_index=2)

        result = scanner.scan(fs, ["D:/ARCHIVE/A", "D:/ARCHIVE/B"])

        found = paths(result.files)
        assert "D:/ARCHIVE/A/one.jpg" in found
        assert "D:/ARCHIVE/B/two.jpg" in found


class TestRenamedRemoveTrashAncestorGuard:
    """Regression coverage for the review finding: a --roots value pointed
    at a subdirectory *inside* a renamed/relocated !REMOVE trash folder
    bypassed both the name guard (no '!REMOVE' text in the path) and the
    sentinel guard (only checked the exact directory being scanned, not
    its ancestors)."""

    def test_root_inside_renamed_trash_batch_dir_is_refused(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/ARCHIVE")
        fs.add_dir("D:/ARCHIVE/OldTrash_renamed_from_REMOVE")
        fs.add_file("D:/ARCHIVE/OldTrash_renamed_from_REMOVE/.dedup-kosz", b"")
        fs.add_dir("D:/ARCHIVE/OldTrash_renamed_from_REMOVE/2026-01-01_00-00")
        fs.add_file(
            "D:/ARCHIVE/OldTrash_renamed_from_REMOVE/2026-01-01_00-00/trashed_photo.jpg",
            b"data", volume_id=1, file_index=1,
        )

        result = scanner.scan(fs, ["D:/ARCHIVE/OldTrash_renamed_from_REMOVE/2026-01-01_00-00"])

        assert result.files == []
        assert "D:/ARCHIVE/OldTrash_renamed_from_REMOVE/2026-01-01_00-00" in result.skipped_kosz

    def test_normal_scan_from_above_is_unaffected(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/ARCHIVE")
        fs.add_dir("D:/ARCHIVE/Travel")
        fs.add_file("D:/ARCHIVE/Travel/photo.jpg", b"data", volume_id=1, file_index=1)

        result = scanner.scan(fs, ["D:/ARCHIVE"])

        assert "D:/ARCHIVE/Travel/photo.jpg" in paths(result.files)
        assert result.skipped_kosz == []


class TestExtraExcludedDirNames:
    """--exclude: user-supplied extra pruned directory
    names, additive on top of config.EXCLUDED_DIR_NAMES_CASEFOLD -- never a
    replacement."""

    def test_extra_excluded_dir_is_pruned_and_files_under_it_absent(self):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_dir(f"{root}/JunkFolder")
        fs.add_file(f"{root}/JunkFolder/junk.jpg", b"junk")
        fs.add_file(f"{root}/keep.jpg", b"keep")

        result = scanner.scan(fs, [root], extra_excluded_dir_names=["JunkFolder"])

        found = paths(result.files)
        assert f"{root}/keep.jpg" in found
        assert f"{root}/JunkFolder/junk.jpg" not in found
        assert f"{root}/JunkFolder" in result.pruned_dirs

    def test_extra_excluded_dir_name_is_casefolded(self):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_dir(f"{root}/JunkFolder")
        fs.add_file(f"{root}/JunkFolder/junk.jpg", b"junk")

        result = scanner.scan(fs, [root], extra_excluded_dir_names=["junkfolder"])

        assert f"{root}/JunkFolder/junk.jpg" not in paths(result.files)

    def test_without_extra_excluded_dir_names_behavior_is_unchanged(self):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_dir(f"{root}/NotExcluded")
        fs.add_file(f"{root}/NotExcluded/photo.jpg", b"photo")

        result = scanner.scan(fs, [root])

        assert f"{root}/NotExcluded/photo.jpg" in paths(result.files)

    def test_extra_excluded_dir_names_never_weaken_remove_protection(self):
        # Regression: --exclude must be additive-only. Passing --exclude
        # with unrelated names must not affect the hard-coded !REMOVE
        # refusal/pruning in any way.
        fs = FakeFileSystem()
        fs.add_dir("!REMOVE")
        fs.add_file("!REMOVE/2026-01-01/x.jpg", b"x")

        with pytest.raises(scanner.RemoveDirRefusedError):
            scanner.scan(fs, ["!REMOVE"], extra_excluded_dir_names=["SomethingElse"])

    def test_extra_excluded_dir_names_prune_remove_dir_still_when_scanning_from_above(self):
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_dir(f"{root}/!REMOVE")
        fs.add_file(f"{root}/!REMOVE/2026-01-01/x.jpg", b"x")
        fs.add_file(f"{root}/keep.jpg", b"keep")

        result = scanner.scan(fs, [root], extra_excluded_dir_names=["SomethingElse"])

        found = paths(result.files)
        assert f"{root}/keep.jpg" in found
        assert not any(p.startswith(f"{root}/!REMOVE") for p in found)

    def test_extra_excluded_dir_name_pruned_below_a_non_excluded_ancestor(self):
        # Regression guard for the merged exclusion set actually surviving
        # recursion: JunkFolder here sits two levels below the scan root,
        # inside a *non*-excluded directory. This is the only shape that
        # tells apart "the merged set is threaded through every _walk()
        # frame" from "only the top-level call sees it and recursive calls
        # silently fall back to config.EXCLUDED_DIR_NAMES_CASEFOLD" -- a
        # fallback that would either stop pruning the user's extra names
        # below the first level, or (worse) stop pruning !REMOVE/.dedup/
        # VIDEO_TS below the first level too.
        fs = FakeFileSystem()
        root = "D:/ARCHIVE"
        fs.add_dir(root)
        fs.add_dir(f"{root}/NotExcluded")
        fs.add_dir(f"{root}/NotExcluded/JunkFolder")
        fs.add_file(f"{root}/NotExcluded/JunkFolder/junk.jpg", b"junk")
        fs.add_file(f"{root}/NotExcluded/keep.jpg", b"keep")

        result = scanner.scan(fs, [root], extra_excluded_dir_names=["JunkFolder"])

        found = paths(result.files)
        assert f"{root}/NotExcluded/keep.jpg" in found
        assert f"{root}/NotExcluded/JunkFolder/junk.jpg" not in found
        assert f"{root}/NotExcluded/JunkFolder" in result.pruned_dirs


class TestPerEntryStatErrorDoesNotWipeSiblings:
    """Regression coverage for the review finding: a stat() failure on one
    directory entry (locked file, AV lock, cloud-sync race) discarded every
    sibling and everything recursively beneath any sibling subdirectories,
    because the old code materialized `list(fs.scandir(dir_path))` inside a
    single try/except that had no partial-results semantics."""

    def test_bad_entry_does_not_drop_sibling_files_or_subdirectories(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/ARCHIVE")
        fs.add_dir("D:/ARCHIVE/SubfolderWithPhotos")
        fs.add_file("D:/ARCHIVE/a_BAD.jpg", volume_id=1, file_index=1)
        fs.set_entry_stat_error("D:/ARCHIVE/a_BAD.jpg", OSError("simulated stat() failure"))
        fs.add_file("D:/ARCHIVE/SubfolderWithPhotos/vacation1.jpg", b"1", volume_id=1, file_index=2)
        fs.add_file("D:/ARCHIVE/SubfolderWithPhotos/vacation2.jpg", b"2", volume_id=1, file_index=3)

        result = scanner.scan(fs, ["D:/ARCHIVE"])

        found = paths(result.files)
        assert "D:/ARCHIVE/SubfolderWithPhotos/vacation1.jpg" in found
        assert "D:/ARCHIVE/SubfolderWithPhotos/vacation2.jpg" in found
        assert any(e.path == "D:/ARCHIVE/a_BAD.jpg" for e in result.errors)


class TestAncestorWalkStopsAtDriveRoot:
    """Regression: this crashed the first-ever real-disk run of the tool.

    _ancestor_kosz_sentinel() walks every ancestor directory of a scan root
    looking for the `.dedup-kosz` sentinel (renamed-!REMOVE guard). The old
    _parent_dir() truncated "D:\\Foo" down to the bare 2-character "D:" one
    level above the real drive root and then stopped -- but Windows treats a
    bare "D:" as "current directory on drive D", NOT an absolute path
    (`os.path.isabs("D:")` is False), so platform_win.to_extended() correctly
    raises ValueError for it. That ValueError is not an OSError/
    PermissionError, so _ancestor_kosz_sentinel()'s except clause never
    caught it, and it propagated all the way out of main(), aborting the
    entire scan. FakeFileSystem-based tests never caught this because its
    scandir() has no concept of path absoluteness -- it just does a dict
    lookup -- exactly like platform_win.to_extended() itself, this is a pure
    Windows path-string quirk, tested here directly and in isolation rather
    than through scanner.scan()/FakeFileSystem (see fakes.py's own module
    docstring for why real path quirks are tested this way)."""

    def test_ancestor_dirs_of_a_deep_path_ends_at_drive_root_with_separator(self):
        ancestors = list(scanner._ancestor_dirs(r"C:\Photo Archive\!NEW\Travel"))
        assert "D:" not in ancestors
        assert ancestors[-1] == "C:\\"

    def test_ancestor_dirs_of_a_shallow_path_terminates_without_bare_drive_letter(self):
        ancestors = list(scanner._ancestor_dirs(r"D:\Top"))
        assert ancestors == ["D:\\"]

    def test_parent_dir_of_drive_root_is_none_not_bare_drive_letter(self):
        assert scanner._parent_dir("D:\\") is None
        assert scanner._parent_dir("D:/") is None

    def test_parent_dir_one_level_above_drive_root_keeps_separator(self):
        assert scanner._parent_dir(r"C:\Photo Archive") == "C:\\"
        assert scanner._parent_dir("D:/Фото") == "D:/"
