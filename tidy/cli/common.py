"""Helpers shared by the CLI modes: filtering, summaries, root resolution, scan pipeline."""
from __future__ import annotations

import argparse
import os
import re
from datetime import timedelta
from typing import Callable, List, Optional, Tuple

from tidy.store.cache import Cache
from tidy.fs.fsmodel import FileSystem
from tidy.moves import applier
from tidy.duplicates import keeper
from tidy.duplicates import report
from tidy.duplicates import scanner
from tidy.duplicates import tiers


def build_confirm(
    yes: bool,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> Callable[[str], bool]:
    """Build the `confirm` callable injected into tiers.find_duplicates().
    With --yes, always confirms (but the estimate is still printed first,
    per requirement 9); otherwise prompts interactively."""

    def confirm(message: str) -> bool:
        print_func(message)
        if yes:
            return True
        answer = input_func("Continue? [y/N]: ")
        return answer.strip().lower() in ("y", "yes", "t", "tak")

    return confirm


class _BypassReadCache:
    """Wraps a Cache so is_hit()/get() never report a hit, forcing every
    candidate to be recomputed -- used for --rescan. Writes still flow
    through to the real cache so future (non-rescan) runs benefit."""

    def __init__(self, inner: Cache) -> None:
        self._inner = inner

    def is_hit(self, path: str, size: int, mtime_ns: int) -> bool:
        return False

    def get(self, path: str):
        return None

    def put(self, cached_file) -> None:
        self._inner.put(cached_file)

    def commit(self) -> None:
        self._inner.commit()

    def close(self) -> None:
        self._inner.close()


def _normalize_extension(ext: str) -> str:
    """Accept both "jpg" and ".jpg" from the user; normalize to a leading
    dot, casefolded, so it can be compared directly against
    os.path.splitext()[1].casefold()."""
    ext = ext if ext.startswith(".") else f".{ext}"
    return ext.casefold()


def _filter_candidates(
    scan_result: scanner.ScanResult,
    min_size: Optional[int],
    extensions: Optional[List[str]] = None,
) -> List[scanner.ScannedFile]:
    candidates = [f for f in scan_result.files if f.is_duplicate_candidate]
    if min_size is not None:
        candidates = [f for f in candidates if f.stat.size >= min_size]
    if extensions is not None:
        normalized_exts = {_normalize_extension(e) for e in extensions}
        candidates = [
            f for f in candidates
            if os.path.splitext(f.path)[1].casefold() in normalized_exts
        ]
    return candidates


def _build_summary(
    scan_result: scanner.ScanResult, tier_result: tiers.TierResult
) -> report.ScanSummary:
    reclaimable = sum(g.size * (len(g.paths) - 1) for g in tier_result.duplicate_groups)
    inventory_only = sum(1 for f in scan_result.files if f.exclusion_reason == "inventory-only")
    zero_byte = sum(1 for f in scan_result.files if f.exclusion_reason == "zero-byte")
    hardlink = sum(1 for f in scan_result.files if f.exclusion_reason == "hardlink-of")
    return report.ScanSummary(
        files_scanned=len(scan_result.files),
        tier1_unique_count=tier_result.tier1_unique_count,
        tier2_dropped_count=tier_result.tier2_dropped_count,
        tier3_not_duplicate_count=tier_result.tier3_not_duplicate_count,
        duplicate_group_count=len(tier_result.duplicate_groups),
        reclaimable_bytes=reclaimable,
        skipped_reparse_count=len(scan_result.skipped_reparse),
        skipped_offline_count=len(scan_result.skipped_offline),
        errors_count=len(scan_result.errors) + len(tier_result.errors),
        zero_byte_count=zero_byte,
        inventory_only_count=inventory_only,
        hardlink_count=hardlink,
    )


def _select_keepers(
    scan_result: scanner.ScanResult,
    tier_result: tiers.TierResult,
    keeper_cfg: keeper.KeeperConfig,
) -> List[keeper.KeeperDecision]:
    """One KeeperDecision per tier_result.duplicate_groups entry, same
    order -- report.build_grouped_report() and _build_rows() both rely on
    this 1:1 pairing."""
    mtimes = {f.path: f.stat.mtime_ns for f in scan_result.files}
    return [
        keeper.select_keeper(group.paths, mtimes, keeper_cfg)
        for group in tier_result.duplicate_groups
    ]


def _build_rows(
    scan_result: scanner.ScanResult,
    tier_result: tiers.TierResult,
    decisions: List[keeper.KeeperDecision],
) -> List[report.ReportRow]:
    rows: List[report.ReportRow] = []
    path_to_group = {}
    path_to_role_reason = {}
    for idx, (group, decision) in enumerate(zip(tier_result.duplicate_groups, decisions), start=1):
        for p in group.paths:
            path_to_group[p] = (str(idx), group.full_hash)
        path_to_role_reason[decision.keeper] = (report.ROLE_KEEPER, "")
        for discard in decision.discarded:
            path_to_role_reason[discard.path] = (report.ROLE_DISCARDED, discard.reason)

    for f in scan_result.files:
        if f.path in path_to_group:
            group_id, full_hash = path_to_group[f.path]
            kind = report.KIND_DUP
        else:
            group_id, full_hash = "", ""
            if f.exclusion_reason == "offline":
                kind = report.KIND_OFFLINE
            elif f.exclusion_reason == "zero-byte":
                kind = report.KIND_ZERO_BYTE
            elif f.exclusion_reason == "hardlink-of":
                kind = report.KIND_HARDLINK_OF
            else:
                kind = report.KIND_UNIQUE
        role, reason = path_to_role_reason.get(f.path, ("", ""))
        rows.append(report.ReportRow(
            group_id=group_id, path=f.path, size=f.stat.size, mtime=f.stat.mtime_ns,
            partial_hash="", full_hash=full_hash, kind=kind, role=role, reason=reason,
        ))

    for err in list(scan_result.errors) + list(tier_result.errors):
        rows.append(report.ReportRow(
            group_id="", path=err.path, size=0, mtime=0, partial_hash="",
            full_hash="", kind=report.KIND_ERROR,
        ))
    return rows


def _format_apply_summary(result: applier.ApplyResult) -> str:
    return "\n".join([
        "=== Applied (--apply) ===",
        f"Moved: {result.moved}, failed: {result.failed}",
        f"Batch: {report.display_path(result.batch_dir)}",
        f"Manifest: {report.display_path(result.manifest_path)}",
    ])


def _format_restore_summary(result: applier.RestoreResult) -> str:
    lines = [
        "=== Restored (--restore) ===",
        f"Restored: {result.restored}, refused: {result.refused}",
    ]
    for entry in result.entries:
        if entry.status == "refused":
            lines.append(f"  REFUSED: {report.display_path(entry.src_abs)} -- {entry.detail}")
    return "\n".join(lines)


def _format_verify_summary(result: applier.VerifyReport) -> str:
    lines = [
        "=== Manifest verification (--verify-manifest) ===",
        f"'planned' entries without a completed move: {len(result.planned_without_move)}",
    ]
    lines.extend(f"  - {report.display_path(p)}" for p in result.planned_without_move)
    lines.append(f"Orphans in the trash (no 'done' entry): {len(result.orphans)}")
    lines.extend(f"  - {report.display_path(p)}" for p in result.orphans)
    lines.append(f"Size/hash mismatches: {len(result.mismatches)}")
    lines.extend(f"  - {m}" for m in result.mismatches)
    return "\n".join(lines)


_SINCE_PATTERN = re.compile(r"(\d+)d")


def _parse_since(text: str) -> timedelta:
    match = _SINCE_PATTERN.fullmatch(text.strip())
    if not match:
        raise ValueError(f"invalid --since format: {text!r} (expected e.g. '7d')")
    return timedelta(days=int(match.group(1)))


def _resolve_roots(args: argparse.Namespace) -> List[str]:
    # Resolve any relative root to an absolute path before it reaches the
    # FileSystem seam: platform_win.to_extended() requires an already-
    # absolute path (the `\\?\` prefix disables Win32 path normalization),
    # so a relative root (running from inside the archive and passing a
    # bare subfolder name, or ".") would otherwise resolve to nothing on
    # disk. Paths already absolute are passed through untouched -- calling
    # os.path.abspath() unconditionally would also re-normalize separators
    # on an already-absolute path, which is unnecessary here and would
    # break FakeFileSystem-based tests that construct forward-slash paths.
    return [root if os.path.isabs(root) else os.path.abspath(root) for root in args.roots]


def _scan_and_select(
    args: argparse.Namespace, fs: FileSystem
) -> Tuple[scanner.ScanResult, tiers.TierResult, List[keeper.KeeperDecision], keeper.KeeperConfig]:
    """Shared scan -> tier duplicate detection -> keeper selection pipeline,
    used by both the normal report path and --review in run(). Raises
    scanner.RemoveDirRefusedError exactly like scan() itself -- callers
    decide how to report it."""
    resolved_roots = _resolve_roots(args)

    scan_result = scanner.scan(fs, resolved_roots, extra_excluded_dir_names=args.exclude)
    candidates = _filter_candidates(scan_result, args.min_size, args.ext)

    cache = Cache(args.db)
    active_cache = _BypassReadCache(cache) if args.rescan else cache
    confirm = build_confirm(args.yes)
    try:
        tier_result = tiers.find_duplicates(fs, candidates, active_cache, confirm=confirm)
    finally:
        # Must run even if find_duplicates raises: Cache batches commits
        # every CACHE_COMMIT_BATCH_SIZE writes, so an uncaught exception
        # partway through a batch would otherwise discard up to a batch's
        # worth of already-computed hashes (some of multi-GB videos) and
        # silently undermine the resumability guarantee for that run.
        cache.close()

    keeper_cfg = keeper.load_keeper_config(args.keeper_config)
    decisions = _select_keepers(scan_result, tier_result, keeper_cfg)
    return scan_result, tier_result, decisions, keeper_cfg


def _resolve_archive_root(args: argparse.Namespace) -> str:
    # !REMOVE always lives under --archive-root (default
    # DEFAULT_ARCHIVE_ROOT), never wherever --roots happened to narrow the
    # scan to -- otherwise a scoped run like "--roots '!NEW\Travel'"
    # would nest !REMOVE under #Travel instead of at the archive root,
    # scattering trash folders across the tree as narrow-root apply runs
    # accumulate. Deliberately independent of resolved_roots. Shared by
    # --apply and --review's in-browser apply, which move files the same way.
    return args.archive_root if os.path.isabs(args.archive_root) else os.path.abspath(args.archive_root)
