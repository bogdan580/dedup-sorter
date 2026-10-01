from __future__ import annotations

import datetime
from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Sequence, Set, Tuple

from tidy import config
from tidy.moves import manifest
from tidy.fs.fsmodel import FileSystem
from tidy.duplicates.keeper import KeeperDecision
from tidy.duplicates.tiers import DuplicateGroup
from tidy.moves.applier._paths import (
    _default_clock,
    _format_ts,
    _join,
    _make_unique_rel,
    _parent_dir,
    _relative_to_root,
    _safe_mtime,
    reserve_batch_dir,
)


@dataclass(frozen=True)
class ApplyEntry:
    src_abs: str
    dst_abs: str
    status: str  # manifest.STATUS_DONE | manifest.STATUS_FAILED
    error: str = ""


@dataclass(frozen=True)
class ApplyResult:
    moved: int
    failed: int
    batch_dir: str
    manifest_path: str
    entries: Tuple[ApplyEntry, ...]


def apply_batch(
    fs: FileSystem,
    archive_root: str,
    duplicate_groups: Sequence[DuplicateGroup],
    decisions: Sequence[KeeperDecision],
    manifest_path: Optional[str] = None,
    *,
    clock: Callable[[], datetime.datetime] = _default_clock,
    batch_dir: Optional[str] = None,
) -> ApplyResult:
    """Move every discard from `decisions` into a fresh
    `!REMOVE\\<RRRR-MM-DD_gg-mm>\\` batch directory, writing a
    planned-then-done/failed manifest row for each. A `batch_dir` from
    reserve_batch_dir() is reused instead, appending to its manifest.

    A single discard's move failing (OSError/PermissionError: locked file,
    permission denied, path too long) is recorded as status=failed and does
    NOT abort the batch. Any other exception (anything other than
    OSError/PermissionError) propagates out of this function uncaught:
    whatever was already confirmed stays confirmed, the interrupted entry
    is left with only its `planned` row on disk, and nothing after it in
    iteration order is even attempted.

    Each discard's own `size`/`hash` override the group's shared values
    when set, falling back to `group.size`/`group.full_hash` otherwise.
    Series discards set both explicitly, since series members were never
    hashed byte-for-byte and share no size; verify_manifest()'s
    `if row.hash:` guard already treats an empty hash as "nothing to
    check" rather than a mismatch.
    """
    if batch_dir is None:
        batch_dir = reserve_batch_dir(fs, archive_root, config.REMOVE_DIR_NAME, clock())

    if manifest_path is None:
        manifest_path = _join(batch_dir, config.MANIFEST_FILENAME)

    claimed: Set[str] = set()
    moved = 0
    failed = 0
    entries: List[ApplyEntry] = []

    for group_id, (group, decision) in enumerate(zip(duplicate_groups, decisions), start=1):
        for discard in decision.discarded:
            src_rel = _relative_to_root(discard.path, archive_root)
            dst_rel = _make_unique_rel(src_rel, claimed)
            # A reused batch_dir may already hold an earlier call's files.
            while fs.exists(_join(batch_dir, dst_rel)):
                dst_rel = _make_unique_rel(src_rel, claimed)
            dst_abs = _join(batch_dir, dst_rel)

            planned_row = manifest.ManifestRow(
                op=manifest.OP_REMOVE,
                src_abs=discard.path,
                dst_rel=dst_rel,
                size=discard.size if discard.size is not None else group.size,
                mtime=_safe_mtime(fs, discard.path),
                hash=discard.hash if discard.hash else group.full_hash,
                group_id=str(group_id),
                keeper_abs=decision.keeper,
                reason=discard.reason,
                ts=_format_ts(clock()),
                tool_version=config.TOOL_VERSION,
                status=manifest.STATUS_PLANNED,
            )
            manifest.append_planned(manifest_path, planned_row)

            try:
                fs.makedirs(_parent_dir(dst_abs) or batch_dir)
                fs.move(discard.path, dst_abs)
            except (OSError, PermissionError) as exc:
                manifest.append_confirmation(
                    manifest_path,
                    replace(planned_row, ts=_format_ts(clock()), status=manifest.STATUS_FAILED, error=str(exc)),
                )
                failed += 1
                entries.append(ApplyEntry(src_abs=discard.path, dst_abs=dst_abs, status=manifest.STATUS_FAILED, error=str(exc)))
                continue

            manifest.append_confirmation(
                manifest_path,
                replace(planned_row, ts=_format_ts(clock()), status=manifest.STATUS_DONE),
            )
            moved += 1
            entries.append(ApplyEntry(src_abs=discard.path, dst_abs=dst_abs, status=manifest.STATUS_DONE))

    return ApplyResult(
        moved=moved, failed=failed, batch_dir=batch_dir, manifest_path=manifest_path,
        entries=tuple(entries),
    )
