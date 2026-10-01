"""Orchestrates --apply / --restore / --verify-manifest. Besides
platform_win.py itself, this is the only module allowed to call
`fs.move()`.

Two apply paths, deliberately separate rather than one function with a
mode flag (see apply_moves()'s docstring for why):
  - apply_batch() -- `op=remove`, files out to `!REMOVE\\<ts>\\`.
  - apply_moves() -- `op=move`, files relocated *inside* the archive by the
    placement suggestions.
restore_batch() and verify_manifest() read both, branching on `row.op`
through resolve_dst().

Pure and OS-agnostic like scanner.py/keeper.py: everything here works on
plain path strings (never os.path/pathlib), so it is fully testable against
FakeFileSystem's forward-slash test paths as well as real backslash Windows
paths carrying the `\\\\?\\` long-path prefix.
"""
# Manifest format: docs/technical/components/manifest-and-db.md
# Guarantees: docs/business/guarantees.md
from __future__ import annotations

from tidy.moves.applier._paths import ensure_archive_dirs, reserve_batch_dir
from tidy.moves.applier.compress import (
    CompressPlan,
    CompressRemoval,
    apply_compress_removals,
    apply_compressions,
)
from tidy.moves.applier.move import MovePlan, apply_moves
from tidy.moves.applier.remove import ApplyEntry, ApplyResult, apply_batch
from tidy.moves.applier.restore import RestoreEntry, RestoreResult, resolve_dst, restore_batch, unsafe_row_reason
from tidy.moves.applier.verify import VerifyReport, verify_manifest
