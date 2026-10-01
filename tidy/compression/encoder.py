"""ffmpeg command construction, staging, progress, and output verification.

Runs ffmpeg/ffprobe as external processes that write directly to
`.dedup/.compress/` staging (real disk, never the FileSystem seam) -- the
same exception documented at thumbnails.py's `_write_video_thumbnail()`:
ffmpeg is an external process and cannot be handed a `FakeFileSystem`
stream. Only tidy/moves/applier ever moves a confirmed, verified result out of
staging and into the archive.
# See: docs/technical/components/compression-mechanism.md
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from tidy.compression.probe import ProbeResult, probe_file, raise_if_blocked
from tidy.compression.probe import _strip_extended_prefix  # same helper, avoid duplicating it

log = logging.getLogger(__name__)

_PCM_AUDIO_PREFIX = "pcm_"
_DURATION_TOLERANCE_S = 0.5

_FRAME_RE = re.compile(r"^frame=(\d+)\s*$", re.MULTILINE)
_XPSNR_Y_RE = re.compile(r"XPSNR\s+y:\s*([0-9.]+)", re.IGNORECASE)

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class EncodeResult:
    """Outcome of one encode attempt. `staging_path` is only meaningful
    when `success` is True."""
    success: bool
    staging_path: str
    reason: str = ""


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of verifying a staged encode against its source
    (docs/technical/components/compression-mechanism.md). `xpsnr_y` is None when verify_xpsnr was disabled or the
    measurement could not be parsed."""
    ok: bool
    reason: str
    xpsnr_y: Optional[float] = None


def staging_path(staging_dir: str, job_id: str, ext: str = ".mp4") -> str:
    """Build the staging file path for one job. Does not create the file."""
    return os.path.join(staging_dir, f"{job_id}{ext}")


def ensure_staging_dir(staging_dir: str) -> None:
    """Create the staging directory if absent. Real disk, no fs seam --
    same rationale as the module docstring."""
    os.makedirs(staging_dir, exist_ok=True)


def _audio_args(probe: ProbeResult) -> List[str]:
    """`-c:a copy` unless the source is PCM, where re-encoding to AAC is
    required (docs/technical/components/compression-mechanism.md)."""
    if not probe.has_audio:
        return []
    if probe.audio_codec.casefold().startswith(_PCM_AUDIO_PREFIX):
        return ["-c:a", "aac", "-b:a", "192k"]
    return ["-c:a", "copy"]


def build_command(
    ffmpeg_exe: str,
    src: str,
    dst: str,
    profile: dict,
    probe: ProbeResult,
    device: str = "",
    input_duration_limit: Optional[float] = None,
) -> List[str]:
    """Build the ffmpeg argv for one encode job. `profile` is one entry of
    config.DEFAULT_COMPRESS_PROFILES, e.g.
    {"encoder": "libx265", "crf": 21, "preset": "slow"} (CPU) or
    {"encoder": "hevc_nvenc", "cq": 23, "preset": "p7"} /
    {"encoder": "hevc_qsv", "global_quality": 23} (hardware).

    `device` is the NVENC GPU index (hwdetect.EncoderInfo.device, e.g.
    "0"/"1") passed as `-gpu <device>`; empty for CPU encoders and for QSV,
    whose EncoderInfo.device is always "" (one default adapter -- see
    hwdetect.py's own contract on that field).

    `input_duration_limit`, when given, adds `-t <seconds>` BEFORE `-i` --
    an INPUT option that limits how much of the source ffmpeg reads at
    all, never a `-t`/`trim` on the output. POST /api/compress/calibrate
    (docs/technical/components/compression-mechanism.md) is the only caller: a `trim` filter hangs framesync,
    so the clip is limited on the INPUT instead.

    The flags in docs/technical/components/compression-mechanism.md are non-optional (rotation,
    fps_mode, pix_fmt, metadata) and apply to every branch below."""
    encoder = profile["encoder"]
    src_stripped = _strip_extended_prefix(src)

    cmd = [
        ffmpeg_exe, "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
        "-noautorotate",
    ]
    if input_duration_limit is not None:
        cmd += ["-t", str(input_duration_limit)]
    cmd += [
        "-protocol_whitelist", "file,pipe",
        "-i", src_stripped,
        "-map", "0", "-map", "-0:d?",
    ]

    if encoder == "libx265":
        cmd += [
            "-c:v", "libx265", "-crf", str(profile["crf"]),
            "-preset", str(profile["preset"]),
            "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
        ]
        cmd += _audio_args(probe)
        cmd += ["-c:s", "copy"]
    elif encoder == "libsvtav1":
        cmd += [
            "-c:v", "libsvtav1", "-crf", str(profile["crf"]),
            "-preset", str(profile["preset"]),
            "-svtav1-params", "tune=0",
            "-pix_fmt", "yuv420p10le",
        ]
        cmd += _audio_args(probe)
    elif encoder == "hevc_nvenc":
# See: docs/technical/components/compression-mechanism.md
        cmd += [
            "-c:v", "hevc_nvenc", "-preset", str(profile["preset"]),
            "-tune", "hq", "-rc", "vbr", "-cq", str(profile["cq"]), "-b:v", "0",
            "-profile:v", "main10", "-pix_fmt", "p010le", "-tag:v", "hvc1",
        ]
        if device:
            cmd += ["-gpu", device]
        cmd += _audio_args(probe)
        cmd += ["-c:s", "copy"]
    elif encoder == "av1_nvenc":
        cmd += [
            "-c:v", "av1_nvenc", "-preset", str(profile["preset"]),
            "-tune", "hq", "-rc", "vbr", "-cq", str(profile["cq"]), "-b:v", "0",
            "-pix_fmt", "p010le",
        ]
        if device:
            cmd += ["-gpu", device]
        cmd += _audio_args(probe)
    elif encoder == "hevc_qsv":
# See: docs/technical/components/compression-mechanism.md
        cmd += [
            "-c:v", "hevc_qsv", "-preset", "veryslow",
            "-global_quality", str(profile["global_quality"]),
            "-profile:v", "main10", "-pix_fmt", "p010le", "-tag:v", "hvc1",
        ]
        cmd += _audio_args(probe)
        cmd += ["-c:s", "copy"]
    elif encoder == "av1_qsv":
        cmd += [
            "-c:v", "av1_qsv", "-preset", "veryslow",
            "-global_quality", str(profile["global_quality"]),
            "-pix_fmt", "p010le",
        ]
        cmd += _audio_args(probe)
    else:
        raise ValueError(f"unknown encoder profile: {encoder}")

    cmd += [
        "-fps_mode", "passthrough",
        "-map_metadata", "0", "-movflags", "use_metadata_tags+faststart",
        "-metadata:s:v:0", f"rotate={probe.rotation}",
        _strip_extended_prefix(dst),
    ]
    return cmd


def _drain_stdout_progress(
    stream, on_progress: Optional[Callable[[float], None]], duration: float
) -> None:
    """Parse ffmpeg's `-progress pipe:1` key=value stream, calling
    on_progress(seconds_encoded) for each out_time_ms= line."""
    for line in stream:
        if on_progress is None:
            continue
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                us = int(line.split("=", 1)[1])
                on_progress(min(us / 1_000_000.0, duration) if duration > 0 else us / 1_000_000.0)
            except (ValueError, IndexError):
                pass


def _drain_stderr(stream, sink: List[str]) -> None:
    for line in stream:
        sink.append(line)


_CANCEL_POLL_INTERVAL_S = 0.3


def _spawn_encode(cmd: List[str]) -> "subprocess.Popen[str]":
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
# See: docs/technical/components/compression-mechanism.md
        text=True, encoding="utf-8", creationflags=_CREATIONFLAGS,
    )


def run_encode(
    cmd: List[str],
    *,
    duration: float = 0.0,
    on_progress: Optional[Callable[[float], None]] = None,
    timeout: Optional[float] = None,
    src_mtime_ns: Optional[int] = None,
    cancel_event: Optional[threading.Event] = None,
) -> EncodeResult:
    """Run one ffmpeg encode command built by build_command().

    `cmd`'s last argument is the staging destination path. stdout carries
    -progress key=value pairs (drained on a background thread so a large
    stderr never deadlocks the pipe); stderr carries only real errors
    because build_command() sets -v error.

    ffmpeg never carries a file's mtime into its output
    (docs/technical/components/compression-mechanism.md) -- series/events grouping depends on it, so when `src_mtime_ns`
    is given, a successful encode's staged file is stamped with it here,
    before tidy/moves/applier ever moves it into the archive.

    `cancel_event`, when given, is polled every `_CANCEL_POLL_INTERVAL_S`
    instead of blocking on one long `proc.wait(timeout=timeout)` -- the
    review UI's queue (tidy/review/workers/compress_queue.py) needs to kill a
    multi-minute encode within a fraction of a second of the user pressing
    "anuluj", not at the next `timeout` boundary. A set event takes
    priority over an eventual `timeout` firing at the same poll: the user
    asked to stop, so this reports "cancelled", not "timeout".

    A launch failure returns success=False with the OS message, except
    Smart App Control (winerror 4551), which raises ToolBlockedError."""
    dst = cmd[-1]
    try:
        proc = _spawn_encode(cmd)
    except OSError as exc:
        raise_if_blocked(exc, cmd[0])
        return EncodeResult(success=False, staging_path=dst, reason=str(exc))

    stderr_lines: List[str] = []
    progress_thread = threading.Thread(
        target=_drain_stdout_progress, args=(proc.stdout, on_progress, duration),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_stderr, args=(proc.stderr, stderr_lines), daemon=True,
    )
    progress_thread.start()
    stderr_thread.start()

    start = time.monotonic()
    cancelled = False
    timed_out = False
    while True:
        wait_slice = (
            _CANCEL_POLL_INTERVAL_S if cancel_event is not None
            else timeout
        )
        try:
            proc.wait(timeout=wait_slice)
            break
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                proc.kill()
                proc.wait()
                break
            if timeout is not None and (time.monotonic() - start) >= timeout:
                timed_out = True
                proc.kill()
                proc.wait()
                break
# See: docs/technical/components/compression-mechanism.md
            continue

    progress_thread.join(timeout=5)
    stderr_thread.join(timeout=5)

    if cancelled:
        return EncodeResult(success=False, staging_path=dst, reason="cancelled")
    if timed_out:
        return EncodeResult(success=False, staging_path=dst, reason="timeout")

    if proc.returncode != 0:
        return EncodeResult(
            success=False, staging_path=dst,
            reason="".join(stderr_lines).strip()[-500:] or f"ffmpeg exit {proc.returncode}",
        )
    if src_mtime_ns is not None:
        try:
            os.utime(dst, ns=(src_mtime_ns, src_mtime_ns))
        except OSError:
            pass
    return EncodeResult(success=True, staging_path=dst)


def _decode_frame_count(ffmpeg_exe: str, path: str, timeout: int = 120) -> Optional[int]:
    """Fully decode `path` and return its frame count, or None on failure.
    Doubles as the "decodes to completion without error" check
    (docs/technical/components/compression-mechanism.md): a nonzero exit or unparseable output is a
    verification failure, not just a missing frame count."""
    cmd = [
        ffmpeg_exe, "-v", "error", "-nostats", "-progress", "pipe:1",
        "-protocol_whitelist", "file,pipe",
        "-i", _strip_extended_prefix(path), "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
# See: docs/technical/components/compression-mechanism.md
            encoding="utf-8", timeout=timeout,
            creationflags=_CREATIONFLAGS,
        )
    except OSError as exc:
        raise_if_blocked(exc, ffmpeg_exe)
        return None
    except (subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    if result.returncode != 0:
        return None
    matches = _FRAME_RE.findall(result.stdout)
    if not matches:
        return None
    return int(matches[-1])


def measure_xpsnr_y(
    ffmpeg_exe: str, staged: str, src: str, timeout: int = 300,
) -> Optional[float]:
    """Measure luma XPSNR of `staged` against `src`, pairing frames by
    decode index rather than by timestamp; the comparison stops at the
    shorter stream, so a clip encoded from the source's head (calibration)
    is compared against that head only. Valid only because encodes run
    with -fps_mode passthrough and verify_output() checks equal frame counts.
    See: docs/technical/components/compression-mechanism.md"""
    # settb+setpts=N renumbers both streams onto one grid: any timestamp-
    # based resampling (the fps filter) misaligns VFR phone footage.
    renumber = "settb=1/1000,setpts=N*10,format=yuv420p"
    filt = (
        f"[0:v]{renumber}[d];"
        f"[1:v]{renumber}[r];[d][r]xpsnr=shortest=1"
    )
    cmd = [
        ffmpeg_exe, "-hide_banner", "-nostats",
        "-an", "-protocol_whitelist", "file,pipe", "-i", _strip_extended_prefix(staged),
        "-an", "-protocol_whitelist", "file,pipe", "-i", _strip_extended_prefix(src),
        "-lavfi", filt, "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
# See: docs/technical/components/compression-mechanism.md
            encoding="utf-8", timeout=timeout,
            creationflags=_CREATIONFLAGS,
        )
    except OSError as exc:
        raise_if_blocked(exc, ffmpeg_exe)
        return None
    except (subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    match = _XPSNR_Y_RE.search(result.stderr or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def verify_output(
    ffmpeg_exe: str,
    ffprobe_exe: str,
    src: str,
    staged: str,
    src_probe: ProbeResult,
    *,
    verify_xpsnr: bool = True,
    min_xpsnr: float = 38.0,
) -> VerifyResult:
    """Verify a staged encode against its source (docs/technical/components/compression-mechanism.md).
    Checks run in order; first failure short-circuits (an expensive XPSNR
    pass is skipped once a cheaper check has already failed). Raises
    ToolBlockedError when ffmpeg/ffprobe cannot be launched."""
    if not os.path.isfile(staged) or os.path.getsize(staged) == 0:
        return VerifyResult(ok=False, reason="staged file missing or empty")

    dst_probe = probe_file(ffprobe_exe, staged)
    if dst_probe is None:
        return VerifyResult(ok=False, reason="staged file unreadable by ffprobe")

    if abs(dst_probe.duration - src_probe.duration) > _DURATION_TOLERANCE_S:
        return VerifyResult(
            ok=False,
            reason=(
                f"duration mismatch: source {src_probe.duration:.2f}s "
                f"vs result {dst_probe.duration:.2f}s"
            ),
        )

    src_frames = _decode_frame_count(ffmpeg_exe, src)
    if src_frames is None:
        return VerifyResult(ok=False, reason="source failed to decode for frame count")
    dst_frames = _decode_frame_count(ffmpeg_exe, staged)
    if dst_frames is None:
        return VerifyResult(ok=False, reason="staged file failed to decode")
    if src_frames != dst_frames:
        return VerifyResult(
            ok=False,
            reason=f"frame count mismatch: source {src_frames} vs result {dst_frames}",
        )

    if not verify_xpsnr:
        return VerifyResult(ok=True, reason="ok (XPSNR skipped)")

    xpsnr_y = measure_xpsnr_y(ffmpeg_exe, staged, src)
    if xpsnr_y is None:
        return VerifyResult(ok=False, reason="XPSNR measurement failed")
    if xpsnr_y < min_xpsnr:
        return VerifyResult(
            ok=False,
            reason=f"XPSNR {xpsnr_y:.2f} dB below threshold {min_xpsnr:.2f} dB",
            xpsnr_y=xpsnr_y,
        )
    return VerifyResult(ok=True, reason="ok", xpsnr_y=xpsnr_y)
