"""Compression candidacy selection, heuristics, and size/time estimation.

Pure logic module -- no subprocess calls, no disk I/O beyond what
the injected `fs` seam provides.  All probe data arrives as
`probe.ProbeResult` objects; this module never calls ffprobe itself.
# See: docs/business/rules/compression.md
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from tidy import config
from tidy.compression.probe import ProbeResult
from tidy.compression.hwdetect import EncoderInfo


# See: docs/technical/components/compression-mechanism.md

@dataclass(frozen=True)
class CompressCandidate:
    """A file that passed the heuristic and is worth compressing."""
    path: str
    size: int
    probe: ProbeResult
    reason: str  # human-readable justification


@dataclass(frozen=True)
class CompressSkipped:
    """A file that was considered but rejected by the heuristic."""
    path: str
    reason: str


def _has_compressed_suffix(path: str) -> bool:
    """True if the file stem ends with _compressed (our own artefact)."""
    # Work on the basename, then strip extension.
    sep = max(path.rfind("/"), path.rfind("\\"))
    name = path[sep + 1:] if sep >= 0 else path
    dot = name.rfind(".")
    stem = name[:dot] if dot > 0 else name
    return stem.casefold().endswith(config.COMPRESSED_SUFFIX.casefold())


def _sibling_compressed_exists(path: str, fs: object) -> bool:
    """True if a file named <stem>_compressed.* exists next to `path`."""
    sep = max(path.rfind("/"), path.rfind("\\"))
    dir_part = path[:sep + 1] if sep >= 0 else ""
    name = path[sep + 1:] if sep >= 0 else path
    dot = name.rfind(".")
    stem = name[:dot] if dot > 0 else name
    # Check the most common case: same extension.
    ext = name[dot:] if dot > 0 else ""
    candidate = f"{dir_part}{stem}{config.COMPRESSED_SUFFIX}{ext}"
    if fs.exists(candidate):
        return True
    return False


def _ascii_only(text: str) -> str:
    """Drop every non-ASCII character. Device names (hwdetect.py's
    EncoderInfo.device_key) come from hardware/driver strings and are not
    guaranteed ASCII; manifest `reason` values can reach the console
    (CLAUDE.md's "console output must be ASCII" rule), so this is applied
    before any device name is embedded in one."""
    return text.encode("ascii", "ignore").decode("ascii")


def resolve_device(
    encoders: Sequence[EncoderInfo],
    encoder_name: str,
    device_key: Optional[str] = None,
) -> Tuple[bool, Optional[EncoderInfo], Optional[str]]:
    """Resolve which EncoderInfo a compress job/apply should use to encode
    with `encoder_name`, from hw.encoders -- the ONLY allowed source (never a
    client-constructed value): an unknown
    key, a key belonging to a different encoder, or any key at all for a
    CPU encoder each reject rather than silently falling back
    (docs/technical/components/compression-mechanism.md). Returns (ok, entry, error):
    - ok=True, entry=None: CPU encoder, no device needed.
    - ok=True, entry=<first or matched device>: hardware encoder.
    - ok=False, entry=None, error=<message>: reject the request outright.

    `device_key=None` (the field omitted) means "first detected device"
    for a hardware encoder -- never an error by itself."""
    matching = [e for e in encoders if e.name == encoder_name]
    if not matching:
        return False, None, f"encoder {encoder_name!r} is not available on this machine"
    if not matching[0].is_hardware:
        if device_key:
            return False, None, f"device_key is not applicable to CPU encoder {encoder_name!r}"
        return True, None, None
    if device_key:
        entry = next((e for e in matching if e.device_key == device_key), None)
        if entry is None:
            return False, None, (
                f"unknown or mismatched device_key {device_key!r} for encoder {encoder_name!r}"
            )
        return True, entry, None
    return True, matching[0], None


def default_profile_for_encoder(encoder: str, profiles: Dict[str, dict]) -> Optional[str]:
    """Which configured profile represents `encoder` for calibration
    purposes (docs/technical/components/compression-mechanism.md: "the default profile of each device").
    Prefers the profile whose config.PROFILE_BPP_SEED_MULTIPLIER is 1.0 --
    the tier config.SEED_CALIBRATION/a measured bpp already represents
    un-scaled (see that dict's own comment); falls back to the first
    configured profile for `encoder` so a newly added encoder with no
    multiplier entry is never silently unresolvable. None when `encoder`
    has no profile in `profiles` at all."""
    candidates = [name for name, p in profiles.items() if p.get("encoder") == encoder]
    if not candidates:
        return None
    for name in candidates:
        if config.PROFILE_BPP_SEED_MULTIPLIER.get(name, 1.0) == 1.0:
            return name
    return candidates[0]


def manifest_reason(profile_name: str, profile: dict, device_key: str = "") -> str:
    """ASCII-safe `reason` text for a CompressPlan's manifest row: encoder,
    profile name, and -- for a hardware profile -- the device (docs/technical/components/compression-mechanism.md
    E3.4). `device_key` is an EncoderInfo.device_key
    ("<encoder>@<device name>"); CPU callers pass "" or the bare encoder
    name, either of which adds nothing here since a CPU row has no device."""
    parts = [profile["encoder"], profile_name]
    if "@" in device_key:
        parts.append(_ascii_only(device_key.split("@", 1)[1]))
    return " ".join(parts)


def compressed_dst_path(path: str, suffix: str) -> str:
    """<original>_compressed.<ext>, next to the original, same extension --
    the only spelling _sibling_compressed_exists() above recognizes.
    Public and shared by every caller that needs to name a compressed
    result before it exists (dedup.py's --compress --apply,
    tidy/review/workers/compress_queue.py's _run_compress_queue()), so the two never drift into
    naming it differently."""
    sep = max(path.rfind("/"), path.rfind("\\"))
    dir_part = path[:sep + 1] if sep >= 0 else ""
    name = path[sep + 1:] if sep >= 0 else path
    dot = name.rfind(".")
    stem = name[:dot] if dot > 0 else name
    ext = name[dot:] if dot > 0 else ""
    return f"{dir_part}{stem}{suffix}{ext}"


def _is_in_excluded_zone(path: str) -> bool:
    """True if path is under !REMOVE, !MOVES, or .dedup."""
    parts_casefold = path.replace("\\", "/").casefold().split("/")
    for part in parts_casefold:
        if part in config.EXCLUDED_DIR_NAMES_CASEFOLD:
            return True
    return False


def _is_video_extension(path: str) -> bool:
    """True if the file extension is in VIDEO_EXTENSIONS_CASEFOLD."""
    dot = path.rfind(".")
    if dot < 0:
        return False
    ext = path[dot:].casefold()
    return ext in config.VIDEO_EXTENSIONS_CASEFOLD


def _bpp_threshold(codec: str, thresholds: Dict[str, float]) -> float:
    """Look up the bpp threshold for `codec`, falling back to 'default'."""
    return thresholds.get(codec.casefold(), thresholds.get("default", 0.12))


def evaluate_file(
    path: str,
    size: int,
    probe_result: Optional[ProbeResult],
    fs: object,
    *,
    bpp_thresholds: Optional[Dict[str, float]] = None,
) -> Tuple[Optional[CompressCandidate], Optional[CompressSkipped]]:
    """Apply the qualification rules (docs/business/rules/compression.md).
    Returns exactly one of
    (candidate, None) or (None, skipped).

    Rules are checked in order; first match wins."""
    if bpp_thresholds is None:
        bpp_thresholds = config.DEFAULT_BPP_THRESHOLDS

    # Rule 1: our own artefact
    if _has_compressed_suffix(path):
        return None, CompressSkipped(path, "already a _compressed artefact")

    # Rule 2: sibling _compressed exists
    if _sibling_compressed_exists(path, fs):
        return None, CompressSkipped(path, "sibling _compressed already exists")

    # Rule 3: not a video
    if not _is_video_extension(path):
        return None, CompressSkipped(path, "not a video extension")

    # Rule 4: excluded zone
    if _is_in_excluded_zone(path):
        return None, CompressSkipped(path, "in excluded zone")

    # Rule 5: probe failed
    if probe_result is None:
        return None, CompressSkipped(path, "probe failed or unsupported")

    # Rule 6: too short
    if probe_result.duration < config.MIN_COMPRESS_DURATION_S:
        return None, CompressSkipped(path, "duration below minimum")

    # Rule 7: intra/uncompressed codec -- always worth it
    if probe_result.codec.casefold() in config.INTRA_CODECS:
        return CompressCandidate(
            path=path, size=size, probe=probe_result,
            reason=f"intra codec ({probe_result.codec})",
        ), None

    # Rule 8: bpp above threshold
    bpp = probe_result.bpp
    threshold = _bpp_threshold(probe_result.codec, bpp_thresholds)
    if bpp > threshold:
        return CompressCandidate(
            path=path, size=size, probe=probe_result,
            reason=f"bpp {bpp:.3f} > threshold {threshold:.3f} for {probe_result.codec}",
        ), None

    # Rule 9: not worth it
    return None, CompressSkipped(
        path, f"bpp {bpp:.3f} <= threshold {threshold:.3f} for {probe_result.codec}",
    )


# See: docs/technical/components/compression-mechanism.md

@dataclass(frozen=True)
class CompressionEstimate:
    """Estimated outcome for one encoder profile on one file."""
    encoder: str        # e.g. "libx265"
    profile_name: str   # e.g. "x265"
    estimated_size: int
    estimated_seconds: float
    calibrated: bool    # False => seed values or unknown
# See: docs/technical/components/compression-mechanism.md
    xpsnr_diff: Optional[float] = None


def estimate_size(
    probe: ProbeResult,
    original_size: int,
    target_bpp: float,
) -> int:
    """Estimated output size in bytes for a given target bpp.
    Clamped to at most 90% of original (docs/technical/components/compression-mechanism.md)."""
    pixels_per_sec = probe.width * probe.height * probe.fps
    if pixels_per_sec <= 0 or probe.duration <= 0:
        return original_size
    estimated = int(target_bpp * pixels_per_sec * probe.duration / 8)
    return min(estimated, int(original_size * 0.9))


def estimate_time(probe: ProbeResult, mpx_per_sec: float) -> float:
    """Estimated encoding time in seconds."""
    if mpx_per_sec <= 0:
        return 0.0
    total_pixels = probe.width * probe.height * probe.fps * probe.duration
    return total_pixels / (mpx_per_sec * 1_000_000)


def _find_calibration_for_encoder(encoder: str, calibration: Dict[str, dict]) -> Optional[dict]:
    """A calibration entry for `encoder`, from a dict now keyed by
    device_key (docs/technical/components/compression-mechanism.md), not bare encoder name: the bare key
    first (CPU's own device_key convention, and a direct hit for a
    single-device machine), then the first "<encoder>@..." device-
    qualified key found. This table's estimate is per PROFILE, not per
    specific device -- which one of several calibrated devices for the
    same encoder feeds it is intentionally unspecified beyond "the first
    one measured"; the "Device" picker (E3.5) is what actually
    chooses a device at apply time."""
    if encoder in calibration:
        return calibration[encoder]
    prefix = f"{encoder}@"
    for key, value in calibration.items():
        if key.startswith(prefix):
            return value
    return None


def estimate_all_profiles(
    probe: ProbeResult,
    original_size: int,
    calibration: Dict[str, dict],
    profiles: Optional[Dict[str, dict]] = None,
) -> List[CompressionEstimate]:
    """Produce estimates for every configured profile.

    `calibration` is keyed by device_key (hwdetect.EncoderInfo.device_key:
    bare encoder name for CPU, "<encoder>@<device name>" for hardware --
    docs/technical/components/compression-mechanism.md) with values {"bpp": float, "mpx_s": float,
    "xpsnr_y": Optional[float]}. Seed values from config are used when no
    calibration entry exists for an encoder; seeds never carry xpsnr_y."""
    if profiles is None:
        profiles = config.DEFAULT_COMPRESS_PROFILES

# See: docs/technical/components/compression-mechanism.md
    baseline_xpsnr = _find_calibration_for_encoder("libx265", calibration)
    baseline_xpsnr_y = baseline_xpsnr.get("xpsnr_y") if baseline_xpsnr else None

    results: List[CompressionEstimate] = []
    for profile_name, profile in profiles.items():
        encoder = profile["encoder"]
        # Resolve bpp and mpx/s: calibration > seed > unknown
        cal = _find_calibration_for_encoder(encoder, calibration)
        seed = config.SEED_CALIBRATION.get(encoder)

# See: docs/technical/components/compression-mechanism.md
        bpp_multiplier = config.PROFILE_BPP_SEED_MULTIPLIER.get(profile_name, 1.0)
        xpsnr_diff: Optional[float] = None
        if cal:
            bpp = cal["bpp"] * bpp_multiplier
            mpx_s = cal["mpx_s"]
            calibrated = True
            cal_xpsnr_y = cal.get("xpsnr_y")
            if cal_xpsnr_y is not None and baseline_xpsnr_y is not None:
                xpsnr_diff = cal_xpsnr_y - baseline_xpsnr_y
        elif seed:
            bpp = seed["bpp"] * bpp_multiplier
            mpx_s = seed["mpx_s"]
            calibrated = False
        else:
            # Unknown encoder -- cannot estimate
            results.append(CompressionEstimate(
                encoder=encoder,
                profile_name=profile_name,
                estimated_size=0,
                estimated_seconds=0.0,
                calibrated=False,
            ))
            continue

        est_size = estimate_size(probe, original_size, bpp)
        est_time = estimate_time(probe, mpx_s)
        results.append(CompressionEstimate(
            xpsnr_diff=xpsnr_diff,
            encoder=encoder,
            profile_name=profile_name,
            estimated_size=est_size,
            estimated_seconds=est_time,
            calibrated=calibrated,
        ))

    return results


# See: docs/technical/components/compression-mechanism.md

@dataclass(frozen=True)
class CompressConfig:
    default_profile: str = "x265"
    default_source: str = config.NEW_DIR_NAME
    suffix: str = config.COMPRESSED_SUFFIX
    parallel_jobs: int = 1
    verify_xpsnr: bool = True
    min_xpsnr: float = 38.0
    # Used only when the user explicitly accepts slightly lower quality for
    # that apply, and still enforced (never skipped outright).
    # See: docs/business/rules/compression.md
    min_xpsnr_relaxed: float = 36.0
    bpp_thresholds: Dict[str, float] = field(
        default_factory=lambda: dict(config.DEFAULT_BPP_THRESHOLDS)
    )
    profiles: Dict[str, dict] = field(
        default_factory=lambda: {k: dict(v) for k, v in config.DEFAULT_COMPRESS_PROFILES.items()}
    )


def _warn_bad_compress_config(path: Optional[str], message: str) -> None:
# See: docs/technical/components/compression-mechanism.md
    location = path if path is not None else "<config.json>"
    print(f"Warning: {location}: compression.{message} -- using the default value.")


def _coerce_str(data: dict, key: str, default: str, path: Optional[str]) -> str:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, str):
        return value
    _warn_bad_compress_config(path, f"'{key}' should be a string")
    return default


def _coerce_bool(data: dict, key: str, default: bool, path: Optional[str]) -> bool:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool):
        return value
    _warn_bad_compress_config(path, f"'{key}' should be a boolean")
    return default


def _coerce_float(data: dict, key: str, default: float, path: Optional[str]) -> float:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    _warn_bad_compress_config(path, f"'{key}' should be a number")
    return default


def _coerce_int(data: dict, key: str, default: int, path: Optional[str]) -> int:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    _warn_bad_compress_config(path, f"'{key}' should be an integer")
    return default


def _coerce_bpp_thresholds(
    data: dict, key: str, default: Dict[str, float], path: Optional[str]
) -> Dict[str, float]:
    if key not in data:
        return dict(default)
    value = data[key]
    if isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)
        for k, v in value.items()
    ):
        return {k: float(v) for k, v in value.items()}
    _warn_bad_compress_config(path, f"'{key}' should be an object {{codec: number}}")
    return dict(default)


def _coerce_profiles(
    data: dict, key: str, default: Dict[str, dict], path: Optional[str]
) -> Dict[str, dict]:
    if key not in data:
        return {k: dict(v) for k, v in default.items()}
    value = data[key]
    if isinstance(value, dict) and all(
        isinstance(v, dict) and "encoder" in v for v in value.values()
    ):
        return {k: dict(v) for k, v in value.items()}
    _warn_bad_compress_config(path, f"'{key}' should be an object of profiles with an 'encoder' field")
    return {k: dict(v) for k, v in default.items()}


def compress_config_from_dict(data: dict, path: Optional[str] = None) -> CompressConfig:
    """Build a CompressConfig from the parsed "compression" object,
    field by field -- a wrong-typed single field falls back to that
    field's default rather than raising, matching
    load_compress_config()'s promise (a malformed file is forgiven)."""
    defaults = CompressConfig()
    return CompressConfig(
        default_profile=_coerce_str(data, "default_profile", defaults.default_profile, path),
        default_source=_coerce_str(data, "default_source", defaults.default_source, path),
        suffix=_coerce_str(data, "suffix", defaults.suffix, path),
        parallel_jobs=_coerce_int(data, "parallel_jobs", defaults.parallel_jobs, path),
        verify_xpsnr=_coerce_bool(data, "verify_xpsnr", defaults.verify_xpsnr, path),
        min_xpsnr=_coerce_float(data, "min_xpsnr", defaults.min_xpsnr, path),
        min_xpsnr_relaxed=_coerce_float(
            data, "min_xpsnr_relaxed", defaults.min_xpsnr_relaxed, path
        ),
        bpp_thresholds=_coerce_bpp_thresholds(
            data, "bpp_thresholds", config.DEFAULT_BPP_THRESHOLDS, path
        ),
        profiles=_coerce_profiles(data, "profiles", config.DEFAULT_COMPRESS_PROFILES, path),
    )


def load_compress_config(path: Optional[str]) -> CompressConfig:
    """Load the "compression" section of config.json at `path`. Missing
    file, unreadable file, malformed JSON, a non-object JSON, or a
    non-object "compression" value all fall back to built-in defaults
    (per-field where possible) -- same forgiving contract as
    keeper.load_keeper_config(), deliberately not shared code with it
    (each module owns its own slice of config.json, mirroring the
    project's existing keeper.py/events.py split)."""
    if path is None:
        return CompressConfig()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return CompressConfig()
    except ValueError:
        _warn_bad_compress_config(path, "invalid JSON")
        return CompressConfig()
    if not isinstance(data, dict):
        _warn_bad_compress_config(path, "expected a JSON object ({...})")
        return CompressConfig()
    section = data.get("compression", {})
    if not isinstance(section, dict):
        _warn_bad_compress_config(path, "'compression' should be an object")
        section = {}
    return compress_config_from_dict(section, path=path)
