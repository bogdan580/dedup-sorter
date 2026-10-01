"""--restore and --verify-manifest modes: operate on an existing manifest, no scan."""
from __future__ import annotations

import argparse
import os

from tidy.fs.fsmodel import FileSystem
from tidy.moves import applier
from tidy.cli.common import _format_restore_summary, _format_verify_summary, _resolve_archive_root


def run_restore(args: argparse.Namespace, fs: FileSystem) -> int:
    # A relative manifest path is made absolute here: platform_win.to_extended()
    # requires an absolute path.
    manifest_path = args.restore if os.path.isabs(args.restore) else os.path.abspath(args.restore)
    restore_result = applier.restore_batch(fs, manifest_path, archive_root=_resolve_archive_root(args))
    print(_format_restore_summary(restore_result))
    return 0


def run_verify_manifest(args: argparse.Namespace, fs: FileSystem) -> int:
    manifest_path = (
        args.verify_manifest if os.path.isabs(args.verify_manifest)
        else os.path.abspath(args.verify_manifest)
    )
    verify_result = applier.verify_manifest(fs, manifest_path, archive_root=_resolve_archive_root(args))
    print(_format_verify_summary(verify_result))
    return 0
