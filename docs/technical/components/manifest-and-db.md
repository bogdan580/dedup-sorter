---
tags: [dedup, technical]
---

# Manifest Format

`manifest.csv` is the **sole** carrier of reversibility. Without it, moves
cannot be undone, so its format and write ordering are questions of safety, not
convenience.

CSV file, UTF-8 encoding with BOM, semicolon separator. Opens in spreadsheet
software without special adjustments.

## Columns

| Column | Content |
|---|---|
| `op` | `remove`, `move`, or `compress` |
| `src_abs` | absolute source path |
| `dst_rel` | destination path — meaning depends on `op`, see below |
| `size`, `mtime`, `hash` | file identity verification on undo |
| `group_id`, `keeper_abs` | group and kept copy; empty for `move` |
| `reason` | `dup-exact`, `dup-visual`, `rule-0`, `event-sort`, `event-remove`, `compress-remove-original`, or compression profile name |
| `ts`, `tool_version` | entry timestamp and tool version |
| `status` | `planned`, `done`, or `failed` |
| `error` | system error message on failed move |

Meaning of values: [../glossary.md](../../glossary.md).

Columns containing paths are protected against spreadsheet formula injection,
and during reading this protection is stripped so that undo receives a
byte-for-byte original path — rule and exception regarding `keeper_abs`:
[security.md](../security.md).

## Meaning of `dst_rel` Depends on Operation

| Operation | `dst_rel` |
|---|---|
| `remove` | **relative** to batch directory |
| `move` | **absolute** |
| `compress` | **absolute** — path to the `_compressed` file next to original |

Reason for asymmetry: undo receives only the manifest path and nothing else.
When moving deeper into the archive, no batch directory exists relative to
which the path could be defined — the only alternative base would be the
archive root, which the undo procedure does not know. Requiring the user to
provide it from memory would create an obstacle during an operation reached
precisely when something has already gone wrong.

An absolute destination does not sacrifice portability that wasn't already
lost — the source path is absolute from the start, so the manifest would not
survive a drive letter change anyway.

## The `compress` Row Describes a File It Did Not Move

The `op=compress` row is the only one where `src_abs` **is not** the moved file.
The file moved is the verified encoding result, previously sitting in the
staging directory under `.dedup`; the original is neither read nor touched in
this operation.

| Column | Content for `op=compress` |
|---|---|
| `src_abs`, `keeper_abs` | original — untouched, recorded so it is known what the result refers to |
| `dst_rel` | absolute path to the `_compressed` file |
| `size`, `hash` | size and hash of the **result**, not the original |
| `reason` | encoder used and quality profile |

A compression batch resides in `!MOVES`, like any batch that does not move
anything to trash.

**Undoing a compression batch moves the `_compressed` file to trash and nothing
more** — the original never left its place, so there is nothing to restore.
The principle "no undo deletes" holds unchanged.

### Removing Originals Is Always a Separate Batch

Compression with original removal checked produces **two** manifests, never a
single mixed one:

| Batch | Where | Operation | What `--restore` Undoes |
|---|---|---|---|
| compression | `!MOVES\<datetime>\` | `compress` | removes `_compressed` files |
| trash | `!REMOVE\<datetime>\` | `remove` | restores originals |

If both kinds of rows were in one manifest, a single undo would have to delete
the result and restore the original at once — making a decision for the user
that they might not want. Kept separate: "I didn't like the result" is undone
via the compression batch, "I discarded too early" — via the trash batch, in
any order and independently.

A single run of the compression queue in the review interface creates **one**
batch of each type, named after the queue start datetime; subsequent files
append rows to it as they complete. Appending to an existing manifest is safe
for the same reason the entire journal is safe — see below.
Details: [compression.md](compression-mechanism.md).

## Appended Journal, Not Modified in Place

A row is written to disk with status `planned` and **flushed to media**; only
**then** is the file move executed, followed by appending the final row with
status `done` or `failed`.

> [!warning]
> **Ordering is critical.** The reverse would leave moved files without any
> entry if interrupted — irrecoverably lost. With a batch the size of the whole
> inbox, this is not a theoretical consideration.

Writing is **append-only**, rather than overwriting a field in an existing row.
Two reasons:

1. Paths contain multi-byte characters, and CSV quoting has variable width.
   There is no single byte offset for "status starts here" that holds across
   every row.
2. Appending is crash-safe: a crash can only corrupt the final, incomplete row,
   never earlier ones. In-place overwriting risks destroying a **previously
   valid** row.

## Behavior on Errors

**A single move failure does not abort the batch.** File in use, permission
denied, path too long — the row gets status `failed` with the system message,
and the batch continues. The summary states how many files moved and how many
failed.

**Name collisions** resolve with a `__dup2` suffix, and `dst_rel` records the
actual name — ensuring undo returns to the right place.

**Rejection is recorded in the manifest**, not silently skipped. The execution
layer checks invariants itself and does not trust that it was called correctly:
destination outside archive, destination inside trash or move journal, source
outside archive, destination identical to source — each produces a `failed`
row with a reason. Checking is necessary because the destination directory name
is user-editable.

## Undo and Verification

**Undo** reverses the entire batch. If it encounters an occupied destination,
it refuses **for that entry**, prints it, and executes the rest.

For a `move` operation, undo **recreates the source directory** if it disappeared.
Sorting a folder empties it, and an empty folder may be cleaned up manually
or disappear via a cloud sync client. Without recreation, the batch would
become permanently irreversible. `remove` rows do not do this — their source
directories reside in the archive proper, which the tool does not empty.

**Batch verification** reports three lists: `planned` entries without a file at
the destination, orphans in trash, and size/hash mismatches. It **fixes
nothing** — it diagnoses state after an interrupted application.

Orphans are searched only among `remove` rows: the absolute path of a `move` or
`compress` row must not be mistreated as a relative path to the batch directory.

Verification is especially relevant for compression: encoding takes minutes,
so an interrupted batch halted on `planned` status is not a theoretical
scenario.

Series rows have empty hashes — a group of similar photos has no shared hash,
and each file has its own size. Verification skips empty hashes, yielding an
honestly weaker manifest instead of a falsely strong one.


---

---
tags: [dedup, technical]
---

# Persistent Tool Data

SQLite cache database in `.dedup\.db\cache.sqlite3`. Stores expensive
computation results and user decisions.

The entire `.db` directory can be deleted — it will recreate on next run. The
cost is recomputing hashes **and losing skips**, which are not calculated values
and cannot be recreated.

## Tables

**`files`** — core table. Key: path.

Columns: `path`, `display_path`, `size`, `mtime_ns`, `volume_id`, `file_index`,
`attributes`, `partial_hash`, `full_hash`, `dhash`, `status`, `error`,
`scanned_at`. Indexes: by size, by volume/file ID pair, and by status.

**Hit condition:** matching path, size, and mtime, with the needed hash column
populated. Then the file is not opened at all.

**`meta`** — single row with schema version and algorithm names.

**`timestamps`** — photo creation timestamp: `path`, `size`, `mtime_ns`,
`taken_at`, `source`. Separate table, not a column in `files`, for two reasons.
Schema creation finishes early when `files` already exists, so a new column
would never appear in an existing database without migrations. Moreover, this
value invalidates differently: hashes are cleared by algorithm changes, while
date is only invalidated when the file itself changes, which the size–mtime
pair already detects.

The date index is read for the **entire archive**, not just candidates of one
run — it answers which directories already contain photos from given days.
Therefore paying for decoding once is worth it.

**`series_skips`**, **`placement_skips`**, **`compress_skips`**, and **`dup_skips`** —
skipped items (`dup_skips` holds the "Exact duplicates" tab's decisions).
Each shaped as `group_key`, `paths`, `member_count`, `skipped_at`.

The key is built **from group members**, because group number is just an index
in a single run's result. Matching is by **subset**, not equality — rule and
rationale: [events-and-series.md](../../business/rules/events-and-series.md).

Separate tables instead of one because a duplicate group, series group, grouping
suggestion, and compression candidate can legitimately cover the same files — a shared
table would allow skipping one to hide another.

**`compress_probes`** — video stream parameters: `path`, `size`, `mtime_ns`,
`codec`, `width`, `height`, `fps`, `bitrate`, `duration`, `has_audio`,
`rotation`, `audio_codec`. Hit condition is the same as in `timestamps`:
matching path, size, and mtime. Thanks to this, reopening the compression tab
does not run `ffprobe` anew for each video.

**`compress_calibration`** — measured encoder performance: `machine_id`,
`encoder`, `bpp`, `mpx_s`, `calibrated_at`, `xpsnr_y` (can be `NULL`). Key is
the **machine–`device_key` pair**: the SQL column is still named `encoder` for
backwards compatibility (`device_key` for CPU **is** its encoder name, so rows
saved before hardware devices were introduced read unchanged), but on the
Python side (`Cache.get_calibration()` / `put_calibration()`) it holds
**`device_key`** — bare encoder name for CPU, `"<encoder>@<device name>"` for
hardware (`hwdetect.EncoderInfo.device_key`), so two cards from the same vendor
on one machine calibrate separately. The key includes machine because the same
file may be processed on two computers and results must not overwrite each
other. `xpsnr_y` — added column (migration `ALTER TABLE ... ADD COLUMN`, so a
pre-existing database receives it on first open, existing rows have `NULL`
here) — feeds the "Quality" column in the estimates table (difference relative
to `libx265` on the same machine). A row originates from an actual encoding
run: either from a finished task in the queue, or from the "Calibrate" button
(`POST /api/compress/calibrate`) —
[compression.md](compression-mechanism.md).

## Invalidation

Hash algorithm change zeroes `partial_hash`, `full_hash`, and `dhash` — these
are computed values.

**Skip tables are outside the reach of this mechanism.** "These photos are
fine" is a human judgement and survives every recalculation. Recalculation
does, however, clear sets of already-applied groups, as those are keyed by
numbers assigned afresh by recalculation.

## Writing and Concurrency

Connection runs in autocommit mode, so schema creation and metadata
synchronization are immediately durable, independent of an ongoing batch of
writes.

File writes are **batched**: transaction opens on first write and commits every
fixed number of rows. This makes an interrupted scan resumable — rows already
committed survive.

> [!warning]
> **Database object does not cross threads.** The connection is created without
> cross-thread sharing allowed, so an object built on the main thread would
> raise an error in a worker thread. Therefore the review server stores the
> database **path**, and each worker thread opens and closes its own connection.

## Other Generated Data

| Directory | Content |
|---|---|
| `.dedup\.reports\` | CSV and HTML reports |
| `.dedup\.thumbs\` | thumbnails, ~200 MB for entire archive |
| `.dedup\.compress\` | encoding workspace; results remain here until verified |
| `.dedup\.tests\` | temporary directories for tests touching real disk |

All are ignored by version control and all can be safely deleted.
