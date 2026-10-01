"""CLI wiring of --inbox."""

import os
from tidy import config
from tidy.cli import args as cli_args
from tidy.cli import main as cli_main
from tidy.analysis import inbox
from fakes import FakeFileSystem


class TestInboxCliWiring:
    """--inbox runs the full scan/tiers/keeper dedup pipeline (unlike
    --series/--events, which skip it), then triages !NEW into an HTML
    report -- never moves anything."""

    def _make_fs(self, root):
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_dir(f"{root}/!NEW")
        fs.add_dir(f"{root}/!NEW/Trip")
        fs.add_dir(f"{root}/#Home")
        fs.add_file(f"{root}/!NEW/Trip/a.jpg", b"trip-a", mtime_ns=1)
        fs.add_file(f"{root}/!NEW/Trip/b.jpg", b"trip-b", mtime_ns=2)
        # A duplicate: same bytes already in the archive outside !NEW.
        fs.add_file(f"{root}/#Home/existing.jpg", b"same-bytes", file_index=100)
        fs.add_file(f"{root}/!NEW/existing-copy.jpg", b"same-bytes", file_index=101)
        return fs

    def test_inbox_writes_html_and_prints_summary(self, tmp_path, capsys):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs(root)
        inbox_report_path = str(tmp_path / "raport_new.html")

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--inbox", "--archive-root", root,
            "--inbox-report", inbox_report_path,
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        assert os.path.isfile(inbox_report_path)
        content = open(inbox_report_path, "r", encoding="utf-8").read()
        assert "<h2>1. Duplicates" in content
        captured = capsys.readouterr()
        assert "!NEW inbox" in captured.out

    def test_inbox_workers_flag_is_forwarded(self, tmp_path, monkeypatch):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs(root)
        captured = {}

        def fake_build_inbox_report(*args, **kw):
            captured["kw"] = kw
            return inbox.InboxReport()

        monkeypatch.setattr(inbox, "build_inbox_report", fake_build_inbox_report)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--inbox", "--archive-root", root, "--workers", "6",
            "--inbox-report", str(tmp_path / "raport_new.html"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        assert captured["kw"]["max_workers"] == 6

    def test_inbox_finds_new_vs_archive_duplicate(self, tmp_path, monkeypatch):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs(root)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--inbox", "--archive-root", root,
            "--inbox-report", str(tmp_path / "raport_new.html"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        captured = {}

        def fake_build_inbox_report(fs_arg, scan_result, tier_result, decisions, *a, **kw):
            captured["decisions"] = decisions
            return inbox.InboxReport()

        monkeypatch.setattr(inbox, "build_inbox_report", fake_build_inbox_report)

        cli_main.run(args, fs)

        discarded = {d.path for dec in captured["decisions"] for d in dec.discarded}
        assert f"{root}/!NEW/existing-copy.jpg" in discarded

    def test_inbox_classifies_coherent_trip_folder(self, tmp_path):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs(root)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--inbox", "--archive-root", root,
            "--inbox-report", str(tmp_path / "raport_new.html"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        content = open(str(tmp_path / "raport_new.html"), "r", encoding="utf-8").read()
        assert "Trip" in content
        assert "coherent folder" in content

    def test_inbox_never_calls_move_itself(self, tmp_path):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs(root)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--inbox", "--archive-root", root,
            "--inbox-report", str(tmp_path / "raport_new.html"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        assert fs.move_calls == []

    def test_inbox_passes_min_size_and_ext_to_build_inbox_report(self, tmp_path, monkeypatch):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs(root)
        captured = {}

        def fake_build_inbox_report(fs_arg, scan_result, tier_result, decisions, *a, **kw):
            captured["kw"] = kw
            return inbox.InboxReport()

        monkeypatch.setattr(inbox, "build_inbox_report", fake_build_inbox_report)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--inbox", "--archive-root", root,
            "--ext", "jpg", "--min-size", "1",
            "--inbox-report", str(tmp_path / "raport_new.html"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        assert captured["kw"]["extensions_casefold"] == frozenset({".jpg"})
        assert captured["kw"]["min_size"] == 1

    def test_inbox_refuses_remove_root(self, tmp_path):
        fs = FakeFileSystem()
        remove_root = str(tmp_path / "ARCHIVE" / config.REMOVE_DIR_NAME).replace("\\", "/")
        fs.add_dir(remove_root)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", remove_root, "-y", "--inbox",
            "--inbox-report", str(tmp_path / "raport_new.html"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        assert cli_main.run(args, fs) == 1
