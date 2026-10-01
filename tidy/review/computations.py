"""Lazy computations, directory/batch caches, slots and series parameter parsing."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from tidy import config
from tidy.analysis import events
from tidy.compression import hwdetect
from tidy.compression import probe
from tidy.analysis import series
from tidy.store.cache import Cache
from tidy.progress import ProgressCounter
from tidy.review.lazy import _LazyComputation
from tidy.review.state import ReviewState
from tidy.review.views.events import build_events_notes, build_events_views
from tidy.review.views.placement import list_archive_dirs
from tidy.review.views.series import build_series_views


def _run_series_computation(state: "ReviewState") -> None:
    """Background-thread worker for GET /api/series.

    Cache is opened HERE, in this worker thread, and closed in `finally`
    -- NOT reused from any Cache the main thread might hold -- because
    cache.py:59 opens sqlite3.connect() without check_same_thread=False,
    so a Cache object created on the main thread would raise
    sqlite3.ProgrammingError the moment this thread touched it (mirrors
    tidy/cli's own `series.find_series` call site, which opens/closes its
    own Cache the same way).

    Never lets an exception escape: this is a daemon thread with no
    caller waiting on it via join()/a return value, so an uncaught
    exception here would just vanish into stderr while the UI stayed on
    "calculating..." forever -- every failure path (Cache open, find_series,
    view-model building) is routed through `state.series_computation.fail()`
    instead."""
    try:
# See: docs/technical/components/review-ui.md
        if not state.db_path or not state.candidates:
            result = series.SeriesResult()
        else:
            cache = Cache(state.db_path)
            try:
                result = series.find_series(
                    state.fs, state.candidates, cache,
                    window_seconds=state.series_window_seconds,
                    hamming_threshold=state.series_hamming_threshold,
                    max_workers=state.max_workers,
                    progress=state.series_progress,
                )
            finally:
                cache.close()

        views, thumb_sources, series_registry = build_series_views(result)
        notes = {"undecoded_files": len(result.errors)}

        def _publish() -> dict:
# See: docs/technical/components/review-ui.md
            state.thumb_sources.update(thumb_sources)
            state.series_registry.update(series_registry)
            return {"groups": views, "notes": notes}

        state.series_computation.finish_ready(_publish)
    except Exception as exc:  # noqa: BLE001 - must never escape a daemon thread
        state.series_computation.fail(str(exc))


def _run_events_computation(state: "ReviewState") -> None:
    """Background-thread worker for GET /api/events -- same reasoning as
    _run_series_computation() above for the never-raise requirement.
    events.find_events() needs no Cache (events have no dHash step), but
    still runs in a background thread rather than inline in
    do_GET(): its GPS-jump splitting and offline geocoding pass over a
    real archive's candidates is not instant either, and do_GET() must
    never block on it."""
    try:
# See: docs/technical/components/review-ui.md
        sizes_by_path = {c.path: c.stat.size for c in state.candidates}
        if not state.db_path or not state.candidates:
            result = events.EventsResult()
        else:
            result = events.find_events(
                state.fs, state.candidates, state.archive_root,
                gap_hours=state.event_gap_hours,
                gps_jump_km=state.gps_jump_km,
                camera_offsets=state.camera_offsets,
                max_workers=state.max_workers,
                progress=state.events_progress,
            )

        views, thumb_sources, events_registry = build_events_views(result, sizes_by_path)
        notes = build_events_notes(result)

        def _publish() -> dict:
# See: docs/technical/components/review-ui.md
            state.thumb_sources.update(thumb_sources)
            state.events_registry.update(events_registry)
            return {"groups": views, "notes": notes}

        state.events_computation.finish_ready(_publish)
    except Exception as exc:  # noqa: BLE001 - must never escape a daemon thread
        state.events_computation.fail(str(exc))


DEFAULT_PLACEMENT_SOURCE = config.NEW_DIR_NAME


DEFAULT_COMPRESS_SOURCE = config.NEW_DIR_NAME


def _dir_index(state: "ReviewState") -> List[dict]:
    """list_archive_dirs() memoised on the state. The placement screen polls
    roughly once a second while computing, and every poll re-validates its
    source directory against this list -- walking two levels of a real
    archive that often would be pointless I/O. Invalidated by
    _invalidate_dir_index() after an apply, which is the only thing in this
    process that changes which directories exist."""
    with state.placement_lock:
        if state.dir_index_cache is None:
            state.dir_index_cache = list_archive_dirs(state.fs, state.archive_root)
        return state.dir_index_cache


def _invalidate_dir_index(state: "ReviewState") -> None:
    with state.placement_lock:
        state.dir_index_cache = None


def _invalidate_batch_cache(state: "ReviewState") -> None:
    """Called after anything that can change what batches.list_batches()
    would report: a fresh apply (a new batch directory), a restore (a
    `restorable` count dropping to zero), or a rescan. Same placement_lock
    as _invalidate_dir_index() -- both guard a small cache invalidated by a
    mutation and read back lazily, not state used by the apply path
    itself."""
    with state.placement_lock:
        state.batch_list_cache = None


def _register_batch(state: "ReviewState", manifest_path: str) -> str:
    """Return the stable id for `manifest_path`, minting a new one (`b1`,
    `b2`, ...) the first time this manifest is seen. Called both from a
    fresh apply (the payload's own "batch_id") and from
    GET /api/batches (batches.list_batches() populating the "Restore" tab),
    so the SAME batch shown in both places always carries the same id for
    the rest of this session -- the property every other id registry in
    this module already guarantees (module docstring's table), extended to
    a fourth kind of id.

    manifest_path is a real, absolute path and is deliberately the ONLY
    thing ever stored here -- POST /api/restore resolves a client's
    batch_id through this dict and nothing else, so a manifest outside
    !MOVES/!REMOVE can never be named this way."""
    with state.placement_lock:
        for existing_id, existing_path in state.batch_registry.items():
            if existing_path == manifest_path:
                return existing_id
        batch_id = f"b{len(state.batch_registry) + 1}"
        state.batch_registry[batch_id] = manifest_path
        return batch_id


def _placement_slots(state: "ReviewState", source_rel: str) -> Tuple[_LazyComputation, ProgressCounter]:
    """Get (or create) the computation + progress counter for one source
    directory, under placement_lock so two concurrent first requests for the
    same directory can never create two computations for it -- the same
    idle->computing race _LazyComputation guards internally, one level up
    where the dict entry itself is created."""
    with state.placement_lock:
        computation = state.placement_computations.get(source_rel)
        if computation is None:
            computation = _LazyComputation()
            state.placement_computations[source_rel] = computation
            state.placement_progress[source_rel] = ProgressCounter()
        return computation, state.placement_progress[source_rel]


def _compress_slots(state: "ReviewState", source_rel: str) -> Tuple[_LazyComputation, ProgressCounter]:
    """Get (or create) the computation + progress counter for one source
    directory -- exact mirror of _placement_slots(), same reason: two
    concurrent first requests for the same directory must never create two
    computations for it. Its own compress_lock, not placement_lock: a
    compress scan probes every video with ffprobe (seconds to minutes) and
    must not queue behind -- or block -- the placement screen's own
    background work."""
    with state.compress_lock:
        computation = state.compress_computations.get(source_rel)
        if computation is None:
            computation = _LazyComputation()
            state.compress_computations[source_rel] = computation
            state.compress_progress[source_rel] = ProgressCounter()
        return computation, state.compress_progress[source_rel]


def _hwdetect_for(state: "ReviewState") -> Optional["hwdetect.HwDetectResult"]:
    """Memoised hwdetect.detect() result, shared by GET /api/compress's
    background worker and GET /api/compress/encoders -- detection probes
    every hardware encoder with a real 1-frame encode (hwdetect.py's
    _test_encoder(), seconds of subprocess work), so it must run at most
    once per server lifetime, not once per request or per source
    directory. None also covers "ffmpeg not resolvable" (hwdetect.detect()
    itself returned None) and a Smart App Control block (reason in
    state.hwdetect_block_reason): neither is memoised, so the next call
    re-detects and recovers without a restart. Not-found fails fast on a
    file-existence check, unlike the successful path's hardware probing
    that this cache exists to avoid repeating."""
    with state.compress_lock:
        if state.hwdetect_cache is None:
            try:
                state.hwdetect_cache = hwdetect.detect(state.compress_config_path)
            except probe.ToolBlockedError as exc:
                state.hwdetect_block_reason = str(exc)
                return None
            state.hwdetect_block_reason = None
        return state.hwdetect_cache


def _note_tool_blocked(state: "ReviewState", exc: "probe.ToolBlockedError") -> str:
    """Record a Smart App Control block seen mid-operation: drops the
    memoised detect result so the next _hwdetect_for() re-checks. Returns
    the readable reason."""
    reason = str(exc)
    with state.compress_lock:
        state.hwdetect_cache = None
        state.hwdetect_block_reason = reason
    return reason


# --- series tuning knobs settable from the review UI (dev mode only) -------
# See: docs/business/rules/events-and-series.md
#
# The UI offers a fixed list of values; these bounds are what guards the
# engine against anything else. The window is the dangerous one, not the
# threshold: it is the ONLY bound on _group_by_similarity()'s pairwise
# Hamming comparison (see series.py's DEFAULT_SERIES_WINDOW_SECONDS
# comment), so a window measured in hours turns a day of continuous
# shooting into one cluster and that comparison quadratic in its size.
SERIES_HAMMING_MIN = 0


SERIES_HAMMING_MAX = 64  # dhash.py packs exactly 64 bits


SERIES_WINDOW_MIN_SECONDS = 1.0


SERIES_WINDOW_MAX_SECONDS = 3600.0


def _parse_series_params(
    payload: dict,
) -> Tuple[Optional[Dict[str, float]], Optional[str]]:
    """Validate the optional series-tuning half of POST /api/rescan's body.

    Returns ({}, None) when neither key is present: the body was a bare
    "{}" long before this feature existed, and a rescan that carries no
    tuning must leave every current value alone.

    Out-of-range values are REFUSED, never clamped. A clamp would leave the
    dev-mode select showing a number the engine did not use, which is the
    one way this control could misreport what produced the groups on
    screen."""
    out: Dict[str, float] = {}

    if "series_hamming_threshold" in payload:
        value = payload["series_hamming_threshold"]
        # bool is an int subclass -- True would otherwise pass as 1.
        if isinstance(value, bool) or not isinstance(value, int):
            return None, "series_hamming_threshold must be an integer"
        if not SERIES_HAMMING_MIN <= value <= SERIES_HAMMING_MAX:
            return None, (
                "series_hamming_threshold out of range "
                f"{SERIES_HAMMING_MIN}-{SERIES_HAMMING_MAX}"
            )
        out["series_hamming_threshold"] = value

    if "series_window_seconds" in payload:
        value = payload["series_window_seconds"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "series_window_seconds must be a number"
        # NaN/inf need no separate test: every comparison below is False for
        # NaN, and the upper bound rejects inf.
        if not SERIES_WINDOW_MIN_SECONDS <= value <= SERIES_WINDOW_MAX_SECONDS:
            return None, (
                "series_window_seconds out of range "
                f"{int(SERIES_WINDOW_MIN_SECONDS)}-{int(SERIES_WINDOW_MAX_SECONDS)} s"
            )
        out["series_window_seconds"] = float(value)

    return out, None
