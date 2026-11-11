"""Routes for the placement tab: source directories, suggestions, skip, apply (op=move)."""
from __future__ import annotations

import json
import urllib.parse
from http import HTTPStatus

from tidy.moves import applier
from tidy.moves import manifest
from tidy.duplicates import report
from tidy.review.computations import (
    DEFAULT_PLACEMENT_SOURCE,
    _dir_index,
    _invalidate_batch_cache,
    _invalidate_dir_index,
    _placement_slots,
    _register_batch,
)
from tidy.review.skips import _set_placement_skip, _skipped_placement_group_ids
from tidy.review.validation import (
    _find_already_applied_group_id,
    _placement_removals,
    _validate_placement_selections,
    sanitize_dest_folder,
)
from tidy.review.workers.placement import _run_placement_computation


def handle_source_dirs(handler, state, parts) -> None:
    handler._send_json(HTTPStatus.OK, {
        "default": DEFAULT_PLACEMENT_SOURCE,
        "dirs": _dir_index(state),
    })


def handle_placement_route(handler, state, parts) -> None:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    source_values = query.get("source") or [DEFAULT_PLACEMENT_SOURCE]
    handle_placement(handler, state, source_values[0])


def handle_placement(handler, state, source_raw: str) -> None:
    """GET /api/placement?source=<rel>. Two independent gates on the
    source directory, since it is a directory this server takes
    from the client (docs/technical/security.md):

      1. sanitize_dest_folder() -- the same syntactic rules the
         destination goes through: relative, no `..`, no drive
         letter, no Windows-illegal names.
      2. membership in the server-built directory index -- the
         client is choosing FROM a list, not naming a path, so
         anything not on that list is rejected outright regardless
         of how well-formed it looks.

    Gate 2 alone would be enough for traversal; gate 1 is kept so a
    malformed value fails with a precise message instead of a flat
    "unknown directory", and so the two screens' path handling can
    never drift apart."""
    source_rel, error = sanitize_dest_folder(source_raw)
    if error is not None:
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": f"source: {error}"})
        return
    known = {entry["rel"].casefold() for entry in _dir_index(state)}
    if source_rel.casefold() not in known:
        handler._send_json(HTTPStatus.BAD_REQUEST, {
            "error": f"unknown source directory: {source_rel!r}",
        })
        return

    computation, progress = _placement_slots(state, source_rel)
    computation.start_if_idle(
        lambda: _run_placement_computation(state, source_rel)
    )
    snapshot = computation.snapshot()
    result = snapshot["result"] or {}
# See: docs/technical/components/review-ui.md
    skipped_ids = _skipped_placement_group_ids(state)
    folders = [
        {
            **folder,
            "suggestions": [
                {
                    **s,
                    "applied": s["group_id"] in state.applied_placement_group_ids,
                    "skipped": s["group_id"] in skipped_ids,
                }
                for s in folder["suggestions"]
            ],
        }
        for folder in result.get("folders", [])
    ]
    done, total = progress.snapshot()
    handler._send_json(HTTPStatus.OK, {
        "status": snapshot["status"],
        "source": source_rel,
        "folders": folders,
        "error": snapshot["error"],
        "notes": result.get("notes", {}),
        "progress": {"done": done, "total": total},
    })


def handle_placement_skip(handler, state, body: bytes) -> None:
# See: docs/technical/components/review-ui.md
    try:
        payload_in = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
        return

    if not isinstance(payload_in, dict):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "body must be an object"})
        return

    group_id = payload_in.get("group_id")
    skip = payload_in.get("skip")
    if not isinstance(group_id, str) or not group_id:
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "group_id is required"})
        return
    if not isinstance(skip, bool):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "skip must be true or false"})
        return

    payload_out, error = _set_placement_skip(state, group_id, skip)
    if error is not None:
        status = (
            HTTPStatus.BAD_REQUEST if error.startswith("unknown")
            else HTTPStatus.INTERNAL_SERVER_ERROR
        )
        handler._send_json(status, {"error": error})
        return
    handler._send_json(HTTPStatus.OK, payload_out)


def handle_apply_placement(handler, state, body: bytes) -> None:
# See: docs/technical/components/review-ui.md
    try:
        payload_in = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
        return

    selections = payload_in.get("selections") if isinstance(payload_in, dict) else None

# See: docs/technical/components/review-ui.md
    with state.lock:
        already_applied_group_id = _find_already_applied_group_id(
            selections, state.applied_placement_group_ids,
        )
        if already_applied_group_id is not None:
            handler._send_json(HTTPStatus.CONFLICT, {
                "error": f"group {already_applied_group_id!r}: already applied",
            })
            return

        plans, validation_error = _validate_placement_selections(
            selections, state.placement_registry, state.archive_root,
        )
        if validation_error is not None:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": validation_error})
            return

        removal_groups, removal_decisions = _placement_removals(
            selections, state.placement_registry,
        )
        batches = []
        if plans:
            move_result = applier.apply_moves(state.fs, state.archive_root, plans)
            batches.append(_batch_payload(state, move_result, sorted({
                s["group_id"] for s in selections if s.get("file_ids")
            })))
        if removal_decisions:
            remove_result = applier.apply_batch(
                state.fs, state.archive_root, removal_groups, removal_decisions,
            )
            batches.append(_batch_payload(state, remove_result, sorted({
                s["group_id"] for s in selections if s.get("remove_ids")
            })))
        batch_group_ids = sorted({s["group_id"] for s in selections})
        # One response, up to two batches (op=move, op=remove); the second
        # rides along as `remove_batch` so each keeps its own restore id.
        payload = dict(batches[0])
        if len(batches) > 1:
            payload["remove_batch"] = batches[1]
        state.applied_placement_group_ids.update(batch_group_ids)
        state.applied_batches.extend(batches)
# See: docs/technical/components/review-ui.md
    _invalidate_dir_index(state)
    _invalidate_batch_cache(state)
    handler._send_json(HTTPStatus.OK, payload)


def _batch_payload(state, result, group_ids) -> dict:
    return {
        "batch_id": _register_batch(state, result.manifest_path),
        "kind": "placement",
        "scan_generation": state.scan_generation,
        "moved": result.moved,
        "failed": result.failed,
        "batch_dir": report.display_path(result.batch_dir),
        "manifest_path": report.display_path(result.manifest_path),
        "group_ids": group_ids,
# See: docs/technical/components/review-ui.md
        "errors": [
            {"path": report.display_path(e.src_abs), "error": e.error}
            for e in result.entries
            if e.status == manifest.STATUS_FAILED
        ],
    }
