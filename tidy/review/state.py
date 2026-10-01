"""Review session state and the compress/calibration job records."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from tidy.analysis import dateindex
from tidy.analysis import events
from tidy.compression import hwdetect
from tidy.analysis import inbox
from tidy.duplicates import report
from tidy.analysis import series
from tidy.media import thumbnails
from tidy.fs.fsmodel import FileSystem
from tidy.duplicates.scanner import ScannedFile
from tidy.duplicates.tiers import DuplicateGroup
from tidy.duplicates.keeper import KeeperDecision
from tidy.progress import DEFAULT_WORKERS, ProgressCounter
from tidy.review.lazy import _LazyComputation


@dataclass
class CompressJob:
    """One file queued by POST /api/apply-compress, carried through the
    encode -> verify -> apply pipeline _run_compress_queue() drives.

    Mutated in place by that background thread while it runs (under
    state.compress_queue_lock, since GET /api/compress/jobs reads the same
    list from a different thread); nothing else ever writes to a job once
    it has been queued. `result_path`/`result_size`/`xpsnr` stay at their
    zero/empty defaults until `status` reaches "done" -- callers must not
    read them before that.

    "encoded" (between "verifying" and "done") means the encode passed its
    own verify step but has NOT yet landed in the archive: the move itself
    (hashing + fs.move() of a whole video file) still has to run. It is a
    real, observable gap, not a rename of "done" -- but it is per-JOB now:
    _run_compress_queue() calls apply_compressions() for this job alone
    immediately after its own verify passes, promoting straight to "done"
    (or "failed", if that move fails) without waiting on any other job in
    the queue.
    See: docs/business/rules/compression.md
    """
    file_id: str
    group_id: str
    path: str  # original, absolute -- never touched until apply_compress_removals()
    size: int  # original size, for the review UI's before/after display
    profile_name: str
# See: docs/technical/components/review-ui.md
    device: str = ""
    device_key: str = ""
    status: str = "queued"  # queued -> encoding -> verifying -> encoded -> done | failed | cancelled
    error: str = ""
    result_size: int = 0
    result_path: str = ""  # <original>_compressed.<ext>, set once apply_compressions() lands it
    xpsnr: Optional[float] = None
# See: docs/technical/components/review-ui.md
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


@dataclass
class CalibrationJob:
    """One POST /api/compress/calibrate run (docs/technical/components/compression-mechanism.md): measures
    every requested device_key against a short (`_CALIBRATION_CLIP_SECONDS`)
    slice of ONE already-registered candidate file, writing straight to
    cache.compress_calibration. Never touches the archive and produces no
    manifest -- this is measurement, not a compress job. Runs in the SAME
    single-slot queue as CompressJob (state.compress_queue_running/
    state.compress_queue_lock/state.compress_cancel_event): the two must
    never run concurrently, since both drive the one real encoder this
    machine has.

    Mutated in place by _run_calibration_job() under state.compress_queue_
    lock, same discipline as CompressJob."""
    file_id: str
    path: str
    size: int
    device_keys: List[str]  # requested, in submission order
    status: str = "queued"  # queued -> running -> done | failed | cancelled
    current_device_key: str = ""
# See: docs/technical/components/review-ui.md
    measured: Dict[str, dict] = field(default_factory=dict)
    error: str = ""


# --- server state ------------------------------------------------------------

@dataclass
class ReviewState:
    fs: FileSystem
    archive_root: str
    duplicate_groups: Sequence[DuplicateGroup]
    decisions: Sequence[KeeperDecision]
    thumbs: thumbnails.ThumbnailStore
    thumb_sources: Dict[str, str]
    file_registry: Dict[str, dict]
    groups_json: List[dict]
    total_reclaimable_bytes: int
    token: str
    host: str = "127.0.0.1"
    port: int = 0
# See: docs/technical/components/review-ui.md
    applied_group_ids: Set[str] = field(default_factory=set)
    applied_batches: List[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    candidates: Sequence[ScannedFile] = ()
    db_path: Optional[str] = None
# See: docs/technical/components/review-ui.md
    series_window_seconds: float = series.DEFAULT_SERIES_WINDOW_SECONDS
    series_hamming_threshold: int = series.DEFAULT_HAMMING_THRESHOLD
    event_gap_hours: float = events.DEFAULT_EVENT_GAP_HOURS
    gps_jump_km: float = events.DEFAULT_GPS_JUMP_KM
    camera_offsets: Dict[str, timedelta] = field(default_factory=dict)
# See: docs/technical/components/review-ui.md
    max_workers: int = DEFAULT_WORKERS
    series_progress: ProgressCounter = field(default_factory=ProgressCounter)
    events_progress: ProgressCounter = field(default_factory=ProgressCounter)

    series_registry: Dict[str, dict] = field(default_factory=dict)
    events_registry: Dict[str, dict] = field(default_factory=dict)
    series_computation: _LazyComputation = field(default_factory=_LazyComputation)
    events_computation: _LazyComputation = field(default_factory=_LazyComputation)
# See: docs/technical/components/review-ui.md
    applied_series_group_ids: Set[str] = field(default_factory=set)
    series_skips: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    series_skip_lock: threading.Lock = field(default_factory=threading.Lock)

# See: docs/technical/components/review-ui.md
    placement_registry: Dict[str, dict] = field(default_factory=dict)
# See: docs/technical/components/review-ui.md
    placement_computations: Dict[str, _LazyComputation] = field(default_factory=dict)
    placement_progress: Dict[str, ProgressCounter] = field(default_factory=dict)
    # The archive's day histogram, built once and reused by every source
    # directory (see _date_index_for). Guarded by placement_lock.
    date_index: Optional[Dict[str, "dateindex.FolderDates"]] = None
# See: docs/technical/components/review-ui.md
    placement_lock: threading.Lock = field(default_factory=threading.Lock)
    placement_id_epoch: int = 0
    applied_placement_group_ids: Set[str] = field(default_factory=set)
    placement_skips: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    # Guards placement_skips alone, same reasoning as series_skip_lock.
    placement_skip_lock: threading.Lock = field(default_factory=threading.Lock)
    dump_folder_names: Sequence[str] = inbox.DEFAULT_DUMP_FOLDER_NAMES
    dir_index_cache: Optional[List[dict]] = None
# See: docs/technical/components/review-ui.md
    rescan: Optional[Callable[[], Tuple[
        Sequence[DuplicateGroup], Sequence[KeeperDecision], Sequence[ScannedFile],
    ]]] = None

# See: docs/technical/components/review-ui.md
    compress_registry: Dict[str, dict] = field(default_factory=dict)
# See: docs/technical/components/review-ui.md
    compress_computations: Dict[str, _LazyComputation] = field(default_factory=dict)
    compress_progress: Dict[str, ProgressCounter] = field(default_factory=dict)
    compress_lock: threading.Lock = field(default_factory=threading.Lock)
    compress_id_epoch: int = 0
    applied_compress_group_ids: Set[str] = field(default_factory=set)
# See: docs/technical/components/review-ui.md
    hwdetect_cache: Optional["hwdetect.HwDetectResult"] = None
    # Readable reason from the last blocked detect (Smart App Control);
    # None after a successful detect. Surfaced as `unavailable_reason`.
    hwdetect_block_reason: Optional[str] = None
    compress_config_path: Optional[str] = None
# See: docs/technical/components/review-ui.md
    compress_staging_dir: Optional[str] = None

# See: docs/technical/components/review-ui.md
    compress_jobs: List[CompressJob] = field(default_factory=list)
    compress_queue_progress: ProgressCounter = field(default_factory=ProgressCounter)
# See: docs/technical/components/review-ui.md
    compress_queue_lock: threading.Lock = field(default_factory=threading.Lock)
    compress_cancel_event: threading.Event = field(default_factory=threading.Event)
    compress_queue_running: bool = False
    calibration_job: Optional["CalibrationJob"] = None
    compress_skips: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    compress_skip_lock: threading.Lock = field(default_factory=threading.Lock)

# See: docs/technical/components/review-ui.md
    batch_registry: Dict[str, str] = field(default_factory=dict)
# See: docs/technical/components/review-ui.md
    batch_list_cache: Optional[List[dict]] = None
    scan_generation: int = 0

    def next_placement_id_prefix(self) -> str:
        """A private id namespace for ONE placement computation ("p1_",
        "p2_", ...), so `p1_3:0` can only ever mean a file from the
        computation that handed it out.

        Under placement_lock, not state.lock, for the reason that lock
        exists: the caller is a background worker, and it must not
        queue behind an apply. The lock is held for one increment.

        The bug this closes: ids were positional per computation ("p3:1"),
        placement_registry is one dict updated by each of them, and
        _LazyComputation never recomputes a directory once ready. Sorting
        !NEW, switching source, then coming back left the screen showing
        !NEW's photos while its ids resolved to the other directory's
        files -- and POST /api/apply-placement moved THOSE, reporting
        success. The stale client-side selections were the visible symptom
        (cards that came back unticked, because the remembered id set
        belonged to a different group of the same number)."""
        with self.placement_lock:
            self.placement_id_epoch += 1
            return f"p{self.placement_id_epoch}_"

    def next_compress_id_prefix(self) -> str:
        """A private id namespace for ONE compress computation ("c1_",
        "c2_", ...), so `c1_3:0` can only ever mean a file from the
        computation that handed it out -- exact mirror of
        next_placement_id_prefix() above, same bug it exists to prevent
        (see that method's docstring): compress_registry is one dict every
        computation update()s, and switching source directories then
        coming back must not let a stale id resolve to the wrong file.

        Under compress_lock, not state.lock, for the same reason
        placement's own version uses placement_lock: the caller is a
        background worker and must not queue behind an apply."""
        with self.compress_lock:
            self.compress_id_epoch += 1
            return f"c{self.compress_id_epoch}_"

    def to_json(self) -> dict:
        groups_out = []
        for g in self.groups_json:
            g_out = dict(g)
            g_out["applied"] = g["group_id"] in self.applied_group_ids
            groups_out.append(g_out)
        return {
            "archive_root": report.display_path(self.archive_root),
            "group_count": len(self.groups_json),
            "total_reclaimable_bytes": self.total_reclaimable_bytes,
            "all_applied": bool(self.groups_json) and len(self.applied_group_ids) == len(self.groups_json),
            "applied_batches": list(self.applied_batches),
            "groups": groups_out,
# See: docs/technical/components/review-ui.md
            "series_window_seconds": self.series_window_seconds,
            "series_hamming_threshold": self.series_hamming_threshold,
            "series_window_seconds_default": series.DEFAULT_SERIES_WINDOW_SECONDS,
            "series_hamming_threshold_default": series.DEFAULT_HAMMING_THRESHOLD,
        }
