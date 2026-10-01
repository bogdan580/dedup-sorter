"""Routes for the compress tab, write side: calibrate, encode queue, discard verified originals."""
from __future__ import annotations

import json
import threading
from http import HTTPStatus

from tidy.moves import applier
from tidy.moves import manifest
from tidy.compression import compress
from tidy.duplicates import report
from tidy.store.cache import Cache
from tidy.progress import ProgressCounter
from tidy.review.computations import _hwdetect_for, _invalidate_batch_cache, _register_batch
from tidy.review.state import CalibrationJob, CompressJob
from tidy.review.validation import (
    _find_already_applied_group_id,
    _validate_compress_remove_selections,
    _validate_compress_selections,
)
from tidy.review.workers.calibration import _run_calibration_job
from tidy.review.workers.compress_queue import _run_compress_queue


def handle_compress_calibrate(handler, state, body: bytes) -> None:
# See: docs/technical/components/review-ui.md
    try:
        payload_in = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
        return
    if not isinstance(payload_in, dict):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "body must be an object"})
        return

    file_id = payload_in.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "file_id is required"})
        return
    # compress_registry ONLY -- same "ids, never a client path"
    # allowlist rule as every other apply endpoint in this module.
    entry = state.compress_registry.get(file_id)
    if entry is None:
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": f"unknown id: {file_id!r}"})
        return

    hw = _hwdetect_for(state)
    if hw is None:
        handler._send_json(HTTPStatus.BAD_REQUEST, {
            "error": (
                "ffmpeg not available on this machine: "
                + state.hwdetect_block_reason
                if state.hwdetect_block_reason
                else "ffmpeg not available on this machine"
            ),
        })
        return

    device_keys_in = payload_in.get("device_keys")
    if device_keys_in is not None:
        if not isinstance(device_keys_in, list) or not all(
            isinstance(k, str) for k in device_keys_in
        ):
            handler._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "device_keys must be a list of strings",
            })
            return
        known_keys = {e.device_key for e in hw.encoders}
        unknown = [k for k in device_keys_in if k not in known_keys]
        if unknown:
            handler._send_json(HTTPStatus.BAD_REQUEST, {
                "error": f"unknown device_key(s): {unknown!r}",
            })
            return
        device_keys = list(device_keys_in)
    else:
        cache = Cache(state.db_path) if state.db_path else None
        try:
            calibration = cache.get_calibration(hw.machine_id) if cache is not None else {}
        finally:
            if cache is not None:
                cache.close()
        device_keys = [
            e.device_key for e in hw.encoders if e.device_key not in calibration
        ]

    if not device_keys:
        handler._send_json(HTTPStatus.OK, {"started": False, "reason": "nothing to measure"})
        return

    with state.lock:
        with state.compress_queue_lock:
            if state.compress_queue_running:
                handler._send_json(HTTPStatus.CONFLICT, {
                    "error": "a compress queue is already running",
                })
                return
            job = CalibrationJob(
                file_id=file_id, path=entry["path"], size=entry["size"],
                device_keys=device_keys,
            )
            state.calibration_job = job
            # A fresh Event, never .clear()'d -- same reasoning as
            # POST /api/apply-compress's own queue start.
            state.compress_cancel_event = threading.Event()
            state.compress_queue_running = True

    thread = threading.Thread(
        target=_run_calibration_job, args=(state, job), daemon=True,
    )
    thread.start()
    handler._send_json(HTTPStatus.OK, {"started": True, "device_keys": device_keys})


def handle_apply_compress(handler, state, body: bytes) -> None:
# See: docs/technical/components/review-ui.md
    try:
        payload_in = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
        return
    if not isinstance(payload_in, dict):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "body must be an object"})
        return

    selections = payload_in.get("selections")
    profile_name = payload_in.get("profile")
    remove_originals = payload_in.get("remove_originals", False)
    # Opt-in per-apply relaxation of the XPSNR safety margin: still an
    # enforced threshold (cfg.min_xpsnr_relaxed), never skipped.
    # See: docs/business/rules/compression.md
    allow_lower_quality = payload_in.get("allow_lower_quality", False)

    if not isinstance(profile_name, str) or not profile_name:
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "profile is required"})
        return
    if not isinstance(remove_originals, bool):
        handler._send_json(HTTPStatus.BAD_REQUEST, {
            "error": "remove_originals must be true or false",
        })
        return
    if not isinstance(allow_lower_quality, bool):
        handler._send_json(HTTPStatus.BAD_REQUEST, {
            "error": "allow_lower_quality must be true or false",
        })
        return

    cfg = compress.load_compress_config(state.compress_config_path)
    profile = cfg.profiles.get(profile_name)
    if profile is None:
        handler._send_json(HTTPStatus.BAD_REQUEST, {
            "error": f"unknown profile: {profile_name!r}",
        })
        return

    device_key_in = payload_in.get("device_key")
    if device_key_in is not None and not isinstance(device_key_in, str):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "device_key must be a string"})
        return

    hw = _hwdetect_for(state)
    hw_encoders = hw.encoders if hw is not None else ()
# See: docs/technical/components/review-ui.md
    device_ok, device_entry, device_error = compress.resolve_device(
        hw_encoders, profile["encoder"], device_key_in,
    )
    if not device_ok:
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": device_error})
        return

# See: docs/technical/components/review-ui.md
    with state.lock:
        with state.compress_queue_lock:
            if state.compress_queue_running:
                handler._send_json(HTTPStatus.CONFLICT, {
                    "error": "a compress queue is already running",
                })
                return

        already_applied_group_id = _find_already_applied_group_id(
            selections, state.applied_compress_group_ids,
        )
        if already_applied_group_id is not None:
            handler._send_json(HTTPStatus.CONFLICT, {
                "error": f"group {already_applied_group_id!r}: already applied",
            })
            return

        entries, validation_error = _validate_compress_selections(
            selections, state.compress_registry,
        )
        if validation_error is not None:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": validation_error})
            return

        job_device = device_entry.device if device_entry is not None else ""
        job_device_key = device_entry.device_key if device_entry is not None else ""
        jobs = [
            CompressJob(
                file_id=e["file_id"], group_id=e["group_id"],
                path=e["path"], size=e["size"], profile_name=profile_name,
                device=job_device, device_key=job_device_key,
            )
            for e in entries
        ]
        selected_group_ids = sorted({e["group_id"] for e in entries})

        with state.compress_queue_lock:
            state.compress_jobs = jobs
            state.compress_queue_progress = ProgressCounter()
# See: docs/technical/components/review-ui.md
            state.compress_cancel_event = threading.Event()
            state.compress_queue_running = True
        state.applied_compress_group_ids.update(selected_group_ids)

    thread = threading.Thread(
        target=_run_compress_queue,
        args=(state, jobs, profile_name, remove_originals),
        kwargs={"allow_lower_quality": allow_lower_quality},
        daemon=True,
    )
    thread.start()
    handler._send_json(HTTPStatus.OK, {"queued": len(jobs), "profile": profile_name})


def handle_apply_compress_remove(handler, state, body: bytes) -> None:
# See: docs/technical/components/review-ui.md
    try:
        payload_in = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
        return
    selections = payload_in.get("selections") if isinstance(payload_in, dict) else None

    with state.lock:
        removals, validation_error = _validate_compress_remove_selections(
            selections, state.compress_registry,
        )
        if validation_error is not None:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": validation_error})
            return

        result = applier.apply_compress_removals(
            state.fs, state.archive_root, removals,
        )
        batch_group_ids = sorted({r.group_id for r in removals if r.group_id})
        payload = {
            "batch_id": _register_batch(state, result.manifest_path),
            "kind": "remove",
            "scan_generation": state.scan_generation,
            "moved": result.moved,
            "failed": result.failed,
            "batch_dir": report.display_path(result.batch_dir),
            "manifest_path": report.display_path(result.manifest_path),
            "group_ids": batch_group_ids,
            "errors": [
                {"path": report.display_path(e.src_abs), "error": e.error}
                for e in result.entries
                if e.status == manifest.STATUS_FAILED
            ],
        }
        state.applied_batches.append(payload)
    _invalidate_batch_cache(state)
    handler._send_json(HTTPStatus.OK, payload)
