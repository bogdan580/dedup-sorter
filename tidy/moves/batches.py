r"""Which `!MOVES`/`!REMOVE` batches exist on disk, and how many of their
files a `--restore` run would actually move back.

Read-only, through the FileSystem seam like every other engine module here,
so it is testable against FakeFileSystem. `applier.resolve_dst()` is the
single place that knows `dst_rel` means an absolute path for `op=move` and
a batch-relative one for `op=remove`, and it is not re-derived here.

Behaviour: docs/technical/components/review-ui.md
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from tidy.moves import applier
from tidy import config
from tidy.moves import manifest
from tidy.fs.fsmodel import FileSystem


def _join(a: str, b: str) -> str:
    """Join with the separator `a` already uses, NOT always "/" like
    tidy/moves/applier's own _join() -- the manifest_path built here is also
    handed straight to os.path.exists() and manifest.read_final_rows()
    (plain open()) with NO seam in between.

    On a real archive, `a` (entry.path from fs.scandir()) already carries
    the `\\?\\` long-path prefix, which disables Win32's own "/" -> "\\"
    conversion entirely -- so a forward slash appended here would silently
    make os.path.exists() and open() fail to find a file that is really
    there. FakeFileSystem's paths never carry backslashes at all, so this
    keeps producing the forward-slash paths every existing test fixture
    expects.
    Seam: docs/technical/architecture.md"""
    sep = "\\" if "\\" in a else "/"
    a = a.rstrip("/\\")
    b = b.lstrip("/\\")
    return f"{a}{sep}{b}"


@dataclass(frozen=True)
class BatchSummary:
    kind: str          # "move" | "remove" (config.MOVES_DIR_NAME / REMOVE_DIR_NAME)
    dir_name: str       # "2026-08-17_22-31" -- the batch directory's own name
    manifest_path: str  # real path -- caller decides whether/how to show it;
                         # never a key the UI hands back to name a restore target
    done: int
    failed: int
    restorable: int      # done rows where dst exists and src is free -- see module docstring
    already_restored: bool
    error: str = ""       # set (only) when the manifest exists but failed to read


def _summarize_one(
    fs: FileSystem, kind: str, dir_name: str, batch_dir: str, manifest_path: str,
    archive_root: Optional[str] = None,
) -> BatchSummary:
    try:
        final_rows = manifest.read_final_rows(manifest_path)
    except (OSError, PermissionError, IndexError, ValueError) as exc:
        # manifest.read_rows() already tolerates a torn LAST line (crash
        # mid-write); what reaches here is real corruption -- a hand-edited
        # file, truncated mid-file, wrong delimiter -- and must not take
        # the rest of the list down with it.
        return BatchSummary(
            kind=kind, dir_name=dir_name, manifest_path=manifest_path,
            done=0, failed=0, restorable=0, already_restored=False, error=str(exc),
        )

    done = 0
    failed = 0
    restorable = 0
    for row in final_rows.values():
        if row.status == manifest.STATUS_DONE:
            done += 1
            # Same pair of checks restore_batch() itself gates on -- see
            # module docstring. A row restore_batch() would refuse (dst
            # missing, or src already occupied) must not be counted here.
            dst_abs = applier.resolve_dst(row, batch_dir)
            if applier.unsafe_row_reason(row, archive_root):
                continue
            if fs.exists(dst_abs) and not fs.exists(row.src_abs):
                restorable += 1
        elif row.status == manifest.STATUS_FAILED:
            failed += 1
        # status=planned rows never left src_abs -- not counted either way,
        # matching restore_batch()'s own "nothing to restore" skip.

    return BatchSummary(
        kind=kind, dir_name=dir_name, manifest_path=manifest_path,
        done=done, failed=failed, restorable=restorable,
        # A batch with nothing that ever succeeded is not "already
        # restored" -- it is a batch that never moved anything, and the two
        # need separate labels in the UI: "already restored" tells the user
        # their undo already happened, while a done=0 batch (every entry
        # failed or is still only planned) never had anything to undo in
        # the first place.
        already_restored=(done > 0 and restorable == 0),
    )


def list_batches(fs: FileSystem, archive_root: str) -> List[BatchSummary]:
    """Every `!MOVES\\<ts>\\`/`!REMOVE\\<ts>\\` batch under `archive_root`
    that has a `manifest.csv`, newest first (batch directory names are
    minute-resolution timestamps -- config.BATCH_DIR_TIMESTAMP_FORMAT --
    so plain string sort is chronological).

    Cost is one `scandir()` per root plus one `read_final_rows()` and a
    handful of `stat`-backed `exists()` calls per batch -- a dozen or two
    directories at most on this archive, meant to be called lazily (only
    when the "Cofnij" tab is opened) and cached by the caller, not on every
    request."""
    out: List[BatchSummary] = []
    for kind, dir_name_const in (
        ("move", config.MOVES_DIR_NAME),
        ("remove", config.REMOVE_DIR_NAME),
    ):
        root = _join(archive_root, dir_name_const)
        try:
            entries = list(fs.scandir(root))
        except (OSError, PermissionError):
            # No !MOVES or !REMOVE yet on a fresh archive -- not an error.
            continue

        for entry in entries:
            if entry.stat is None or not entry.stat.is_dir:
                continue
            manifest_path = _join(entry.path, config.MANIFEST_FILENAME)
            # Real os.path.exists(), not fs.exists(): manifest.csv is
            # written by manifest.py through plain open(), independent of
            # the FileSystem seam (manifest.py's own module docstring) --
            # fs only ever knows about the batch DIRECTORY (from scandir())
            # and the files apply_batch()/apply_moves() actually moved
            # through it, never the manifest file itself.
            if not os.path.exists(manifest_path):
                # !REMOVE also holds the moved-out files themselves
                # alongside each batch's manifest -- a directory entry with
                # no manifest.csv is either mid-write or foreign, and is
                # silently skipped rather than guessed at (module docstring).
                continue
            out.append(_summarize_one(fs, kind, entry.name, entry.path, manifest_path, archive_root))

    out.sort(key=lambda b: b.dir_name, reverse=True)
    return out


@dataclass(frozen=True)
class BatchEntry:
    """One `status=done` row of a single batch -- the "Cofnij" tab's lazy,
    per-batch thumbnail preview.

    `path` is applier.resolve_dst()'s answer -- where the file actually IS
    right now, never `dst_rel`/`src_abs` directly -- exactly like
    BatchSummary.restorable above is computed from the same call, so a
    thumbnail rendered from this path is always the same file
    restore_batch() would move."""
    path: str
    size: int
    restorable: bool


def list_batch_entries(
    fs: FileSystem, manifest_path: str, archive_root: Optional[str] = None,
) -> List[BatchEntry]:
    """Every `status=done` row of the batch at `manifest_path`, for
    tidy/review/handler/routes_restore.py's GET /api/batch-entries -- the per-batch detail the
    "Cofnij" row's restorable COUNT alone can't show.

    `batch_dir` is derived with os.path.dirname(), not the FileSystem seam:
    `manifest_path` is a REAL path here, read through plain open()/os.path
    exactly like list_batches() above handles it. os.path on Windows is
    ntpath, which treats "/" and "\\" as equivalent
    separators, so this resolves correctly for both a FakeFileSystem test's
    forward-slash manifest_path and a real archive's backslash,
    `\\\\?\\`-prefixed one -- the same guarantee _join()'s callers rely on.

    `restorable` reuses the EXACT SAME `fs.exists()` pair _summarize_one()
    uses for BatchSummary.restorable: a file this lists as restorable must
    never disagree with the count the "Cofnij" row already promised."""
    batch_dir = os.path.dirname(manifest_path)
    final_rows = manifest.read_final_rows(manifest_path)

    out: List[BatchEntry] = []
    for row in final_rows.values():
        if row.status != manifest.STATUS_DONE:
            continue
        dst_abs = applier.resolve_dst(row, batch_dir)
        restorable = (
            not applier.unsafe_row_reason(row, archive_root)
            and fs.exists(dst_abs) and not fs.exists(row.src_abs)
        )
        out.append(BatchEntry(path=dst_abs, size=row.size, restorable=restorable))
    return out
