"""Local review server.

Serves a single-page review UI over `http.server`, bound to 127.0.0.1 only.
Four of the five tabs write: "Exact duplicates" and "Series and similar"
send discards to !REMOVE (op=remove); "Grouping suggestions" moves files
deeper into the archive (op=move, POST /api/apply-placement) and sends the
cards marked red to !REMOVE (op=remove, same endpoint, separate batch); "Sugestia
kompresji" lands a new file next to its untouched original (op=compress,
POST /api/apply-compress) and can discard that original only afterwards,
as a separate op=remove batch. The events registry is read-only -- no
apply endpoint is ever handed it.

Five id->path registries, each an allowlist read ONLY by the endpoints
listed -- an id absent from the relevant registry is always rejected, never
silently resolved through a different one:
  - file_registry (exact-duplicate groups): POST /api/apply, POST /api/open.
  - series_registry (series groups): POST /api/apply-series,
    POST /api/open.
  - events_registry (event groups): POST /api/open ONLY -- events stay
    read-only, so no apply endpoint is ever handed this dict.
  - placement_registry (placement suggestions): POST /api/apply-placement,
    POST /api/open.
  - compress_registry (compress candidates, GET /api/compress): populated
    by build_compress_views(), read by POST /api/apply-compress,
    POST /api/apply-compress-remove, and POST /api/open.

The five id namespaces are disjoint by construction ("1:0" for duplicates,
"s1:0"/"e1:0"/"p1:0"/"c1:0" for the rest), which is what makes resolving an
id by trying each registry in turn unambiguous: at most one can ever match.

Security: docs/technical/security.md

Skips ("skip this group" / "skip this suggestion") are server-side and
persisted, keyed by MEMBER PATHS rather than group_id -- a group_id is only
a position in one run's result list and means something different after
every rescan. Skips: docs/technical/components/manifest-and-db.md

Selection model: POST /api/apply's body is
`{"selections": [{"group_id", "keeper_id", "discard_ids"}, ...]}`. Every id
in that body is opaque and untrusted -- it is resolved to a real filesystem
path ONLY through `file_registry` (built alongside `thumb_sources` by
`build_group_views()`), never used to build a path directly. Validation
(`_validate_selections()`) checks every selection before a single file is
moved: exactly one keeper per group, every id must belong to the group_id
it's submitted under, no duplicate/overlapping ids, and any id absent from
`file_registry` is an explicit 400, not a silently-ignored no-op.

Behaviour: docs/technical/components/review-ui.md
"""
from __future__ import annotations

import secrets
import threading
import webbrowser
from datetime import timedelta
from http.server import ThreadingHTTPServer
from typing import Callable, Dict, Optional, Sequence, Tuple

from tidy.analysis import events
from tidy.analysis import inbox
from tidy.analysis import series
from tidy.media import thumbnails
from tidy.fs.fsmodel import FileSystem
from tidy.duplicates.scanner import ScannedFile
from tidy.duplicates.tiers import DuplicateGroup
from tidy.duplicates.keeper import KeeperDecision
from tidy.progress import DEFAULT_WORKERS
from tidy.review.handler import _make_handler_class
from tidy.review.skips import _load_compress_skips, _load_placement_skips, _load_series_skips
from tidy.review.state import ReviewState
from tidy.review.views.duplicates import build_group_views


# --- server lifecycle ---------------------------------------------------------

def start_server(
    fs: FileSystem,
    archive_root: str,
    duplicate_groups: Sequence[DuplicateGroup],
    decisions: Sequence[KeeperDecision],
    thumbs_dir: str,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token: Optional[str] = None,
    candidates: Sequence[ScannedFile] = (),
    db_path: Optional[str] = None,
    series_window_seconds: float = series.DEFAULT_SERIES_WINDOW_SECONDS,
    series_hamming_threshold: int = series.DEFAULT_HAMMING_THRESHOLD,
    event_gap_hours: float = events.DEFAULT_EVENT_GAP_HOURS,
    gps_jump_km: float = events.DEFAULT_GPS_JUMP_KM,
    camera_offsets: Optional[Dict[str, timedelta]] = None,
    max_workers: int = DEFAULT_WORKERS,
    dump_folder_names: Sequence[str] = inbox.DEFAULT_DUMP_FOLDER_NAMES,
    rescan: Optional[Callable[[], Tuple[
        Sequence[DuplicateGroup], Sequence[KeeperDecision], Sequence[ScannedFile],
    ]]] = None,
    compress_config_path: Optional[str] = None,
    compress_staging_dir: Optional[str] = None,
) -> Tuple[ThreadingHTTPServer, threading.Thread, ReviewState]:
    """Build the view models, bind a ThreadingHTTPServer on `host`:`port`
    (port=0 picks an ephemeral free port), and start serving in a daemon
    background thread. Returns immediately -- callers (serve() below, or
    tests) decide when/how to shut it down. Never binds to anything but
    loopback by default -- nothing here is meant to be reachable off this
    machine (docs/technical/security.md).

    `candidates`/`db_path`/the tuning knobs are all
    keyword-only with defaults that reproduce today's behavior exactly
    (empty candidates, no db_path -- see ReviewState's own field comments)
    -- every existing caller/fixture that doesn't pass them keeps working
    unchanged; only a caller that explicitly wants GET /api/series or
    GET /api/events to compute real data needs to pass them."""
    groups_json, thumb_sources, file_registry = build_group_views(duplicate_groups, decisions)
    total_reclaimable = sum(g["reclaimable_bytes"] for g in groups_json)

    state = ReviewState(
        fs=fs,
        archive_root=archive_root,
        duplicate_groups=list(duplicate_groups),
        decisions=list(decisions),
        thumbs=thumbnails.ThumbnailStore(fs, thumbs_dir),
        thumb_sources=thumb_sources,
        file_registry=file_registry,
        groups_json=groups_json,
        total_reclaimable_bytes=total_reclaimable,
        token=token or secrets.token_urlsafe(32),
        host=host,
        candidates=list(candidates),
        db_path=db_path,
        series_window_seconds=series_window_seconds,
        series_hamming_threshold=series_hamming_threshold,
        event_gap_hours=event_gap_hours,
        gps_jump_km=gps_jump_km,
        camera_offsets=dict(camera_offsets) if camera_offsets else {},
        max_workers=max_workers,
        dump_folder_names=tuple(dump_folder_names),
        rescan=rescan,
        compress_config_path=compress_config_path,
        compress_staging_dir=compress_staging_dir,
    )

# See: docs/technical/architecture.md
    _load_series_skips(state)
    # Same for the placement screen's own persisted skips -- see
    # _load_placement_skips().
    _load_placement_skips(state)
    # Same for the compress screen's own persisted skips -- see
    # _load_compress_skips().
    _load_compress_skips(state)

    handler_cls = _make_handler_class(state)
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    state.port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, state


def serve(
    fs: FileSystem,
    archive_root: str,
    duplicate_groups: Sequence[DuplicateGroup],
    decisions: Sequence[KeeperDecision],
    thumbs_dir: str,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    print_func: Callable[[str], None] = print,
    candidates: Sequence[ScannedFile] = (),
    db_path: Optional[str] = None,
    series_window_seconds: float = series.DEFAULT_SERIES_WINDOW_SECONDS,
    series_hamming_threshold: int = series.DEFAULT_HAMMING_THRESHOLD,
    event_gap_hours: float = events.DEFAULT_EVENT_GAP_HOURS,
    gps_jump_km: float = events.DEFAULT_GPS_JUMP_KM,
    camera_offsets: Optional[Dict[str, timedelta]] = None,
    max_workers: int = DEFAULT_WORKERS,
    dump_folder_names: Sequence[str] = inbox.DEFAULT_DUMP_FOLDER_NAMES,
    rescan: Optional[Callable[[], Tuple[
        Sequence[DuplicateGroup], Sequence[KeeperDecision], Sequence[ScannedFile],
    ]]] = None,
    compress_config_path: Optional[str] = None,
    compress_staging_dir: Optional[str] = None,
) -> int:
    """Blocking CLI entrypoint (`dedup.py --review`): start the server,
    print its URL, optionally open a browser tab, and block until the user
    interrupts (Ctrl+C) -- mirrors a typical local dev server's lifecycle.

    New keyword-only params simply forwarded to
    start_server() -- see its docstring for why every default reproduces
    today's behavior unchanged."""
    httpd, thread, state = start_server(
        fs, archive_root, duplicate_groups, decisions, thumbs_dir,
        host=host, port=port,
        candidates=candidates, db_path=db_path,
        series_window_seconds=series_window_seconds,
        series_hamming_threshold=series_hamming_threshold,
        event_gap_hours=event_gap_hours, gps_jump_km=gps_jump_km,
        camera_offsets=camera_offsets,
        max_workers=max_workers,
        dump_folder_names=dump_folder_names,
        rescan=rescan,
        compress_config_path=compress_config_path,
        compress_staging_dir=compress_staging_dir,
    )
    url = f"http://{state.host}:{state.port}/"
    print_func("=== Review server ===")
    print_func(url)
    print_func("Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
# See: docs/technical/architecture.md
        while thread.is_alive():
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print_func("Stopping server...")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0
