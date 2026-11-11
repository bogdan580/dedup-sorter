# AGENTS.md

Guidance for AI agents (Claude Code, Cursor, Aider, Gemini, ...) working in this
repository.

## What this is

A Windows-only Python tool that tidies a large photo/video archive: exact
duplicates, visually-similar series, event/trip grouping, `!NEW` inbox triage,
video recompression, and a local review UI.
The repo lives in `.dedup/` **inside** the archive it operates on; the archive
root is `.dedup`'s parent, derived in `tidy/paths.py`. No packaging: the code is
the package `tidy/`, `dedup.py` is a 10-line entry point.

**`docs/` is the single source of truth.** Start at [`docs/index.md`](docs/index.md).
When a decision looks arbitrary, the reason is written there — read it before
changing behaviour. Before touching a component, read its document:

| Area | Read first |
|---|---|
| layering, module map, import guards | `docs/technical/architecture.md` |
| `tidy/review/` (registries, locks, routes, page assembly) | `docs/technical/components/review-ui.md` |
| compression / ffmpeg | `docs/technical/components/compression-mechanism.md` |
| manifests, cache DB, skips | `docs/technical/components/manifest-and-db.md` |
| tests and conventions | `docs/technical/development/conventions-and-tests.md` |
| what goes in code vs docs | `docs/technical/development/documentation.md` |

## Commands

```
python -m pip install -r requirements-dev.txt   # runtime deps + pytest
python -m pytest -q                             # full suite (~6 min)
python -m pytest tests/test_series.py -q         # one file
python -m pytest -q -k "placement and skip"      # by name
```

The full suite must pass with 0 failures. Up to 2 hardware-encoder tests may
skip on machines without them; real-ffmpeg tests skip until `run.cmd` or
`tools/fetch-ffmpeg.ps1` has been run once.

Run the tool from the archive root (relative `--roots` resolve against CWD):
`python dedup.py` (dry-run), `--review`, `--series`, `--events`, `--inbox`,
`--compress [--apply]`, `--apply`, `--restore "!REMOVE\<batch>\manifest.csv"`.
Full reference: `docs/business/instructions/CLI.md`.

## Structural rules (tests enforce these)

- **FileSystem seam.** Every module except `tidy/fs/platform_win.py` reaches the
  disk only through an injected `fs` object (`tidy/fs/fsmodel.py` Protocol) —
  never `os`, `os.path`, `pathlib` or `shutil` for filesystem state. Tests use
  `tests/fakes.py::FakeFileSystem`.
- Archive paths are plain strings (`/` in tests, `\\?\` prefix in production);
  never `os.path.join`/`Path` on them in engine modules.
- `tidy/moves/applier/` is the **only** caller of `fs.move()`.
- Plain `open()` on real disk is allowed only for tool bookkeeping under
  `.dedup/`: `moves/manifest.py`, `store/cache.py`, `media/thumbnails.py`,
  `compression/encoder.py`. `tests/test_dry_run_guarantee.py` greps for
  destructive calls and fails by design if one appears elsewhere.
- **Import modules, not functions:** `from tidy.analysis import series`, then
  `series.find_series(...)` — tests monkeypatch module attributes.
- Optional dependencies degrade a feature, never the tool.

## Safety invariants (non-negotiable — these are the product)

1. **Nothing is ever deleted.** "Delete" = move into `!REMOVE\<date_time>\` with
   a `manifest.csv`. Physical deletion is always the user's manual act.
2. **Default is dry-run.** Only `--apply`/`--restore` and explicit review-UI
   apply buttons move anything.
3. **Every move is reversible** via `--restore`. Manifest is an append-only log:
   `planned` before the move, `done`/`failed` after.
4. **Moves never overwrite** — `FileExistsError`, collisions get `__dup2`.
5. **`!REMOVE` is hard-excluded** from scanning in every mode; `--roots "!REMOVE"`
   is refused; a `.dedup-kosz` sentinel blocks it even if renamed.
6. **Nothing leaves the machine.** Review server binds 127.0.0.1; geocoding is
   offline; no network calls in `dedup.py`/`tidy/` (only the launchers' ffmpeg
   download, which sends no archive data).
7. **Review UI trusts ids, never client-supplied paths**; every mutating POST
   needs the header token plus Host/Origin checks. Details: `docs/technical/security.md`.

## After every change — keep these in sync (CRITICAL)

| You changed | Also update |
|---|---|
| business logic, architecture, config rules | the matching file in `docs/` (rationale goes there, code gets a one-line anchor like `# See: docs/business/rules/compression.md`) |
| `README.md` | **both** translations: `README.pl.md` and `README.uk.md` |
| any user-visible review-UI string | all three languages (en, pl, uk) in `tidy/review/locales.py` |
| a term with a technical name (`dup-exact`, `planned`, ...) | `docs/glossary.md` |
| CLI flags | `docs/business/instructions/CLI.md` |

## Conventions

- **Language:** code, comments, docstrings, console output, commit messages and
  `docs/` are English. The only localized surfaces are the README translations
  (`README.pl.md`, `README.uk.md`) and the review UI (`tidy/review/locales.py`).
  Console output must be ASCII (Windows `cp1250`).
- **Comments state contracts, not rationale:** a contract where it is enforced
  (max two sentences), a non-obvious invariant, a trap on that exact line, or a
  one-line anchor into `docs/`. No narrative docstrings.
- Commit messages name the capability (series, events, inbox, review UI, video
  compression), never a phase number. There is no `PLAN.md`.
- Tests: `class TestSomething` / `test_*`, one file per module, worlds built with
  `FakeFileSystem`. **No test may touch the real archive.**
- Tests exercising `manifest.py`'s real disk write use the `real_archive_root`
  fixture (`tests/conftest.py`); never pass a literal like `"D:/Archive"`.
- Review UI JS/CSS is edited in `tidy/review/static/`, not in Python.
- Generated dirs under `.dedup/` (`.reports/`, `.db/`, `.thumbs/`, `.tests/`)
  are gitignored and deletable; deleting `.db/` also discards the user's skips.
- `config.json` is forgiving: a malformed field warns and falls back to default.
