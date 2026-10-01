# AGENTS.md

This file provides guidance to AI agents (like Claude Code, Cursor, Aider, Gemini, etc.) when working with code in this repository.

## What this is

A Windows-only Python tool that tidies a large photo/video archive: exact duplicate
detection, visually-similar series, event/trip grouping, `!NEW` inbox triage, and
video recompression.
The repo lives in `.dedup/` **inside** the archive it operates on — the archive root
is always `.dedup`'s parent directory, derived from `tidy/paths.py`'s own location.
No packaging, no `setup.py`: the code is the package `tidy/` at the repo root,
`dedup.py` is a 10-line entry point (`tidy.cli.main.main()`), and
`tests/conftest.py` puts the repo root on `sys.path`.

**`docs/` is the single source of truth**, in English. Start at
[`docs/index.md`](docs/index.md), which routes to every other document. When a
decision looks arbitrary, the reason is written down there — read it before
changing behaviour. `README.md` is a ~60-line entry page that points into `docs/`.

Code carries contracts, not prose: a module comment may anchor to a document
(`# Rules: docs/business/rules/keeper.md`) but must not restate it. The rule
deciding what goes where is
[`docs/technical/development/documentation.md`](docs/technical/development/documentation.md).

There is no `PLAN.md` and no "Faza N" vocabulary — both were retired and git holds
the history. Capability names replace the phase numbers: series, events, `!NEW`
inbox, grouping suggestions, review UI, video compression.

## Commands

```
python -m pip install -r requirements-dev.txt   # runtime deps + pytest
python -m pytest -q                             # full suite (~6 min, ~1480 tests)
python -m pytest tests/test_series.py -q         # one file
python -m pytest tests/review -q                 # review UI tests (also tests/cli)
python -m pytest tests/test_series.py::TestSkipIdentity -q      # one class
python -m pytest -q -k "placement and skip"      # by name
```

`requirements.txt` is deliberately runtime-only; `pytest` lives only in
`requirements-dev.txt`.

Running the tool (from the archive root, not from `.dedup`, because relative
`--roots` resolve against the process CWD):

```
python dedup.py                    # dry-run scan of the whole archive
python dedup.py --review           # local review UI on 127.0.0.1
python dedup.py --series | --events | --inbox      # plan-only analysis modes
python dedup.py --compress                        # list recompression candidates
python dedup.py --compress --apply                # encode, verify, land results
python dedup.py --apply            # move discarded duplicates to !REMOVE
python dedup.py --restore "!REMOVE\<batch>\manifest.csv"
```

`run.cmd` is the double-click launcher for the review UI (fixed port 8765).

Baseline: **1476 passed, 0 failed** (~6 min; the compression tests run real ffmpeg
encodes on short generated clips, including hardware encoders when this machine
actually has working ones -- see docs/technical/components/compression-mechanism.md; on a machine without
them, up to 2 hardware-encoder tests skip instead; on a fresh clone run `run.cmd` or
`tools/fetch-ffmpeg.ps1` once first, otherwise the real-ffmpeg tests skip). Any failure is a regression.

## Architecture

### The FileSystem seam (the single most important structural rule)

`tidy/fs/fsmodel.py` defines a `FileSystem` `Protocol` plus frozen `StatInfo`/`DirEntryInfo`
dataclasses. **Every module except `tidy/fs/platform_win.py` reaches the disk only through
an injected `fs` object** — never `os`, `os.path`, `pathlib`, or `shutil` for
filesystem *state*. That is what lets the entire engine be unit-tested in-memory
against `tests/fakes.py::FakeFileSystem` without ever touching the real archive.

Consequences you must respect when adding code:

- Paths are plain strings joined with `/` in tests and carry the Windows extended
  `\\?\` long-path prefix in production. Never use `os.path.join`/`Path` on archive
  paths in engine modules; compare paths prefix-aware.
- `tidy/fs/platform_win.py` is the **only** OS-specific module and the only place with real
  `os.scandir`/`os.stat`/`os.rename`. Its `WindowsFileSystem` class is exempt from
  unit-test coverage (integration-only) — the pure helpers like `to_extended()` are
  not.
- The `tidy/moves/applier/` package is the **only** code allowed to call `fs.move()`.
- Deliberate exceptions that write real disk with plain `open()`, because they are
  tool-internal bookkeeping under `.dedup/` and never archive content:
  `tidy/moves/manifest.py`, `tidy/store/cache.py` (`.db/cache.sqlite3`),
  `tidy/media/thumbnails.py` (`.thumbs/`), `tidy/compression/encoder.py`
  (`.compress/` staging -- ffmpeg is an external process and cannot be handed a
  `FakeFileSystem` stream; the archive move still goes through the applier package).

`tests/test_dry_run_guarantee.py` statically greps the engine modules for
destructive call substrings (`os.remove(`, `.rename(`, `shutil.move(`, …) and fails
if any appears. Adding a raw filesystem call to a listed module breaks that test by
design.

### Layering

```
tidy/config.py            constants only, no I/O
tidy/paths.py             the only __file__-derived locations (DEDUP_DIR, ARCHIVE_ROOT)
tidy/fs/fsmodel.py        FileSystem Protocol + StatInfo/DirEntryInfo
tidy/fs/platform_win.py   the one real implementation (Win32/ctypes, long paths,
                          junctions, hard links, cloud-placeholder detection)

tidy/duplicates/scanner.py  recursive walk; exclusions, !REMOVE refusal, hard-link
                          collapse, inventory-only classification
tidy/duplicates/hashing.py  BLAKE2b over an already-open BinaryIO (never over a path)
tidy/duplicates/tiers.py  3-tier dedup: size -> partial hash (first+last 64 KB) -> full hash
tidy/store/cache.py       SQLite path+size+mtime -> hash; also series_skips / placement_skips
tidy/duplicates/keeper.py which copy survives (config.json rules; rule 0 = !NEW always loses)
tidy/duplicates/report.py raport.csv (UTF-8 BOM, ';' delimiter) + console summaries
tidy/moves/manifest.py    manifest.csv: append-only event log, the sole vehicle of undo
tidy/moves/applier/       --apply / --restore / --verify-manifest; op=remove, op=move,
                          op=compress (remove, move, compress, restore, verify, _paths;
                          __init__ = public API)

tidy/media/               imaging dhash sharpness timeline gps: per-file image primitives
tidy/analysis/series.py   time clusters -> dHash/Hamming groups -> suggested keeper
tidy/analysis/events.py   time-gap split -> GPS-jump split -> offline place name
tidy/analysis/dateindex.py which existing folders already hold photos from these days
tidy/analysis/inbox.py    wires dedup + series + placement over !NEW
tidy/moves/batches.py     which !MOVES/!REMOVE batches exist and what is still restorable
tidy/media/thumbnails.py  .thumbs cache; Pillow + pillow-heif + imageio-ffmpeg for video
tidy/progress.py          thread-safe (done, total) counter for --workers parallel decode

tidy/compression/probe.py    video stream parameters via bundled ffprobe + compress_probes cache
tidy/compression/compress.py compression candidacy heuristic, size/time model, config section
tidy/compression/hwdetect.py ffmpeg binary resolution (config.json -> bin/ffmpeg -> PATH),
                          encoder probing by real init, machine id for calibration
tidy/compression/encoder.py  ffmpeg argv, encode into .dedup/.compress staging, verify result
bin/ffmpeg/               pinned static GPL build; only VERSION.txt + LICENSE are in git,
                          the .exe files are fetched and SHA-verified by tools/fetch-ffmpeg.ps1
                          (called by run.cmd / run.ps1 / run.sh)
                          Rules: docs/technical/components/compression-mechanism.md

dedup.py                  10-line entry point -> tidy.cli.main.main()
tidy/cli/                 thin CLI + orchestration only: args, common, main, run_dedup,
                          run_restore, run_series, run_events, run_inbox, run_review,
                          run_compress
tidy/review/              the review UI package (see below): stdlib http.server + SPA
```

Import rule: engine modules are imported as module objects and called through them
(`from tidy.analysis import series`, then `series.find_series(...)`), because tests
monkeypatch them; never `from ... import func`. `tidy/paths.py` is the only place
that derives a location from `__file__`.

Optional dependencies degrade a *feature*, never the tool: missing `pillow-heif`
hides `.HEIC` from series/events; missing `imageio-ffmpeg` gives video a
placeholder thumbnail; missing `reverse_geocoder` yields events without place names.
Import guards live in the modules that need them (`tidy/media/imaging.py`, `tidy/analysis/events.py`).

### Safety invariants (non-negotiable — these are the product)

1. **Nothing is ever deleted.** "Delete" means move into `!REMOVE\<date_time>\` with
   a `manifest.csv`. Physical deletion is always the user's manual act.
2. **Default is dry-run.** Only `--apply`/`--restore` and the review UI's explicit
   apply buttons move anything.
3. **Every move is reversible** via `--restore <manifest>`. Manifest rows are
   written with `status=planned` *before* the move and appended again as
   `done`/`failed` after — an append-only log, so a crash can only corrupt the last
   partial line. `--verify-manifest` diagnoses an interrupted batch.
4. **Moves never overwrite.** `fs.move()` must raise `FileExistsError` rather than
   clobber; placement collisions get a `__dup2` suffix.
5. **`!REMOVE` is hard-excluded** from scanning in every mode, cannot be re-enabled
   by any flag, explicit `--roots "!REMOVE"` is *refused* with an error, and a
   `.dedup-kosz` sentinel file blocks entry even if the folder is renamed.
6. **Nothing leaves the machine.** The review server binds 127.0.0.1 only;
   reverse geocoding is offline (bundled GeoNames data). No network calls anywhere
   in `dedup.py`/`tidy/` -- the one exception is the launchers' ffmpeg download
   (`tools/fetch-ffmpeg.ps1`), which never sends archive data.

### tidy/review/

Package map: `security` (Host/Origin/token checks, CSP), `lazy` (`_LazyComputation`),
`validation`, `skips`, `computations`, `state` (`ReviewState`), `server` (start/serve;
its module docstring is a ~40-line design record — read it before touching the
package), `views/` (view-model builders per tab), `workers/` (rescan, placement,
compress queue, calibration threads), `handler/` (HTTP handler + route table).

- **Page assembly.** `page.py` inlines `static/index.html`, `static/css/app.css` and
  12 `static/js/*.js` files into one document at import (`INDEX_HTML`); marker lines
  `/*@inline ...*/` / `//@inline ...` are replaced in `INLINE_FILES` order, an unknown
  marker or missing file raises. Edit the JS/CSS in `static/`, not in Python.
  `.gitattributes` forces LF there.
- **Route table.** `handler/routes.py` holds `GET_ROUTES`, `GET_PREFIX_ROUTES` and
  `POST_ROUTES`; the POST whitelist is the keys of `POST_ROUTES` (unlisted = 404).
  Route functions are plain `handle_*(handler, state, ...)` in `handler/routes_*.py`.

- `ReviewState` owns everything the request handlers mutate; heavy computations
  (series, events, placement) run lazily in background threads via
  `_LazyComputation` and are polled by the UI through progress counters. A
  `_LazyComputation` never recomputes once ready, so invalidating a view means
  replacing the object, not resetting it.
- **Five disjoint id registries** — `file_registry`, `series_registry`,
  `events_registry`, `placement_registry`, `compress_registry` — act as
  allowlists. Ids are namespaced (`1:0`, `s1:0`, `e1:0`, `p1:0`, `c1:0`) so
  resolution by trying each registry is unambiguous. A client-supplied *path* is never trusted; only ids are.
  `events_registry` is deliberately never handed to any apply endpoint.
- Every mutating `POST` goes through `_check_mutating_request()`: a token in a
  **header** (not the URL), plus `Host`/`Origin` validation to defeat DNS rebinding.
- Skips ("pomin ta grupe") are server-side and persisted in the cache DB, keyed by
  **member file paths**, never by group id — a group id is a position in one run's
  output and means something different after every rescan. A shrunk skipped group
  stays hidden; a grown one comes back. `series_skips`, `placement_skips` and
  `compress_skips` are separate tables on purpose.
- The compress tab adds a long-running background **queue** rather than an
  immediate apply: one job at a time, cancellable, each job encoding into
  `.dedup/.compress` and landing its own `!MOVES` batch as soon as it verifies.
  A job's original reaches `!REMOVE` only via that job's own confirmed
  `apply_compressions()` entry — never from the request list.
  Rules: docs/business/rules/compression.md, docs/technical/components/compression-mechanism.md
- Three operation kinds reach the applier package from here: `op=remove` (duplicates,
  series and discarded compression originals → `!REMOVE`), `op=compress`
  (verified encode → next to its original) and `op=move` (placement
  suggestions → deeper into the archive). Manifests of the latter two live in
  `!MOVES`.
- The two series thresholds (Hamming distance, time window) are session state on
  `ReviewState`, seeded from the CLI flags and re-tunable from the Serie tab. A
  changed value travels **only in the body of `POST /api/rescan`** — that is what
  makes "takes effect after Rescan" true rather than merely promised — and
  `_parse_series_params()` **refuses** an out-of-range value instead of clamping
  it. `_run_rescan()` assigns it inside the same `with state.lock:` that swaps the
  `_LazyComputation` objects; doing it in the caller afterwards would let a
  concurrent `GET /api/series` cache a result computed on the old threshold.
- `POST /api/rescan` rebuilds every registry, so it bumps `scan_generation` and
  clears the applied-id sets: ids are positional, and a surviving one would mark
  an unrelated group as already applied and hide it. `applied_batches` survives —
  it references manifests on disk, not ids.


### KEEP DOCUMENTATION IN SYNC (CRITICAL)

**Whenever you modify business logic, architecture, or configuration rules in the Python code, YOU MUST proactively update the corresponding Markdown files in the docs/ directory.**
- Do NOT leave long explanatory comments in the Python code explaining *why* a change was made or how a new rule works.
- Put the rationale and rules in the appropriate docs/business/ or docs/technical/ file.
- Leave only a one-line anchor in the code (e.g., # See: docs/business/rules/compression.md).
- This keeps docs/ as the single source of truth for the project.

## Conventions

- **All console output must be in English.** This universally solves the Windows `cp1250` encoding issue and makes the codebase standard for Open Source. Do not use Polish or Cyrillic strings in print statements or CLI arguments. All documentation (including `README.md` and the `docs/` folder) must also be written exclusively in English.
- Code comments and docstrings are English; commit messages should also be in English, naming the capability rather than a phase number.
- **Comments state contracts, not rationale.** What stays in code: a contract at the
  point where it is enforced (max two sentences), a non-obvious invariant about that
  module, a trap in exactly that line, and a one-line anchor into `docs/`. Everything
  else — the *why*, the alternatives, the measurements, the dates — belongs in
  `docs/`, once. Do not reintroduce narrative docstrings; do not delete a contract
  because "it is in the docs". The boundary, with worked examples, is
  [`docs/technical/development/documentation.md`](docs/technical/development/documentation.md).
- Tests are `class TestSomething` with `test_*` methods, one file per module, and
  build worlds with `FakeFileSystem`. **No test may touch the real archive.**
- Tests that exercise `tidy/moves/manifest.py`'s real (non-seam) disk write must use the
  `real_archive_root` fixture from `tests/conftest.py`, which creates and cleans up
  `.dedup/.tests/<test-name>/Archive`. Never pass a literal `"D:/Archive"` as
  `archive_root` — it resolves to a real folder at the drive root.
- Generated artefacts all live in dot-directories under `.dedup/` and are
  gitignored: `.reports/`, `.db/`, `.thumbs/`, `.tests/`. Any of them can be deleted
  safely; deleting `.db/` only costs a re-hash — except that it also discards the
  user's persisted skip decisions.
- `config.json` is user-editable and forgiving: a malformed field warns and falls
  back to its default, never aborts a scan.
