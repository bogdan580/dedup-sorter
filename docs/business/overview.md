---
tags: [dedup, business]
---

# What is Dedup

A tool for organizing a large photo and video archive that has grown over years. Operates on a local archive stored on a Windows drive.

## Five Problems

**Exact duplicates.** The same files copied to multiple locations: backups, "copy (1)", dumps from consecutive devices. The tool finds them, selects a copy to retain, and allows moving the rest aside. Rules:
[business/rules/duplicates.md](rules/duplicates.md),
[business/rules/version-selection.md](rules/version-selection.md).

**Series and near-duplicates.** Bursts, recompressions, multiple attempts at the same framing. Files are not byte-identical, so normal dedup cannot detect them — visual comparison comes into play here. Rules: [business/rules/events-and-series.md](rules/events-and-series.md).

**Event grouping.** Automatic sorting of loose files into trips based on time and location, proposing destination folders tailored to the archive's conventions. Rules: [business/rules/events-and-series.md](rules/events-and-series.md),
[business/rules/version-selection.md](rules/version-selection.md).

**`!NEW` inbox triage.** A single command: deduplicating new files against the entire archive, detecting series, and generating suggestions for where each folder belongs. Rules: [business/rules/inbox.md](rules/inbox.md).

**Videos taking up far too much space.** Smartphone recordings have bit headroom that can be reduced without visible quality loss. The tool identifies files for which recompression actually makes sense, encodes them, and leaves the decision regarding the original to the user. Rules:
[business/rules/compression.md](rules/compression.md).

## Requirements It Stemmed From

| # | Requirement |
|---|---|
| R1 | Find file duplicates across different directories |
| R2 | Ability to specify specific directories to search |
| R3 | Reusable tool, not a one-off script |
| R4 | On demand: similar photos, e.g. series from a short time window |
| R5 | On demand: grouping suggestions into directories by location and trips |
| R6 | `!NEW` as an inbox — files **and entire folders** |
| R7 | Review UI: what will be removed, series review |
| R8 | Executing grouping suggestions, not just reporting them |
| R9 | Video size reduction without visible quality loss, keeping original for a separate decision |

## Two Modes of Operation

**Command line** executes operations on exact duplicates (`--apply`, `--restore`) and compression (`--compress --apply`), and **plans** the rest: `--series`, `--events`, and `--inbox` generate a report for review, but do not move or delete any file.

**Review UI** (`--review`) is the decision center. Exact duplicates and series can be applied from it (files move to `!REMOVE`), grouping suggestions move files deeper into the archive (cards marked red go to `!REMOVE`), and compression creates new files alongside originals. Series and events cannot be assessed reasonably from a CSV table — thumbnails are necessary.

## What This Tool Does Not Do

> [!info] What this tool does not do
> - **Does not delete.** Permanent deletion is always performed manually by the user — see
>   [business/guarantees.md](guarantees.md).
> - **Does not guess for the user.** The destination directory is a proposal that can be edited; keeper selection is a suggestion that can be changed.
> - **Does not process images beyond recompression.** Video compression does not change resolution, crop, or color-correct; photos are not compressed at all.
> - **Does not evaluate photo content.** Recognizes visual similarity, but does not recognize faces, objects, or compositional quality.
> - **Does not support formats unreadable by the image decoder** in image-based modes — RAW, `.psd`, `.cdr`. In byte deduplication, they work normally because hashes matter there, not image content.
> - **Does not work outside Windows.** The file engine relies on Win32: long paths, junctions, hard links, cloud placeholder detection.

Full list of limits: [business/instructions/limitations.md](instructions/limitations.md).


---

---
tags: [dedup, business]
---

# Archive Facts

Characteristics of the dataset for which this tool was built. These numbers are not trivia — they dictate rules. Date coverage of 87% versus GPS coverage of 31% is the reason why time is the primary axis for event splitting, with location being merely an enrichment.

Values come from measurements of the entire archive. With significant content changes, they should be recalculated, as several thresholds were chosen specifically for them.

## Scale

| Metric | Value |
|---|---|
| Files / size | 17,513 / 314.52 GB |
| Photos / video | 14,616 / 2,701 |
| Free disk space | 31.9 GB |
| Date in EXIF | 87% of sample |
| GPS in EXIF | 31% of sample |

Cameras present in the collection: realme 6 Pro, SM-S901B, COOLPIX P90, Lenovo P780, Redmi 4, Nokia 7 plus, NIKON D3100, Canon EOS 550D. The last two are DSLRs without GPS and are prone to clock drift — see [business/rules/events-and-series.md](rules/events-and-series.md).

Free space is ten times smaller than the archive size. Therefore, removal is a move within the same volume: it consumes no extra space and is instantaneous.

## File Type Inventory

| Group | Extensions | Files |
|---|---|---|
| Photos | `.jpg` 13,694, `.HEIC` 769, `.png` 115, `.jpeg` 38 | **14,616** |
| Video | `.mp4` 2,321, `.MOV` 187, `.3gp` 129, `.wmv` 20, `.mpg` 19, `.VOB` 16, `.AVI` 5, `.DAT` 2, `.3gpp` 2 | **2,701** |
| RAW and editing | `.ARW` 8, `.psd` 5, `.cdr` 3 | **16** |
| Audio | `.mp3` 24, `.ogg` 15 | **39** |
| Sidecars and system | `.AAE` 89, `.IFO` 7, `.BUP` 6, `.db` 6, `.ini` 5, `.xmp` 1 | **114** |
| Others | `.VCD` 6, `.exe` 3, `.txt` 3, `.rar` 2, `.gif` 2, `.FNT` 1 | **17** |

The sum is short of 17,513 by about ten items — these are isolated extensions outside the top thirty.

**Pairs with the same base name in the same directory:**

| Pair | Count | Meaning |
|---|---|---|
| `.HEIC` + `.jpg` | 18 | conversion alongside original — determined by [original rule](rules/events-and-series.md) |
| `.ARW` + `.jpg` | 3 | **not a duplicate** — RAW and exported JPG, see [business/rules/duplicates.md](rules/duplicates.md) |
| `.HEIC` + `.MOV` | 0 | Live Photos do not occur in this collection |

RAW files are marginally present in the archive — 16 files out of 17,513. That is why lacking a RAW decoding library is a deliberate choice, not an oversight to fix.

## Naming Conventions

Derived from disk inspection. Suggestions for destination directories are generated **in this convention**, not an invented one.

| Pattern | Examples |
|---|---|
| `#Location` at top level | `#Home`, `#Trip-A`, `#Travel`, `#Trip-B`, `#Trip-C`, `#Trip-D`, `#Місто` |
| `YYYY Location` in subfolder | `#Travel\2023 Mountains`, `#Trip-C\2019 Місто-Море` |
| `Location month year` | `#Trip-B\Lakes February 2024` |
| `DD.MM.YYYY Event` | `#Trip-D\26.03.2015 Photoshoot` |

The convention is non-uniform, which is a reality the tool must handle. The `YYYY Location` convention is followed by most second-level directories. The rest cannot be recognized by name alone — `#Home\Kielce`, `#Home\1 rok 15-16`, `#Home\Cars` say nothing about when their contents were created. Therefore, matching a destination directory does not rely solely on name, but additionally on the dates of photos already residing there — see [business/rules/version-selection.md](rules/version-selection.md).

Names contain Cyrillic and diacritical marks, and paths may exceed 260 characters. This is a hard requirement for the file engine, not an edge case.

### Dates Embedded in Filenames

When EXIF is missing, the date is often in the filename itself — but not in a single format. Four variants appear in the archive, differing only in separators:

| Format | Example |
|---|---|
| without separators | `IMG_20210425_010518`, `VID20210425010518` |
| date and time separated by space | `2021-02-15 194231.jpg` |
| dashes with millisecond tail | `2025-05-12-230123475.mp4` |
| underscores between every field | `2020_05_06_08_50_23~2.mp4` |

Recognizing only the first format previously impacted **118 of 1,581** files in the inbox: each of them had a readable date in the name, yet fell back to modification date. And modification date is the date the file was **copied**, so all of them landed in the wrong year — hence in the wrong destination directory.

The separator must be **the same on both sides**: `2021-02-15` is a date, `2021-0215` is likely an identifier. For the same reason, a four-digit segment is only parsed as a year in the 1995–2100 range. Otherwise, epoch millisecond names (`1780592864140.png`) parse as year 1780, and Messenger IDs (`received_280487600480313`) and Telegram IDs (`2_5316582463433089328`) accidentally match date patterns.

## The `!NEW` Inbox

Uploads arrive as **entire folders**, not loose files — and this determines how they are handled: the tool operates on folders, not a bag of files.

The uploaded folder's name can be descriptive (`Gdańsk`, `Travel`) or generic (`Camera`, `DCIM`). The former is a hint with higher priority than the geocoder; the latter is ignored — rule in [business/rules/version-selection.md](rules/version-selection.md).

A typical difficult case is a `Camera` folder: a camera roll dump spanning over five years without any subdivision. Such a folder must not be moved as a whole, because a single directory name cannot describe five years of photos.
