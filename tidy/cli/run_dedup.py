"""Default mode: exact-duplicate scan, report, then --review or --apply."""
from __future__ import annotations

import argparse

from tidy.fs.fsmodel import FileSystem
from tidy.moves import applier
from tidy.duplicates import report
from tidy.duplicates import scanner
from tidy.cli.common import (
    _build_rows,
    _build_summary,
    _format_apply_summary,
    _resolve_archive_root,
    _scan_and_select,
)
from tidy.cli.run_review import run_review


def run_dedup(args: argparse.Namespace, fs: FileSystem) -> int:
    try:
        scan_result, tier_result, decisions, keeper_cfg = _scan_and_select(args, fs)
    except scanner.RemoveDirRefusedError as exc:
        print(str(exc))
        return 1

    summary = _build_summary(scan_result, tier_result)
    rows = _build_rows(scan_result, tier_result, decisions)
    report.write_csv(args.report, rows)

    grouped = report.build_grouped_report(
        tier_result.duplicate_groups, decisions, top_n=keeper_cfg.report_top_n
    )
    print(report.format_summary(summary))
    print(report.format_grouped_report(grouped))

    if getattr(args, "review", False):
        return run_review(args, fs, scan_result, tier_result, decisions)

    if getattr(args, "apply", False):
        apply_result = applier.apply_batch(
            fs, _resolve_archive_root(args), tier_result.duplicate_groups, decisions
        )
        print(_format_apply_summary(apply_result))

    return 0
