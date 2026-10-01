"""Which existing archive folders already hold photos taken on the same
days as the files being placed.

Scoring is per DAY and by COUNT of overlapping files, never by a folder's
min/max date range. A day histogram simply ignores days that don't match,
instead of being wrecked by unrelated files whose only timestamp is an
mtime, or by an unrelated older trip that happens to share the folder.

Like every other engine here, this module only reads -- through the
FileSystem seam, so it is testable against FakeFileSystem -- and only ever
produces candidates the user picks from (propose, never guess).
"""
# See: docs/business/rules/version-selection.md
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from tidy import config
from tidy.media import timeline
from tidy.store.cache import Cache
from tidy.fs.fsmodel import FileSystem
from tidy.progress import DEFAULT_WORKERS, ProgressCounter
from tidy.duplicates.report import display_path
from tidy.duplicates.scanner import ScannedFile

# See: docs/business/rules/events-and-series.md
DEFAULT_TOLERANCE_DAYS = 1

# Below this, an overlap is more likely coincidence than a shared trip: any
# large folder will collide with a stray day or two of a big enough source.
MIN_OVERLAP_FILES = 2


@dataclass(frozen=True)
class FolderDates:
    """One archive folder's day histogram. `days` counts only files whose
    date could be established at all, so `total` is not necessarily its
    sum -- the difference is how much of the folder this index cannot
    speak for."""
    rel: str
    days: Dict[date, int]
    total: int


@dataclass(frozen=True)
class DateMatch:
    rel: str
    overlap_files: int  # files in this folder falling on (or near) a source day
    overlap_days: int  # source days covered, counting near misses
    exact_days: int  # source days this folder holds photos from EXACTLY
    total: int  # files in the folder overall
    first_day: Optional[date] = None
    last_day: Optional[date] = None

    def describe(self) -> str:
        """The one-line reason shown next to the candidate. Says what was
        counted, not how confident the tool is -- the user makes that call."""
        if self.first_day is None:
            return ""
        if self.first_day == self.last_day:
            span = f"{self.first_day:%Y-%m-%d}"
        else:
            span = f"{self.first_day:%Y-%m-%d}..{self.last_day:%Y-%m-%d}"
        return f"already has {self.overlap_files} files from {span}"


def _relative_dir(path: str, archive_root: str) -> Optional[str]:
    """`path`'s parent directory relative to `archive_root`, or None when
    `path` is not under it. Both sides go through display_path() first:
    scanned paths carry the long-path prefix and `archive_root` does not
    (the same asymmetry that silently emptied the placement screen)."""
    norm_path = display_path(path).replace("\\", "/")
    norm_root = display_path(archive_root).replace("\\", "/").rstrip("/")
    if not norm_path.casefold().startswith(norm_root.casefold() + "/"):
        return None
    segments = norm_path[len(norm_root) + 1:].split("/")
    if len(segments) < 2:
        return ""  # a file sitting directly in the archive root
    return "\\".join(segments[:-1])


def _is_excluded(rel_dir: str) -> bool:
    """Tool directories and the inbox itself. `!NEW` is excluded because it
    is where the unplaced files live: matching them against each other
    would offer the user the folder they are trying to empty."""
    if not rel_dir:
        return True
    first = rel_dir.replace("/", "\\").split("\\")[0].casefold()
    return (
        first in config.EXCLUDED_DIR_NAMES_CASEFOLD
        or first == config.NEW_DIR_NAME.casefold()
    )


def extract_days(
    fs: FileSystem,
    files: Sequence[ScannedFile],
    *,
    cache: Optional[Cache] = None,
    max_workers: int = DEFAULT_WORKERS,
    progress: Optional[ProgressCounter] = None,
) -> Dict[str, date]:
    """path -> the day each file was taken, EXIF first (timeline.py's own
    chain). Cached per file in the SQLite cache when one is given, keyed on
    (size, mtime_ns) exactly like the hash cache: decoding EXIF for the
    whole archive is minutes of work the first time and nothing after.

    Cache reads and writes happen on THIS thread, never inside the pool --
    sqlite3 connections are not shared across threads, and series.py
    already establishes that split. Only the decode is parallel."""
    if progress is not None:
        progress.set_total(len(files))

    out: Dict[str, date] = {}
    pending: List[ScannedFile] = []

    for f in files:
        cached = (
            cache.get_timestamp(f.path, f.stat.size, f.stat.mtime_ns)
            if cache is not None else None
        )
        if cached is None:
            pending.append(f)
            continue
        try:
            out[f.path] = datetime.fromisoformat(cached[0]).date()
        except ValueError:
            pending.append(f)  # unreadable row: recompute rather than trust
            continue
        if progress is not None:
            progress.increment()

    def _one(scanned: ScannedFile) -> Tuple[ScannedFile, timeline.Timestamp]:
        name = scanned.path.replace("\\", "/").rsplit("/", 1)[-1]
        ts = timeline.extract_timestamp(fs, scanned.path, name, scanned.stat.mtime_ns)
        if progress is not None:
            progress.increment()
        return scanned, ts

    if max_workers > 1 and len(pending) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(_one, pending))
    else:
        results = [_one(f) for f in pending]

    for scanned, ts in results:
        out[scanned.path] = ts.when.date()
        if cache is not None:
            cache.put_timestamp(
                scanned.path, scanned.stat.size, scanned.stat.mtime_ns,
                ts.when.isoformat(), ts.source,
            )
    if cache is not None:
        cache.commit()
    return out


def build_folder_dates(
    fs: FileSystem,
    archive_root: str,
    files: Sequence[ScannedFile],
    *,
    cache: Optional[Cache] = None,
    max_workers: int = DEFAULT_WORKERS,
    progress: Optional[ProgressCounter] = None,
) -> Dict[str, FolderDates]:
    """Day histogram per archive folder, keyed by the folder's path relative
    to `archive_root` (`#Home\\2021 MOUNTAINS`) -- the same key
    tidy/review/views/placement.py's list_archive_dirs() uses for `rel`, so the two indexes
    join without translation.

    `files` is expected to be the media subset of one archive-wide scan.
    Folders under `!NEW` and the tool directories are skipped before any
    decoding happens, so the inbox never costs EXIF reads here."""
    relevant = [f for f in files if not _is_excluded(_relative_dir(f.path, archive_root) or "")]
    days_by_path = extract_days(
        fs, relevant, cache=cache, max_workers=max_workers, progress=progress,
    )

    counts: Dict[str, Dict[date, int]] = {}
    totals: Dict[str, int] = {}
    for f in relevant:
        rel = _relative_dir(f.path, archive_root)
        if rel is None:
            continue
        totals[rel] = totals.get(rel, 0) + 1
        day = days_by_path.get(f.path)
        if day is not None:
            per_day = counts.setdefault(rel, {})
            per_day[day] = per_day.get(day, 0) + 1

    return {
        rel: FolderDates(rel=rel, days=counts.get(rel, {}), total=total)
        for rel, total in totals.items()
    }


def rank_by_date_overlap(
    index: Dict[str, FolderDates],
    source_days: Iterable[date],
    *,
    exclude: Optional[Set[str]] = None,
    tolerance_days: int = DEFAULT_TOLERANCE_DAYS,
    min_overlap_files: int = MIN_OVERLAP_FILES,
    limit: Optional[int] = None,
) -> List[DateMatch]:
    """Folders whose existing contents overlap `source_days`, best first.

    Ordering is by how many of the source's DAYS a folder covers before how
    many FILES it has on them: a folder holding both days of a two-day trip
    is a better answer than one holding fifty photos from a single
    afternoon of it. Ties break on file count, then on name, so the order is
    stable between runs -- an unstable list would reshuffle under the user's
    cursor on every refresh."""
    wanted: Set[date] = set(source_days)
    if not wanted:
        return []

    excluded_cf = {e.casefold() for e in (exclude or set())}
# See: docs/business/rules/events-and-series.md
    near: Dict[date, Set[date]] = {}
    for day in wanted:
        for delta in range(-tolerance_days, tolerance_days + 1):
            near.setdefault(day + timedelta(days=delta), set()).add(day)

    matches: List[DateMatch] = []
    for rel, folder in index.items():
        if rel.casefold() in excluded_cf:
            continue
        overlap_files = 0
        covered: Set[date] = set()
        exact: Set[date] = set()
        matched_days: List[date] = []
        for day, count in folder.days.items():
            vouches = near.get(day)
            if not vouches:
                continue
            overlap_files += count
            covered |= vouches
            if day in wanted:
                exact.add(day)
            matched_days.append(day)
        if overlap_files < min_overlap_files:
            continue
        matches.append(DateMatch(
            rel=rel, overlap_files=overlap_files, overlap_days=len(covered),
            exact_days=len(exact), total=folder.total,
            first_day=min(matched_days), last_day=max(matched_days),
        ))

# See: docs/business/rules/events-and-series.md
    matches.sort(key=lambda m: (
        -m.exact_days, -m.overlap_days, -m.overlap_files, m.rel.casefold(),
    ))
    return matches[:limit] if limit is not None else matches
