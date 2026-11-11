"""Persisted skip helpers for series, placement and compress groups."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from tidy.analysis import series
from tidy.store.cache import Cache
from tidy.review.state import ReviewState


# See: docs/technical/components/review-ui.md

def _load_series_skips(state: "ReviewState") -> None:
    """Populate state.series_skips from the cache DB. Called from
    start_server() on the main thread (the same thread that will never touch
    this Cache again -- it is opened and closed here), before a single
    request can be served.

    A missing/unreadable DB is not an error worth refusing to start over: the
    user gets a review server whose skips are session-only, which is strictly
    better than no review server. `db_path` is None for most tests and for
    any caller that never asked for series at all, and that path is not even
    a degraded case -- there is nothing to load."""
    if not state.db_path:
        return
    try:
        cache = Cache(state.db_path)
    except Exception:  # noqa: BLE001 - a broken cache must not block the UI
        return
    try:
        stored = cache.list_series_skips()
    except Exception:  # noqa: BLE001 - e.g. a DB from a future schema
        return
    finally:
        cache.close()
    with state.series_skip_lock:
        state.series_skips = {key: frozenset(paths) for key, paths in stored}


def _series_group_member_paths(state: "ReviewState", group_id: str) -> List[str]:
    """The real filesystem paths of one series group, read out of
    series_registry -- the same allowlist POST /api/apply-series and
    POST /api/open resolve ids through (module docstring's registry table).

    This is why POST /api/series-skip takes a group_id and never a list of
    paths: a client can only ever name a group the server itself published,
    so no request can make the server record (and later act on) a path it
    never scanned. An unknown group_id yields [], which the caller turns into
    a 400."""
    return [
        entry["path"]
        for entry in state.series_registry.values()
        if entry["group_id"] == group_id
    ]


def _skipped_series_group_ids(state: "ReviewState") -> Set[str]:
    """Which of the currently-published series groups are covered by a stored
    skip. Recomputed per GET /api/series rather than cached: the input
    (series_registry) is fixed once the computation is ready, but the skip set
    changes on every checkbox click, and a stale answer here is exactly the
    "I unchecked it and it stayed hidden" bug. Cost is a walk over a few
    thousand dict entries plus a subset test per group -- far below the JSON
    serialization of the same response."""
    with state.series_skip_lock:
        stored = list(state.series_skips.values())
    if not stored:
        return set()

    paths_by_group: Dict[str, List[str]] = {}
    for entry in state.series_registry.values():
        paths_by_group.setdefault(entry["group_id"], []).append(entry["path"])

    return {
        group_id
        for group_id, paths in paths_by_group.items()
        if series.group_is_skipped(paths, stored)
    }


def _set_series_skip(
    state: "ReviewState", group_id: str, skip: bool
) -> Tuple[Optional[dict], Optional[str]]:
    """Record or clear the skip for one series group, in memory AND in the
    cache DB, or return an error message.

    Returns (payload, None) on success. `payload["persisted"]` is False when
    this session has no db_path (tests, and any caller that never passed one):
    the skip still takes effect for this run, it just cannot outlive it --
    reported rather than silently pretended, since "remembered next time" is
    the entire point of the feature.

    Un-skipping deletes EVERY stored set that covers this group, not just the
    exactly-matching one -- see cache.remove_series_skips()'s docstring for
    why a narrower delete would leave the checkbox looking broken."""
    paths = _series_group_member_paths(state, group_id)
    if not paths:
        return None, f"unknown series group_id: {group_id!r}"

    normalized = series.normalize_member_paths(paths)
    wanted = frozenset(normalized)

    if skip:
        key = series.skip_key(paths)
        added, removed = {key: wanted}, []
    else:
        key = None
        added = {}
        with state.series_skip_lock:
            removed = [k for k, stored in state.series_skips.items() if wanted <= stored]

    persisted = False
    if state.db_path:
        try:
            cache = Cache(state.db_path)
            try:
                if skip:
                    cache.add_series_skip(
                        key, normalized, datetime.now(timezone.utc).isoformat(),
                    )
                else:
                    cache.remove_series_skips(removed)
            finally:
                cache.close()
            persisted = True
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            return None, f"failed to save the decision in the database: {exc}"

# See: docs/technical/components/review-ui.md
    with state.series_skip_lock:
        state.series_skips.update(added)
        for k in removed:
            state.series_skips.pop(k, None)

    return {
        "group_id": group_id,
        "skipped": skip,
        "persisted": persisted,
        "skipped_group_count": len(_skipped_series_group_ids(state)),
    }, None


# See: docs/technical/components/review-ui.md

def _load_placement_skips(state: "ReviewState") -> None:
    """Populate state.placement_skips from the cache DB. Called from
    start_server() on the main thread, same timing/reasoning as
    _load_series_skips()."""
    if not state.db_path:
        return
    try:
        cache = Cache(state.db_path)
    except Exception:  # noqa: BLE001 - a broken cache must not block the UI
        return
    try:
        stored = cache.list_placement_skips()
    except Exception:  # noqa: BLE001 - e.g. a DB from a future schema
        return
    finally:
        cache.close()
    with state.placement_skip_lock:
        state.placement_skips = {key: frozenset(paths) for key, paths in stored}


def _placement_group_member_paths(state: "ReviewState", group_id: str) -> List[str]:
    """The real filesystem paths of one placement suggestion, read out of
    placement_registry -- the same allowlist POST /api/apply-placement and
    POST /api/open resolve ids through."""
    return [
        entry["path"]
        for entry in state.placement_registry.values()
        if entry["group_id"] == group_id
    ]


def _skipped_placement_group_ids(state: "ReviewState") -> Set[str]:
    """Which of the currently-published placement suggestions are covered by
    a stored skip. Recomputed per GET /api/placement, same reasoning as
    _skipped_series_group_ids()."""
    with state.placement_skip_lock:
        stored = list(state.placement_skips.values())
    if not stored:
        return set()

    paths_by_group: Dict[str, List[str]] = {}
    for entry in state.placement_registry.values():
        paths_by_group.setdefault(entry["group_id"], []).append(entry["path"])

    return {
        group_id
        for group_id, paths in paths_by_group.items()
        if series.group_is_skipped(paths, stored)
    }


def _set_placement_skip(
    state: "ReviewState", group_id: str, skip: bool
) -> Tuple[Optional[dict], Optional[str]]:
    """Record or clear the skip for one placement suggestion, in memory AND
    in the cache DB, or return an error message. Same shape/reasoning as
    _set_series_skip() -- see its docstring for why paths (not group_id) are
    the identity, why un-skipping deletes every covering stored set, and why
    `payload["persisted"]` can be False."""
    paths = _placement_group_member_paths(state, group_id)
    if not paths:
        return None, f"unknown placement group_id: {group_id!r}"

    normalized = series.normalize_member_paths(paths)
    wanted = frozenset(normalized)

    if skip:
        key = series.skip_key(paths)
        added, removed = {key: wanted}, []
    else:
        key = None
        added = {}
        with state.placement_skip_lock:
            removed = [k for k, stored in state.placement_skips.items() if wanted <= stored]

    persisted = False
    if state.db_path:
        try:
            cache = Cache(state.db_path)
            try:
                if skip:
                    cache.add_placement_skip(
                        key, normalized, datetime.now(timezone.utc).isoformat(),
                    )
                else:
                    cache.remove_placement_skips(removed)
            finally:
                cache.close()
            persisted = True
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            return None, f"failed to save the decision in the database: {exc}"

    with state.placement_skip_lock:
        state.placement_skips.update(added)
        for k in removed:
            state.placement_skips.pop(k, None)

    return {
        "group_id": group_id,
        "skipped": skip,
        "persisted": persisted,
        "skipped_group_count": len(_skipped_placement_group_ids(state)),
    }, None


# See: docs/technical/components/review-ui.md

def _load_compress_skips(state: "ReviewState") -> None:
    """Populate state.compress_skips from the cache DB. Called from
    start_server() on the main thread, same timing/reasoning as
    _load_placement_skips()."""
    if not state.db_path:
        return
    try:
        cache = Cache(state.db_path)
    except Exception:  # noqa: BLE001 - a broken cache must not block the UI
        return
    try:
        stored = cache.list_compress_skips()
    except Exception:  # noqa: BLE001 - e.g. a DB from a future schema
        return
    finally:
        cache.close()
    with state.compress_skip_lock:
        state.compress_skips = {key: frozenset(paths) for key, paths in stored}


def _compress_group_member_paths(state: "ReviewState", group_id: str) -> List[str]:
    """The real filesystem paths of one compress group, read out of
    compress_registry -- the same allowlist every compress endpoint
    resolves ids through."""
    return [
        entry["path"]
        for entry in state.compress_registry.values()
        if entry["group_id"] == group_id
    ]


def _skipped_compress_group_ids(state: "ReviewState") -> Set[str]:
    """Which of the currently-published compress candidates are covered by
    a stored skip. Recomputed per GET /api/compress, same reasoning as
    _skipped_placement_group_ids()."""
    with state.compress_skip_lock:
        stored = list(state.compress_skips.values())
    if not stored:
        return set()

    paths_by_group: Dict[str, List[str]] = {}
    for entry in state.compress_registry.values():
        paths_by_group.setdefault(entry["group_id"], []).append(entry["path"])

    return {
        group_id
        for group_id, paths in paths_by_group.items()
        if series.group_is_skipped(paths, stored)
    }


def _set_compress_skip(
    state: "ReviewState", group_id: str, skip: bool
) -> Tuple[Optional[dict], Optional[str]]:
    """Record or clear the skip for one compress candidate, in memory AND
    in the cache DB, or return an error message. Same shape/reasoning as
    _set_placement_skip()."""
    paths = _compress_group_member_paths(state, group_id)
    if not paths:
        return None, f"unknown compress group_id: {group_id!r}"

    normalized = series.normalize_member_paths(paths)
    wanted = frozenset(normalized)

    if skip:
        key = series.skip_key(paths)
        added, removed = {key: wanted}, []
    else:
        key = None
        added = {}
        with state.compress_skip_lock:
            removed = [k for k, stored in state.compress_skips.items() if wanted <= stored]

    persisted = False
    if state.db_path:
        try:
            cache = Cache(state.db_path)
            try:
                if skip:
                    cache.add_compress_skip(
                        key, normalized, datetime.now(timezone.utc).isoformat(),
                    )
                else:
                    cache.remove_compress_skips(removed)
            finally:
                cache.close()
            persisted = True
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            return None, f"failed to save the decision in the database: {exc}"

    with state.compress_skip_lock:
        state.compress_skips.update(added)
        for k in removed:
            state.compress_skips.pop(k, None)

    return {
        "group_id": group_id,
        "skipped": skip,
        "persisted": persisted,
        "skipped_group_count": len(_skipped_compress_group_ids(state)),
    }, None


# See: docs/technical/components/review-ui.md

def _load_dup_skips(state: "ReviewState") -> None:
    """Populate state.dup_skips from the cache DB, same timing/degradation as
    _load_series_skips()."""
    if not state.db_path:
        return
    try:
        cache = Cache(state.db_path)
    except Exception:  # noqa: BLE001 - a broken cache must not block the UI
        return
    try:
        stored = cache.list_dup_skips()
    except Exception:  # noqa: BLE001 - e.g. a DB from a future schema
        return
    finally:
        cache.close()
    with state.dup_skip_lock:
        state.dup_skips = {key: frozenset(paths) for key, paths in stored}


def _dup_group_member_paths(state: "ReviewState", group_id: str) -> List[str]:
    """The real paths of one exact-duplicate group, read out of file_registry
    only -- an id from another screen's registry is unknown here."""
    return [
        entry["path"]
        for entry in state.file_registry.values()
        if entry["group_id"] == group_id
    ]


def _skipped_dup_group_ids(state: "ReviewState") -> Set[str]:
    """Which published duplicate groups are covered by a stored skip;
    recomputed per GET /api/state."""
    with state.dup_skip_lock:
        stored = list(state.dup_skips.values())
    if not stored:
        return set()

    paths_by_group: Dict[str, List[str]] = {}
    for entry in state.file_registry.values():
        paths_by_group.setdefault(entry["group_id"], []).append(entry["path"])

    return {
        group_id
        for group_id, paths in paths_by_group.items()
        if series.group_is_skipped(paths, stored)
    }


def _set_dup_skip(
    state: "ReviewState", group_id: str, skip: bool
) -> Tuple[Optional[dict], Optional[str]]:
    """Record or clear the skip for one duplicate group, in memory AND in the
    cache DB, or return an error message. Same contract as _set_series_skip()."""
    paths = _dup_group_member_paths(state, group_id)
    if not paths:
        return None, f"unknown duplicate group_id: {group_id!r}"

    normalized = series.normalize_member_paths(paths)
    wanted = frozenset(normalized)

    if skip:
        key = series.skip_key(paths)
        added, removed = {key: wanted}, []
    else:
        key = None
        added = {}
        with state.dup_skip_lock:
            removed = [k for k, stored in state.dup_skips.items() if wanted <= stored]

    persisted = False
    if state.db_path:
        try:
            cache = Cache(state.db_path)
            try:
                if skip:
                    cache.add_dup_skip(
                        key, normalized, datetime.now(timezone.utc).isoformat(),
                    )
                else:
                    cache.remove_dup_skips(removed)
            finally:
                cache.close()
            persisted = True
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            return None, f"failed to save the decision in the database: {exc}"

    with state.dup_skip_lock:
        state.dup_skips.update(added)
        for k in removed:
            state.dup_skips.pop(k, None)

    return {
        "group_id": group_id,
        "skipped": skip,
        "persisted": persisted,
        "skipped_group_count": len(_skipped_dup_group_ids(state)),
    }, None
