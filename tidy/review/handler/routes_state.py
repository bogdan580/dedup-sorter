"""Routes for the top-level views: index, state, series, events, rescan, and the shared lazy-computation responder."""
from __future__ import annotations

import json
from http import HTTPStatus
from typing import Callable, Optional, Set

from tidy.moves import applier
from tidy.duplicates import report
from tidy.progress import ProgressCounter
from tidy.review.computations import (
    _invalidate_batch_cache,
    _parse_series_params,
    _register_batch,
    _run_events_computation,
    _run_series_computation,
)
from tidy.review.lazy import _LazyComputation
from tidy.review.page import INDEX_HTML
from tidy.review.locales import LOCALES
from tidy.review.skips import _set_series_skip, _skipped_series_group_ids
from tidy.review.state import ReviewState
from tidy.review.validation import _find_already_applied_group_id, _validate_series_selections
from tidy.review.workers.rescan import _run_rescan


def handle_index(handler, state, parts) -> None:
    lang = "en"
    if hasattr(state, "compress_config_path") and state.compress_config_path:
        import os
        if os.path.exists(state.compress_config_path):
            try:
                with open(state.compress_config_path, "r", encoding="utf-8") as _f:
                    _cfg = json.load(_f)
                    lang = _cfg.get("ui_language", "en")
            except Exception:
                pass
    if lang not in LOCALES:
        lang = "en"
    i18n_str = json.dumps(LOCALES[lang])
    
    html = INDEX_HTML.replace("__DEDUP_TOKEN__", state.token)
    html = html.replace("__DEDUP_I18N__", i18n_str)
    handler._send_html(HTTPStatus.OK, html)


def handle_state(handler, state, parts) -> None:
    handler._send_json(HTTPStatus.OK, state.to_json())


def handle_lazy(
    handler,
    state: ReviewState,
    lazy: _LazyComputation,
    worker_fn: Callable[["ReviewState"], None],
    *,
    applied_ids: Optional[Set[str]] = None,
    progress: Optional[ProgressCounter] = None,
    skipped_ids: Optional[Set[str]] = None,
) -> None:
# See: docs/technical/components/review-ui.md
    lazy.start_if_idle(lambda: worker_fn(state))
    snapshot = lazy.snapshot()
    result = snapshot["result"] or {}
    groups = result.get("groups", [])
    if applied_ids is not None:
        groups = [{**g, "applied": g["group_id"] in applied_ids} for g in groups]
# See: docs/technical/components/review-ui.md
    if skipped_ids is not None:
        groups = [{**g, "skipped": g["group_id"] in skipped_ids} for g in groups]
    done, total = (progress.snapshot() if progress is not None else (0, 0))
    handler._send_json(HTTPStatus.OK, {
        "status": snapshot["status"],
        "groups": groups,
        "error": snapshot["error"],
        "notes": result.get("notes", {}),
        "progress": {"done": done, "total": total},
    })


def handle_series(handler, state, parts) -> None:
# See: docs/technical/components/review-ui.md
    handle_lazy(handler, state, 
        state.series_computation, _run_series_computation,
        applied_ids=state.applied_series_group_ids,
        progress=state.series_progress,
        skipped_ids=_skipped_series_group_ids(state),
    )


def handle_events(handler, state, parts) -> None:
# See: docs/technical/components/review-ui.md
    handle_lazy(handler, state, 
        state.events_computation, _run_events_computation,
        progress=state.events_progress,
    )


def handle_series_skip(handler, state, body: bytes) -> None:
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

    payload_out, error = _set_series_skip(state, group_id, skip)
    if error is not None:
# See: docs/technical/components/review-ui.md
        status = (
            HTTPStatus.BAD_REQUEST if error.startswith("unknown")
            else HTTPStatus.INTERNAL_SERVER_ERROR
        )
        handler._send_json(status, {"error": error})
        return
    handler._send_json(HTTPStatus.OK, payload_out)


def handle_rescan(handler, state, body: bytes) -> None:
# See: docs/technical/components/review-ui.md
    if state.rescan is None:
        handler._send_json(HTTPStatus.BAD_REQUEST, {
            "error": "rescan not available in this session",
        })
        return
    try:
        payload_in = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
        return
    if not isinstance(payload_in, dict):
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "body must be an object"})
        return
# See: docs/technical/components/review-ui.md
    series_params, params_error = _parse_series_params(payload_in)
    if params_error is not None:
        handler._send_json(HTTPStatus.BAD_REQUEST, {"error": params_error})
        return
    try:
        summary = _run_rescan(state, series_params)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        handler._send_json(
            HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)},
        )
        return
    handler._send_json(HTTPStatus.OK, summary)


def handle_apply_series(handler, state, body: bytes) -> None:
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
            selections, state.applied_series_group_ids,
        )
        if already_applied_group_id is not None:
            handler._send_json(HTTPStatus.CONFLICT, {
                "error": f"group {already_applied_group_id!r}: already applied",
            })
            return

        groups, decisions, validation_error = _validate_series_selections(
            selections, state.series_registry,
        )
        if validation_error is not None:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": validation_error})
            return

        result = applier.apply_batch(
            state.fs, state.archive_root, groups, decisions,
        )
        batch_group_ids = sorted({s["group_id"] for s in selections})
        payload = {
            "batch_id": _register_batch(state, result.manifest_path),
            "kind": "series",
            "scan_generation": state.scan_generation,
            "moved": result.moved,
            "failed": result.failed,
            "batch_dir": report.display_path(result.batch_dir),
            "manifest_path": report.display_path(result.manifest_path),
            "group_ids": batch_group_ids,
        }
        state.applied_series_group_ids.update(batch_group_ids)
        state.applied_batches.append(payload)
    _invalidate_batch_cache(state)
    handler._send_json(HTTPStatus.OK, payload)
