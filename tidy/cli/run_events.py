"""--events mode: trip/event grouping, plan only."""
from __future__ import annotations

import argparse

from tidy.fs.fsmodel import FileSystem
from tidy.analysis import events
from tidy.duplicates import scanner
from tidy.cli.common import _filter_candidates, _parse_since, _resolve_archive_root, _resolve_roots


def run_events(args: argparse.Namespace, fs: FileSystem) -> int:
    # The events mode, like the series mode, doesn't need exact-duplicate
    # tier2/3 hashing or keeper selection -- it scans, then hands the same
    # is_duplicate_candidate list straight to events.find_events().
    try:
        scan_result = scanner.scan(fs, _resolve_roots(args), extra_excluded_dir_names=args.exclude)
    except scanner.RemoveDirRefusedError as exc:
        print(str(exc))
        return 1
    candidates = _filter_candidates(scan_result, args.min_size, args.ext)
    camera_offsets = events.load_camera_offsets(args.keeper_config)
    events_result = events.find_events(
        fs, candidates, _resolve_archive_root(args),
        gap_hours=args.event_gap_hours, gps_jump_km=args.gps_jump_km,
        camera_offsets=camera_offsets, max_workers=args.workers,
    )
    if args.since:
        try:
            since_delta = _parse_since(args.since)
        except ValueError as exc:
            print(str(exc))
            return 1
        events_result = events.EventsResult(
            events=events.filter_new_locations(events_result.events, since_delta),
            suspicious=events_result.suspicious,
            camera_drift_warnings=events_result.camera_drift_warnings,
        )
    events.write_events_csv(args.events_report, events_result)
    print(events.format_events_summary(events_result))
    return 0
