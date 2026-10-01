"""manifest.csv read/write -- the sole vehicle of reversibility for
--apply/--restore/--verify-manifest.

Schema, UTF-8 BOM, `config.CSV_DELIMITER` (';'):
    op, src_abs, dst_rel, size, mtime, hash, group_id, keeper_abs, reason,
    ts, tool_version, status
plus one extra column, `error`, carrying the system message for a row
whose move failed.

Every state transition (planned -> done, planned -> failed) appends a
brand new full row for the same `src_abs` rather than rewriting the first
one. Readers (`read_rows()` + `resolve_final_rows()`) take the *last* row
in file order per `src_abs` as that entry's authoritative final state.

Format: docs/technical/components/manifest-and-db.md
CSV guard: docs/technical/security.md
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import Dict, Iterable, List

from tidy import config
from tidy.duplicates.report import _csv_safe, _FORMULA_TRIGGER_CHARS


def _unescape_for_fs(value: str) -> str:
    """Reverse _csv_safe()'s leading-quote guard for a field that is about
    to drive a real fs.move()/fs.exists() call. Every path this module
    returns is unescaped and ready to hand to `fs.move()`; `keeper_abs` is
    informational only and is deliberately NOT unescaped on read.
    See: docs/technical/security.md"""
    if len(value) >= 2 and value[0] == "'" and value[1] in _FORMULA_TRIGGER_CHARS:
        return value[1:]
    return value

MANIFEST_COLUMNS = (
    "op", "src_abs", "dst_rel", "size", "mtime", "hash", "group_id",
    "keeper_abs", "reason", "ts", "tool_version", "status", "error",
)

STATUS_PLANNED = "planned"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

OP_REMOVE = "remove"
OP_MOVE = "move"  # in-archive sorting, written by applier.apply_moves().
OP_COMPRESS = "compress"  # video compression, written by applier.apply_compressions().

# `dst_rel` is read differently per `op`, and every consumer must branch on
# it (applier.resolve_dst() is the single place that does):
#
#   op=remove   -> `dst_rel` is relative to the batch directory holding this
#                  manifest, i.e. "!REMOVE\<ts>\".
#   op=move     -> `dst_rel` holds an ABSOLUTE destination path.
#   op=compress -> `dst_rel` holds an ABSOLUTE destination path (the
#                  <original>_compressed.mp4 file).
#
# Format: docs/technical/components/manifest-and-db.md


@dataclass(frozen=True)
class ManifestRow:
    op: str
    src_abs: str
    dst_rel: str
    size: int
    mtime: int
    hash: str
    group_id: str
    keeper_abs: str
    reason: str
    ts: str
    tool_version: str
    status: str
    error: str = ""


def _ensure_header(path: str) -> None:
    if os.path.exists(path):
        return
    # Defensive: in production, apply_batch() already created this
    # directory for real via fs.makedirs(batch_dir) before ever writing
    # here, since the manifest lives inside its own batch dir. Guarding
    # here too costs nothing and makes manifest.py correct standalone
    # rather than silently depending on that call ordering.
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=config.CSV_DELIMITER)
        writer.writerow(MANIFEST_COLUMNS)
        f.flush()
        os.fsync(f.fileno())


def _append_row(path: str, row: ManifestRow) -> None:
    _ensure_header(path)
    # Append in plain "utf-8" (no BOM): the BOM belongs once, at the very
    # start of the file, written by _ensure_header() above.
    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=config.CSV_DELIMITER)
        writer.writerow([
            row.op,
            _csv_safe(row.src_abs),
            _csv_safe(row.dst_rel),
            row.size,
            row.mtime,
            row.hash,
            row.group_id,
            _csv_safe(row.keeper_abs),
            row.reason,
            row.ts,
            row.tool_version,
            row.status,
            row.error,
        ])
        f.flush()
        os.fsync(f.fileno())


def append_planned(path: str, row: ManifestRow) -> None:
    """Append a `status=planned` row, flushed and fsynced before returning.
    Must be called (and durably on disk) before the corresponding
    fs.move() is ever attempted -- see module docstring."""
    _append_row(path, row)


def append_confirmation(path: str, row: ManifestRow) -> None:
    """Append a `status=done`/`status=failed` row for the same `src_abs`
    as a prior append_planned() call, flushed and fsynced before
    returning. Called only after the move attempt has resolved."""
    _append_row(path, row)


def _row_from_fields(line: List[str]) -> ManifestRow:
    return ManifestRow(
        op=line[0],
        src_abs=_unescape_for_fs(line[1]),
        dst_rel=_unescape_for_fs(line[2]),
        size=int(line[3]),
        mtime=int(line[4]),
        hash=line[5],
        group_id=line[6],
        keeper_abs=line[7],
        reason=line[8],
        ts=line[9],
        tool_version=line[10],
        status=line[11],
        error=line[12] if len(line) > 12 else "",
    )


def read_rows(path: str) -> List[ManifestRow]:
    """Read every row from `path`, in file order. Raises OSError (via the
    normal open() failure) if `path` doesn't exist -- callers
    (--restore/--verify-manifest) are expected to be pointed at a manifest
    that was actually produced by a prior --apply run.

    A crash landing mid-write of a row's own bytes can only ever tear the
    LAST physical line in the file -- every earlier row was already
    flushed+fsynced as a complete unit before the next append began. A
    malformed last line is treated as an incomplete write-in-progress and
    silently dropped; a malformed row anywhere else in the file is real
    corruption and still raises."""
    rows: List[ManifestRow] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=config.CSV_DELIMITER)
        header = next(reader, None)
        if header is None:
            return rows
        lines = [line for line in reader if line]
    for idx, line in enumerate(lines):
        try:
            rows.append(_row_from_fields(line))
        except (IndexError, ValueError):
            if idx == len(lines) - 1:
                continue
            raise
    return rows


def resolve_final_rows(rows: Iterable[ManifestRow]) -> Dict[str, ManifestRow]:
    """The last row in file order for each `src_abs` is that entry's
    authoritative final state (see module docstring)."""
    final: Dict[str, ManifestRow] = {}
    for row in rows:
        final[row.src_abs] = row
    return final


def read_final_rows(path: str) -> Dict[str, ManifestRow]:
    """Convenience: read_rows() + resolve_final_rows() in one call."""
    return resolve_final_rows(read_rows(path))
