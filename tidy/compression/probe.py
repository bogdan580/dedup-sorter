"""Video stream probing via ffprobe, with cache in compress_probes.

Pure parsing logic is testable without ffprobe; the subprocess call is
isolated in `_run_ffprobe()` so integration tests can target it separately.

This module calls ffprobe (never ffmpeg) and reads its JSON output.
It does NOT use the FileSystem seam -- ffprobe is an external process that
reads directly from disk, same exception as thumbnails.py.
# See: docs/technical/components/compression-mechanism.md
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

from tidy import config

log = logging.getLogger(__name__)


class ToolBlockedError(OSError):
    """ffmpeg/ffprobe cannot be launched because Windows Smart App Control
    blocked the executable. Machine-wide: callers abort the whole operation
    instead of treating it as a per-file failure.
    See: docs/technical/components/compression-mechanism.md"""


def raise_if_blocked(exc: OSError, exe: str) -> None:
    """Raise ToolBlockedError (chained from `exc`) when `exc` is the
    Smart App Control launch block; return otherwise."""
    if getattr(exc, "winerror", None) == config.WINERROR_APP_CONTROL_BLOCKED:
        raise ToolBlockedError(
            f"ffmpeg/ffprobe blocked by Smart App Control "
            f"(WinError {config.WINERROR_APP_CONTROL_BLOCKED}): {exe}"
        ) from exc


@dataclass(frozen=True)
class ProbeResult:
    """Parsed stream parameters for one video file."""
    codec: str          # e.g. "hevc", "h264", "mjpeg"
    width: int
    height: int
    fps: float          # average frame rate
    bitrate: int        # total file bitrate in bits/s
    duration: float     # seconds
    has_audio: bool
    rotation: int       # display rotation from side data, 0 if absent
    audio_codec: str = ""  # e.g. "aac", "pcm_s16le"; "" when has_audio is False

    @property
    def bpp(self) -> float:
        """Bits per pixel: bitrate / (width * height * fps).
        Returns 0.0 when any dimension or fps is zero."""
        pixels_per_sec = self.width * self.height * self.fps
        if pixels_per_sec <= 0:
            return 0.0
        return self.bitrate / pixels_per_sec


def _strip_extended_prefix(path: str) -> str:
    """Strip Windows \\\\?\\ / \\\\?\\UNC\\ prefix for subprocess args."""
    _UNC = "\\\\?\\UNC\\"
    _LONG = "\\\\?\\"
    if path.startswith(_UNC):
        return "\\\\" + path[len(_UNC):]
    if path.startswith(_LONG):
        return path[len(_LONG):]
    return path


def _run_ffprobe(ffprobe_exe: str, path: str, timeout: int = 30) -> Optional[dict]:
    """Run ffprobe -show_streams -show_format on `path`, return parsed JSON
    or None on a per-file failure. Raises ToolBlockedError when ffprobe
    itself cannot be launched (Smart App Control)."""
    stripped = _strip_extended_prefix(path)
    cmd = [
        ffprobe_exe,
        "-v", "error",
        "-show_streams",
        "-protocol_whitelist", "file,pipe",
        "-show_format",
        "-print_format", "json",
        stripped,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
# See: docs/technical/components/compression-mechanism.md
            encoding="utf-8",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            log.debug("ffprobe returned %d for %s: %s",
                      result.returncode, path, result.stderr[:200])
            return None
        return json.loads(result.stdout)
    except OSError as exc:
        raise_if_blocked(exc, ffprobe_exe)
        log.debug("ffprobe failed for %s: %s", path, exc)
        return None
    except (subprocess.TimeoutExpired, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.debug("ffprobe failed for %s: %s", path, exc)
        return None


def _parse_rate_key(stream: dict, key: str) -> Optional[float]:
    """Parse one ffprobe '<num>/<den>' (or plain numeric) rate field by
    key name. None when the key is absent or unparseable."""
    raw = stream.get(key, "")
    if "/" in raw:
        parts = raw.split("/")
        try:
            num, den = float(parts[0]), float(parts[1])
            if den > 0:
                return num / den
        except (ValueError, IndexError):
            return None
        return None
    if raw:
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _parse_fps(stream: dict) -> float:
    """Extract fps from avg_frame_rate or r_frame_rate.
    avg_frame_rate is preferred because r_frame_rate can be misleading
    for VFR content: for the bpp/duration math ProbeResult.fps feeds
    (compress.py's estimator), the true long-run average is what
    matters."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = _parse_rate_key(stream, key)
        if value is not None:
            return value
    return 0.0


def _parse_rotation(stream: dict) -> int:
    """Extract display rotation from side_data_list or tags."""
    # Modern ffprobe puts it in side_data_list
    for sd in stream.get("side_data_list", []):
        if "rotation" in sd:
            try:
                return int(float(sd["rotation"]))
            except (ValueError, TypeError):
                pass
    # Older ffprobe puts it in tags
    tags = stream.get("tags", {})
    rotate = tags.get("rotate", "")
    if rotate:
        try:
            return int(rotate)
        except ValueError:
            pass
    return 0


def parse_probe_json(data: dict) -> Optional[ProbeResult]:
    """Parse ffprobe JSON output into a ProbeResult.
    Returns None if no video stream is found.

    This is a pure function -- testable without ffprobe."""
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video_stream = None
    has_audio = False
    audio_codec = ""
    for s in streams:
        if s.get("codec_type") == "video":
            # Skip attached pictures (album art in MP4)
            disposition = s.get("disposition", {})
            if disposition.get("attached_pic", 0):
                continue
            if video_stream is None:
                video_stream = s
        elif s.get("codec_type") == "audio":
            has_audio = True
            if not audio_codec:
                audio_codec = s.get("codec_name", "")

    if video_stream is None:
        return None

    codec = video_stream.get("codec_name", "unknown")
    try:
        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))
    except (ValueError, TypeError):
        width, height = 0, 0

    fps = _parse_fps(video_stream)
    rotation = _parse_rotation(video_stream)

    # Duration: prefer format-level (container), fall back to stream.
    duration = 0.0
    for src in (fmt.get("duration"), video_stream.get("duration")):
        if src is not None:
            try:
                duration = float(src)
                break
            except (ValueError, TypeError):
                continue

    # Bitrate: prefer format-level (total), fall back to stream.
    bitrate = 0
    for src in (fmt.get("bit_rate"), video_stream.get("bit_rate")):
        if src is not None:
            try:
                bitrate = int(src)
                break
            except (ValueError, TypeError):
                continue
    # If bitrate is still 0, compute from file size and duration.
    if bitrate == 0 and duration > 0:
        try:
            file_size = int(fmt.get("size", 0))
            if file_size > 0:
                bitrate = int(file_size * 8 / duration)
        except (ValueError, TypeError):
            pass

    return ProbeResult(
        codec=codec,
        width=width,
        height=height,
        fps=fps,
        bitrate=bitrate,
        duration=duration,
        has_audio=has_audio,
        rotation=rotation,
        audio_codec=audio_codec,
    )


def probe_file(ffprobe_exe: str, path: str) -> Optional[ProbeResult]:
    """Probe a single file. Returns None on a per-file failure; raises
    ToolBlockedError when ffprobe cannot be launched."""
    data = _run_ffprobe(ffprobe_exe, path)
    if data is None:
        return None
    return parse_probe_json(data)


def probe_file_cached(
    ffprobe_exe: str,
    path: str,
    size: int,
    mtime_ns: int,
    cache: object = None,
) -> Optional[ProbeResult]:
    """Probe with cache lookup / store. `cache` may be None (no caching).
    When provided, `cache` must be a `cache.Cache` instance (typed as object
    to avoid circular import). ToolBlockedError propagates and nothing is
    cached for a blocked probe."""
    if cache is not None:
        cached = cache.get_compress_probe(path, size, mtime_ns)
        if cached is not None:
            return ProbeResult(
                codec=cached["codec"],
                width=cached["width"],
                height=cached["height"],
                fps=cached["fps"],
                bitrate=cached["bitrate"],
                duration=cached["duration"],
                has_audio=cached["has_audio"],
                rotation=cached["rotation"],
                audio_codec=cached.get("audio_codec", ""),
            )

    result = probe_file(ffprobe_exe, path)
    if result is not None and cache is not None:
        cache.put_compress_probe(path, size, mtime_ns, {
            "codec": result.codec,
            "width": result.width,
            "height": result.height,
            "fps": result.fps,
            "bitrate": result.bitrate,
            "duration": result.duration,
            "has_audio": result.has_audio,
            "rotation": result.rotation,
            "audio_codec": result.audio_codec,
        })
    return result
