"""View-model builder for the exact-duplicate groups and the thumbnail id helper."""
from __future__ import annotations

import hashlib
from typing import Dict, List, Sequence, Tuple

from tidy.duplicates import report
from tidy.media import thumbnails
from tidy.duplicates.tiers import DuplicateGroup
from tidy.duplicates.keeper import KeeperDecision


def build_group_views(
    duplicate_groups: Sequence[DuplicateGroup],
    decisions: Sequence[KeeperDecision],
) -> Tuple[List[dict], Dict[str, str], Dict[str, dict]]:
    """Pair `duplicate_groups` with `decisions` exactly like tidy/cli's
    `_select_keepers()`/`_build_rows()` (same order, one decision per
    group) and build JSON-ready view models for the "Exact duplicates"
    screen, plus two server-side registries that request payloads are only
    ever looked up against, never trusted directly:

    - `thumb_id -> source_path`, trusted as an allowlist by
      `GET /api/thumb/<id>`.
    - `file_id -> {"path", "group_id", "size", "hash", "reason"}`, trusted
      as an allowlist by `POST /api/apply`.
      Every file in every group (the keeper and each discard) gets a
      stable id `f"{group_id}:{idx}"` (idx 0 for the keeper, 1.. for
      discards in order) -- mirrors `thumb_sources` exactly: a client only
      ever sends an id, this module resolves it to a real path server-side,
      and an id absent from this dict is rejected rather than silently
      built into a path.

    Each of `views[i]["keeper"]` and `views[i]["discarded"][j]` also carries
    its own `"size"` (same value as the matching `file_registry[id]["size"]`
    above -- an exact-duplicate group means every member is byte-identical
    in size, but the UI shows it per-card, not as one shared group total),
    so the review page can label every card with its own size.

    One thumbnail per group (`group.full_hash` as the id), not per path:
    every path in an exact-duplicate group is byte-identical by definition,
    so any single member (the keeper) is representative."""
    views: List[dict] = []
    thumb_sources: Dict[str, str] = {}
    file_registry: Dict[str, dict] = {}

    for idx, (group, decision) in enumerate(zip(duplicate_groups, decisions), start=1):
        group_id = str(idx)

        thumb_id = group.full_hash if thumbnails.supports(decision.keeper) else None
        if thumb_id is not None:
            thumb_sources[thumb_id] = decision.keeper
# See: docs/technical/components/review-ui.md
        thumb_kind = thumbnails.kind(decision.keeper)

        keeper_id = f"{group_id}:0"
        file_registry[keeper_id] = {
            "path": decision.keeper,
            "group_id": group_id,
            "size": group.size,
            "hash": group.full_hash,
            "reason": None,
        }

        discarded_views = []
        for d_idx, discard in enumerate(decision.discarded, start=1):
            discard_id = f"{group_id}:{d_idx}"
            file_registry[discard_id] = {
                "path": discard.path,
                "group_id": group_id,
                "size": group.size,
                "hash": group.full_hash,
                "reason": discard.reason,
            }
            discarded_views.append({
                "path": report.display_path(discard.path),
                "reason": discard.reason,
                "id": discard_id,
# See: docs/technical/components/review-ui.md
                "size": group.size,
            })

        views.append({
            "group_id": group_id,
            "size": group.size,
            "reclaimable_bytes": group.size * len(decision.discarded),
            "thumb_id": thumb_id,
            "thumb_kind": thumb_kind,
            "keeper": {
                "path": report.display_path(decision.keeper),
                "id": keeper_id,
                "size": group.size,
            },
            "discarded": discarded_views,
        })

    return views, thumb_sources, file_registry


# See: docs/technical/components/review-ui.md
def thumb_id_for_path(path: str) -> str:
    digest = hashlib.blake2b(path.encode("utf-8"), digest_size=16).hexdigest()
    return f"p_{digest}"
