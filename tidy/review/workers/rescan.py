"""Background rescan of every registry."""
from __future__ import annotations

from typing import Dict, Optional

from tidy.progress import ProgressCounter
from tidy.review.lazy import _LazyComputation
from tidy.review.state import ReviewState
from tidy.review.views.duplicates import build_group_views


def _run_rescan(
    state: "ReviewState", series_params: Optional[Dict[str, float]] = None,
) -> dict:
    """Re-walk the archive and throw away every computed view (POST
    /api/rescan).

    A full scan rather than a recompute of the current tab: the reason to
    press this is that the archive changed on disk -- another batch dropped
    into `!NEW`, files moved by hand -- and a recompute over the file list
    captured at startup would answer confidently from a stale world.

    `series_params` (already validated by _parse_series_params()) is applied
    INSIDE the same `with state.lock:` that replaces series_computation
    below, deliberately not by the caller afterwards: between this function
    returning and a caller assigning those fields there is a window in which
    a concurrent GET /api/series starts the fresh computation on the OLD
    threshold -- and _LazyComputation then caches that result as "ready"
    for the rest of the session.

    Every registry is rebuilt from scratch, so the applied-id sets are
    cleared with them. They must be: ids are positional (`p3:1`), a fresh
    scan renumbers everything, and a surviving id set would mark whichever
    unrelated group inherited that number as already applied, silently
    hiding it from the user. `applied_batches` is kept -- it is the log of
    what this session actually moved, and it references manifests on disk,
    not ids."""
    if state.rescan is None:
        raise RuntimeError("rescan not available in this session")

    duplicate_groups, decisions, candidates = state.rescan()
    groups_json, thumb_sources, file_registry = build_group_views(duplicate_groups, decisions)

    with state.lock:
        state.duplicate_groups = list(duplicate_groups)
        state.decisions = list(decisions)
        state.candidates = list(candidates)
        state.groups_json = groups_json
        state.file_registry = file_registry
        state.total_reclaimable_bytes = sum(g["reclaimable_bytes"] for g in groups_json)
        state.thumb_sources = dict(thumb_sources)
        state.series_registry.clear()
        state.events_registry.clear()
        state.placement_registry.clear()
        state.compress_registry.clear()
        state.applied_group_ids.clear()
        state.applied_series_group_ids.clear()
        state.applied_placement_group_ids.clear()
        state.applied_compress_group_ids.clear()
# See: docs/technical/components/review-ui.md
        state.scan_generation += 1
        # _LazyComputation deliberately never recomputes once ready, so
        # refreshing a tab means replacing the object, not resetting it.
        state.series_computation = _LazyComputation()
        state.events_computation = _LazyComputation()
        state.series_progress = ProgressCounter()
        state.events_progress = ProgressCounter()
# See: docs/technical/components/review-ui.md
        if series_params:
            state.series_window_seconds = series_params.get(
                "series_window_seconds", state.series_window_seconds,
            )
            state.series_hamming_threshold = series_params.get(
                "series_hamming_threshold", state.series_hamming_threshold,
            )
        applied_window = state.series_window_seconds
        applied_hamming = state.series_hamming_threshold

    with state.placement_lock:
        state.placement_computations.clear()
        state.placement_progress.clear()
# See: docs/technical/components/review-ui.md
        state.date_index = None
        state.dir_index_cache = None
        state.batch_list_cache = None

    with state.compress_lock:
        state.compress_computations.clear()
        state.compress_progress.clear()
# See: docs/technical/components/review-ui.md

    return {
        "duplicate_groups": len(groups_json),
        "candidates": len(state.candidates),
        "series_window_seconds": applied_window,
        "series_hamming_threshold": applied_hamming,
    }
