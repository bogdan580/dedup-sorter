"""View-model builder for the compression screen."""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set, Tuple

from tidy.compression import compress
from tidy.duplicates import report
from tidy.media import thumbnails
from tidy.review.views.duplicates import thumb_id_for_path


def build_compress_views(
    candidates: Sequence["compress.CompressCandidate"],
    skipped: Sequence["compress.CompressSkipped"],
    calibration: Dict[str, dict],
    cfg: "compress.CompressConfig",
    *,
    id_prefix: str = "c1_",
    available_encoders: Optional[Set[str]] = None,
) -> Tuple[List[dict], List[dict], Dict[str, str], Dict[str, dict]]:
    """View models for the compress screen, from already-computed
    candidates/skipped (GET /api/compress's background worker scans and
    probes before calling this -- see its docstring for why: this function
    stays pure/synchronous, no subprocess, no disk, unit-testable with
    hand-built CompressCandidate/CompressSkipped and FakeFileSystem).

    Returns (candidates_json, skipped_json, thumb_sources, compress_registry)
    -- the fifth registry, same allowlist contract as placement_registry
    above: a later apply endpoint (not built by this step) resolves ids
    through this dict, and a client-supplied path is never trusted.

    `id_prefix` is this one computation's private namespace
    (ReviewState.next_compress_id_prefix()), so ids from two source
    directories can never collide in the merged compress_registry -- same
    reasoning as build_placement_views()'s own id_prefix, same bug it
    would otherwise reproduce.

    Each candidate is a "group of one" file, unlike placement's multi-file
    suggestions -- the `:0` suffix is kept anyway so every id in this
    module has the same `<prefix><n>:<i>` shape and no registry needs a
    special case to tell them apart.

    `available_encoders` (a set of hwdetect.EncoderInfo.name, i.e.
    {e.name for e in hw.encoders}) restricts which of cfg.profiles get an
    estimate at all: a hardware profile whose encoder this machine cannot
    actually run is never shown, let alone accepted later by
    POST /api/apply-compress (docs/technical/components/compression-mechanism.md). None (the default) means
    "no restriction" -- existing callers that pre-date this parameter keep
    seeing every configured profile."""
    candidates_json: List[dict] = []
    thumb_sources: Dict[str, str] = {}
    compress_registry: Dict[str, dict] = {}

    profiles = cfg.profiles
    if available_encoders is not None:
        profiles = {
            name: p for name, p in profiles.items()
            if p.get("encoder") in available_encoders
        }

    for i, c in enumerate(candidates, start=1):
        group_id = f"{id_prefix}{i}"
        file_id = f"{group_id}:0"
        estimates = compress.estimate_all_profiles(
            c.probe, c.size, calibration, profiles=profiles,
        )
        thumb_id = thumb_id_for_path(c.path) if thumbnails.supports(c.path) else None
        if thumb_id is not None:
            thumb_sources[thumb_id] = c.path
        compress_registry[file_id] = {
            "path": c.path, "group_id": group_id, "size": c.size,
        }
        candidates_json.append({
            "id": file_id,
            "group_id": group_id,
            "path": report.display_path(c.path),
            "size": c.size,
            "reason": c.reason,
            "thumb_id": thumb_id,
            "thumb_kind": thumbnails.kind(c.path),
            "estimates": [
                {
                    "encoder": e.encoder,
                    "profile_name": e.profile_name,
                    "estimated_size": e.estimated_size,
                    "estimated_seconds": e.estimated_seconds,
                    "calibrated": e.calibrated,
                    "xpsnr_diff": e.xpsnr_diff,
                }
                for e in estimates
            ],
        })

    skipped_json = [
        {"path": report.display_path(s.path), "reason": s.reason}
        for s in skipped
    ]

    return candidates_json, skipped_json, thumb_sources, compress_registry
