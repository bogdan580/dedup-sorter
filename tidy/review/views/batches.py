"""View-model builder for the per-batch entry listing."""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from tidy.moves import batches
from tidy.duplicates import report
from tidy.media import thumbnails
from tidy.review.views.duplicates import thumb_id_for_path


def build_batch_entries_view(
    entries: Sequence["batches.BatchEntry"],
) -> Tuple[List[dict], Dict[str, str]]:
    """View models for GET /api/batch-entries -- the "Restore" tab's lazy,
    per-batch thumbnail preview.

    Unlike every other build_*_views() above, this returns NO id registry:
    a batch entry backs no apply/open endpoint (restoring is whole-batch
    only, POST /api/restore, keyed by batch_id -- never a per-file id), so
    thumb_sources -- the one allowlist that always applies -- is the only
    thing this needs to populate.

    A NOT-restorable entry gets thumb_id=None unconditionally, even when
    thumbnails.supports() would say yes: batches.list_batch_entries()'s
    `restorable` already established that resolve_dst()'s path may not
    physically hold this file any more (or something already occupies
    where restore would put it back) -- registering a thumb_id anyway would
    let the browser request a thumbnail this function cannot vouch for.
    The front end already renders a placeholder for thumb_id=None
    (makeCard(), unchanged), so nothing is lost by refusing to guess."""
    views: List[dict] = []
    thumb_sources: Dict[str, str] = {}
    for entry in entries:
        thumb_id = None
        if entry.restorable and thumbnails.supports(entry.path):
            thumb_id = thumb_id_for_path(entry.path)
            thumb_sources[thumb_id] = entry.path
        views.append({
            "path": report.display_path(entry.path),
            "size": entry.size,
            "restorable": entry.restorable,
            "thumb_id": thumb_id,
            "thumb_kind": thumbnails.kind(entry.path),
        })
    return views, thumb_sources
