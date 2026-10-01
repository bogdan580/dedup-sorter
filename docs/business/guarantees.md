---
tags: [dedup, business]
---

# Safety Guarantees

> [!important] Overarching Principle
> Nothing is deleted without explicit user consent. Every destructive
> operation is a move to `!REMOVE` with a manifest and the ability to fully
> roll back. Permanent deletion is performed manually by the user.

This principle overrides every other rule in this documentation. If
any feature were to violate it, this principle wins, not the feature.

## Six Guarantees

**1. Nothing disappears from disk.** "Deletion" means moving to
`!REMOVE\<date_time>\`. Permanent deletion is performed manually by the user —
the tool never does this, in any mode, even after applying.

**2. By default, nothing moves.** Without `--apply` and without clicking "Apply"
in the review UI, the tool only reports. Analysis modes
(`--series`, `--events`, `--inbox`) do not move files even with `--apply` —
they generate a plan for review.

**3. Every move can be undone.** Removal (`op=remove`), sorting
within the archive (`op=move`), and inserting a compression result (`op=compress`)
record a manifest that `--restore` rolls back entirely. The manifest is the sole
medium of reversibility — without it, a batch cannot be undone. Format
details: [technical/components/manifest-and-db.md](../technical/components/manifest-and-db.md).

**4. No move overwrites an existing file.** A name collision results in the
suffix `__dup2`, never replacement. This also applies to compression: the result
is created **alongside** the original as a new file, and the original is neither modified
nor deleted — [business/rules/compression.md](rules/compression.md). When `--restore` encounters an occupied
location, it refuses **for that single entry**, logs it, and continues the rest of the batch —
a manually restored file remains untouched.

**5. The trash cannot be processed as a source.** See below.

**6. Nothing leaves the disk.** Photos, thumbnails, and GPS coordinates are
never uploaded. The review UI listens exclusively on `127.0.0.1`, fetches
no network assets, and geocoding runs offline. Mechanisms:
[technical/security.md](../technical/security.md).
The single network access in the project is outbound only and carries no data:
the launchers (`run.cmd`, `run.ps1`, `run.sh`) download the pinned ffmpeg build
from BtbN's GitHub releases when it is missing or fails its SHA-256 check.
`dedup.py` itself never goes online —
[Bundled ffmpeg Binary](../technical/components/compression-mechanism.md#bundled-ffmpeg-binary).

## Protection of `!REMOVE`

The trash resides **inside** the scanned archive, so without protection a subsequent
run would treat files moved there as duplicates — and in the worst case,
would select the trash copy as the keeper and discard the original from the archive. The block is
threefold and deliberately redundant:

1. `!REMOVE` is excluded from scanning in **all** modes,
   regardless of `--roots`, `--exclude`, and `config.json`. This cannot be
   disabled by any parameter.
2. Explicitly specifying `--roots "!REMOVE"` (or a subfolder inside it) results in a
   **refusal with an error message**, not a silent skip.
3. The marker file `!REMOVE\.dedup-kosz` blocks entry even if
   the trash directory were ever renamed.

The same protection covers `!MOVES`.

## Scope of Responsibility

The tool **never**:

- deletes a file from disk,
- deletes a directory — even an empty one; an empty folder is reported only,
- overwrites a file at the destination,
- modifies file contents: camera clock correction works in memory
  only and never touches EXIF data,
- executes suggestions without user designation.

The user is responsible for: emptying `!REMOVE`, cleaning up empty directories
after sorting, and making backups before either action.

## When Removal Requires Particular Caution

> [!warning]
> Three situations where a discarded file **does not** have a byte-for-byte identical copy
> elsewhere — the rest of the tool relies on the assumption that it does:
>
> - **Series.** A group of similar photos consists of several distinct shots, not copies. Therefore,
>   a series group must keep **at least one** file; attempting to discard
>   all of them is rejected before anything moves. Rules:
>   [business/rules/events-and-series.md](rules/events-and-series.md).
> - **Grouping suggestion.** Moves files deeper into the archive, not into the trash.
>   A name collision in the destination directory means two **different** photos with the same
>   camera filename — byte-identical ones have already been removed earlier, because
>   deduplication runs before organization.
> - **Video original after compression.** The compression result is not a copy of the original,
>   but a lower-quality version — transcoding cannot be reversed.
>   Therefore, the original moves to the trash only after a verified result, as a
>   **separate batch**, undone independently of the compression batch itself. Rules:
>   [business/rules/compression.md](rules/compression.md).
> - **Lack of archive backup.** Until the archive is backed up to an external
>   drive, `!REMOVE` is the only safety net. Do not delete its
>   contents before creating a backup — see
>   [business/instructions/limitations.md](instructions/limitations.md).
