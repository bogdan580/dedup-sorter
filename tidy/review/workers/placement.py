"""Background placement computation."""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from tidy.analysis import dateindex
from tidy.analysis import inbox
from tidy.duplicates import report
from tidy.store.cache import Cache
from tidy.fs.fsmodel import FileSystem
from tidy.duplicates.scanner import ScannedFile
from tidy.progress import ProgressCounter
from tidy.review.computations import _placement_slots
from tidy.review.state import ReviewState
from tidy.review.validation import _join_path
from tidy.review.views.placement import build_placement_views, list_archive_dirs


def _date_index_for(
    state: "ReviewState", source_files: Sequence[ScannedFile],
    *, progress: Optional[ProgressCounter] = None,
) -> Tuple[Dict[str, "dateindex.FolderDates"], Dict[str, date]]:
    """(archive day-histogram, day per source file) for the placement screen.

    The archive half is memoised on the state and shared by every source
    directory -- it describes the archive, not the source, and rebuilding it
    per tab switch would repeat work the SQLite cache already made cheap
    (measured on this archive: 11s cold for ~14k files, 0.5s warm).

    Opens its own Cache on this thread and closes it here: cache.py's
    sqlite3 connection is not shared across threads, the same reason
    _run_series_computation() opens its own. A failure to build the index is
    NOT a failure of the screen -- the date ranking is an enhancement over
    the name-based candidate list, so a broken cache degrades to the old
    behaviour instead of taking the whole tab down with it.

    `progress` is the placement screen's bar, shared with the
    classify_source() call before it -- hence a ProgressCounter.phase() per
    dateindex call rather than the counter itself (see that method: a raw
    counter would have each call's opening set_total() wipe the previous
    one's). Unlike classify_source()'s share, these two totals are NOT
    declared up front: the archive half is memoised, so on every source
    directory after the first it costs nothing and must not inflate the bar
    with work that will never happen."""
    media = [f for f in source_files if inbox._is_media(f.path)]
    cache: Optional[Cache] = None
    try:
        if state.db_path:
            cache = Cache(state.db_path)
        with state.placement_lock:
            index = state.date_index
        if index is None:
            index = dateindex.build_folder_dates(
                state.fs, state.archive_root, state.candidates,
                cache=cache, max_workers=state.max_workers,
                progress=progress.phase() if progress is not None else None,
            )
            with state.placement_lock:
                state.date_index = index
        days = dateindex.extract_days(
            state.fs, media, cache=cache, max_workers=state.max_workers,
            progress=progress.phase() if progress is not None else None,
        )
        return index, days
    except Exception:
        return {}, {}
    finally:
        if cache is not None:
            cache.close()


def _run_placement_computation(state: "ReviewState", source_rel: str) -> None:
    """Background worker for GET /api/placement. Same never-raise contract
    as the series/events workers: this is a daemon
    thread nobody joins, so every failure path goes through .fail() rather
    than vanishing into stderr while the UI waits forever.

    Needs no Cache: placement is events.py all the way down (no dHash), so
    unlike _run_series_computation() there is no sqlite3 connection to open
    on this thread."""
    computation, progress = _placement_slots(state, source_rel)
    try:
        source_abs = _join_path(state.archive_root, source_rel.replace("\\", "/"))

# See: docs/technical/components/review-ui.md
        to_remove = {
            discard.path
            for decision in state.decisions
            for discard in decision.discarded
        }
        files = [c for c in state.candidates if c.path not in to_remove]

        classifications, non_media, engine_notes = inbox.classify_source(
            state.fs, source_abs, files, state.archive_root,
            camera_offsets=state.camera_offsets,
            event_gap_hours=state.event_gap_hours,
            gps_jump_km=state.gps_jump_km,
            max_workers=state.max_workers,
            dump_folder_names=state.dump_folder_names,
# See: docs/technical/components/review-ui.md
            progress=progress,
        )

        sizes_by_path = {c.path: c.stat.size for c in state.candidates}
        dir_index = list_archive_dirs(state.fs, state.archive_root)
        date_index, days_by_path = _date_index_for(state, files, progress=progress)
        folders, thumb_sources, placement_registry = build_placement_views(
            classifications, sizes_by_path, dir_index,
            date_index=date_index, days_by_path=days_by_path,
# See: docs/technical/components/review-ui.md
            id_prefix=state.next_placement_id_prefix(),
        )

        # Folders with nothing left to place are reported, never touched --
        # this archive has been observed emptying such folders on its own.
        classified_names = {c.name for c in classifications}
        empty_folders = [
            name for name in _immediate_subdir_names(state.fs, source_abs)
            if name not in classified_names
        ]

        def _publish() -> dict:
            state.thumb_sources.update(thumb_sources)
            state.placement_registry.update(placement_registry)
            return {
                "folders": folders,
                "notes": {
                    "skipped_non_media": [report.display_path(p) for p in non_media],
                    "empty_folders": sorted(empty_folders),
                    "source": source_rel,
# See: docs/technical/components/review-ui.md
                    "camera_drift_warnings": engine_notes["camera_drift_warnings"],
                    "suspicious": [
                        {**item, "path": report.display_path(item["path"])}
                        for item in engine_notes["suspicious"]
                    ],
                },
            }

        computation.finish_ready(_publish)
    except Exception as exc:  # noqa: BLE001 - must never escape a daemon thread
        computation.fail(str(exc))


def _immediate_subdir_names(fs: FileSystem, dir_abs: str) -> List[str]:
    try:
        entries = list(fs.scandir(dir_abs))
    except (OSError, PermissionError):
        return []
    return [e.name for e in entries if e.stat is not None and e.stat.is_dir]
