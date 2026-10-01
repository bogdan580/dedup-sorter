"""View-model builder and destination candidates for the placement screen."""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from tidy import config
from tidy.analysis import dateindex
from tidy.analysis import inbox
from tidy.duplicates import report
from tidy.media import thumbnails
from tidy.fs.fsmodel import FileSystem
from tidy.review.views.duplicates import thumb_id_for_path


# See: docs/technical/components/review-ui.md

def list_archive_dirs(
    fs: FileSystem, archive_root: str, *, max_depth: int = 2,
) -> List[dict]:
    """Directories inside the archive, at most `max_depth` levels down, each
    with the number of files sitting directly in it.

    Serves both halves of the screen from one walk:
      - the source-directory picker. The client picks a `rel` from this
        server-built list; it never sends a path of its own, so there is no
        traversal to defend against on that side at all.
      - the "existing folder" candidates for a suggestion. The file count
        is what makes the list usable -- "#Home\\2024 Gdansk (402 files)"
        tells you at a glance whether it is the folder you meant.

    Two levels is not an arbitrary cap: it is the shape of this archive
    (`#Home\\2024 Gdansk`, `#Travel\\2023 Mountains`). Tool directories
    (`!REMOVE`, `!MOVES`, `.dedup`, `VIDEO_TS`) are pruned at the top level;
    `!NEW` is deliberately kept, since it is the default source."""
    out: List[dict] = []

    def walk(dir_abs: str, rel_segments: List[str], depth: int) -> None:
        try:
            entries = list(fs.scandir(dir_abs))
        except (OSError, PermissionError):
            return
        file_count = 0
        subdirs = []
        for entry in entries:
            if entry.stat is None:
                continue
            if entry.stat.is_dir:
                subdirs.append(entry)
            else:
                file_count += 1
        if rel_segments:
            out.append({
                "rel": "\\".join(rel_segments),
                "file_count": file_count,
                "depth": len(rel_segments),
            })
        if depth >= max_depth:
            return
        for entry in sorted(subdirs, key=lambda e: e.name.casefold()):
            if depth == 0 and entry.name.casefold() in config.EXCLUDED_DIR_NAMES_CASEFOLD:
                continue
            walk(entry.path, rel_segments + [entry.name], depth + 1)

    walk(archive_root, [], 0)
    return out


def _dest_candidates(
    suggested_folder: str,
    dir_index: Sequence[dict],
    *,
    date_index: Optional[Dict[str, "dateindex.FolderDates"]] = None,
    source_days: Optional[Set[date]] = None,
) -> List[dict]:
    """Existing archive folders offered alongside a suggestion.

    Folders whose contents were taken on the SAME DAYS as these files are
    ranked first (see dateindex.py), ahead of name-based scoping to the
    suggestion's own top-level folder -- falling back to every directory
    when that top-level does not exist yet. The rest of the archive is
    always offered too, flagged `outside`, never hidden: scoping controls
    ordering, not reachability.

    See: docs/business/rules/version-selection.md"""
    top_level = suggested_folder.replace("/", "\\").split("\\")[0]
    if not top_level:
        return []
    prefix = f"{top_level}\\".casefold()
    top_cf = top_level.casefold()
    scoped = [
        {"rel": entry["rel"], "file_count": entry["file_count"]}
        for entry in dir_index
        if entry["rel"].casefold() == top_cf or entry["rel"].casefold().startswith(prefix)
    ]
    def _outside_scope(taken: Set[str]) -> List[dict]:
        """Every remaining archive directory, so nothing is unreachable from
        this suggestion. `!NEW` and its subfolders stay out: they are what
        this screen is emptying, never a destination."""
        return [
            {"rel": entry["rel"], "file_count": entry["file_count"], "outside": True}
            for entry in dir_index
            if entry["rel"].casefold() not in taken
            and entry["rel"].casefold() != config.NEW_DIR_NAME.casefold()
            and not entry["rel"].casefold().startswith(f"{config.NEW_DIR_NAME}\\".casefold())
        ]

    if not scoped:
        scoped = _outside_scope(set())
# See: docs/technical/components/review-ui.md
        for entry in scoped:
            entry.pop("outside", None)
        rest_of_archive: List[dict] = []
    else:
        rest_of_archive = _outside_scope({c["rel"].casefold() for c in scoped})

    if not date_index or not source_days:
        return scoped + rest_of_archive

    matches = dateindex.rank_by_date_overlap(date_index, source_days)
    if not matches:
        return scoped + rest_of_archive

# See: docs/technical/components/review-ui.md
    counts_by_rel = {e["rel"].casefold(): e["file_count"] for e in dir_index}
    matched_cf = {m.rel.casefold() for m in matches}
    promoted = [
        {
            "rel": match.rel,
            "file_count": counts_by_rel.get(match.rel.casefold(), match.total),
            "overlap_files": match.overlap_files,
            "overlap_days": match.overlap_days,
            "why": match.describe(),
        }
        for match in matches
    ]
    rest = [c for c in scoped if c["rel"].casefold() not in matched_cf]
    outside = [c for c in rest_of_archive if c["rel"].casefold() not in matched_cf]
    return promoted + rest + outside


def build_placement_views(
    classifications: Sequence["inbox.FolderClassification"],
    sizes_by_path: Mapping[str, int],
    dir_index: Sequence[dict],
    *,
    date_index: Optional[Dict[str, "dateindex.FolderDates"]] = None,
    days_by_path: Optional[Mapping[str, date]] = None,
    id_prefix: str = "p",
) -> Tuple[List[dict], Dict[str, str], Dict[str, dict]]:
    """View models for the placement screen, plus the thumb_sources and
    `placement_registry` pair the other two screens' builders also produce.

    Unlike events_registry (read only by /api/open), placement_registry IS
    read by an apply endpoint. It is still its own dict, never merged into
    file_registry or series_registry: each apply endpoint resolves ids
    through exactly one registry, so an id from one screen can never be
    moved by another screen's endpoint.

    `id_prefix` is the id NAMESPACE for this one call, and the server passes
    a fresh one per computation (ReviewState.next_placement_id_prefix()).
    The counter below restarts at 1 on every call, so without it every
    source directory would hand out the same `p1`, `p1:0`, ... -- and since
    placement_registry is one dict that each computation `update()`s, the
    ids on a screen the user came back to would resolve to the OTHER
    directory's files. That is not hypothetical: it moved the wrong files,
    silently, with no validation error (the group_id check passes, because
    the group ids collided too). Callers that build one set of views in
    isolation (tests) can leave the default."""
    folders: List[dict] = []
    thumb_sources: Dict[str, str] = {}
    placement_registry: Dict[str, dict] = {}
    suggestion_counter = 0
    existing_rels = {entry["rel"].casefold() for entry in dir_index}

    for folder_idx, classification in enumerate(classifications, start=1):
        suggestions: List[dict] = []
        for suggestion in classification.suggestions:
            suggestion_counter += 1
            group_id = f"{id_prefix}{suggestion_counter}"
            entries: List[dict] = []
            for i, path in enumerate(suggestion.paths):
                file_id = f"{group_id}:{i}"
                size = sizes_by_path.get(path, 0)
                thumb_id = thumb_id_for_path(path) if thumbnails.supports(path) else None
                if thumb_id is not None:
                    thumb_sources[thumb_id] = path
                entries.append({
                    "id": file_id,
                    "path": report.display_path(path),
                    "size": size,
                    "thumb_id": thumb_id,
                    "thumb_kind": thumbnails.kind(path),
                })
                placement_registry[file_id] = {
                    "path": path, "group_id": group_id, "size": size,
                }
            source_days = {
                days_by_path[p] for p in suggestion.paths
                if days_by_path is not None and p in days_by_path
            }
            suggestions.append({
                "group_id": group_id,
                "suggested_folder": suggestion.suggested_folder,
                "matched_existing_dir": suggestion.matched_existing_dir,
# See: docs/technical/components/review-ui.md
                "dest_exists": suggestion.suggested_folder.casefold() in existing_rels,
                # Counts live here, in the UI, instead of inside the
                # directory name where they went stale on the next import.
                "photo_count": suggestion.photo_count,
                "video_count": suggestion.video_count,
                "candidates": _dest_candidates(
                    suggestion.suggested_folder, dir_index,
                    date_index=date_index, source_days=source_days,
                ),
                "entries": entries,
            })

        folders.append({
            "folder_id": f"f{folder_idx}",
            "name": classification.name,
            "classification": classification.classification,
# See: docs/technical/components/review-ui.md
            "whole_folder": classification.classification == inbox.KIND_COHERENT,
            "suggestions": suggestions,
        })

    return folders, thumb_sources, placement_registry
