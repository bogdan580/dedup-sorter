"""Command-line surface: default paths and the argument parser."""
from __future__ import annotations

import argparse
import os

from tidy import config
from tidy.analysis import events
from tidy import paths
from tidy import progress
from tidy.analysis import series


# The archive root is the parent of the .dedup repo root (tidy.paths) --
# keeps the tool portable across machines/drives instead of hard-coding one
# user's archive path.
DEFAULT_ARCHIVE_ROOT = str(paths.ARCHIVE_ROOT)
_DEDUP_DIR = os.path.join(DEFAULT_ARCHIVE_ROOT, config.DEDUP_DIR_NAME)
# Everything generated goes into a dot-prefixed subdirectory of .dedup\
# (config.DB_DIR_NAME / REPORTS_DIR_NAME / THUMBS_DIR_NAME) instead of
# sitting among the tool's own source files. config.json is the one file
# here that is *input* -- hand-edited by the user -- so it stays at the
# top of .dedup\ where it is easy to find.
_DB_DIR = os.path.join(_DEDUP_DIR, config.DB_DIR_NAME)
_REPORTS_DIR = os.path.join(_DEDUP_DIR, config.REPORTS_DIR_NAME)
DEFAULT_REPORT_PATH = os.path.join(_REPORTS_DIR, config.DEFAULT_REPORT_FILENAME)
DEFAULT_DB_PATH = os.path.join(_DB_DIR, config.DEFAULT_DB_FILENAME)
DEFAULT_KEEPER_CONFIG_PATH = os.path.join(_DEDUP_DIR, "config.json")
DEFAULT_THUMBS_DIR = os.path.join(_DEDUP_DIR, config.THUMBS_DIR_NAME)
DEFAULT_SERIES_REPORT_PATH = os.path.join(_REPORTS_DIR, "report_series.csv")
DEFAULT_EVENTS_REPORT_PATH = os.path.join(_REPORTS_DIR, "report_events.csv")
DEFAULT_INBOX_REPORT_PATH = os.path.join(_REPORTS_DIR, "report_new.html")
DEFAULT_COMPRESS_STAGING_DIR = os.path.join(_DEDUP_DIR, config.COMPRESS_DIR_NAME)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dedup.py",
        description="Photo/video archive duplicate scanner, apply and restore.",
    )
    parser.add_argument("--roots", nargs="+", default=[DEFAULT_ARCHIVE_ROOT],
                         help="Directories to scan (default: whole archive root).")
    parser.add_argument("-y", "--yes", action="store_true",
                         help="Skip the interactive confirmation before hashing.")
    parser.add_argument("--report", default=DEFAULT_REPORT_PATH,
                         help="Path to write the CSV report to.")
    parser.add_argument("--rescan", action="store_true",
                         help="Bypass the cache: recompute every hash from scratch.")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                         help="Path to the SQLite cache database.")
    parser.add_argument("--min-size", dest="min_size", type=int, default=None,
                         help="Ignore candidate files smaller than this many bytes.")
    parser.add_argument(
        "--exclude", nargs="+", default=None,
        help="Extra directory names to prune during the walk, on top of the "
             "hard-coded !REMOVE/.dedup/VIDEO_TS exclusions (additive only -- "
             "can never weaken those).",
    )
    parser.add_argument(
        "--ext", nargs="+", default=None,
        help="Restrict duplicate candidates to these extensions (accepts "
             "either \"jpg\" or \".jpg\" for each one). Files of other "
             "extensions are still scanned and still appear in the report, "
             "just never proposed as duplicates.",
    )
    parser.add_argument("--keeper-config", dest="keeper_config", default=DEFAULT_KEEPER_CONFIG_PATH,
                         help="Path to config.json (priority dirs, copy-name patterns, report size).")
    parser.add_argument(
        "--archive-root", dest="archive_root", default=DEFAULT_ARCHIVE_ROOT,
        help="Where !REMOVE/<batch>/ is created for --apply. Independent of --roots: "
             "!REMOVE always lives at the top of the whole archive, never "
             "inside whatever (possibly narrower) --roots subtree was actually scanned -- "
             "only override this for tests or a genuinely different archive location.",
    )

    # --apply performs real moves after the normal scan; --restore/
    # --verify-manifest instead operate on a manifest from a *previous*
    # --apply run and bypass scanning entirely. --review is a fourth mode
    # built on the same scan -> tier -> keeper pipeline as the default
    # report path -- all four are mutually exclusive with each other.
    # Flags: docs/business/instructions/CLI.md
    apply_group = parser.add_mutually_exclusive_group()
    apply_group.add_argument(
        "--apply", action="store_true",
        help="After scanning, move discarded duplicates into !REMOVE/<batch>/ with a manifest.",
    )
    apply_group.add_argument(
        "--restore", default=None, metavar="MANIFEST_PATH",
        help="Restore every completed move recorded in MANIFEST_PATH. Skips scanning.",
    )
    apply_group.add_argument(
        "--verify-manifest", dest="verify_manifest", default=None, metavar="MANIFEST_PATH",
        help="Report planned-without-move/orphans/mismatches for MANIFEST_PATH. Skips scanning.",
    )
    apply_group.add_argument(
        "--review", action="store_true",
        help="After scanning, start the local review UI at "
             "http://127.0.0.1:<port>/ instead of just printing a summary.",
    )
    apply_group.add_argument(
        "--series", action="store_true",
        help="After scanning, detect visually-similar photo series "
             "instead of exact-duplicate detection: bursts, re-exports, "
             "same-scene shots. Prints a summary and writes --series-report.",
    )
    apply_group.add_argument(
        "--events", action="store_true",
        help="After scanning, group files into trip/event folders and "
             "suggest destination names instead of exact-duplicate "
             "detection. Prints a summary and writes --events-report. Never "
             "moves anything -- produces a plan only.",
    )
    apply_group.add_argument(
        "--inbox", action="store_true",
        help="After scanning, triage !NEW: dedup vs the whole "
             "archive, series within !NEW, and coherent/mixed folder "
             "placement suggestions, all in one HTML report "
             "(--inbox-report). Never moves anything -- produces a plan only.",
    )
    parser.add_argument(
        "--port", type=int, default=0,
        help="TCP port for --review's local server (default: 0, picks a free port).",
    )
    parser.add_argument(
        "--no-browser", dest="no_browser", action="store_true",
        help="With --review, don't auto-open a browser tab.",
    )
    parser.add_argument(
        "--thumbs-dir", dest="thumbs_dir", default=DEFAULT_THUMBS_DIR,
        help="Where --review caches generated thumbnails (default: .dedup/.thumbs).",
    )
    parser.add_argument(
        "--series-report", dest="series_report", default=DEFAULT_SERIES_REPORT_PATH,
        help="Where --series writes its CSV report (default: .dedup/.reports/report_series.csv).",
    )
    parser.add_argument(
        "--series-window-seconds", dest="series_window_seconds", type=float,
        default=series.DEFAULT_SERIES_WINDOW_SECONDS,
        help="With --series, max time gap (seconds) between photos to consider "
             "clustering them at all (default: 300).",
    )
    parser.add_argument(
        "--series-hamming-threshold", dest="series_hamming_threshold", type=int,
        default=series.DEFAULT_HAMMING_THRESHOLD,
        help="With --series, max dHash Hamming distance for two photos to be "
             "considered the same shot (default: 6).",
    )
    parser.add_argument(
        "--events-report", dest="events_report", default=DEFAULT_EVENTS_REPORT_PATH,
        help="Where --events writes its CSV report (default: .dedup/.reports/report_events.csv).",
    )
    parser.add_argument(
        "--event-gap-hours", dest="event_gap_hours", type=float,
        default=events.DEFAULT_EVENT_GAP_HOURS,
        help="With --events, min time gap (hours) between files to start a "
             "new event (default: 8).",
    )
    parser.add_argument(
        "--gps-jump-km", dest="gps_jump_km", type=float,
        default=events.DEFAULT_GPS_JUMP_KM,
        help="With --events, min distance (km) between consecutive GPS "
             "fixes to split an event into two places (default: 25).",
    )
    parser.add_argument(
        "--since", dest="since", default=None, metavar="Nd",
        help="With --events, only list events whose suggested folder is a "
             "genuinely new location (not already in the archive) and whose "
             "latest file is within the last N days (e.g. '7d').",
    )
    parser.add_argument(
        "--inbox-report", dest="inbox_report", default=DEFAULT_INBOX_REPORT_PATH,
        help="Where --inbox writes its HTML report (default: .dedup/.reports/report_new.html).",
    )
    # --compress is deliberately NOT a member of apply_group above: unlike
    # --series/--events/--inbox, it needs --apply as a modifier
    # ("--compress --apply"), and --apply already lives in that
    # mutually-exclusive group. Its own conflicts with review/series/events/
    # inbox/restore/verify-manifest are instead rejected explicitly at the
    # top of run().
    parser.add_argument(
        "--compress", action="store_true",
        help="After scanning, list video files worth recompressing (heuristic "
             "+ size/time estimates) instead of exact-duplicate detection. "
             "Plan-only unless combined with --apply, which actually encodes, "
             "verifies, and moves each result into the archive next to its "
             "original as <name>_compressed.<ext>. Never touches originals "
             "unless --compress-remove-originals is also given.",
    )
    parser.add_argument(
        "--compress-remove-originals", dest="compress_remove_originals", action="store_true",
        help="With --compress --apply, move every original whose compressed "
             "result was verified and moved into the archive to !REMOVE, as a "
             "separate, independently restorable batch. "
             "Still dry-run without --apply; not sticky -- must be passed "
             "explicitly on every apply run.",
    )
    parser.add_argument(
        "--compress-staging-dir", dest="compress_staging_dir", default=DEFAULT_COMPRESS_STAGING_DIR,
        help="Where --compress --apply stages ffmpeg output before a "
             "verified result is moved into the archive (default: .dedup/.compress).",
    )
    parser.add_argument(
        "--compress-device", dest="compress_device", type=int, default=None,
        help="With --compress --apply and a hardware profile (NVENC/QSV), "
             "index into this machine's detected devices for that encoder "
             "(0 = first, per hwdetect.py's detection order). Optional; "
             "defaults to the first detected device. Ignored for CPU profiles.",
    )
    parser.add_argument(
        "--workers", dest="workers", type=int, default=progress.DEFAULT_WORKERS,
        help=f"Number of parallel threads for --series/--events/--inbox/--review's "
             f"per-file decode work (dHash, EXIF/GPS reads) -- default {progress.DEFAULT_WORKERS}. "
             "1 disables parallelism (original sequential behaviour).",
    )
    return parser
