"""Visually-similar photo series detection.

Groups Pillow-decodable candidate photos into time-based clusters (cheap:
sort + gap check), then within each cluster finds visually-similar
sub-groups via dHash + Hamming distance, and ranks members to *suggest*
(never decide) a keeper. Final decisions happen in the review UI.

Video, RAW/.psd/.cdr, and audio are out of scope -- this module only ever
looks at files `thumbnails.kind()` classifies as `"image"` (NOT
`thumbnails.supports()`, which also accepts video when ffmpeg is available
-- that capability is for the review UI's thumbnails only, not series
similarity).

dHash values are cached in the SQLite `dhash` column cache.py already
reserves for this purpose, so repeat runs skip re-decoding unchanged files
(mirrors tiers.py's partial/full-hash caching). Sharpness/resolution/EXIF-
presence are cheap relative to a second full decode and aren't persisted --
only unions of >=2 members ever need them, a small subset of all
candidates.
"""
# See: docs/business/rules/events-and-series.md
# dHash cache: docs/technical/components/manifest-and-db.md
from __future__ import annotations

import csv
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from tidy import config
from tidy.media import dhash
from tidy.media import sharpness
from tidy.media import thumbnails
from tidy.media import timeline
from tidy.store.cache import Cache, CachedFile
from tidy.fs.fsmodel import FileSystem
from tidy.media.imaging import DECODE_ERRORS, PILLOW_AVAILABLE
from tidy.progress import DEFAULT_WORKERS, ProgressCounter
from tidy.duplicates.report import display_path, ensure_parent_dir
from tidy.duplicates.scanner import ScannedFile

if PILLOW_AVAILABLE:
    from PIL import Image

# See: docs/business/rules/events-and-series.md
DEFAULT_SERIES_WINDOW_SECONDS = 5 * 60

BURST_SECONDS = 3
SAME_SHOT_SECONDS = 30
KIND_BURST = "burst"
KIND_SAME_SHOT = "same-shot"
KIND_SAME_SCENE = "same-scene"

DEFAULT_HAMMING_THRESHOLD = 6

# See: docs/business/rules/events-and-series.md
_SOURCE_FORMATS_CASEFOLD = frozenset({".heic", ".arw"})
_CONVERTED_FORMATS_CASEFOLD = frozenset({".jpg", ".jpeg"})


@dataclass(frozen=True)
class SeriesMember:
    path: str
    timestamp: timeline.Timestamp
    dhash: int
    width: int
    height: int
    sharpness: float
    size: int
    has_exif: bool


@dataclass(frozen=True)
class SeriesGroup:
    members: Tuple[SeriesMember, ...]
    suggested_keeper: str
    max_gap_seconds: float
    kind: str  # KIND_BURST | KIND_SAME_SHOT | KIND_SAME_SCENE


@dataclass
class SeriesResult:
    groups: List[SeriesGroup] = field(default_factory=list)
# See: docs/business/rules/events-and-series.md
    errors: List[str] = field(default_factory=list)


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat()


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _stem_ext(path: str) -> Tuple[str, str]:
    name = _basename(path)
    dot = name.rfind(".")
    if dot <= 0:
        return name.casefold(), ""
    return name[:dot].casefold(), name[dot:].casefold()


# --- dHash: cached via the SQLite `dhash` column ----------------------------

def _decode_hex_dhash(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


def _put_dhash(cache: Cache, scanned: ScannedFile, value: int, clock: Callable[[], str]) -> None:
    """Write `value` into the cache's dhash column, preserving whatever
    partial_hash/full_hash tiers.py may already have written for this same
    path -- cache.put() is INSERT OR REPLACE over every column, so writing
    this row without first reading the existing one would silently wipe
    tiers.py's hashes back to NULL for any candidate --review also ran
    exact-duplicate detection on."""
    existing = cache.get(scanned.path)
    cache.put(CachedFile(
        path=scanned.path,
        display_path=scanned.path,
        size=scanned.stat.size,
        mtime_ns=scanned.stat.mtime_ns,
        volume_id=scanned.stat.volume_id,
        file_index=scanned.stat.file_index,
        attributes=scanned.stat.attributes,
        partial_hash=existing.partial_hash if existing else None,
        full_hash=existing.full_hash if existing else None,
        dhash=f"{value:016x}",
        status="ok",
        error=None,
        scanned_at=clock(),
    ))


def _get_dhash(
    fs: FileSystem, cache: Cache, scanned: ScannedFile, clock: Callable[[], str],
    *, force_recompute: bool = False,
) -> Optional[int]:
# See: docs/business/rules/events-and-series.md
    if not force_recompute:
        is_fresh = cache.is_hit(scanned.path, scanned.stat.size, scanned.stat.mtime_ns)
        if is_fresh:
            cached = cache.get(scanned.path)
            decoded = _decode_hex_dhash(cached.dhash) if cached else None
            if decoded is not None:
                return decoded

    try:
        with fs.open_read(scanned.path) as stream:
            with Image.open(stream) as image:
                value = dhash.compute_dhash(image)
    except DECODE_ERRORS:
        return None

    _put_dhash(cache, scanned, value, clock)
    return value


# See: docs/business/rules/events-and-series.md

def _dhash_for_cluster_parallel(
    fs: FileSystem,
    cache: Cache,
    cluster: Sequence[_TimedEntry],
    clock: Callable[[], str],
    force_recompute: bool,
    max_workers: int,
) -> Tuple[List["_MemberWithDhash"], List[str]]:
    """Parallel counterpart to running _get_dhash() once per member of one
    time cluster, in cluster order.

    cache.is_hit()/cache.get()/cache.put() (via _put_dhash()) run ONLY on
    the calling thread -- cache.py's Cache can't cross threads (no
    check_same_thread=False, see cache.py's own module docstring) -- the
    ThreadPoolExecutor workers below only ever decode an image and return a
    plain int or None, never touch `cache`. Cache-hit members are resolved
    first, serially (cheap: sqlite reads, no decode); only genuine misses
    go through the pool.

    Iterates `cluster` by index and rebuilds the result in that same order,
    so a caller comparing this function's output against the sequential
    _get_dhash() loop's output (same cluster, same cache state) gets an
    identical with_dhash list regardless of which path ran -- the point of
    tests/test_series.py's parallel-vs-sequential-agree tests."""
    hit_values: Dict[int, int] = {}
    to_decode: List[int] = []

    for i, (scanned, _ts) in enumerate(cluster):
        if not force_recompute and cache.is_hit(scanned.path, scanned.stat.size, scanned.stat.mtime_ns):
            cached = cache.get(scanned.path)
            decoded = _decode_hex_dhash(cached.dhash) if cached else None
            if decoded is not None:
                hit_values[i] = decoded
                continue
        to_decode.append(i)

    def _decode_only(i: int) -> Tuple[int, Optional[int]]:
        scanned, _ts = cluster[i]
        try:
            with fs.open_read(scanned.path) as stream:
                with Image.open(stream) as image:
                    return i, dhash.compute_dhash(image)
        except DECODE_ERRORS:
            return i, None

    decoded_values: Dict[int, Optional[int]] = {}
    if to_decode:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for i, value in executor.map(_decode_only, to_decode):
                decoded_values[i] = value

    with_dhash: List[_MemberWithDhash] = []
    errors: List[str] = []
    for i, (scanned, ts) in enumerate(cluster):
        if i in hit_values:
            with_dhash.append((scanned, ts, hit_values[i]))
            continue
        value = decoded_values.get(i)
        if value is None:
            errors.append(scanned.path)
        else:
            _put_dhash(cache, scanned, value, clock)
            with_dhash.append((scanned, ts, value))
    return with_dhash, errors


# --- per-member metrics (resolution/sharpness/EXIF), computed only for -----
# --- members that survive into a real (>=2) similarity group --------------

@dataclass(frozen=True)
class _RawMetrics:
    width: int
    height: int
    sharpness: float
    has_exif: bool


def _get_raw_metrics(fs: FileSystem, path: str) -> Optional[_RawMetrics]:
    try:
        with fs.open_read(path) as stream:
            with Image.open(stream) as image:
                width, height = image.size
                has_exif = bool(image.getexif())
                sharpness_value = sharpness.laplacian_variance(image)
    except DECODE_ERRORS:
        return None
    return _RawMetrics(width=width, height=height, sharpness=sharpness_value, has_exif=has_exif)


# --- time clustering ---------------------------------------------------------

_TimedEntry = Tuple[ScannedFile, timeline.Timestamp]


def _cluster_by_time(entries: Sequence[_TimedEntry], window_seconds: float) -> List[List[_TimedEntry]]:
    """Split time-sorted entries into clusters, bounding each cluster's
    total span to `window_seconds` from its *first* member -- not just the
    gap between consecutive entries. A pure consecutive-gap check would
    let a steady stream of photos a few seconds apart chain into one
    unboundedly large cluster (a whole day of continuous shooting), which
    would defeat the "compare only within a cluster" cost bound the outer
    window exists for in the first place."""
    ordered = sorted(entries, key=lambda pair: pair[1].when)
    clusters: List[List[_TimedEntry]] = []
    current: List[_TimedEntry] = []
    cluster_start: Optional[datetime] = None

    for entry in ordered:
        when = entry[1].when
        if cluster_start is not None and (when - cluster_start).total_seconds() > window_seconds:
            if len(current) > 1:
                clusters.append(current)
            current = []
            cluster_start = None
        if cluster_start is None:
            cluster_start = when
        current.append(entry)
    if len(current) > 1:
        clusters.append(current)
    return clusters


# --- visual-similarity grouping (union-find over Hamming distance) --------

_MemberWithDhash = Tuple[ScannedFile, timeline.Timestamp, int]


def _group_by_similarity(entries: Sequence[_MemberWithDhash], hamming_threshold: int) -> List[List[int]]:
    n = len(entries)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if dhash.hamming_distance(entries[i][2], entries[j][2]) <= hamming_threshold:
                union(i, j)

    grouped: Dict[int, List[int]] = {}
    for i in range(n):
        grouped.setdefault(find(i), []).append(i)
    return [idxs for idxs in grouped.values() if len(idxs) > 1]


# --- keeper suggestion ranking -----------------------------------------------

def _is_original_over(source_ext: str, converted_ext: str) -> bool:
    return source_ext in _SOURCE_FORMATS_CASEFOLD and converted_ext in _CONVERTED_FORMATS_CASEFOLD


def _select_suggested_keeper(members: Sequence[SeriesMember]) -> str:
    stems = {m.path: _stem_ext(m.path) for m in members}

    def original_rank(m: SeriesMember) -> int:
# See: docs/business/rules/events-and-series.md
        stem, ext = stems[m.path]
        for other in members:
            if other.path == m.path:
                continue
            other_stem, other_ext = stems[other.path]
            if other_stem == stem and _is_original_over(ext, other_ext):
                return 0
        return 1

    def rank_key(m: SeriesMember):
        return (
            original_rank(m),
            -m.size,
            -(m.width * m.height),
            -m.sharpness,
            0 if m.has_exif else 1,
            m.path,
        )

    return min(members, key=rank_key).path


def _classify_kind(max_gap_seconds: float) -> str:
    if max_gap_seconds <= BURST_SECONDS:
        return KIND_BURST
    if max_gap_seconds <= SAME_SHOT_SECONDS:
        return KIND_SAME_SHOT
    return KIND_SAME_SCENE


# --- entry point --------------------------------------------------------------

def find_series(
    fs: FileSystem,
    candidates: Sequence[ScannedFile],
    cache: Cache,
    *,
    window_seconds: float = DEFAULT_SERIES_WINDOW_SECONDS,
    hamming_threshold: int = DEFAULT_HAMMING_THRESHOLD,
    clock: Callable[[], str] = _default_clock,
    force_recompute: bool = False,
    max_workers: int = DEFAULT_WORKERS,
    progress: Optional[ProgressCounter] = None,
) -> SeriesResult:
    """Detect visually-similar photo series among `candidates` (typically
    the same is_duplicate_candidate list tiers.find_duplicates() consumes).
    Never raises: a single undecodable file is recorded in
    SeriesResult.errors and skipped, everything else keeps going.
    `force_recompute` (--rescan) bypasses the dhash cache read, matching
    the rest of the CLI's --rescan contract.

    `max_workers` (--workers, default progress.DEFAULT_WORKERS) parallelizes
    the two per-candidate decode-bound phases (timestamp extraction, dHash
    computation) via ThreadPoolExecutor -- cache.sqlite3 access itself stays
    entirely on the calling thread (see _dhash_for_cluster_parallel()'s
    docstring). `max_workers<=1` runs the original sequential code path
    exactly, byte-for-byte.

    `progress` (optional) is updated as candidates are processed -- see
    progress.ProgressCounter's docstring for why this is a plain shared
    counter rather than a callback threaded through the engine."""
    result = SeriesResult()
    if not PILLOW_AVAILABLE:
        return result

# See: docs/business/rules/events-and-series.md
    photo_candidates = [c for c in candidates if thumbnails.kind(c.path) == "image"]

    if progress is not None:
        progress.set_total(len(photo_candidates))

    def _timestamp_for(c: ScannedFile) -> timeline.Timestamp:
        ts = timeline.extract_timestamp(fs, c.path, _basename(c.path), c.stat.mtime_ns)
        if progress is not None:
            progress.increment()
        return ts

    if max_workers > 1 and len(photo_candidates) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            timestamps = list(executor.map(_timestamp_for, photo_candidates))
    else:
        timestamps = [_timestamp_for(c) for c in photo_candidates]
    timestamped: List[_TimedEntry] = list(zip(photo_candidates, timestamps))

    clusters = _cluster_by_time(timestamped, window_seconds)
    if progress is not None:
        progress.add_total(sum(len(cluster) for cluster in clusters))

    for cluster in clusters:
        if max_workers > 1 and len(cluster) > 1:
            with_dhash, cluster_errors = _dhash_for_cluster_parallel(
                fs, cache, cluster, clock, force_recompute, max_workers,
            )
        else:
            with_dhash = []
            cluster_errors = []
            for scanned, ts in cluster:
                value = _get_dhash(fs, cache, scanned, clock, force_recompute=force_recompute)
                if value is None:
                    cluster_errors.append(scanned.path)
                else:
                    with_dhash.append((scanned, ts, value))
        result.errors.extend(cluster_errors)
        if progress is not None:
            progress.increment(len(cluster))

        for idxs in _group_by_similarity(with_dhash, hamming_threshold):
            group_entries = [with_dhash[i] for i in idxs]
            members: List[SeriesMember] = []
            for scanned, ts, dhash_value in group_entries:
                metrics = _get_raw_metrics(fs, scanned.path)
                if metrics is None:
                    result.errors.append(scanned.path)
                    continue
                members.append(SeriesMember(
                    path=scanned.path, timestamp=ts, dhash=dhash_value,
                    width=metrics.width, height=metrics.height,
                    sharpness=metrics.sharpness, size=scanned.stat.size,
                    has_exif=metrics.has_exif,
                ))

            if len(members) < 2:
                continue  # a decode failure among the group can shrink it below 2

            whens = [m.timestamp.when for m in members]
            max_gap = (max(whens) - min(whens)).total_seconds()
            result.groups.append(SeriesGroup(
                members=tuple(members),
                suggested_keeper=_select_suggested_keeper(members),
                max_gap_seconds=max_gap,
                kind=_classify_kind(max_gap),
            ))

    return result


# --- persistent "skip this group" identity ---
# See: docs/business/rules/events-and-series.md
#
# A skipped group has to stay skipped across RUNS, not just across renders,
# so it needs a name that survives a rescan. The group_id the review UI uses
# ("s1", "s2", ...) cannot be it: it is a position in this run's result list,
# and find_series() renumbers from 1 every time -- "s3" means a different set
# of photos after any file is added, removed or re-dated. The only thing
# about a group that is genuinely stable is WHICH FILES are in it, so that
# is what identifies a skip.

def normalize_member_paths(paths: Iterable[str]) -> Tuple[str, ...]:
    """The canonical, comparable form of a group's member paths: separators
    unified to "/", casefolded, de-duplicated, sorted.

    Casefolding is deliberate even though cache.py stores paths verbatim.
    This archive lives on Windows, where `D:\\Foto\\a.jpg` and `d:\\foto\\A.JPG`
    are the SAME file -- a skip recorded under one spelling must still match
    when a later scan (a different --roots argument, a drive letter typed in
    another case) reports the other. A false MISS here would silently re-show
    a group the user already dismissed, which is exactly the complaint this
    feature answers."""
    return tuple(sorted({p.replace("\\", "/").casefold() for p in paths}))


def skip_key(paths: Iterable[str]) -> str:
    """Stable primary key for one skipped group: a blake2b digest of its
    normalized member paths (same hash family tidy/review/views/duplicates.py's thumb_id_for_path()
    uses for its ids).

    A digest rather than the raw path list because it is the SQLite primary
    key -- fixed width regardless of how many members a group has or how deep
    the archive nests. The paths themselves are stored alongside it anyway
    (see cache.add_series_skip), so nothing is lost by hashing."""
    joined = "\n".join(normalize_member_paths(paths))
    return hashlib.blake2b(joined.encode("utf-8"), digest_size=16).hexdigest()


def group_is_skipped(
    member_paths: Iterable[str], skipped_sets: Iterable[FrozenSet[str]]
) -> bool:
    """True when this group's members are covered by an earlier skip.

    Subset, not equality, and the asymmetry is the whole point:

    - members SHRANK (a photo was deleted, or moved to !REMOVE by an apply on
      another screen) -> still a subset of what was skipped -> stays hidden.
      The user's decision was "these photos are fine as they are"; losing one
      of them does not make the rest newly interesting.
    - members GREW (a new photo matched into the same series) -> no longer a
      subset -> the group comes back. There is now something in it the user
      has never actually looked at, and silently hiding that would be the one
      way this feature could lose a file.

    `skipped_sets` is the caller's already-normalized in-memory mirror of the
    series_skips table (see tidy/review/skips.py's _load_series_skips)."""
    wanted = frozenset(normalize_member_paths(member_paths))
    if not wanted:
        return False
    return any(wanted <= stored for stored in skipped_sets)


# --- CSV export + console summary (mirrors report.py's write_csv/format_*) --

_CSV_COLUMNS = (
    "group_id", "kind", "role", "path", "width", "height", "sharpness",
    "size", "has_exif", "timestamp", "timestamp_source",
)

ROLE_SUGGESTED_KEEPER = "suggested-keeper"
ROLE_SUGGESTED_DISCARD = "suggested-discard"

_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t")


def _csv_safe(value: str) -> str:
    """Same CSV/formula-injection guard as report._csv_safe -- duplicated
    rather than imported since it's a 2-line private helper and importing a
    leaf module's private name would be worse coupling than repeating it."""
    if value and value[0] in _FORMULA_TRIGGER_CHARS:
        return "'" + value
    return value


def write_series_csv(path: str, result: SeriesResult) -> None:
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=config.CSV_DELIMITER)
        writer.writerow(_CSV_COLUMNS)
        for group_id, group in enumerate(result.groups, start=1):
            for member in group.members:
                role = (
                    ROLE_SUGGESTED_KEEPER if member.path == group.suggested_keeper
                    else ROLE_SUGGESTED_DISCARD
                )
                writer.writerow([
                    group_id, group.kind, role, _csv_safe(display_path(member.path)),
                    member.width, member.height, f"{member.sharpness:.2f}",
                    member.size, member.has_exif,
                    member.timestamp.when.isoformat(), member.timestamp.source,
                ])


def format_series_summary(result: SeriesResult, *, top_n: int = 20) -> str:
    total_members = sum(len(g.members) for g in result.groups)
    counts_by_kind: Dict[str, int] = {}
    for g in result.groups:
        counts_by_kind[g.kind] = counts_by_kind.get(g.kind, 0) + 1

    lines = [
        "=== Series and similar photos (--series) ===",
        f"Groups: {len(result.groups)} (total files in groups: {total_members})",
        f"  burst (<={BURST_SECONDS}s): {counts_by_kind.get(KIND_BURST, 0)}",
        f"  same shot (<={SAME_SHOT_SECONDS}s): {counts_by_kind.get(KIND_SAME_SHOT, 0)}",
        f"  same scene: {counts_by_kind.get(KIND_SAME_SCENE, 0)}",
        f"Decode errors: {len(result.errors)}",
    ]
    shown = result.groups[:top_n]
    if shown:
        lines.append(f"{len(shown)} of {len(result.groups)} groups:")
    for i, group in enumerate(shown, start=1):
        lines.append(
            f"  {i}. [{group.kind}, {len(group.members)} files] "
            f"suggested keeper -> {display_path(group.suggested_keeper)}"
        )
        for member in group.members:
            marker = "keeper" if member.path == group.suggested_keeper else "for review"
            lines.append(
                f"       - ({marker}) {display_path(member.path)} "
                f"{member.width}x{member.height}, sharpness={member.sharpness:.1f}, "
                f"{member.size / (1024 * 1024):.2f} MB"
            )
    return "\n".join(lines)
