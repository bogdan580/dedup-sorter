---
tags: [dedup, business, rules]
---

# Video Compression

Smartphone videos in this archive occupy many times more space than justified
by their content. Compression answers one question: **which videos can be transcoded
with genuine space gains and without visible quality degradation** — and then
allows deciding what to do with the original.

> [!important] Overarching Principle
> Compression **never modifies or deletes the original.** A new file is created
> alongside it, with the `_compressed` suffix. Deleting the original is a **separate
> decision** and a separate, reversible batch — see
> [business/guarantees.md](../guarantees.md).

## When a File Qualifies for Compression

Checks run in order; the first match determines the verdict.

| # | Condition | Verdict |
|---|---|---|
| 1 | name ends in `_compressed` | skip — tool's own artifact |
| 2 | `<name>_compressed.<ext>` already exists alongside | skip — result already exists |
| 3 | extension is not a video extension | skip |
| 4 | file resides in a directory permanently excluded from scans (`!REMOVE`, `!MOVES`, `.dedup`) | skip |
| 5 | reading stream parameters failed | skip and note — no guessing |
| 6 | duration shorter than 2 seconds | skip — overhead exceeds gains |
| 7 | intra-frame or uncompressed codec | **worthy** — gain independent of bitrate |
| 8 | `bpp` above threshold for source codec | **worthy** — bit headroom available |
| 9 | all other cases | skip — generational loss with no gain |

Codecs from rule 7: `mjpeg`, `dv`, `rawvideo`, `prores`, `ffv1`, `mpeg2video`.

Cloud placeholders and hard links are skipped before stream parameter reading:
opening a placeholder would force downloading the video from the cloud, and compressing
one path of a multi-link file would orphan the remaining links.

## `bpp` and Thresholds

`bpp` (*bits per pixel*) is bitrate divided by the number of pixels per
second:

```
bpp = bitrate / (width × height × frame_rate)
```

This single number indicates how many bits are allocated per image pixel — in other words,
how much excess headroom can be trimmed. It is comparable across resolutions and frame
rates, which raw bitrate cannot provide: 20 Mbps is plenty for 1080p30, but low for 4K60.

| Source Codec | Threshold |
|---|---|
| `h264` | 0.15 |
| `hevc`, `vp9` | 0.10 |
| `av1` | 0.08 |
| others | 0.12 |

Thresholds can be edited in `config.json`. The order of magnitude motivating them:
smartphone video recorded in HEVC reaches about **0.45 bpp** in this archive —
many times above the threshold. Material that has already been compressed once sits below,
and a second pass would yield only generational loss.

Rule 1 recognizes **only the tool's own** suffix and deliberately avoids attempting to detect
markings from other tools (`_x265`, `_HB`). A filename is not proof, and material genuinely
already compressed will be filtered out by Rule 8 based on measured stream parameters anyway.

## `!NEW` is Not Excluded

Unlike [Rule 0](version-selection.md), where an inbox copy always loses,
compression treats `!NEW` normally — in fact, it is its default source directory.
There, the question is which of two **identical** copies survives; here, the task is transcoding
an individual file in place. Fresh smartphone dumps are precisely the material possessing
the greatest bit headroom.

`!REMOVE` and `!MOVES` remain strictly excluded, as in all modes.

## Quality Profiles

The choice comes down to a single axis: **smaller file size vs higher quality headroom**.
Names shown in the UI reflect this tradeoff rather than codec names.

| Profile | Characteristic |
|---|---|
| High Quality | largest output, safest — passes quality threshold even on difficult material |
| Enhanced Quality | intermediate output |
| Standard Quality | smallest output using the HEVC codec |
| Smallest File | AV1: smallest output, longest encoding time |

The list is ordered from safest to smallest — selecting "smallest file" is a deliberate
step down, not a default choice.

Three HEVC profiles exist because CRF 21 alone does not guarantee sufficient margin over
the quality threshold on real-world footage. Test on a single real recording:

| CRF | XPSNR-Y | Size relative to CRF 21 |
|---|---|---|
| 21 | 37.24 dB | — |
| 19 | 38.35 dB | approx. +35% |
| 18 | 38.99 dB | approx. +56% |

Only CRF 18 passes the 38 dB threshold with a clear margin (approx. 1 dB). The profile
from `config.json` (`default_profile`, default CRF 21) is selected by the CLI;
the UI opens the list sorted from safest down. Choosing a profile dynamically based on
source `bpp` has been postponed — [technical/development/backlog.md](../../technical/development/backlog.md).

Hardware profiles (NVENC, Quick Sync) exist alongside CPU profiles and adhere to
the same quality threshold rule below — they appear in the UI and CLI **only** on a machine
possessing the physical encoder (not merely its name in the FFmpeg build list). The tradeoff:
hardware encoders encode much faster, but at equivalent visual quality they typically produce
a **larger** file than CPU — so no option is strictly superior, and the UI displays size and time
side by side rather than implying faster is always better. Hardware detection and benchmarking
mechanics: [technical/components/compression-mechanism.md](../../technical/components/compression-mechanism.md).

## Output Quality Threshold

Each result is compared against the original using the XPSNR metric and is **rejected**
if it fails to achieve **38 dB**. A rejected result never enters the archive, and the
original remains untouched — an unsuccessful compression costs nothing beyond time.

On difficult-to-compress footage, a result can look visually fine yet land just under
a decibel below threshold. For such cases, an **explicit relaxed quality consent** option
exists, lowering the threshold to **36 dB**. Two aspects of this consent are essential:

- it is granted **for a single specific operation** and does not carry over to the next —
  the checkbox resets on every run;
- **it does not disable verification**, it only adjusts the threshold. There is no way for a result
  to enter the archive without comparison against the original.

Beyond visual quality, verification checks: file existence and non-zero size, duration
matching within a 0.5-second tolerance, matching frame count, and successful complete decode.

## When Originals May Be Discarded

> [!warning]
> **The original moves to `!REMOVE` only after a verified result.**
> Failed encoding or failed verification never moves the original — regardless
> of what was selected prior to starting. This is the only scenario where a user could
> genuinely lose footage, so the removal list is built from **verified results**,
> not queued tasks.

A second safety check: if the result turns out **not smaller** than the original,
the original remains in place regardless of the checkbox. A compression that failed to reduce
filesize is no reason to discard the source.

By default, originals are **not** moved, and this default is not configurable:
there is no field in `config.json` enabling "move originals to `!REMOVE`", in the UI
the checkbox resets every time the view renders, and in the CLI it requires a dedicated flag
(`--compress-remove-originals`). Moving files to the trash must be a deliberate decision
made for a specific batch, visible on screen when clicking — not a stale setting from a month ago.
The same decision can be made after encoding from the results list: only rows with verified
results can be selected. Mechanics:
[technical/components/review-ui.md](../../technical/components/review-ui.md).

Discarding originals is always a **separate batch** with its own manifest —
distinct from the compression batch itself. The separation is intentional: "I don't like the result"
is undone via the compression batch, while "I discarded the source too early" is undone via the trash batch,
independently and in any order. Mechanics:
[technical/components/manifest-and-db.md](../../technical/components/manifest-and-db.md).

## Skips

A video can be skipped permanently. Skips are recorded by file path, not list index,
and persist across recalculations — identical to skips for series and grouping suggestions.
