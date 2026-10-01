---
tags: [dedup, technical]
---

# Video Compression Mechanics

Domain rules — what qualifies, what the thresholds are, when an original may be
discarded — reside in [../business/rules/compression.md](../../business/rules/compression.md).
Here is how they are enforced.

## Module Breakdown

| Module | Role |
|---|---|
| `tidy/compression/probe.py` | reading stream parameters via `ffprobe`, with cache |
| `tidy/compression/compress.py` | qualification heuristics, estimation model, `compression` section from `config.json` — pure logic |
| `tidy/compression/hwdetect.py` | ffmpeg binary resolution, detection of actually working encoders, machine identifier |
| `tidy/compression/encoder.py` | constructing ffmpeg command, encoding to staging, verifying result |
| `tidy/moves/applier/compress.py` | `op=compress` and moving result into archive, separate batch for removing originals |

Separating `compress.py` (pure) from `probe.py` and `encoder.py` (I/O) is a goal
in itself: all decision logic can be tested in memory, adhering to the seam
described in [filesystem-seam.md](../architecture.md).

## Pipeline: Staging, Then Seam

```
encoder    -- ffmpeg -->    .dedup\.compress\<n>.mp4     staging directory, disk
applier    -- file move ->  <original>_compressed.<ext>  archive, via seam
```

This division is mandated by three rules at once:

- **The `tidy/moves/applier` package remains the sole executor of file moves.** ffmpeg
  is an external process — it cannot be fed a stream from a filesystem mock, so
  it writes to the real disk. It writes strictly **under `.dedup`**, under the
  exact same exception as the thumbnails directory.
- **Movement via the seam never overwrites.** A name collision results in a
  `__dup2` suffix, not replacement of an existing file.
- **Interrupted encoding leaves artifacts in staging, never in the archive.** The
  `.dedup\.compress\` directory can be deleted entirely.

`encoder.py` is not subject to the static check forbidding real disk calls — by
definition it runs a process that writes to disk. It is, however, covered by a
test verifying that every output path lies within the staging directory.

## Parameter Reading and Cache

Read parameters include codec, dimensions, average framerate, bitrate, duration,
presence and codec of audio tracks, and rotation from metadata. From these
values, `bpp` is calculated, which underpins the entire heuristic.

Results land in the `compress_probes` table of the cache database, keyed by
path, size, and modification time — reopening the tab does not run `ffprobe`
anew. Tables: [persistence.md](manifest-and-db.md).

## Estimation Model

Two independent axes: **size depends on profile, time depends on machine.**

```
size = profile_bpp * width * height * framerate * duration / 8
time = (width * height * framerate * duration) / throughput
```

Throughput is measured in megapixels per second, not real-time multipliers — only
then does it transfer across resolutions.

Size estimation is **clamped to 90% of the original**: the model does not know
that a codec cannot invent bits absent from the source, so for low input `bpp`
material it would otherwise overestimate.

### Starting Values and Calibration

The tool starts with values measured on a single machine and a single file —
these are **seeds**, not measurements of this computer. As long as this is the
case, the UI prefixes every number with an approximation sign, and for a
profile without any baseline it displays a question mark.

Seeds (`config.SEED_CALIBRATION`) come from a single file — a Samsung S22
recording, 1080p60 HEVC, 25.76 s, 172.10 MB, 0.448 `bpp` — encoded on an
i9-12900HK:

| Profile | Result `bpp` | Throughput | XPSNR-Y |
|---|---|---|---|
| `libx265` CRF 21 `slow` | 0.101 | 20.9 Mpx/s | 38.90 dB |
| `libsvtav1` CRF 30 `p5` | 0.075 | 14.6 Mpx/s | 38.28 dB |

Hardware encoders have no seeds: they have not been measured and must not be
guessed.

> [!important]
> **No fabricated number is ever shown as a measurement.** Missing data yields a
> question mark, not a plausible-looking value.

Calibration happens **automatically, from real work**: every completed and
transferred job records its measured `bpp` and throughput into the
`compress_calibration` table, keyed by **machine identifier** and encoder name.
The next estimate for this encoder on this computer uses the measurement, and
the approximation sign disappears. The key includes machine because the same
file may be processed on two computers and results must not overwrite each
other.

Calibration is keyed by **encoder**, and HEVC quality profiles share a single
encoder with different target `bpp`s. Therefore, saving divides the measured
value by the profile multiplier, and reading multiplies it back — a measurement
from any profile normalizes to a common base instead of skewing the estimates of
the others.

## ffmpeg Command

Four command elements **are not optional**; each fixes a specific, observed defect:

| Element | Purpose |
|---|---|
| disabling autorotation plus explicit rotation in metadata | without this, ffmpeg bakes rotation into pixels, turns 1920x1080 into 1080x1920, and drops rotation matrix — file plays correctly, but no longer matches source structure |
| framerate `passthrough` mode | phone sources have variable framerate; without this, AV1 enforces constant framerate and inserts duplicate frames, drifting frame numbering against the source |
| `hvc1` compatibility tag | without it, Windows Photos, QuickTime, and TVs will not play HEVC in an MP4 container |
| 10-bit pixel format | yields a smaller file at the same quality and eliminates color banding on gradients |

The audio track is **copied without re-encoding**; an exception is a PCM source,
where copying would mean carrying uncompressed audio into a compressed file.
Metadata — creation timestamp, GPS, manufacturer tags — are preserved in full.

**ffmpeg does not preserve file modification time**, so it is set separately on
the output file. The entire archive timeline — events, date index — relies on
this value.

## Result Verification

Checks run in order of increasing cost; the first failure aborts evaluation, so
expensive quality measurement never runs after a cheaper failure:

1. file exists and is non-empty,
2. readable by `ffprobe`,
3. duration matches source within 0.5 s tolerance,
4. frame count identical to source,
5. luminance XPSNR measurement above threshold.

Threshold and permission to lower it are domain rules —
[../business/rules/compression.md](../../business/rules/compression.md).

### Quality Measurement Pitfalls

The measurement procedure contains five details, each of which has previously
distorted results:

- **comparison in 8-bit, not 10-bit.** Converting both sides to 10-bit with
  mixed sources yielded 12–24 dB instead of 39–43 dB — appearing as a quality
  disaster when it was not;
- **log level must pass info messages**, because the filter prints its summary
  at that exact level; at "errors only", nothing is captured;
- **frames are paired by sequence, not timestamps.** Both streams are
  renumbered (`settb` + `setpts=N`) and compared frame-for-frame. Previously
  resampling both sides to constant framerate with the `fps` filter — even
  nominal — paired mismatched frames on variable framerate phone videos:
  measurement yielded 22–32 dB where actual quality was 37–41 dB. Sequential
  pairing is correct because encoding passes frames unchanged (`passthrough`),
  and step 4 confirms an identical frame count;
- **comparison ends on the shorter stream** (`shortest=1`). The calibration
  snippet is shorter than the source; without this option, its last frame was
  compared against all remaining source frames, dropping measurement to ~11 dB;
- **snippets are cut using input time limits**, never the `trim` filter — which
  broke frame synchronization.

ffmpeg diagnostic stream is decoded explicitly as UTF-8. Without this, paths
containing characters outside the console codepage — such as the archive root
itself — crashed the reader thread silently, and quality measurement failed
regardless of how good the result was.

## Hardware: Detection, Devices, Commands

`hwdetect.py` checks four hardware encoders (`hevc_nvenc`, `av1_nvenc`,
`hevc_qsv`, `av1_qsv`) separately from the two CPU ones. Mere presence on the
`ffmpeg -encoders` list **says nothing about hardware** — static GPL builds bundle
all encoders regardless of the machine — so the only proof is an actual attempt
to encode a single frame. The size of this test frame is **256×256**, not
smaller: at 64×64, `hevc_qsv` fails with an initialization error (exit -40) even
though it functions properly at standard resolutions — verified on Intel UHD
Graphics 770.

### Device Enumeration

- **NVENC**: `-gpu list` prints one line per CUDA device (`[ GPU #0 - <name> has
  Compute SM x.y ]`, verified directly in `nvenc.c` source). Each discovered card
  gets its **own** initialization test with `-gpu <n>` — presence on the CUDA
  list does not prove that a specific encoder (e.g., `av1_nvenc` on an older card)
  works on it. If the list cannot be parsed and device `0` passes the test on its
  own, a single generic entry ("GPU 0") is retained.
- **Quick Sync**: one device — default adapter. Card name comes from querying
  `Win32_VideoController` via PowerShell (offline, with timeout); absence of name
  does not block detection, the encoder still receives an entry keyed by encoder
  name alone, like CPU.

### `device_key`: Calibration Identity

Every detected encoder has a `device_key`: `"<encoder>"` for CPU (e.g.,
`libx265`), `"<encoder>@<device name>"` for hardware (e.g., `hevc_nvenc@NVIDIA
GeForce RTX 4060`). This is the sole key under which performance measurements
are stored and read — two cards from the same vendor in one machine calibrate
separately, and the same encoder across two user computers never collides (the
second axis is the machine identifier, see below).

> [!important]
> The hardware detection result **is not saved to disk** — it lives only in
> process memory (`ReviewState.hwdetect_cache`). A persisted result after a driver
> update would offer an encoder that no longer works; rechecking takes only a few
> seconds once per server launch, so there is no reason to persist it.

### ffmpeg Commands for Hardware

The same invariants as for `libx265`/`libsvtav1` (autorotation disabled,
`-fps_mode passthrough`, 10-bit pixel format, `-tag:v hvc1` for HEVC, `-c:s
copy` only for HEVC) apply without exception. Differences:

| | NVENC | Quick Sync |
|---|---|---|
| Rate control / quality | `-rc vbr -cq <n> -b:v 0` | `-global_quality <n>` |
| Preset | `-preset p7` (slowest/best) | `-preset veryslow` |
| 10-bit pixel format | `p010le` | `p010le` |
| Device selection | `-gpu <index>` | none — one default adapter (sufficient) |

`p010le`, not `yuv420p10le` as with `libx265`: `ffmpeg -h encoder=hevc_nvenc` (and
similarly `hevc_qsv`) on the bundled binary lists `p010le` among supported pixel
formats, not planar `yuv420p10le` — verified directly on the binary, not
guessed.

### Per-Device Calibration

Calibration occurs through two paths:

1. **Automatically, from real work** — as described above, now keyed by
   `device_key` instead of encoder name alone. Rows saved before `device_key` was
   introduced remain valid without migration: CPU `device_key` **is** its
   encoder name, so `libx265`/`libsvtav1` read as before.
2. **On demand, via "Calibrate" button** (`POST /api/compress/calibrate`) —
   encodes an ~6-second snippet of the specified file (input limit `-t 6`, never
   `trim` filter — which broke frame sync during quality measurement, see
   below) on each requested device, measures `bpp`, `Mpx/s`, and XPSNR-Y relative
   to the same source snippet, writes to database, and **immediately deletes the
   staging file** — nothing enters the archive, no manifest is produced. Runs in
   the same single-slot queue as `POST /api/apply-compress`: compression and
   calibration never run simultaneously, as both are driven by the machine's
   single real encoder.

The "Quality" column in the estimates table shows the XPSNR-Y difference
relative to `libx265` on the **same machine** — only when both measurements
exist; otherwise "?", never an unmeasured number. Database schema:
[persistence.md](manifest-and-db.md).

## Encoding Queue

Encoding is a multi-minute operation, so in the review UI it has its own queue
with progress, cancellation, and **one job at a time** — the encoder saturates
all cores anyway, and a second parallel stream accelerates nothing.

Each file undergoes its **own complete cycle**: encoding, verification, moving to
the archive, and when original removal is requested — its own move to trash —
**immediately**, rather than batched at queue completion. The reason is
observable: with batched moves, the first video in a long queue did not appear
in its directory until all remaining jobs finished.

The entire queue lands in **one** `!MOVES\<start>\` batch and (when removing
originals) **one** `!REMOVE\<start>\` batch, named after queue launch time. Each
file appends its `planned`/`done` rows upon completion. One launch represents one
user decision, hence one pair of batches in the Restore tab, rather than two per
video. The batch folder is created on first needed move: a queue where everything
fails leaves no empty batches. Encoding or verification errors do not append to
the manifest; a failed result move stays in the `!MOVES` manifest as `failed`,
and the original remains in place.

Original removal is built **from the move result of the same job**, never from
the task list — there is no code path where a failed encode could reach trash.
The rule governing this is in
[../business/rules/compression.md](../../business/rules/compression.md).

## Launch Blocking (`ToolBlockedError`)

When the system refuses to run `ffmpeg`/`ffprobe` (`OSError` with `winerror`
4551, i.e., Smart App Control), `probe.py` raises `ToolBlockedError` instead of
returning `None` for the file. The block affects the whole machine, so it halts
the operation: `hwdetect.detect()`, probing, encoding, and verification propagate
the exception, and the CLI and review server translate it into a single clean
message. The server does not cache blocked detection, so unblocking takes effect
without restart. Symptoms and workarounds:
[limitations.md](../../business/instructions/limitations.md).

## ffmpeg Binary

Not searched in the system — a pinned build lives in `bin\ffmpeg\`, fetched by
the launchers. Resolution chain, build, and license:
[Bundled ffmpeg Binary](#bundled-ffmpeg-binary).

## Bundled ffmpeg Binary

Video compression does not search for ffmpeg in the system. It uses one
**pinned build** under `bin\ffmpeg\`, alongside `ffprobe`, the license file,
and the build description.

```
bin\ffmpeg\ffmpeg.exe       not in git -- fetched by tools\fetch-ffmpeg.ps1
bin\ffmpeg\ffprobe.exe      not in git -- fetched by tools\fetch-ffmpeg.ps1
bin\ffmpeg\VERSION.txt      in git: version, pinned URL, SHA-256 sums
bin\ffmpeg\LICENSE          in git: GPL v3 and component library licenses
bin\ffmpeg\INSTALLED.txt    not in git -- written only by the "latest" fallback
```

### Why a Pinned Build

**Identical results on every computer.** The same archive file encoded on a
laptop and a desktop goes through the exact same binary and libraries, so result
differences depend on the selected profile, not whatever happens to be installed.
Otherwise calibration would compare two arbitrary builds.

**Complete codec set.** The binary bundled by the thumbnail library lacks an
AV1 encoder, and system installations vary widely. The pinned build has
confirmed `libx265`, `libsvtav1`, and hardware HEVC and AV1 variants for NVENC
and Quick Sync.

### Why Fetched, Not Versioned

The two binaries are ~160 MB each. GitHub rejects files over 100 MB, and keeping
them in history would make every clone several hundred megabytes. They are
therefore gitignored and fetched once by the launchers (`run.cmd`, `run.ps1`,
`run.sh`) through `tools\fetch-ffmpeg.ps1`:

1. If both `.exe` files exist and their SHA-256 matches `VERSION.txt` (or
   `INSTALLED.txt`, see step 3), nothing happens — no network access.
2. Otherwise it downloads the **pinned URL** from `VERSION.txt`, verifies the
   archive's SHA-256, extracts it, verifies both binaries' SHA-256 and places them
   in `bin\ffmpeg\`.
3. If the pinned build is gone (BtbN prunes daily builds; month-end builds are
   kept long-term, which is why a month-end one is pinned) or does not match, it
   falls back to BtbN's **latest** build. That one cannot be checked by hash, so
   it must pass a functional check instead: `ffmpeg -encoders` lists `libx265`
   and `libsvtav1`, and `ffprobe` runs. Its hashes are recorded in
   `INSTALLED.txt`, so later launches accept it without a download.
4. If nothing can be fetched (offline, file in use), the launcher warns and
   **still starts the review UI** — a missing ffmpeg disables only compression,
   never the tool.

Downloads are accepted only from `https://github.com/BtbN/FFmpeg-Builds/releases/download/`.
This is the project's **only network access**, and it lives in the launcher, not
in `dedup.py` — [guarantees.md](../../business/guarantees.md). Running
`python dedup.py` directly never downloads anything; without a fetched build,
compression falls back to `ffmpeg` on PATH or reports itself unavailable.

### Build Information

Static `win64-gpl` from the public BtbN build repository. Static, making the
directory self-contained without pulling external DLLs. GPL, because `libx265`
is GPL-licensed and LGPL builds omit it.

Exact version, release, download URL, and checksums live in `VERSION.txt` — not
here, so updates only require editing one place. `tools\fetch-ffmpeg.ps1` reads
its `Pinned URL`, `Pinned zip SHA-256`, `Fallback URL` fields and the two binary
sums.

### License

The project code is MIT. ffmpeg is not distributed with the repository; the
launcher downloads the GPL build from its publisher and runs it as a separate
process. `LICENSE` in `bin\ffmpeg\` is the build's own license, and the
corresponding source is linked from `VERSION.txt`.

### Path Resolution

In order:

1. `ffmpeg_path` from `compression` section in `config.json` — manual override,
2. `bin\ffmpeg\ffmpeg.exe` fetched by the launcher — **default path**,
3. `ffmpeg` in system PATH — fallback when `bin\ffmpeg\` was not fetched.

`ffprobe` is taken from the same directory as the resolved `ffmpeg`.

The library providing ffmpeg for thumbnails is **not** part of this chain and
remains where it was — with thumbnails. Comparison:
[dependencies.md](../dependencies.md).

### Encoder List Is Not Proof

> [!warning]
> The static build contains **all** encoders regardless of the machine, so mere
> presence on the list proves nothing about hardware. Quick Sync AV1 appears on
> GPUs having an AV1 decoder but no encoder.

Hence hardware encoders are considered available only after an **initialization
attempt**: encoding one frame from a test pattern generator. The detection result
is remembered in-process, as querying hardware on every tab open costs several
seconds.

### Updates

Pick a newer **month-end** BtbN autobuild release, then rewrite `Pinned URL`,
`Pinned zip SHA-256` and both binary sums in `VERSION.txt`, replace `LICENSE`
with the archive's `LICENSE.txt`, run the full test suite (the compression tests
encode with the real binary), and commit that alone. On the next launch every
machine sees the hash mismatch and fetches the new build.


### From code: `tidy/compression/compress.py`
```python
---------------------------------------------------------------------------
Heuristic: should this file be compressed?
---------------------------------------------------------------------------
```


### From code: `tidy/compression/compress.py`
```python
---------------------------------------------------------------------------
Size and time estimation
---------------------------------------------------------------------------
```


### From code: `tidy/compression/compress.py`
```python
    XPSNR-Y difference vs. libx265's own calibrated measurement, on the
    SAME machine -- only when both are actually measured (never seeded);
    None otherwise, per docs/technical/components/compression-mechanism.md's own "never an unmeasured
    number" rule. The review UI's "Jakosc" column in the estimates table
    shows "odniesienie" for libx265 itself and "?" when this is None.
```


### From code: `tidy/compression/compress.py`
```python
    Reference point for the "Jakosc" column (E4.6): libx265's own
    measured XPSNR on THIS machine, or None until it has one -- a seed
    value is never used as the baseline either, same "never an
    unmeasured number" rule as everywhere else in this module.
```


### From code: `tidy/compression/compress.py`
```python
        config.PROFILE_BPP_SEED_MULTIPLIER differentiates same-encoder
        profiles (x265/x265_medium/x265_high all resolve to "libx265"
        here) so they don't collapse to one identical estimate -- see
        that dict's own comment for where the factors come from.
```


### From code: `tidy/compression/compress.py`
```python
---------------------------------------------------------------------------
config.json's "compression" section
---------------------------------------------------------------------------
```


### From code: `tidy/compression/compress.py`
```python
    Same rationale as keeper._warn_bad_config(): config.json is meant to
    be hand-edited, so every fallback prints instead of silently
    discarding the user's edit.
```


### From code: `tidy/compression/encoder.py`
```python
        p010le, not yuv420p10le: `ffmpeg -h encoder=hevc_nvenc` lists
        p010le, not yuv420p10le, among supported pixel formats (verified
        against the bundled binary, docs/technical/components/compression-mechanism.md). "-b:v 0" hands rate
        control fully to -cq under -rc vbr, per NVENC's own convention.
```


### From code: `tidy/compression/encoder.py`
```python
        preset veryslow is QSV's own best-quality preset (verified against
        `-h encoder=hevc_qsv`, docs/technical/components/compression-mechanism.md); no
        -qsv_device here -- hwdetect.py's QSV EncoderInfo.device is
        always "" (one default adapter), so `device` is accepted for
        signature uniformity with the NVENC branches but unused.
```


### From code: `tidy/compression/encoder.py`
```python
        ffmpeg's own output is always UTF-8 (ffmpeg's own convention),
        regardless of the console's codepage -- without this, decoding
        falls back to locale.getpreferredencoding() and _drain_stderr()'s
        reader thread dies silently (an unhandled UnicodeDecodeError in a
        background thread, per probe.py's own fix for the same cause) the
        moment ffmpeg echoes back a path outside cp1250, such as this
        archive's own root (`C:\Фото архів`) -- losing the stderr this
        function's own failure `reason` is built from.
```


### From code: `tidy/compression/encoder.py`
```python
            Neither cancelled nor timed out yet: cancel_event is being
            polled, keep looping. Without cancel_event this branch is
            unreachable -- wait_slice was `timeout` itself, so
            TimeoutExpired here already means the deadline passed.
```


### From code: `tidy/compression/encoder.py`
```python
            Same fix as probe.py's _run_ffprobe() and run_encode()'s own
            Popen above: ffmpeg's output is UTF-8 regardless of the
            console codepage.
```


### From code: `tidy/compression/encoder.py`
```python
            Same fix as probe.py's _run_ffprobe(): ffmpeg's stderr is
            UTF-8 regardless of the console codepage. Without this, a
            source path outside cp1250 (this archive's own root,
            `C:\Фото архів`) raised UnicodeDecodeError while decoding
            ffmpeg's stderr -- silently in _readerthread, leaving
            result.stderr None, so the regex below never matched and
            every XPSNR measurement on this archive failed with
            "XPSNR measurement failed" regardless of actual quality.
```


### From code: `tidy/compression/hwdetect.py`
```python
QSV/NVENC reject a 64x64 init-test frame (observed: hevc_qsv exits -40 on
an integrated Intel GPU) even though the encoder works fine at real sizes --
256x256 is the smallest size confirmed to pass on both encoder families, so
the init test below must never shrink under it again.
```


### From code: `tidy/compression/hwdetect.py`
```python
nvenc.c's nvenc_check_device(), loglevel AV_LOG_INFO when listing:
av_log(..., "[ GPU #%d - < %s > has Compute SM %d.%d ]\n", idx, name, ...)
-- one line per CUDA device, printed by `-gpu list` before ffmpeg exits
with AVERROR_EXIT. Verified against ffmpeg master source;
no NVIDIA GPU was available to confirm the real line at runtime.
```


### From code: `tidy/compression/hwdetect.py`
```python
Fallback label when `-gpu list` output cannot be parsed but device 0
still passes its own init test -- a real, working device with an unknown
name is still reported, never dropped (docs/technical/components/compression-mechanism.md).
```


### From code: `tidy/compression/hwdetect.py`
```python
    `-gpu list` makes ffmpeg exit with an error after printing the list
    (nvenc.c returns AVERROR_EXIT) -- the list is on stderr regardless of
    returncode, so it is read unconditionally here.
```


### From code: `tidy/compression/hwdetect.py`
```python
    `-gpu list` didn't parse -- still try device 0 alone before giving up
    (docs/technical/components/compression-mechanism.md: unparseable list + working device 0 => one generic
    entry, never silently dropped).
```


### From code: `tidy/compression/probe.py`
```python
            ffprobe's JSON is always UTF-8 (ffmpeg's own convention),
            regardless of the console's codepage -- without this, decoding
            falls back to locale.getpreferredencoding(), which raises
            UnicodeDecodeError on a non-UTF-8 codepage (e.g. Windows
            cp1250) the moment the JSON echoes back a `path` containing
            any character outside that codepage, such as this archive's
            own root (`C:\Фото архів`).
```
