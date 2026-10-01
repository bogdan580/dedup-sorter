"""Filesystem abstraction seam.

`FileSystem` is a typing.Protocol implemented for real by
`platform_win.WindowsFileSystem` and, in tests, by an in-memory
`FakeFileSystem`. Every other module (scanner, hashing, tiers, cache)
depends only on this Protocol and the frozen dataclasses below, never on
`os`/`os.path`/`pathlib` directly for filesystem *state* -- keeping the
rest of the codebase OS-agnostic and unit-testable without touching real
disks.
"""
# Contract: docs/technical/architecture.md
from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Iterable, Optional, Protocol

# Windows FILE_ATTRIBUTE_* bits relevant to "this file lives in the cloud,
# reading it would trigger a download" detection.
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_OFFLINE_MASK = (
    _FILE_ATTRIBUTE_OFFLINE
    | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    | _FILE_ATTRIBUTE_RECALL_ON_OPEN
)


@dataclass(frozen=True)
class StatInfo:
    """Snapshot of filesystem metadata for a single path.

    `volume_id`/`file_index` together identify a file uniquely on a given
    Windows volume (used for hard-link detection); either may be None when
    unavailable (e.g. FAT volumes, or a FileSystem implementation that
    doesn't support them).
    """

    size: int
    mtime_ns: int
    volume_id: Optional[int]
    file_index: Optional[int]
    nlink: int
    attributes: int
    is_dir: bool
    is_reparse_point: bool
    reparse_tag: Optional[int]

    @property
    def is_offline(self) -> bool:
        """True if this file is OFFLINE / RECALL_ON_DATA_ACCESS / RECALL_ON_OPEN
        (i.e. a cloud placeholder whose content is not locally resident)."""
        return bool(self.attributes & _OFFLINE_MASK)


@dataclass(frozen=True)
class DirEntryInfo:
    """One entry returned by `FileSystem.scandir`.

    Carries the full `StatInfo` inline (as `os.DirEntry.stat()` does on
    Windows) so callers can make pruning decisions (reparse point, offline,
    hard link) without an extra `stat()` round-trip.

    `stat` is None exactly when `stat_error` is set: a per-entry stat()
    failure (locked file, transient I/O error, AV lock, cloud-sync race)
    must not abort enumeration of the rest of the directory, so
    `FileSystem.scandir()` implementations report such failures inline as
    an entry with no stat rather than raising out of the iterator -- see
    scanner._walk, which records it as a per-file ScanError and moves on.
    """

    path: str
    name: str
    stat: Optional[StatInfo] = None
    stat_error: Optional[str] = None


class FileSystem(Protocol):
    """The test seam. Real implementation: platform_win.WindowsFileSystem.
    Fake implementation for tests: FakeFileSystem (see tests/conftest.py).

    Only `tidy/moves/applier` (plus `platform_win.py` itself) is allowed to call
    `move()`; everything else in the engine remains read-only against this
    seam.
    """

    def scandir(self, path: str) -> Iterable[DirEntryInfo]:
        ...

    def stat(self, path: str) -> StatInfo:
        ...

    def open_read(self, path: str) -> BinaryIO:
        ...

    def move(self, src: str, dst: str) -> None:
        """Move the file at `src` to `dst`. Must raise `FileExistsError`
        if `dst` already exists rather than silently overwriting it.

        See: docs/business/guarantees.md
        """
        ...

    def exists(self, path: str) -> bool:
        ...

    def makedirs(self, path: str) -> None:
        """Create `path` and any missing parent directories. No error if
        `path` already exists (exist_ok semantics)."""
        ...

    def touch(self, path: str) -> None:
        """Create an empty file at `path` if it doesn't already exist;
        no-op otherwise. Used to seed the `.dedup-kosz` sentinel file."""
        ...
