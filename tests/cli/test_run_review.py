"""CLI wiring of --review."""

from tidy.cli import args as cli_args
from tidy.cli import main as cli_main
from tidy.review import server as review_server
from tidy.analysis import events
from tidy.analysis import inbox
from tidy import progress
from tidy.analysis import series
from fakes import FakeFileSystem


class TestReviewCliWiring:
    """--review runs the same scan/keeper pipeline as the default
    report path, then hands off to review_server.serve() instead of just
    printing a summary. review_server.serve() itself blocks until
    Ctrl+C'd and opens a real browser tab -- both wrong for a test -- so
    it's monkeypatched here to capture what run() would have handed it,
    mirroring how tests/review/ already exercises
    review_server.serve()/start_server() directly and for real."""

    def _make_fs_with_duplicates_at(self, root):
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_file(f"{root}/a.jpg", b"same-bytes", volume_id=1, file_index=1)
        fs.add_file(f"{root}/b.jpg", b"same-bytes", volume_id=1, file_index=2)
        return fs

    def test_review_calls_review_server_serve_with_scan_results(self, tmp_path, monkeypatch):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_duplicates_at(root)
        captured = {}

        def fake_serve(fs_arg, archive_root, duplicate_groups, decisions, thumbs_dir, **kw):
            captured["archive_root"] = archive_root
            captured["duplicate_groups"] = duplicate_groups
            captured["decisions"] = decisions
            captured["thumbs_dir"] = thumbs_dir
            captured["kw"] = kw
            return 0

        monkeypatch.setattr(review_server, "serve", fake_serve)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--review", "--archive-root", root,
            "--port", "12345", "--no-browser",
            "--thumbs-dir", str(tmp_path / "thumbs"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        assert captured["archive_root"] == root
        assert len(captured["duplicate_groups"]) == 1
        assert len(captured["decisions"]) == 1
        assert captured["thumbs_dir"] == str(tmp_path / "thumbs")
        # --review also forwards the series/events tuning
        # (candidates/db_path/tuning knobs) -- see the dedicated wiring
        # tests below for each of these individually; here just pin down
        # the full kwarg set (except candidates, checked by path below) so
        # an accidental future omission fails loudly.
        kw = dict(captured["kw"])
        candidate_paths = sorted(f.path for f in kw.pop("candidates"))
        assert candidate_paths == sorted([f"{root}/a.jpg", f"{root}/b.jpg"])
        # Rescan button: the server is handed
        # the CLI's own scan pipeline as a callable rather than growing its
        # own copy of --roots/--min-size/--ext handling.
        assert callable(kw.pop("rescan"))
        assert kw == {
            "port": 12345,
            "open_browser": False,
            "db_path": str(tmp_path / "cache.sqlite3"),
            "series_window_seconds": series.DEFAULT_SERIES_WINDOW_SECONDS,
            "series_hamming_threshold": series.DEFAULT_HAMMING_THRESHOLD,
            "event_gap_hours": events.DEFAULT_EVENT_GAP_HOURS,
            "gps_jump_km": events.DEFAULT_GPS_JUMP_KM,
            "camera_offsets": {},
            # --workers: forwarded
            # straight through, same as every other tuning knob above.
            "max_workers": progress.DEFAULT_WORKERS,
            # Read from the same config.json as camera_offsets
            # so the placement tab and `--inbox` agree on which folder names
            # are camera-roll dumps rather than trips.
            "dump_folder_names": inbox.DEFAULT_DUMP_FOLDER_NAMES,
            # Same config.json again, this time so hwdetect.detect() and
            # compress.load_compress_config() resolve the identical
            # "compression" section.
            "compress_config_path": args.keeper_config,
            # Same staging directory --compress --apply itself uses, so a queue started from the review UI stages
            # to the identical place the CLI does.
            "compress_staging_dir": args.compress_staging_dir,
        }

    def test_review_never_calls_move_itself(self, tmp_path, monkeypatch):
        # --review's own apply happens only via the browser's POST
        # /api/apply, never as a side effect of tidy/cli's run() -- the
        # dry-run guarantee (tests/test_dry_run_guarantee.py) must hold for
        # --review exactly like every other non---apply invocation.
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_duplicates_at(root)
        monkeypatch.setattr(review_server, "serve", lambda *a, **kw: 0)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--review", "--archive-root", root,
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        assert fs.move_calls == []

    def test_review_returns_review_server_exit_code(self, tmp_path, monkeypatch):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_duplicates_at(root)
        monkeypatch.setattr(review_server, "serve", lambda *a, **kw: 7)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--review", "--archive-root", root,
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        assert cli_main.run(args, fs) == 7

    def test_review_forwards_db_path_and_default_tuning_knobs(self, tmp_path, monkeypatch):
        # review_server.serve() opens its own sqlite3
        # connection lazily, in its own worker thread (sqlite3 connections
        # can't cross threads) -- tidy/cli must hand it only the *path*
        # (args.db), never a Cache instance of its own.
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_duplicates_at(root)
        captured = {}
        monkeypatch.setattr(review_server, "serve", lambda *a, **kw: captured.update(kw) or 0)

        db_path = str(tmp_path / "cache.sqlite3")
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--review", "--archive-root", root,
            "--report", str(tmp_path / "raport.csv"),
            "--db", db_path,
        ])

        cli_main.run(args, fs)

        assert captured["db_path"] == db_path
        assert captured["series_window_seconds"] == series.DEFAULT_SERIES_WINDOW_SECONDS
        assert captured["series_hamming_threshold"] == series.DEFAULT_HAMMING_THRESHOLD
        assert captured["event_gap_hours"] == events.DEFAULT_EVENT_GAP_HOURS
        assert captured["gps_jump_km"] == events.DEFAULT_GPS_JUMP_KM

    def test_review_forwards_explicit_tuning_knobs_from_cli(self, tmp_path, monkeypatch):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_duplicates_at(root)
        captured = {}
        monkeypatch.setattr(review_server, "serve", lambda *a, **kw: captured.update(kw) or 0)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--review", "--archive-root", root,
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
            "--series-window-seconds", "120",
            "--series-hamming-threshold", "3",
            "--event-gap-hours", "4",
            "--gps-jump-km", "50",
        ])

        cli_main.run(args, fs)

        assert captured["series_window_seconds"] == 120.0
        assert captured["series_hamming_threshold"] == 3
        assert captured["event_gap_hours"] == 4.0
        assert captured["gps_jump_km"] == 50.0

    def test_review_forwards_camera_offsets_from_keeper_config(self, tmp_path, monkeypatch):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_duplicates_at(root)
        config_path = tmp_path / "config.json"
        config_path.write_text('{"camera_offsets": {"NIKON D3100": "+1h"}}', encoding="utf-8")
        captured = {}
        monkeypatch.setattr(review_server, "serve", lambda *a, **kw: captured.update(kw) or 0)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--review", "--archive-root", root,
            "--keeper-config", str(config_path),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        assert captured["camera_offsets"] == {"NIKON D3100": events.parse_camera_offset("+1h")}

    def test_review_min_size_and_ext_narrow_candidates_passed_to_serve(self, tmp_path, monkeypatch):
        # Requirement C: --min-size/--ext must narrow the candidate pool
        # handed to the "Serie i podobne"/"Sugestie grupowania" tabs
        # exactly like they already narrow --series/--events on the CLI --
        # otherwise the UI and the CLI would see two different candidate
        # pools for the same archive.
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_file(f"{root}/a.jpg", b"same-bytes-jpg-1", volume_id=1, file_index=1)
        fs.add_file(f"{root}/b.jpg", b"same-bytes-jpg-1", volume_id=1, file_index=2)
        fs.add_file(f"{root}/c.png", b"x" * 5, volume_id=1, file_index=3)
        captured = {}
        monkeypatch.setattr(review_server, "serve", lambda *a, **kw: captured.update(kw) or 0)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--review", "--archive-root", root,
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
            "--ext", "jpg",
        ])

        cli_main.run(args, fs)

        candidate_paths = sorted(f.path for f in captured["candidates"])
        assert candidate_paths == sorted([f"{root}/a.jpg", f"{root}/b.jpg"])

    def test_review_does_not_compute_series_or_events_before_serving(self, tmp_path, monkeypatch):
        # Requirement D: series/events must be computed lazily, inside
        # tidy/review's own worker thread, on first GET /api/series or
        # /api/events -- never up front in tidy/cli, or --review would
        # block for minutes decoding every image in the archive before the
        # server even starts. Both engines are monkeypatched to blow up if
        # called at all; run() must still complete successfully.
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_duplicates_at(root)

        def boom(*a, **kw):
            raise AssertionError("series/events must not be computed before --review serves")

        monkeypatch.setattr(series, "find_series", boom)
        monkeypatch.setattr(events, "find_events", boom)
        monkeypatch.setattr(review_server, "serve", lambda *a, **kw: 0)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--review", "--archive-root", root,
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        assert cli_main.run(args, fs) == 0
