"""Route tables. The POST table is the single whitelist: an unlisted path is a 404."""
from __future__ import annotations

from tidy.review.handler import (
    routes_compress,
    routes_compress_apply,
    routes_dup,
    routes_placement,
    routes_restore,
    routes_state,
)

# Exact GET paths -> fn(handler, state, urlsplit_result)
GET_ROUTES = {
    "/": routes_state.handle_index,
    "/api/state": routes_state.handle_state,
    "/api/series": routes_state.handle_series,
    "/api/events": routes_state.handle_events,
    "/api/source-dirs": routes_placement.handle_source_dirs,
    "/api/placement": routes_placement.handle_placement_route,
    "/api/compress": routes_compress.handle_compress_route,
    "/api/compress/encoders": routes_compress.handle_compress_encoders,
    "/api/compress/jobs": routes_compress.handle_compress_jobs,
    "/api/batches": routes_restore.handle_batches,
    "/api/batch-entries": routes_restore.handle_batch_entries_route,
}

# GET path prefixes -> fn(handler, state, urlsplit_result); tried after GET_ROUTES
GET_PREFIX_ROUTES = {
    "/api/thumb/": routes_dup.handle_thumb,
}

# Every POST path -> fn(handler, state, body); all sit behind _check_mutating_request.
POST_ROUTES = {
    "/api/apply": routes_dup.handle_apply,
    "/api/apply-series": routes_state.handle_apply_series,
    "/api/apply-placement": routes_placement.handle_apply_placement,
    "/api/open": routes_dup.handle_open,
    "/api/thumb/rotate": routes_dup.handle_rotate_thumb,
    "/api/rescan": routes_state.handle_rescan,
    "/api/restore": routes_restore.handle_restore,
    "/api/series-skip": routes_state.handle_series_skip,
    "/api/placement-skip": routes_placement.handle_placement_skip,
    "/api/compress/skip": routes_compress.handle_compress_skip,
    "/api/compress/cancel": routes_compress.handle_compress_cancel,
    "/api/apply-compress": routes_compress_apply.handle_apply_compress,
    "/api/apply-compress-remove": routes_compress_apply.handle_apply_compress_remove,
    "/api/compress/calibrate": routes_compress_apply.handle_compress_calibrate,
}
