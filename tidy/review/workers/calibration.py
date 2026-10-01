"""Background compression calibration job."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from tidy.compression import compress
from tidy import config
from tidy.compression import encoder
from tidy.compression import probe
from tidy.store.cache import Cache
from tidy.review.computations import _hwdetect_for, _note_tool_blocked
from tidy.review.state import CalibrationJob, ReviewState


# --- calibration: POST /api/compress/calibrate's background worker --------

def _run_calibration_job(state: "ReviewState", job: "CalibrationJob") -> None:
    """Background worker for POST /api/compress/calibrate (docs/technical/components/compression-mechanism.md
    E4.3). Never-escape contract, same as every other background worker in
    this module (the `except Exception` at the bottom).

    Per device_key: build that device's own default profile
    (compress.default_profile_for_encoder()), encode a
    `_CALIBRATION_CLIP_SECONDS` slice via encoder.build_command()'s
    `input_duration_limit` (never the `trim` filter; docs/technical/components/compression-mechanism.md),
    measure XPSNR-Y against the SAME (untouched) source, write the
    measurement to cache.compress_calibration, and delete the staged file
    immediately. Nothing here ever reaches tidy/moves/applier: no manifest, no
    archive move -- this is measurement, not a compress job. One device
    failing (unresolvable, encode error) never aborts the rest -- it is
    just absent from job.measured, same per-entry resilience as
    _run_compress_queue()'s own per-job handling."""
    cache = Cache(state.db_path) if state.db_path else None
    try:
        hw = _hwdetect_for(state)
        cfg = compress.load_compress_config(state.compress_config_path)
        if hw is None:
            job.status = "failed"
            job.error = (
                state.hwdetect_block_reason or "encoder unavailable on this machine"
            )
            return

        encoder.ensure_staging_dir(state.compress_staging_dir)
        try:
            src_mtime_ns = state.fs.stat(job.path).mtime_ns
        except (OSError, PermissionError):
            src_mtime_ns = None
        probe_result = probe.probe_file_cached(
            hw.ffprobe_exe, job.path, job.size, src_mtime_ns or 0, cache=cache,
        )
        if probe_result is None:
            job.status = "failed"
            job.error = "probe failed"
            return
        clip_seconds = min(probe_result.duration, _CALIBRATION_CLIP_SECONDS)

        for idx, device_key in enumerate(job.device_keys, start=1):
            if state.compress_cancel_event.is_set():
                job.status = "cancelled"
                break

            entry = next((e for e in hw.encoders if e.device_key == device_key), None)
            profile_name = (
                compress.default_profile_for_encoder(entry.name, cfg.profiles)
                if entry is not None else None
            )
            profile = cfg.profiles.get(profile_name) if profile_name else None
            if entry is None or profile is None:
# See: docs/technical/components/review-ui.md
                continue

            job.current_device_key = device_key
            job.status = "running"
            staged = encoder.staging_path(
                state.compress_staging_dir, f"calib-{idx}",
            )
            cmd = encoder.build_command(
                hw.ffmpeg_exe, job.path, staged, profile, probe_result,
                device=entry.device, input_duration_limit=_CALIBRATION_CLIP_SECONDS,
            )
            start = time.time()
            encode_result = encoder.run_encode(
                cmd, duration=clip_seconds, cancel_event=state.compress_cancel_event,
            )
            elapsed = time.time() - start
            if not encode_result.success:
                continue

            try:
                result_size = os.path.getsize(encode_result.staging_path)
            except OSError:
                result_size = 0
            xpsnr_y = encoder.measure_xpsnr_y(
                hw.ffmpeg_exe, encode_result.staging_path, job.path,
            )
# See: docs/technical/components/review-ui.md
            try:
                os.remove(encode_result.staging_path)
            except OSError:
                pass

            total_pixels = (
                probe_result.width * probe_result.height *
                probe_result.fps * clip_seconds
            )
            if total_pixels <= 0 or elapsed <= 0:
                continue
            measured_mpx_s = total_pixels / (elapsed * 1_000_000)
            measured_bpp = (result_size * 8) / total_pixels
# See: docs/technical/components/review-ui.md
            bpp_multiplier = config.PROFILE_BPP_SEED_MULTIPLIER.get(profile_name, 1.0)
            normalized_bpp = measured_bpp / bpp_multiplier if bpp_multiplier > 0 else measured_bpp

            measurement = {
                "bpp": normalized_bpp, "mpx_s": measured_mpx_s, "xpsnr_y": xpsnr_y,
            }
            job.measured[device_key] = measurement
            if cache is not None:
                cache.put_calibration(
                    hw.machine_id, device_key, normalized_bpp, measured_mpx_s,
                    datetime.now(timezone.utc).isoformat(), xpsnr_y=xpsnr_y,
                )

        if job.status != "cancelled":
            job.status = "done"
        if job.measured:
# See: docs/technical/components/review-ui.md
            with state.compress_lock:
                state.compress_computations.clear()
                state.compress_progress.clear()
    except probe.ToolBlockedError as exc:
        job.status = "failed"
        job.error = _note_tool_blocked(state, exc)
    except Exception as exc:  # noqa: BLE001 - must never escape a daemon thread
        job.status = "failed"
        job.error = str(exc)
    finally:
        if cache is not None:
            cache.close()
        with state.compress_queue_lock:
            state.compress_queue_running = False


# See: docs/technical/components/review-ui.md
_CALIBRATION_CLIP_SECONDS = 6.0
