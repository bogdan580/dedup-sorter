"""--inbox mode: !NEW triage report, plan only."""
from __future__ import annotations

import argparse
import os

from tidy.store.cache import Cache
from tidy.fs.fsmodel import FileSystem
from tidy import config
from tidy.analysis import events
from tidy.analysis import inbox
from tidy.duplicates import scanner
from tidy.cli.common import _normalize_extension, _resolve_archive_root, _scan_and_select


def run_inbox(args: argparse.Namespace, fs: FileSystem) -> int:
    # The inbox mode needs the full dedup-vs-archive pipeline (unlike
    # --series/--events, which skip it) -- !NEW already lives inside
    # the archive tree, so the normal _scan_and_select() over
    # --roots (default: the whole archive) already covers it; rule 0
    # (keeper.is_new_path) already makes every !NEW duplicate lose to
    # its archive copy with no special-casing needed here.
    try:
        scan_result, tier_result, decisions, _keeper_cfg = _scan_and_select(args, fs)
    except scanner.RemoveDirRefusedError as exc:
        print(str(exc))
        return 1
    archive_root_resolved = _resolve_archive_root(args)
    new_root = os.path.join(archive_root_resolved, config.NEW_DIR_NAME)
    camera_offsets = events.load_camera_offsets(args.keeper_config)
    # Mirrors --series/--events narrowing their candidate list by the
    # same flags before handing it to their engine -- --min-size/--ext
    # must affect the series/placement stage the same way they already
    # affect _scan_and_select()'s own duplicate detection above, not
    # just the duplicates section of this report.
    extensions_casefold = (
        frozenset(_normalize_extension(e) for e in args.ext) if args.ext else None
    )
    series_cache = Cache(args.db)
    try:
        inbox_report = inbox.build_inbox_report(
            fs, scan_result, tier_result, decisions, archive_root_resolved, new_root, series_cache,
            camera_offsets=camera_offsets, event_gap_hours=args.event_gap_hours,
            gps_jump_km=args.gps_jump_km, series_window_seconds=args.series_window_seconds,
            series_hamming_threshold=args.series_hamming_threshold, force_recompute=args.rescan,
            min_size=args.min_size, extensions_casefold=extensions_casefold,
            max_workers=args.workers,
        )
    finally:
        series_cache.close()
    inbox.write_inbox_html(args.inbox_report, inbox_report)
    print(inbox.format_inbox_summary(inbox_report))
    return 0
