"""CLI argparse defaults and the confirm-gate builder."""

import os
import pytest
from tidy import config
from tidy.cli import args as cli_args
from tidy.cli import common
from tidy.analysis import events
from tidy import progress
from tidy.analysis import series


class TestArgParserDefaults:
    def test_defaults(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([])
        assert args.roots == [cli_args.DEFAULT_ARCHIVE_ROOT]
        assert args.yes is False
        assert args.report == cli_args.DEFAULT_REPORT_PATH
        assert args.rescan is False
        assert args.db == cli_args.DEFAULT_DB_PATH
        assert args.min_size is None
        assert args.keeper_config == cli_args.DEFAULT_KEEPER_CONFIG_PATH
        assert args.apply is False
        assert args.restore is None
        assert args.verify_manifest is None
        assert args.exclude is None
        assert args.ext is None
        assert args.review is False
        assert args.port == 0
        assert args.no_browser is False
        assert args.thumbs_dir == cli_args.DEFAULT_THUMBS_DIR
        assert args.series is False
        assert args.series_report == cli_args.DEFAULT_SERIES_REPORT_PATH
        assert args.series_window_seconds == series.DEFAULT_SERIES_WINDOW_SECONDS
        assert args.series_hamming_threshold == series.DEFAULT_HAMMING_THRESHOLD
        assert args.events is False
        assert args.events_report == cli_args.DEFAULT_EVENTS_REPORT_PATH
        assert args.event_gap_hours == events.DEFAULT_EVENT_GAP_HOURS
        assert args.gps_jump_km == events.DEFAULT_GPS_JUMP_KM
        assert args.since is None
        assert args.inbox is False
        assert args.inbox_report == cli_args.DEFAULT_INBOX_REPORT_PATH
        assert args.workers == progress.DEFAULT_WORKERS

    def test_every_generated_default_lives_in_a_dot_prefixed_dir(self):
        """Every directory the tool generates
        starts with a dot, so one Explorer "hidden files" toggle hides all
        of them and nothing generated sits loose among the source files.
        Asserts the *shape* of each default path (its parent directory
        name), not the absolute location, which depends on where the
        archive lives."""
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([])
        generated = [
            args.report, args.db, args.thumbs_dir, args.series_report,
            args.events_report, args.inbox_report,
        ]
        for path in generated:
            parent = os.path.basename(os.path.dirname(path))
            assert parent.startswith("."), f"{path} is not inside a dot-prefixed dir"
        assert os.path.basename(os.path.dirname(args.db)) == config.DB_DIR_NAME
        assert os.path.basename(args.thumbs_dir) == config.THUMBS_DIR_NAME
        for path in (args.report, args.series_report, args.events_report, args.inbox_report):
            assert os.path.basename(os.path.dirname(path)) == config.REPORTS_DIR_NAME

    def test_keeper_config_stays_at_the_top_of_dedup_dir(self):
        """config.json is *input* (hand-edited by the user), not a
        generated artefact, so it deliberately stays where it is easy to
        find instead of being buried in a dot-prefixed directory."""
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([])
        assert os.path.basename(os.path.dirname(args.keeper_config)) == config.DEDUP_DIR_NAME

    def test_custom_values(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", "D:/A", "D:/B",
            "-y",
            "--report", "out.csv",
            "--rescan",
            "--db", "mine.sqlite3",
            "--min-size", "1024",
            "--keeper-config", "mine-config.json",
            "--exclude", "JunkFolder", "OtherJunk",
            "--ext", "jpg", ".png",
        ])
        assert args.roots == ["D:/A", "D:/B"]
        assert args.yes is True
        assert args.report == "out.csv"
        assert args.rescan is True
        assert args.db == "mine.sqlite3"
        assert args.min_size == 1024
        assert args.keeper_config == "mine-config.json"
        assert args.exclude == ["JunkFolder", "OtherJunk"]
        assert args.ext == ["jpg", ".png"]

    def test_workers_custom_value(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--workers", "7"])
        assert args.workers == 7

    def test_apply_flag(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--apply"])
        assert args.apply is True

    def test_restore_option(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--restore", "some/manifest.csv"])
        assert args.restore == "some/manifest.csv"

    def test_verify_manifest_option(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--verify-manifest", "some/manifest.csv"])
        assert args.verify_manifest == "some/manifest.csv"

    def test_apply_and_restore_are_mutually_exclusive(self):
        parser = cli_args.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--apply", "--restore", "some/manifest.csv"])

    def test_review_flag(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--review"])
        assert args.review is True

    def test_review_and_apply_are_mutually_exclusive(self):
        parser = cli_args.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--review", "--apply"])

    def test_review_and_restore_are_mutually_exclusive(self):
        parser = cli_args.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--review", "--restore", "some/manifest.csv"])

    def test_port_and_no_browser_and_thumbs_dir_custom_values(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--review", "--port", "9999", "--no-browser", "--thumbs-dir", "mine/thumbs",
        ])
        assert args.port == 9999
        assert args.no_browser is True
        assert args.thumbs_dir == "mine/thumbs"

    def test_restore_and_verify_manifest_are_mutually_exclusive(self):
        parser = cli_args.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--restore", "a.csv", "--verify-manifest", "b.csv"])

    def test_series_flag(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--series"])
        assert args.series is True

    def test_series_and_apply_are_mutually_exclusive(self):
        parser = cli_args.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--series", "--apply"])

    def test_series_and_review_are_mutually_exclusive(self):
        parser = cli_args.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--series", "--review"])

    def test_series_custom_values(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--series", "--series-report", "mine.csv",
            "--series-window-seconds", "120", "--series-hamming-threshold", "10",
        ])
        assert args.series_report == "mine.csv"
        assert args.series_window_seconds == 120.0
        assert args.series_hamming_threshold == 10

    def test_events_flag(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--events"])
        assert args.events is True

    def test_events_and_apply_are_mutually_exclusive(self):
        parser = cli_args.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--events", "--apply"])

    def test_events_and_series_are_mutually_exclusive(self):
        parser = cli_args.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--events", "--series"])

    def test_events_custom_values(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--events", "--events-report", "mine.csv",
            "--event-gap-hours", "4", "--gps-jump-km", "10", "--since", "7d",
        ])
        assert args.events_report == "mine.csv"
        assert args.event_gap_hours == 4.0
        assert args.gps_jump_km == 10.0
        assert args.since == "7d"


class TestBuildConfirm:
    def test_yes_flag_always_confirms_without_prompting(self):
        printed = []
        confirm = common.build_confirm(yes=True, print_func=printed.append)
        assert confirm("estimate message") is True
        assert printed == ["estimate message"]

    def test_interactive_yes_answer(self):
        printed = []
        confirm = common.build_confirm(yes=False, input_func=lambda prompt: "t", print_func=printed.append)
        assert confirm("msg") is True
        assert printed == ["msg"]

    def test_interactive_no_answer(self):
        confirm = common.build_confirm(yes=False, input_func=lambda prompt: "n", print_func=lambda m: None)
        assert confirm("msg") is False


class TestInboxArgParser:
    def test_inbox_flag(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--inbox"])
        assert args.inbox is True

    def test_inbox_and_apply_are_mutually_exclusive(self):
        parser = cli_args.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--inbox", "--apply"])

    def test_inbox_and_events_are_mutually_exclusive(self):
        parser = cli_args.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--inbox", "--events"])

    def test_inbox_and_series_are_mutually_exclusive(self):
        parser = cli_args.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--inbox", "--series"])

    def test_inbox_custom_report_path(self):
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--inbox", "--inbox-report", "mine.html"])
        assert args.inbox_report == "mine.html"
