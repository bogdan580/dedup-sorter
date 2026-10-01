"""Event/trip grouping and offline place-name suggestion.

Pipeline: time-gap split -> GPS-jump split -> GPS inheritance -> offline
place name and folder suggestion.

This module only ever proposes a plan -- it never touches the filesystem
beyond the read-only FileSystem seam and one archive-root `scandir()` to
look for an existing matching top-level folder.
"""
# See: docs/business/rules/events-and-series.md
# Naming convention: docs/business/overview.md
from __future__ import annotations

import csv
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Dict, List, Optional, Sequence, Tuple

from tidy import config
from tidy.media import gps as gps_module
from tidy.media import timeline
from tidy.fs.fsmodel import FileSystem
from tidy.progress import DEFAULT_WORKERS, ProgressCounter
from tidy.duplicates.report import display_path, ensure_parent_dir
from tidy.duplicates.scanner import ScannedFile

try:
    import reverse_geocoder
    REVERSE_GEOCODER_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    REVERSE_GEOCODER_AVAILABLE = False

DEFAULT_EVENT_GAP_HOURS = 8.0
DEFAULT_GPS_JUMP_KM = 25.0
# See: docs/business/rules/events-and-series.md
SUSPICIOUS_MIN_YEAR = 1995

_EARTH_RADIUS_KM = 6371.0088

GPS_SOURCE_OWN = "own"
GPS_SOURCE_INHERITED = "inherited"
GPS_SOURCE_SUSPICIOUS = "suspicious"
GPS_SOURCE_NONE = "none"

# See: docs/business/rules/events-and-series.md
_COUNTRY_DISPLAY_NAMES = {
    "PL": "Poland", "PT": "Portugal", "SK": "Slovakia", "ES": "Spain",
    "UA": "Ukraine", "IT": "Italy", "DE": "Germany", "CZ": "Czechia",
    "AT": "Austria", "HU": "Hungary", "GB": "UK", "FR": "France",
    "GR": "Greece", "HR": "Croatia", "TR": "Turkey", "NL": "Netherlands",
    "BE": "Belgium", "CH": "Switzerland", "SI": "Slovenia",
}


def _haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(min(1.0, h)))


# --- camera model + clock-offset config.json loading -------------------------

_OFFSET_PATTERN = re.compile(r"^([+-])(?:(\d+)h)?(?:(\d+)m)?$")

_CAMERA_MODEL_TAG = 0x0110  # EXIF "Model" -- IFD0 top level, unlike
# See: docs/business/rules/events-and-series.md


def parse_camera_offset(text: str) -> timedelta:
    """Parse a config.json camera_offsets value like "+2h15m"/"-1h"/"+45m".
    Raises ValueError for anything else -- callers decide how to report a
    bad entry (see load_camera_offsets)."""
    match = _OFFSET_PATTERN.match(text.strip())
    if not match or (match.group(2) is None and match.group(3) is None):
        raise ValueError(f"invalid offset format: {text!r} (expected e.g. '+2h15m', '-1h', '+45m')")
    sign = 1 if match.group(1) == "+" else -1
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    return sign * timedelta(hours=hours, minutes=minutes)


def load_camera_offsets(path: Optional[str]) -> Dict[str, timedelta]:
    """Load the optional "camera_offsets" object from config.json (format:
    `{"NIKON D3100": "+2h15m"}`). Mirrors keeper.load_keeper_config's
    graceful-degradation contract: missing file/key, malformed JSON, or a
    wrong-typed entry never crashes --events, just falls back to "no
    offset for this model" with a printed warning so a hand-editing typo
    isn't silently ignored."""
    if path is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return {}
    except ValueError:
        print(f"Warning: {path}: invalid JSON -- ignoring camera_offsets.")
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("camera_offsets")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        print(f"Warning: {path}: 'camera_offsets' should be an object {{model: offset}} -- ignoring.")
        return {}

    offsets: Dict[str, timedelta] = {}
    for model, text in raw.items():
        if not isinstance(model, str) or not isinstance(text, str):
            print(f"Warning: {path}: camera_offsets['{model}'] should be a string -- skipping.")
            continue
        try:
            offsets[model] = parse_camera_offset(text)
        except ValueError as exc:
            print(f"Warning: {path}: camera_offsets['{model}']: {exc} -- skipping.")
    return offsets


def _extract_camera_model(fs: FileSystem, path: str) -> Optional[str]:
    if not timeline.PILLOW_AVAILABLE:
        return None
    try:
        with fs.open_read(path) as stream:
            with timeline.Image.open(stream) as image:
                raw = image.getexif().get(_CAMERA_MODEL_TAG)
    except timeline.DECODE_ERRORS:
        return None
    if not isinstance(raw, str):
        return None
    model = raw.strip().strip("\x00")
    return model or None


# --- per-candidate metadata ---------------------------------------------------

@dataclass(frozen=True)
class _RawEntry:
    scanned: ScannedFile
    when: datetime  # camera-offset-corrected
    timestamp_source: str
    camera_model: Optional[str]
    own_gps: Optional[Tuple[float, float]]
    is_suspicious: bool


def _build_raw_entries(
    fs: FileSystem,
    candidates: Sequence[ScannedFile],
    offsets: Dict[str, timedelta],
    now: datetime,
    *,
    max_workers: int = DEFAULT_WORKERS,
    progress: Optional[ProgressCounter] = None,
) -> List[_RawEntry]:
    """Per-candidate metadata extraction (timestamp + camera model + own
    GPS) -- the one decode-bound loop in this module (there is no dHash
    step here, so unlike series.py there is no cache to keep off worker
    threads: every one of these three calls is a pure read through the
    FileSystem seam, so the whole per-candidate computation can run
    entirely inside the thread pool, not just part of it).

    `max_workers<=1` (or a single candidate) runs the original sequential
    loop, byte-for-byte. ThreadPoolExecutor.map() preserves input order, so
    the returned list is in the exact same order as `candidates` regardless
    of worker count -- downstream (_split_by_time_gap() etc.) never
    observes a difference."""
    if progress is not None:
        progress.set_total(len(candidates))

    def _build_one(scanned: ScannedFile) -> _RawEntry:
        name = scanned.path.replace("\\", "/").rsplit("/", 1)[-1]
        ts = timeline.extract_timestamp(fs, scanned.path, name, scanned.stat.mtime_ns)
        model = _extract_camera_model(fs, scanned.path)
        offset = offsets.get(model) if model else None
        when = ts.when + offset if offset else ts.when
        own_gps = gps_module.extract_gps(fs, scanned.path)
        suspicious = when.year < SUSPICIOUS_MIN_YEAR or when > now
        entry = _RawEntry(
            scanned=scanned, when=when, timestamp_source=ts.source,
            camera_model=model, own_gps=own_gps, is_suspicious=suspicious,
        )
        if progress is not None:
            progress.increment()
        return entry

    if max_workers > 1 and len(candidates) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(_build_one, candidates))
    return [_build_one(c) for c in candidates]


# --- step 1: time-gap splitting ----------------------------------------------

def _split_by_time_gap(entries: Sequence[_RawEntry], gap_hours: float) -> List[List[_RawEntry]]:
    ordered = sorted(entries, key=lambda e: e.when)
    if not ordered:
        return []
    gap = timedelta(hours=gap_hours)
    groups: List[List[_RawEntry]] = [[ordered[0]]]
    for entry in ordered[1:]:
        if entry.when - groups[-1][-1].when > gap:
            groups.append([])
        groups[-1].append(entry)
    return groups


# --- step 2: GPS-jump sub-splitting -------------------------------------------

def _split_by_gps_jump(entries: Sequence[_RawEntry], jump_km: float) -> List[List[_RawEntry]]:
    """Cut `entries` (already time-sorted) wherever two consecutive usable
    GPS fixes (own GPS, not suspicious) are more than `jump_km` apart. The
    cut point is the midpoint *time* between those two fixes, so members
    with no GPS of their own that fall chronologically between them are
    unambiguously assigned to one side or the other -- needed before step
    3's inheritance can run within a single, place-consistent group.

    Comparisons use a (when, original_index) key rather than `when` alone:
    two donor fixes can share the exact same timestamp (e.g. both fell back
    to a coarse-grained mtime, or a batch copy stamped identical mtimes on
    files from two different places) -- a midpoint-by-time-only cut then
    collapses to that single shared instant, and a plain `>=` comparison
    would put *both* tied donors on the same side, silently defeating the
    split for exactly the pair of fixes that triggered it. Including the
    index breaks that tie deterministically (donor1 always precedes donor2
    in iteration order) without changing anything for the normal case where
    w1 != w2, since the index component only matters when two keys' `when`
    values are equal."""
    donors = [
        (idx, e.when, e.own_gps) for idx, e in enumerate(entries)
        if e.own_gps is not None and not e.is_suspicious
    ]
    cut_keys: List[Tuple[datetime, int]] = []
    for (idx1, w1, g1), (idx2, w2, g2) in zip(donors, donors[1:]):
        if _haversine_km(g1, g2) > jump_km:
            cut_keys.append((w1 + (w2 - w1) / 2, idx2))
    if not cut_keys:
        return [list(entries)]

    buckets: List[List[_RawEntry]] = [[] for _ in range(len(cut_keys) + 1)]
    for idx, entry in enumerate(entries):
        key = (entry.when, idx)
        bucket_idx = sum(1 for cut_key in cut_keys if key >= cut_key)
        buckets[bucket_idx].append(entry)
    return [b for b in buckets if b]


# --- step 3: GPS inheritance ---------------------------------------------------

@dataclass(frozen=True)
class EventMember:
    path: str
    when: datetime
    timestamp_source: str
    camera_model: Optional[str]
    gps: Optional[Tuple[float, float]]
    gps_source: str  # GPS_SOURCE_OWN | _INHERITED | _SUSPICIOUS | _NONE
    is_suspicious: bool


def _inherit_gps(entries: Sequence[_RawEntry]) -> List[EventMember]:
    donors = [(i, e.when, e.own_gps) for i, e in enumerate(entries)
              if e.own_gps is not None and not e.is_suspicious]
    members: List[EventMember] = []
    for entry in entries:
        if entry.is_suspicious:
            gps_value, source = None, GPS_SOURCE_SUSPICIOUS
        elif entry.own_gps is not None:
            gps_value, source = entry.own_gps, GPS_SOURCE_OWN
        elif donors:
            _, _, nearest_gps = min(donors, key=lambda d: abs((d[1] - entry.when).total_seconds()))
            gps_value, source = nearest_gps, GPS_SOURCE_INHERITED
        else:
            gps_value, source = None, GPS_SOURCE_NONE
        members.append(EventMember(
            path=entry.scanned.path, when=entry.when, timestamp_source=entry.timestamp_source,
            camera_model=entry.camera_model, gps=gps_value, gps_source=source,
            is_suspicious=entry.is_suspicious,
        ))
    return members


# --- step 4: offline place naming + folder suggestion -------------------------

def _existing_top_level_dirs(fs: FileSystem, archive_root: str) -> Dict[str, str]:
    """casefold(name) -> actual on-disk name, for archive_root's immediate
    subdirectories, excluding the hard-excluded bookkeeping dirs -- used to
    prefer an existing folder over inventing a new one."""
    result: Dict[str, str] = {}
    try:
        entries = fs.scandir(archive_root)
    except OSError:
        return result
    for entry in entries:
        if entry.stat is None or not entry.stat.is_dir:
            continue
        casefolded = entry.name.casefold()
        if casefolded in config.EXCLUDED_DIR_NAMES_CASEFOLD or casefolded == config.NEW_DIR_NAME.casefold():
            continue
        result[casefolded] = entry.name
    return result


def _geocode_all(coords: Sequence[Tuple[float, float]]) -> List[Optional[Tuple[str, str]]]:
    """One batched offline lookup (GeoNames data bundled in the pip
    package -- no network access) for every event that has
    a usable GPS point, rather than one lookup per event: RGeocoder loads
    its k-d tree once per process regardless, but batching keeps this
    module's contract simple and avoids relying on that library-internal
    singleton behavior. Returns one (city_name, country_cc) or None per
    input coordinate, same order. Never raises: reverse_geocoder not being
    installed, or a lookup failure, degrades to "no place name" for every
    entry rather than aborting the whole --events run."""
    if not coords:
        return []
    if not REVERSE_GEOCODER_AVAILABLE:
        return [None] * len(coords)
    try:
        results = reverse_geocoder.search(list(coords), mode=1, verbose=False)
    except Exception:
        return [None] * len(coords)
    out: List[Optional[Tuple[str, str]]] = []
    for entry in results:
        name = entry.get("name")
        cc = entry.get("cc")
        out.append((name, cc) if name else None)
    return out


def _suggest_top_level(
    existing_dirs_casefold: Dict[str, str], city_name: Optional[str], country_cc: Optional[str]
) -> Tuple[str, bool]:
    """Returns (top_level_name, matched_existing). Prefers an exact
    (casefold) match against an existing archive top-level folder -- by
    city name first, then by the country's conventional display name --
    over inventing a new folder. Deliberately exact-match only (not fuzzy
    substring matching): a fuzzy match risks silently picking the wrong
    one of two similarly-named folders, and this is only ever a
    *suggestion* the user reviews before anything moves."""
    candidates = []
    if city_name:
        candidates.append(f"#{city_name}")
    if country_cc:
        candidates.append(f"#{_COUNTRY_DISPLAY_NAMES.get(country_cc.upper(), country_cc.upper())}")
    for candidate in candidates:
        existing = existing_dirs_casefold.get(candidate.casefold())
        if existing is not None:
            return existing, True
    if country_cc:
        return f"#{_COUNTRY_DISPLAY_NAMES.get(country_cc.upper(), country_cc.upper())}", False
    if city_name:
        return f"#{city_name}", False
    return "#Nieznane", False


# --- entry point ----------------------------------------------------------

@dataclass(frozen=True)
class Event:
    members: Tuple[EventMember, ...]  # sorted by `when`
    place_name: Optional[str]
    country_cc: Optional[str]
    suggested_folder: str
    matched_existing_dir: bool
    photo_count: int
    video_count: int


@dataclass
class EventsResult:
    events: List[Event] = field(default_factory=list)
    suspicious: List[EventMember] = field(default_factory=list)
    camera_drift_warnings: List[str] = field(default_factory=list)


def _extension_of(path: str) -> str:
    basename = path.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in basename:
        return ""
    return f".{basename.rsplit('.', 1)[-1].casefold()}"


def _count_kinds(members: Sequence[EventMember]) -> Tuple[int, int]:
    photos = videos = 0
    for m in members:
        ext = _extension_of(m.path)
        if ext in config.PHOTO_EXTENSIONS_CASEFOLD:
            photos += 1
        elif ext in config.VIDEO_EXTENSIONS_CASEFOLD:
            videos += 1
    return photos, videos


def _camera_drift_warnings(all_entries: Sequence[_RawEntry], offsets: Dict[str, timedelta]) -> List[str]:
    """A deliberately simple diagnostic (a full "sugerowany offset"
    computed from cross-camera alignment would need reliably inferring a
    numeric offset by comparing against another camera's corroborated
    timeline, which is out of scope for this pass). What's
    implemented instead: flag any camera model with suspicious-dated
    members that has no config.json entry yet, so the archive owner knows
    which model to investigate and, once they've worked out the real
    drift, add to "camera_offsets" themselves."""
    by_model: Dict[str, int] = {}
    for entry in all_entries:
        if entry.is_suspicious and entry.camera_model:
            by_model[entry.camera_model] = by_model.get(entry.camera_model, 0) + 1
    warnings = []
    for model, count in sorted(by_model.items()):
        if model in offsets:
            continue
        warnings.append(
            f"Model '{model}': {count} files with a suspicious date (before "
            f"{SUSPICIOUS_MIN_YEAR} or in the future) -- the camera clock may be off. "
            f"No entry in camera_offsets in config.json."
        )
    return warnings


def find_events(
    fs: FileSystem,
    candidates: Sequence[ScannedFile],
    archive_root: str,
    *,
    gap_hours: float = DEFAULT_EVENT_GAP_HOURS,
    gps_jump_km: float = DEFAULT_GPS_JUMP_KM,
    camera_offsets: Optional[Dict[str, timedelta]] = None,
    now: Optional[datetime] = None,
    max_workers: int = DEFAULT_WORKERS,
    progress: Optional[ProgressCounter] = None,
    name_hint: Optional[str] = None,
) -> EventsResult:
    """Detect trip/event groups among `candidates` and suggest a
    destination folder for each. Never raises: a single undecodable file
    just gets no GPS/camera-model metadata and falls back to mtime for its
    timestamp (timeline.extract_timestamp()'s own contract), same as the
    other engine modules.

    `max_workers` (--workers, default progress.DEFAULT_WORKERS) parallelizes
    _build_raw_entries()'s per-candidate metadata extraction -- see that
    function's docstring. `progress` (optional) is updated as candidates
    are processed.

    `name_hint` is the name of the !NEW folder these candidates came from,
    when they all came from one. Such a name outranks geocoding --
    "Gdansk"/"Travel" are the user telling you where they were, and they
    know better than GeoNames. inbox.py passes None for dump-folder names
    ("Camera", "Baseus", ...), so this function never has to know which
    names are meaningless; it just trusts the hint it is given."""
    now = now if now is not None else datetime.now()
    offsets = camera_offsets or {}

    raw_entries = _build_raw_entries(
        fs, candidates, offsets, now, max_workers=max_workers, progress=progress,
    )
    existing_dirs = _existing_top_level_dirs(fs, archive_root)

    time_groups = _split_by_time_gap(raw_entries, gap_hours)
    final_groups: List[List[_RawEntry]] = []
    for group in time_groups:
        final_groups.extend(_split_by_gps_jump(group, gps_jump_km))

# See: docs/business/rules/events-and-series.md
    members_by_group: List[List[EventMember]] = []
    representative_coords: List[Optional[Tuple[float, float]]] = []
    for group in final_groups:
        members = _inherit_gps(group)
        members_by_group.append(members)
        representative = next((m.gps for m in members if m.gps is not None), None)
        representative_coords.append(representative)

    coords_to_geocode = [c for c in representative_coords if c is not None]
    geocoded = iter(_geocode_all(coords_to_geocode))
    place_by_index: Dict[int, Optional[Tuple[str, str]]] = {}
    for idx, coord in enumerate(representative_coords):
        place_by_index[idx] = next(geocoded) if coord is not None else None

    events: List[Event] = []
    for idx, members in enumerate(members_by_group):
        place = place_by_index[idx]
        city_name, country_cc = place if place else (None, None)
# See: docs/business/rules/events-and-series.md
        label_place = name_hint or city_name
        top_level, matched = _suggest_top_level(existing_dirs, label_place, country_cc)
        photo_count, video_count = _count_kinds(members)
        first_when = members[0].when
        if label_place:
            label = f"{first_when:%Y} {label_place}"
        else:
            label = f"{first_when:%Y} (nieznane miejsce)"
        events.append(Event(
            members=tuple(members), place_name=city_name, country_cc=country_cc,
            suggested_folder=f"{top_level}\\{label}", matched_existing_dir=matched,
            photo_count=photo_count, video_count=video_count,
        ))

    suspicious = [m for members in members_by_group for m in members if m.is_suspicious]
    drift_warnings = _camera_drift_warnings(raw_entries, offsets)
    return EventsResult(events=events, suspicious=suspicious, camera_drift_warnings=drift_warnings)


def filter_new_locations(events: Sequence[Event], since: timedelta, now: Optional[datetime] = None) -> List[Event]:
    """The "--since 7d" filter: events whose suggested top-level folder
    doesn't already exist on disk AND whose latest member falls within
    `since` of `now`."""
    now = now if now is not None else datetime.now()
    cutoff = now - since
    return [
        e for e in events
        if not e.matched_existing_dir and e.members and e.members[-1].when >= cutoff
    ]


# --- CSV export + console summary (mirrors series.py's pattern) --------------

_CSV_COLUMNS = (
    "event_id", "suggested_folder", "matched_existing", "path", "when",
    "timestamp_source", "camera_model", "gps_lat", "gps_lon", "gps_source",
    "is_suspicious",
)

_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t")


def _csv_safe(value: str) -> str:
    if value and value[0] in _FORMULA_TRIGGER_CHARS:
        return "'" + value
    return value


def write_events_csv(path: str, result: EventsResult) -> None:
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=config.CSV_DELIMITER)
        writer.writerow(_CSV_COLUMNS)
        for event_id, event in enumerate(result.events, start=1):
            for member in event.members:
                lat, lon = member.gps if member.gps else ("", "")
                writer.writerow([
                    event_id, _csv_safe(event.suggested_folder), event.matched_existing_dir,
                    _csv_safe(display_path(member.path)), member.when.isoformat(),
                    member.timestamp_source, member.camera_model or "",
                    lat, lon, member.gps_source, member.is_suspicious,
                ])


def format_events_summary(result: EventsResult, *, top_n: int = 20) -> str:
    lines = [
        "=== Events and grouping suggestions (--events) ===",
        f"Events: {len(result.events)}",
        f"Files with a suspicious date: {len(result.suspicious)}",
    ]
    shown = result.events[:top_n]
    if shown:
        lines.append(f"{len(shown)} of {len(result.events)} events:")
    for i, event in enumerate(shown, start=1):
        existing = "existing folder" if event.matched_existing_dir else "NEW folder"
        lines.append(
            f"  {i}. [{len(event.members)} files, {existing}] -> {event.suggested_folder}"
        )
    if result.camera_drift_warnings:
        lines.append("Camera clock drift warnings:")
        lines.extend(f"  - {w}" for w in result.camera_drift_warnings)
    if result.suspicious:
        lines.append(f"{len(result.suspicious)} files with a suspicious date (separate section, excluded from GPS inheritance):")
        for m in result.suspicious[:top_n]:
            lines.append(f"  - {display_path(m.path)} ({m.when.isoformat()})")
    return "\n".join(lines)
