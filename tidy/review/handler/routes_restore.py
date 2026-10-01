"""Routes for the "Restore" tab: batch inventory, per-batch entries, restore."""
from __future__ import annotations

import json
import urllib.parse
from http import HTTPStatus

from tidy.moves import applier
from tidy.moves import batches
from tidy.duplicates import report
from tidy.review.computations import _invalidate_batch_cache, _invalidate_dir_index, _register_batch
from tidy.review.views.batches import build_batch_entries_view


def handle_batches(handler, state, parts) -> None:
# See: docs/technical/components/review-ui.md
    with state.placement_lock:
        cached = state.batch_list_cache
    if cached is not None:
        handler._send_json(HTTPStatus.OK, {"batches": cached})
        return

    summaries = batches.list_batches(state.fs, state.archive_root)
    out = [
        {
            "batch_id": _register_batch(state, b.manifest_path),
            "kind": b.kind,
            "dir_name": b.dir_name,
            "done": b.done,
            "failed": b.failed,
            "restorable": b.restorable,
            "already_restored": b.already_restored,
            "error": b.error,
        }
        for b in summaries
    ]
    with state.placement_lock:
        state.batch_list_cache = out
    handler._send_json(HTTPStatus.OK, {"batches": out})


def handle_batch_entries_route(handler, state, parts) -> None:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    batch_id_values = query.get("batch_id") or [""]
    handle_batch_entries(handler, state, batch_id_values[0])


def handle_batch_entries(handler, state, batch_id: str) -> None:
# See: docs/technical/components/review-ui.md
    with state.placement_lock:
        manifest_path = state.batch_registry.get(batch_id)
    if manifest_path is None:
        handler._send_json(HTTPStatus.BAD_REQUEST, {
            "error": f"unknown batch_id: {batch_id!r}",
        })
        return

    try:
        entries = batches.list_batch_entries(state.fs, manifest_path, state.archive_root)
    except (OSError, PermissionError, IndexError, ValueError) as exc:
# See: docs/technical/components/review-ui.md
        handler._send_json(HTTPStatus.OK, {"entries": [], "error": str(exc)})
        return

    views, thumb_sources = build_batch_entries_view(entries)
    with state.placement_lock:
# See: docs/technical/components/review-ui.md
        state.thumb_sources.update(thumb_sources)
    handler._send_json(HTTPStatus.OK, {"entries": views})


def handle_restore(handler, state, body: bytes) -> None:
# See: docs/technical/components/review-ui.md
    try:
        payload_in = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
        return

    batch_id = payload_in.get("batch_id") if isinstance(payload_in, dict) else None
    if not isinstance(batch_id, str) or not batch_id:
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "batch_id is required"})
        return

    with state.placement_lock:
        manifest_path = state.batch_registry.get(batch_id)
    if manifest_path is None:
        handler._send_json(HTTPStatus.BAD_REQUEST, {
            "error": f"unknown batch_id: {batch_id!r}",
        })
        return

    with state.lock:
# See: docs/technical/components/review-ui.md
        result = applier.restore_batch(state.fs, manifest_path, archive_root=state.archive_root)

        applied_entry = next(
            (b for b in state.applied_batches if b.get("batch_id") == batch_id),
            None,
        )
        if (
            applied_entry is not None
            and applied_entry.get("scan_generation") == state.scan_generation
        ):
            target_set = {
                "dup": state.applied_group_ids,
                "series": state.applied_series_group_ids,
                "placement": state.applied_placement_group_ids,
# See: docs/technical/components/review-ui.md
                "compress": state.applied_compress_group_ids,
            }.get(applied_entry.get("kind"))
            if target_set is not None:
                target_set.difference_update(applied_entry.get("group_ids", ()))

        payload = {
            "batch_id": batch_id,
            "restored": result.restored,
# See: docs/technical/components/review-ui.md
            "refused": result.refused,
            "entries": [
                {
                    "path": report.display_path(e.src_abs),
                    "status": e.status,
                    "detail": e.detail,
                }
                for e in result.entries
            ],
        }
# See: docs/technical/components/review-ui.md
    _invalidate_dir_index(state)
    _invalidate_batch_cache(state)
    handler._send_json(HTTPStatus.OK, payload)
