"""Three-tier duplicate detection:

  Tier 1: group by size. A unique size can't have a duplicate -- dropped,
          no read at all.
  Tier 2: BLAKE2b over first+last window_bytes ("partial hash"). Same size
          but different partial hash -> not a duplicate, dropped here.
  Tier 3: full-file BLAKE2b, only for files that survived tier 2. Files
          <= 2*window_bytes already had their whole content covered by the
          tier-2 read, so their tier-3 hash is the tier-2 hash reused
          verbatim (no second read).

A SQLite Cache (cache.py) makes repeat runs cheap: a cache hit (same path,
size and mtime_ns, with the relevant hash already populated) skips
open_read entirely.

Before any tier-2/3 hashing, `find_duplicates` computes an estimate and
calls the injected `confirm(message) -> bool`. Declining skips all hashing;
`--yes` non-interactive callers simply pass a `confirm` that always returns
True (the estimate is still computed and available on the result either
way, and it's up to the caller whether/how to print it before deciding).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from tidy import config
from tidy.duplicates import hashing
from tidy.store.cache import Cache, CachedFile
from tidy.fs.fsmodel import FileSystem
from tidy.duplicates.scanner import ScannedFile, ScanError


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DuplicateGroup:
    size: int
    full_hash: str
    paths: Tuple[str, ...]


@dataclass
class TierEstimate:
    file_count: int
    total_bytes: int
    message: str


@dataclass
class TierResult:
    duplicate_groups: List[DuplicateGroup] = field(default_factory=list)
    tier1_unique_count: int = 0
    tier2_dropped_count: int = 0
    tier3_not_duplicate_count: int = 0
    errors: List[ScanError] = field(default_factory=list)
    confirmed: bool = True
    estimate: Optional[TierEstimate] = None


def _group_by_size(candidates: Iterable[ScannedFile]) -> Dict[int, List[ScannedFile]]:
    groups: Dict[int, List[ScannedFile]] = {}
    for c in candidates:
        groups.setdefault(c.stat.size, []).append(c)
    return groups


def _files_needing_tier2(candidates: Iterable[ScannedFile]) -> List[ScannedFile]:
    groups = _group_by_size(candidates)
    needing: List[ScannedFile] = []
    for size, members in groups.items():
        if len(members) > 1:
            needing.extend(members)
    return needing


def estimate_work(candidates: Iterable[ScannedFile]) -> TierEstimate:
    needing = _files_needing_tier2(candidates)
    total_bytes = sum(c.stat.size for c in needing)
    gb = total_bytes / (1024 ** 3)
    message = f"To read: {gb:.2f} GB in {len(needing)} files"
    return TierEstimate(file_count=len(needing), total_bytes=total_bytes, message=message)


def _make_cached_file(
    scanned: ScannedFile,
    *,
    partial_hash: Optional[str],
    full_hash: Optional[str],
    dhash: Optional[str],
    status: str,
    error: Optional[str],
    clock: Callable[[], str],
) -> CachedFile:
    return CachedFile(
        path=scanned.path,
        display_path=scanned.path,
        size=scanned.stat.size,
        mtime_ns=scanned.stat.mtime_ns,
        volume_id=scanned.stat.volume_id,
        file_index=scanned.stat.file_index,
        attributes=scanned.stat.attributes,
        partial_hash=partial_hash,
        full_hash=full_hash,
        dhash=dhash,
        status=status,
        error=error,
        scanned_at=clock(),
    )


def _get_partial_hash(
    fs: FileSystem,
    cache: Cache,
    scanned: ScannedFile,
    window_bytes: int,
    errors: List[ScanError],
    clock: Callable[[], str],
) -> Optional[str]:
    cached = cache.get(scanned.path)
    is_fresh = cache.is_hit(scanned.path, scanned.stat.size, scanned.stat.mtime_ns)
    if is_fresh and cached is not None and cached.partial_hash is not None:
        return cached.partial_hash

# See: docs/technical/components/duplicates-mechanism.md
    existing_dhash = cached.dhash if cached else None

    try:
        with fs.open_read(scanned.path) as stream:
            partial = hashing.partial_hash(stream, scanned.stat.size, window_bytes)
            full = None
            if hashing.can_reuse_partial_as_full(scanned.stat.size, window_bytes):
                full = partial
        cache.put(
            _make_cached_file(
                scanned, partial_hash=partial, full_hash=full, dhash=existing_dhash,
                status="ok", error=None, clock=clock,
            )
        )
        return partial
    except (OSError, PermissionError) as exc:
        errors.append(ScanError(path=scanned.path, message=str(exc)))
        cache.put(
            _make_cached_file(
                scanned, partial_hash=None, full_hash=None, dhash=existing_dhash,
                status="error", error=str(exc), clock=clock,
            )
        )
        return None


def _get_full_hash(
    fs: FileSystem,
    cache: Cache,
    scanned: ScannedFile,
    partial_hash: str,
    window_bytes: int,
    errors: List[ScanError],
    clock: Callable[[], str],
) -> Optional[str]:
    if hashing.can_reuse_partial_as_full(scanned.stat.size, window_bytes):
        return partial_hash  # already computed during tier 2, no second read

    cached = cache.get(scanned.path)
    is_fresh = cache.is_hit(scanned.path, scanned.stat.size, scanned.stat.mtime_ns)
    if is_fresh and cached is not None and cached.full_hash is not None:
        return cached.full_hash

    # See _get_partial_hash's identical comment: preserve any dhash
    # series.py already cached for this path instead of nulling it out.
    existing_dhash = cached.dhash if cached else None

    try:
        with fs.open_read(scanned.path) as stream:
            full = hashing.full_hash(stream)
        cache.put(
            _make_cached_file(
                scanned, partial_hash=partial_hash, full_hash=full, dhash=existing_dhash,
                status="ok", error=None, clock=clock,
            )
        )
        return full
    except (OSError, PermissionError) as exc:
        errors.append(ScanError(path=scanned.path, message=str(exc)))
        cache.put(
            _make_cached_file(
                scanned, partial_hash=partial_hash, full_hash=None, dhash=existing_dhash,
                status="error", error=str(exc), clock=clock,
            )
        )
        return None


def find_duplicates(
    fs: FileSystem,
    candidates: Iterable[ScannedFile],
    cache: Cache,
    *,
    confirm: Callable[[str], bool],
    window_bytes: int = config.TIER2_WINDOW_BYTES,
    clock: Callable[[], str] = _default_clock,
) -> TierResult:
    candidates = list(candidates)
    size_groups = _group_by_size(candidates)

    result = TierResult()
    tier2_input: List[ScannedFile] = []
    for size, members in size_groups.items():
        if len(members) == 1:
            result.tier1_unique_count += 1
        else:
            tier2_input.extend(members)

    result.estimate = estimate_work(candidates)

    if not tier2_input:
        result.confirmed = True
        return result

    if not confirm(result.estimate.message):
        result.confirmed = False
        return result
    result.confirmed = True

    errors: List[ScanError] = []

    # Tier 2: partial hash, grouped by (size, partial_hash).
    tier2_groups: Dict[Tuple[int, str], List[ScannedFile]] = {}
    partial_hashes: Dict[str, str] = {}
    for scanned in tier2_input:
        partial = _get_partial_hash(fs, cache, scanned, window_bytes, errors, clock)
        if partial is None:
            continue  # error already recorded
        partial_hashes[scanned.path] = partial
        tier2_groups.setdefault((scanned.stat.size, partial), []).append(scanned)

    for key, members in tier2_groups.items():
        if len(members) == 1:
            result.tier2_dropped_count += 1
            continue

        # Tier 3: full hash, grouped by (size, full_hash), within this
        # tier-2 survivor group.
        size, partial = key
        tier3_groups: Dict[str, List[ScannedFile]] = {}
        for scanned in members:
            full = _get_full_hash(
                fs, cache, scanned, partial_hashes[scanned.path], window_bytes, errors, clock
            )
            if full is None:
                continue
            tier3_groups.setdefault(full, []).append(scanned)

        for full_hash_value, group_members in tier3_groups.items():
            if len(group_members) == 1:
                result.tier3_not_duplicate_count += 1
                continue
            result.duplicate_groups.append(
                DuplicateGroup(
                    size=size,
                    full_hash=full_hash_value,
                    paths=tuple(m.path for m in group_members),
                )
            )

    result.errors = errors
    return result
