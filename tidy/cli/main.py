"""CLI entry: dispatch one run to its mode module."""
from __future__ import annotations

import argparse
from typing import List, Optional

from tidy.fs.fsmodel import FileSystem
from tidy.moves import applier
from tidy.cli.args import build_arg_parser
from tidy.cli.common import _resolve_archive_root
from tidy.cli.run_compress import _run_compress
from tidy.cli.run_dedup import run_dedup
from tidy.cli.run_events import run_events
from tidy.cli.run_inbox import run_inbox
from tidy.cli.run_restore import run_restore, run_verify_manifest
from tidy.cli.run_series import run_series


def run(args: argparse.Namespace, fs: FileSystem) -> int:
    """Orchestrate one full dry-run scan, or --restore/--verify-manifest
    against a manifest from a previous --apply run, or --review's local
    review UI. Returns a process exit code. Takes `fs` as a parameter
    (rather than constructing WindowsFileSystem itself) so tests can inject
    a FakeFileSystem -- see main() for the real-filesystem wiring."""
    for name in applier.ensure_archive_dirs(fs, _resolve_archive_root(args)):
        print(f"Created missing directory: {name}")

    if getattr(args, "compress", False):
        # --compress lives outside apply_group (see build_arg_parser()) so
        # --apply can modify it; its conflicts with the group's other
        # modes must therefore be rejected here instead of by argparse.
        conflicting = (
            getattr(args, "restore", None) or getattr(args, "verify_manifest", None)
            or getattr(args, "review", False) or getattr(args, "series", False)
            or getattr(args, "events", False) or getattr(args, "inbox", False)
        )
        if conflicting:
            print(
                "Error: --compress cannot be combined with "
                "--restore/--verify-manifest/--review/--series/--events/--inbox."
            )
            return 1
        return _run_compress(args, fs)

    # build_arg_parser()'s mutually-exclusive group guarantees at most one
    # of restore/verify_manifest/apply/review is set.
    if getattr(args, "restore", None):
        return run_restore(args, fs)
    if getattr(args, "verify_manifest", None):
        return run_verify_manifest(args, fs)
    if getattr(args, "series", False):
        return run_series(args, fs)
    if getattr(args, "events", False):
        return run_events(args, fs)
    if getattr(args, "inbox", False):
        return run_inbox(args, fs)
    return run_dedup(args, fs)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    from tidy.fs.platform_win import WindowsFileSystem  # lazy: keeps this module OS-agnostic at import time
    return run(args, WindowsFileSystem())
