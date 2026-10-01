"""Constants shared across the dedup engine.

No filesystem access, no OS-specific code -- just names, sizes and sets
that scanner.py / hashing.py / cache.py / tiers.py / report.py agree on.
"""
from __future__ import annotations

# --- Hash algorithms -------------------------------------------------------
BLAKE2B = "blake2b"
DEFAULT_PARTIAL_ALGO = BLAKE2B
DEFAULT_HASH_ALGO = BLAKE2B
# Perceptual-hash algo name; the cache column exists, currently unused.
DEFAULT_DHASH_ALGO = "dhash64"

# --- Three-tier duplicate detection -----------------------------------------
# See: docs/technical/components/duplicates-mechanism.md
# Tier 2: BLAKE2b over the first TIER2_WINDOW_BYTES and last TIER2_WINDOW_BYTES
# of the file (~128KB/file read total for large files).
TIER2_WINDOW_BYTES = 64 * 1024  # 64 KB

# Files whose size is <= this reuse the tier-2 (partial) hash as the tier-3
# (full) hash: the "first 64KB + last 64KB" window already covers every byte
# of the file exactly once, so a second read/hash pass is redundant.
SMALL_FILE_REUSE_THRESHOLD = TIER2_WINDOW_BYTES * 2  # 128 KB

# --- Cache (SQLite) ----------------------------------------------------------
CACHE_SCHEMA_VERSION = 1
# Rows are committed in batches so an interrupted scan is resumable without
# redoing already-committed hashing work.
CACHE_COMMIT_BATCH_SIZE = 200
DEFAULT_DB_FILENAME = "cache.sqlite3"

# --- Report -------------------------------------------------------------
DEFAULT_REPORT_FILENAME = "report.csv"
CSV_DELIMITER = ";"

# --- Tool-internal directories inside .dedup\ -------------------------------
# Everything the tool generates lives in a dot-prefixed directory, never
# loose among the source files: one Explorer "hidden files" toggle then
# hides the whole lot at once, and the dot marks at a glance which
# directories are tool bookkeeping rather than content. These are relative
# names joined onto .dedup\ by tidy/cli -- the actual locations stay
# overridable per run via --db/--report/--thumbs-dir and friends.
DB_DIR_NAME = ".db"
REPORTS_DIR_NAME = ".reports"

# --- Review UI ---------------------------------------------------------------
THUMBS_DIR_NAME = ".thumbs"

# --- Compression -------------------------------------------------------------
# See: docs/business/rules/compression.md
COMPRESS_DIR_NAME = ".compress"
COMPRESSED_SUFFIX = "_compressed"
# Relative to the repo root (.dedup/).  Resolved by hwdetect.py.
BUNDLED_FFMPEG_REL = "bin/ffmpeg"

# winerror raised by process launch when Smart App Control blocks an unsigned exe.
WINERROR_APP_CONTROL_BLOCKED = 4551

# bpp thresholds: source codec -> minimum bpp above which recompression
# is expected to yield a meaningful size reduction (heuristic rule 8).
DEFAULT_BPP_THRESHOLDS = {
    "h264": 0.15,
    "hevc": 0.10,
    "vp9": 0.10,
    "av1": 0.08,
    "default": 0.12,
}

# Intra / uncompressed codecs: always worth compressing (rule 7).
INTRA_CODECS = frozenset((
    "mjpeg", "dv", "rawvideo", "prores", "ffv1", "mpeg2video",
))

# Default encoding profiles. CRF 21 alone does not reliably clear
# min_xpsnr=38.0 on real (non-seed) footage, so two higher-quality x265
# tiers exist alongside it (measured on one real capture): CRF 19 ->
# 38.35 dB (~0.35 dB margin, +35% size vs CRF 21), CRF 18 -> 38.99 dB
# (~1 dB margin, +56% size). Same encoder as "x265" in all three cases
# (SEED_CALIBRATION below is keyed by encoder, not profile, so its size/
# time seed applies to all of them -- an approximation until per-profile
# calibration exists; see compress.estimate_all_profiles()).
DEFAULT_COMPRESS_PROFILES = {
    "x265": {"encoder": "libx265", "crf": 21, "preset": "slow"},
    "x265_medium": {"encoder": "libx265", "crf": 19, "preset": "slow"},
    "x265_high": {"encoder": "libx265", "crf": 18, "preset": "slow"},
    "av1":  {"encoder": "libsvtav1", "crf": 30, "preset": "5"},
    # Hardware profiles (docs/technical/components/compression-mechanism.md). Quality key differs by encoder
    # family (cq for NVENC, global_quality for QSV) -- compress.py's config
    # loader validates profiles generically by "encoder" alone, so this
    # never needed a schema change. No SEED_CALIBRATION entry for any of
    # these: hardware performance/size is unmeasured until this machine's
    # own compress queue or POST /api/compress/calibrate produces a real
    # sample (docs/technical/components/compression-mechanism.md) -- the UI shows "?" until then, never a
    # guessed number.
    "hevc_nvenc": {"encoder": "hevc_nvenc", "cq": 23, "preset": "p7"},
    "av1_nvenc": {"encoder": "av1_nvenc", "cq": 30, "preset": "p7"},
    "hevc_qsv": {"encoder": "hevc_qsv", "global_quality": 23},
    "av1_qsv": {"encoder": "av1_qsv", "global_quality": 30},
}

# Seed calibration values (docs/technical/components/compression-mechanism.md). Keyed by
# device_key (hwdetect.EncoderInfo.device_key): CPU encoders' device_key
# equals their bare encoder name, so these two rows are unaffected by the
# device_key introduction (docs/technical/components/compression-mechanism.md) -- deliberately not renamed to
# avoid orphaning them. No hardware rows: never invent a calibration
# number for hardware not measured on it (docs/technical/components/compression-mechanism.md own hard rule).
SEED_CALIBRATION = {
    "libx265": {"bpp": 0.101, "mpx_s": 20.9},
    "libsvtav1": {"bpp": 0.075, "mpx_s": 14.6},
}

# Stopgap for the "SEED_CALIBRATION is keyed by encoder, not profile" gap
# above: without this, x265/x265_medium/x265_high shared one identical bpp
# and rendered identical size estimates in the compress tab's table despite
# being different CRFs. Factors are the one real-footage measurement on
# record: CRF19 -> ~+35% size, CRF18 -> ~+56% size vs CRF21
# (docs/business/rules/compression.md). Still an approximation
# (compress.estimate_all_profiles() marks it uncalibrated) until each
# profile is measured directly.
PROFILE_BPP_SEED_MULTIPLIER = {
    "x265": 1.0,
    "x265_medium": 1.35,
    "x265_high": 1.56,
    "av1": 1.0,
    # Hardware profiles: no measured factor exists yet (no seed to scale --
    # see SEED_CALIBRATION's own comment), so 1.0 is a neutral placeholder,
    # not a claim of equivalence (docs/technical/components/compression-mechanism.md).
    "hevc_nvenc": 1.0,
    "av1_nvenc": 1.0,
    "hevc_qsv": 1.0,
    "av1_qsv": 1.0,
}

# Minimum source duration to consider for compression (seconds).
MIN_COMPRESS_DURATION_S = 2.0

# --- Manifest / apply / restore ---------------------------------------------
# Format: docs/technical/components/manifest-and-db.md
MANIFEST_FILENAME = "manifest.csv"
# Version string stamped into every manifest row's tool_version column --
# bump this whenever the manifest schema or apply/restore semantics change,
# so a future --verify-manifest/--restore run can tell which tool version
# produced an old manifest.
TOOL_VERSION = "0.4.0"
# strftime() pattern for the "!REMOVE\<RRRR-MM-DD_gg-mm>\" batch directory name.
BATCH_DIR_TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M"

# --- Names that are hard-excluded from scanning / duplicate candidacy -------
# See: docs/business/rules/duplicates.md
REMOVE_DIR_NAME = "!REMOVE"
KOSZ_SENTINEL_FILENAME = ".dedup-kosz"
DEDUP_DIR_NAME = ".dedup"
VIDEO_TS_DIR_NAME = "VIDEO_TS"

# "!MOVES\<RRRR-MM-DD_gg-mm>\" holds the manifest for an op=move batch
# (in-archive sorting). It never holds the files themselves -- an op=move
# lands them at their real destination inside the archive. A separate
# top-level name rather than a subdirectory of !REMOVE on purpose: filing a
# sorting log under a directory that means "on the way out" would
# misrepresent every move as a deletion.
MOVES_DIR_NAME = "!MOVES"

# The inbox sentinel. Hard-coded, not configurable via config.json:
# keeper.py's rule 0 (a !NEW copy never outranks an archive copy) must
# never be bypassable through user config, so unlike priority_dirs it is
# not read from config.json.
# See: docs/business/rules/version-selection.md
NEW_DIR_NAME = "!NEW"

# Directory names pruned during the walk (casefold-compared).
# MOVES_DIR_NAME belongs here for the same reason REMOVE_DIR_NAME does:
# it is tool-managed bookkeeping, never archive content. Nothing there is
# a photo today (only manifest.csv), but leaving it walkable would make
# every past sorting batch grow the scan for no reason.
EXCLUDED_DIR_NAMES_CASEFOLD = frozenset(
    name.casefold()
    for name in (REMOVE_DIR_NAME, MOVES_DIR_NAME, DEDUP_DIR_NAME, VIDEO_TS_DIR_NAME)
)

# Filenames that are inventoried but never proposed as duplicate candidates.
EXCLUDED_INVENTORY_ONLY_FILENAMES_CASEFOLD = frozenset(
    name.casefold() for name in ("desktop.ini", "Thumbs.db")
)

# Extensions that are inventoried but never proposed as duplicate candidates
# (includes the generic "*.db" rule plus sidecar formats that must never be
# treated independently of the photo/video they belong to).
EXCLUDED_INVENTORY_ONLY_EXTENSIONS_CASEFOLD = frozenset(
    ext.casefold() for ext in (".db", ".aae", ".xmp")
)

# --- Event and place suggestions ---------------------------------------------
# Used only for the "N photos, M videos" counts in a suggested folder name --
# not a duplicate-candidacy or scan-inclusion rule, so this deliberately
# doesn't need to be exhaustive against the archive's full inventory (RAW/
# audio/sidecars are irrelevant to that count either way).
PHOTO_EXTENSIONS_CASEFOLD = frozenset(
    ext.casefold() for ext in (".jpg", ".jpeg", ".heic", ".png")
)
VIDEO_EXTENSIONS_CASEFOLD = frozenset(
    ext.casefold() for ext in (
        ".mp4", ".mov", ".3gp", ".3gpp", ".wmv", ".mpg", ".vob", ".avi", ".dat",
    )
)
