---
tags: [dedup, development]
---

# Backlog

Open items. Not history — what is pending.

## Verification on Real Disk

Code ready, real-volume run not yet performed. The reason this is a separate
category rather than "done": the filesystem mock does not replicate certain
Windows rules.

| Item | Notes |
|---|---|
| Undo and verification after interrupted application | the only path of retreat in the entire tool |
| Collision during undo | manually recreate a file at destination; undo must refuse this entry, execute the rest, and leave the manually recreated file untouched |
| Resuming a genuinely interrupted scan | |
| Full run across a large archive | to date, small test sets only |
| HEIC files on real data | verify conversion pairs alongside full run |
| Applying suggestions from browser | initial run on two insignificant files, immediately undo |
| Double-clicking dry-run launcher in Explorer | requires presence for confirmation prompt |

## Deliberately Deferred Features

No implementation plan — recorded to avoid recurring as "ideas".

**Calculating camera clock correction.** Today the tool only warns that a given
model has files with suspicious dates. Automatic calculation would require
correlation against a confirmed timeline of another camera, for instance via a
shared geotagged event.

**Consolidating three copies of file extension rule.** Three modules maintain
independent, slightly differing implementations of extension parsing. The
difference concerns dotfiles (starting with a period) and currently produces no
observable defect — no variant considers such files media.

**Investigating disappearing empty folders in inbox.** Inspect folder state
immediately before and immediately after triage. A background cloud sync client is a
suspect.

**Sorting within inbox** without moving into archive.

**Selecting compression profile based on source `bpp`.** Currently the profile
comes from configuration or UI selection. Automation — a more aggressive profile
for material with very high bit headroom — makes sense only once data from more
files is gathered.

**Automatic destination folder assignment.** Deliberately rejected — the tool
presents candidates and does not guess. Rationale in
[../business/rules/placement.md](../../business/rules/version-selection.md).

**Deleting emptied folders.** Prohibited by governing principle.

## Repository Fixes

**Missing dry-run launcher.** The file is referenced in documentation and has its
own tests, but does not exist in the repository — four tests fail as a result.

**Review launcher discrepancy.** Test expects a different thread count than what
the launcher actually passes. Needs a decision on which value is canonical,
aligning both sides.

**Flaky compression queue cancellation test.** The test verifying that
cancellation stops unstarted jobs unblocks the first job **after** cancellation
— racing between `done` and `cancelled` against the encoder's cancellation event
observer. Passes in isolation, occasionally fails under full test suite load.
Needs a decision on intended behavior, tightening one of the sides.

## Data Maintenance

Not code — user-side decisions and actions.

**Archive backup to external storage.** Prerequisite to deleting anything from
trash.

**Large mixed inbox dumps.** A mixed folder spanning multiple years must not be
moved as a whole. First dedup against the archive, then split into events.

**Sensitive files in the archive root.** Plain text files with credentials or
recovery codes do not belong in a photo archive. The tool neither opens nor
moves them; keep such data in a password manager.
