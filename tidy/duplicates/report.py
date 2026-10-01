"""raport.csv writer + console summary formatting.

CSV contract: UTF-8 BOM (Excel-openable), ';'
delimiter, CRLF line endings, RFC-4180 quoting -- all of which
`csv.writer` gives us for free once the file is opened with
`encoding='utf-8-sig', newline=''`.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Sequence, Tuple

from tidy import config

if TYPE_CHECKING:
# See: docs/technical/components/duplicates-mechanism.md
    from tidy.duplicates.keeper import KeeperDecision
    from tidy.duplicates.tiers import DuplicateGroup

CSV_COLUMNS = (
    "group_id", "path", "size", "mtime", "partial_hash", "full_hash", "kind",
    "role", "reason",
)

# Valid values for ReportRow.role -- who a row is, within its duplicate
# group, per the keeper decision. Blank for non-duplicate rows.
ROLE_KEEPER = "keeper"
ROLE_DISCARDED = "discarded"

_LONG_PATH_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t")


def display_path(path: str) -> str:
    """Strip the internal `\\\\?\\`/`\\\\?\\UNC\\` long-path prefix for
    human-facing output. The prefix is required internally for every real
    FileSystem syscall to support archive paths over 260 chars, but is
    noise in a report a person is meant to eyeball."""
    if path.startswith(_UNC_PREFIX):
        return "\\\\" + path[len(_UNC_PREFIX):]
    if path.startswith(_LONG_PATH_PREFIX):
        return path[len(_LONG_PATH_PREFIX):]
    return path


def ensure_parent_dir(path: str) -> None:
    """Create the directory an output file is about to be written into.

    The generated artefact directories are not in git and the user may
    delete them, so the first write would otherwise fail with "No such
    file or directory". Shared by every writer that opens a real file
    directly rather than through the FileSystem seam, deliberately,
    because these are tool-internal outputs and never archive content.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _csv_safe(value: str) -> str:
    """Neutralize CSV/formula injection: Excel treats a cell starting with
    =, +, -, @, or a tab as a formula. raport.csv's `path` column is built
    directly from filesystem names this tool doesn't control (a manually
    renamed file/folder in the archive), so a leading quote is added to
    force spreadsheet apps to treat it as literal text.

    See: docs/technical/security.md"""
    if value and value[0] in _FORMULA_TRIGGER_CHARS:
        return "'" + value
    return value

# Valid values for ReportRow.kind.
KIND_DUP = "dup"
KIND_UNIQUE = "unique"
KIND_HARDLINK_OF = "hardlink-of"
KIND_OFFLINE = "offline"
KIND_ZERO_BYTE = "zero-byte"
KIND_ERROR = "error"


@dataclass(frozen=True)
class ReportRow:
    group_id: str
    path: str
    size: int
    mtime: int
    partial_hash: str
    full_hash: str
    kind: str
    role: str = ""
    reason: str = ""


def write_csv(path: str, rows: Iterable[ReportRow]) -> None:
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=config.CSV_DELIMITER)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow(
                [row.group_id, _csv_safe(display_path(row.path)), row.size, row.mtime,
                 row.partial_hash, row.full_hash, row.kind, row.role, row.reason]
            )


@dataclass
class ScanSummary:
    files_scanned: int
    tier1_unique_count: int
    tier2_dropped_count: int
    tier3_not_duplicate_count: int
    duplicate_group_count: int
    reclaimable_bytes: int
    skipped_reparse_count: int
    skipped_offline_count: int
    errors_count: int
    zero_byte_count: int
    inventory_only_count: int
    hardlink_count: int


def _format_gb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 ** 3):.2f} GB"


def format_summary(summary: ScanSummary) -> str:
    lines = [
        "=== Scan summary ===",
        f"Files scanned: {summary.files_scanned}",
        f"Tier 1 (unique size, dropped): {summary.tier1_unique_count}",
        f"Tier 2 (different head/tail, dropped): {summary.tier2_dropped_count}",
        f"Tier 3 (same head, different content): {summary.tier3_not_duplicate_count}",
        f"Duplicate groups: {summary.duplicate_group_count}",
        f"Reclaimable: {_format_gb(summary.reclaimable_bytes)}",
        f"Skipped (reparse point): {summary.skipped_reparse_count}",
        f"Skipped (offline/cloud): {summary.skipped_offline_count}",
        f"Hard links: {summary.hardlink_count}",
        f"Zero-byte files: {summary.zero_byte_count}",
        f"Inventory-only files: {summary.inventory_only_count}",
        f"Errors: {summary.errors_count}",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class GroupReportEntry:
    keeper: str
    discarded: Tuple[str, ...]
    size: int
    reclaimable_bytes: int


@dataclass(frozen=True)
class GroupedReport:
    total_reclaimable_bytes: int
    group_count: int
    top_groups: Tuple[GroupReportEntry, ...]


def build_grouped_report(
    groups: Sequence["DuplicateGroup"],
    decisions: Sequence["KeeperDecision"],
    top_n: int = 20,
) -> GroupedReport:
    """Build the grouped report from 1:1-paired
    `tiers.DuplicateGroup`/`keeper.KeeperDecision` sequences -- one decision
    per group, in the same order, exactly as produced by iterating
    `tier_result.duplicate_groups` and calling `keeper.select_keeper` on
    each. Duck-typed (no runtime import of tiers/keeper) so report.py stays
    a leaf module."""
    entries = [
        GroupReportEntry(
            keeper=decision.keeper,
            discarded=tuple(d.path for d in decision.discarded),
            size=group.size,
            reclaimable_bytes=group.size * len(decision.discarded),
        )
        for group, decision in zip(groups, decisions)
    ]
    total = sum(e.reclaimable_bytes for e in entries)
    top = tuple(sorted(entries, key=lambda e: e.reclaimable_bytes, reverse=True)[:top_n])
    return GroupedReport(
        total_reclaimable_bytes=total, group_count=len(entries), top_groups=top,
    )


def format_grouped_report(grouped: GroupedReport) -> str:
    lines = [
        "=== Grouped report ===",
        f"Duplicate groups: {grouped.group_count}",
        f"Reclaimable (all groups): {_format_gb(grouped.total_reclaimable_bytes)}",
        f"{len(grouped.top_groups)} largest groups by reclaimable space:",
    ]
    for i, entry in enumerate(grouped.top_groups, start=1):
        lines.append(
            f"  {i}. keeper={display_path(entry.keeper)} "
            f"reclaimed={_format_gb(entry.reclaimable_bytes)} "
            f"({len(entry.discarded)} do !REMOVE)"
        )
        for discarded_path in entry.discarded:
            lines.append(f"       - {display_path(discarded_path)}")
    return "\n".join(lines)
