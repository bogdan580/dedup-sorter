"""Background compress candidate computation and the encode queue."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from tidy.moves import applier
from tidy.moves import batches
from tidy.compression import compress
from tidy import config
from tidy.compression import encoder
from tidy.moves import manifest
from tidy.compression import probe
from tidy.duplicates import report
from tidy.duplicates import scanner
from tidy.store.cache import Cache
from tidy.review.computations import (
    _compress_slots,
    _hwdetect_for,
    _invalidate_batch_cache,
    _note_tool_blocked,
    _register_batch,
)
from tidy.review.state import CompressJob, ReviewState
from tidy.review.validation import _join_path
from tidy.review.views.compress import build_compress_views


def _run_compress_computation(state: "ReviewState", source_rel: str) -> None:
    """Background worker for GET /api/compress. Same never-raise contract
    as every other lazy worker in this module (see
    _run_placement_computation()'s docstring): a daemon thread nobody
    joins, so every failure path goes through .fail() rather than leaving
    the UI on "calculating..." forever.

    Unlike the placement screen, the scan here is recursive with no depth
    limit (docs/technical/components/review-ui.md): _dir_index()'s picker stays depth-limited by
    list_archive_dirs(), but a chosen directory's own videos can be nested
    arbitrarily deep, and the whole point of picking a directory is to
    catch all of a trip's clips, not just the ones sitting flat inside it.

    Mirrors tidy/cli's own _compress_candidates(): scanner.scan() first
    (offline/hardlink-of files are skipped before ever probing -- probing
    an offline file would force a cloud download just to look at it), then
    probe.probe_file_cached()/compress.evaluate_file() per video file, then
    build_compress_views() over the finished candidate/skipped lists --
    build_compress_views() itself stays pure and calls neither."""
    computation, progress = _compress_slots(state, source_rel)
    try:
        source_abs = _join_path(state.archive_root, source_rel.replace("\\", "/"))

        hw = _hwdetect_for(state)
        if hw is None:
# See: docs/technical/components/review-ui.md
            reason = state.hwdetect_block_reason

            def _publish_unavailable() -> dict:
                return {
                    "candidates": [], "skipped": [], "encoders_available": False,
                    "unavailable_reason": reason,
                }
            computation.finish_ready(_publish_unavailable)
            return

        scan_result = scanner.scan(state.fs, [source_abs])
        video_files = [
            f for f in scan_result.files
            if f.exclusion_reason not in ("offline", "hardlink-of")
            and os.path.splitext(f.path)[1].casefold() in config.VIDEO_EXTENSIONS_CASEFOLD
        ]
        progress.set_total(len(video_files))

        cfg = compress.load_compress_config(state.compress_config_path)

# See: docs/technical/components/review-ui.md
        cache = Cache(state.db_path) if state.db_path else None
        try:
            candidates: List["compress.CompressCandidate"] = []
            skipped: List["compress.CompressSkipped"] = []
            for f in video_files:
                probe_result = probe.probe_file_cached(
                    hw.ffprobe_exe, f.path, f.stat.size, f.stat.mtime_ns, cache=cache,
                )
                candidate, skip = compress.evaluate_file(
                    f.path, f.stat.size, probe_result, state.fs,
                    bpp_thresholds=cfg.bpp_thresholds,
                )
                if candidate is not None:
                    candidates.append(candidate)
                else:
                    skipped.append(skip)
                progress.increment()
# See: docs/technical/components/review-ui.md
            calibration = cache.get_calibration(hw.machine_id) if cache is not None else {}
        finally:
            if cache is not None:
                cache.close()

        candidates_json, skipped_json, thumb_sources, compress_registry = build_compress_views(
            candidates, skipped, calibration, cfg,
# See: docs/technical/components/review-ui.md
            id_prefix=state.next_compress_id_prefix(),
            available_encoders={e.name for e in hw.encoders},
        )

        def _publish() -> dict:
            state.thumb_sources.update(thumb_sources)
            state.compress_registry.update(compress_registry)
            return {"candidates": candidates_json, "skipped": skipped_json}

        computation.finish_ready(_publish)
    except probe.ToolBlockedError as exc:
        reason = _note_tool_blocked(state, exc)
        computation.finish_ready(lambda: {
            "candidates": [], "skipped": [], "encoders_available": False,
            "unavailable_reason": reason,
        })
    except Exception as exc:  # noqa: BLE001 - must never escape a daemon thread
        computation.fail(str(exc))


# --- compress queue: POST /api/apply-compress's background worker ----------

def _record_queue_batch(
    state: "ReviewState", batch_index: Optional[int], kind: str,
    result: "applier.ApplyResult", group_ids: List[str],
) -> int:
    """Fold one job's ApplyResult into the queue's single applied_batches
    entry for `kind`, appending it on first use. The entry is replaced,
    never mutated in place: /api/state serializes a shallow copy of the list."""
    with state.lock:
        if batch_index is None:
            state.applied_batches.append({
                "batch_id": _register_batch(state, result.manifest_path),
                "kind": kind,
                "scan_generation": state.scan_generation,
                "moved": 0,
                "failed": 0,
                "batch_dir": report.display_path(result.batch_dir),
                "manifest_path": report.display_path(result.manifest_path),
                "group_ids": [],
            })
            batch_index = len(state.applied_batches) - 1
        prev = state.applied_batches[batch_index]
        state.applied_batches[batch_index] = {
            **prev,
            "moved": prev["moved"] + result.moved,
            "failed": prev["failed"] + result.failed,
            "group_ids": prev["group_ids"] + list(group_ids),
        }
    return batch_index


def _run_compress_queue(
    state: "ReviewState", jobs: List["CompressJob"], profile_name: str, remove_originals: bool,
    allow_lower_quality: bool = False,
) -> None:
    """Background worker for POST /api/apply-compress, spawned as a daemon
    thread nobody joins -- same never-escape contract as every other
    background worker in this module (the `except Exception` at the
    bottom). Per job: probe -> build_command()/run_encode()/verify_output()
    -> ONE apply_compressions() call for just that job's own CompressPlan,
    landing it in the archive immediately -- and, when `remove_originals`
    is set, ONE apply_compress_removals() call for just that job's own
    original, immediately after its own move succeeds. Each file therefore
    lands (appended to the queue's shared manifest) as soon as it verifies,
    rather than all of them after the whole queue finishes (the earlier
    design, mirrored from tidy/cli/run_compress.py's `_run_compress()`, kept for the
    CLI's own fire-and-forget use):
    in the interactive review UI, batching every move to the end meant the
    first file in a multi-file queue stayed invisible in its own folder
    for as long as the rest of the queue took to encode. `remove_originals`
    only ever touches THIS job's own original, built from THIS job's own
    apply_compressions() result -- never from `jobs` directly -- so a
    failed encode or a failed archive move can never produce a
    CompressRemoval for it (docs/business/rules/compression.md).

    Per-job resilience: one job failing (probe/encode/verify) never
    aborts the rest of the queue, matching apply_batch()/apply_moves()'s
    own per-entry resilience one layer up -- it is marked "failed" (or
    "cancelled", if a cancellation raced it) and the loop continues.

    `allow_lower_quality`, when set, verifies against
    cfg.min_xpsnr_relaxed instead of cfg.min_xpsnr for every job in this
    queue -- an explicit, per-apply opt-in from the "Jakosc" panel
    checkbox for content that is fine visually but does not clear the
    default safety margin. Still a real, enforced threshold -- never
    skipped outright (docs/business/rules/compression.md).

    The whole queue shares ONE `!MOVES\\<start>\\` and ONE `!REMOVE\\<start>\\`
    batch, named after the queue's start and reserved lazily on first use,
    so a queue in which everything fails leaves no empty batch behind."""
    state.compress_queue_progress.set_total(len(jobs))
    queue_started = datetime.now()
    moves_batch_dir: Optional[str] = None
    remove_batch_dir: Optional[str] = None
    moves_batch_index: Optional[int] = None
    remove_batch_index: Optional[int] = None
    cache = Cache(state.db_path) if state.db_path else None
    try:
        hw = _hwdetect_for(state)
        cfg = compress.load_compress_config(state.compress_config_path)
        profile = cfg.profiles.get(profile_name)
        if hw is None or profile is None:
            with state.compress_queue_lock:
                for job in jobs:
                    job.status = "failed"
                    job.error = (
                        state.hwdetect_block_reason
                        or "encoder unavailable on this machine"
                    )
            return

        encoder.ensure_staging_dir(state.compress_staging_dir)

        for idx, job in enumerate(jobs, start=1):
            if state.compress_cancel_event.is_set():
                with state.compress_queue_lock:
                    still_queued = [j for j in jobs[idx - 1:] if j.status == "queued"]
                    for remaining in still_queued:
                        remaining.status = "cancelled"
                state.compress_queue_progress.increment(len(still_queued))
                break

            try:
                src_mtime_ns = state.fs.stat(job.path).mtime_ns
            except (OSError, PermissionError):
                src_mtime_ns = None

            probe_result = probe.probe_file_cached(
                hw.ffprobe_exe, job.path, job.size, src_mtime_ns or 0, cache=cache,
            )
            if probe_result is None:
                with state.compress_queue_lock:
                    job.status = "failed"
                    job.error = "probe failed"
                state.compress_queue_progress.increment()
                continue

# See: docs/technical/components/review-ui.md
            staged = encoder.staging_path(state.compress_staging_dir, str(idx))
            cmd = encoder.build_command(
                hw.ffmpeg_exe, job.path, staged, profile, probe_result, device=job.device,
            )

            with state.compress_queue_lock:
                job.status = "encoding"
                job.started_at = time.time()
            encode_result = encoder.run_encode(
                cmd, duration=probe_result.duration, src_mtime_ns=src_mtime_ns,
                cancel_event=state.compress_cancel_event,
            )
# See: docs/technical/components/review-ui.md
            encode_seconds = time.time() - job.started_at
            if not encode_result.success:
                with state.compress_queue_lock:
                    if encode_result.reason == "cancelled":
                        job.status = "cancelled"
                    else:
                        job.status = "failed"
                        job.error = encode_result.reason
                    job.finished_at = time.time()
                state.compress_queue_progress.increment()
                continue

            with state.compress_queue_lock:
                job.status = "verifying"
            effective_min_xpsnr = (
                cfg.min_xpsnr_relaxed if allow_lower_quality else cfg.min_xpsnr
            )
            verify_result = encoder.verify_output(
                hw.ffmpeg_exe, hw.ffprobe_exe, job.path, encode_result.staging_path,
                probe_result, verify_xpsnr=cfg.verify_xpsnr, min_xpsnr=effective_min_xpsnr,
            )
            if not verify_result.ok:
                with state.compress_queue_lock:
                    job.status = "failed"
                    job.error = verify_result.reason
                    job.finished_at = time.time()
                state.compress_queue_progress.increment()
                continue

# See: docs/technical/components/review-ui.md
            with state.compress_queue_lock:
                job.status = "encoded"
                try:
                    job.result_size = os.path.getsize(encode_result.staging_path)
                except OSError:
                    job.result_size = 0
                job.xpsnr = verify_result.xpsnr_y
            state.compress_queue_progress.increment()

# See: docs/technical/components/review-ui.md
            plan = applier.CompressPlan(
                original_abs=job.path, staging_abs=encode_result.staging_path,
                dst_abs=compress.compressed_dst_path(job.path, cfg.suffix),
                group_id=job.group_id,
                reason=compress.manifest_reason(profile_name, profile, job.device_key),
            )
            if moves_batch_dir is None:
                moves_batch_dir = applier.reserve_batch_dir(
                    state.fs, state.archive_root, config.MOVES_DIR_NAME, queue_started,
                )
            compress_result = applier.apply_compressions(
                state.fs, state.archive_root, [plan], batch_dir=moves_batch_dir,
            )
            entry = compress_result.entries[0] if compress_result.entries else None

            with state.compress_queue_lock:
                job.finished_at = time.time()
                if entry is not None and entry.status == manifest.STATUS_DONE:
                    job.status = "done"
                    job.result_path = entry.dst_abs
                else:
                    job.status = "failed"
                    job.error = (entry.error if entry else "") or "move into the archive failed"

            if entry is not None and entry.status == manifest.STATUS_DONE and cache is not None:
# See: docs/technical/components/review-ui.md
                total_pixels = (
                    probe_result.width * probe_result.height *
                    probe_result.fps * probe_result.duration
                )
                if total_pixels > 0 and encode_seconds > 0:
                    measured_mpx_s = total_pixels / (encode_seconds * 1_000_000)
                    measured_bpp = (job.result_size * 8) / total_pixels
# See: docs/technical/components/review-ui.md
                    bpp_multiplier = config.PROFILE_BPP_SEED_MULTIPLIER.get(profile_name, 1.0)
                    normalized_bpp = measured_bpp / bpp_multiplier if bpp_multiplier > 0 else measured_bpp
                    cache.put_calibration(
                        hw.machine_id, job.device_key or profile["encoder"],
                        normalized_bpp, measured_mpx_s,
                        datetime.now(timezone.utc).isoformat(),
                        xpsnr_y=verify_result.xpsnr_y,
                    )

            moves_batch_index = _record_queue_batch(
                state, moves_batch_index, "compress", compress_result,
                [job.group_id] if job.result_path else [],
            )

            if remove_originals and entry is not None and entry.status == manifest.STATUS_DONE:
# See: docs/technical/components/review-ui.md
                if remove_batch_dir is None:
                    remove_batch_dir = applier.reserve_batch_dir(
                        state.fs, state.archive_root, config.REMOVE_DIR_NAME, queue_started,
                    )
                removal_result = applier.apply_compress_removals(
                    state.fs, state.archive_root,
                    [applier.CompressRemoval(
                        original_abs=entry.src_abs, compressed_abs=entry.dst_abs,
                        group_id=job.group_id,
                    )],
                    batch_dir=remove_batch_dir,
                )
                remove_batch_index = _record_queue_batch(
                    state, remove_batch_index, "remove", removal_result,
                    [job.group_id] if removal_result.moved else [],
                )
    except probe.ToolBlockedError as exc:
        reason = _note_tool_blocked(state, exc)
        with state.compress_queue_lock:
            for job in jobs:
                if job.status in ("queued", "encoding", "verifying", "encoded"):
                    job.status = "failed"
                    job.error = reason
                    job.finished_at = time.time()
    except Exception as exc:  # noqa: BLE001 - must never escape a daemon thread
        with state.compress_queue_lock:
            for job in jobs:
                if job.status in ("queued", "encoding", "verifying", "encoded"):
                    job.status = "failed"
                    job.error = str(exc)
                    job.finished_at = time.time()
    finally:
        if cache is not None:
            cache.close()
        with state.compress_queue_lock:
            state.compress_queue_running = False
        _invalidate_batch_cache(state)


# --- removable candidates reconstructed from disk --------------------------

def _compress_removable_candidates(state: "ReviewState") -> List[dict]:
    """Which compressed results' originals can now be discarded to
    !REMOVE, read from `!MOVES` manifests on disk -- NOT from
    state.compress_jobs (session-only, lost on restart) and NOT scoped to
    the current scan_generation (a restarted process has no in-memory
    generation to compare against) -- so this panel survives a server
    restart mid-review exactly like the "Restore" tab's own
    batches.list_batches() does (docs/technical/components/review-ui.md).

    Every `!MOVES` batch on disk whose rows are op=compress, full stop --
    batches.list_batches()'s kind="move" covers `!MOVES` (op=compress rows
    live there, per manifest.py's own table), not just op=move rows.

    A row is offered only when BOTH the original and the compressed
    result are still physically present (same "must actually be there
    right now" discipline BatchSummary.restorable/BatchEntry.restorable
    already apply) -- a row whose original was already discarded by hand,
    or whose result was moved/deleted outside this tool, is silently
    dropped rather than offered as if it were still actionable.

    Idempotent across repeated calls (GET /api/compress/jobs polls this on
    every request): an (original, compressed) pair already registered in
    compress_registry keeps its existing id rather than minting a new one
    every poll, which would otherwise flood the registry with duplicate
    entries for the same on-disk row.

    De-duplicated by (src_abs, dst_abs): re-compressing the same original
    across separate sessions (e.g. trying different quality profiles,
    deleting the compressed sibling in between to retry) leaves several
    `!MOVES` batches whose op=compress rows all name the SAME deterministic
    dst_abs (compress.compressed_dst_path() depends only on the original's
    own path, never on which profile produced it) -- but only one file can
    physically exist at that path right now. batches.list_batches() returns
    newest-first, so the first row seen for a given pair is the one that
    actually wrote the file currently there; every older row for the same
    pair describes bytes that no longer exist at dst_abs and is dropped,
    never shown as a second, stale-sized copy of the same result."""
    existing_by_pair: Dict[Tuple[str, str], str] = {
        (entry["path"], entry["compressed_path"]): file_id
        for file_id, entry in state.compress_registry.items()
        if "compressed_path" in entry
    }

    out: List[dict] = []
    seen_pairs: Set[Tuple[str, str]] = set()
    for summary in batches.list_batches(state.fs, state.archive_root):
        if summary.kind != "move":
            continue
        try:
            final_rows = manifest.read_final_rows(summary.manifest_path)
        except (OSError, PermissionError, IndexError, ValueError):
# See: docs/technical/components/review-ui.md
            continue

        batch_dir = os.path.dirname(summary.manifest_path)
        for row in final_rows.values():
            if row.op != manifest.OP_COMPRESS or row.status != manifest.STATUS_DONE:
                continue
            if applier.unsafe_row_reason(row, state.archive_root):
                continue
            dst_abs = applier.resolve_dst(row, batch_dir)
            pair_key = (row.src_abs, dst_abs)
            if pair_key in seen_pairs:
                continue
            if not state.fs.exists(row.src_abs) or not state.fs.exists(dst_abs):
                continue
            try:
                original_size = state.fs.stat(row.src_abs).size
# See: docs/technical/components/review-ui.md
                compressed_size = state.fs.stat(dst_abs).size
            except (OSError, PermissionError):
                continue
            seen_pairs.add(pair_key)

            file_id = existing_by_pair.get(pair_key)
            if file_id is None:
                prefix = state.next_compress_id_prefix()
                group_id = f"{prefix}1"
                file_id = f"{group_id}:0"
                state.compress_registry[file_id] = {
                    "path": row.src_abs, "compressed_path": dst_abs,
                    "group_id": group_id, "size": original_size,
                }
                existing_by_pair[pair_key] = file_id

            entry = state.compress_registry[file_id]
            out.append({
                "file_id": file_id,
                "group_id": entry["group_id"],
                "path": report.display_path(row.src_abs),
                "compressed_path": report.display_path(dst_abs),
                "original_size": original_size,
                "compressed_size": compressed_size,
            })

    return out
