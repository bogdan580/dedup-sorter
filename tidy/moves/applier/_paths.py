"""Pure path helpers shared by the applier modules."""
from __future__ import annotations

import datetime
from typing import List, Optional, Set, Tuple

from tidy import config
from tidy.fs.fsmodel import FileSystem
from tidy.duplicates.report import display_path


def _default_clock() -> datetime.datetime:
    return datetime.datetime.now()


# --- pure string path helpers (mirror scanner.py / keeper.py style) --------

def _parent_dir(path: str) -> Optional[str]:
    idx = max(path.rfind("/"), path.rfind("\\"))
    if idx <= 0:
        return None
    return path[:idx]


def _basename(path: str) -> str:
    idx = max(path.rfind("/"), path.rfind("\\"))
    return path[idx + 1:] if idx >= 0 else path


def _join(a: str, b: str) -> str:
    a = a.rstrip("/\\")
    b = b.lstrip("/\\")
    return f"{a}/{b}"


def _norm_key(path: str) -> str:
    """Casefold, slash-normalized form used only to compare two paths --
    never passed to a FileSystem call (mirrors scanner._normalize_for_compare)."""
    return path.replace("\\", "/").rstrip("/").casefold()


def _relative_to_root(path: str, root: str) -> str:
    """`path` relative to `root`, preserving directory structure, working
    with both forward and back slashes and stripping the internal
    `\\\\?\\`/`\\\\?\\UNC\\` long-path prefix from both sides before
    comparing (real archive paths carry it; a plain `archive_root` string
    normally doesn't). Falls back to the basename when `path` is not under
    `root`, keeping the function total rather than raising.
    """
    norm_path = display_path(path).replace("\\", "/")
    norm_root = display_path(root).replace("\\", "/").rstrip("/")
    if _norm_key(norm_path) == _norm_key(norm_root):
        return _basename(norm_path)
    prefix = norm_root + "/"
    if _norm_key(norm_path).startswith(_norm_key(prefix)):
        return norm_path[len(prefix):]
    return _basename(norm_path)


def _split_stem_ext(path: str) -> Tuple[str, str, str]:
    """Split `path` into (dir_part_including_separator, stem, extension).
    A leading dot is part of the stem, not an extension (`.gitignore`),
    matching the `dot_idx > 0` test this was factored out of."""
    idx = max(path.rfind("/"), path.rfind("\\"))
    dir_part = path[:idx + 1] if idx >= 0 else ""
    name_part = path[idx + 1:]
    dot_idx = name_part.rfind(".")
    if dot_idx > 0:
        return dir_part, name_part[:dot_idx], name_part[dot_idx:]
    return dir_part, name_part, ""


def _make_unique_rel(dst_rel: str, claimed: Set[str]) -> str:
    """Append __dup2, __dup3, ... to the file stem until `dst_rel` is
    unique among `claimed` (case-insensitively, matching Windows).
    `claimed` is mutated to include whichever name is returned."""
    key = _norm_key(dst_rel)
    if key not in claimed:
        claimed.add(key)
        return dst_rel

    dir_part, stem, ext = _split_stem_ext(dst_rel)
    n = 2
    while True:
        candidate = f"{dir_part}{stem}__dup{n}{ext}"
        candidate_key = _norm_key(candidate)
        if candidate_key not in claimed:
            claimed.add(candidate_key)
            return candidate
        n += 1


def _is_inside(root: str, path: str) -> bool:
    """True when `path` is `root` itself or lies under it. Compares in
    _norm_key() space (casefolded, slash-normalized, `\\\\?\\` stripped via
    display_path) and never touches the filesystem -- a pure containment
    test on strings, used to refuse an op=move destination that would leave
    the archive."""
    root_key = _norm_key(display_path(root))
    path_key = _norm_key(display_path(path))
    return path_key == root_key or path_key.startswith(root_key + "/")


def _make_unique_abs(fs: FileSystem, dst_abs: str, claimed: Set[str]) -> str:
    """apply_batch()'s __dup2/__dup3 suffixing, for an absolute destination
    that has to dodge two kinds of collision at once: names already
    `claimed` earlier in this same batch, AND files that already exist on
    disk at the destination.

    The on-disk check is what makes this different from _make_unique_rel():
    a `!REMOVE\\<ts>\\` batch directory is created fresh and empty, so
    nothing can already be sitting there, but an op=move lands inside the
    live archive where `IMG_1234.jpg` may well exist already. Suffixing
    rather than refusing is right precisely because the dedup pass runs
    *before* placement: a byte-identical copy is already gone by then, so a
    surviving name collision means two genuinely different photos that
    happen to share a camera-assigned filename. Overwriting would destroy
    one of them; refusing would strand the file for a reason the user
    cannot act on."""
    candidate = dst_abs
    n = 2
    while _norm_key(candidate) in claimed or fs.exists(candidate):
        dir_part, stem, ext = _split_stem_ext(dst_abs)
        candidate = f"{dir_part}{stem}__dup{n}{ext}"
        n += 1
    claimed.add(_norm_key(candidate))
    return candidate


def _format_ts(dt: datetime.datetime) -> str:
    return dt.isoformat()


def _make_unique_batch_dir(fs: FileSystem, remove_root: str, batch_name: str) -> str:
    """Return a `remove_root`-relative batch directory path guaranteed not
    to already exist on `fs`, appending `_2`, `_3`, ... to `batch_name` if
    needed.

    `batch_name` is a minute-resolution clock timestamp
    (`config.BATCH_DIR_TIMESTAMP_FORMAT`), so two `apply_batch()` calls
    inside the same clock-minute would otherwise compute the same batch
    directory and silently merge two logically separate batches into one
    manifest. Mirrors _make_unique_rel()'s suffixing pattern above, one
    level up (directory, not filename).
    See: docs/technical/components/review-ui.md"""
    candidate = _join(remove_root, batch_name)
    if not fs.exists(candidate):
        return candidate
    n = 2
    while True:
        candidate = _join(remove_root, f"{batch_name}_{n}")
        if not fs.exists(candidate):
            return candidate
        n += 1


def ensure_archive_dirs(fs: FileSystem, archive_root: str) -> List[str]:
    """Create whichever of `!NEW`, `!MOVES`, `!REMOVE` is missing directly
    under `archive_root` and return the created names. No-op when
    `archive_root` itself does not exist -- a mistyped --archive-root must
    never conjure a directory tree. `!REMOVE` always carries its sentinel."""
    if not fs.exists(archive_root):
        return []
    created: List[str] = []
    for name in (config.NEW_DIR_NAME, config.MOVES_DIR_NAME, config.REMOVE_DIR_NAME):
        path = _join(archive_root, name)
        if not fs.exists(path):
            fs.makedirs(path)
            created.append(name)
    fs.touch(_join(_join(archive_root, config.REMOVE_DIR_NAME), config.KOSZ_SENTINEL_FILENAME))
    return created


def reserve_batch_dir(
    fs: FileSystem, archive_root: str, container_name: str, started_at: datetime.datetime,
) -> str:
    """Create and return a fresh `<container_name>\\<ts>\\` batch directory
    named after `started_at`. A caller landing several apply calls into ONE
    batch (the review UI's compress queue) reserves it once and passes it
    back as `batch_dir=`; every row still gets its own planned/done pair.
    See: docs/technical/components/compression-mechanism.md"""
    container = _join(archive_root, container_name)
    fs.makedirs(container)
    if container_name == config.REMOVE_DIR_NAME:
        fs.touch(_join(container, config.KOSZ_SENTINEL_FILENAME))
    batch_dir = _make_unique_batch_dir(
        fs, container, started_at.strftime(config.BATCH_DIR_TIMESTAMP_FORMAT),
    )
    fs.makedirs(batch_dir)
    return batch_dir


def _safe_mtime(fs: FileSystem, path: str) -> int:
    try:
        return fs.stat(path).mtime_ns
    except (OSError, PermissionError):
        return 0


def _safe_size(fs: FileSystem, path: str) -> int:
    """Size for a manifest row when the caller didn't supply one. Read from
    disk rather than defaulted to 0, because verify_manifest() compares this
    number against the destination later -- a placeholder 0 would turn every
    later --verify-manifest run into a wall of false size mismatches."""
    try:
        return fs.stat(path).size
    except (OSError, PermissionError):
        return 0
