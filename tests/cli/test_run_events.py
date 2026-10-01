"""CLI wiring of --events."""

import os
from tidy import config
from tidy.cli import args as cli_args
from tidy.cli import main as cli_main
from tidy.analysis import events
from fakes import FakeFileSystem


class TestEventsCliWiring:
    """--events scans (like --series) but hands the candidate list
    straight to events.find_events() -- writes a CSV and prints a summary
    instead of the exact-duplicate report, and never moves anything."""

    def _make_fs_with_one_photo(self, root):
        import io as _io

        from PIL import ExifTags, Image

        def jpeg_bytes(when):
            buf = _io.BytesIO()
            image = Image.new("RGB", (20, 15), (10, 20, 30))
            exif = Image.Exif()
            exif[ExifTags.IFD.Exif] = {36867: when}
            image.save(buf, "JPEG", exif=exif.tobytes())
            return buf.getvalue()

        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_file(f"{root}/a.jpg", jpeg_bytes("2024:01:01 10:00:00"))
        return fs

    def test_events_writes_csv_and_prints_summary(self, tmp_path, capsys):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_one_photo(root)
        events_report_path = str(tmp_path / "raport_wydarzenia.csv")

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--events", "--archive-root", root,
            "--events-report", events_report_path,
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        assert os.path.isfile(events_report_path)
        captured = capsys.readouterr()
        assert "Events and grouping suggestions" in captured.out
        assert "Events: 1" in captured.out

    def test_events_workers_flag_is_forwarded(self, tmp_path, monkeypatch):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_one_photo(root)
        captured = {}

        def fake_find_events(fs_arg, candidates, archive_root, **kw):
            captured["kw"] = kw
            return events.EventsResult()

        monkeypatch.setattr(events, "find_events", fake_find_events)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--events", "--archive-root", root, "--workers", "5",
            "--events-report", str(tmp_path / "raport_wydarzenia.csv"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        assert captured["kw"]["max_workers"] == 5

    def test_events_never_calls_move_itself(self, tmp_path):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_one_photo(root)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--events", "--archive-root", root,
            "--events-report", str(tmp_path / "raport_wydarzenia.csv"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        assert fs.move_calls == []

    def test_events_does_not_run_exact_duplicate_confirmation_prompt(self, tmp_path):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_one_photo(root)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "--events", "--archive-root", root,  # no -y
            "--events-report", str(tmp_path / "raport_wydarzenia.csv"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0  # would hang/raise on a real input() call if reached

    def test_events_refuses_remove_root(self, tmp_path):
        fs = FakeFileSystem()
        remove_root = str(tmp_path / "ARCHIVE" / config.REMOVE_DIR_NAME).replace("\\", "/")
        fs.add_dir(remove_root)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", remove_root, "-y", "--events",
            "--events-report", str(tmp_path / "raport_wydarzenia.csv"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        assert cli_main.run(args, fs) == 1

    def test_events_since_filters_out_events_matching_existing_folder(self, tmp_path):
        # The one event in _make_fs_with_one_photo has no GPS, so it can
        # never match an existing folder -- --since here only proves the
        # flag is wired through without raising, the actual filtering
        # logic itself is covered by test_events.py::TestFilterNewLocations.
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_one_photo(root)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--events", "--archive-root", root, "--since", "7d",
            "--events-report", str(tmp_path / "raport_wydarzenia.csv"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        assert cli_main.run(args, fs) == 0

    def test_events_bad_since_format_reports_error(self, tmp_path):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_one_photo(root)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--events", "--archive-root", root, "--since", "banana",
            "--events-report", str(tmp_path / "raport_wydarzenia.csv"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        assert cli_main.run(args, fs) == 1

    def test_events_loads_camera_offsets_from_keeper_config(self, tmp_path, monkeypatch):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_one_photo(root)
        config_path = tmp_path / "config.json"
        config_path.write_text('{"camera_offsets": {"NIKON D3100": "+1h"}}', encoding="utf-8")
        captured = {}

        def fake_find_events(fs_arg, candidates, archive_root, **kw):
            captured["kw"] = kw
            return events.EventsResult()

        monkeypatch.setattr(events, "find_events", fake_find_events)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--events", "--archive-root", root,
            "--keeper-config", str(config_path),
            "--events-report", str(tmp_path / "raport_wydarzenia.csv"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        assert captured["kw"]["camera_offsets"] == {"NIKON D3100": events.parse_camera_offset("+1h")}
