"""View-model builder for the series screen."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from tidy.duplicates import report
from tidy.analysis import series
from tidy.media import thumbnails
from tidy.duplicates.keeper import REASON_DUP_VISUAL
from tidy.review.views.duplicates import thumb_id_for_path


# See: docs/technical/components/review-ui.md

def build_series_views(
    result: "series.SeriesResult",
) -> Tuple[List[dict], Dict[str, str], Dict[str, dict]]:
    """Build JSON-ready view models for the "Series and similar" screen from a
    series.SeriesResult, plus the same kind of server-side allowlists
    build_group_views() produces (see its docstring): thumb_id ->
    source_path, and a series_registry (file_id -> {"path", "group_id",
    "size", "hash", "reason"}) trusted by BOTH POST /api/apply-series and
    POST /api/open -- unlike build_events_views()'s events_registry below,
    which POST /api/open alone may read.

    Every file entry carries the SAME core fields as build_group_views()'s
    keeper/discarded entries (path/size/id/thumb_id/thumb_kind) so the
    front-end can render both with the same existing makeCard() -- plus
    series-specific fields (width/height/sharpness/has_exif) the exact-
    duplicate screen has no use for.

    `hash` is always "" (series members are never byte-hashed -- dHash is a
    perceptual fingerprint, not a byte hash) and `reason` is always
    REASON_DUP_VISUAL: apply_batch()'s per-discard size/hash override reads
    both straight from this registry via _validate_series_selections(), so
    every manifest row for a series discard carries its OWN size, never a
    shared group-level value a series group doesn't actually have."""
    views: List[dict] = []
    thumb_sources: Dict[str, str] = {}
    series_registry: Dict[str, dict] = {}

    for idx, group in enumerate(result.groups, start=1):
        group_id = f"s{idx}"
        entries: List[dict] = []
        suggested_keeper_id: Optional[str] = None

        for i, member in enumerate(group.members):
            file_id = f"{group_id}:{i}"

            thumb_id = thumb_id_for_path(member.path) if thumbnails.supports(member.path) else None
            if thumb_id is not None:
                thumb_sources[thumb_id] = member.path
            # thumb_kind is always computed, independent of supports() --
            # same reasoning as build_group_views() above.
            thumb_kind = thumbnails.kind(member.path)

            entries.append({
                "path": report.display_path(member.path),
                "size": member.size,
                "id": file_id,
                "thumb_id": thumb_id,
                "thumb_kind": thumb_kind,
                "width": member.width,
                "height": member.height,
                "sharpness": member.sharpness,
                "has_exif": member.has_exif,
            })
            series_registry[file_id] = {
                "path": member.path, "group_id": group_id, "size": member.size,
                "hash": "", "reason": REASON_DUP_VISUAL,
            }
            if member.path == group.suggested_keeper:
                suggested_keeper_id = file_id

        views.append({
            "group_id": group_id,
            "kind": group.kind,
            "max_gap_seconds": group.max_gap_seconds,
            "suggested_keeper_id": suggested_keeper_id,
            "entries": entries,
        })

    return views, thumb_sources, series_registry
