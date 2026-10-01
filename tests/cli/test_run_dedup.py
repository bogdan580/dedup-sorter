"""run() orchestration of the default dedup mode and --apply."""

import pytest
from tidy import config
from tidy.cli import args as cli_args
from tidy.cli import common
from tidy.cli import main as cli_main
from tidy.duplicates import report
from tidy.duplicates import scanner
from fakes import FakeFileSystem


class TestRunEndToEnd:
    def _make_fs_with_duplicates(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/ARCHIVE")
        fs.add_file("D:/ARCHIVE/a.jpg", b"same-bytes", volume_id=1, file_index=1)
        fs.add_file("D:/ARCHIVE/b.jpg", b"same-bytes", volume_id=1, file_index=2)
        fs.add_file("D:/ARCHIVE/unique.jpg", b"only-one-of-me", volume_id=1, file_index=3)
        return fs

    def test_run_writes_report_and_returns_zero(self, tmp_path, capsys):
        fs = self._make_fs_with_duplicates()
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", "D:/ARCHIVE",
            "-y",
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        assert (tmp_path / "raport.csv").exists()
        captured = capsys.readouterr()
        assert "Scan summary" in captured.out

    def test_run_refuses_remove_root_without_crashing(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_dir("!REMOVE")
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", "!REMOVE",
            "-y",
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code != 0
        assert not (tmp_path / "raport.csv").exists()

    def test_second_run_reuses_cache_no_reopen(self, tmp_path):
        fs = self._make_fs_with_duplicates()
        parser = cli_args.build_arg_parser()
        db_path = str(tmp_path / "cache.sqlite3")
        args = parser.parse_args([
            "--roots", "D:/ARCHIVE", "-y",
            "--report", str(tmp_path / "raport.csv"), "--db", db_path,
        ])

        cli_main.run(args, fs)
        fs.open_read_calls.clear()
        cli_main.run(args, fs)

        assert fs.open_read_calls == []

    def test_relative_root_is_resolved_against_cwd(self, tmp_path, monkeypatch):
        # Regression: platform_win.to_extended() requires an already-
        # absolute path; a relative --roots value must be resolved before
        # it reaches the FileSystem seam, not silently fail.
        monkeypatch.chdir(tmp_path)
        resolved_root = str(tmp_path / "ARCHIVE")
        fs = FakeFileSystem()
        fs.add_dir(resolved_root)
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", "ARCHIVE",
            "-y",
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        assert resolved_root in fs.stat_calls

    def test_cache_is_closed_even_if_find_duplicates_raises(self, tmp_path, monkeypatch):
        # Regression: cache.close() (which commits the in-flight batch)
        # must run even when the hashing/tiering step raises, otherwise an
        # unexpected exception silently discards up to a batch's worth of
        # already-computed hashes.
        from tidy.store import cache as cache_module
        from tidy.duplicates import tiers as tiers_module

        fs = self._make_fs_with_duplicates()
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", "D:/ARCHIVE",
            "-y",
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        closed = []
        original_close = cache_module.Cache.close

        def spy_close(self):
            closed.append(True)
            return original_close(self)

        monkeypatch.setattr(cache_module.Cache, "close", spy_close)
        monkeypatch.setattr(
            tiers_module, "find_duplicates",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        with pytest.raises(RuntimeError):
            cli_main.run(args, fs)

        assert closed == [True]


class TestExcludeCliWiring:
    """--exclude: extra pruned directory names, wired from
    args.exclude into scanner.scan(). Additive only on top of the
    hard-coded !REMOVE/.dedup/VIDEO_TS exclusions."""

    def test_exclude_prunes_extra_dir_end_to_end(self, tmp_path, capsys):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_dir(f"{root}/JunkFolder")
        fs.add_file(f"{root}/JunkFolder/junk.jpg", b"junk")
        fs.add_file(f"{root}/keep.jpg", b"keep")
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y",
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
            "--exclude", "JunkFolder",
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        report_text = (tmp_path / "raport.csv").read_text(encoding="utf-8-sig")
        assert "junk.jpg" not in report_text
        assert "keep.jpg" in report_text

    def test_exclude_does_not_weaken_remove_refusal(self, tmp_path):
        # Regression: --exclude with unrelated names must not affect the
        # hard-coded !REMOVE refusal.
        fs = FakeFileSystem()
        fs.add_dir("!REMOVE")
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", "!REMOVE",
            "-y",
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
            "--exclude", "SomethingElse",
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code != 0
        assert not (tmp_path / "raport.csv").exists()


class TestFilterCandidates:
    """Unit tests for common._filter_candidates(): the layer that restricts
    which scanned files are treated as duplicate candidates, independent of
    scanner.py's exclusion/pruning."""

    def _make_scanned_file(self, path, size, ext):
        from tidy.fs.fsmodel import StatInfo
        stat = StatInfo(
            size=size, mtime_ns=0, volume_id=None, file_index=None, nlink=1,
            attributes=0, is_dir=False, is_reparse_point=False, reparse_tag=None,
        )
        return scanner.ScannedFile(path=f"{path}{ext}", stat=stat, is_duplicate_candidate=True)

    def _make_scan_result(self, files):
        result = scanner.ScanResult()
        result.files = files
        return result

    def test_no_filters_returns_all_candidates(self):
        files = [self._make_scanned_file("a", 100, ".jpg"), self._make_scanned_file("b", 200, ".mp4")]
        result = self._make_scan_result(files)

        candidates = common._filter_candidates(result, min_size=None, extensions=None)

        assert {f.path for f in candidates} == {"a.jpg", "b.mp4"}

    def test_ext_filter_restricts_to_given_extensions(self):
        files = [self._make_scanned_file("a", 100, ".jpg"), self._make_scanned_file("b", 200, ".mp4")]
        result = self._make_scan_result(files)

        candidates = common._filter_candidates(result, min_size=None, extensions=[".jpg"])

        assert {f.path for f in candidates} == {"a.jpg"}

    def test_ext_filter_accepts_extension_without_leading_dot(self):
        files = [self._make_scanned_file("a", 100, ".jpg"), self._make_scanned_file("b", 200, ".mp4")]
        result = self._make_scan_result(files)

        candidates = common._filter_candidates(result, min_size=None, extensions=["jpg"])

        assert {f.path for f in candidates} == {"a.jpg"}

    def test_ext_filter_is_casefolded(self):
        files = [self._make_scanned_file("a", 100, ".JPG")]
        result = self._make_scan_result(files)

        candidates = common._filter_candidates(result, min_size=None, extensions=["jpg"])

        assert {f.path for f in candidates} == {"a.JPG"}

    def test_ext_and_min_size_filters_compose(self):
        files = [
            self._make_scanned_file("a", 100, ".jpg"),
            self._make_scanned_file("b", 5, ".jpg"),
            self._make_scanned_file("c", 100, ".mp4"),
        ]
        result = self._make_scan_result(files)

        candidates = common._filter_candidates(result, min_size=50, extensions=["jpg"])

        assert {f.path for f in candidates} == {"a.jpg"}


class TestExtCliWiring:
    """--ext: restricts duplicate candidates to given
    extensions end-to-end via run()."""

    def test_ext_narrows_duplicate_report_end_to_end(self, tmp_path):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_file(f"{root}/a.jpg", b"same-bytes", volume_id=1, file_index=1)
        fs.add_file(f"{root}/b.jpg", b"same-bytes", volume_id=1, file_index=2)
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y",
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
            "--ext", "mp4",
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        captured_report = (tmp_path / "raport.csv").read_text(encoding="utf-8-sig")
        import csv as csv_module
        with open(tmp_path / "raport.csv", "r", encoding="utf-8-sig", newline="") as f:
            reader = csv_module.reader(f, delimiter=";")
            header = next(reader)
            rows = list(reader)
        kind_idx = header.index("kind")
        kinds = {row[kind_idx] for row in rows}
        assert report.KIND_DUP not in kinds


class TestKeeperWiringInRun:
    """run() must run keeper selection over every duplicate group
    and surface it both in the console grouped report and in raport.csv's
    role/reason columns."""

    def _write_keeper_config(self, tmp_path, priority_dirs=("#Travel",)):
        import json
        path = tmp_path / "keeper_config.json"
        path.write_text(
            json.dumps({
                "priority_dirs": list(priority_dirs),
                "copy_name_patterns": ["- kopia", "(1)"],
                "report_top_n": 20,
            }),
            encoding="utf-8",
        )
        return str(path)

    def test_rule_0_new_vs_archive_reflected_in_report_and_csv(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_dir("D:/ARCHIVE")
        fs.add_dir("D:/ARCHIVE/!NEW")
        fs.add_dir("D:/ARCHIVE/!NEW/Camera")
        fs.add_dir("D:/ARCHIVE/#Travel")
        fs.add_dir("D:/ARCHIVE/#Travel/2023 Mountains")
        new_path = "D:/ARCHIVE/!NEW/Camera/img.jpg"
        archive_path = "D:/ARCHIVE/#Travel/2023 Mountains/img.jpg"
        fs.add_file(new_path, b"same-bytes", volume_id=1, file_index=1)
        fs.add_file(archive_path, b"same-bytes", volume_id=1, file_index=2)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", "D:/ARCHIVE",
            "-y",
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
            "--keeper-config", self._write_keeper_config(tmp_path),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        captured_report = (tmp_path / "raport.csv").read_text(encoding="utf-8-sig")
        assert "Travel" in captured_report

        import csv as csv_module
        with open(tmp_path / "raport.csv", "r", encoding="utf-8-sig", newline="") as f:
            reader = csv_module.reader(f, delimiter=";")
            header = next(reader)
            rows = {r[1]: r for r in reader}
        role_idx = header.index("role")
        reason_idx = header.index("reason")

        assert rows[archive_path][role_idx] == "keeper"
        assert rows[new_path][role_idx] == "discarded"
        assert rows[new_path][reason_idx] == "rule-0"

    def test_grouped_report_printed_to_console(self, tmp_path, capsys):
        fs = FakeFileSystem()
        fs.add_dir("D:/ARCHIVE")
        fs.add_file("D:/ARCHIVE/a.jpg", b"same-bytes", volume_id=1, file_index=1)
        fs.add_file("D:/ARCHIVE/b.jpg", b"same-bytes", volume_id=1, file_index=2)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", "D:/ARCHIVE",
            "-y",
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
            "--keeper-config", self._write_keeper_config(tmp_path),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Grouped report" in captured.out

    def test_no_duplicates_still_runs_cleanly(self, tmp_path, capsys):
        fs = FakeFileSystem()
        fs.add_dir("D:/ARCHIVE")
        fs.add_file("D:/ARCHIVE/unique.jpg", b"only-one", volume_id=1, file_index=1)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", "D:/ARCHIVE",
            "-y",
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
            "--keeper-config", self._write_keeper_config(tmp_path),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Grouped report" in captured.out


class TestApplyCliWiring:
    """--apply after the normal scan/keeper pipeline. archive_root
    here is a real tmp_path-backed directory (not a bare "D:/ARCHIVE"
    fake-only string) because manifest.py does real disk I/O independent of
    the `fs` seam -- see tidy/moves/applier's/manifest.py's module docstrings and
    tests/test_applier.py's own note on this."""

    def _make_fs_with_duplicates_at(self, root):
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_file(f"{root}/a.jpg", b"same-bytes", volume_id=1, file_index=1)
        fs.add_file(f"{root}/b.jpg", b"same-bytes", volume_id=1, file_index=2)
        return fs

    def test_run_without_apply_never_calls_move(self, tmp_path):
        # The real dry-run guarantee now that a genuine move primitive
        # exists in the codebase (see tests/test_dry_run_guarantee.py).
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_duplicates_at(root)
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y",
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        assert fs.move_calls == []

    def test_run_with_apply_moves_discards_and_prints_summary(self, tmp_path, capsys):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_duplicates_at(root)
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--apply", "--archive-root", root,
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        assert len(fs.move_calls) == 1
        captured = capsys.readouterr()
        assert "moved" in captured.out.lower()

    def test_dry_run_creates_missing_top_level_dirs(self, tmp_path, capsys):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_duplicates_at(root)
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--archive-root", root,
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        assert cli_main.run(args, fs) == 0

        for name in (config.NEW_DIR_NAME, config.MOVES_DIR_NAME, config.REMOVE_DIR_NAME):
            assert fs.exists(f"{root}/{name}")
        assert fs.move_calls == []
        out = capsys.readouterr().out
        assert "Created missing directory: !NEW" in out
        out.encode("ascii")

    def test_apply_places_remove_dir_at_archive_root_not_narrowed_roots(self, tmp_path):
        # Regression: --archive-root must govern !REMOVE placement even
        # when --roots is narrower -- a scoped first apply run (with a
        # "!NEW\Travel"-style narrow root) must not
        # nest !REMOVE under the narrow root instead of the archive root.
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_duplicates_at(root)
        fs.add_dir(f"{root}/Narrow")
        fs.add_file(f"{root}/Narrow/a2.jpg", b"same-bytes", volume_id=1, file_index=3)
        fs.add_file(f"{root}/Narrow/b2.jpg", b"same-bytes", volume_id=1, file_index=4)
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", f"{root}/Narrow", "-y", "--apply", "--archive-root", root,
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        assert fs.exists(f"{root}/!REMOVE") is True
        assert fs.exists(f"{root}/Narrow/!REMOVE") is False

    def test_apply_then_rescan_finds_zero_duplicates(self, tmp_path, capsys):
        # Apply, then scan again -- second pass must
        # report zero duplicate groups (and never re-enter !REMOVE, already
        # guaranteed structurally by scanner.py's hard exclusion).
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_duplicates_at(root)
        parser = cli_args.build_arg_parser()
        apply_args = parser.parse_args([
            "--roots", root, "-y", "--apply", "--archive-root", root,
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])
        cli_main.run(apply_args, fs)

        rescan_args = parser.parse_args([
            "--roots", root, "-y",
            "--report", str(tmp_path / "raport2.csv"),
            "--db", str(tmp_path / "cache2.sqlite3"),
        ])
        exit_code = cli_main.run(rescan_args, fs)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Duplicate groups: 0" in captured.out
