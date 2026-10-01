"""Thumbnail cache.

Thumbnails are generated once into `.dedup\\.thumbs\\<id>.jpg` and served
read-only afterwards by tidy/review. Source bytes are read through the
FileSystem seam (`fs.open_read`), exactly like hashing.py, so generation is
unit-testable against FakeFileSystem; the thumbnail cache directory itself
is real disk, written with plain `open()`/`os.makedirs()` -- mirroring
cache.py's cache.sqlite3, both are tool-internal bookkeeping under
`.dedup`, never archive content, so neither goes through the FileSystem
seam for writes.

Caller supplies the thumbnail id (tidy/review/views/duplicates.py uses a duplicate
group's existing `full_hash`) -- every path in an exact-duplicate group is
byte-identical by definition, so one thumbnail genuinely serves the whole
group; no extra hashing work needed here.
"""
# Behaviour: docs/technical/components/review-ui.md
from __future__ import annotations

import functools
import os
import subprocess
import tempfile
from typing import Optional, Tuple

from tidy.fs.fsmodel import FileSystem

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
    _PILLOW_AVAILABLE = True
# See: docs/technical/components/review-ui.md
    _DECODE_ERRORS: Tuple[type, ...] = (
        OSError, UnidentifiedImageError, ValueError, MemoryError, Image.DecompressionBombError,
    )
except ImportError:  # pragma: no cover - Pillow is a stated requirement
    _PILLOW_AVAILABLE = False
    _DECODE_ERRORS = (OSError, ValueError, MemoryError)

# See: docs/technical/components/review-ui.md
if _PILLOW_AVAILABLE:
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        _HEIF_AVAILABLE = True
    except (ImportError, OSError, RuntimeError):  # pragma: no cover - pillow-heif optional-at-import-time guard
# See: docs/technical/components/review-ui.md
        _HEIF_AVAILABLE = False
else:  # pragma: no cover - Pillow itself already unavailable
    _HEIF_AVAILABLE = False

THUMB_MAX_DIM = 640

# See: docs/technical/components/review-ui.md
_HEIF_EXTENSIONS_CASEFOLD = frozenset({".heic", ".heif"})
_IMAGE_EXTENSIONS_CASEFOLD = frozenset({".jpg", ".jpeg", ".png"}) | _HEIF_EXTENSIONS_CASEFOLD

_VIDEO_EXTENSIONS_CASEFOLD = frozenset({".mp4", ".mov", ".3gp", ".wmv", ".mpg", ".avi"})

# See: docs/technical/components/review-ui.md
_FFMPEG_TIMEOUT_SECONDS = 15

_LONG_PATH_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def kind(path: str) -> Optional[str]:
    """Classify `path` as `"image"`, `"video"`, or `None` (unsupported/
    unknown), based on extension ONLY -- unlike `supports()`, this never
    depends on which codecs/binaries happen to be installed, so it is safe
    to call from view-model code (tidy/review/views) that wants to show a
    video-specific placeholder even when no thumbnail was actually
    generated (e.g. ffmpeg unavailable)."""
    ext = os.path.splitext(path)[1].casefold()
    if ext in _IMAGE_EXTENSIONS_CASEFOLD:
        return "image"
    if ext in _VIDEO_EXTENSIONS_CASEFOLD:
        return "video"
    return None


@functools.lru_cache(maxsize=1)
def _ffmpeg_exe() -> Optional[str]:
    """Resolve (and memoize) the path to the ffmpeg binary bundled by
    imageio_ffmpeg. Cached after the first call -- successful OR failed --
    so repeated `supports()`/`ensure()` calls for a batch of videos don't
    re-import imageio_ffmpeg or re-extract its bundled binary each time.
    Tests simulate an unavailable ffmpeg by monkeypatching this function
    directly (bypassing the cache), not by clearing it."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, OSError, RuntimeError):
        return None


def _strip_extended_prefix(path: str) -> str:
    """Strip a Windows `\\\\?\\` (or `\\\\?\\UNC\\`) extended-length prefix,
    if present, before handing a path to the ffmpeg subprocess.

    This build of ffmpeg actually accepts `\\\\?\\`-prefixed paths directly
    -- including ones with Cyrillic characters and spaces -- so this isn't
    working around an observed failure. It's kept as defense in depth
    anyway: ensure() may receive already-prefixed paths from real callers
    (platform_win.py's WindowsFileSystem hands out prefixed paths from
    scandir()), and passing the plain, un-prefixed form to an external
    process is the more conservative choice across ffmpeg builds/versions
    this codebase hasn't tested against."""
    if path.startswith(_UNC_PREFIX):
        return "\\\\" + path[len(_UNC_PREFIX):]
    if path.startswith(_LONG_PATH_PREFIX):
        return path[len(_LONG_PATH_PREFIX):]
    return path


class _VideoFrameError(Exception):
    """Raised internally when ffmpeg can't produce a usable frame for a
    video source -- caught inside ThumbnailStore.ensure() alongside Pillow's
    own decode errors, never allowed to propagate out of ensure()."""


def _run_ffmpeg_frame(exe: str, src: str, seek: str, frame_path: str) -> bool:
    """Run one ffmpeg attempt seeking to `seek` seconds into `src`, writing
    a single frame to `frame_path`. Returns True only if ffmpeg exited 0
    AND actually produced a non-empty file -- ffmpeg can occasionally exit
    0 without writing anything for a stream with no frame at that offset.
    Deliberately not `check=True`: the caller inspects `returncode` itself
    so only genuinely exceptional conditions (timeout, missing binary, OS-
    level launch failure) need except clauses here, not the ordinary
    "video too short for this seek offset" case."""
    cmd = [
        exe, "-nostdin", "-loglevel", "error", "-y",
        "-ss", seek, "-protocol_whitelist", "file,pipe", "-i", src, "-frames:v", "1", "-an", "-f", "image2", frame_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        return False
    if result.returncode != 0:
        return False
    return os.path.isfile(frame_path) and os.path.getsize(frame_path) > 0


def _extract_video_frame(source_path: str) -> Optional[str]:
    """Extract a single representative frame from the video at
    `source_path` into a fresh temp JPEG, returning its path -- or None on
    any failure. Tries 1s into the clip first (skips a possible black/blank
    opening frame), then retries at 0s for very short clips where 1s is
    past the end. The caller is responsible for removing the returned file
    once done with it."""
    exe = _ffmpeg_exe()
    if exe is None:
        return None
    stripped_src = _strip_extended_prefix(source_path)

    fd, frame_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    os.remove(frame_path)  # ffmpeg must create it fresh; an empty file from
    # mkstemp would otherwise make the size check below meaningless if
    # ffmpeg fails silently without touching it.

    for seek in ("1", "0"):
        if _run_ffmpeg_frame(exe, stripped_src, seek, frame_path):
            return frame_path

    if os.path.exists(frame_path):
        os.remove(frame_path)
    return None


def supports(path: str) -> bool:
    """True if `path`'s extension is one thumbnails.py can actually decode
    with the codecs/binaries installed right now: Pillow for images
    (additionally pillow-heif for HEIC/HEIF specifically); Pillow AND a
    resolvable ffmpeg binary for videos."""
    file_kind = kind(path)
    if file_kind == "image":
        if not _PILLOW_AVAILABLE:
            return False
        ext = os.path.splitext(path)[1].casefold()
        if ext in _HEIF_EXTENSIONS_CASEFOLD:
            return _HEIF_AVAILABLE
        return True
    if file_kind == "video":
        return _PILLOW_AVAILABLE and _ffmpeg_exe() is not None
    return False


class ThumbnailStore:
    """Generates and caches thumbnails under `thumbs_dir`, keyed by a
    caller-supplied `thumb_id` (opaque to this class -- never derived from
    filesystem paths, so it's safe to also use as a lookup key against
    untrusted request input, see tidy/review)."""

    def __init__(self, fs: FileSystem, thumbs_dir: str) -> None:
        self._fs = fs
        self._dir = thumbs_dir
        os.makedirs(self._dir, exist_ok=True)

    def path_for_id(self, thumb_id: str) -> str:
        return os.path.join(self._dir, f"{thumb_id}.jpg")

    def ensure(self, source_path: str, thumb_id: str) -> Optional[str]:
        """Return the on-disk thumbnail path for `thumb_id`, generating it
        from `source_path` on first request. Returns None -- never raises
        -- if the source can't be decoded (unsupported format, corrupt
        file, read error): a single bad photo must not break the whole
        review screen, only fall back to a placeholder there.

        Every duplicate group shares one thumb_id (see
        tidy/review/views/duplicates.py's build_group_views()), so concurrent requests for
        different cards in the same group
        (ThreadingHTTPServer runs one thread per request) can legitimately
        call ensure() with the *same* thumb_id before either has finished.
        A fixed, shared ".tmp" name would let two such threads race on the
        same temp file; tempfile.mkstemp() below gives each call its own
        collision-free temp file instead, so the only shared operation is
        the final os.replace(), which is atomic on both POSIX and Windows
        and never raises just because the destination already exists."""
        out_path = self.path_for_id(thumb_id)
        if os.path.exists(out_path):
            return out_path
        file_kind = kind(source_path)
        if file_kind is None or not supports(source_path):
            return None

        tmp_fd, tmp_path = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        os.close(tmp_fd)  # mkstemp is used only for a unique filename here
        try:
            if file_kind == "image":
                self._write_image_thumbnail(source_path, tmp_path)
            else:  # "video"
                self._write_video_thumbnail(source_path, tmp_path)
        except (*_DECODE_ERRORS, _VideoFrameError):
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return None

        try:
            os.replace(tmp_path, out_path)
        except OSError:
# See: docs/technical/components/review-ui.md
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if not os.path.exists(out_path):
                return None

        return out_path

    def rotate(self, thumb_id: str, quarter_turns: int = 1) -> bool:
        """Rotate the already-cached thumbnail for `thumb_id` clockwise by
        `quarter_turns * 90` degrees, overwriting it in place. Returns
        False (no-op) if no cached thumbnail exists yet -- the caller
        (tidy/review/handler/routes_dup.py's rotate endpoint) always calls ensure() first,
        so this only has to handle "never generated"/"source undecodable"
        defensively, not race to generate one itself.

        Manual override for photos whose EXIF
        orientation is missing or simply wrong -- exif_transpose() in
        ensure() already fixes the common case; this handles what's left.
        Only ever touches the cached .jpg under thumbs_dir, never the
        original archive file at the source path -- the review UI's "nothing
        is lost without consent" guarantee only applies to archive content, but
        there's no reason to risk it here either."""
        out_path = self.path_for_id(thumb_id)
        if not os.path.exists(out_path):
            return False
# See: docs/technical/components/review-ui.md
        degrees = (-90 * quarter_turns) % 360
        tmp_path = None
        try:
            with Image.open(out_path) as image:
                rotated = image.convert("RGB").rotate(degrees, expand=True)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
            os.close(tmp_fd)
            rotated.save(tmp_path, "JPEG", quality=85)
            os.replace(tmp_path, out_path)
        except _DECODE_ERRORS:
            if tmp_path is not None and os.path.exists(tmp_path):
                os.remove(tmp_path)
            return False
        return True

    def _write_image_thumbnail(self, source_path: str, tmp_path: str) -> None:
        """Read `source_path` through the FileSystem seam (fake-fs
        testable, matching hashing.py) and write a resized JPEG to
        `tmp_path`. Raises on any Pillow decode error/read error -- caught
        by the shared except clause in ensure()."""
        with self._fs.open_read(source_path) as stream:
            with Image.open(stream) as image:
# See: docs/technical/components/review-ui.md
                oriented_image = ImageOps.exif_transpose(image)
                rgb_image = oriented_image.convert("RGB")
                rgb_image.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM))
                rgb_image.save(tmp_path, "JPEG", quality=85)

    def _write_video_thumbnail(self, source_path: str, tmp_path: str) -> None:
        """Extract one frame from the video at `source_path` via ffmpeg and
        write a resized JPEG to `tmp_path`, reusing the exact same Pillow
        resize path as images.

        Deliberately bypasses `self._fs.open_read()` -- ffmpeg is an
        external process that needs a real filesystem path to open itself
        (there is no way to hand it a FakeFileSystem-backed in-memory
        stream), unlike the Pillow image path above which only ever needs
        Python-readable bytes. `source_path` must therefore name a real
        file on disk for video sources; this matches how ensure() is
        actually called in production (tidy/review passes real
        on-disk paths) and is why this codebase's video tests use real
        temp files instead of FakeFileSystem."""
        frame_path = _extract_video_frame(source_path)
        if frame_path is None:
            raise _VideoFrameError(f"ffmpeg could not extract a frame from {source_path!r}")
        try:
            with Image.open(frame_path) as image:
                rgb_image = image.convert("RGB")
                rgb_image.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM))
                rgb_image.save(tmp_path, "JPEG", quality=85)
        finally:
            if os.path.exists(frame_path):
                os.remove(frame_path)
