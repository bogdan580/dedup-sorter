---
tags: [dedup, glossary]
---

# Glossary of Terms

Binding terminology. Code, documentation, review UI, and reports use the same words with the same meanings — if something differs from this file elsewhere, that other place is an error.

Technical names (`dup-exact`, `coherent`) are given as they actually appear in output files and code. We do not translate them in data — only here.

## Archive Directories

| Term | Meaning |
|---|---|
| **Archive** | The parent directory of `.dedup`, automatically detected. It is the default scope of every scan. |
| **`!NEW`** | The inbox. A place for fresh uploads — individual files and entire folders — before they reach their final destination in the archive. |
| **`!REMOVE`** | The trash. Contains batches of discarded files, each with its own manifest. Nothing disappears from disk until the user manually empties this directory. |
| **`!MOVES`** | Log of internal moves within the archive, including compression batches. Contains **only manifests** — the files themselves move to their destination locations, not here. |
| **`.dedup`** | The tool's directory. Lives inside the archive, but is never part of its content. |
| **Dump folder** | A folder whose name indicates an automatic device dump: `Camera`, `DCIM`, `Download`, `Baseus`, `100ANDRO`, `Nowy folder`. Never moved as a whole, even if its contents look coherent. |

## Files and Their Roles in a Scan

| Term | Meaning |
|---|---|
| **Candidate** | A file that can potentially be considered a duplicate. Exclusions remove this status. |
| **Inventory file** | A file visible in the report, but never proposed for removal: `desktop.ini`, `Thumbs.db`, any `*.db`, 0-byte files. |
| **Sidecar** | An accompanying file for a photo (`.AAE`, `.xmp`), always evaluated together with its photo, never independently. A RAW/editing pair (`.ARW`, `.psd`, `.cdr`) + `.jpg` with the same name is treated the same way. |
| **Offline file** | A cloud placeholder (OneDrive, Google Drive). Reading it would force downloading content, so the tool never opens it. |
| **Hard link** | Multiple filesystem paths pointing to the same file on disk. Collapsed into a single representative — otherwise the tool would propose "removing" a file with no actual copy. |

## Duplicates

| Term | Meaning |
|---|---|
| **Exact duplicate** | A file byte-for-byte identical to another file. The only duplicate type that the tool asserts with certainty. |
| **Group** | A set of files deemed to have identical content. A group has exactly one keeper. |
| **Keeper** | The copy that is kept. Chosen automatically, but the user can designate another one in the review UI. |
| **Discarded** | A copy designated to be moved to `!REMOVE`. Discarding is a proposal — it only becomes reality upon applying. |
| **Rule 0** | The overarching keeper selection rule: a copy from `!NEW` always loses to a copy in the archive, regardless of other criteria. |

## Visual Similarity

| Term | Meaning |
|---|---|
| **Series** | A group of photos that are **not** byte-identical, but depict the same scene/shot: burst, recompression, multiple attempts at the same framing. |
| **dHash** | Perceptual image hash. Resistant to recompression and resizing, unlike a byte hash. |
| **Hamming distance** | A measure of difference between two dHashes. The smaller it is, the more similar the images. |
| **Time window** | Maximum time interval between photos to consider them as a single scene at all. |

In a series, **more than one file can be kept** — these are several distinct, valuable shots, not redundant copies of the same content. This distinguishes a series from a duplicate group.

## Events

| Term | Meaning |
|---|---|
| **Event** | A set of files created around the same time and place — a trip, an occasion, a day. |
| **Time gap** | The time interval after which a new event begins. The primary splitting axis, as it also works for files without GPS. |
| **GPS jump** | A location shift large enough to split one event into two locations — e.g. a one-day excursion during an extended stay. |
| **GPS inheritance** | Assigning coordinates of the closest temporal neighbor from the same event to a file without its own location. |
| **Camera offset** | Clock correction for a specific camera model, applied strictly in memory. Never modifies files. |
| **Suspicious date** | A date before the year 1995 or in the future. Such a file does not participate in GPS inheritance — its timeline is uncertain. |

## Grouping Suggestions

| Term | Meaning |
|---|---|
| **Suggestion** | A proposal to move a set of files to a specific archive directory. Always a proposal — the tool does not guess on behalf of the user. |
| **Destination directory** | The location where files will go. Editable; can point to an existing directory or one created upon applying. |
| **Coherent folder** (`coherent`) | A subfolder of `!NEW` whose files constitute exactly one event. Receives a single suggestion: move whole folder. |
| **Mixed folder** (`mixed`) | A subfolder spanning more than one event. Each event gets a separate suggestion; the folder is never moved as a whole. |
| **Loose files** (`loose`) | Files sitting directly in `!NEW`, without a subfolder. Always handled per event — there is no folder to move. |
| **No media** (`no_media`) | A folder containing no files the tool can evaluate. Reported only. |

## Video Compression

| Term | Meaning |
|---|---|
| **Compression candidate** | A video that passed the heuristics: has bit headroom, is not a tool artifact, and is readable. Rules: [business/rules/compression.md](business/rules/compression.md). |
| **`bpp`** | Bits per pixel: bitrate divided by pixel count per second. A metric of bit headroom comparable across resolutions. |
| **Quality profile** | A named set of encoding settings. Presented as a "smaller file / higher quality margin" tradeoff, not as codec names. |
| **XPSNR** | Metric of output similarity to the original, in decibels. A result below the threshold is rejected, leaving the original untouched. |
| **`_compressed`** | Suffix of the output file. Created **alongside** the original, never replacing it. A file with this suffix is no longer a candidate. |
| **Encoding working directory** | `.dedup\.compress\` — location where the output stays until verification passes. An aborted encode leaves garbage here, never in the archive. |
| **Calibration** | Replacing estimates with measured values on this machine. Occurs automatically from completed jobs or on demand via the "Measure" button. |
| **`device_key`** | Identity of a single encoder on a single device under which calibration is saved: bare encoder name for CPU, `"<encoder>@<device name>"` for hardware. |

## Operations

| Term | Meaning |
|---|---|
| **Dry-run** | Default mode: the tool only reports what it proposes. No file is moved. |
| **Apply** | Executing proposals: moving files and writing the manifest. The only moment anything changes on disk. |
| **Batch** | A single apply operation: a `<date>_<time>` directory with a manifest. Each click of "Apply" creates its own batch. |
| **Manifest** | The `manifest.csv` file describing a batch. The only medium of reversibility — without it, moves cannot be undone. |
| **Restore** | Reversing an entire batch based on its manifest. |
| **Batch verification** | Checking batch consistency without changing anything. Used to detect an interrupted apply operation. |
| **Skip** | A persistent user decision to never show a given group or suggestion again. Saved by file paths, not by group number — group numbers change after every recalculation. |
| **Recalculation** (*rescan*) | Rebuilding results from scratch. Does not clear skips: these are user decisions, not calculated values. |

## Values in Output Files

These appear in `raport.csv` and `manifest.csv`. We do not translate them — they are part of the data format.

**`reason`** — why a file entered the batch:

| Value | Meaning |
|---|---|
| `dup-exact` | Discarded as a byte-for-byte duplicate. |
| `dup-visual` | Discarded as part of a series. |
| `rule-0` | Discarded by Rule 0 — copy from `!NEW`. |
| `event-sort` | Moved as part of grouping suggestions. Does not discard anything. |
| `compress-remove-original` | Original discarded following successful, verified compression. |

**`op`** — operation type:

| Value | Meaning |
|---|---|
| `remove` | Move to `!REMOVE`. |
| `move` | Move within archive. |
| `compress` | Insert verified compression result alongside original. Original remains untouched. |

**`status`** — manifest row state:

| Value | Meaning |
|---|---|
| `planned` | Move planned, not yet executed. Recorded **before** file movement. |
| `done` | Move completed. |
| `failed` | Move failed; reason in `error` column. Does not abort the rest of the batch. |

**`kind`** — how the scan classified the file:

| Value | Meaning |
|---|---|
| `dup` | Belongs to a duplicate group. |
| `unique` | No duplicates. |
| `hardlink-of` | Hard link to another row. |
| `offline` | Cloud placeholder, content not read. |
| `zero-byte` | Empty file. |
| `error` | Failed to read file. |

**`role`** — role in group: `keeper` or `discarded`.

## Capability Names Instead of Phase Numbers

Code and older documentation still use "Phase N" labels — indicating the **point of inception**, not the feature. Names in the right-hand column are binding; the left column is only for reading legacy code that hasn't undergone migration yet.

| Former label | Current name |
|---|---|
| Phase 1–3 | scan and exact duplicates |
| Phase 4 | CLI modes |
| Phase 4B | review UI |
| Phase 5, 5B | series |
| Phase 6 | events |
| Phase 7 | `!NEW` inbox |
| Phase 8 | grouping suggestions |

In new code and new documentation, "Phase N" is not used.
