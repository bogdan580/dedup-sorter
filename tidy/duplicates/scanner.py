"""Recursive filesystem walk over the `FileSystem` seam.

Pure and OS-agnostic: everything here talks only to `fsmodel.FileSystem`
(`scandir`/`stat`), never to `os`/`os.path` directly, so it can be fully
unit-tested against `FakeFileSystem` and behaves identically against the
real archive via `platform_win.WindowsFileSystem`.
"""
# What is excluded and why: docs/business/rules/duplicates.md
# Seam contract: docs/technical/architecture.md
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from tidy import config
from tidy.fs.fsmodel import FileSystem, StatInfo


class RemoveDirRefusedError(ValueError):
    """Raised when a scan root is (or is inside) the `!REMOVE` trash dir."""


@dataclass(frozen=True)
class ScannedFile:
    path: str
    stat: StatInfo
    is_duplicate_candidate: bool
    exclusion_reason: Optional[str] = None  # zero-byte | inventory-only | offline | hardlink-of
    hardlink_of: Optional[str] = None


@dataclass(frozen=True)
class ScanError:
    path: str
    message: str


@dataclass
class ScanResult:
    files: List[ScannedFile] = field(default_factory=list)
    skipped_reparse: List[str] = field(default_factory=list)
    skipped_offline: List[str] = field(default_factory=list)
    skipped_kosz: List[str] = field(default_factory=list)
    pruned_dirs: List[str] = field(default_factory=list)
    errors: List[ScanError] = field(default_factory=list)
    hardlink_groups: Dict[Tuple[int, int], List[str]] = field(default_factory=dict)


def _is_remove_path(path: str) -> bool:
    """True if any path segment casefold-matches the `!REMOVE` dir name."""
    normalized = path.replace("\\", "/")
    segments = [seg for seg in normalized.split("/") if seg]
    remove_cf = config.REMOVE_DIR_NAME.casefold()
    return any(seg.casefold() == remove_cf for seg in segments)


def _normalize_for_compare(path: str) -> str:
    """Casefold, slash-normalized form of `path`, used only to compare two
    paths for equality/containment -- never passed to a FileSystem call."""
    return path.replace("\\", "/").rstrip("/").casefold()


def _is_drive_root(path: str) -> bool:
    """True for a Windows drive root: "D:" (2 chars) or "D:\\"/"D:/" (3
    chars). Distinguishing these matters: Windows treats a bare "D:" as
    drive-relative -- the *current directory* on that drive, not an
    absolute path (`os.path.isabs("D:")` is False) -- while "D:\\" is a
    genuine absolute path that `platform_win.to_extended()` accepts. See
    `_parent_dir()`."""
    return len(path) in (2, 3) and path[1:2] == ":" and (
        len(path) == 2 or path[2] in ("/", "\\")
    )


def _parent_dir(path: str) -> Optional[str]:
    """Pure string parent-directory computation (no os.path), so this stays
    OS-agnostic and works identically for forward-slash fake paths and real
    backslash Windows paths. Returns None once at a drive root / no more
    separators are left.

    One level above the drive root, the naive slice-at-separator would
    truncate e.g. "D:\\Foo" down to the bare, non-absolute "D:" instead of
    the real, scannable drive root "D:\\" -- see `_is_drive_root()`. That
    bare "D:" reaching `platform_win.to_extended()` raises a `ValueError`,
    which is not an `OSError`, so it would abort the whole scan instead of
    being caught as a per-path error."""
    if _is_drive_root(path):
        return None
    idx = max(path.rfind("/"), path.rfind("\\"))
    if idx <= 0:
        return None
    parent = path[:idx]
    if _is_drive_root(parent):
        parent = path[:idx + 1]
    return parent


def _ancestor_dirs(path: str) -> Iterable[str]:
    current = _parent_dir(path)
    while current is not None:
        yield current
        current = _parent_dir(current)


def _ancestor_kosz_sentinel(fs: FileSystem, path: str) -> Optional[str]:
    """Return the first ancestor directory of `path` that itself contains
    the `.dedup-kosz` sentinel, or None. This catches a root pointed at a
    subdirectory *inside* a renamed/relocated `!REMOVE` trash folder (e.g.
    `--roots "...\\OldTrash_renamed_from_REMOVE\\2026-01-01_00-00"`) -- the
    per-directory sentinel check in `_walk` only stops recursion when
    walking *into* the trash dir from above; it can't catch a root that
    starts already inside it, several levels past the sentinel."""
    kosz_cf = config.KOSZ_SENTINEL_FILENAME.casefold()
    for ancestor in _ancestor_dirs(path):
        try:
            entries = list(fs.scandir(ancestor))
        except (OSError, PermissionError):
            continue
        if any(e.name.casefold() == kosz_cf for e in entries):
            return ancestor
    return None


def _dedupe_roots(roots: List[str]) -> List[str]:
    """Canonicalize and de-duplicate requested roots: if one root is equal
    to, or nested inside, another requested root, drop the nested one.
    Without this, overlapping --roots (e.g. the archive root plus one of
    its own subfolders) walk the overlap twice, producing two ScannedFile
    entries for the same physical file and a phantom "duplicate group"
    containing that file paired with itself. Order-independent: sorts the
    broadest (shortest normalized) root first so nesting is always caught
    regardless of the order --roots was given in."""
    with_norm = [(root, _normalize_for_compare(root)) for root in roots]
    with_norm.sort(key=lambda pair: len(pair[1]))
    kept: List[Tuple[str, str]] = []
    for original, normalized in with_norm:
        if any(
            normalized == kept_norm or normalized.startswith(kept_norm + "/")
            for _, kept_norm in kept
        ):
            continue
        kept.append((original, normalized))
    return [original for original, _ in kept]


def _extension_casefold(name: str) -> str:
    idx = name.rfind(".")
    if idx <= 0:
        return ""
    return name[idx:].casefold()


def _classify_file(
    entry_path: str,
    name: str,
    stat: StatInfo,
    hardlink_seen: Dict[Tuple[int, int], str],
    result: ScanResult,
) -> ScannedFile:
    name_cf = name.casefold()
    ext_cf = _extension_casefold(name)

    if stat.is_offline:
        result.skipped_offline.append(entry_path)
        return ScannedFile(
            path=entry_path, stat=stat, is_duplicate_candidate=False,
            exclusion_reason="offline",
        )

    if stat.nlink > 1 and stat.volume_id is not None and stat.file_index is not None:
        key = (stat.volume_id, stat.file_index)
        representative = hardlink_seen.get(key)
        if representative is None:
            hardlink_seen[key] = entry_path
            result.hardlink_groups.setdefault(key, []).append(entry_path)
        else:
            result.hardlink_groups.setdefault(key, []).append(entry_path)
            return ScannedFile(
                path=entry_path, stat=stat, is_duplicate_candidate=False,
                exclusion_reason="hardlink-of", hardlink_of=representative,
            )

    if stat.size == 0:
        return ScannedFile(
            path=entry_path, stat=stat, is_duplicate_candidate=False,
            exclusion_reason="zero-byte",
        )

    if (
        name_cf in config.EXCLUDED_INVENTORY_ONLY_FILENAMES_CASEFOLD
        or ext_cf in config.EXCLUDED_INVENTORY_ONLY_EXTENSIONS_CASEFOLD
    ):
        return ScannedFile(
            path=entry_path, stat=stat, is_duplicate_candidate=False,
            exclusion_reason="inventory-only",
        )

    return ScannedFile(path=entry_path, stat=stat, is_duplicate_candidate=True)


def _walk(
    fs: FileSystem,
    dir_path: str,
    result: ScanResult,
    hardlink_seen: Dict[Tuple[int, int], str],
    excluded_dir_names_casefold: frozenset,
) -> None:
    try:
        entries = list(fs.scandir(dir_path))
    except (OSError, PermissionError) as exc:
        result.errors.append(ScanError(path=dir_path, message=str(exc)))
        return

    entry_names_cf = {entry.name.casefold() for entry in entries}
    if config.KOSZ_SENTINEL_FILENAME.casefold() in entry_names_cf:
        result.skipped_kosz.append(dir_path)
        return

    for entry in entries:
        if entry.stat is None:
# See: docs/technical/components/duplicates-mechanism.md
            result.errors.append(
                ScanError(path=entry.path, message=entry.stat_error or "stat failed")
            )
            continue

        name_cf = entry.name.casefold()

        if entry.stat.is_dir:
            if name_cf in excluded_dir_names_casefold:
                result.pruned_dirs.append(entry.path)
                continue
            if entry.stat.is_reparse_point:
                result.skipped_reparse.append(entry.path)
                continue
            _walk(fs, entry.path, result, hardlink_seen, excluded_dir_names_casefold)
        else:
            result.files.append(
                _classify_file(entry.path, entry.name, entry.stat, hardlink_seen, result)
            )


def scan(
    fs: FileSystem,
    roots: Iterable[str],
    extra_excluded_dir_names: Optional[Iterable[str]] = None,
) -> ScanResult:
    """Scan every root, returning a ScanResult. Never raises for per-path
    errors (permission denied, missing root, etc.) -- those are captured in
    `ScanResult.errors` and the scan continues elsewhere. Only refuses
    outright (raises `RemoveDirRefusedError`) when a root is `!REMOVE`
    itself.

    `extra_excluded_dir_names` (--exclude) is additive only: it is
    casefolded and unioned with
    `config.EXCLUDED_DIR_NAMES_CASEFOLD`, never substituted for it -- there
    is no way to pass anything here that weakens the hard-coded `!REMOVE`/
    `.dedup`/`VIDEO_TS` pruning."""
    result = ScanResult()
    hardlink_seen: Dict[Tuple[int, int], str] = {}
    excluded_dir_names_casefold = config.EXCLUDED_DIR_NAMES_CASEFOLD | frozenset(
        name.casefold() for name in (extra_excluded_dir_names or ())
    )

    for root in _dedupe_roots(list(roots)):
        if _is_remove_path(root):
            raise RemoveDirRefusedError(
                f"Refusing to scan {root!r}: {config.REMOVE_DIR_NAME} is hard-excluded "
                "and cannot be targeted, even explicitly."
            )

        kosz_ancestor = _ancestor_kosz_sentinel(fs, root)
        if kosz_ancestor is not None:
            result.skipped_kosz.append(root)
            continue

        try:
            root_stat = fs.stat(root)
        except (OSError, PermissionError) as exc:
            result.errors.append(ScanError(path=root, message=str(exc)))
            continue

        if root_stat.is_reparse_point:
            result.skipped_reparse.append(root)
            continue

        _walk(fs, root, result, hardlink_seen, excluded_dir_names_casefold)

# See: docs/technical/components/duplicates-mechanism.md
    seen_paths: set = set()
    deduped_files: List[ScannedFile] = []
    for f in result.files:
        key = _normalize_for_compare(f.path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        deduped_files.append(f)
    result.files = deduped_files

    return result
