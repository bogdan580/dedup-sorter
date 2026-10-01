"""Routes for exact-duplicate groups and per-file actions: thumbnails, open, apply."""
from __future__ import annotations

import json
import os
from http import HTTPStatus

from tidy import config
from tidy.moves import applier
from tidy.duplicates import report
from tidy.review.computations import _invalidate_batch_cache, _register_batch
from tidy.review.validation import _find_already_applied_group_id, _validate_selections


def serve_thumb(handler, state, thumb_id: str) -> None:
    source_path = state.thumb_sources.get(thumb_id)
    if source_path is None:
        handler._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown thumbnail id"})
        return
    out_path = state.thumbs.ensure(source_path, thumb_id)
    if out_path is None:
        handler._send_json(HTTPStatus.NOT_FOUND, {"error": "thumbnail unavailable"})
        return
    try:
        with open(out_path, "rb") as f:
            body = f.read()
    except OSError as exc:
        handler._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        return
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "image/jpeg")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_thumb(handler, state, parts) -> None:
    serve_thumb(handler, state, parts.path[len("/api/thumb/"):])


def handle_rotate_thumb(handler, state, body: bytes) -> None:
# See: docs/technical/components/review-ui.md
    try:
        payload_in = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
        return

    thumb_id = payload_in.get("thumb_id") if isinstance(payload_in, dict) else None
    if not isinstance(thumb_id, str) or not thumb_id:
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "thumb_id is required"})
        return

    source_path = state.thumb_sources.get(thumb_id)
    if source_path is None:
        handler._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown thumbnail id"})
        return

# See: docs/technical/components/review-ui.md
    if state.thumbs.ensure(source_path, thumb_id) is None:
        handler._send_json(HTTPStatus.NOT_FOUND, {"error": "thumbnail unavailable"})
        return

    if not state.thumbs.rotate(thumb_id, 1):
        handler._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "rotate failed"})
        return
    handler._send_json(HTTPStatus.OK, {"rotated": thumb_id})


def handle_open(handler, state, body: bytes) -> None:
# See: docs/technical/components/review-ui.md
    try:
        payload_in = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
        return

    file_id = payload_in.get("file_id") if isinstance(payload_in, dict) else None
    if not isinstance(file_id, str) or not file_id:
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "file_id is required"})
        return

# See: docs/technical/components/review-ui.md
    entry = state.file_registry.get(file_id)
    if entry is None:
        entry = state.series_registry.get(file_id)
    if entry is None:
        entry = state.events_registry.get(file_id)
    if entry is None:
        entry = state.placement_registry.get(file_id)
    if entry is None:
        entry = state.compress_registry.get(file_id)
    if entry is None:
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": f"unknown id: {file_id!r}"})
        return

    ext = os.path.splitext(str(entry["path"]))[1].casefold()
    if ext not in config.PHOTO_EXTENSIONS_CASEFOLD | config.VIDEO_EXTENSIONS_CASEFOLD:
        # See: docs/technical/security.md
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "file type is not allowed to be opened"})
        return

    try:
# See: docs/technical/components/review-ui.md
        os.startfile(entry["path"])
    except OSError as exc:
        handler._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        return
    handler._send_json(HTTPStatus.OK, {"opened": file_id})


def handle_apply(handler, state, body: bytes) -> None:
# See: docs/technical/components/review-ui.md
    try:
        payload_in = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
        return

    selections = payload_in.get("selections") if isinstance(payload_in, dict) else None

    with state.lock:
# See: docs/technical/components/review-ui.md
        already_applied_group_id = _find_already_applied_group_id(
            selections, state.applied_group_ids,
        )
        if already_applied_group_id is not None:
            handler._send_json(HTTPStatus.CONFLICT, {
                "error": f"group {already_applied_group_id!r}: already applied",
            })
            return

        groups, decisions, validation_error = _validate_selections(
            selections, state.file_registry,
        )
        if validation_error is not None:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": validation_error})
            return

        result = applier.apply_batch(
            state.fs, state.archive_root, groups, decisions,
        )
# See: docs/technical/components/review-ui.md
        batch_group_ids = sorted({s["group_id"] for s in selections})
        payload = {
            "batch_id": _register_batch(state, result.manifest_path),
            "kind": "dup",
            "scan_generation": state.scan_generation,
            "moved": result.moved,
            "failed": result.failed,
            "batch_dir": report.display_path(result.batch_dir),
            "manifest_path": report.display_path(result.manifest_path),
            "group_ids": batch_group_ids,
        }
        state.applied_group_ids.update(batch_group_ids)
        state.applied_batches.append(payload)
    _invalidate_batch_cache(state)
    handler._send_json(HTTPStatus.OK, payload)
