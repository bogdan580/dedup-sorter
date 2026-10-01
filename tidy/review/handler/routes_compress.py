"""Routes for the compress tab, read side: candidates, encoders, jobs, skip, cancel."""
from __future__ import annotations

import json
import time
import urllib.parse
from http import HTTPStatus
from typing import Dict

from tidy.duplicates import report
from tidy.store.cache import Cache
from tidy.review.computations import (
    DEFAULT_COMPRESS_SOURCE,
    _compress_slots,
    _dir_index,
    _hwdetect_for,
)
from tidy.review.skips import _set_compress_skip, _skipped_compress_group_ids
from tidy.review.validation import sanitize_dest_folder
from tidy.review.workers.compress_queue import _compress_removable_candidates, _run_compress_computation


def handle_compress_route(handler, state, parts) -> None:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    source_values = query.get("source") or [DEFAULT_COMPRESS_SOURCE]
    handle_compress(handler, state, source_values[0])


def handle_compress(handler, state, source_raw: str) -> None:
    """GET /api/compress?source=<rel>. Same two gates as
    handle_placement() above, on the same kind of client-supplied
    directory (docs/technical/security.md):

      1. sanitize_dest_folder() -- syntax only.
      2. membership in the server-built directory index -- the
         client is choosing FROM a list, not naming a path.

    Gate 2 alone stops traversal; gate 1 stays so a malformed value
    fails with a precise message and the two screens' path handling
    never drifts apart (see handle_placement()'s own comment)."""
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

    computation, progress = _compress_slots(state, source_rel)
    computation.start_if_idle(
        lambda: _run_compress_computation(state, source_rel)
    )
    snapshot = computation.snapshot()
    result = snapshot["result"] or {}
# See: docs/technical/components/review-ui.md
    skipped_ids = _skipped_compress_group_ids(state)
    candidates = [
        {
            **c,
            "applied": c["group_id"] in state.applied_compress_group_ids,
            "skipped": c["group_id"] in skipped_ids,
        }
        for c in result.get("candidates", [])
    ]
    done, total = progress.snapshot()
    handler._send_json(HTTPStatus.OK, {
        "status": snapshot["status"],
        "source": source_rel,
        "candidates": candidates,
        "skipped": result.get("skipped", []),
        "error": snapshot["error"],
        "unavailable_reason": result.get("unavailable_reason"),
        "progress": {"done": done, "total": total},
    })


def handle_compress_encoders(handler, state, parts) -> None:
    """GET /api/compress/encoders. hwdetect.detect() is memoised on
    `state` by _hwdetect_for() (its own docstring explains why:
    hardware-encoder probing is seconds of subprocess work that
    must not repeat per request). A None result means ffmpeg is not
    resolvable at all -- degrades to "unavailable", never an error
    response, per this module's "missing optional dependency
    degrades the feature, not the tool" contract (CLAUDE.md)."""
    hw = _hwdetect_for(state)
    if hw is None:
        handler._send_json(HTTPStatus.OK, {
            "available": False, "encoders": [],
            "machine_id": None, "ffmpeg_version": None,
            "unavailable_reason": state.hwdetect_block_reason,
        })
        return
# See: docs/technical/components/review-ui.md
    cache = Cache(state.db_path) if state.db_path else None
    calibration: Dict[str, dict] = {}
    if cache is not None:
        try:
            calibration = cache.get_calibration(hw.machine_id)
        finally:
            cache.close()
    encoders = [
        {
            "name": e.name,
            "display_name": e.display_name,
            "device": e.device,
            "device_key": e.device_key,
            "is_hardware": e.is_hardware,
            "calibrated": e.device_key in calibration,
        }
        for e in hw.encoders
    ]
    handler._send_json(HTTPStatus.OK, {
        "available": True,
        "encoders": encoders,
        "machine_id": hw.machine_id,
        "ffmpeg_version": hw.ffmpeg_version,
        "unavailable_reason": None,
    })


def handle_compress_jobs(handler, state, parts) -> None:
    """GET /api/compress/jobs. Read-only, no mutating-request gate
    (mirrors GET /api/batches) -- two sections in one response
    (docs/technical/components/review-ui.md): the live encode
    queue's progress (session-only, from state.compress_jobs), and
    the disk-sourced "these originals can now be discarded" list
    (_compress_removable_candidates(), which survives a restart).
    """
    with state.compress_queue_lock:
        running = state.compress_queue_running
        jobs_snapshot = list(state.compress_jobs)
        calibration_job = state.calibration_job
    now = time.time()
    jobs_json = [
        {
            "file_id": job.file_id,
            "group_id": job.group_id,
            "path": report.display_path(job.path),
            "status": job.status,
            "error": job.error,
            "original_size": job.size,
            "result_size": job.result_size,
            "xpsnr": job.xpsnr,
            "profile_name": job.profile_name,
# See: docs/technical/components/review-ui.md
            "elapsed_seconds": (
                round((job.finished_at or now) - job.started_at, 1)
                if job.started_at is not None else 0
            ),
        }
        for job in jobs_snapshot
    ]
    done, total = state.compress_queue_progress.snapshot()
    calibration_json = None
    if calibration_job is not None:
        calibration_json = {
            "file_id": calibration_job.file_id,
            "status": calibration_job.status,
            "current_device_key": calibration_job.current_device_key,
            "device_keys": calibration_job.device_keys,
            "measured": calibration_job.measured,
            "error": calibration_job.error,
        }
    handler._send_json(HTTPStatus.OK, {
        "running": running,
        "jobs": jobs_json,
        "progress": {"done": done, "total": total},
        "removable": _compress_removable_candidates(state),
        "calibration": calibration_json,
    })


def handle_compress_skip(handler, state, body: bytes) -> None:
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

    payload_out, error = _set_compress_skip(state, group_id, skip)
    if error is not None:
        status = (
            HTTPStatus.BAD_REQUEST if error.startswith("unknown")
            else HTTPStatus.INTERNAL_SERVER_ERROR
        )
        handler._send_json(status, {"error": error})
        return
    handler._send_json(HTTPStatus.OK, payload_out)


def handle_compress_cancel(handler, state, body: bytes) -> None:
# See: docs/technical/components/review-ui.md
    with state.compress_queue_lock:
        running = state.compress_queue_running
        if running:
            state.compress_cancel_event.set()
    handler._send_json(HTTPStatus.OK, {"cancelled": running})
