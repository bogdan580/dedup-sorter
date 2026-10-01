---
tags: [dedup, usage]
---

# Workflows and Scenarios

## Principle of Order

Always in this sequence: **dry-run on everything → apply on a narrow scope →
apply on everything**. Never the reverse.

The reason is practical, not ceremonial: the first run on real data
always reveals something unexpected.

## Full Archive Preview

```
python .dedup\dedup.py
```

Does not move anything. Review `.dedup\.reports\raport.csv` and the summary
in the console.

## Removing Duplicates in a Single Directory

First a targeted dry-run, then the same with application:

```
python .dedup\dedup.py --roots "!NEW\SubdirectoryName"
python .dedup\dedup.py --roots "!NEW\SubdirectoryName" --apply
```

Check the manifest in `!REMOVE\<date_time>\` and the summary of moved
file counts. Once confident in the result, run the same without `--roots`.

> [!warning]
> **Caution with multiple directories at once:** applying will move **all**
> found groups, including duplicates existing solely between directories that were
> intended only as reference points. Details:
> [business/instructions/limitations.md](limitations.md).

## Undoing a Mistake

```
python .dedup\dedup.py --restore "!REMOVE\<date_time>\manifest.csv"
```

Internal archive moves are undone the same way — the manifest is located
in `!MOVES`.

## After an Interrupted Apply

If an apply operation was interrupted — closed console window, power
outage — check batch status **before** attempting to roll back:

```
python .dedup\dedup.py --verify-manifest "!REMOVE\<date_time>\manifest.csv"
```

This command does not repair anything. It reports planned entries without executed
moves, orphans in the trash, and size mismatches.

## Reviewing in the Browser

```
python .dedup\dedup.py --review
```

For each group, you can see which file is kept and which ones go to the trash. Clicking
a card alters the selection. Two apply buttons — all and current page —
each with confirmation. Nothing moves until you click.

This is also where series and grouping suggestions are evaluated: doing this
meaningfully from a CSV table is impossible.

## Series and Events from the Command Line

```
python .dedup\dedup.py --series
python .dedup\dedup.py --events
```

Both **report only**. The output is a CSV to inspect; moves are executed
via the review UI.

## New Inbox Triage

```
python .dedup\dedup.py --inbox
```

A single command: dedup against the entire archive, series within the inbox, and suggestions
for where each folder belongs. Output: a single HTML report. Step sequence
is enforced — [business/rules/inbox.md](../rules/inbox.md).

## Trip Video Compression

In the review UI, on the **Compression Suggestion** tab:

1. Select source directory — default is `!NEW`, but the list covers the entire
   archive, with recursive subdirectory scanning;
2. Review candidates: each shows its current size as well as estimated
   size and duration across each quality profile;
3. Deselect items you do not want to touch, or skip them permanently;
4. Choose a profile and click **Compress**. Encoding runs in the background, file by
   file, with a progress bar and cancellation support. Each completed result appears
   alongside its original immediately, not at the end of the entire queue.

Upon completion, a results list appears with selectable rows and a button
moving **selected originals** to `!REMOVE`. Alternatively, you can enable
deleting originals before starting — the result is the same, only the timing
of the decision differs.

From the command line, when previewing alone is enough or the configured default
profile is appropriate:

```
python .dedup\dedup.py --compress --roots "#Home\2024 Gdansk"
python .dedup\dedup.py --compress --roots "#Home\2024 Gdansk" --apply
```

> [!warning]
> Before discarding originals, **inspect several results** — recompression cannot
> be undone. Moving originals itself is reversible via `--restore`,
> but only until you empty the trash.

## Emptying the Trash

Done **manually**. The tool never deletes from disk.

The directory layout `!REMOVE\<date_time>\` provides a natural chronological order:
delete oldest batches first. **Do not delete anything until the archive is backed up** —
[business/instructions/limitations.md](limitations.md).
