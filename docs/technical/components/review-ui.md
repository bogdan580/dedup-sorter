---
tags: [dedup, technical]
---

# Review Interface

Local server based on the standard library, listening exclusively on `127.0.0.1`.
The entire browser application — HTML, CSS, and JavaScript — is served as a
**single document**; the server does not serve separate static files, which is a
deliberate decision: that is one less path for serving files from disk. Code
resides in the `tidy/review/` package.

## Package Layout

| Element | Role |
|---|---|
| `security.py` | `Host` and `Origin` header checking, token, CSP header |
| `lazy.py` | `_LazyComputation` — background computation slot polled by the UI |
| `validation.py` | selection and destination directory validators for apply endpoints |
| `skips.py` | skips stored in the cache database |
| `computations.py` | lazy computations, cached directories and batches, series parameters |
| `state.py` | `ReviewState` — session state accessed by request handlers |
| `server.py` | server runner; its docstring records the design |
| `page.py` | page assembly from `static/` |
| `views/` | view models: duplicates, series, events, grouping suggestions, compression, batches |
| `workers/` | worker threads: rescan, suggestions, compression queue, calibration |
| `handler/` | HTTP handling and routing table |
| `static/` | `index.html`, `css/app.css`, `js/*.js` |

## Page Assembly

`static/index.html` contains marker lines (`/*@inline css/app.css*/` in style blocks,
`//@inline js/common.js` in scripts). Upon import, `page.py` replaces each marker
with the content of the specified file and exposes the result as `INDEX_HTML`.
Content ordering is defined by the `INLINE_FILES` tuple; a marker outside it or a
missing file raises an exception — nothing is silently skipped. Files in `static/`
have forced LF line endings (`.gitattributes`).

Why a single document: the CSP header allows scripts and styles strictly as
`'unsafe-inline'` (otherwise only `'self'`), and tests read the entire page as
plain text. Changing JavaScript means editing `static/js/*.js` — a new file must
be added to `INLINE_FILES` and have its marker placed in `index.html`.

## Route Table

`handler/routes.py` contains three tables: `GET_ROUTES` (exact paths),
`GET_PREFIX_ROUTES` (prefixes, evaluated after exact matches), and `POST_ROUTES`.
**The list of allowed POST paths is the keys of `POST_ROUTES`** — a path not in
the table receives a 404 response before reaching token verification. Route
functions are standard `handle_*` functions in `handler/routes_*.py`, and
`handler/__init__.py` enforces gate ordering. Security details:
[security.md](../security.md).

Without this screen, image-based modes would be useless. Photo series cannot be
evaluated from a CSV spreadsheet.

## Five Tabs

| Tab | Content | Moves Files? |
|---|---|---|
| Exact duplicates | compact list — files are identical, so **paths** matter, not images | yes, to trash |
| Series and similar | tile grid, suggested keeper highlighted | yes, to trash |
| Grouping suggestions | editable destination directory, candidates, thumbnail strips; cards cycle unmarked / blue (move) / red (remove) | yes, deeper into archive; red cards to trash |
| Compression suggestion | candidates from selected directory, estimation table, encoding queue | creates new files; originals to trash only on separate command |
| Restore | batches existing on disk and number of files that can be undone | restores |

## State and Background Computations

The server maintains a single state object accessed by all request handlers.

Costly computations — series, events, suggestions, probing videos for
compression — run **lazily, in worker threads**. The request returns immediately
with "computing" status, while the browser polls every second and draws a
progress bar. A synchronous alternative was rejected: with tens of thousands of
photos, a request would hang for minutes and hit browser timeouts.

Two properties of this mechanism are essential:

- **Dedicated lock, not shared with the rest of state.** Otherwise an ongoing
  series computation would block every apply action.
- **Single-step publication of results.** The worker computes and constructs
  models locally, merging registries **and** setting status to ready only at the
  very end. The reverse order would allow seeing "ready" before IDs can be
  resolved into paths.

Once computed, a result is not recomputed. Recomputation requires an explicit,
separate command.

## ID Registries

> [!important]
> **A path from the client never reaches a filesystem operation.** The only bridge
> from request to file consists of five registries functioning as allowlists:

| Registry | Read by |
|---|---|
| duplicates | duplicate application, opening file |
| series | series application, opening file |
| events | **strictly** opening file |
| suggestions | suggestion application, opening file |
| compression | compression job dispatch, calibration dispatch, original removal, opening file |

ID namespaces are **disjoint by design** — each type has its own prefix — ensuring
that resolving an ID across registries is unambiguous: at most one can match.

Registry separation is a security boundary, not mere tidiness. Adding event
files to the duplicate registry would allow the public endpoint to move files
from a tab designed never to move anything. Therefore, the event registry is
**never** passed to apply validation — even "for convenience".

For the same reason, applying series has its **own endpoint**, rather than a flag
on an existing one: a flag would delegate to the client the decision of which
registry the server inspects.

## Screen Behavior

**Pagination and layout** operate purely in the browser. Changing page size or
column count does not query the server again — the full set of groups is received
once. Page numbers are tracked separately for each tab; a shared counter caused
switching from a high page on one tab to a shorter tab to render an empty page.

**Selection** defaults to matching exactly what the CLI would produce. Clicking a
card transfers keeper status; for series, it toggles membership in the retained
set, since a series group can keep multiple files.

**Application** offers two buttons — all and current page — each with a
confirmation modal showing the size of the **current selection**, not the entire
set. Each click creates **its own batch** with its own manifest, and results
append to the panel rather than overwriting it. Once applied, a group is rejected
upon resubmission — enforced by the server, not just hidden in the UI. A request
mixing new and already applied groups is rejected in its entirety.

**Path difference highlighting.** Paths in a group are compared segment by
segment from left to right; the portion from the first divergence to the end of
directories is highlighted, common prefix is dimmed, and file name is handled
separately — a differing filename conveys different information than a
differing location. Segments are rendered as plain text, never HTML: names come
from disk and are not trusted content.

**Skipping a group** is persisted server-side on every tab that offers it
(exact duplicates, series, grouping suggestions, compression): the checkbox posts
`{"group_id", "skip"}` to that tab's own endpoint (`/api/dup-skip`,
`/api/series-skip`, `/api/placement-skip`, `/api/compress/skip`), the server
resolves the id through **that tab's registry only**, and writes the member paths to
that tab's own cache table before updating its in-memory mirror. The browser flips
the group's flag only after the server confirms. Every group is sent with a
`skipped` flag (for duplicates on `GET /api/state`, computed per request) rather
than omitted, so a skipped group can be revealed with "Show skipped" and taken
back. Skipped groups are excluded from paging, the summary bar, and both apply
buttons. Skip mirrors are loaded once at server start and are **not** reset by a
rescan — they are keyed by paths, not by positional ids. Rule:
[events-and-series.md](../../business/rules/events-and-series.md).

**Double-clicking opens the file** in the default system application. Works even
on tabs that move nothing — opening does not modify the archive.

## Visual Styling

Color palette, spacing, and typography are defined once as CSS variables on
`:root`, rather than scattered across rules. Dark mode is not a separate stylesheet
— the same variables receive alternate values under `prefers-color-scheme: dark`,
so any element consuming a token adapts automatically without its own `@media`
rule. Introducing raw colors or font sizes breaks this consistency silently —
working fine until someone toggles the system theme.

The header (tabs, Rescan button, toolbar) and bottom apply bar are pinned
(`position: sticky`) to top and bottom of the viewport rather than scrolling
with the group list — across hundreds of cards, this ensures actions remain one
click away. The toolbar wraps onto multiple rows on narrow viewports rather than
clipping controls, since the number of simultaneously visible controls (source
folder, series thresholds, show skipped, zoom, pagination) depends on the active
tab.

Each group (duplicate, series, suggestion) has a header — a description bar on
the left, with rotation button and "skip this group" toggle on the right. This
toggle was previously a separate control at the bottom right of each card
(double-sized checkbox); the user requested moving it back to the group header,
where attention naturally lands after reviewing a row of photos, avoiding
needless separation of group actions. Size and prominence were preserved through
actual element dimensions rather than CSS scaling.

Buttons, inputs, and dropdowns share a uniform appearance (borders, border radius,
`:hover`/`:focus-visible` states) across all tabs — disparate styles for identical
form controls would imply differing semantics.

## Compression Suggestion Tab

Built on the same architecture as the grouping suggestions tab: identical source
folder selection, same lazy background computation with progress bar, same
principle of "client submits selection from a server-built list, not an arbitrary
path". Default directory is `!NEW`.

**One deliberate difference from grouping:** folder selection in the dropdown is
shallow, but **scanning the selected directory is recursive without depth
limits**. Vacation videos sit in subfolders; a user selecting a vacation folder
expects all of them, not just those at root level.

The `source` parameter in `GET /api/compress` passes through the **same two
gates** as `/api/placement`: syntax (relative path, no `..`, no drive letter, no
illegal Windows characters), followed by membership in the server-built directory
index. Rejection returns `400` without triggering computations. Computation is
per-directory, and concurrent initial requests for the same directory do not
spawn duplicate computations.

`POST /api/rescan` clears the compression registry like others, but does **not**
invalidate data describing the machine or disk rather than the run: detected
encoders, calibration results, or skips (keyed by file paths).

Each candidate receives an estimation table — one row per quality profile,
columns for size and time. Values lacking measurements on this machine are marked
approximate, and complete absence of a baseline produces a question mark; where
they originate: [compression.md](compression-mechanism.md).

### Non-Sticky Controls

The "delete originals upon completion" checkbox and lower quality consent
checkbox **reset upon every render**. These are the only places in the UI where
checking a box leads to moving files or lowering quality thresholds without a
second confirmation click, so neither may persist into a subsequent, unrelated
operation. For the same reason, they have no representation in `config.json`.

While the queue is running, both checkboxes and the "Quality" selector are
**disabled and reflect values of that specific queue** — read strictly at click
time, leaving them active would misleadingly imply changes still take effect.

The entire action bar — "Quality", both checkboxes, "Compress", queue progress,
and aggregate summary line — resides in the **sticky top header**, on a dedicated
line immediately below "Source folder". Candidate lists can be long; launch
button and progress of an hour-long queue must remain visible without scrolling
to page bottom.

### "Quality" Selector and Performance Calibration

A single "Quality" selector chooses both profile and target encoding device at
once. Options share **exact names with rows in the estimation table** under
videos (shared `COMPRESS_PROFILE_LABELS` list), and an entry is generated only
for profile–device pairs whose encoder `hwdetect.py` discovered on this machine
(CPU included — `GET /api/compress/encoders` returns `libx265`/`libsvtav1` as
standard entries). Pairing a profile with an incompatible encoder's device is
impossible in the UI, and the server validates it again anyway
(`compress.resolve_device()`). Device name is appended to the label only when an
encoder possesses multiple devices (e.g., two NVIDIA cards) — otherwise they
would be indistinguishable. Selection is retained across re-renders, and during
queue execution displays the profile and device of **that** queue.

For devices lacking measurements on this machine, a "Calibrate" button appears —
triggering `POST /api/compress/calibrate` on the first visible candidate, running
in the same single-slot queue as "Compress" (never concurrent with real
compression). Once calibrated, the disappearing button indicates the device now
has a measured number instead of a question mark — without separate client-side
tracking. Calibration mechanics:
[compression.md](compression-mechanism.md).

The estimation table features a fourth column, "Quality (dB)": XPSNR-Y difference
relative to `libx265` on the same machine, `"reference"` for `libx265` itself,
and a question mark if either measurement is missing.

### Summary and Results Panel

Above the encode launch button sits a summary line: prior to execution —
**estimated** size and time for currently selected candidates and chosen profile;
after completion — **actual** aggregate size before/after and elapsed time.
Numbers are restricted to candidates from the active folder, as the queue is
singular and shared across folders.

During queue execution, progress text shows `~estimated/elapsed` (e.g.,
"compressing in progress (~15min/2min)..."): estimate is the sum of task encoding
estimates for their profile (excluding verification time, which is unmeasured —
hence elapsed time can exceed estimate), elapsed is time already consumed. Tasks
without estimates contribute nothing; a queue without any estimates shows `~?`.

The results panel — list of originals ready to be moved to trash — is
reconstructed **from manifests on disk**, not session memory. Closing the browser
or restarting the server mid-queue cannot wipe out "these originals can now be
discarded". A row appears only when both files — original and result — physically
exist; following the same discipline as the Restore tab.

A row lacking a confirmed, verified result **has no checkbox**. This invariant is
enforced server-side; the interface merely reflects it. The "Select all" checkbox
above the list toggles all rows simultaneously and is checked if and only if all
rows are selected. Moving files still requires an explicit button click and
confirmation.

One queue run constitutes one `!MOVES` batch and — if original removal was
requested — one `!REMOVE` batch, both named after queue start time. Files append
as they finish, making the batch visible (and restorable) while encoding is still
in progress; undo reverses whatever has completed so far.

## Restore Tab

The list covers **all** batches found on disk, not just those created in the
current browser session. An undo button that forgets yesterday's batch upon
server restart is only half a feature.

**The count of restorable files is calculated using the exact same check that undo
itself performs**: destination file must exist, and source location must be free.
A looser rule would have the list promise an undo that the actual operation
refuses — leaving the user unable to tell which is misleading. For the same
reason, thumbnail paths derive from the same resolution function: preview
displays the file that undo will actually move.

Entries with `planned` status count toward neither total — having never left the
source location, there is nothing to undo.

**A batch where nothing succeeded is not an already undone batch.** These two
states have distinct UI labels: "already undone" indicates reversal occurred,
whereas a batch with zero successful moves never moved anything in the first
place.

### Missing Manifest vs Unreadable Manifest

These are distinct situations handled differently by the UI.

A batch directory **without** `manifest.csv` is **silently skipped**. Trash
stores files alongside manifests, so a directory partially cleaned up manually
or originating from an older tool version is normal, not an error.

A manifest that **exists** but cannot be read appears in the list as an entry
**with an error message**. This is precisely a batch the user must be informed
about. An exception would blank the entire screen over one broken item. A torn
final line does not fall into this category — the manifest layer tolerates it by
design, see [manifest.md](manifest-and-db.md).

Thumbnail preview is **loaded lazily on demand**, specifically for the selected
batch. The main list shows only the count, as individual batches in this archive
contain well over a hundred files.

## Developer Mode

An unlabeled checkbox in the bottom-left corner, disabled by default, present on
every tab. It exposes two things: raw results panel (JSON for each batch with
undo button) and **series detection threshold tuning**.

Thresholds are visible only when two conditions are met simultaneously: developer
mode enabled **and** Series tab active — having no effect on other tabs, they
would otherwise be dead controls. Value lists initialize from currently active
server values; the page contains no hardcoded engine defaults, ensuring a session
started with `--series-hamming-threshold 3` displays 3.

**Modifying a threshold sends no request.** The value reaches the engine
exclusively in the body of `POST /api/rescan` — making the promise "takes effect
after Rescan" genuine. Until then, the block is marked pending, and the indicator
clears only upon server confirmation, never on optimistic client assumptions.

Value boundaries are enforced by the server, not the `<option>` list: out-of-range
values are rejected **before** walking the disk, as a multi-minute scan
culminating in "that number is invalid" would be the worst outcome. New
thresholds are applied in the same critical section where `_run_rescan()` swaps
`_LazyComputation` objects — preventing concurrent `GET /api/series` from starting
a new computation using old thresholds and caching it as ready.

Meaning of thresholds and effects on skipped groups:
[events-and-series.md](../../business/rules/events-and-series.md).

## Thumbnails

Generated once into the thumbnails directory, then read thereafter. For duplicate
groups, **one** thumbnail suffices, as all group members are byte-identical; its
ID is simply the group hash. Series, event, and suggestion files receive per-file
thumbnails with distinct prefix IDs — avoiding namespace collisions between two
independent ID schemes.

Videos receive a single frame captured from the first few seconds of recording,
as frame zero is often black. Codec absence, corrupted files, or timeouts yield a
clean placeholder tile, never an exception — a single bad video does not crash
the screen.

## Shutdown

The server responds to keyboard interrupts (Ctrl+C) in approximately 1.5 seconds.
This requires waiting on threads **with timeouts**: on Windows, acquiring locks
without timeouts is uninterruptible, and keyboard interrupts only set flags
noticed on the main thread's subsequent bytecode execution.


### From code: `tidy/media/thumbnails.py`
```python
    DecompressionBombError is a plain Exception subclass (not an
    OSError), raised by Image.open()/.convert()/.thumbnail() whenever a
    source image exceeds Pillow's MAX_IMAGE_PIXELS guard (~89 MP) -- a
    realistic size for large scans/panoramas in a photo archive, and
    nothing here raises MAX_IMAGE_PIXELS. Without it in _DECODE_ERRORS it
    would propagate uncaught out of ensure(), breaking this method's own
    "never raises" contract for exactly the kind of file it exists to
    guard against.
```


### From code: `tidy/media/thumbnails.py`
```python
Degradation: docs/technical/dependencies.md
pillow-heif plugs a HEIC/HEIF decoder into Pillow's own
Image.open() via register_heif_opener() -- once called, ".heic"/".heif"
sources are opened exactly like any other Pillow-decodable image
everywhere else in this module (and in series.py/dhash.py/sharpness.py/
timeline.py, which all go through plain Image.open() too). Guarded the
same defensive way as Pillow itself above: an environment without
pillow-heif installed must still import this module and keep decoding
jpg/jpeg/png -- only HEIC/HEIF support is lost, tracked via
_HEIF_AVAILABLE (checked per-extension in supports() below).
```


### From code: `tidy/media/thumbnails.py`
```python
        OSError/RuntimeError alongside ImportError: mirrors _ffmpeg_exe()'s
        guard below -- pillow-heif can be installed but still fail here if
        its native libheif fails to initialize, not just when the package
        itself is missing.
```


### From code: `tidy/media/thumbnails.py`
```python
Thumbnails are always generated at 2x the original 320px baseline, so
the review UI's thumbnail-zoom control (tidy/review, --thumb-scale)
has real pixels to show at 2x display size instead of upscaling a
blurrier source. Existing cached files under .dedup/.thumbs/ from before
this change stay at the old resolution -- ThumbnailStore.ensure() only
checks whether a file already exists at thumb_id's path, it has no
notion of "stale" -- so upgrading an existing archive's thumbnails needs
a one-time delete of that cache directory (purely generated and
disposable).
```


### From code: `tidy/media/thumbnails.py`
```python
Extensions Pillow can decode in this archive, either natively (jpg/jpeg/
png) or via the pillow-heif plugin registered above (heic/heif). RAW/psd/
cdr are already excluded from anything visual. Kept extension-only,
independent of whether pillow-heif is actually resolvable -- see
kind()/supports() split below, same pattern used for video/ffmpeg.
```


### From code: `tidy/media/thumbnails.py`
```python
Video containers this archive actually contains that ffmpeg (via
imageio_ffmpeg's bundled binary) can decode a frame from. Kept
extension-only, independent of whether ffmpeg is
actually resolvable -- see kind()/supports() split below.
```


### From code: `tidy/media/thumbnails.py`
```python
ffmpeg subprocess timeout for a single frame-extraction attempt.
15s was verified empirically against real files during development
(synthetic lavfi-generated clips and a corrupt/truncated .mp4 both fail
in well under a second -- ffmpeg detects "moov atom not found" instantly
and exits non-zero rather than hanging); 15s is a generous ceiling for a
slow/locked network drive or a genuinely pathological file, not a value
tuned to any observed flakiness.
```


### From code: `tidy/media/thumbnails.py`
```python
            Verified for real (not just in theory) against this
            machine's disk: unlike POSIX rename, Windows' MoveFileEx can
            sporadically raise PermissionError/OSError when two threads
            race to replace the *same* destination at nearly the same
            instant, even though each source tmp_path is independently
            unique. If another thread's replace already won (out_path
            exists), its content is equally valid for this thumb_id --
            only a genuine failure (out_path still absent) is real.
```


### From code: `tidy/media/thumbnails.py`
```python
        PIL's Image.rotate() turns counter-clockwise for a positive
        angle; negating quarter_turns*90 makes the button's single click
        read as "rotate clockwise", the direction users expect from a
        rotate icon on a photo they're viewing right-side-up-but-tilted.
```


### From code: `tidy/media/thumbnails.py`
```python
                Without this, thumbnails ignored the
                source's EXIF Orientation tag entirely, so any photo
                whose camera/phone stores pixels landscape-but-tagged-
                rotated (a very common case) displayed sideways/upside
                down in the review UI. exif_transpose() rotates the pixel
                data to match what every other viewer already shows and
                strips the tag, so the thumbnail alone is self-consistent.
```


### From code: `tidy/review/computations.py`
```python
        No db_path or no candidates means there is nothing to compute
        (every existing test/fixture that doesn't
        pass these -- the overwhelming majority -- must keep working
        unchanged) -- ready with an empty group list, never an
        exception from Cache(None) or an empty-input engine call.
```


### From code: `tidy/review/computations.py`
```python
            Merged into series_registry, NEVER into file_registry -- see
            the module docstring's registry
            separation table. _validate_selections() (POST /api/apply)
            only ever looks at file_registry, so a series member id can
            never be resolved into something that endpoint moves; only
            _validate_series_selections() (POST /api/apply-series) and
            handle_open() (POST /api/open) ever read series_registry.
```


### From code: `tidy/review/computations.py`
```python
        sizes_by_path (build_events_views()'s requirement -- see its
        docstring) is built from `candidates` regardless of whether
        find_events() actually runs below, so the empty-input short
        circuit still produces a normal (trivially empty) call.
```


### From code: `tidy/review/computations.py`
```python
            events_registry is read ONLY by handle_open() -- never
            merged into file_registry or series_registry, so an
            event member id can never be resolved by either apply
            endpoint.
```


### From code: `tidy/review/skips.py`
```python
    Memory is updated only AFTER the write succeeded, so the in-memory
    mirror can never claim something the table does not contain -- the next
    start would then "forget" a skip the UI had already hidden.
```


### From code: `tidy/review/state.py`
```python
    Resolved ONCE at queue time by compress.resolve_device() (docs/technical/components/compression-mechanism.md
    E3.4), never re-resolved mid-queue: "" for CPU profiles; for
    hardware, `device` is the EncoderInfo.device passed straight to
    encoder.build_command(), `device_key` is its calibration identity
    (hwdetect.EncoderInfo.device_key), used to build this job's manifest
    `reason` (compress.manifest_reason()) and to key its calibration
    write-back (E4).
```


### From code: `tidy/review/state.py`
```python
    Wall-clock markers for the review UI's live "estimated / elapsed"
    display -- started_at is set once, when status first reaches
    "encoding"; finished_at is set once, at whichever terminal status
    (done/failed/cancelled) the job ends at, so elapsed time freezes
    instead of continuing to grow in a later GET /api/compress/jobs poll.
```


### From code: `tidy/review/state.py`
```python
    device_key -> {"bpp": float, "mpx_s": float, "xpsnr_y": Optional[float]}
    for each device actually measured so far -- a device_key can be
    absent (its own encode/measure failed) without failing the whole job.
```


### From code: `tidy/review/state.py`
```python
    Per-group tracking, not a single one-shot lock (any second POST
    /api/apply getting a flat 409 regardless of what it asked for), since
    "Zastosuj wszystko" and
    "Zastosuj biezaca strone" both need to submit MULTIPLE times over a
    session -- once per button click, each potentially a different subset
    of groups. `applied_group_ids` is the server-side allowlist a new
    request's group_ids are checked against (never trust the client to
    simply not resubmit an already-moved group); `applied_batches` is an
    append-only log, one entry per successful apply, each carrying its
    OWN batch_dir/manifest_path -- never overwritten, matching
    applier.apply_batch() creating a fresh !REMOVE\<timestamp>\ directory
    per call (see applier._make_unique_batch_dir()).
```


### From code: `tidy/review/state.py`
```python
    Inputs for the series/events engines, all with defaults
    that make the "not provided" case (every existing test/fixture, and
    every start_server()/serve() caller before this step) behave exactly
    like today -- see _run_series_computation()/_run_events_computation()
    above: no db_path or no candidates means an immediate "ready" with an
    empty group list, never a crash from Cache(None) or similar.
```


### From code: `tidy/review/state.py`
```python
    Tuning knobs mirror the CLI's own --series-window-seconds/--series-
    hamming-threshold/--event-gap-hours/--gps-jump-km flags (tidy/cli) --
    defaulted from the SAME constants the engines themselves default to
    (series.DEFAULT_*/events.DEFAULT_*), never a value typed in by hand
    here, so this module can never silently drift out of sync with the
    engines it calls.
```


### From code: `tidy/review/state.py`
```python
    --workers, forwarded as-is into both series.find_series() and
    events.find_events() -- one knob for both computations'
    ThreadPoolExecutor, mirrored from progress.py's own default so this
    module never drifts out of sync with it either.
```


### From code: `tidy/review/state.py`
```python
    Progress counters: one per lazy computation, created once per
    ReviewState (not per computation run -- there is only ever one real
    run per process, since _LazyComputation never recomputes once
    "ready") and updated from inside the worker thread, polled from the
    HTTP handler thread via .snapshot(). See
    progress.ProgressCounter's own docstring for the thread-safety
    reasoning.
```


### From code: `tidy/review/state.py`
```python
    THE key safety property of this step (see module docstring's
    registry-separation table) -- series_registry is
    populated ONLY by build_series_views() and is read by BOTH
    _validate_series_selections() (POST /api/apply-series) and
    handle_open() (POST /api/open); events_registry is populated ONLY by
    build_events_views() and is read ONLY by handle_open() -- events
    stay read-only, so no apply endpoint is ever given this dict.
    _validate_selections() (POST /api/apply, the code path that ends in
    applier.apply_batch() moving exact-duplicate files to !REMOVE) is
    NEVER given either dict.
```


### From code: `tidy/review/state.py`
```python
    Series' own "already applied" allowlist, checked by
    POST /api/apply-series exactly like applied_group_ids gates
    POST /api/apply -- kept separate because a series group_id ("s1") and
    a duplicate group_id ("1") live in disjoint id spaces but could in
    principle collide as bare strings if merged into one set.
```


### From code: `tidy/review/state.py`
```python
    The in-memory mirror of cache.py's `series_skips` table:
    group_key -> that skip's normalized member paths (see
    series.normalize_member_paths). Loaded once by _load_series_skips() at
    start_server(), and thereafter the ONLY thing any request thread reads
    -- GET /api/series consults it on every poll, and a sqlite open per poll
    would be both slow and impossible (cache.py's connection cannot cross
    threads, see _run_series_computation()'s docstring). Writes go through
    POST /api/series-skip, which updates this dict AND the table together.

    Deliberately NOT reset by _run_rescan(), unlike every applied_*_group_id
    set: those are keyed by ids the rescan renumbers, while a skip is keyed
    by the member paths themselves, which mean the same thing before and
    after a rescan (same reasoning as the front-end's destUsage).
```


### From code: `tidy/review/state.py`
```python
    Guards series_skips alone. Its own lock, not state.lock: a
    checkbox click must never queue behind an in-flight apply, and an apply
    must never wait on one.
```


### From code: `tidy/review/state.py`
```python
    Keyed by source directory: unlike series/events, this screen
    can be pointed at a different directory, and each one is its own
    computation. _LazyComputation still never recomputes a key once ready,
    so switching back to a directory already looked at is instant.
```


### From code: `tidy/review/state.py`
```python
    One counter per source directory, reported by GET /api/placement and
    fed by every step of _run_placement_computation() -- including the
    date index below, which used to tick a counter of its own that no
    endpoint ever read (so its minutes of work looked like a stalled bar).
```


### From code: `tidy/review/state.py`
```python
    Guards creation of the per-source entries above -- NOT shared with
    state.lock, for the same reason _LazyComputation has its own: a
    placement computation running for minutes must never block an apply.
```


### From code: `tidy/review/state.py`
```python
    Bumped once per placement computation, and never reset -- not even by
    a rescan. It is what makes the ids of two source directories disjoint
    (see next_placement_id_prefix() and build_placement_views()); a
    counter that restarted would re-create the collision it exists to
    prevent the first time the user came back to an earlier directory.
```


### From code: `tidy/review/state.py`
```python
    The same persisted-skip mechanism as series_skips above, mirrored for
    placement suggestions. Its own dict/table (cache.placement_skips), never merged
    with series_skips: see cache._ensure_placement_skip_schema()'s
    docstring for why a shared table would be wrong here (a series group
    and a placement suggestion can legitimately cover the same paths).
    Loaded once by _load_placement_skips() at start_server(); writes go
    through POST /api/placement-skip. Deliberately NOT reset by
    _run_rescan(), same reasoning as series_skips.
```


### From code: `tidy/review/state.py`
```python
    POST /api/rescan's engine, injected by tidy/cli rather than called
    from here: this module knows nothing about argparse, --roots or the
    keeper config, and re-deriving the scan pipeline inside the server
    would make a second place that has to stay in step with the CLI's.
    None (every test/fixture that doesn't pass one) means the endpoint
    reports itself unavailable instead of pretending to refresh.
```


### From code: `tidy/review/state.py`
```python
    Keyed by source directory, same reason as placement_computations:
    this screen can be pointed at a different directory, and each one is
    its own computation that never recomputes once ready.
```


### From code: `tidy/review/state.py`
```python
    One counter per source directory, fed by every probed file in
    _run_compress_computation() -- see placement_progress's own comment
    for why this must be a real per-step counter, not a bar that sits at
    0 until the whole directory is done.
```


### From code: `tidy/review/state.py`
```python
    Guards creation of the per-source entries above, plus hwdetect_cache
    below -- its own lock, not state.lock and not placement_lock: probing
    every video in a directory with ffprobe (and, for
    GET /api/compress/encoders, test-encoding a frame per hardware
    encoder) can take minutes, and must never queue behind -- or block --
    an apply, a restore, or the placement screen's own background work.
```


### From code: `tidy/review/state.py`
```python
    Bumped once per compress computation, and never reset -- not even by
    a rescan. Same purpose as placement_id_epoch: it is what makes the
    ids of two source directories disjoint (see next_compress_id_prefix()
    and build_compress_views()); a counter that restarted would
    re-create the collision it exists to prevent the first time the user
    came back to an earlier directory.
```


### From code: `tidy/review/state.py`
```python
    hwdetect.detect()'s result, memoised here by _hwdetect_for() -- see
    its own docstring for why this must survive across requests and
    across a rescan (encoders describe the MACHINE, not this scan).
```


### From code: `tidy/review/state.py`
```python
    Read from the same config.json as camera_offsets/dump_folder_names
    (tidy/cli's --review branch), so hwdetect.detect() and
    compress.load_compress_config() resolve the SAME "compression"
    section every other consumer of --keeper-config does -- never a
    second, silently-diverging path to the same file.
```


### From code: `tidy/review/state.py`
```python
    Where _run_compress_queue() stages ffmpeg output before a verified
    result is moved into the archive -- the review-server equivalent of
    tidy/cli's --compress-staging-dir, threaded through start_server()/
    serve() the same way compress_config_path is (tidy/cli's --review
    branch passes args.compress_staging_dir, DEFAULT_COMPRESS_STAGING_DIR
    by default -- never a second, independently-invented default here).
```


### From code: `tidy/review/state.py`
```python
    Guards compress_jobs, compress_queue_running and the identity of
    compress_cancel_event -- its own lock, not compress_lock (which guards
    the CANDIDATE-LIST registries/computations) and not state.lock (which
    guards the apply endpoints for the other four tabs): a multi-minute
    encode running under this lock's discipline must never be blocked by,
    or itself block, an unrelated apply/restore/rescan.
```


### From code: `tidy/review/state.py`
```python
    Reassigned (never .clear()'d) at the start of every fresh queue by
    POST /api/apply-compress -- a stale Event object still referenced by a
    just-finished worker thread must never be reused for the next run.
```


### From code: `tidy/review/state.py`
```python
    POST /api/compress/calibrate's own job (docs/technical/components/compression-mechanism.md) -- the
    CURRENT/last run only, same replace-wholesale shape as compress_jobs.
    Shares compress_queue_lock/compress_queue_running/compress_cancel_event
    with the CompressJob queue above ON PURPOSE: calibration and a real
    compress apply must never run at the same time, both drive the one
    real encoder this machine has.
```


### From code: `tidy/review/state.py`
```python
    applied_compress_group_ids (declared above, next to compress_id_epoch)
    doubles as this queue's "already submitted" guard: POST
    /api/apply-compress adds the selected group_ids to it at QUEUE time
    (not completion time), so a second POST for the same selection while
    the first is still encoding also 409s -- same discipline every other
    apply endpoint's applied_*_group_ids follows.

    The persisted "pomin ten plik" decision for the compress screen --
    same shape/reasoning as series_skips/placement_skips (module
    docstring): keyed by member paths via series.skip_key(), never by
    group_id, and deliberately NOT reset by _run_rescan().

    dup_skips / dup_skip_lock: the same mirror for the "Exact
    duplicates" tab (cache.dup_skips), loaded by _load_dup_skips() at
    start_server(), written through POST /api/dup-skip, guarded by its
    own lock, and likewise NOT reset by a rescan.
```


### From code: `tidy/review/state.py`
```python
    batches.list_batches() walks !MOVES/!REMOVE and reads every manifest
    found there -- cheap (a dozen-odd directories on this archive) but not
    free, and pointless to redo on every poll of a tab nobody left. None
    means "not computed yet"; invalidated by _invalidate_batch_cache()
    after anything that could change which batches exist or which of
    their rows are still restorable (an apply, a restore, a rescan).
```


### From code: `tidy/review/state.py`
```python
    Bumped by _run_rescan() only. POST /api/restore compares this against
    the scan_generation an applied_batches entry was recorded under
    before touching applied_group_ids/applied_series_group_ids/
    applied_placement_group_ids -- after a rescan every group_id is
    renumbered from 1, so blindly clearing an old id would unmark
    whichever unrelated group inherited that number (see _run_rescan()'s
    own comment on why it clears those sets outright).
```


### From code: `tidy/review/state.py`
```python
            The series tuning the next computation will use, plus the
            engine's own defaults. The dev-mode selects initialise from
            these instead of from hardcoded HTML values, so a session
            started with --series-hamming-threshold 3 shows 3 -- a control
            that cannot display the value in force would misdescribe the
            groups on screen. Defaults come along because "which of these
            is the standard one" is not derivable client-side without
            duplicating series.DEFAULT_* in JS.
```


### From code: `tidy/review/handler/routes_compress.py`
```python
    "applied"/"skipped" are stamped on every response rather than
    baked into the cached result, same reasoning as
    handle_placement()'s own folders/suggestions. "applied" reflects
    state.applied_compress_group_ids and "skipped" the compress_skips table.
```


### From code: `tidy/review/handler/routes_compress.py`
```python
    Real per-machine measurements only, written back by
    _run_compress_queue() after a completed job -- an encoder
    this machine has never actually compressed with stays
    uncalibrated rather than borrowing config.SEED_CALIBRATION's
    single-sample, single-machine guess.
```


### From code: `tidy/review/handler/routes_compress.py`
```python
            Frozen at job.finished_at once the job reaches a
            terminal status -- see CompressJob's own docstring for
            why elapsed time must stop growing there instead of
            ticking up forever on a later poll.
```


### From code: `tidy/review/handler/routes_compress.py`
```python
    POST /api/compress/skip body: {"group_id": "c1_3", "skip":
    true}. Exact mirror of handle_placement_skip() above,
    swapping placement_* for compress_* throughout.
```


### From code: `tidy/review/handler/routes_compress.py`
```python
    POST /api/compress/cancel, empty body. Sets
    state.compress_cancel_event when a queue is currently
    running; a no-op (still 200) otherwise -- there is nothing
    to cancel, not an error. The worker thread (_run_compress_queue)
    polls this event between jobs and inside encoder.run_encode()
    itself (its own cancel_event parameter).
```


### From code: `tidy/review/handler/routes_compress_apply.py`
```python
    POST /api/compress/calibrate body: {"file_id": "c1_1:0",
    "device_keys": [...]}, device_keys optional (default: every
    detected device not yet calibrated on this machine).
    docs/technical/components/compression-mechanism.md. Runs in the SAME single-slot queue as
    POST /api/apply-compress -- never concurrently, both drive
    the one real encoder this machine has.
```


### From code: `tidy/review/handler/routes_compress_apply.py`
```python
    POST /api/apply-compress body: {"selections": [{"group_id",
    "file_ids"}, ...], "profile": "x265", "remove_originals":
    false}. Unlike every synchronous apply endpoint above, the
    actual work here is asynchronous: state.lock wraps only the
    check-then-validate-then-launch part, never the encode
    itself, which would block the whole server for minutes.
```


### From code: `tidy/review/handler/routes_compress_apply.py`
```python
    compress.resolve_device() is the ONLY path that turns a
    client-supplied device_key into an EncoderInfo -- it checks
    membership in hw.encoders itself, so an unknown id, a
    mismatched-encoder id, or any id at all for a CPU profile all
    come back as ok=False here (docs/technical/components/compression-mechanism.md), never reaching
    the queue below.
```


### From code: `tidy/review/handler/routes_compress_apply.py`
```python
    Lock order throughout this module is state.lock (outer) ->
    compress_queue_lock (inner) -- never the reverse -- so this
    nesting can never deadlock against _run_compress_queue()'s
    own (separate, never-nested) uses of either lock.
```


### From code: `tidy/review/handler/routes_compress_apply.py`
```python
            A fresh Event, never .clear()'d -- a stale Event
            object still referenced by a just-finished worker
            thread must never be reused for this run.
```


### From code: `tidy/review/handler/routes_compress_apply.py`
```python
        Marked applied NOW, at queue time -- not when the queue
        finishes -- so a second POST for the same selection
        while the first is still encoding also 409s above.
```


### From code: `tidy/review/handler/routes_compress_apply.py`
```python
    POST /api/apply-compress-remove body: {"selections":
    [{"group_id", "file_ids"}, ...]} (docs/technical/components/review-ui.md).
    Ids resolve ONLY through compress_registry entries carrying
    a "compressed_path" (see _validate_compress_remove_selections()),
    so an unverified candidate row structurally cannot be
    selected here.
```


### From code: `tidy/review/handler/routes_dup.py`
```python
    POST /api/thumb/rotate body: {"thumb_id": "<id>"}. Same
    allowlist rule as GET /api/thumb/<id>: thumb_id is opaque and
    untrusted, resolved ONLY through state.thumb_sources (built by
    build_group_views()/build_series_views()/build_events_views(),
    never derived from client input) -- a client can never rotate
    a cached file it wasn't already allowed to fetch. Reached only
    after do_POST()'s _check_mutating_request() gate has passed.
```


### From code: `tidy/review/handler/routes_dup.py`
```python
    rotate() only ever rewrites an already-cached file; ensure()
    first guarantees one exists (e.g. this thumb was never
    actually fetched by the browser yet, an edge case but not one
    this endpoint should 404 on if the source itself is fine).
```


### From code: `tidy/review/handler/routes_dup.py`
```python
    POST /api/open body: {"file_id": "<id>"}. Same rule as
    /api/apply's selections: `file_id` is opaque and untrusted,
    resolved to a real filesystem path ONLY through
    `state.file_registry` (the exact same allowlist dict
    build_group_views() built for /api/apply) -- a client can
    never make this endpoint open an arbitrary path of its own
    choosing, whether or not that path happens to also exist on
    disk. Reached only after do_POST()'s _check_mutating_request()
    gate has already passed for this request.
```


### From code: `tidy/review/handler/routes_dup.py`
```python
    file_registry (exact-duplicate groups) is tried first, then
    series_registry, then events_registry, then placement_registry,
    then compress_registry -- the order is arbitrary for /api/open
    itself (all five are equally valid "openable" ids here, unlike
    the apply endpoints, each of which must never see the others'
    registries) and cannot resolve an id ambiguously either way,
    since the five id namespaces are disjoint. A fixed order still
    matches every other lookup in this module.
```


### From code: `tidy/review/handler/routes_dup.py`
```python
        No shell=True, no subprocess, no string built from client
        input -- just the OS "open with default app" call on a
        path that came exclusively from file_registry above
        (docs/technical/security.md).
```


### From code: `tidy/review/handler/routes_dup.py`
```python
    JSON parsing/validation runs strictly after the Host/Origin/
    token gate above -- an attacker who fails that gate never
    gets a different error message (400 vs 403) to distinguish
    a well-formed-but-rejected body from a malformed one. Shared
    by both apply endpoints (dup and series) -- only the
    shape of `selections` inside the body differs from here on.
```


### From code: `tidy/review/handler/routes_dup.py`
```python
        Checked before _validate_selections() and over
        the whole request at once -- a request that mixes an
        already-applied group_id with brand-new ones is rejected
        in full (409), exactly like a step-3 shape error rejects
        the whole request rather than applying the valid part.
```


### From code: `tidy/review/handler/routes_dup.py`
```python
        selections is known (post-validation) to be a list of
        dicts each with a unique, string group_id -- this is the
        exact set of groups this batch just moved.
```


### From code: `tidy/review/handler/routes_placement.py`
```python
    "applied" is stamped per suggestion on every response, never
    baked into the immutable cached result, so a suggestion just
    applied stops being offered on the very next poll without
    recomputing the whole directory. "skipped" is stamped the same
    way, for the same reason -- the persisted decision changes on
    every checkbox click, while the cached computation result is
    frozen once ready. Sent for every suggestion, never by
    omitting the skipped ones (same reasoning as GET /api/series's
    skipped_ids).
```


### From code: `tidy/review/handler/routes_placement.py`
```python
    POST /api/placement-skip body: {"group_id": "p3", "skip": true}.
    Same round-trip shape as handle_series_skip() above, same
    reasoning: the DB write happens on click, not at apply time.
    Reached only after do_POST()'s _check_mutating_request() gate
    has already passed.
```


### From code: `tidy/review/handler/routes_placement.py`
```python
    JSON parsing/validation runs strictly after the Host/Origin/
    token gate above -- an attacker who fails that gate never
    gets a different error message (400 vs 403) to distinguish
    a well-formed-but-rejected body from a malformed one. Shared
    by both apply endpoints (dup and series) -- only the
    shape of `selections` inside the body differs from here on.
```


### From code: `tidy/review/handler/routes_placement.py`
```python
    The op=move counterpart of /api/apply-series.
    Same shape -- own registry, own applied-set, same
    check-then-validate-then-move ordering under state.lock.
    `file_ids` (blue cards) end in applier.apply_moves(), which
    refuses a destination inside !REMOVE; `remove_ids` (red
    cards) end in applier.apply_batch() with reason
    `event-remove` and an empty keeper. Each non-empty half is
    its own batch with its own batch_id; when both exist the
    response is the move batch with the remove batch nested as
    `remove_batch`. Either list may be empty, not both; ids
    must be disjoint; `dest` is validated only when `file_ids`
    is non-empty.
```


### From code: `tidy/review/handler/routes_placement.py`
```python
            batch_id/kind let POST /api/restore find
            this entry again by id and know which applied_*
            set to un-mark from.
```


### From code: `tidy/review/handler/routes_placement.py`
```python
            Surfaced rather than summed into "failed": a
            refused destination (D25) is a different problem
            from a locked file, and the user can only act on
            it if they are told which entry and why.
```


### From code: `tidy/review/handler/routes_placement.py`
```python
    A move creates directories, so the cached directory index
    is stale the moment this returns -- the next poll must see
    the folder that was just created as a candidate. The new
    batch itself makes the "Cofnij" tab's list stale too.
```


### From code: `tidy/review/handler/routes_restore.py`
```python
    The "Cofnij" tab's inventory of
    every !MOVES/!REMOVE batch on disk, not just this session's --
    read-only (no mutating-request gate, same as GET /api/state),
    and cached on the state exactly like _dir_index() caches
    list_archive_dirs() (batch_list_cache is invalidated by the
    same events: apply, restore, rescan).
```


### From code: `tidy/review/handler/routes_restore.py`
```python
    GET /api/batch-entries?batch_id=<id> -- the "Cofnij" tab's
    lazy, per-batch file list, fetched only when a row is expanded
    (nothing here runs on tab load, unlike GET /api/batches).
    batch_id resolved through the same batch_registry allowlist
    POST /api/restore uses, rejected identically on an unknown
    id -- a batch this can preview is always exactly a batch that
    can be restored.
```


### From code: `tidy/review/handler/routes_restore.py`
```python
        Mirrors batches.list_batches()'s own per-batch error
        handling (its module docstring): a corrupted manifest
        must not blank this response, only say so -- the "Cofnij"
        row itself already surfaces the same error via
        BatchSummary.error, so this is not the first place a user
        would see it.
```


### From code: `tidy/review/handler/routes_restore.py`
```python
        Same lock/merge discipline as handle_batches()'s own
        batch_list_cache above (this handler runs synchronously,
        no background thread/_LazyComputation -- see
        _LazyComputation.finish_ready()'s docstring, ~1068, for
        why an id must never be handed to the browser before
        GET /api/thumb/<id> can resolve it).
```


### From code: `tidy/review/handler/routes_restore.py`
```python
    POST /api/restore body: {"batch_id": "b1"}. batch_id is opaque
    and untrusted, resolved to a real manifest path only through
    state.batch_registry -- the same allowlist rule every id in
    this module follows. batch_registry covers batches from this
    session's applies and every older !MOVES/!REMOVE batch
    GET /api/batches has ever listed, so a manifest from a
    previous session restores exactly like one from this one.

    Whole-batch only: this calls applier.restore_batch() exactly
    as `--restore` does, with no per-file selection -- one click
    undoes everything apply_moves()/apply_batch() did for that
    manifest.
```


### From code: `tidy/review/handler/routes_restore.py`
```python
        Same lock apply_moves()/apply_batch() hold for their whole
        duration -- a restore must never race an apply over the
        same files, in either direction.
```


### From code: `tidy/review/handler/routes_restore.py`
```python
        Only unmark group_ids if THIS batch was applied in the
        CURRENT scan generation -- see ReviewState.scan_generation
        for why a stale generation's ids must never be touched.
```


### From code: `tidy/review/handler/routes_restore.py`
```python
                "compress" batches (op=compress, !MOVES) are added
                by POST /api/apply-compress below; "remove" kind
                batches (op=remove, either from --compress-remove-
                originals or POST /api/apply-compress-
                remove) never populate an applied_*_
                group_ids set of their own -- a compress-removal's
                group_id is minted per-request from the removal
                candidate's own id (see handle_apply_compress_
                remove()), not tracked as "already applied" the
                way a candidate screen's selectable groups are.
```


### From code: `tidy/review/handler/routes_restore.py`
```python
            Kept separate from "restored", never summed: "already
            at the destination" and "locked/otherwise refused" are
            different problems, and only the split lets the user
            tell which one they are looking at (same reasoning as
            /api/apply-placement's own "errors" list).
```


### From code: `tidy/review/handler/routes_restore.py`
```python
    A restore recreates src_abs and, for op=move rows, empties the
    destination directory it moved out of -- the same two caches
    an apply invalidates are stale for the same reason here.
```


### From code: `tidy/review/handler/routes_state.py`
```python
    Reached only after do_GET()'s own _check_host() call --
    Host is checked on every GET, not re-checked per-endpoint, so
    every endpoint gets the same DNS-rebinding protection
    (docs/technical/security.md) without a single new line of
    security code.
    The first request for this resource starts the background
    computation (if not already started/finished) and EVERY
    request -- including this one -- returns immediately with
    whatever the current status is; do_GET() must never block.
```


### From code: `tidy/review/handler/routes_state.py`
```python
    The flag the series screen hides a group by. Sent for every
    group (True or False), never by omitting the skipped ones from
    the list: the client needs the full set to offer "Pokaz
    pominiete" and let the user take a decision back, and a group
    that vanished from the response could never be un-skipped
    from the UI at all.
```


### From code: `tidy/review/handler/routes_state.py`
```python
    "done"/"total" straight from the ProgressCounter the background
    worker thread is updating -- read here, on the request-
    handling thread, via the SAME lock-guarded snapshot() every
    other reader uses. (0, 0) before the worker has set a real
    total yet is indistinguishable from "nothing to do", which is
    fine: the front-end only renders a bar once total > 0.
```


### From code: `tidy/review/handler/routes_state.py`
```python
    series groups carry their own "applied"
    flag, same idea as to_json()'s dup groups -- computed
    fresh on every response from state.applied_series_group_ids
    (never baked into the cached _LazyComputation result
    itself, which is immutable once ready), so a group that
    was just applied stops being offered again on the very
    next GET /api/series without needing a fresh computation.
```


### From code: `tidy/review/handler/routes_state.py`
```python
        The persisted "pomin" decisions are computed fresh per
        response for the same reason `applied` is -- they
        change between polls (a checkbox click), while the
        cached computation result is frozen once ready.
```


### From code: `tidy/review/handler/routes_state.py`
```python
    The whole-archive event view. Kept (and still tested) for
    CLI parity, but the review screen no longer renders it --
    /api/placement measures one source directory against the
    rest of the archive instead of regrouping the archive
    against itself.
```


### From code: `tidy/review/handler/routes_state.py`
```python
    POST /api/series-skip body: {"group_id": "s3", "skip": true}.
    Checking "pomin" must survive the session, so the click itself
    is a server round trip rather than a purely client-side flag --
    the DB write happens now, not at apply time (a group the user
    skips is a group they will never submit, so there is no later
    moment to hang it on).

    Behind the same _check_mutating_request() gate as every other
    POST (do_POST() applies it before dispatching here): this writes
    to the user's cache DB and changes what later screens show, so a
    cross-site POST must not be able to blank someone's review.
    It moves no files -- nothing here can reach tidy/moves/applier.
```


### From code: `tidy/review/handler/routes_state.py`
```python
        400 for an unknown group_id (a client bug or a stale page),
        500 for a DB write that failed (the server's problem, and
        one the user must see -- the UI would otherwise hide a group
        that nothing on disk remembers).
```


### From code: `tidy/review/handler/routes_state.py`
```python
    A mutating-gate endpoint despite moving nothing: it walks
    the whole archive and throws away every computed view, so
    a cross-site POST could stall the tool for minutes.
```


### From code: `tidy/review/handler/routes_state.py`
```python
    Validated BEFORE the walk, not after: a refused threshold
    must leave the disk unread and every tuning field
    untouched, rather than spend minutes rescanning and only
    then report that the value it scanned for was invalid.
```


### From code: `tidy/review/handler/routes_state.py`
```python
    JSON parsing/validation runs strictly after the Host/Origin/
    token gate above -- an attacker who fails that gate never
    gets a different error message (400 vs 403) to distinguish
    a well-formed-but-rejected body from a malformed one. Shared
    by both apply endpoints (dup and series) -- only the
    shape of `selections` inside the body differs from here on.
```


### From code: `tidy/review/handler/routes_state.py`
```python
    A fully separate handler, not a branch of
    /api/apply sharing its body shape -- see the module
    docstring's registry-separation table for why. Same lock,
    same "check already-applied before validating" ordering,
    same all-or-nothing validation as the dup endpoint below,
    just against series_registry/applied_series_group_ids
    instead of file_registry/applied_group_ids.
```


### From code: `tidy/review/views/duplicates.py`
```python
        thumb_kind is extension-only (see thumbnails.kind()), independent
        of thumb_id/thumbnails.supports():
        the UI needs to tell a video with NO thumbnail (ffmpeg missing/
        failed) apart from an image with no thumbnail, even though
        neither has a thumb_id in that case.
```


### From code: `tidy/review/views/duplicates.py`
```python
                Per-file size for this card, mirroring
                file_registry[discard_id]["size"] above -- an
                exact-duplicate group means every member is the same
                size as group.size, but each card gets its OWN "size"
                key rather than relying on a shared group-level field.
```


### From code: `tidy/review/views/duplicates.py`
```python
The "p_" prefix keeps per-file thumbnail ids disjoint from the plain hex
full_hash ids used for duplicate-group thumbnails in the same thumb_sources
dict -- no full_hash ever starts with "p_". blake2b is one-way, so
thumb_sources remains the only path from an id back to a real filesystem
path; the hex digest is also safe to use directly as a filename.
```


### From code: `tidy/review/views/placement.py`
```python
        Nothing was scoped, so this IS the whole list -- not a remainder
        hidden below one. The flag would only push it into the client's
        "inne katalogi" group and leave "istniejace katalogi" empty.
```


### From code: `tidy/review/views/placement.py`
```python
    A date match is promoted OUT of the name-based scoping, not filtered
    by it. The whole failure this fixes is the right folder living under a
    top level the name never pointed at, so restricting the ranking to the
    scoped subset would reintroduce it -- `2021 MOUNTAINS` only surfaces at all
    because the calendar, not the name, put it there.
```


### From code: `tidy/review/views/placement.py`
```python
                The flag above is events.py's and means only that the
                TOP-LEVEL folder exists (`#Home`), which the screen was
                rendering as "istniejacy katalog" against the full path.
                It read as "this destination is already on disk" while
                `#Home\2022 High Mountains` did not exist at all --
                so the badge now follows this field instead, which asks
                about the folder actually being proposed.
```


### From code: `tidy/review/views/placement.py`
```python
            Only a coherent folder is offered as a single move. A dump
            folder never reaches this flag as True -- inbox.py forces it
            to "mixed" regardless of how tidy its contents look.
```


### From code: `tidy/review/views/series.py`
```python
View-model builders for the series and events screens. See
docs/technical/components/review-ui.md for the id-registry allowlist pattern shared
with build_group_views() above.
```


### From code: `tidy/review/workers/calibration.py`
```python
                Validated at submission time (POST /api/compress/
                calibrate only accepts a known device_key) -- reachable
                only if hw.encoders itself changed between validation and
                this thread starting, an unlikely race that must not
                crash the rest of the job.
```


### From code: `tidy/review/workers/calibration.py`
```python
            Staging is scratch space for this measurement alone -- unlike
            a real compress job's staged file, nothing ever moves this
            one into the archive, so it is removed right away rather
            than left for the next server start to find.
```


### From code: `tidy/review/workers/calibration.py`
```python
            Same normalization as _run_compress_queue()'s own write-back:
            a measurement taken with a non-1.0-multiplier profile (should
            not happen here -- default_profile_for_encoder() prefers a
            multiplier-1.0 profile -- but stays correct if it ever falls
            back to one that isn't) still lands on the CRF21-equivalent
            baseline other profiles for the same encoder scale from.
```


### From code: `tidy/review/workers/calibration.py`
```python
            A fresh measurement changes what estimate_all_profiles()
            would compute for EVERY source directory, not just this
            candidate's own -- but _LazyComputation never recomputes once
            ready (CLAUDE.md), so the only way to make the next GET
            /api/compress?source=... reflect it is to drop the cached
            computations, the same invalidation POST /api/rescan already
            does for the same reason. compress_registry/compress_jobs
            stay untouched: ids and job history are still valid, this is
            a change to numbers shown alongside them, not to identity.
```


### From code: `tidy/review/workers/calibration.py`
```python
Length of the clip POST /api/compress/calibrate measures each device
against -- an INPUT-side limit (encoder.build_command()'s
input_duration_limit, "-t" before "-i"), never the `trim` filter
(docs/technical/components/compression-mechanism.md).
```


### From code: `tidy/review/workers/compress_queue.py`
```python
            Missing ffmpeg degrades the feature, not the tool (CLAUDE.md's
            "Optional dependencies" contract): report an empty, ready
            screen rather than raising through .fail(), which the front
            end would render as a scan error instead of "unavailable".
```


### From code: `tidy/review/workers/compress_queue.py`
```python
        Own Cache, opened and closed on this thread: cache.py's sqlite3
        connection cannot cross threads (same rationale as
        _date_index_for(), which opens its own for the same reason).
```


### From code: `tidy/review/workers/compress_queue.py`
```python
            This machine's own real measurements, written back by
            _run_compress_queue() after every completed job -- {} (never
            config.SEED_CALIBRATION's seed values) when nothing has been
            compressed on this machine yet, so estimate_all_profiles()
            correctly falls back to the seed and marks the result "≈".
```


### From code: `tidy/review/workers/compress_queue.py`
```python
            One namespace per computation, same reason as
            next_placement_id_prefix(): compress_registry is merged, not
            replaced, across every source directory looked at this
            session.
```


### From code: `tidy/review/workers/compress_queue.py`
```python
            Staging filenames are plain indices, matching tidy/cli's own
            _run_compress() -- ffmpeg's "-y" always overwrites a leftover
            from an earlier, incomplete queue run under the same index.
```


### From code: `tidy/review/workers/compress_queue.py`
```python
            Encode-only wall time, deliberately NOT job.finished_at -
            job.started_at (that span also covers verify_output()'s own
            decode+XPSNR passes and the archive move) -- this is the
            figure compress.estimate_time() actually models, so it is
            the only one safe to feed back into calibration below.
```


### From code: `tidy/review/workers/compress_queue.py`
```python
            Verified but still sitting in staging -- "encoded" is real,
            observable state here (the move below still has to hash and
            fs.move() a whole video file), not merely a rename of "done".
```


### From code: `tidy/review/workers/compress_queue.py`
```python
            Landed in the archive NOW, one apply_compressions() call for
            just this job -- see this function's own docstring for why
            per-job beats batching every plan to the end of the queue.
```


### From code: `tidy/review/workers/compress_queue.py`
```python
                Feed this job's OWN real, measured numbers back into
                cache.compress_calibration, so the NEXT estimate for this
                encoder on this machine uses what actually happened here
                instead of config.SEED_CALIBRATION's single-sample,
                single-machine guess (docs/technical/components/compression-mechanism.md).
```


### From code: `tidy/review/workers/compress_queue.py`
```python
                    compress_calibration is keyed by DEVICE_KEY, not
                    profile (config.SEED_CALIBRATION's own shape, now
                    extended -- docs/technical/components/compression-mechanism.md) -- but x265/
                    x265_medium/x265_high share "libx265" (device_key
                    "libx265" for the CPU encoder) while targeting
                    different bpp. Divide back out the same multiplier
                    estimate_all_profiles() will re-apply on read, so a
                    measurement from ANY x265 tier normalizes to the same
                    CRF21-equivalent baseline config.SEED_CALIBRATION's
                    own libx265 entry already represents. job.device_key
                    is "" only if resolution somehow failed to set it --
                    falls back to the encoder's own bare name so a
                    measurement is still recorded somewhere sane rather
                    than silently dropped.
```


### From code: `tidy/review/workers/compress_queue.py`
```python
                Structural safety net (docs/business/rules/compression.md), per job:
                only reachable when THIS job's own move just succeeded,
                built from THIS job's own `entry` -- never from `jobs`
                directly -- so a failed encode or a failed archive move
                can never reach apply_compress_removals().
```


### From code: `tidy/review/workers/compress_queue.py`
```python
            A corrupted manifest must not take the rest of this list
            down with it -- same tolerance batches.list_batches() itself
            already applies to its own per-batch reads.
```


### From code: `tidy/review/workers/compress_queue.py`
```python
                The file physically at dst_abs right now, not this row's
                own recorded size -- see this function's own docstring on
                why an older manifest sharing this pair could otherwise
                report a stale number for whatever currently lives there.
```


### From code: `tidy/review/workers/placement.py`
```python
        Enforced here rather than assumed: a file
        the duplicates tab is about to send to !REMOVE must not also be
        offered a new home. Without this subtraction the user would be
        asked to file photos that are on their way out of the archive.
```


### From code: `tidy/review/workers/placement.py`
```python
            Both slow steps (this one and the date index below) feed the
            SAME counter the endpoint already reports, so the progress
            bar covers the whole wait, not just part of it.
```


### From code: `tidy/review/workers/placement.py`
```python
            One namespace per computation: the registry below is merged,
            not replaced, and this directory's screen stays reachable
            long after another one has been computed.
```


### From code: `tidy/review/workers/placement.py`
```python
                    A camera whose clock has drifted puts
                    photos in someone else's event, where they inherit
                    someone else's location -- and this screen is where
                    those photos get a destination folder proposed. The
                    warning has to travel with the suggestions, not be
                    left behind in the engine that produced it.
```


### From code: `tidy/review/workers/rescan.py`
```python
        Every group_id above is about to be renumbered from 1, so any
        applied_batches entry recorded under the OLD numbering must
        never be used to un-mark a group again -- POST /api/restore checks
        this counter before doing so (see ReviewState.scan_generation).
```


### From code: `tidy/review/workers/rescan.py`
```python
        Same step as the replacement above, on purpose (see this
        function's docstring): the new computation object and the values
        it will read must become visible together.
```


### From code: `tidy/review/workers/rescan.py`
```python
        The archive itself changed, so its day histogram is stale too --
        dropping it is what makes a rescan pick up a folder the user
        filled between runs. The SQLite cache keeps this cheap: only
        files whose size/mtime moved are decoded again.
```


### From code: `tidy/review/workers/rescan.py`
```python
        The archive changed, so batches.list_batches()'s restorable counts
        (each a fresh fs.exists() pair) could be stale too, even though a
        rescan never itself touches !MOVES/!REMOVE.
```


### From code: `tidy/review/workers/rescan.py`
```python
        hwdetect_cache is NOT cleared here -- it describes the machine
        (which encoders/devices are available), not this scan, and stays
        valid across a rescan exactly like calibration results would
        (docs/technical/components/review-ui.md).

        compress_skips (loaded separately, see _load_compress_skips())
        and dup_skips (_load_dup_skips()) also survive -- keyed by
        member paths, not ids, same as series_skips/placement_skips
        above.

        A compress queue that happens to be RUNNING at the moment of a
        rescan is deliberately left untouched: rescan only clears the
        CANDIDATE-LIST registries/computations above (compress_registry,
        compress_computations, compress_progress, applied_compress_
        group_ids), never state.compress_jobs/compress_queue_running/
        compress_cancel_event -- an in-flight encode has nothing to do
        with which files are still offered as candidates, and killing it
        out from under the user because they clicked "Rescan" in an
        unrelated tab would be a surprising, unrequested cancellation.
        It simply runs to completion (or is cancelled explicitly via
        POST /api/compress/cancel) on its own schedule.
```


### From code: `tidy/review/workers/rescan.py`
```python
        Echo of what the next series computation will actually use, read
        under the lock above rather than off `state` here -- this is what
        the client clears its "zmiana czeka na Rescan" marker from, so it
        must be the value that took effect, not one a later request could
        already have changed.
```


### From code: `tidy/review/skips.py`
```python
--- persisted "pomin ta grupe" for the series screen ----------------------

The decision is stored by MEMBER PATHS, never by group_id -- series.py's
skip_key()/group_is_skipped() own that reasoning (a group_id is a position
in one run's result list and means something different after every
rescan). This section is only the plumbing: load the table once, answer
"is this group skipped?" from memory, and write both places at once.
```


### From code: `tidy/review/skips.py`
```python
--- persisted "pomin ta grupe" for the placement screen -------------------

Mirrors the series-screen section above line for line -- same reasoning
throughout (identity by member paths via series.skip_key()/
series.group_is_skipped(), reused as-is rather than duplicated: a stable
group identity from a set of file paths is not a series-specific idea, and
a second implementation of the same subset rule would be a second place
for the two screens to drift apart). Only the registry/table/state fields
differ: placement_registry instead of series_registry, placement_skips
instead of series_skips, cache.*_placement_skip instead of cache.*_series_skip.
```


### From code: `tidy/review/skips.py`
```python
--- persisted "pomin ten plik" for the compress screen ---------------------

Exact mirror of the placement-screen section above (see its own comment):
identity by member paths via series.skip_key()/series.group_is_skipped(),
reused rather than duplicated. Only the registry/table/state fields
differ: compress_registry instead of placement_registry, compress_skips
instead of placement_skips, cache.*_compress_skip instead of
cache.*_placement_skip. A compress "group" happens to always be exactly
one file (build_compress_views()'s "group of one"), but nothing here
assumes that -- the same one-or-more-member logic works either way, and
the disk-sourced removable candidates (assigned their own fresh group_ids
by _compress_removable_candidates()) reuse this registry too.
```


### From code: `tidy/review/skips.py`
```python
--- persisted "skip this group" for the exact-duplicates screen ----------

Same mechanism as the series section above: identity by member paths via
series.skip_key()/series.group_is_skipped(), reused rather than
duplicated. Only the registry/table/state fields differ: file_registry
instead of series_registry, dup_skips instead of series_skips,
cache.*_dup_skip(s) instead of cache.*_series_skip. Unlike the lazy tabs,
the skipped flag is computed per GET /api/state (_skipped_dup_group_ids())
from the paths in file_registry, so a rescan that renumbers the groups
still hides the same ones. POST /api/dup-skip resolves group_id through
file_registry only -- an id from another tab is "unknown".
```


### From code: `tidy/review/state.py`
```python
--- placement screen -----------------------------------------------
placement_registry is the FOURTH registry: populated only by
build_placement_views(), read by _validate_placement_selections()
(POST /api/apply-placement) and handle_open(). It is never merged
into file_registry, series_registry or events_registry, so no id can
cross from one screen's apply endpoint to another's.
```


### From code: `tidy/review/state.py`
```python
--- compress screen ("Sugestia kompresji") ---------------------------
compress_registry is the FIFTH registry: populated only by
build_compress_views(), read by a later apply endpoint (not built by
this step) and never merged into file_registry, series_registry,
events_registry or placement_registry -- so no id can cross from one
screen's apply endpoint to another's (module docstring's table, now a
fifth row).
```


### From code: `tidy/review/state.py`
```python
--- compress queue (POST /api/apply-compress) --------------------
The CURRENT/last queue run only -- replaced wholesale by every fresh
POST /api/apply-compress, never accumulated across runs. A queue is a
mutable, restartable, cancellable, appendable sequence of jobs, unlike
the "compute once, cache forever" shape _LazyComputation models, so it
is plain state guarded by its own lock instead.
```


### From code: `tidy/review/state.py`
```python
--- "Cofnij" tab / POST /api/restore --------------------------------
batch_registry is a FIFTH id->path allowlist, the same rule every
other registry in this module follows (module docstring): the client
only ever sends a batch_id, never a manifest path -- see
_register_batch(). Guarded by placement_lock alongside
batch_list_cache/dir_index_cache, not state.lock: GET /api/batches is
read-only (like GET /api/state) and must never block behind a
long-running apply that holds state.lock for its whole duration.
```


### From code: `tidy/review/views/placement.py`
```python
--- the placement screen ("Sugestie grupowania") ----------------------------

This screen shows one source directory (default `!NEW`) measured against
the REST of the archive -- "can any of this move into a folder I already
have?". The engine is inbox.py; events.py stays underneath it, called per
folder.
```
