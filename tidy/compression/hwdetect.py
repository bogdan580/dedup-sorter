"""Hardware encoder detection and ffmpeg binary resolution.

Resolves the ffmpeg/ffprobe binary location using the chain:
  config.json ("ffmpeg_path") -> bin/ffmpeg/ in repo -> PATH

Then probes which encoders are actually available (not just listed)
by attempting a 1-frame encode with each candidate, and which physical
device(s) each hardware encoder actually works on.
# See: docs/technical/components/compression-mechanism.md
"""
from __future__ import annotations

import functools
import json
import logging
import os
import platform
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from tidy import config
from tidy import paths
from tidy.compression.probe import raise_if_blocked

log = logging.getLogger(__name__)

# Encoders this module knows about, grouped by codec family.
_KNOWN_ENCODERS: Dict[str, List[str]] = {
    "x265": ["libx265"],
    "av1": ["libsvtav1"],
    "hevc_hw": ["hevc_nvenc", "hevc_qsv"],
    "av1_hw": ["av1_nvenc", "av1_qsv"],
}

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# See: docs/technical/components/compression-mechanism.md
_TEST_ENCODE_FRAME_SIZE = "256x256"

_NVENC_GPU_LIST_RE = re.compile(r"GPU #(\d+)\s*-\s*<\s*(.+?)\s*>")

_NVENC_GENERIC_DEVICE_NAME = "GPU 0"


@dataclass(frozen=True)
class EncoderInfo:
    """One available encoder with optional device info.

    `device_key` is the stable identity calibration data is stored under
    (cache.compress_calibration, keyed together with machine_id): "<encoder>"
    for CPU encoders, "<encoder>@<device name>" for hardware. Two physical
    GPUs of the same encoder therefore calibrate separately."""
    name: str           # e.g. "libx265", "hevc_nvenc"
    display_name: str   # e.g. "x265 (CPU)", "HEVC / NVENC (RTX 4060)"
    device: str         # e.g. "" for CPU/QSV default adapter, "0"/"1" for NVENC GPU index
    is_hardware: bool
    device_key: str = ""  # "" only for callers predating this field; detect() always sets it


@dataclass(frozen=True)
class HwDetectResult:
    """Everything hwdetect discovered about this machine."""
    ffmpeg_exe: str
    ffprobe_exe: str
    ffmpeg_version: str
    machine_id: str
    encoders: Tuple[EncoderInfo, ...]


def _repo_root() -> str:
    """The .dedup/ directory (repo root), resolved from this file's location."""
    return str(paths.DEDUP_DIR)


def _resolve_ffmpeg_exe(config_json_path: Optional[str] = None) -> Optional[str]:
    """Resolve ffmpeg binary. Chain: config.json -> bin/ffmpeg/ -> PATH."""
    # 1. config.json override
    if config_json_path:
        try:
            with open(config_json_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            custom = cfg.get("compression", {}).get("ffmpeg_path")
            if custom and os.path.isfile(custom):
                return custom
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    # 2. Bundled binary in bin/ffmpeg/
    repo = _repo_root()
    bundled = os.path.join(repo, config.BUNDLED_FFMPEG_REL, "ffmpeg.exe")
    if os.path.isfile(bundled):
        return bundled

    # 3. PATH fallback
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found

    return None


def _resolve_ffprobe_exe(ffmpeg_exe: str) -> Optional[str]:
    """Derive ffprobe path from ffmpeg path (same directory)."""
    directory = os.path.dirname(ffmpeg_exe)
    ffprobe = os.path.join(directory, "ffprobe.exe")
    if os.path.isfile(ffprobe):
        return ffprobe
    # Try without .exe for non-Windows
    ffprobe_noext = os.path.join(directory, "ffprobe")
    if os.path.isfile(ffprobe_noext):
        return ffprobe_noext
    # Fallback: try PATH
    import shutil
    return shutil.which("ffprobe")


def _get_ffmpeg_version(ffmpeg_exe: str) -> str:
    """Extract version string from ffmpeg -version."""
    try:
        result = subprocess.run(
            [ffmpeg_exe, "-version"],
            capture_output=True, text=True, timeout=10,
            creationflags=_CREATIONFLAGS,
        )
        if result.returncode == 0:
            first_line = result.stdout.split("\n")[0]
            return first_line.strip()
    except OSError as exc:
        raise_if_blocked(exc, ffmpeg_exe)
    except subprocess.TimeoutExpired:
        pass
    return "unknown"


def _check_launchable(exe: str) -> None:
    """Run `<exe> -version`; raises ToolBlockedError when Smart App Control
    blocks the launch. Any other failure is ignored here."""
    try:
        subprocess.run(
            [exe, "-version"], capture_output=True, timeout=10,
            creationflags=_CREATIONFLAGS,
        )
    except OSError as exc:
        raise_if_blocked(exc, exe)
    except subprocess.TimeoutExpired:
        pass


def _list_encoders(ffmpeg_exe: str) -> List[str]:
    """Get list of available encoder names from ffmpeg -encoders."""
    try:
        result = subprocess.run(
            [ffmpeg_exe, "-encoders", "-hide_banner"],
            capture_output=True, text=True, timeout=10,
            creationflags=_CREATIONFLAGS,
        )
        if result.returncode != 0:
            return []
        encoders = []
        for line in result.stdout.splitlines():
            line = line.strip()
            # Lines look like " V..... libx265 ..."
            if line and line[0] in ("V", "A", "S") and len(line) > 7:
                parts = line.split()
                if len(parts) >= 2:
                    encoders.append(parts[1])
        return encoders
    except (subprocess.TimeoutExpired, OSError):
        return []


def _test_encoder(ffmpeg_exe: str, encoder_name: str, device: str = "") -> bool:
    """Test if an encoder actually works by encoding 1 frame from lavfi.
    For static GPL builds, the encoder list includes HW encoders even when
    the hardware is absent -- this is the only reliable test. Frame size is
    `_TEST_ENCODE_FRAME_SIZE`, not smaller -- see that constant's contract."""
    cmd = [
        ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration=0.04:size={_TEST_ENCODE_FRAME_SIZE}:rate=25",
        "-frames:v", "1",
        "-c:v", encoder_name,
    ]
    if device:
        if "nvenc" in encoder_name:
            cmd.extend(["-gpu", device])
        elif "qsv" in encoder_name:
            cmd.extend(["-qsv_device", device])

    cmd.extend(["-f", "null", "-"])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=15,
            creationflags=_CREATIONFLAGS,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _list_nvenc_devices(ffmpeg_exe: str, encoder_name: str = "hevc_nvenc") -> List[Tuple[str, str]]:
    """Enumerate CUDA devices NVENC sees, via `-gpu list` (docs/technical/components/compression-mechanism.md
    fact: prints to stderr, one line per GPU). Returns [(index, name), ...],
    possibly empty when the output doesn't parse (no GPU, driver missing,
    or a future ffmpeg changing the log line) -- callers fall back to a
    single generic device rather than treating this as "no hardware"."""
    cmd = [
        ffmpeg_exe, "-y", "-hide_banner",
        "-f", "lavfi", "-i", f"testsrc=duration=0.04:size={_TEST_ENCODE_FRAME_SIZE}:rate=25",
        "-frames:v", "1",
        "-c:v", encoder_name, "-gpu", "list",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
            creationflags=_CREATIONFLAGS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
# See: docs/technical/components/compression-mechanism.md
    output = (result.stdout or "") + (result.stderr or "")
    return [(m.group(1), m.group(2).strip()) for m in _NVENC_GPU_LIST_RE.finditer(output)]


def _detect_intel_gpu_name(timeout: int = 5) -> Optional[str]:
    """Best-effort Intel GPU model name for QSV's display_name/device_key,
    via Win32_VideoController -- offline, local WMI/CIM only, never
    blocks detection when it fails or times out (docs/technical/components/compression-mechanism.md: "A missing
    name must not block detection")."""
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-CimInstance Win32_VideoController).Name",
            ],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_CREATIONFLAGS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if "intel" in line.casefold():
            return line
    return None


def _make_display_name(encoder: str, device_name: Optional[str] = None) -> str:
    """Human-readable display name for an encoder, optionally with its
    device: "HEVC / NVENC (NVIDIA GeForce RTX 4060)"."""
    names = {
        "libx265": "x265 (CPU)",
        "libsvtav1": "AV1 / SVT (CPU)",
        "hevc_nvenc": "HEVC / NVENC",
        "av1_nvenc": "AV1 / NVENC",
        "hevc_qsv": "HEVC / Quick Sync",
        "av1_qsv": "AV1 / Quick Sync",
    }
    base = names.get(encoder, encoder)
    if device_name:
        return f"{base} ({device_name})"
    return base


def _device_key(encoder: str, device_name: Optional[str]) -> str:
    """Calibration identity for one encoder+device combination (see
    EncoderInfo's own docstring). A hardware encoder whose device name is
    unknown still gets a usable (if less specific) key, same as CPU."""
    if device_name:
        return f"{encoder}@{device_name}"
    return encoder


def get_machine_id() -> str:
    """Stable identifier for this machine, used to key calibration data.
    Combines hostname + CPU name so the same user on two machines gets
    separate calibration entries. Unchanged by device_key's introduction
    (docs/technical/components/compression-mechanism.md): renaming it would orphan every calibration row
    already on disk."""
    node = platform.node()
    cpu = platform.processor() or "unknown-cpu"
    return f"{node}/{cpu}"


def _detect_nvenc_variant(ffmpeg_exe: str, encoder_name: str, listed: set) -> List[EncoderInfo]:
    """Every working device for one NVENC encoder name (hevc_nvenc or
    av1_nvenc). Each candidate GPU from `-gpu list` gets its OWN init test
    with `-gpu <n>` -- a GPU present in CUDA's device list is not proof
    that particular encoder (e.g. av1_nvenc on a pre-Ada card) works on
    it (docs/technical/components/compression-mechanism.md)."""
    if encoder_name not in listed:
        return []
    devices = _list_nvenc_devices(ffmpeg_exe, encoder_name)
    out: List[EncoderInfo] = []
    if devices:
        for index, name in devices:
            if _test_encoder(ffmpeg_exe, encoder_name, device=index):
                out.append(EncoderInfo(
                    name=encoder_name,
                    display_name=_make_display_name(encoder_name, name or None),
                    device=index,
                    is_hardware=True,
                    device_key=_device_key(encoder_name, name or None),
                ))
        return out
# See: docs/technical/components/compression-mechanism.md
    if _test_encoder(ffmpeg_exe, encoder_name, device="0"):
        out.append(EncoderInfo(
            name=encoder_name,
            display_name=_make_display_name(encoder_name, _NVENC_GENERIC_DEVICE_NAME),
            device="0",
            is_hardware=True,
            device_key=_device_key(encoder_name, _NVENC_GENERIC_DEVICE_NAME),
        ))
    return out


def _detect_qsv_variant(ffmpeg_exe: str, encoder_name: str, listed: set) -> List[EncoderInfo]:
    """QSV: one device, the default adapter -- no per-GPU enumeration
    (docs/technical/components/compression-mechanism.md). A missing/unrecognized card name still yields an
    entry, keyed by the bare encoder name like a CPU encoder."""
    if encoder_name not in listed:
        return []
    if not _test_encoder(ffmpeg_exe, encoder_name):
        return []
    device_name = _detect_intel_gpu_name()
    return [EncoderInfo(
        name=encoder_name,
        display_name=_make_display_name(encoder_name, device_name),
        device="",
        is_hardware=True,
        device_key=_device_key(encoder_name, device_name),
    )]


def detect(
    config_json_path: Optional[str] = None,
) -> Optional[HwDetectResult]:
    """Full hardware detection. Returns None only when ffmpeg/ffprobe is not
    found; raises probe.ToolBlockedError when either cannot be launched
    (Smart App Control).

    Never persisted to disk: kept in process memory only
    (tidy/review/state.py's `state.hwdetect_cache`).
    See: docs/technical/components/compression-mechanism.md"""
    ffmpeg_exe = _resolve_ffmpeg_exe(config_json_path)
    if ffmpeg_exe is None:
        log.warning("ffmpeg not found -- compression unavailable")
        return None

    ffprobe_exe = _resolve_ffprobe_exe(ffmpeg_exe)
    if ffprobe_exe is None:
        log.warning("ffprobe not found alongside ffmpeg -- compression unavailable")
        return None

    version = _get_ffmpeg_version(ffmpeg_exe)
    _check_launchable(ffprobe_exe)
    listed = set(_list_encoders(ffmpeg_exe))
    machine_id = get_machine_id()

    encoders: List[EncoderInfo] = []

    # CPU encoders -- just check if listed (they don't need HW init)
    for name in ("libx265", "libsvtav1"):
        if name in listed:
            encoders.append(EncoderInfo(
                name=name,
                display_name=_make_display_name(name),
                device="",
                is_hardware=False,
                device_key=_device_key(name, None),
            ))

    # HW encoders -- must pass the per-device init test at
    # _TEST_ENCODE_FRAME_SIZE, never the encoder list alone (docs/technical/components/compression-mechanism.md).
    encoders.extend(_detect_nvenc_variant(ffmpeg_exe, "hevc_nvenc", listed))
    encoders.extend(_detect_nvenc_variant(ffmpeg_exe, "av1_nvenc", listed))
    encoders.extend(_detect_qsv_variant(ffmpeg_exe, "hevc_qsv", listed))
    encoders.extend(_detect_qsv_variant(ffmpeg_exe, "av1_qsv", listed))

    return HwDetectResult(
        ffmpeg_exe=ffmpeg_exe,
        ffprobe_exe=ffprobe_exe,
        ffmpeg_version=version,
        machine_id=machine_id,
        encoders=tuple(encoders),
    )
