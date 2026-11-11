---
tags: [dedup, business, rules]
---

# What is a Duplicate

**An exact duplicate is a file identical byte-for-byte to another file.** This is
the only type of duplicate that the tool determines with certainty, and the only one
where discarding an instance is safe by definition — the retained copy has the
exact same content down to the byte.

Visual similarity is a separate matter governed by different rules and a different risk
profile: [business/rules/events-and-series.md](events-and-series.md).

## What is Never a Duplicate

The following files are visible in reports, but are **never** proposed for
removal. Not because they happen to have no copies — the tool deliberately ignores them.

| Item | Reason |
|---|---|
| `desktop.ini`, `Thumbs.db`, any `*.db` | system and cache files, not archive content |
| `VIDEO_TS` directories | `.IFO` and `.BUP` files can be identical across different DVDs; removing them breaks DVD disk structure |
| 0-byte files | report only |
| Sidecars `.AAE` and `.xmp` | always retained alongside their photo, never evaluated independently |
| RAW or editing file + `.jpg` pair with same name | two distinct files of different value: source asset and exported image — treated jointly as a sidecar |
| `.dedup`, `!REMOVE`, `!MOVES` | tool internal directories; trash protection described in [business/guarantees.md](../guarantees.md) |

> [!warning]
> **RAW and JPG sharing the same base name are a pair, not a duplicate.** This rule
> exists because an error here is irreversible in practice: a user losing the source file
> is left with only the rendered export and cannot recover the original.

## Files Visible But Excluded From Evaluation

| Situation | Behavior |
|---|---|
| **Hard link** | multiple paths pointing to the same file; collapsed to one representative so the tool does not propose removing a non-existent duplicate copy |
| **Offline file** | cloud placeholder; reading it forces downloading content, so the file is not opened |
| **Unreadable file** | read errors are reported without interrupting the scan |

## Format Scope Depends on Mode

A distinction that can be confusing, stated plainly:

| Format | Exact Dedup | Series, Events, Inbox |
|---|---|---|
| `.jpg`, `.jpeg`, `.png`, `.HEIC` | yes | yes |
| Video | yes | events only |
| Audio | yes | no |
| RAW, `.psd`, `.cdr` | yes | no |

Exact deduplication works on **every** file because it compares bytes without needing to
understand the format. Image-based modes require decoding, so any format unreadable
by the decoder is excluded. This is a deliberate boundary — without decoding, any decision
on such a file would be mere guesswork.

Audio is omitted from image modes for a different reason: perceptual image hashing
and geographic coordinates do not apply to audio.

## User-Defined Scope Restrictions

Beyond permanent rules, scope can be narrowed: by minimum file size, list of
extensions, and list of directories. Narrowing **never expands** the set — none
of the exclusions described above can be disabled through flags. Flags:
[business/instructions/CLI.md](../instructions/CLI.md).

## Skipping a Group in the Review UI

On the "Exact duplicates" tab, "Skip this group" works exactly like skipping a
series group: the decision is **permanent**, recorded **by the group's file
paths** (never by group number), and the group disappears from the tab on this
and every later run. A group that loses a copy stays hidden; a group that gains a
new copy reappears. "Show skipped groups" (with the hidden count) is the way back,
and unticking the skip there deletes the stored decision.

Skipped groups are never submitted by "Apply current page" / "Apply all", and the
CSV report still lists every group. Rule details:
[business/rules/events-and-series.md](events-and-series.md#skipping-a-group).
