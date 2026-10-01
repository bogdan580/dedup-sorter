"""--review mode: hand the scan result to the local review server."""
from __future__ import annotations

import argparse
from typing import List

from tidy.fs.fsmodel import FileSystem
from tidy.analysis import events
from tidy.analysis import inbox
from tidy.duplicates import keeper
from tidy.review import server as review_server
from tidy.duplicates import scanner
from tidy.duplicates import tiers
from tidy.cli.common import _filter_candidates, _resolve_archive_root, _scan_and_select


def run_review(
    args: argparse.Namespace,
    fs: FileSystem,
    scan_result: scanner.ScanResult,
    tier_result: tiers.TierResult,
    decisions: List[keeper.KeeperDecision],
) -> int:
    # candidates/db_path/the tuning knobs below are forwarded straight
    # through and never used to compute anything in this process:
    # series.find_series()/events.find_events() must run exactly once,
    # lazily, inside tidy/review's own worker thread (its own sqlite3
    # connection -- Cache can't cross threads), not here at startup, or
    # --review would block for minutes decoding every image in the
    # archive before the server even starts.
    #
    # candidates reuses the exact same _filter_candidates() call as
    # --series/--events -- so --min-size/--ext narrow the "Serie i
    # podobne"/"Sugestie grupowania" tabs exactly like they narrow
    # --series/--events on the CLI, keeping one candidate pool for the
    # whole archive instead of two that could silently disagree.
    #
    # Lazy computation: docs/technical/components/review-ui.md
    # Cache threading: docs/technical/components/manifest-and-db.md
    candidates = _filter_candidates(scan_result, args.min_size, args.ext)
    camera_offsets = events.load_camera_offsets(args.keeper_config)
    # Read from the same config.json as camera_offsets, so the
    # placement tab and an `--inbox` run agree on which folder names
    # are camera-roll dumps rather than trips.
    dump_folder_names = inbox.load_dump_folder_names(args.keeper_config)

    def _rescan():
        """POST /api/rescan: the same pipeline this branch just ran, on
        demand. Defined here rather than inside tidy/review because
        this is where --roots/--min-size/--ext/--keeper-config are
        known; the server would otherwise need its own copy of the CLI's
        scan configuration, which is exactly the kind of second source
        of truth the candidates comment above exists to prevent.

        Runs the FULL walk, not a recompute: the reason to press the
        button is that the archive changed on disk -- a new batch
        dropped into !NEW -- and the startup file list cannot show that.
        The SQLite cache keeps unchanged files from being rehashed."""
        fresh_scan, fresh_tiers, fresh_decisions, _cfg = _scan_and_select(args, fs)
        return (
            fresh_tiers.duplicate_groups,
            fresh_decisions,
            _filter_candidates(fresh_scan, args.min_size, args.ext),
        )

    return review_server.serve(
        fs, _resolve_archive_root(args), tier_result.duplicate_groups, decisions,
        args.thumbs_dir, port=args.port, open_browser=not args.no_browser,
        candidates=candidates, db_path=args.db,
        series_window_seconds=args.series_window_seconds,
        series_hamming_threshold=args.series_hamming_threshold,
        event_gap_hours=args.event_gap_hours, gps_jump_km=args.gps_jump_km,
        camera_offsets=camera_offsets, max_workers=args.workers,
        dump_folder_names=dump_folder_names,
        rescan=_rescan,
        # Same config.json as camera_offsets/dump_folder_names above,
        # so hwdetect.detect() and compress.load_compress_config()
        # resolve the identical "compression" section every other
        # --keeper-config consumer does.
        compress_config_path=args.keeper_config,
        # Same staging directory --compress --apply itself uses (CLI's
        # own --compress-staging-dir flag, DEFAULT_COMPRESS_STAGING_DIR
        # by default) -- never a second, independently-invented default
        # inside tidy/review.
        compress_staging_dir=args.compress_staging_dir,
    )
