---
tags: [dedup, business, rules]
---

# Keeper Selection

The keeper is the copy that is kept. In an exact duplicate group, there is
exactly one — the rest are discarded. The choice is a **suggestion**: the review
UI allows designating a different file, and that manual selection overrides any rule below.

## Rule 0 — Overarching and Non-Configurable

> [!important] Rule 0
> **A copy from the `!NEW` inbox never wins over a copy in the archive.**
>
> Without this rule, scoring might consider `!NEW\Camera\IMG_1234.jpg` better than
> `#Travel\2023 Mountains\IMG_1234.jpg` — because the path is shorter. The consequence would be
> reverting previous organizing work: the tool would delete an already sorted
> file, leaving an unsorted one behind.
>
> This rule applies before all other criteria and cannot be disabled
> via `config.json`. Rejection for this reason is marked in the report
> as `rule-0`.

## Tie-Breaking Criteria

Applied after Rule 0, in this order, until the first tie-break:

1. **Directory from priority list** — a copy in a user-specified directory wins.
2. **Name without copy markers** — a file without `(1)`, `- kopia`, `- copy`, `_1` wins
   over one that has them.
3. **Shorter path.**
4. **Older timestamp.**
5. **Alphabetical order** — strictly to ensure repeatable results across runs.

File size does not feature here, because in an exact duplicate group all
files have identical size by definition. Size acts as a tie-breaker in
series instead — see [business/rules/events-and-series.md](events-and-series.md).

**When both copies reside in `!NEW`**, the keeper becomes the one in a named folder.
A named folder carries trip information, whereas an unnamed camera roll dump carries none.

## Configuration

The `config.json` file in the tool directory accepts:

| Field | Meaning |
|---|---|
| `priority_dirs` | directories whose copies win first (default: empty; `config.example.json` shows examples such as `#Travel`, `#Family`) |
| `copy_name_patterns` | filename substrings considered copy indicators |
| `report_top_n` | how many largest groups to show in the console summary |

**An invalid configuration file never interrupts a scan.** Invalid JSON or
wrongly typed fields fall back silently to default values with a console warning.
The rationale is practical: aborting a long scan due to a typo in an optional
file would be worse than proceeding with defaults.

## Discard Reason

Every discarded file is assigned a reason, visible in the report and manifest:

| Reason | When |
|---|---|
| `rule-0` | copy from `!NEW`, discarded unconditionally |
| `dup-exact` | byte duplicate discarded by the criteria chain |
| `dup-visual` | element of a series |
| `event-sort` | move as part of grouping suggestions — does not discard anything |

Meaning of values: [glossary.md](../../glossary.md).


---

---
tags: [dedup, business, rules]
---

# Grouping Suggestions

A suggestion answers one question: **what from the source directory's contents
can be moved into directories you already have?** The default source is the `!NEW`
inbox, but any archive directory can be specified — for instance, to break up an old,
poorly organized folder.

A suggestion is a proposal. The tool **does not guess** — it presents candidates
and leaves the choice to the user.

## Folder Classification

Each subfolder of the source directory receives one of four classifications:

| Classification | Recognition | Proposal |
|---|---|---|
| **Coherent** | all files constitute exactly one event | move **whole folder** in a single operation |
| **Mixed** | files span multiple events | **never whole folder** — a separate suggestion for each event |
| **Loose files** | files sitting directly in the source directory, without a subfolder | like mixed folder contents — one suggestion per event |
| **No media** | no files the tool can evaluate | report only |

Event partitioning is calculated according to the rules in
[business/rules/events-and-series.md](events-and-series.md).

## Dump Folders Are Never Moved as a Whole

> [!warning]
> Classification based on content alone is not enough. A `Camera` folder ends up
> "mixed" because it actually contains years of photos — but a dump from a single session
> would appear "coherent" and receive a proposal to move the whole folder under one name.
> And a name like `Baseus` describes no trip.
>
> Therefore, an explicit list of dump names exists: `Camera`, `Baseus`, `DCIM`,
> `Download`, `100ANDRO`, `Nowy folder`. A folder with such a name **never** receives
> a "whole folder" suggestion, regardless of how coherent its contents are.
> The list can be configured in `config.json` — adding another dump name does not require code changes.

## Where Proposed Names Come From

In order of priority:

**1. Source directory name.** `!NEW\Gdańsk` yields `2024 Gdańsk`, not a name
from the geocoder. The user who named the folder knows more than a geographical
database. Names from the dump list are ignored here — they carry no meaning.

**2. GPS location**, converted offline into a placename.

**3. Date**, when neither a folder name nor location is available.

## Matching Existing Directories

The tool displays a **candidate list** — existing subdirectories matching
by location or date range, showing the number of files already inside. The user selects
one or creates a new one.

Automatic selection was deliberately rejected. Existing folder names on disk are non-uniform
(`2023 Mountains`, `Lakes February 2024`, `26.03.2015 Photoshoot`), so a "place plus year" rule
would still miss cases — and a mistake means dumping photos into someone else's trip.

**Matching by days, not by name.** A destination directory often has nothing in common
with the source by name: `!NEW\Mountains 2021 trip` belongs in `#Home\2021 MOUNTAINS`,
while name-based matching might suggest `#Home\2022 Mountains + spa` — correct place,
wrong year and wrong trip. The calendar, however, matches: these photos were taken
on specific days, and the destination directory already contains photos from those exact days.

Scoring is calculated **per day and by file count**, never by directory date span.
A span would be corrupted by a single outlier photo dated years apart.

The color of the destination directory field indicates what will happen with what is in the field
**right now**: the directory exists or will be created upon applying.

## Execution

**Files move deeper into the archive, not to the trash.** The manifest lands in `!MOVES`,
and rollback works the same as with removal.

**Name collisions never overwrite** — the file receives a `__dup2` suffix. A collision
here indicates two **different** photos sharing the same camera-assigned filename:
byte-identical ones were removed earlier, as dedup runs before organization.
Overwriting would destroy one, and refusing would leave the file stranded in the inbox
for a reason the user cannot resolve.

**Whole folder is not a directory rename**, but moving each file individually.
A directory rename cannot merge into an **existing** destination directory —
and suggestions routinely target existing directories — nor can it be partially rolled back.

**Non-media files and empty directories are only reported.** Never moved, never deleted.

**Rollback restores the source directory.** Sorting a folder empties it, and
an emptied folder is exactly what a user cleans up manually right after — or what
vanishes automatically if a cloud sync client runs in the background. Without
recreating the directory, the batch would become permanently irreversible.

## Skipping Suggestions

Works just like skipping a series group and for the same reason: the decision is
persistent and recorded by file paths. Rule details:
[business/rules/events-and-series.md](events-and-series.md).
