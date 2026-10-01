"""The ONLY OS-specific module. Real Windows filesystem access implementing
the `fsmodel.FileSystem` Protocol.

Unit-test coverage note (read before trusting this file's numbers):
- `to_extended()` and the attribute-decoding helpers (`is_reparse_point`,
  `is_directory_attr`) are pure string/int functions and ARE covered by
  tests/test_platform_win.py.
- `WindowsFileSystem` (scandir/stat/open_read) and the ctypes
  `GetFileInformationByHandle` fallback for `file_index` are
  INTEGRATION-ONLY: they call real `os.scandir`/`os.stat`/`open()` against
  a real Windows filesystem, including junctions, hard links and
  OneDrive-style offline placeholders. None of that can be faked without a
  real disk, so this class is exempt from the 80% coverage bar and must be
  verified by hand against a real directory.
  `_file_index_via_ctypes_fallback()` is a stub that returns None rather
  than opening a HANDLE and calling GetFileInformationByHandle, which
  would require a live file descriptor -- it is the single riskiest,
  least-testable piece of this module.
"""
from __future__ import annotations

import os
from typing import BinaryIO, Iterable, Optional

from tidy.fs.fsmodel import DirEntryInfo, FileSystem, StatInfo

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_DIRECTORY = 0x10

_LONG_PATH_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def to_extended(path: str) -> str:
    """Prefix `path` with `\\\\?\\` (or `\\\\?\\UNC\\` for UNC paths) so
    Windows accepts paths longer than MAX_PATH (260 chars). Idempotent: a
    path that already carries the prefix is returned unchanged.

    The `\\\\?\\` prefix disables Win32 path normalization ENTIRELY -- not
    just MAX_PATH checking, but also `/` -> `\\` separator conversion, which
    the normal Win32 path parser otherwise does for free. With the prefix
    in effect, a forward slash is no longer treated as a separator at all,
    and both `os.rename()`/`os.makedirs()` fail with WinError 123
    ("filename, directory name, or volume label syntax is incorrect") the
    instant a prefixed path contains one. So this function normalizes to
    `\\` unconditionally before prefixing, not just when the caller happens
    to already use `\\`.

    Also requires a fully-qualified, already-absolute path -- a relative or
    drive-relative path silently resolves to nothing on disk once prefixed
    (e.g. `\\\\?\\.` or `\\\\?\\sub`). Callers must resolve to an absolute
    path (`os.path.abspath`) before this function ever sees it; this is
    enforced at the one place real paths enter the system --
    tidy/cli/main.py:run() -- not here, since this module has no access to a
    working directory concept in tests run against FakeFileSystem.
    """
    if path.startswith(_LONG_PATH_PREFIX):
        # Idempotent means "safe to call twice", not "the tail after the
        # prefix is trusted as-is": a caller can hand back an already-
        # prefixed path built with "/". Once prefixed, Win32 will never
        # normalize that slash, so this branch must.
        return _LONG_PATH_PREFIX + path[len(_LONG_PATH_PREFIX):].replace("/", "\\")
    if path.startswith("\\\\") or path.startswith("//"):
        normalized = path.replace("/", "\\")
        return _UNC_PREFIX + normalized[2:]
    normalized = path.replace("/", "\\")
    if not os.path.isabs(normalized):
        raise ValueError(
            f"to_extended() requires an absolute path, got {path!r}. "
            "Resolve with os.path.abspath() before calling."
        )
    return _LONG_PATH_PREFIX + normalized


def is_reparse_point(attributes: int) -> bool:
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def is_directory_attr(attributes: int) -> bool:
    return bool(attributes & FILE_ATTRIBUTE_DIRECTORY)


class WindowsFileSystem:
    """Real FileSystem implementation. See module docstring: integration-
    only, exercised by hand against the real archive, not by the automated
    unit test suite."""

    def scandir(self, path: str) -> Iterable[DirEntryInfo]:
        extended = to_extended(path)
        with os.scandir(extended) as it:
            for entry in it:
                try:
                    st = self._stat_from_os_stat_result(entry.stat(follow_symlinks=False))
                except OSError as exc:
                    # A locked file, AV lock, or cloud-sync race on THIS
                    # entry must not abort enumeration of the rest of the
                    # directory (os.scandir already listed all names in one
                    # syscall; only the lazy per-entry stat() can fail here).
                    yield DirEntryInfo(path=entry.path, name=entry.name, stat_error=str(exc))
                    continue
                yield DirEntryInfo(path=entry.path, name=entry.name, stat=st)

    def stat(self, path: str) -> StatInfo:
        st = os.stat(to_extended(path), follow_symlinks=False)
        return self._stat_from_os_stat_result(st)

    def open_read(self, path: str) -> BinaryIO:
        return open(to_extended(path), "rb")

    def move(self, src: str, dst: str) -> None:
        # Use the rename primitive, not the replace primitive -- the
        # rename primitive raises FileExistsError when
        # `dst` already exists instead of silently overwriting it. Every
        # move this tool ever performs (apply *and* restore) must fail
        # loudly on an unexpected destination collision; the orchestration
        # layer (tidy/moves/applier) also checks existence before calling this, but
        # this is defense in depth, not a substitute for that check.
        os.rename(to_extended(src), to_extended(dst))

    def exists(self, path: str) -> bool:
        return os.path.exists(to_extended(path))

    def makedirs(self, path: str) -> None:
        os.makedirs(to_extended(path), exist_ok=True)

    def touch(self, path: str) -> None:
        if os.path.exists(to_extended(path)):
            return
        open(to_extended(path), "a").close()

    def _stat_from_os_stat_result(self, st: os.stat_result) -> StatInfo:
        attributes = getattr(st, "st_file_attributes", 0)
        file_index = st.st_ino or self._file_index_via_ctypes_fallback()
        return StatInfo(
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            volume_id=st.st_dev or None,
            file_index=file_index or None,
            nlink=st.st_nlink,
            attributes=attributes,
            is_dir=is_directory_attr(attributes),
            is_reparse_point=is_reparse_point(attributes),
            reparse_tag=None,
        )

    def _file_index_via_ctypes_fallback(self) -> Optional[int]:
        # Stub: a full implementation opens a HANDLE via CreateFileW and
        # calls GetFileInformationByHandle to recover nFileIndexHigh/Low
        # when st_ino == 0. Deliberately not implemented here.
        # Consequence: docs/business/instructions/limitations.md
        return None
