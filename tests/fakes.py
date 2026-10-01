"""FakeFileSystem: an in-memory stand-in for fsmodel.FileSystem, used by
every test in this suite that needs to walk/stat/read "files". Nothing here
ever touches a real disk -- trees are built entirely from dicts of
StatInfo/DirEntryInfo, per the task's safety requirement that tests never
scan the real archive.
"""
from __future__ import annotations

import io
from typing import Dict, List, Optional

from tidy.fs.fsmodel import DirEntryInfo, StatInfo


def _parent_of(path: str) -> Optional[str]:
    idx = path.rfind("/")
    if idx <= 0:
        return None
    return path[:idx]


def make_stat(
    *,
    size: int = 0,
    mtime_ns: int = 0,
    volume_id: Optional[int] = 1,
    file_index: Optional[int] = None,
    nlink: int = 1,
    attributes: int = 0,
    is_dir: bool = False,
    is_reparse_point: bool = False,
    reparse_tag: Optional[int] = None,
) -> StatInfo:
    return StatInfo(
        size=size,
        mtime_ns=mtime_ns,
        volume_id=volume_id,
        file_index=file_index,
        nlink=nlink,
        attributes=attributes,
        is_dir=is_dir,
        is_reparse_point=is_reparse_point,
        reparse_tag=reparse_tag,
    )


class FakeFileSystem:
    """Build with add_dir()/add_file(), then pass as the `fs` argument to
    scanner.scan() / tiers functions exactly like a real FileSystem.

    Paths are plain strings joined with "/" (no OS path semantics needed --
    real Windows path quirks like `\\\\?\\` long-path prefixing are covered
    separately, and in isolation, by tests for platform_win.to_extended()).
    """

    def __init__(self) -> None:
        self._stats: Dict[str, StatInfo] = {}
        self._children: Dict[str, List[str]] = {}
        self._contents: Dict[str, bytes] = {}
        self._stat_errors: Dict[str, Exception] = {}
        self._open_errors: Dict[str, Exception] = {}
        self._entry_stat_errors: Dict[str, Exception] = {}
        self._move_errors: Dict[str, Exception] = {}
        self._entry_path_overrides: Dict[str, str] = {}
        self.open_read_calls: List[str] = []
        self.stat_calls: List[str] = []
        self.move_calls: List[tuple] = []

    # -- fixture building ------------------------------------------------
    def add_dir(self, path: str, *, entry_path: Optional[str] = None, **stat_overrides) -> "FakeFileSystem":
        stat_overrides.setdefault("is_dir", True)
        self._stats[path] = make_stat(**stat_overrides)
        self._children.setdefault(path, [])
        parent = _parent_of(path)
        if parent is not None:
            self._children.setdefault(parent, []).append(path)
        if entry_path is not None:
            self.set_entry_path_override(path, entry_path)
        return self

    def set_entry_path_override(self, path: str, entry_path: str) -> "FakeFileSystem":
        """Make scandir() yield `entry_path` as this child's DirEntryInfo.path
        instead of its own internal (always "/"-joined) key -- reproducing
        what platform_win.WindowsFileSystem.scandir() really does: its
        entry.path carries the `\\\\?\\` long-path prefix (real backslashes),
        which is a DIFFERENT string from the plain path used to reach it.
        Regression for the "Cofnij"/batches.py bug:
        no path in FakeFileSystem ever carried that prefix before, so no
        test using it could reproduce the one condition that broke
        list_batches() in production -- batches._join()/to_extended()
        mishandling an already-prefixed base. `path` (the internal key)
        still drives stat/exists/scandir-parent-child lookups unchanged;
        only the `.path` attribute yielded to callers differs."""
        self._entry_path_overrides[path] = entry_path
        return self

    def add_file(self, path: str, content: bytes = b"", **stat_overrides) -> "FakeFileSystem":
        stat_overrides.setdefault("size", len(content))
        if stat_overrides.get("file_index") is None:
            stat_overrides["file_index"] = abs(hash(path)) % (2 ** 31)
        self._stats[path] = make_stat(is_dir=False, **stat_overrides)
        self._contents[path] = content
        parent = _parent_of(path)
        if parent is not None:
            self._children.setdefault(parent, []).append(path)
        return self

    def set_stat_error(self, path: str, exc: Exception) -> "FakeFileSystem":
        self._stat_errors[path] = exc
        return self

    def set_open_error(self, path: str, exc: Exception) -> "FakeFileSystem":
        self._open_errors[path] = exc
        return self

    def set_entry_stat_error(self, path: str, exc: Exception) -> "FakeFileSystem":
        """Simulate a per-entry stat() failure surfacing through scandir()
        (locked file, AV lock, cloud-sync race) -- the entry is yielded
        with stat=None/stat_error set, exactly as platform_win.py's real
        scandir() does, instead of scandir() itself raising."""
        self._entry_stat_errors[path] = exc
        return self

    def set_move_error(self, src: str, exc: Exception) -> "FakeFileSystem":
        """Inject a failure for the next move() call whose `src` matches.
        Use an (OSError, PermissionError)-derived `exc` to simulate an
        ordinary per-file failure (locked file, permission denied) that
        applier.apply_batch() is expected to catch and record as
        status=failed, continuing the batch. Use anything else (a bespoke
        BaseException subclass, KeyboardInterrupt, ...) to simulate a hard
        process kill mid-batch: apply_batch()'s narrow
        `except (OSError, PermissionError)` must NOT catch it, so it
        propagates out of apply_batch() uncaught."""
        self._move_errors[src] = exc
        return self

    def _remove_from_parent_children(self, path: str) -> None:
        parent = _parent_of(path)
        if parent is not None and parent in self._children:
            self._children[parent] = [c for c in self._children[parent] if c != path]

    def _add_to_parent_children(self, path: str) -> None:
        parent = _parent_of(path)
        if parent is not None:
            self._children.setdefault(parent, []).append(path)

    # -- FileSystem protocol ----------------------------------------------
    def scandir(self, path: str):
        for child_path in self._children.get(path, []):
            name = child_path.rsplit("/", 1)[-1]
            entry_path = self._entry_path_overrides.get(child_path, child_path)
            if child_path in self._entry_stat_errors:
                yield DirEntryInfo(path=entry_path, name=name, stat_error=str(self._entry_stat_errors[child_path]))
                continue
            yield DirEntryInfo(path=entry_path, name=name, stat=self._stats[child_path])

    def stat(self, path: str) -> StatInfo:
        self.stat_calls.append(path)
        if path in self._stat_errors:
            raise self._stat_errors[path]
        return self._stats[path]

    def open_read(self, path: str):
        self.open_read_calls.append(path)
        if path in self._open_errors:
            raise self._open_errors[path]
        if path not in self._contents:
            raise AssertionError(
                f"open_read() called for a path with no registered content: {path}"
            )
        return io.BytesIO(self._contents[path])

    def move(self, src: str, dst: str) -> None:
        if src in self._move_errors:
            raise self._move_errors[src]
        if dst in self._stats:
            raise FileExistsError(f"move(): destination already exists: {dst}")
        self.move_calls.append((src, dst))
        stat = self._stats.pop(src)
        content = self._contents.pop(src, None)
        self._remove_from_parent_children(src)
        self._stats[dst] = stat
        if content is not None:
            self._contents[dst] = content
        self._add_to_parent_children(dst)

    def exists(self, path: str) -> bool:
        return path in self._stats

    def makedirs(self, path: str) -> None:
        existing = self._stats.get(path)
        if existing is not None and existing.is_dir:
            return
        to_create: List[str] = []
        current: Optional[str] = path
        while current is not None and current not in self._stats:
            to_create.append(current)
            current = _parent_of(current)
        for dir_path in reversed(to_create):
            self.add_dir(dir_path)

    def touch(self, path: str) -> None:
        if path in self._stats:
            return
        self.add_file(path, b"")
