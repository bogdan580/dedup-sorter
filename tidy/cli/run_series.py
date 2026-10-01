"""--series mode: visually-similar photo series, plan only."""
from __future__ import annotations

import argparse

from tidy.store.cache import Cache
from tidy.fs.fsmodel import FileSystem
from tidy.duplicates import scanner
from tidy.analysis import series
from tidy.cli.common import _filter_candidates, _resolve_roots


def run_series(args: argparse.Namespace, fs: FileSystem) -> int:
    # The series mode doesn't need exact-duplicate tier2/3 hashing or
    # keeper selection at all -- it scans, then hands the same
    # is_duplicate_candidate list straight to series.find_series(),
    # skipping _scan_and_select()'s tiers.find_duplicates() (and its
    # interactive size-estimate confirmation) entirely.
    try:
        scan_result = scanner.scan(fs, _resolve_roots(args), extra_excluded_dir_names=args.exclude)
    except scanner.RemoveDirRefusedError as exc:
        print(str(exc))
        return 1
    candidates = _filter_candidates(scan_result, args.min_size, args.ext)
    cache = Cache(args.db)
    try:
        series_result = series.find_series(
            fs, candidates, cache,
            window_seconds=args.series_window_seconds,
            hamming_threshold=args.series_hamming_threshold,
            force_recompute=args.rescan,
            max_workers=args.workers,
        )
    finally:
        cache.close()
    series.write_series_csv(args.series_report, series_result)
    print(series.format_series_summary(series_result))
    return 0
