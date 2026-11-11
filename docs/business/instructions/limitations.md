---
tags: [dedup, usage]
---

# Known Limitations

Current state: what does not work, what works differently than expected, and what
has not been tested on a real physical drive.

## Unsupported Formats

| Format | Limitation |
|---|---|
| `.HEIC` | requires an installed HEIF decoder; without it, invisible to series, events, and thumbnails. Operates normally in byte deduplication |
| RAW, `.psd`, `.cdr` | permanently excluded from image-based modes — no decoder. This is an intentional design boundary |
| Video | excluded from series detection; still captured in byte deduplication, events, and compression |
| Audio | excluded from image-based modes; deduplicated by byte |

## Console Crash on Characters Outside Active Codepage

> [!bug]
> **Symptom:** during a scan covering directories with Cyrillic characters, the summary
> crashes with an encoding error, and **the entire process terminates with an error code before reaching
> change application**. The CSV report manages to save successfully, but no file
> is moved — despite console output looking like a hard scan crash.
>
> **Cause:** Windows console operates in a code page that does not support
> these characters.
>
> **Workaround:** before running, switch the console to UTF-8 using
> `chcp 65001` or set the environment variable `PYTHONIOENCODING` to `utf-8`.

This is also why everything the tool prints to the console is
English ASCII text — see
[technical/development/conventions-and-tests.md](../../technical/development/conventions-and-tests.md).

## Video Compression Limits

**Hardware encoders yield larger files for identical visual quality.** NVENC and
Quick Sync encode many times faster than CPU-based `libx265`/`libsvtav1`, but at
comparable quality (XPSNR), their output is usually larger — this is a known tradeoff
of these encoders, not a defect in this tool. For an archive encoded once and kept for
years, CPU often wins; the "Compression Suggestion" tab shows both axes (size and time)
simultaneously specifically so this decision is well-informed. Details:
[technical/components/compression-mechanism.md](../../technical/components/compression-mechanism.md).

**The quality threshold (XPSNR) applies identically on CPU and hardware.** Choosing
a hardware encoder does not relax criteria — a result below `min_xpsnr`
(or `min_xpsnr_relaxed` if relaxed quality consent is checked)
fails the task just as with `libx265`/`libsvtav1`, and the original remains untouched.

**Estimates are approximations until the first real benchmark on this machine.**
Values displayed for CPU profiles originate from measurements taken on a single
reference machine and file, until this specific computer encodes something itself — at which
point they are replaced with measured data. Hardware profiles have no initial
seed: until this computer benchmarks them itself (compression queue or the
"Measure" button), the tab displays a question mark, never a fabricated number.
The approximation mark disappears automatically; how this works:
[technical/components/compression-mechanism.md](../../technical/components/compression-mechanism.md).

**Laptop thermal throttling can distort measurements.** A measurement conducted on a
hot, throttled CPU or GPU yields lower throughput and thus longer estimated durations
than on a cool machine. Remedy: re-run the benchmark using the "Measure" button.

**GPU device selection is restricted to devices detected by `hwdetect.py`.**
Two cards from the same vendor (e.g. two NVIDIA GPUs) appear as separate items in the
"Quality" dropdown (displaying the card name) and calibrate independently; device selection
in the CLI (`--compress-device`) represents an index into this same list, not the OS adapter number.

**Encoding takes time.** A dozen trip videos can mean hours of CPU work,
one file at a time. The queue can be cancelled, and an interrupted run leaves no garbage
in the archive — only in the working directory under `.dedup`, which can be deleted entirely.

**A compressed file is not a duplicate of the original.** For duplicate detection,
these are two distinct files, and both will appear in reports until the original is moved
to the trash. Series detection does not cover video, so it will not link them either.

**The first launcher run downloads about 200 MB** (ffmpeg, SHA-256 verified); the binaries are not stored in git.
Rationale and consequences: [technical/components/compression-mechanism.md](../../technical/components/compression-mechanism.md#bundled-ffmpeg-binary).

## Smart App Control Blocks FFmpeg

> [!bug]
> **Symptom:** compression is unavailable, and the "Compression Suggestion" tab
> (or console with `--compress`) displays the message "ffmpeg/ffprobe
> blocked by Smart App Control (WinError 4551)" along with the executable path.
> No file is encoded or moved.
>
> **Cause:** Smart App Control blocks the execution of unsigned
> `ffmpeg.exe` and `ffprobe.exe` binaries located in `bin\ffmpeg`. The block is system-wide,
> aborting the entire operation with a single message rather than per-file.
>
> **How to confirm:** in Windows Event Viewer, under the
> `Microsoft-Windows-CodeIntegrity/Operational` log, look for Event IDs 3033 and 3077
> for this file path.
>
> **Workarounds:** disable Smart App Control (Windows Security → App & browser control
> → Smart App Control settings; note that once turned off, it cannot be re-enabled
> without reinstalling Windows), or point to an alternative, unblocked binary via the
> `compression.ffmpeg_path` key in `config.json`.
> `ffprobe` must reside in the same directory as the specified `ffmpeg`.

Once unblocked, simply refresh the web page or use **Rescan** — the server
checks FFmpeg availability anew, no restart needed. Path resolution chain for FFmpeg:
[technical/components/compression-mechanism.md](../../technical/components/compression-mechanism.md).

## Narrowing Scope Does Not Narrow Application

Specifying multiple directories at once and applying changes moves **all**
found duplicate groups, not just those tied to the directory intended as the source.

Example: specifying an upload folder alongside two archive directories as a reference
point will also move any duplicates found **between** the two reference directories,
even if they were intended only for comparison.

Inbox triage calculates this exact distinction, but **only plans** — there is no
mode combining inbox triage with direct change application.

## Hard Links on Volumes Lacking File Identifiers

Multiple paths pointing to a **single** file are identified by a tuple of identifiers:
volume ID and file ID. This tuple comes from standard OS metadata queries and is always
available on NTFS.

However, on certain volumes, the file identifier returns as zero — common on FAT
and some network shares. Windows provides an alternative path in that case: opening a file
handle and querying file information via that handle.
**This fallback is not implemented** — the fallback routine returns None.

The consequence is mild and one-way: on such volumes, hard links will not be collapsed
into a single candidate, meaning the same file appears in the report multiple times as a duplicate
group. This is a redundant proposal, not data loss — and it does not affect the archive
for which the tool was designed, which resides on NTFS.

## Aspects Not Verified on a Real Drive

The following operate in automated tests on an in-memory/mock filesystem, but have not
undergone runs on a real physical volume. The mock does not emulate certain Windows-specific behaviors — details in
[technical/architecture.md](../../technical/architecture.md).

- **Rollback and verification after an interrupted apply operation.** This is the sole
  safety net in the entire tool. The risk is considered low because the underlying move primitives
  are the same as those validated on real drives — but it is worth noting before relying on
  rollback in a critical situation.
- **Resuming a genuinely interrupted scan.**
- **A full run over the entire complete archive.** Verification runs so far covered
  small test sets.
- **HEIC files on real data** — 769 files and 18 conversion pairs have not yet undergone a live run.
- **Applying grouping suggestions from the browser UI**, including cards marked
  red (to `!REMOVE`). A first run should be tested on two non-critical files and
  rolled back immediately.

## Risks Requiring User Decisions

> [!warning]
> **The archive has no backup.** It occupies more than ten times the available
> free disk space, so a local copy cannot be made. Until the archive is backed up to
> external media, **the trash is the only safety net** — do not delete its contents beforehand.
> Proper sequence: backup first, free space afterwards.

> [!warning]
> **Deduplication only frees space once the trash is emptied.** The move operation itself
> takes place within a single volume and does not change the amount of free disk space.

> [!warning]
> **Camera clock correction is manual.** The tool warns that a given camera model has
> files with suspicious timestamps, but **does not calculate** the offset value — the user
> must determine and configure it.

> [!warning]
> **Empty directories in the inbox can vanish on their own.** This was observed on this
> specific drive; suspect is a background cloud sync client.
> The report reflects actual current state rather than assuming an empty directory survives until read.
