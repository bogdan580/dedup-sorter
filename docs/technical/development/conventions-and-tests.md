---
tags: [dedup, development]
---

# Code Conventions

## ASCII Characters in Console Output

> [!warning]
> **Everything the tool prints to the console is written in English using ASCII characters.**
> Examples: `Apply`, `skip this group`, `Moved`.

This is not sloppy. The Windows console operates in a codepage that does not
include many characters (Polish diacritics, Cyrillic), and a single non-ASCII
character in a summary line crashes the process **before** changes can be
applied. Symptoms and workarounds:
[limitations.md](../../business/instructions/limitations.md).

**The rule applies to what is printed, not the whole file.** Non-ASCII
characters are justified and occasionally necessary in code:

- **directory names in data** — the default list of priority directories
  contains Cyrillic names, because that is how directories are named on disk;
  silently "correcting" them would break matching;
- **review interface messages** — localized (English, Polish, Ukrainian) in
  `tidy/review/locales.py` and served as UTF-8 to the browser, where console
  limitations do not apply;
- **comments** quoting example paths.

## Languages

| Location | Language |
|---|---|
| Comments and docstrings in code | English |
| Console output (stdout/stderr) | English, ASCII |
| Review UI | localized: English (default), Polish, Ukrainian (`tidy/review/locales.py`) |
| `docs/`, `README.md` | English (`README.pl.md` and `README.uk.md` are translations of the README) |
| Commit messages | English |

Commit messages describe what changed, not which stage was completed.

## Naming

The [glossary](../../glossary.md) applies. Technical names appearing in data
(`dup-exact`, `coherent`, `planned`) are quoted verbatim and left untranslated.

**Phase numbers do not appear.** A "Phase N" label describes when something was
created, not what it is. Capability names apply — the transition table is in
the glossary.

## Code

- Engine code lives in the `tidy/` package; `dedup.py` is merely an entry point.
  Engine modules are imported as module objects and invoked through them
  (`from tidy.analysis import series`, `series.find_series`), because tests mock
  them. Only `tidy/paths.py` derives locations from `__file__` —
  [architecture.md](../architecture.md).
- Data structures are **immutable** wherever possible.
- Disk access is strictly via injected contract — the rule and its consequences:
  [architecture.md](../architecture.md).
- Archive paths are manipulated as strings, never via `os.path` or `pathlib`.
- An error in an individual file must not halt processing of the rest. This
  applies to scanning, hashing, thumbnail generation, video encoding, and move
  execution.

## Docstrings After Documentation Migration

Code retains the **contract**, not prose. Guidelines, good and bad examples,
and anchor formats to documentation: [documentation.md](documentation.md).


---

---
tags: [dedup, development]
---

# Tests

```
python -m pip install -r requirements-dev.txt
python -m pytest -q                              # all
python -m pytest tests/test_series.py -q          # single file
python -m pytest tests/test_series.py::TestSkipIdentity -q
python -m pytest -q -k "placement and skip"
```

A full run takes approximately five minutes — a significant portion of this time
is spent on real video encodings.

## Overriding Principle: Tests Do Not Touch Archive

> [!important]
> **No test scans the real archive.** All file trees are constructed in memory
> using a mock conforming to the filesystem contract. This is a direct
> consequence of the seam described in
> [architecture.md](../architecture.md) — and the primary
> reason this seam exists.

The mock builds the tree through straightforward calls adding directories and
files, with paths as ordinary forward-slash delimited strings.

## When the Mock Is Not Enough

Three situations require the real disk:

**Manifest writing.** Manifests are written via standard file opening, outside
the seam, so any test touching this path requires a real directory. A fixture
creating and cleaning up a directory under `.dedup\.tests` serves this purpose.

> [!danger] Never pass paths directly.
> A literal such as `"D:/Archive"` points to a real directory at drive root and the test will actually write there.

**Windows-specific behaviors.** Long paths, junctions, hard links, cloud files
— cannot be replicated in memory. The platform layer is verified manually in
this regard, while its pure helper functions are covered by standard tests.

**Real video encoding.** The encoder cannot be faked: command construction
tests are pure, but actual encoding and result verification execute the bundled
ffmpeg binary against self-generated clips — one-second clips at low resolution
to keep the suite fast enough to run on every change. These tests skip when the
binary cannot be resolved (ffmpeg is downloaded on first launcher run, not stored
in git, so a fresh clone without it skips them); the rule "missing dependency
degrades the feature, not the tool" applies here as well.

**Real image bytes.** Decoding tests use images generated by a graphics library
rather than mocks — otherwise they would test code against their own
simplifications. This trap has sprung before: test fixtures previously stored
timestamps in a different EXIF structure location than real cameras do, causing
tests to validate code consistent with the same mistake.

## Safety Net

`tests/test_dry_run_guarantee.py` scans the **source text** of engine modules
searching for deletion and move calls. The presence of any such call fails the
test.

The platform layer is exempt from this check — it by definition contains the
real calls. Every other module must reach the disk strictly through the injected
contract.

The test reads sources as text, so mass comment edits can trigger it. It should
be run after modifications touching multiple modules simultaneously.

`tests/test_docs_anchors.py` functions identically for documentation: `docs/...`
anchors in code must point to existing files, and obsolete planning terminology
must not return. Scope: [documentation.md](documentation.md).

Compression contract tests form an additional safety net. They enforce the
rule whose violation represents the only scenario of actual data loss: **the
original is not sent to trash until the result is confirmed**. Failed encoding,
failed verification, and output larger than the original are tested separately
— three distinct code paths, one contract. A fourth test ensures that a `source`
parameter traversing outside the archive returns `400` without triggering any
computations. Rule:
[compression.md](../../business/rules/compression.md).

| Contract | File |
|---|---|
| failed encoding, failed verification, and result larger than original do not move original (via queue) | `tests/review/test_compress_queue.py` |
| result larger than or equal to original does not move original (applier itself) | `tests/test_applier_compress.py` |
| `source` with directory traversal: `400`, no computation | `tests/review/test_compress_views_validation.py` |

## Conventions

- One test file per module, classes grouping test cases, `test_*` methods.
- Review server tests (`tests/review/`) operate on **real network sockets**, not
  mocks — covering security, validation, and full multi-group paginated sessions.
- Browser behavior is verified against the **served script source**. There is no
  browser or JavaScript engine in this repository, so tests verify script content
  rather than execution.

## Layout of `tests/` Directory

Engine module tests sit flat in `tests/` (`test_scanner.py`, `test_series.py`,
…). Two areas have dedicated subdirectories:

- `tests/review/` — review UI (17 files), including `conftest.py` with fixtures
  and `helpers.py`;
- `tests/cli/` — command line interface (8 files, `test_run_*.py` and `test_args.py`).

**`conftest.py` vs `helpers.py`.** Fixtures (running server on a real socket,
mock filesystem worlds) belong in `conftest.py`; regular helper functions and
constants belong in `helpers.py` and are imported directly (`from .helpers import ...`).

**Why `__init__.py`.** Both subdirectories contain them because filenames repeat
across directories (for example `tests/review/test_series.py` and
`tests/test_series.py`), and pytest in `prepend` mode without packages would
refuse to collect tests sharing a basename. For the same reason, helper imports
are relative.

## Suite Status

The complete suite (1,476 tests) passes with no known failures. Up to 2 tests
are skipped (hardware encoding tests when that encoder is absent on this machine).
Every failure is therefore a regression.

## Verification Outside Test Suite

A list of scenarios to verify against real data, along with execution status,
resides in [backlog.md](backlog.md). The test suite alone is
insufficient to consider a change complete if it touches behaviors that the mock
does not replicate.
