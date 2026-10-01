"""--compress mode: candidate scan, plan summary, encode and land results."""
from __future__ import annotations

import argparse
import os
from typing import Dict, List, Optional, Tuple

from tidy.store.cache import Cache
from tidy.fs.fsmodel import FileSystem
from tidy.moves import applier
from tidy.compression import compress
from tidy import config
from tidy.compression import encoder
from tidy.compression import hwdetect
from tidy.moves import manifest
from tidy.compression import probe
from tidy.duplicates import report
from tidy.duplicates import scanner
from tidy.cli.common import _format_apply_summary, _resolve_archive_root, _resolve_roots


def _compress_candidates(
    fs: FileSystem,
    roots: List[str],
    exclude: Optional[List[str]],
    ffprobe_exe: str,
    cache: Cache,
    bpp_thresholds: Dict[str, float],
) -> Tuple[List[compress.CompressCandidate], List[compress.CompressSkipped]]:
    """Scan `roots` and split every video file into
    compress.evaluate_file()'s candidate/skipped lists. Recursive and
    unrestricted by is_duplicate_candidate (only the hard-excluded zones
    scanner.scan() already prunes apply; docs/technical/components/review-ui.md) -- offline (cloud-placeholder) and hardlink-of files
    are skipped before ever probing: probing an offline file would force a
    cloud download just to look at it, and compressing one name of a
    multiply-hardlinked file would silently orphan its other names.
    """
    scan_result = scanner.scan(fs, roots, extra_excluded_dir_names=exclude)
    candidates: List[compress.CompressCandidate] = []
    skipped: List[compress.CompressSkipped] = []
    for f in scan_result.files:
        if f.exclusion_reason in ("offline", "hardlink-of"):
            continue
        if os.path.splitext(f.path)[1].casefold() not in config.VIDEO_EXTENSIONS_CASEFOLD:
            continue
        probe_result = probe.probe_file_cached(
            ffprobe_exe, f.path, f.stat.size, f.stat.mtime_ns, cache=cache,
        )
        candidate, skip = compress.evaluate_file(
            f.path, f.stat.size, probe_result, fs, bpp_thresholds=bpp_thresholds,
        )
        if candidate is not None:
            candidates.append(candidate)
        else:
            skipped.append(skip)
    return candidates, skipped


def _format_compress_plan_summary(
    candidates: List[compress.CompressCandidate],
    skipped: List[compress.CompressSkipped],
    cfg: compress.CompressConfig,
) -> str:
    lines = [
        "=== Compression suggestion (--compress) ===",
        f"Candidates: {len(candidates)}, skipped: {len(skipped)}",
    ]
    total_original = 0
    total_estimated = 0
    for c in candidates:
        estimates = compress.estimate_all_profiles(c.probe, c.size, {}, profiles=cfg.profiles)
        est = next((e for e in estimates if e.profile_name == cfg.default_profile), None)
        total_original += c.size
        est_txt = f"~{est.estimated_size} B" if est and est.estimated_size else "?"
        lines.append(f"  {report.display_path(c.path)}: {c.size} B -> {est_txt} ({c.reason})")
        if est:
            total_estimated += est.estimated_size
    if candidates:
        lines.append(
            f"Total (default profile '{cfg.default_profile}'): "
            f"{total_original} B -> ~{total_estimated} B"
        )
    return "\n".join(lines)


def _run_compress(args: argparse.Namespace, fs: FileSystem) -> int:
    """--compress entry point: a Smart App Control block (ToolBlockedError)
    from any stage aborts with one ASCII line and exit code 1."""
    try:
        return _run_compress_stages(args, fs)
    except probe.ToolBlockedError as exc:
        # The message embeds the exe path, which may hold non-cp1250 characters.
        print("Error: " + str(exc).encode("ascii", "backslashreplace").decode("ascii"))
        return 1


def _run_compress_stages(args: argparse.Namespace, fs: FileSystem) -> int:
    """--compress: list recompression candidates (plan-only), or with
    --apply, actually encode + verify + move each result into the
    archive, optionally moving verified originals to !REMOVE as a
    separate batch (--compress-remove-originals).
    See: docs/business/rules/compression.md, docs/technical/components/manifest-and-db.md

    Structural safety: `removals` below is built only
    from apply_compressions()'s own ApplyResult.entries filtered to
    status=manifest.STATUS_DONE, never from `plans` (the request list) --
    a failed encode or a failed move can never reach
    apply_compress_removals(), which is the point.
    """
    cfg = compress.load_compress_config(args.keeper_config)
    archive_root_resolved = _resolve_archive_root(args)

    hw = hwdetect.detect(args.keeper_config)
    if hw is None:
        print("Error: ffmpeg/ffprobe not found -- compression unavailable.")
        return 1

    cache = Cache(args.db)
    try:
        try:
            candidates, skipped = _compress_candidates(
                fs, _resolve_roots(args), args.exclude, hw.ffprobe_exe, cache, cfg.bpp_thresholds,
            )
        except scanner.RemoveDirRefusedError as exc:
            print(str(exc))
            return 1
    finally:
        cache.close()

    if not getattr(args, "apply", False):
        print(_format_compress_plan_summary(candidates, skipped, cfg))
        return 0

    profile = cfg.profiles.get(cfg.default_profile)
    if profile is None:
        print(f"Error: unknown compression profile '{cfg.default_profile}'.")
        return 1
    available_encoders = {e.name for e in hw.encoders}
    if profile["encoder"] not in available_encoders:
        print(
            f"Error: encoder '{profile['encoder']}' for profile "
            f"'{cfg.default_profile}' is not available on this machine."
        )
        return 1

    # --compress-device is an index into THIS encoder's own detected
    # devices (hw.encoders filtered to profile["encoder"], in hwdetect.py's
    # detection order) -- never a raw GPU index, and never resolved any
    # other way (docs/technical/components/compression-mechanism.md's "wylacznie przez liste hw.encoders").
    device_entry = None
    matching_devices = [e for e in hw.encoders if e.name == profile["encoder"]]
    if matching_devices and matching_devices[0].is_hardware:
        device_index = getattr(args, "compress_device", None) or 0
        if device_index < 0 or device_index >= len(matching_devices):
            print(
                f"Error: --compress-device {device_index} out of range "
                f"(available: 0..{len(matching_devices) - 1}) for profile "
                f"'{cfg.default_profile}'."
            )
            return 1
        device_entry = matching_devices[device_index]
    device = device_entry.device if device_entry is not None else ""
    device_key = device_entry.device_key if device_entry is not None else ""

    encoder.ensure_staging_dir(args.compress_staging_dir)

    plans: List[applier.CompressPlan] = []
    for idx, candidate in enumerate(candidates, start=1):
        staged = encoder.staging_path(args.compress_staging_dir, str(idx))
        cmd = encoder.build_command(
            hw.ffmpeg_exe, candidate.path, staged, profile, candidate.probe, device=device,
        )
        try:
            src_mtime_ns = fs.stat(candidate.path).mtime_ns
        except (OSError, PermissionError):
            src_mtime_ns = None
        encode_result = encoder.run_encode(
            cmd, duration=candidate.probe.duration, src_mtime_ns=src_mtime_ns,
        )
        if not encode_result.success:
            print(f"  ENCODING FAILED: {report.display_path(candidate.path)} -- {encode_result.reason}")
            continue
        verify_result = encoder.verify_output(
            hw.ffmpeg_exe, hw.ffprobe_exe, candidate.path, encode_result.staging_path,
            candidate.probe, verify_xpsnr=cfg.verify_xpsnr, min_xpsnr=cfg.min_xpsnr,
        )
        if not verify_result.ok:
            print(f"  VERIFICATION FAILED: {report.display_path(candidate.path)} -- {verify_result.reason}")
            continue
        plans.append(applier.CompressPlan(
            original_abs=candidate.path, staging_abs=encode_result.staging_path,
            dst_abs=compress.compressed_dst_path(candidate.path, cfg.suffix), group_id=str(idx),
            reason=compress.manifest_reason(cfg.default_profile, profile, device_key),
        ))

    if not plans:
        print("=== Compression (--apply) ===")
        print("No verified results to move.")
        return 0

    compress_result = applier.apply_compressions(fs, archive_root_resolved, plans)
    print("=== Compression (--apply): results moved to archive ===")
    print(_format_apply_summary(compress_result))

    if getattr(args, "compress_remove_originals", False):
        removals = [
            applier.CompressRemoval(original_abs=entry.src_abs, compressed_abs=entry.dst_abs)
            for entry in compress_result.entries
            if entry.status == manifest.STATUS_DONE
        ]
        if removals:
            removal_result = applier.apply_compress_removals(fs, archive_root_resolved, removals)
            print("=== Originals moved to !REMOVE (--compress-remove-originals) ===")
            print(_format_apply_summary(removal_result))

    return 0
