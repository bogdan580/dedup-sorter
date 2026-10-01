---
tags: [dedup, technical]
---

# Dependencies

Principle: **the absence of a library disables the feature, not the tool.** Exact
duplicate detection runs on the standard library alone. Import guards reside
within the modules that require them, rather than in a single central place.

Versions are pinned. Installation: [../business/instructions/installation.md](../business/instructions/installation.md).

## Runtime Dependencies

| Package | Purpose | Without it |
|---|---|---|
| `Pillow` | image decoding: perceptual hash, sharpness, EXIF, thumbnails | image-based modes do not work |
| `numpy` | perceptual hash and sharpness measurement | same as above |
| `pillow-heif` | decoding `.HEIC` and `.HEIF` | HEIC files are invisible to series, events, and thumbnails; they work normally in byte-level dedup |
| `imageio-ffmpeg` | single frame from video for thumbnail | videos get a placeholder tile, rest works |
| `reverse-geocoder` | offline place names | events are created without geographic names, based on date alone |

`pillow-heif` hooks into the image decoder via a one-time registration upon
importing the thumbnail module. Thanks to this, other modules open HEIC files
with a standard call, without any changes on their side.

Registration is guarded just like the image decoder itself — a package may be
installed, yet its native library may fail to start. Declaring extension support
is separated from the **actual availability** of the codec; the same pattern
applies to video.

`imageio-ffmpeg` ships its own binary, so it does not require installing
`ffmpeg` in the system or administrator privileges. It is launched with an
argument list, without a shell, and with a strict timeout. It handles **only
thumbnails** — video compression uses a different binary and does not touch this one.

## Video Compression Is Not a pip Dependency

ffmpeg and `ffprobe` used for compression are a **pinned build** under `bin\ffmpeg\`.
They are not in git: the launchers (`run.cmd`, `run.ps1`, `run.sh`) download them
once via `tools\fetch-ffmpeg.ps1` and verify their SHA-256 against
`bin\ffmpeg\VERSION.txt`. The rationale, license, fallback and update instructions:
[Bundled ffmpeg Binary](components/compression-mechanism.md#bundled-ffmpeg-binary).

The rule "absence of a dependency disables the feature" still holds: if
`bin\ffmpeg\` was never fetched and ffmpeg is not in the system PATH, the compression
tab reports feature unavailability, while the rest of the tool functions unchanged.

`reverse-geocoder` carries geographical data within the package itself — it works
offline, without downloading anything upon first use. It pulls `scipy` as a
transitive dependency.

## Deliberately Omitted Dependency

**RAW decoding library.** There are 16 RAW and editing project files out of over 17,000
in the archive. The dependency and its weight are unjustified at this scale, so
these formats are excluded from image-based modes. This is a deliberate boundary,
not a gap to be filled — see
[../business/rules/duplicates.md](../business/rules/duplicates.md).

## Development Dependencies

The runtime dependencies file is **deliberately** free of test tools — it is a
set for someone who only wants to run the tool. A development environment
installs a separate file that includes the runtime requirements and adds the
test runner.

The test suite uses no test runner plugins: it imports only the standard library,
project modules, and the test framework itself.


### From code: `tidy/media/imaging.py`
```python
    DecompressionBombError is a plain Exception subclass (not OSError),
    raised whenever a source image exceeds Pillow's MAX_IMAGE_PIXELS
    guard, so it must be listed explicitly alongside OSError.
```
