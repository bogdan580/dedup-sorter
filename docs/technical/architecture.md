---
tags: [dedup, technical]
---

# Architecture

The engine modules form the `tidy/` package at the repository root. `dedup.py` is
a ten-line entry point; all command-line logic resides in `tidy/cli/`. Tests add
the repository root to the search path (`tests/conftest.py`), so nothing needs
to be installed.

## Organizing Principle

The entire engine is **clean and OS-agnostic**. Disk access occurs exclusively
through an injected object satisfying the filesystem contract — described in
[filesystem-seam.md](architecture.md). The only module aware of Windows
is the platform layer.

From this follows the dependency direction: lower-level modules know nothing
about higher-level modules.

## Package Layout

```
dedup.py                     entry point
tidy/
  config.py  progress.py  paths.py
  fs/           fsmodel, platform_win
  duplicates/   scanner, hashing, tiers, keeper, report
  store/        cache
  moves/        manifest, batches, applier/ (package)
  media/        imaging, dhash, sharpness, timeline, gps, thumbnails
  analysis/     series, events, dateindex, inbox
  compression/  probe, compress, hwdetect, encoder
  cli/          args, common, main, run_*
  review/       review interface (security, lazy, validation, skips,
                computations, state, server, page, views/, workers/,
                handler/, static/)
tests/          one file per module; tests/review/ and tests/cli/
```

### Import Rule

Engine modules are imported **as module objects** and called through them:
`from tidy.analysis import series`, then `series.find_series(...)`. Not
`from tidy.analysis.series import find_series`. Tests swap engine functions via
`monkeypatch` on the module object; importing a name directly would capture the
original and the patch would not take effect.

### Locations Derived from `__file__`

Only `tidy/paths.py` derives locations from `__file__` (`DEDUP_DIR`,
`ARCHIVE_ROOT`; `abspath` deliberately). These are used by: finding bundled
ffmpeg in `hwdetect` and the archive root in the CLI.

## Layers

**Core / Foundation**

| Module | Role |
|---|---|
| `tidy/config.py` | constants: thresholds, directory names, extension sets. No I/O |
| `tidy/paths.py` | the only place deriving locations from `__file__` |
| `tidy/fs/fsmodel.py` | filesystem contract plus immutable structures describing a file |
| `tidy/fs/platform_win.py` | the only real contract implementation: long paths, reparse points, hard links, cloud files |

**Duplicate Engine**

| Module | Role |
|---|---|
| `tidy/duplicates/scanner.py` | recursive tree walk, exclusions, refusal to enter trash, hard link collapsing, file classification |
| `tidy/duplicates/hashing.py` | BLAKE2b hashes computed on an open stream, never on a path |
| `tidy/duplicates/tiers.py` | three-tier duplicate detection — [duplicate-detection.md](components/duplicates-mechanism.md) |
| `tidy/store/cache.py` | persistent results cache — [persistence.md](components/manifest-and-db.md) |
| `tidy/duplicates/keeper.py` | choosing the copy to retain according to [domain rules](../business/rules/version-selection.md) |
| `tidy/duplicates/report.py` | writing `raport.csv` and formatting console summaries |
| `tidy/moves/manifest.py` | reading and writing the manifest — [manifest.md](components/manifest-and-db.md) |
| `tidy/moves/applier/` | package — the only one executing file moves; `__init__` is the public API, while `remove`, `move`, `compress`, `restore`, `verify`, and `_paths` split the work |

**Image Primitives** (`tidy/media/`)

`imaging.py` (shared decoder availability flag), `dhash.py` (perceptual hash),
`sharpness.py` (sharpness), `timeline.py` (photo creation time), `gps.py`
(coordinates). Each operates on an already opened image or on a stream — the
file is decoded **once**, and the result is reused by all of them.

**Higher-Order Engines**

| Module | Role |
|---|---|
| `tidy/analysis/series.py` | time clusters, similarity grouping, keeper suggestion |
| `tidy/analysis/events.py` | splitting into events by time and location, place naming |
| `tidy/analysis/dateindex.py` | which existing directories contain photos from given days |
| `tidy/analysis/inbox.py` | orchestrates dedup, series, and events for inbox; classifies folders |
| `tidy/moves/batches.py` | which batches exist on disk and how many can be undone |
| `tidy/media/thumbnails.py` | thumbnails, including video frames |
| `tidy/progress.py` | progress counter shared across worker threads |

**Video Compression** (`tidy/compression/`)

| Module | Role |
|---|---|
| `probe.py` | video stream parameters with cache |
| `compress.py` | qualification heuristics and estimation model — pure logic |
| `hwdetect.py` | ffmpeg binary resolution and detection of working encoders |
| `encoder.py` | running ffmpeg into working directory and verifying the result |

The whole system is described in [compression.md](components/compression-mechanism.md). `encoder.py` is
the only module outside the platform layer that writes to the real disk —
strictly under `.dedup`, because ffmpeg is an external process and cannot be
hooked into the seam. The result reaches the archive normally, through the
`tidy/moves/applier` package.

Higher-order engines **do not duplicate** the duplicate engine — they call it.

**Entry Points**

| Module | Role |
|---|---|
| `dedup.py` | entry point — calls `tidy.cli.main.main()` |
| `tidy/cli/` | command line and orchestration without domain logic: `args` (parser), `common` (shared scan and selection), `main` (mode dispatcher), `run_dedup`, `run_restore`, `run_series`, `run_events`, `run_inbox`, `run_review`, `run_compress` |
| `tidy/review/` | review server with browser application — [review-ui.md](components/review-ui.md) |

## Flow

```
tree scan  ->  duplicate detection  ->  keeper selection  ->  decisions
                      |                                            |
                cache database                                CSV report
                                                                   |
                                              application -> manifest -> move
```

The first three steps are common to all modes. Image-based modes hook in after
the scan, on a filtered pool of candidates — this ensures the CLI and the review
interface see the same set.

File movement occurs exclusively in the final step and only upon explicit user
command.

## Degradation Without Optional Dependencies

The absence of a library disables the **feature**, not the tool. Import guards
reside in the modules that need them. Summary:
[dependencies.md](dependencies.md).


---

---
tags: [dedup, technical]
---

# Filesystem Seam

The most critical structural rule of this project.

`tidy/fs/fsmodel.py` defines a contract (`Protocol`) describing the filesystem as
well as immutable `StatInfo` and `DirEntryInfo` structures. **Every module
outside the platform layer touches the disk exclusively through an injected
object satisfying this contract** — never via `os`, `os.path`, `pathlib`, or
`shutil` when it comes to filesystem *state*.

Thanks to this, the entire engine can be tested in memory, without touching the
real archive — see [../development/testing.md](development/conventions-and-tests.md).

## Who Can Do What

| Module | Permission |
|---|---|
| `tidy/fs/platform_win.py` | **only** module with real system calls |
| `tidy/moves/applier/` (package) | **only** caller of `move()` |
| others | read-only access through injected contract |

Four exceptions write to disk using standard `open()`, because they maintain the
tool's **own bookkeeping** rather than archive content:
`tidy/moves/manifest.py` (manifest), `tidy/store/cache.py` (cache database),
`tidy/media/thumbnails.py` (thumbnails), and `tidy/compression/encoder.py`
(encoding staging — [compression.md](components/compression-mechanism.md)). All of them
write strictly to directories inside `.dedup`.

## The Contract

Read methods: `scandir()`, `stat()`, `open_read()`.
Write methods: `move()`, `exists()`, `makedirs()`, `touch()`.

Two requirements beyond signatures:

> [!important]
> **`move()` must raise `FileExistsError` when the destination exists.** It must
> never overwrite a file. This implements Guarantee #4
> from [../business/guarantees.md](../business/guarantees.md);
> the implementation must select a system primitive that behaves this way.

**An error on a single entry must not abort directory traversal.** A locked
file, a transient I/O error, an antivirus lock, or a race with a cloud client
are reported as an entry without metadata and with an error description,
instead of an exception terminating the iteration. The scan records such an
occurrence and continues.

## Windows Pitfalls

**Long path prefix.** Production paths use the `\\?\` prefix, which removes
the 260-character limit. Test paths are regular strings with slashes. All path
comparisons must account for this — a filter written without this in mind will
simply match nothing.

**Do not use `os.path.join` or `pathlib` on archive paths** in engine modules.
Path operations are performed on strings so that they behave identically in
both worlds.

> [!caution]
> **The mock does not catch everything.** Real differences it lets slip
> through: `"D:"` vs `"D:\\"`, regular slashes under a long path prefix, actual
> length limits. Code touching these areas requires verification on a real disk,
> not just a unit test.

## Special File Handling

The contract carries in metadata the information needed for decisions **without**
additional disk queries:

- **Reparse point** (junction, symlink) — the scan does not descend into it,
  eliminating the risk of loops in the tree.
- **Cloud file** — attributes indicating content not present locally. Such a file
  is never opened, as reading would trigger a download.
- **Hard link** — volume and file identifiers allow recognizing that multiple
  paths refer to a single file.

## Test Coverage

Pure platform layer helper functions (path transformations, attribute decoding)
are unit tested. The disk access class itself is **inherently integration-focused**
— requiring a real volume with junctions, hard links, and cloud files — so it
is exempt from the coverage threshold and verified manually.


### From code: `tidy/review/server.py`
```python
    The "pomin ta grupe" decisions taken in
    earlier runs, read before the first request can be served so the very
    first GET /api/series already knows what to hide. No-op without a
    db_path -- see _load_series_skips().
```


### From code: `tidy/review/server.py`
```python
        Polled join, not thread.join() -- on Windows that call could not be
        interrupted, so Ctrl+C did nothing at all and the console window had
        to be closed by hand.

        Why: Thread.join() with no timeout is a lock acquire with no
        timeout, and CPython's thread_nt.h says outright that those are
        uninterruptible on Windows. The console's Ctrl+C only sets a flag;
        it becomes a KeyboardInterrupt when the MAIN thread next runs
        bytecode -- and this one was parked in that acquire until the
        serve_forever() thread finished, which is never. Measured: the
        interrupt landed after 20s (when the worker happened to end) with a
        bare join(), and after 1s with the loop below.

        Waking up twice a second costs nothing (the work happens on the
        server thread) and is the whole fix: each return from join() is a
        chance for the interpreter to raise.
```


### From code: `tidy/review/server.py`
```python
        Printed so the user can see Ctrl+C was heard -- shutdown() below
        waits for serve_forever()'s poll cycle, and a silent pause is
        exactly what "it does not stop" felt like.
```


### From code: `tidy/review/lazy.py`
```python
--- lazy background computation for series/events ------------------------

GET /api/series and GET /api/events can take minutes on a real archive
(dHash + pairwise Hamming comparisons, offline geocoding),
so they must never run inline in a request-handling thread the way
GET /api/state does. _LazyComputation is the small state machine that
lets the FIRST request for a given resource kick off exactly one
background thread while every request (including that first one)
returns immediately with whatever the current status is.
```
