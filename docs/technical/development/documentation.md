---
tags: [dedup, development]
---

# Documentation Guidelines

This file determines **where** to record content and **what** not to record at
all. Apply it before writing a new paragraph — in code or in `docs/`.

## Two Overriding Rules

**1. One fact, one place.** If you are writing something that already exists
elsewhere — delete what you are writing and insert a link. Repetition is not
"just in case" redundancy: it is a second source of truth that will diverge from
the first.

**2. Current state, not the path to it.** Documentation describes how things are.
Change history lives in git and stays there.

> [!warning] Do Not Record
> - deployment and measurement dates ("verified on <date>", "requested on <date>"),
> - stage and phase numbers ("Phase 5B", "Step 2 of 2", "PLAN.md Sec.3b"),
> - comparisons with previous versions ("previously worked differently", "will eventually change"),
> - labor estimates and statuses like "done".

If a rule is in effect — describe it in the present tense. If it is no longer in
effect — delete it.

## Where Content Goes

| Directory | Content |
|---|---|
| `business/` | Domain rules: what constitutes a duplicate, which copy is kept, what splits an outing into two. Guarantees made to the user. |
| `technical/` | Architecture, contracts between modules, file formats, operating mechanics. |
| `business/instructions/` | Installation, flags, scenarios, known limitations. |
| `development/` | Tests, code conventions, this file, backlog. |

### Business / Technical Boundary Test

> **Will this content change if we rewrite the implementation without changing
> behavior?**
> No → `business/`. Yes → `technical/`.

| Topic | `business/` | `technical/` |
|---|---|---|
| Deletion | nothing vanishes from disk, everything can be undone | append-only CSV, UTF-8 BOM, `;` delimiter |
| Duplicates | duplicate means identical content; RAW and JPG are not duplicates | three tiers, BLAKE2b, 64 KB window, cache |
| Events | a sufficiently large gap starts a new outing | thread pool, progress counter |
| Privacy | nothing leaves the disk | listening on 127.0.0.1, token in header |

Describing the same thing from two perspectives **is not a duplicate**, as long
as one side answers "what is the rule" and the other "how do we enforce it". A
duplicate begins when the same answer is repeated.

Specify numerical threshold values exclusively in `business/` — in `technical/`,
reference them via links. Otherwise, changing the default gap between events
requires editing two files, and the second will always be forgotten.

## What Stays in Code

Code is not free of documentation — it is free of **prose**. The module retains
only what would compromise safety or force the reader to leave the file in the
middle of a difficult line if moved to `docs/`.

| Stays | Moves to `docs/` |
|---|---|
| Contract at enforcement point, **at most two sentences** | Design rationale, considered alternatives |
| Non-obvious invariant concerning this module | Description of how other modules work |
| Pitfall located exactly in this line | Narrative on flow through the system |
| Anchor: a single line with a path to the document | Anything with a date, phase number, or reference to `PLAN.md` |

The contract remains primarily where the signature does not express it: protocol
methods, functions with requirements beyond types, places where violating a rule
is silent rather than loud.

> [!success] Good
> ```python
> def move(self, src: str, dst: str) -> None:
>     """Must raise FileExistsError instead of overwriting an existing file.
>     See: docs/business/guarantees.md
>     """
> ```

The contract is at the exact spot where someone might break it. The rationale —
what harm overwriting causes — is in the document, once.

> [!failure] Bad
> ```python
> def move(self, src: str, dst: str) -> None:
>     """Phase 3 (PLAN.md Sec.3b) adds four writing methods, because
>     moving to !REMOVE requires a file move. We decided on
>     this last month after the first version overwrote files...
>     """
> ```

History, phase number, date, and external content in one place.

## Anchors

Code links to `docs/`. **`docs/` never links to code line numbers.**

A single direction means one place to update during refactoring. A link to
`applier.py:412` breaks with the first inserted line and goes unnoticed; a path
like `docs/technical/components/manifest-and-db.md` survives every refactor.

Anchor format — a single line, path relative to repository root:

```python
# See: docs/business/rules/version-selection.md
```

Referring to a module by name (`see tidy/moves/applier`) is permissible; to a
line number — is not.

## Language and Formatting

- **`docs/` is written in English.** Everything is English: documentation, code
  comments, console output and commit messages. The only localized surface is
  the review UI (`tidy/review/locales.py`: English, Polish, Ukrainian);
  `README.pl.md` and `README.uk.md` are translations of the README.
- **Strings printed to the console must be ASCII.** This rule applies to what
  lands on stdout/stderr, not the entire file — review UI strings and directory
  names in data may contain non-ASCII characters. Scope and rationale:
  [conventions-and-tests.md](conventions-and-tests.md).
- Technical names appearing in data (`dup-exact`, `coherent`, `planned`) are
  quoted literally and not translated. Their meaning is in
  [../glossary.md](../../glossary.md) — the sole place permitted to explain them.
- Lines up to 80 characters, `##` and `###` headers, tables instead of lists
  when comparing alternatives.

## Adding a New Document

1. Determine the directory using the boundary test on this page.
2. Check whether the content belongs in an existing file — create a new document
   when answering a **different question**, not when discussing a different
   module.
3. Add it to the navigation table in [../index.md](../../index.md). A document
   unreachable from `index.md` does not exist.
4. If extracting content from code — remove the original in the same commit and
   leave an anchor. Copying without removing creates the exact problem this
   documentation eliminates.

## Enforcement

Documentation divergence must be a test failure, not a matter of vigilance.
The static test `tests/test_docs_anchors.py` — modeled after
`tests/test_dry_run_guarantee.py`, which reads sources as text — verifies that:

- every `docs/**.md` path mentioned in code (`tidy/`: `.py`, `.js`, `.html`,
  `.css`, and `dedup.py`) points to an existing file,
- no engine module or `requirements*.txt` file contains the strings `PLAN.md`,
  `Faza `, `plan_compression`, `plan_gpu`, or (case-insensitively) `plan section`.

The second condition prevents regression to obsolete vocabulary: active
capability names replacing phase numbers are in [../glossary.md](../../glossary.md).

**The check covers engine modules, not the `tests/` directory** — for the exact
same reason `test_dry_run_guarantee.py` excludes itself: test files are sometimes
places where a forbidden string serves as **data**. Three assertions in
`tests/review/test_http_core.py` and `tests/review/test_ui_tabs_paths.py` verify
that phase names do **not** leak into the review UI. Applying the ban to them
would force removing a safety check just to satisfy the rule intended to
strengthen it.
