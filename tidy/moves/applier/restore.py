from __future__ import annotations

import datetime
from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Set, Tuple

from tidy import config
from tidy.moves import manifest
from tidy.fs.fsmodel import FileSystem
from tidy.moves.applier._paths import (
    _default_clock,
    _format_ts,
    _join,
    _make_unique_batch_dir,
    _make_unique_rel,
    _parent_dir,
    _is_inside,
    _relative_to_root,
)


def resolve_dst(row: "manifest.ManifestRow", batch_dir: str) -> str:
    """Absolute destination a manifest row's file was moved to. The single
    place that knows `dst_rel` means different things per `op` -- see the
    note next to manifest.OP_MOVE.

    Public because batches.list_batches() needs the exact same resolution
    restore_batch()/verify_manifest() use to decide `restorable` -- a
    duplicated copy of this branch would risk the restore tab promising a
    restore that restore_batch() then refuses."""
    if row.op in (manifest.OP_MOVE, manifest.OP_COMPRESS):
        return row.dst_rel
    return _join(batch_dir, row.dst_rel)

def _has_parent_segment(path: str) -> bool:
    return ".." in path.replace("\\", "/").split("/")


def unsafe_row_reason(row: "manifest.ManifestRow", archive_root: Optional[str]) -> str:
    """Why a manifest row must not be acted on, or "" when it is safe.
    A manifest is user-editable input: its paths are validated, never trusted.
    No-op when `archive_root` is None.
    See: docs/technical/security.md"""
    if archive_root is None:
        return ""
    # _is_inside() is a string test, so a ".." segment could climb out of a matching prefix.
    if _has_parent_segment(row.src_abs) or not _is_inside(archive_root, row.src_abs):
        return "source path is outside the archive -- refused"
    if row.op in (manifest.OP_MOVE, manifest.OP_COMPRESS):
        if _has_parent_segment(row.dst_rel) or not _is_inside(archive_root, row.dst_rel):
            return "destination path is outside the archive -- refused"
        return ""
    rel = row.dst_rel
    if (
        rel.startswith(("/", "\\"))
        or (len(rel) >= 2 and rel[1] == ":")
        or _has_parent_segment(rel)
    ):
        return "batch-relative destination is unsafe -- refused"
    return ""


@dataclass(frozen=True)
class RestoreEntry:
    src_abs: str
    dst_abs: str
    status: str  # "restored" | "refused"
    detail: str = ""


@dataclass(frozen=True)
class RestoreResult:
    restored: int
    refused: int
    entries: Tuple[RestoreEntry, ...]


def restore_batch(
    fs: FileSystem,
    manifest_path: str,
    *,
    archive_root: Optional[str] = None,
    clock: Callable[[], datetime.datetime] = _default_clock,
) -> RestoreResult:
    """Reverse every completed (`status=done`, final state) move recorded
    in `manifest_path`. Never overwrites: if something already exists at a
    row's `src_abs`, that one entry is refused and reported, and the rest
    of the batch is still restored.

    Mirrors apply_batch()'s per-entry resilience: an ordinary OSError/
    PermissionError restoring one file (locked, permissions, path issue)
    must not abort recovery of the rest of the batch -- restore is the
    operation a user relies on precisely when something has already gone
    wrong, so it must not be more fragile than apply.

    Handles `op=move` rows as well as `op=remove`: the only difference is
    where the file is now (resolve_dst), plus recreating the original
    parent directory for move rows -- see below.

    `op=compress` rows are handled differently from every other op:
    apply_compressions() never touched the original, so there is nothing
    to move back to `src_abs`. Restoring a compress
    row instead moves the `_compressed` result itself into a fresh
    `!REMOVE\\<ts>\\` batch -- undoing the compression by discarding its
    output through the exact same reversible mechanism as any other
    removal, so *that* action can itself be undone later. `archive_root`
    is required only when the manifest being restored contains such rows."""
    final_rows = manifest.read_final_rows(manifest_path)
    batch_dir = _parent_dir(manifest_path) or ""

    restored = 0
    refused = 0
    entries: List[RestoreEntry] = []
    compress_undo_manifest: Optional[str] = None
    compress_undo_claimed: Set[str] = set()

    for src_abs, row in final_rows.items():
        if row.status != manifest.STATUS_DONE:
            # planned/failed rows never left src_abs -- nothing to restore.
            continue
        dst_abs = resolve_dst(row, batch_dir)

        unsafe = unsafe_row_reason(row, archive_root)
        if unsafe:
            refused += 1
            entries.append(RestoreEntry(
                src_abs=src_abs, dst_abs=dst_abs, status="refused", detail=unsafe,
            ))
            continue

        if row.op == manifest.OP_COMPRESS:
            if archive_root is None:
                raise ValueError(
                    "restore_batch() requires archive_root to restore an op=compress manifest"
                )
            if not fs.exists(dst_abs):
                refused += 1
                entries.append(RestoreEntry(
                    src_abs=src_abs, dst_abs=dst_abs, status="refused",
                    detail="compressed result no longer exists -- restore refused for this entry",
                ))
                continue

            if compress_undo_manifest is None:
                remove_root = _join(archive_root, config.REMOVE_DIR_NAME)
                fs.makedirs(remove_root)
                fs.touch(_join(remove_root, config.KOSZ_SENTINEL_FILENAME))
                undo_name = clock().strftime(config.BATCH_DIR_TIMESTAMP_FORMAT)
                undo_batch_dir = _make_unique_batch_dir(fs, remove_root, undo_name)
                fs.makedirs(undo_batch_dir)
                compress_undo_manifest = _join(undo_batch_dir, config.MANIFEST_FILENAME)
            else:
                undo_batch_dir = _parent_dir(compress_undo_manifest) or ""

            undo_dst_rel = _make_unique_rel(
                _relative_to_root(dst_abs, archive_root), compress_undo_claimed
            )
            undo_dst_abs = _join(undo_batch_dir, undo_dst_rel)

            undo_row = manifest.ManifestRow(
                op=manifest.OP_REMOVE,
                src_abs=dst_abs,
                dst_rel=undo_dst_rel,
                size=row.size,
                mtime=row.mtime,
                hash=row.hash,
                group_id=row.group_id,
                keeper_abs=row.keeper_abs,
                reason=f"undo of compression: {row.reason}",
                ts=_format_ts(clock()),
                tool_version=config.TOOL_VERSION,
                status=manifest.STATUS_PLANNED,
            )
            manifest.append_planned(compress_undo_manifest, undo_row)
            try:
                fs.makedirs(_parent_dir(undo_dst_abs) or undo_batch_dir)
                fs.move(dst_abs, undo_dst_abs)
            except (OSError, PermissionError) as exc:
                manifest.append_confirmation(
                    compress_undo_manifest,
                    replace(undo_row, ts=_format_ts(clock()), status=manifest.STATUS_FAILED, error=str(exc)),
                )
                refused += 1
                entries.append(RestoreEntry(
                    src_abs=src_abs, dst_abs=undo_dst_abs, status="refused",
                    detail=f"restore failed: {exc}",
                ))
                continue

            manifest.append_confirmation(
                compress_undo_manifest,
                replace(undo_row, ts=_format_ts(clock()), status=manifest.STATUS_DONE),
            )
            restored += 1
            entries.append(RestoreEntry(src_abs=src_abs, dst_abs=undo_dst_abs, status="restored"))
            continue

        if fs.exists(src_abs):
            refused += 1
            entries.append(RestoreEntry(
                src_abs=src_abs, dst_abs=dst_abs, status="refused",
                detail="destination already exists -- restore refused for this entry",
            ))
            continue

        try:
            if row.op == manifest.OP_MOVE:
                # Only for moves, and only here: sorting a folder out of
                # !NEW empties it, and emptied folders are exactly the ones
                # a user tidies away by hand afterwards. Without this, a
                # perfectly ordinary "I moved the photos, then deleted the
                # leftover empty !NEW\Gdansk" makes the batch unrestorable.
                # op=remove rows deliberately do not: their source parents
                # live in the archive proper, which this tool never empties.
                parent = _parent_dir(src_abs)
                if parent:
                    fs.makedirs(parent)
            fs.move(dst_abs, src_abs)
        except (OSError, PermissionError) as exc:
            refused += 1
            entries.append(RestoreEntry(
                src_abs=src_abs, dst_abs=dst_abs, status="refused",
                detail=f"restore failed: {exc}",
            ))
            continue

        restored += 1
        entries.append(RestoreEntry(src_abs=src_abs, dst_abs=dst_abs, status="restored"))

    return RestoreResult(restored=restored, refused=refused, entries=tuple(entries))
