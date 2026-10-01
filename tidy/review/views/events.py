"""View-model builders for the events screen."""
from __future__ import annotations

from typing import Dict, List, Mapping, Tuple

from tidy.analysis import events
from tidy.duplicates import report
from tidy.media import thumbnails
from tidy.review.views.duplicates import thumb_id_for_path


def build_events_views(
    result: "events.EventsResult",
    sizes_by_path: Mapping[str, int],
) -> Tuple[List[dict], Dict[str, str], Dict[str, dict]]:
    """Build JSON-ready view models for the "Grouping suggestions" screen
    from an events.EventsResult, plus the same kind of thumb_sources/
    events_registry pair build_series_views() above produces for series --
    except events_registry is read ONLY by POST /api/open, never by any
    apply endpoint: events are read-only by construction, since op=move for
    whole-event suggestions does not exist for this registry.

    `sizes_by_path`: events.EventMember has no "size" field of its own, but
    the view model must carry one for every card like the other two screens
    do -- the caller (a later step) is expected to pass a path->size map
    sourced from ScannedFile.stat.size for every scanned candidate. A path
    missing from that map (should not happen for a real caller, but this
    function must not raise over it) falls back to 0 rather than raising,
    since a wrong-but-present size is a much smaller problem for a review
    screen than a crashed request.

    `when` is serialized to an ISO 8601 string (not the raw datetime
    EventMember carries) so the returned dict is JSON-serializable as-is,
    same requirement/reasoning as the rest of this module's view models."""
    views: List[dict] = []
    thumb_sources: Dict[str, str] = {}
    events_registry: Dict[str, dict] = {}

    for idx, event in enumerate(result.events, start=1):
        group_id = f"e{idx}"
        entries: List[dict] = []

        for i, member in enumerate(event.members):
            file_id = f"{group_id}:{i}"
            size = sizes_by_path.get(member.path, 0)

            thumb_id = thumb_id_for_path(member.path) if thumbnails.supports(member.path) else None
            if thumb_id is not None:
                thumb_sources[thumb_id] = member.path
            thumb_kind = thumbnails.kind(member.path)

            entries.append({
                "path": report.display_path(member.path),
                "size": size,
                "id": file_id,
                "thumb_id": thumb_id,
                "thumb_kind": thumb_kind,
                "when": member.when.isoformat(),
                "timestamp_source": member.timestamp_source,
                "camera_model": member.camera_model,
                "gps_source": member.gps_source,
                "is_suspicious": member.is_suspicious,
            })
            events_registry[file_id] = {
                "path": member.path, "group_id": group_id, "size": size,
            }

        views.append({
            "group_id": group_id,
            "suggested_folder": event.suggested_folder,
            "matched_existing_dir": event.matched_existing_dir,
            "place_name": event.place_name,
            "country_cc": event.country_cc,
            "photo_count": event.photo_count,
            "video_count": event.video_count,
            "entries": entries,
        })

    return views, thumb_sources, events_registry


def build_events_notes(result: "events.EventsResult") -> dict:
    """Split out from build_events_views() rather than folded into its
    return tuple: these are warnings about camera-clock drift and
    timestamps too unreliable to place in any event at all, not part of
    the group-shaped view models the UI grid renders -- but they still
    MUST reach the UI, just as a separate, simpler payload."""
    return {
        "suspicious": [
            {
                "path": report.display_path(member.path),
                "when": member.when.isoformat(),
                "camera_model": member.camera_model,
            }
            for member in result.suspicious
        ],
        "camera_drift_warnings": list(result.camera_drift_warnings),
    }
