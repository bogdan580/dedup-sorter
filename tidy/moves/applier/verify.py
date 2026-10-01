from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from tidy import config
from tidy.duplicates import hashing
from tidy.moves import manifest
from tidy.fs.fsmodel import FileSystem
from tidy.moves.applier._paths import _norm_key, _parent_dir, _relative_to_root
from tidy.moves.applier.restore import resolve_dst, unsafe_row_reason


@dataclass(frozen=True)
class VerifyReport:
    planned_without_move: Tuple[str, ...]
    orphans: Tuple[str, ...]
    mismatches: Tuple[str, ...]


def _list_files_recursive(fs: FileSystem, dir_path: str) -> List[str]:
    files: List[str] = []
    try:
        entries = list(fs.scandir(dir_path))
    except (OSError, PermissionError):
        return files
    for entry in entries:
        if entry.stat is None:
            continue
        if entry.stat.is_dir:
            files.extend(_list_files_recursive(fs, entry.path))
        else:
            files.append(entry.path)
    return files


def verify_manifest(
    fs: FileSystem, manifest_path: str, *, archive_root: Optional[str] = None,
) -> VerifyReport:
    """Read-only repair-mode report: never modifies anything, only lists
    three findings for a human to act on:
      - planned_without_move: rows whose final status is still `planned`
        (an --apply run that was interrupted before the move resolved).
      - orphans: files physically present in the batch dir that no
        `status=done` row accounts for.
      - mismatches: for `status=done` rows, the destination's current
        size/hash doesn't match what the manifest recorded.

    Handles both ops. For an `op=move` batch the batch directory holds
    nothing but the manifest, so the orphan scan below has nothing to find
    -- which is correct, not a gap: an op=move destination lives in the
    archive proper, where "a file the manifest doesn't account for" is the
    normal state of every other photo you own. Size/hash checking, on the
    other hand, applies to move rows exactly as it does to remove rows.
    """
    final_rows = manifest.read_final_rows(manifest_path)
    batch_dir = _parent_dir(manifest_path) or ""

    planned_without_move = tuple(sorted(
        row.src_abs for row in final_rows.values() if row.status == manifest.STATUS_PLANNED
    ))

    done_rows = [row for row in final_rows.values() if row.status == manifest.STATUS_DONE]
    # Only op=remove rows can account for a file sitting in the batch dir;
    # op=move/op=compress rows' dst_rel is an absolute path somewhere else
    # entirely and must not be mistaken for a batch-relative one.
    done_dst_keys = {
        _norm_key(row.dst_rel) for row in done_rows
        if row.op not in (manifest.OP_MOVE, manifest.OP_COMPRESS)
    }
    manifest_filename_key = _norm_key(config.MANIFEST_FILENAME)

    orphans: List[str] = []
    for file_path in _list_files_recursive(fs, batch_dir):
        rel = _relative_to_root(file_path, batch_dir)
        if _norm_key(rel) == manifest_filename_key:
            continue
        if _norm_key(rel) not in done_dst_keys:
            orphans.append(file_path)

    mismatches: List[str] = []
    for row in done_rows:
        unsafe = unsafe_row_reason(row, archive_root)
        if unsafe:
            mismatches.append(f"{row.src_abs}: {unsafe}")
            continue
        dst_abs = resolve_dst(row, batch_dir)
        try:
            st = fs.stat(dst_abs)
        except (OSError, PermissionError):
            mismatches.append(f"{row.src_abs}: destination missing at {dst_abs}")
            continue
        if st.size != row.size:
            mismatches.append(
                f"{row.src_abs}: size mismatch (manifest={row.size}, actual={st.size})"
            )
            continue
        if row.hash:
            try:
                with fs.open_read(dst_abs) as stream:
                    actual_hash = hashing.full_hash(stream)
            except (OSError, PermissionError):
                actual_hash = None
            if actual_hash is not None and actual_hash != row.hash:
                mismatches.append(f"{row.src_abs}: hash mismatch at {dst_abs}")

    return VerifyReport(
        planned_without_move=planned_without_move,
        orphans=tuple(sorted(orphans)),
        mismatches=tuple(mismatches),
    )
