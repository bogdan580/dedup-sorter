from __future__ import annotations

import datetime
from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Sequence, Set

from tidy import config
from tidy.duplicates import hashing
from tidy.moves import manifest
from tidy.fs.fsmodel import FileSystem
from tidy.duplicates.keeper import REASON_COMPRESS_REMOVE, Discard, KeeperDecision
from tidy.duplicates.report import display_path
from tidy.duplicates.tiers import DuplicateGroup
from tidy.moves.applier._paths import (
    _default_clock,
    _format_ts,
    _is_inside,
    _join,
    _make_unique_abs,
    _norm_key,
    _parent_dir,
    _safe_mtime,
    _safe_size,
    reserve_batch_dir,
)
from tidy.moves.applier.remove import ApplyEntry, ApplyResult, apply_batch


@dataclass(frozen=True)
class CompressPlan:
    """One verified, staged encode to move into the archive next to its
    untouched original. Unlike MovePlan, the file this function moves
    (`staging_abs`) is never the row's subject (`original_abs`) -- the
    original is not read, hashed, or touched by this function at all.
    Removing it later (a separate, explicit decision) is
    apply_compress_removals()'s job, not this one's.

    Callers MUST only pass a plan whose `staging_abs` already passed
    encoder.verify_output() -- this function has no opinion on video
    quality or correctness, only on where a confirmed file ends up."""
    original_abs: str
    staging_abs: str
    dst_abs: str  # <original>_compressed.<ext>, absolute, next to the original
    group_id: str = ""
    reason: str = ""


def _refuse_compress_reason(archive_root: str, plan: CompressPlan) -> str:
    """Same containment discipline as _refuse_move_reason(), applied to the
    compressed result's destination. The original is never moved by this
    function, so it needs no such check here."""
    remove_root = _join(archive_root, config.REMOVE_DIR_NAME)
    moves_root = _join(archive_root, config.MOVES_DIR_NAME)

    if not _is_inside(archive_root, plan.dst_abs):
        return "destination is outside the archive root"
    if _is_inside(remove_root, plan.dst_abs):
        return f"destination is inside {config.REMOVE_DIR_NAME}"
    if _is_inside(moves_root, plan.dst_abs):
        return f"destination is inside {config.MOVES_DIR_NAME} -- that directory holds manifests, not videos"
    if _norm_key(display_path(plan.dst_abs)) == _norm_key(display_path(plan.original_abs)):
        return "destination is the same path as the original"
    return ""


def apply_compressions(
    fs: FileSystem,
    archive_root: str,
    plans: Sequence[CompressPlan],
    manifest_path: Optional[str] = None,
    *,
    clock: Callable[[], datetime.datetime] = _default_clock,
    batch_dir: Optional[str] = None,
) -> ApplyResult:
    """Move each confirmed, verified staged encode into the archive next to
    its original, writing an `op=compress` manifest under `!MOVES\\<ts>\\`
    (or appending to the one in `batch_dir`, see reserve_batch_dir())
    -- structurally a sibling of apply_moves(), sharing its batch-dir /
    manifest-under-!MOVES / absolute-dst_rel shape, but moving
    `plan.staging_abs` rather than the row's own `src_abs`: the original is
    never touched here. See: docs/technical/components/manifest-and-db.md

    `size`/`hash` are read from the staged file (the RESULT), matching
    manifest.py's op=compress documentation; `keeper_abs` is set to
    `original_abs` since a compress row discards nothing. Per-entry
    resilience matches apply_moves() exactly.
    """
    if batch_dir is None:
        batch_dir = reserve_batch_dir(fs, archive_root, config.MOVES_DIR_NAME, clock())

    if manifest_path is None:
        manifest_path = _join(batch_dir, config.MANIFEST_FILENAME)

    claimed: Set[str] = set()
    moved = 0
    failed = 0
    entries: List[ApplyEntry] = []

    for plan in plans:
        refusal = _refuse_compress_reason(archive_root, plan)
        dst_abs = plan.dst_abs if refusal else _make_unique_abs(fs, plan.dst_abs, claimed)

        size = 0 if refusal else _safe_size(fs, plan.staging_abs)
        file_hash = ""
        if not refusal:
            try:
                with fs.open_read(plan.staging_abs) as stream:
                    file_hash = hashing.full_hash(stream)
            except (OSError, PermissionError):
                file_hash = ""

        planned_row = manifest.ManifestRow(
            op=manifest.OP_COMPRESS,
            src_abs=plan.original_abs,
            dst_rel=dst_abs,
            size=size,
            mtime=0 if refusal else _safe_mtime(fs, plan.staging_abs),
            hash=file_hash,
            group_id=plan.group_id,
            keeper_abs=plan.original_abs,
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
            entries.append(ApplyEntry(src_abs=plan.original_abs, dst_abs=dst_abs, status=manifest.STATUS_FAILED, error=refusal))
            continue

        try:
            fs.makedirs(_parent_dir(dst_abs) or archive_root)
            fs.move(plan.staging_abs, dst_abs)
        except (OSError, PermissionError) as exc:
            manifest.append_confirmation(
                manifest_path,
                replace(planned_row, ts=_format_ts(clock()), status=manifest.STATUS_FAILED, error=str(exc)),
            )
            failed += 1
            entries.append(ApplyEntry(src_abs=plan.original_abs, dst_abs=dst_abs, status=manifest.STATUS_FAILED, error=str(exc)))
            continue

        manifest.append_confirmation(
            manifest_path,
            replace(planned_row, ts=_format_ts(clock()), status=manifest.STATUS_DONE),
        )
        moved += 1
        entries.append(ApplyEntry(src_abs=plan.original_abs, dst_abs=dst_abs, status=manifest.STATUS_DONE))

    return ApplyResult(
        moved=moved, failed=failed, batch_dir=batch_dir, manifest_path=manifest_path,
        entries=tuple(entries),
    )


# --- apply_compress_removals (op=remove, discarding a compressed original) --

@dataclass(frozen=True)
class CompressRemoval:
    """One already-applied, confirmed op=compress result whose original is
    now eligible to be discarded. Callers MUST build this list only from
    apply_compressions()'s own ApplyResult.entries filtered to
    status=manifest.STATUS_DONE -- never from the request list handed to
    apply_compressions() -- so a failed encode or a failed move can never
    reach this function. There is no flag here to skip a failed one, because a failed one is
    never constructable in the first place.
    See: docs/business/rules/compression.md"""
    original_abs: str
    compressed_abs: str
    group_id: str = ""


def apply_compress_removals(
    fs: FileSystem,
    archive_root: str,
    removals: Sequence[CompressRemoval],
    manifest_path: Optional[str] = None,
    *,
    clock: Callable[[], datetime.datetime] = _default_clock,
    batch_dir: Optional[str] = None,
) -> ApplyResult:
    """Move each removal's original into a fresh `!REMOVE\\<ts>\\` batch
    (or the reserved `batch_dir`, passed through to apply_batch()),
    separate from the `!MOVES\\<ts>\\` batch apply_compressions() wrote for
    the same run (two manifests, never one mixed; docs/technical/components/manifest-and-db.md).

    A thin overlay over apply_batch(), not a reimplementation: its only job is turning each confirmed compress result
    into the DuplicateGroup/KeeperDecision shape apply_batch() already
    knows how to plan, move, and manifest reversibly -- one singleton
    group per removal, keeper=the compressed result, discard=the original.

    Second safety net (docs/business/rules/compression.md): a removal whose compressed
    result is not strictly smaller than the original is never handed to
    apply_batch() -- recorded instead as a failed entry with no manifest
    row and the original never touched, regardless of what the caller
    (--compress-remove-originals / the review UI checkbox) asked for.
    This is deliberately independent of whatever already-passed
    verification produced the CompressRemoval: encoder.verify_output()
    checks duration/frame count/quality, never size.
    """
    duplicate_groups: List[DuplicateGroup] = []
    decisions: List[KeeperDecision] = []
    extra_entries: List[ApplyEntry] = []

    for removal in removals:
        try:
            original_size = fs.stat(removal.original_abs).size
        except (OSError, PermissionError):
            extra_entries.append(ApplyEntry(
                src_abs=removal.original_abs, dst_abs="", status=manifest.STATUS_FAILED,
                error="original no longer readable -- removal skipped",
            ))
            continue
        try:
            compressed_size = fs.stat(removal.compressed_abs).size
        except (OSError, PermissionError):
            extra_entries.append(ApplyEntry(
                src_abs=removal.original_abs, dst_abs="", status=manifest.STATUS_FAILED,
                error="compressed result no longer readable -- removal skipped",
            ))
            continue

        if compressed_size >= original_size:
            extra_entries.append(ApplyEntry(
                src_abs=removal.original_abs, dst_abs="", status=manifest.STATUS_FAILED,
                error=(
                    f"compressed result ({compressed_size}B) is not smaller than "
                    f"the original ({original_size}B) -- removal skipped"
                ),
            ))
            continue

        duplicate_groups.append(DuplicateGroup(
            size=original_size, full_hash="", paths=(removal.original_abs,),
        ))
        decisions.append(KeeperDecision(
            keeper=removal.compressed_abs,
            discarded=(Discard(
                path=removal.original_abs, reason=REASON_COMPRESS_REMOVE, size=original_size,
            ),),
        ))

    result = apply_batch(
        fs, archive_root, duplicate_groups, decisions, manifest_path,
        clock=clock, batch_dir=batch_dir,
    )

    if not extra_entries:
        return result
    return replace(
        result,
        failed=result.failed + len(extra_entries),
        entries=result.entries + tuple(extra_entries),
    )
