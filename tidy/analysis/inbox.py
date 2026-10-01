"""`!NEW` inbox triage.

Ties together three already-existing engines against the `!NEW` subset of
one ordinary archive-wide scan, in this order:

  1. Dedup vs the whole archive -- reuses tiers.find_duplicates() +
     keeper.select_keeper() unchanged (rule 0 already discards every !NEW
     copy that duplicates an archive copy; see keeper.is_new_path). This
     module only *filters* the resulting decisions down to groups that
     touch !NEW, for display.
  2. Series (visual similarity) within !NEW's surviving files -- reuses
     series.find_series() unchanged, scoped to files under !NEW that
     step 1 didn't already discard.
  3. Placement suggestions -- for each top-level folder directly under
     `!NEW`, classifies it "coherent" (its media files form exactly one
     event -> suggest moving the whole folder) or "mixed" (more than one
     event -> suggest moving each event's files separately, never the
     folder as a whole). Loose files directly in `!NEW` (no subfolder)
     are always treated like a mixed folder's contents: one suggestion
     per event, never a "whole folder" move, since there is no folder to
     move.

Non-media files (`.txt`, `.zip`, `.pdf`, RAW -- anything not in
config.PHOTO_EXTENSIONS_CASEFOLD/VIDEO_EXTENSIONS_CASEFOLD) are left
untouched and listed separately as "needs a manual decision" -- this
module never proposes moving them. Folders that already contain zero
files, or that step 1's dedup suggestions would empty entirely if
accepted, are reported (never deleted) -- `!NEW` folders have been
observed emptying out on their own between sessions, so this is surfaced
explicitly rather than assumed away.

Like series.py/events.py, this module only ever proposes a plan -- it
never touches the filesystem beyond the read-only FileSystem seam.
"""
# See: docs/business/rules/inbox.md
# Placement rules: docs/business/rules/version-selection.md
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from tidy import config
from tidy.analysis import events as events_module
from tidy.duplicates import keeper
from tidy.analysis import series as series_module
from tidy.duplicates import tiers
from tidy.store.cache import Cache
from tidy.fs.fsmodel import FileSystem
from tidy.progress import DEFAULT_WORKERS, ProgressCounter
from tidy.duplicates.report import display_path, ensure_parent_dir
from tidy.duplicates.scanner import ScannedFile

# Folder classification kinds.
KIND_COHERENT = "coherent"  # exactly one event -> suggest moving the whole folder
KIND_MIXED = "mixed"  # more than one event -> per-event suggestions, no whole-folder move
KIND_LOOSE = "loose"  # files directly in !NEW, no subfolder -- always per-event
KIND_NO_MEDIA = "no-media"  # folder has files but none are photo/video

# See: docs/business/rules/inbox.md
DEFAULT_DUMP_FOLDER_NAMES: Tuple[str, ...] = (
    "Camera", "Baseus", "DCIM", "Download", "100ANDRO", "Nowy folder",
)


def load_dump_folder_names(path: Optional[str]) -> Tuple[str, ...]:
    """Load the optional "dump_folder_names" list from config.json. Same
    graceful-degradation contract as keeper.load_keeper_config() and
    events.load_camera_offsets(): missing file/key, malformed JSON, or a
    wrong-typed value falls back to DEFAULT_DUMP_FOLDER_NAMES with a printed
    warning rather than raising -- a typo here must never abort a scan, and
    must not be silent either, since the fallback changes which folders get
    offered as a single move."""
    if path is None:
        return DEFAULT_DUMP_FOLDER_NAMES
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return DEFAULT_DUMP_FOLDER_NAMES
    except ValueError:
        print(f"Warning: {path}: invalid JSON -- using default dump_folder_names.")
        return DEFAULT_DUMP_FOLDER_NAMES
    if not isinstance(data, dict):
        return DEFAULT_DUMP_FOLDER_NAMES
    raw = data.get("dump_folder_names")
    if raw is None:
        return DEFAULT_DUMP_FOLDER_NAMES
    if isinstance(raw, list) and all(isinstance(v, str) for v in raw):
        return tuple(raw)
    print(f"Warning: {path}: 'dump_folder_names' should be a list of strings "
          "-- using the default value.")
    return DEFAULT_DUMP_FOLDER_NAMES

# Sentinel key for the "loose files directly under !NEW" group.
LOOSE_KEY = ""


def _segments(path: str) -> List[str]:
    normalized = path.replace("\\", "/")
    return [seg for seg in normalized.split("/") if seg]


def _new_relative_segments(path: str) -> Optional[List[str]]:
    """Segments of `path` after its first `!NEW` segment (casefold), or
    None if `path` has no `!NEW` segment at all. Deliberately independent
    of any particular archive-root/new-root string -- like keeper.is_new_path,
    it just looks for the sentinel segment anywhere in the path, so it
    works the same regardless of which --roots produced this ScannedFile."""
    new_cf = config.NEW_DIR_NAME.casefold()
    segs = _segments(path)
    for idx, seg in enumerate(segs):
        if seg.casefold() == new_cf:
            return segs[idx + 1:]
    return None


def _extension_of(path: str) -> str:
    segs = _segments(path)
    basename = segs[-1] if segs else path
    if "." not in basename:
        return ""
    return f".{basename.rsplit('.', 1)[-1].casefold()}"


def _is_media(path: str) -> bool:
    ext = _extension_of(path)
    return ext in config.PHOTO_EXTENSIONS_CASEFOLD or ext in config.VIDEO_EXTENSIONS_CASEFOLD


def _passes_filters(
    f: ScannedFile, min_size: Optional[int], extensions_casefold: Optional[frozenset],
) -> bool:
    if min_size is not None and f.stat.size < min_size:
        return False
    if extensions_casefold is not None and _extension_of(f.path) not in extensions_casefold:
        return False
    return True


def _group_new_files_by_top_folder(files: Sequence[ScannedFile]) -> Dict[str, List[ScannedFile]]:
    """Group files under `!NEW` by their top-level `!NEW` subfolder name;
    files directly in `!NEW` (no subfolder) land under LOOSE_KEY. Files
    with no `!NEW` segment at all (shouldn't happen for an already-filtered
    caller, but defensive) are silently skipped."""
    groups: Dict[str, List[ScannedFile]] = {}
    for f in files:
        rel = _new_relative_segments(f.path)
        if not rel:
            continue
        key = rel[0] if len(rel) > 1 else LOOSE_KEY
        groups.setdefault(key, []).append(f)
    return groups


def _list_existing_new_subdirs(fs: FileSystem, new_root: str) -> List[str]:
    try:
        entries = fs.scandir(new_root)
    except OSError:
        return []
    return [e.name for e in entries if e.stat is not None and e.stat.is_dir]


# --- placement suggestions ----------------------------------------------------

@dataclass(frozen=True)
class PlacementSuggestion:
    suggested_folder: str
    matched_existing_dir: bool
    paths: Tuple[str, ...]
# See: docs/business/rules/inbox.md
    photo_count: int = 0
    video_count: int = 0


@dataclass(frozen=True)
class FolderClassification:
    name: str  # top-level !NEW subfolder name, or "" for loose files
    classification: str  # KIND_COHERENT | KIND_MIXED | KIND_LOOSE | KIND_NO_MEDIA
    suggestions: Tuple[PlacementSuggestion, ...]


def _classify_group(
    fs: FileSystem,
    name: str,
    group_files: Sequence[ScannedFile],
    archive_root: str,
    *,
    camera_offsets: Dict[str, timedelta],
    event_gap_hours: float,
    gps_jump_km: float,
    max_workers: int = DEFAULT_WORKERS,
    dump_folder_names: Sequence[str] = DEFAULT_DUMP_FOLDER_NAMES,
    progress: Optional[ProgressCounter] = None,
) -> Tuple[FolderClassification, List[str], "events_module.EventsResult"]:
    """Returns (classification, non_media_paths, events_result) for one
    top-level group (a real subfolder, or the LOOSE_KEY pseudo-group).

    The raw EventsResult comes back alongside the classification because it
    carries the camera-clock warnings: a file whose camera
    clock has drifted lands in someone else's event and inherits their
    location, so "this model has suspicious dates" has to reach the user
    rather than being dropped on the floor by the layer that only wanted
    the folder classification."""
    media_files = [f for f in group_files if _is_media(f.path)]
    non_media_paths = [f.path for f in group_files if not _is_media(f.path)]

    if not media_files:
        return (
            FolderClassification(name=name, classification=KIND_NO_MEDIA, suggestions=()),
            non_media_paths,
            events_module.EventsResult(),
        )

    is_loose = name == LOOSE_KEY
# See: docs/business/rules/inbox.md
    is_dump = name.casefold() in {n.casefold() for n in dump_folder_names}
    name_hint = None if (is_loose or is_dump) else name

    events_result = events_module.find_events(
        fs, media_files, archive_root,
        gap_hours=event_gap_hours, gps_jump_km=gps_jump_km, camera_offsets=camera_offsets,
        max_workers=max_workers, name_hint=name_hint, progress=progress,
    )

    if not is_loose and not is_dump and len(events_result.events) == 1:
        ev = events_result.events[0]
        suggestion = PlacementSuggestion(
            suggested_folder=ev.suggested_folder, matched_existing_dir=ev.matched_existing_dir,
            paths=tuple(m.path for m in ev.members),
            photo_count=ev.photo_count, video_count=ev.video_count,
        )
        return (
            FolderClassification(name=name, classification=KIND_COHERENT, suggestions=(suggestion,)),
            non_media_paths,
            events_result,
        )

    kind = KIND_LOOSE if is_loose else KIND_MIXED
    suggestions = tuple(
        PlacementSuggestion(
            suggested_folder=ev.suggested_folder, matched_existing_dir=ev.matched_existing_dir,
            paths=tuple(m.path for m in ev.members),
            photo_count=ev.photo_count, video_count=ev.video_count,
        )
        for ev in events_result.events
    )
    return (
        FolderClassification(name=name, classification=kind, suggestions=suggestions),
        non_media_paths,
        events_result,
    )


def group_files_by_subfolder(files: Sequence[ScannedFile], source_abs: str) -> Dict[str, List[ScannedFile]]:
    """Group `files` by their immediate subfolder under `source_abs`; files
    sitting directly in `source_abs` land under LOOSE_KEY.

    The generalisation of _group_new_files_by_top_folder() that
    the review screen needs: it can point at ANY directory inside the archive,
    not only `!NEW`, so grouping can no longer key off the `!NEW` sentinel
    segment. Comparison is casefolded and slash-normalised, matching the
    rest of this module's path handling.

    Both sides go through display_path() first, which is what the sentinel
    version never had to care about: scanned paths carry WindowsFileSystem's
    long-path prefix, while `source_abs` is built from a plain archive-root
    string. Segmenting them raw makes every prefix comparison fail, and that
    failure is silent -- every file drops out, every subfolder is then
    reported as empty, and the screen truthfully renders "nothing to
    move" for a directory full of photos."""
    source_segments = [s.casefold() for s in _segments(display_path(source_abs))]
    groups: Dict[str, List[ScannedFile]] = {}
    for f in files:
        segs = _segments(display_path(f.path))
        lowered = [s.casefold() for s in segs]
        if lowered[:len(source_segments)] != source_segments:
            continue
        rel = segs[len(source_segments):]
        if not rel:
            continue
        key = rel[0] if len(rel) > 1 else LOOSE_KEY
        groups.setdefault(key, []).append(f)
    return groups


def classify_source(
    fs: FileSystem,
    source_abs: str,
    files: Sequence[ScannedFile],
    archive_root: str,
    *,
    camera_offsets: Optional[Dict[str, timedelta]] = None,
    event_gap_hours: float = events_module.DEFAULT_EVENT_GAP_HOURS,
    gps_jump_km: float = events_module.DEFAULT_GPS_JUMP_KM,
    max_workers: int = DEFAULT_WORKERS,
    dump_folder_names: Sequence[str] = DEFAULT_DUMP_FOLDER_NAMES,
    progress: Optional[ProgressCounter] = None,
) -> Tuple[List[FolderClassification], List[str], Dict[str, list]]:
    """Placement suggestions for one source directory: step 3 of
    build_inbox_report(), lifted out so the review screen can run it
    against any directory and against a caller-chosen file list.

    `files` is expected to be already narrowed by the caller -- in the
    review server's case, the same candidate pool the other tabs use, minus
    whatever the duplicates tab is about to discard. That subtraction is the
    caller's job precisely because it is the ordering rule this module
    insists on: proposing a home for a file that is on its way to !REMOVE
    wastes the user's attention on a decision that will be thrown away.

    Returns (classifications, non_media_paths, notes), accumulated over
    every subfolder. `notes` carries the camera-clock warnings and the
    suspicious-timestamp list from every per-folder events run: those must
    reach the user, and this is the only layer that sees all of them at
    once. Warnings are de-duplicated -- the same camera model drifting
    shows up once per folder it appears in, and repeating an identical
    warning ten times just trains the reader to skip it.

    `progress` (optional) is the review screen's bar. Every
    media file costs exactly one metadata decode inside find_events(), so the
    total is known before any decoding starts: it is declared here in one go
    and each folder gets a ProgressCounter.phase() that only ticks it off.
    Declaring it up front rather than folder by folder is what keeps the bar
    from re-scaling under the user's eyes on a source directory with many
    subfolders."""
    classifications: List[FolderClassification] = []
    non_media: List[str] = []
    suspicious: List[dict] = []
    drift_warnings: List[str] = []
    seen_warnings: set = set()
    seen_suspicious: set = set()

    groups = group_files_by_subfolder(files, source_abs)
    media_counts = {
        name: sum(1 for f in group_files if _is_media(f.path))
        for name, group_files in groups.items()
    }
    if progress is not None:
        progress.add_total(sum(media_counts.values()))

    for name, group_files in sorted(groups.items()):
        classification, group_non_media, events_result = _classify_group(
            fs, name, group_files, archive_root,
            camera_offsets=camera_offsets or {},
            event_gap_hours=event_gap_hours, gps_jump_km=gps_jump_km,
            max_workers=max_workers, dump_folder_names=dump_folder_names,
            progress=(
                progress.phase(declared=media_counts[name])
                if progress is not None else None
            ),
        )
        classifications.append(classification)
        non_media.extend(group_non_media)
        for warning in events_result.camera_drift_warnings:
            if warning not in seen_warnings:
                seen_warnings.add(warning)
                drift_warnings.append(warning)
        for member in events_result.suspicious:
            if member.path in seen_suspicious:
                continue
            seen_suspicious.add(member.path)
            suspicious.append({
                "path": member.path,
                "when": member.when.isoformat(),
                "camera_model": member.camera_model,
            })

    notes = {"suspicious": suspicious, "camera_drift_warnings": drift_warnings}
    return classifications, non_media, notes


# --- entry point ---------------------------------------------------------------

@dataclass
class InboxReport:
    duplicate_groups: List[Tuple["tiers.DuplicateGroup", keeper.KeeperDecision]] = field(default_factory=list)
    series_result: Optional[series_module.SeriesResult] = None
    folders: List[FolderClassification] = field(default_factory=list)
    empty_folders: List[str] = field(default_factory=list)
    will_be_empty_after_dedup: List[str] = field(default_factory=list)
    skipped_non_media: List[str] = field(default_factory=list)


def build_inbox_report(
    fs: FileSystem,
    scan_result,
    tier_result: "tiers.TierResult",
    decisions: Sequence[keeper.KeeperDecision],
    archive_root: str,
    new_root: str,
    cache: Cache,
    *,
    camera_offsets: Optional[Dict[str, timedelta]] = None,
    event_gap_hours: float = events_module.DEFAULT_EVENT_GAP_HOURS,
    gps_jump_km: float = events_module.DEFAULT_GPS_JUMP_KM,
    series_window_seconds: float = series_module.DEFAULT_SERIES_WINDOW_SECONDS,
    series_hamming_threshold: int = series_module.DEFAULT_HAMMING_THRESHOLD,
    force_recompute: bool = False,
    min_size: Optional[int] = None,
    extensions_casefold: Optional[frozenset] = None,
    max_workers: int = DEFAULT_WORKERS,
    dump_folder_names: Sequence[str] = DEFAULT_DUMP_FOLDER_NAMES,
) -> InboxReport:
    """Build the full triage report from an already-completed
    scan (`scan_result`/`tier_result`/`decisions`, exactly what
    tidy/cli/common.py's `_scan_and_select()` returns for `--roots archive_root`,
    which already covers `!NEW` since it lives inside the archive tree).
    `min_size`/`extensions_casefold` mirror the CLI's --min-size/--ext:
    they narrow which !NEW files are fed to series/placement analysis, the
    same way they already narrow tier_result's own duplicate detection --
    they never affect which folders count as empty/emptied-by-dedup, which
    must always reflect real on-disk contents regardless of those filters.
    Never raises for a single bad file -- every sub-engine it calls
    (series.find_series/events.find_events) already has that contract."""
    camera_offsets = camera_offsets or {}
    report = InboxReport()

    # --- 1. dedup vs archive: keep only groups that touch !NEW at all ----
    for group, decision in zip(tier_result.duplicate_groups, decisions):
        if any(keeper.is_new_path(p) for p in group.paths):
            report.duplicate_groups.append((group, decision))

    to_remove = {
        discard.path
        for _, decision in report.duplicate_groups
        for discard in decision.discarded
    }

    all_new_files = [f for f in scan_result.files if keeper.is_new_path(f.path)]
    all_new_groups = _group_new_files_by_top_folder(all_new_files)

# See: docs/business/rules/inbox.md
    non_candidate_new_files = [f for f in all_new_files if not f.is_duplicate_candidate]
    report.skipped_non_media.extend(
        f.path for f in non_candidate_new_files if f.path not in to_remove
    )

    candidate_new_files = [
        f for f in all_new_files
        if f.is_duplicate_candidate and _passes_filters(f, min_size, extensions_casefold)
    ]
    remaining_new_files = [f for f in candidate_new_files if f.path not in to_remove]
    remaining_groups = _group_new_files_by_top_folder(remaining_new_files)

# See: docs/business/rules/inbox.md
    after_dedup_all_new_files = [f for f in all_new_files if f.path not in to_remove]
    after_dedup_groups = _group_new_files_by_top_folder(after_dedup_all_new_files)

    # --- 2. series within !NEW's surviving files --------------------------
    report.series_result = series_module.find_series(
        fs, remaining_new_files, cache,
        window_seconds=series_window_seconds, hamming_threshold=series_hamming_threshold,
        force_recompute=force_recompute, max_workers=max_workers,
    )

    # --- 3. placement suggestions, per top-level folder / loose files -----
    for name, group_files in sorted(remaining_groups.items()):
        classification, non_media_paths, _events_result = _classify_group(
            fs, name, group_files, archive_root,
            camera_offsets=camera_offsets, event_gap_hours=event_gap_hours, gps_jump_km=gps_jump_km,
            max_workers=max_workers, dump_folder_names=dump_folder_names,
        )
        report.folders.append(classification)
        report.skipped_non_media.extend(non_media_paths)

    # --- empty-folder / will-become-empty reporting -----------------------
    existing_subdirs = _list_existing_new_subdirs(fs, new_root)
    for subdir_name in sorted(existing_subdirs):
        if not all_new_groups.get(subdir_name):
            report.empty_folders.append(subdir_name)
        elif not after_dedup_groups.get(subdir_name):
            report.will_be_empty_after_dedup.append(subdir_name)

    return report


# --- HTML report -----------------------------------------------------------

def _esc(value: str) -> str:
    return html.escape(display_path(value))


def _render_duplicates_section(report: InboxReport) -> str:
    lines = ["<h2>1. Duplicates (already in the archive)</h2>"]
    if not report.duplicate_groups:
        lines.append("<p>No duplicates involving !NEW.</p>")
        return "\n".join(lines)
    lines.append(f"<p>Groups with at least one copy in !NEW: {len(report.duplicate_groups)}</p>")
    lines.append("<ul>")
    for group, decision in report.duplicate_groups:
        lines.append(f"<li>keeper: {_esc(decision.keeper)}<ul>")
        for discard in decision.discarded:
            lines.append(f"<li>to !REMOVE ({html.escape(discard.reason)}): {_esc(discard.path)}</li>")
        lines.append("</ul></li>")
    lines.append("</ul>")
    return "\n".join(lines)


def _render_series_section(report: InboxReport) -> str:
    lines = ["<h2>2. Series in !NEW</h2>"]
    result = report.series_result
    if result is None or not result.groups:
        lines.append("<p>No series detected.</p>")
        return "\n".join(lines)
    lines.append(f"<p>Groups: {len(result.groups)}</p>")
    lines.append("<ul>")
    for group in result.groups:
        lines.append(f"<li>[{html.escape(group.kind)}] suggested keeper: {_esc(group.suggested_keeper)}<ul>")
        for member in group.members:
            marker = "keeper" if member.path == group.suggested_keeper else "for review"
            lines.append(f"<li>({marker}) {_esc(member.path)}</li>")
        lines.append("</ul></li>")
    lines.append("</ul>")
    return "\n".join(lines)


_CLASSIFICATION_LABELS = {
    KIND_COHERENT: "coherent folder -- 1 event",
    KIND_MIXED: "mixed folder -- different events",
    KIND_LOOSE: "loose files in !NEW",
    KIND_NO_MEDIA: "no media files",
}


def _render_placement_section(report: InboxReport) -> str:
    lines = ["<h2>3. Grouping suggestions</h2>"]
    for folder in report.folders:
        display_name = folder.name if folder.name else "(loose files in !NEW)"
        label = _CLASSIFICATION_LABELS.get(folder.classification, folder.classification)
        lines.append(f"<h3>{html.escape(display_name)} -- {html.escape(label)}</h3>")
        if folder.classification == KIND_COHERENT:
            suggestion = folder.suggestions[0]
            existing = "existing folder" if suggestion.matched_existing_dir else "NEW folder"
            lines.append(
                f"<p>Move the whole folder ({len(suggestion.paths)} files, {existing}) -&gt; "
                f"{html.escape(suggestion.suggested_folder)}</p>"
            )
        elif folder.suggestions:
            lines.append("<ul>")
            for suggestion in folder.suggestions:
                existing = "existing folder" if suggestion.matched_existing_dir else "NEW folder"
                lines.append(
                    f"<li>{len(suggestion.paths)} files ({existing}) -&gt; "
                    f"{html.escape(suggestion.suggested_folder)}<ul>"
                )
                for p in suggestion.paths:
                    lines.append(f"<li>{_esc(p)}</li>")
                lines.append("</ul></li>")
            lines.append("</ul>")
        else:
            lines.append("<p>No suggestions (non-media files only).</p>")

    if report.empty_folders:
        lines.append("<h3>Folders already empty (report only, not deleted)</h3><ul>")
        lines.extend(f"<li>!NEW\\{html.escape(name)}</li>" for name in report.empty_folders)
        lines.append("</ul>")

    if report.will_be_empty_after_dedup:
        lines.append("<h3>Folders that become empty once duplicates are accepted</h3><ul>")
        lines.extend(f"<li>!NEW\\{html.escape(name)}</li>" for name in report.will_be_empty_after_dedup)
        lines.append("</ul>")

    if report.skipped_non_media:
        lines.append("<h3>Non-media files (skipped, need a decision)</h3><ul>")
        lines.extend(f"<li>{_esc(p)}</li>" for p in sorted(report.skipped_non_media))
        lines.append("</ul>")

    return "\n".join(lines)


def write_inbox_html(path: str, report: InboxReport) -> None:
    body = "\n".join([
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        "<title>!NEW report</title></head><body>",
        "<h1>!NEW inbox report</h1>",
        _render_duplicates_section(report),
        _render_series_section(report),
        _render_placement_section(report),
        "</body></html>",
    ])
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def format_inbox_summary(report: InboxReport) -> str:
    lines = [
        "=== !NEW inbox (--inbox) ===",
        f"Duplicate groups involving !NEW: {len(report.duplicate_groups)}",
        f"Series in !NEW: {len(report.series_result.groups) if report.series_result else 0}",
        f"Folders/groups to classify: {len(report.folders)}",
    ]
    for folder in report.folders:
        display_name = folder.name if folder.name else "(loose files)"
        label = _CLASSIFICATION_LABELS.get(folder.classification, folder.classification)
        lines.append(f"  - {display_name}: {label} ({len(folder.suggestions)} suggestions)")
    if report.empty_folders:
        lines.append(f"Folders already empty: {', '.join(report.empty_folders)}")
    if report.will_be_empty_after_dedup:
        lines.append(f"Folders that become empty after duplicates: {', '.join(report.will_be_empty_after_dedup)}")
    lines.append(f"Non-media files (skipped): {len(report.skipped_non_media)}")
    return "\n".join(lines)
