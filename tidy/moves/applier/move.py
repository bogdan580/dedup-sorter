from __future__ import annotations

import datetime
from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Sequence, Set

from tidy import config
from tidy.moves import manifest
from tidy.fs.fsmodel import FileSystem
from tidy.duplicates.keeper import REASON_EVENT_SORT
from tidy.duplicates.report import display_path
from tidy.moves.applier._paths import (
    _default_clock,
    _format_ts,
    _is_inside,
    _join,
    _make_unique_abs,
    _make_unique_batch_dir,
    _norm_key,
    _parent_dir,
    _safe_mtime,
    _safe_size,
)
from tidy.moves.applier.remove import ApplyEntry, ApplyResult


@dataclass(frozen=True)
class MovePlan:
    """One file to relocate *inside* the archive. `dst_abs` is the fully
    resolved destination the caller decided on -- this module never derives
    a folder name from a place name or a date. That split is deliberate:
    naming is a review-screen concern (the user can edit the suggested
    folder), while everything below is about performing the move safely and
    reversibly.

    `size`/`hash` are optional. Left unset, `size` is read from disk (so
    verify_manifest() has something real to compare against) and `hash`
    stays empty, which verify_manifest()'s `if row.hash:` guard already
    treats as "nothing to check" -- placement suggestions never hash their
    files byte-for-byte, exactly like series discards."""
    src_abs: str
    dst_abs: str
    group_id: str = ""
    reason: str = REASON_EVENT_SORT
    size: Optional[int] = None
    hash: str = ""


def _refuse_move_reason(archive_root: str, plan: MovePlan) -> str:
    """Return "" when `plan` is safe to perform, or a human-readable reason
    to refuse it. Every check here is a containment invariant that must
    hold no matter what the caller passed: the review screen is expected to
    validate the destination too (it accepts a user-edited folder name), but
    this is the layer that actually calls fs.move(), so it does not trust
    that it was called correctly."""
    remove_root = _join(archive_root, config.REMOVE_DIR_NAME)
    moves_root = _join(archive_root, config.MOVES_DIR_NAME)

    if not _is_inside(archive_root, plan.src_abs):
        return "source is outside the archive root"
    if not _is_inside(archive_root, plan.dst_abs):
        return "destination is outside the archive root"
    if _norm_key(display_path(plan.dst_abs)) == _norm_key(display_path(archive_root)):
        return "destination is the archive root itself, not a file path"
    if _is_inside(remove_root, plan.dst_abs):
        # A "move" into the trash would be a deletion wearing a sorting
        # op's clothes: it would skip apply_batch()'s batch-dir layout and
        # show up in reports as a placement rather than a removal.
        return f"destination is inside {config.REMOVE_DIR_NAME} -- removals go through apply_batch()"
    if _is_inside(moves_root, plan.dst_abs):
        return f"destination is inside {config.MOVES_DIR_NAME} -- that directory holds manifests, not photos"
    if _norm_key(display_path(plan.src_abs)) == _norm_key(display_path(plan.dst_abs)):
        return "source and destination are the same path"
    return ""


def apply_moves(
    fs: FileSystem,
    archive_root: str,
    plans: Sequence[MovePlan],
    manifest_path: Optional[str] = None,
    *,
    clock: Callable[[], datetime.datetime] = _default_clock,
) -> ApplyResult:
    """Relocate files *inside* the archive per the placement suggestions,
    writing an `op=move` manifest so `--restore` can put every one of them
    back.

    Structurally a sibling of apply_batch(), not a mode of it: folding them
    together would put a branch inside the one function allowed to
    rearrange the user's archive.

    Differences from apply_batch(), all of them consequences of the
    destination being live archive rather than a fresh trash directory:

      - The batch directory under `!MOVES\\<ts>\\` holds *only* the
        manifest. The files themselves are never in it.
      - `dst_rel` carries an absolute path: there is no batch directory
        for it to be relative to, and `--restore` gets nothing but a
        manifest path to work from.
      - Collisions are resolved against what is already on disk, not just
        against this batch (_make_unique_abs).
      - A destination that would leave the archive, land in `!REMOVE`, or
        collide with the manifest store is refused per entry
        (_refuse_move_reason) and recorded as `status=failed` -- refused
        rather than raised, so one bad suggestion cannot abort a batch that
        is otherwise fine, and recorded rather than skipped, so a refusal
        is never silent.

    Per-entry resilience matches apply_batch() exactly: an OSError/
    PermissionError on one move is recorded as failed and the batch
    continues; anything else propagates with the interrupted entry left
    `planned` on disk.

    A whole-folder suggestion is passed in as one MovePlan per file, never
    as a directory rename. A rename could not merge into an existing
    destination folder, and it could not be partially restored -- both of
    which this archive needs, since suggestions routinely target a
    `#Home` that already exists.
    See: docs/business/rules/version-selection.md
    """
    moves_root = _join(archive_root, config.MOVES_DIR_NAME)
    fs.makedirs(moves_root)

    batch_name = clock().strftime(config.BATCH_DIR_TIMESTAMP_FORMAT)
    batch_dir = _make_unique_batch_dir(fs, moves_root, batch_name)
    fs.makedirs(batch_dir)

    if manifest_path is None:
        manifest_path = _join(batch_dir, config.MANIFEST_FILENAME)

    claimed: Set[str] = set()
    moved = 0
    failed = 0
    entries: List[ApplyEntry] = []

    for plan in plans:
        refusal = _refuse_move_reason(archive_root, plan)
        # A refused plan keeps its requested destination in the manifest
        # (not a uniquified one): the row is a record of what was asked for
        # and rejected, and reserving a name for a move that will not happen
        # would push a later, legitimate plan onto a __dup2 suffix.
        dst_abs = plan.dst_abs if refusal else _make_unique_abs(fs, plan.dst_abs, claimed)

        # A refused plan is never stat()ed: the refusal was decided from the
        # paths alone, and a source that lives outside the archive (one of
        # the refusal reasons) may not be readable -- or exist -- at all.
        # Its row records what was asked for, and 0/0 is the honest size and
        # mtime for a file that was never touched.
        size = plan.size if plan.size is not None else (0 if refusal else _safe_size(fs, plan.src_abs))
        mtime = 0 if refusal else _safe_mtime(fs, plan.src_abs)

        planned_row = manifest.ManifestRow(
            op=manifest.OP_MOVE,
            src_abs=plan.src_abs,
            dst_rel=dst_abs,
            size=size,
            mtime=mtime,
            hash=plan.hash,
            group_id=plan.group_id,
            keeper_abs="",  # a move discards nothing, so there is no keeper.
            reason=plan.reason,
            ts=_format_ts(clock()),
            tool_version=config.TOOL_VERSION,
            status=manifest.STATUS_PLANNED,
        )
        manifest.append_planned(manifest_path, planned_row)

        if refusal:
            manifest.append_confirmation(
                manifest_path,
                replace(planned_row, ts=_format_ts(clock()), status=manifest.STATUS_FAILED, error=refusal),
            )
            failed += 1
            entries.append(ApplyEntry(src_abs=plan.src_abs, dst_abs=dst_abs, status=manifest.STATUS_FAILED, error=refusal))
            continue

        try:
            fs.makedirs(_parent_dir(dst_abs) or archive_root)
            fs.move(plan.src_abs, dst_abs)
        except (OSError, PermissionError) as exc:
            manifest.append_confirmation(
                manifest_path,
                replace(planned_row, ts=_format_ts(clock()), status=manifest.STATUS_FAILED, error=str(exc)),
            )
            failed += 1
            entries.append(ApplyEntry(src_abs=plan.src_abs, dst_abs=dst_abs, status=manifest.STATUS_FAILED, error=str(exc)))
            continue

        manifest.append_confirmation(
            manifest_path,
            replace(planned_row, ts=_format_ts(clock()), status=manifest.STATUS_DONE),
        )
        moved += 1
        entries.append(ApplyEntry(src_abs=plan.src_abs, dst_abs=dst_abs, status=manifest.STATUS_DONE))

    return ApplyResult(
        moved=moved, failed=failed, batch_dir=batch_dir, manifest_path=manifest_path,
        entries=tuple(entries),
    )
