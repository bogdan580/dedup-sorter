---
tags: [dedup, usage]
---

# Command Line Reference

```
python .dedup\dedup.py [mode] [options]
```

Default threshold values are provided for convenience; their **meaning and rationale**
live in domain rules linked below.

## Mutually Exclusive Modes

At most one can be specified. Without any of them, the tool performs a duplicate
scan in dry-run mode.

| Flag | Action | Moves files |
|---|---|---|
| `--apply` | moves discarded duplicates to the trash with a manifest | **yes** |
| `--restore MANIFEST` | rolls back the entire batch recorded in the manifest | yes, restores |
| `--verify-manifest MANIFEST` | reports batch inconsistencies | no |
| `--review` | launches the review UI | only on click |
| `--series` | detects series of similar photos | no |
| `--events` | groups files into events | no |
| `--inbox` | inbox triage: dedup, series, and suggestions in a single report | no |
| `--compress` | identifies videos worth transcoding; with `--apply` encodes them | with `--apply` |

`--series`, `--events`, and `--inbox` on the command line **only plan**.
Execution takes place in the review UI.

`--compress` is the only analytical mode that **can be combined
with `--apply`** — and the only one that must not be combined with other modes;
attempting to do so results in a refusal error message.

## General Options

| Flag | Meaning | Default |
|---|---|---|
| `--roots PATH [...]` | limit scan to specified subdirectories | entire archive |
| `-y`, `--yes` | skip confirmation prompt before hashing | prompts |
| `--report PATH` | path to write CSV report to | `.dedup\.reports\raport.csv` |
| `--rescan` | bypass cache database and recalculate everything from scratch | disabled |
| `--db PATH` | cache database path | `.dedup\.db\cache.sqlite3` |
| `--min-size BYTES` | skip files smaller than specified byte count | no limit |
| `--exclude NAME [...]` | additional directory names to skip | see below |
| `--ext EXTENSION [...]` | restrict candidates to specified extensions | all |
| `--keeper-config PATH` | custom configuration file | `.dedup\config.json` |
| `--archive-root PATH` | where to create trash upon applying | archive directory |
| `--workers NUMBER` | number of decoding threads for image-based modes; `1` disables concurrency | 4 |

`--exclude` **appends** to fixed exclusions that cannot be disabled
by any parameter — see
[safety guarantees](../guarantees.md)
and [what is a duplicate](../rules/duplicates.md).

`--ext` narrows down only the pool of **candidates for removal**. Remaining files
are still scanned and visible in the report.

Change `--archive-root` only when you are genuinely pointing to an archive
different from the one where the tool resides.

On every run — in any mode, including dry-run — the tool
creates missing `!NEW`, `!MOVES`, and `!REMOVE` directories directly inside
the archive directory (along with the `.dedup-kosz` sentinel file in `!REMOVE`)
and prints which ones were created. Existing directories are left untouched.
If the archive directory itself does not exist (e.g. typo in `--archive-root`),
nothing is created.

## Review UI Options

| Flag | Meaning | Default |
|---|---|---|
| `--port NUMBER` | server port | `0` — free port chosen automatically |
| `--no-browser` | do not open browser tab | opens |
| `--thumbs-dir PATH` | thumbnails directory | `.dedup\.thumbs` |

## Series Options

| Flag | Meaning | Default |
|---|---|---|
| `--series-report PATH` | path to save report | `.dedup\.reports\raport_serie.csv` |
| `--series-window-seconds NUMBER` | maximum time interval between photos | 300 |
| `--series-hamming-threshold NUMBER` | maximum image hash difference | 6 |

Threshold meanings: [business/rules/events-and-series.md](../rules/events-and-series.md).

Both numbers also set the initial values of controls in the review UI
(Series tab, developer mode), where they can be adjusted for the session duration —
the change takes effect after Rescan and is not persisted. Permanent
settings are configured using these flags, e.g. in `run.cmd`.

## Event Options

| Flag | Meaning | Default |
|---|---|---|
| `--events-report PATH` | path to save report | `.dedup\.reports\raport_wydarzenia.csv` |
| `--event-gap-hours NUMBER` | time gap starting a new event | 8 |
| `--gps-jump-km NUMBER` | location jump splitting an event | 25 |
| `--since Nd` | only recent events from places absent from the archive, e.g. `7d` | no limit |

Threshold meanings: [business/rules/events-and-series.md](../rules/events-and-series.md).

## Compression Options

| Flag | Meaning | Default |
|---|---|---|
| `--compress-remove-originals` | with `--compress --apply`: moves each original to `!REMOVE` whose result passed verification and entered the archive | does not move |
| `--compress-staging-dir PATH` | encoding working directory | `.dedup\.compress` |
| `--compress-device N` | with a hardware profile (NVENC/QSV): device index among those detected by `hwdetect.py` for this encoder (0 = first) | first detected device |

Without `--apply`, the command only lists candidates alongside estimated
sizes after compression — dry-run applies here as everywhere else.
`--compress-remove-originals` without `--apply` does nothing.

Scope is narrowed using the **existing** `--roots` flag; this mode
has no separate directory selection. The quality profile is taken from `config.json`,
from the `compression.default_profile` field — the CLI does not allow switching it,
because profile selection is a decision made when viewing estimates, which
are only shown in the review UI.

`--compress-device` only matters when `default_profile` points to
a hardware encoder — for CPU profiles it is ignored. An index outside
the range of detected devices aborts the command with an error (ASCII, without encoding)
before encoding begins, not in the middle of it.

> [!warning]
> The original is moved to the trash only after a verified result and always as a
> **separate batch**, rolled back independently. Rules:
> [business/rules/compression.md](../rules/compression.md).

## Inbox Options

| Flag | Meaning | Default |
|---|---|---|
| `--inbox-report PATH` | path to save HTML report | `.dedup\.reports\raport_new.html` |

Inbox triage also respects `--min-size`, `--ext`, `--keeper-config`, and
all series and event thresholds — they function inside it identically to
standalone modes.
