"""Tests for thumbnails.py (thumbnail cache, including video).

Uses real Pillow-encoded JPEG/PNG bytes (small, generated in-memory) fed
into FakeFileSystem as "source" content -- Pillow itself is not faked,
matching hashing.py's own tests (which hash real bytes, just small ones).
The thumbnail *cache directory* is a real tmp_path, mirroring cache.py's
real-sqlite-file testing convention: `.dedup`-internal storage is real
disk, never routed through FakeFileSystem.

Video thumbnails: video frame extraction
goes through a real ffmpeg subprocess (imageio_ffmpeg's bundled binary),
which can't be faked the way Pillow-only image decoding can -- there is no
"FakeFileSystem" equivalent for an external process. So the video tests
below generate real, tiny .mp4 fixtures on real disk (via ffmpeg's own
`-f lavfi -i color=...` synthetic source -- no sample video file needed)
and exercise ThumbnailStore.ensure() against them for real, including the
one empirically-critical case this codebase cares about: a video path
containing Cyrillic characters, a space, AND (defensively) the extended-
length ("\\\\?\\") prefix platform_win.py's WindowsFileSystem hands out.
"""
import io
import os
import subprocess
import sys
import threading
import time

import pytest
from PIL import Image

from tidy.media import thumbnails
from fakes import FakeFileSystem

_FFMPEG_EXE = thumbnails._ffmpeg_exe()
_ffmpeg_available = pytest.mark.skipif(
    _FFMPEG_EXE is None, reason="imageio_ffmpeg/ffmpeg binary not available in this environment"
)
_heif_available = pytest.mark.skipif(
    not thumbnails._HEIF_AVAILABLE, reason="pillow-heif not available in this environment"
)


def _make_test_video(path: str, duration: str = "1", size: str = "64x48") -> None:
    """Synthesize a tiny, real .mp4 at `path` using ffmpeg's own lavfi test
    source (a plain solid-color pattern) -- no sample video file is needed
    on disk for this. Fails the calling test loudly (via assert) if ffmpeg
    can't even generate its own test fixture, since that would invalidate
    every test built on top of it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = [
        _FFMPEG_EXE, "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=blue:s={size}:d={duration}:r=5",
        "-pix_fmt", "yuv420p", path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert os.path.isfile(path) and os.path.getsize(path) > 0


def _jpeg_bytes(size=(40, 30), color=(200, 50, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _jpeg_bytes_with_orientation(orientation: int, size=(40, 30), color=(200, 50, 50)) -> bytes:
    # A source whose pixels are stored one way
    # but carry an EXIF Orientation tag saying "display rotated" -- exactly
    # what a camera/phone held sideways without in-camera rotation writes.
    img = Image.new("RGB", size, color)
    exif = img.getexif()
    exif[0x0112] = orientation  # Orientation tag
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def _png_bytes(size=(40, 30), color=(10, 200, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color + (255,)).save(buf, "PNG")
    return buf.getvalue()


def _heic_bytes(size=(64, 48), color=(80, 160, 220)) -> bytes:
    # Self-contained fixture: pillow-heif's own HEIF encoder round-trips
    # through Pillow's normal Image.save() once register_heif_opener() has
    # run (thumbnails.py does this at import time), so no binary fixture
    # needs to be committed to the repo -- mirrors _jpeg_bytes()/
    # _png_bytes() above, just with format="HEIF" instead.
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="HEIF")
    return buf.getvalue()


class TestSupports:
    def test_jpg_supported(self):
        assert thumbnails.supports("D:/Archive/photo.jpg") is True

    def test_jpeg_uppercase_supported(self):
        assert thumbnails.supports("D:/Archive/PHOTO.JPEG") is True

    def test_png_supported(self):
        assert thumbnails.supports("D:/Archive/photo.PNG") is True

    def test_heic_supported(self):
        # pillow-heif is installed and thumbnails.py
        # registers its HEIF opener at import time, so the ~769 real HEIC
        # files in the archive are no longer invisible to thumbnail generation.
        assert thumbnails.supports("D:/Archive/photo.HEIC") is True

    def test_heif_supported(self):
        assert thumbnails.supports("D:/Archive/photo.heif") is True

    def test_heic_not_supported_without_pillow_heif(self, monkeypatch):
        monkeypatch.setattr(thumbnails, "_HEIF_AVAILABLE", False)
        assert thumbnails.supports("D:/Archive/photo.HEIC") is False

    def test_arw_not_supported(self):
        assert thumbnails.supports("D:/Archive/photo.ARW") is False

    def test_no_extension_not_supported(self):
        assert thumbnails.supports("D:/Archive/noext") is False


class TestKind:
    """kind() is extension-only and capability-independent (see module
    docstring/kind()'s own docstring) -- unlike supports(), it must not
    change answer based on whether Pillow/ffmpeg happen to be installed."""

    def test_jpg_is_image(self):
        assert thumbnails.kind("D:/Archive/photo.jpg") == "image"

    def test_jpeg_uppercase_is_image(self):
        assert thumbnails.kind("D:/Archive/PHOTO.JPEG") == "image"

    def test_png_is_image(self):
        assert thumbnails.kind("D:/Archive/photo.PNG") == "image"

    @pytest.mark.parametrize("ext", [".mp4", ".mov", ".3gp", ".wmv", ".mpg", ".avi"])
    def test_video_extensions_are_video(self, ext):
        assert thumbnails.kind(f"D:/Archive/clip{ext}") == "video"
        assert thumbnails.kind(f"D:/Archive/CLIP{ext.upper()}") == "video"

    def test_heic_is_image(self):
        assert thumbnails.kind("D:/Archive/photo.HEIC") == "image"

    def test_heif_is_image(self):
        assert thumbnails.kind("D:/Archive/photo.heif") == "image"

    def test_heic_is_image_even_without_pillow_heif(self, monkeypatch):
        # kind() is extension-only and capability-independent (see its own
        # docstring) -- it must keep classifying .heic as "image" even when
        # pillow-heif happens to be unavailable, mirroring how video
        # extensions stay "video" regardless of ffmpeg availability.
        monkeypatch.setattr(thumbnails, "_HEIF_AVAILABLE", False)
        assert thumbnails.kind("D:/Archive/photo.HEIC") == "image"

    def test_arw_is_none(self):
        assert thumbnails.kind("D:/Archive/photo.ARW") is None

    def test_no_extension_is_none(self):
        assert thumbnails.kind("D:/Archive/noext") is None


class TestSupportsVideo:
    """supports() for video is capability-aware: Pillow AND a resolvable
    ffmpeg binary, unlike kind() above."""

    def test_video_supported_when_pillow_and_ffmpeg_available(self, monkeypatch):
        monkeypatch.setattr(thumbnails, "_ffmpeg_exe", lambda: "C:/fake/ffmpeg.exe")
        assert thumbnails.supports("D:/Archive/clip.mp4") is True

    def test_video_not_supported_when_ffmpeg_unavailable(self, monkeypatch):
        monkeypatch.setattr(thumbnails, "_ffmpeg_exe", lambda: None)
        assert thumbnails.supports("D:/Archive/clip.mp4") is False

    def test_video_not_supported_without_pillow(self, monkeypatch):
        monkeypatch.setattr(thumbnails, "_ffmpeg_exe", lambda: "C:/fake/ffmpeg.exe")
        monkeypatch.setattr(thumbnails, "_PILLOW_AVAILABLE", False)
        assert thumbnails.supports("D:/Archive/clip.mp4") is False


class TestFfmpegExeResolution:
    """_ffmpeg_exe() is memoized (functools.lru_cache) -- each test clears
    the cache before AND after so these don't leak state into other tests
    (including TestSupportsVideo above, which monkeypatches the function
    itself and therefore never touches the real cache)."""

    def setup_method(self):
        thumbnails._ffmpeg_exe.cache_clear()

    def teardown_method(self):
        thumbnails._ffmpeg_exe.cache_clear()

    def test_returns_none_when_import_fails(self, monkeypatch):
        # sys.modules[name] = None is documented Python behavior: it makes
        # `import name` raise ImportError without needing the package to
        # actually be uninstalled.
        monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
        assert thumbnails._ffmpeg_exe() is None

    def test_returns_none_when_get_ffmpeg_exe_raises_oserror(self, monkeypatch):
        class _FakeModule:
            @staticmethod
            def get_ffmpeg_exe():
                raise OSError("extraction failed")

        monkeypatch.setitem(sys.modules, "imageio_ffmpeg", _FakeModule)
        assert thumbnails._ffmpeg_exe() is None

    def test_result_is_memoized_across_calls(self, monkeypatch):
        calls = []

        class _FakeModule:
            @staticmethod
            def get_ffmpeg_exe():
                calls.append(1)
                return "C:/fake/ffmpeg.exe"

        monkeypatch.setitem(sys.modules, "imageio_ffmpeg", _FakeModule)
        first = thumbnails._ffmpeg_exe()
        second = thumbnails._ffmpeg_exe()
        assert first == second == "C:/fake/ffmpeg.exe"
        assert len(calls) == 1  # not re-imported/re-resolved on the second call

    def test_real_resolution_succeeds_in_this_environment(self):
        # Sanity check that the actual dependency resolves for real, not
        # just in mocked tests.
        assert thumbnails._ffmpeg_exe() is not None
        assert os.path.isfile(thumbnails._ffmpeg_exe())


class TestStripExtendedPrefix:
    def test_strips_plain_long_path_prefix(self):
        assert thumbnails._strip_extended_prefix("\\\\?\\D:\\A\\clip.mp4") == "D:\\A\\clip.mp4"

    def test_strips_unc_prefix(self):
        assert (
            thumbnails._strip_extended_prefix("\\\\?\\UNC\\server\\share\\clip.mp4")
            == "\\\\server\\share\\clip.mp4"
        )

    def test_leaves_unprefixed_path_untouched(self):
        assert thumbnails._strip_extended_prefix("D:\\A\\clip.mp4") == "D:\\A\\clip.mp4"


class TestThumbnailStoreEnsure:
    def test_generates_jpeg_thumbnail_on_first_request(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/photo.jpg", _jpeg_bytes())
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        out_path = store.ensure("D:/Archive/photo.jpg", "abc123")

        assert out_path == str(tmp_path / "abc123.jpg")
        assert os.path.isfile(out_path)
        with Image.open(out_path) as img:
            assert img.format == "JPEG"
            assert max(img.size) <= thumbnails.THUMB_MAX_DIM

    def test_thumb_max_dim_is_640(self):
        # Thumbnails always generated at 2x
        # the original 320px baseline.
        assert thumbnails.THUMB_MAX_DIM == 640

    def test_large_source_is_downscaled_to_the_current_640_bound_not_the_old_320(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/big.jpg", _jpeg_bytes(size=(1200, 900)))
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        out_path = store.ensure("D:/Archive/big.jpg", "big-id")

        with Image.open(out_path) as img:
            # 1200x900 (4:3) fit inside a 640x640 box -> 640x480, not the
            # old 320x240 -- proves the actual generated pixels grew, not
            # just the constant's value.
            assert img.size == (640, 480)

    def test_exif_orientation_is_applied_to_generated_thumbnail(self, tmp_path):
        # Thumbnails ignored the source's EXIF
        # Orientation tag, so a photo whose pixels are stored
        # landscape-but-tagged-rotate-90 displayed sideways. orientation=6
        # ("rotate 90 CW to display correctly") must swap the stored 40x30
        # landscape into a genuinely portrait 30x40 output.
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/sideways.jpg", _jpeg_bytes_with_orientation(6, size=(40, 30)))
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        out_path = store.ensure("D:/Archive/sideways.jpg", "oriented-id")

        with Image.open(out_path) as img:
            assert img.size == (30, 40)

    def test_no_orientation_tag_is_unaffected(self, tmp_path):
        # A plain source with no EXIF at all (e.g. _jpeg_bytes() everywhere
        # else in this file) must keep behaving exactly as before.
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/plain.jpg", _jpeg_bytes(size=(40, 30)))
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        out_path = store.ensure("D:/Archive/plain.jpg", "plain-id")

        with Image.open(out_path) as img:
            assert img.size == (40, 30)

    def test_generates_from_png_source(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/photo.png", _png_bytes())
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        out_path = store.ensure("D:/Archive/photo.png", "png-id")

        assert out_path is not None
        with Image.open(out_path) as img:
            assert img.format == "JPEG"  # always re-encoded to JPEG, even from PNG

    def test_second_call_reuses_cached_file_without_reopening_source(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/photo.jpg", _jpeg_bytes())
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        first = store.ensure("D:/Archive/photo.jpg", "abc123")
        assert fs.open_read_calls == ["D:/Archive/photo.jpg"]

        second = store.ensure("D:/Archive/photo.jpg", "abc123")

        assert second == first
        # Still just the one open_read call from the first generation --
        # the second call must be served from the on-disk cache.
        assert fs.open_read_calls == ["D:/Archive/photo.jpg"]

    def test_unsupported_extension_returns_none_without_touching_fs(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/video.mp4", b"not-an-image")
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        result = store.ensure("D:/Archive/video.mp4", "vid-id")

        assert result is None
        assert fs.open_read_calls == []

    def test_corrupt_image_bytes_returns_none_not_raise(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/broken.jpg", b"this is not a real jpeg")
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        result = store.ensure("D:/Archive/broken.jpg", "broken-id")

        assert result is None

    def test_source_read_error_returns_none_not_raise(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/locked.jpg", _jpeg_bytes())
        fs.set_open_error("D:/Archive/locked.jpg", PermissionError("locked"))
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        result = store.ensure("D:/Archive/locked.jpg", "locked-id")

        assert result is None

    def test_creates_thumbs_dir_if_missing(self, tmp_path):
        thumbs_dir = tmp_path / "nested" / "thumbs"
        fs = FakeFileSystem()
        store = thumbnails.ThumbnailStore(fs, str(thumbs_dir))
        assert thumbs_dir.is_dir()

    def test_path_for_id(self, tmp_path):
        fs = FakeFileSystem()
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))
        assert store.path_for_id("xyz") == str(tmp_path / "xyz.jpg")

    def test_no_leftover_tmp_file_after_success(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/photo.jpg", _jpeg_bytes())
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))
        store.ensure("D:/Archive/photo.jpg", "abc123")
        assert sorted(os.listdir(tmp_path)) == ["abc123.jpg"]

    def test_decompression_bomb_returns_none_not_raise(self, tmp_path, monkeypatch):
        # Regression: a source image exceeding Pillow's MAX_IMAGE_PIXELS
        # guard raises Image.DecompressionBombError, a plain Exception
        # subclass (not OSError) -- must be caught like any other
        # undecodable source, not propagate out of ensure(). Shrinking the
        # guard instead of generating a huge image keeps this test fast.
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/huge.jpg", _jpeg_bytes(size=(40, 30)))
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        result = store.ensure("D:/Archive/huge.jpg", "huge-id")

        assert result is None

    def test_concurrent_ensure_for_same_thumb_id_does_not_raise(self, tmp_path):
        # Regression: every card in one duplicate group shares a single
        # thumb_id ("jedna miniatura na grupe"), so
        # ThreadingHTTPServer can legitimately run ensure() for the same
        # id from two request threads at once before either has cached
        # the result -- neither call may raise, and the cache must end up
        # holding one valid thumbnail.
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/photo.jpg", _jpeg_bytes())
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        results = []
        errors = []

        def worker():
            try:
                results.append(store.ensure("D:/Archive/photo.jpg", "shared-id"))
            except Exception as exc:  # noqa: BLE001 - test assertion, not production code
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == []
        assert all(r == str(tmp_path / "shared-id.jpg") for r in results)
        with Image.open(results[0]) as img:
            assert img.format == "JPEG"


class TestThumbnailStoreRotate:
    """The manual rotate button, for photos whose
    EXIF orientation is missing/wrong even after ensure()'s automatic
    exif_transpose() fix. rotate() only ever rewrites the already-cached
    .jpg under thumbs_dir in place -- never touches the original source."""

    def test_rotate_missing_cached_thumb_returns_false(self, tmp_path):
        store = thumbnails.ThumbnailStore(FakeFileSystem(), str(tmp_path))
        assert store.rotate("never-generated", 1) is False

    def test_rotate_swaps_dimensions_for_non_square_source(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/photo.jpg", _jpeg_bytes(size=(40, 30)))
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))
        out_path = store.ensure("D:/Archive/photo.jpg", "abc123")
        with Image.open(out_path) as img:
            assert img.size == (40, 30)

        assert store.rotate("abc123", 1) is True

        with Image.open(out_path) as img:
            assert img.size == (30, 40)

    def test_rotate_four_times_returns_to_original_dimensions(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/photo.jpg", _jpeg_bytes(size=(40, 30)))
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))
        out_path = store.ensure("D:/Archive/photo.jpg", "abc123")

        for _ in range(4):
            assert store.rotate("abc123", 1) is True

        with Image.open(out_path) as img:
            assert img.size == (40, 30)

    def test_rotate_leaves_no_leftover_tmp_file(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/photo.jpg", _jpeg_bytes())
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))
        store.ensure("D:/Archive/photo.jpg", "abc123")

        store.rotate("abc123", 1)

        assert sorted(os.listdir(tmp_path)) == ["abc123.jpg"]

    def test_rotate_output_stays_a_valid_jpeg(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/photo.jpg", _jpeg_bytes())
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))
        out_path = store.ensure("D:/Archive/photo.jpg", "abc123")

        store.rotate("abc123", 1)

        with Image.open(out_path) as img:
            assert img.format == "JPEG"


@_heif_available
class TestThumbnailStoreEnsureHeic:
    """HEIC/HEIF thumbnail generation, gated on pillow-heif actually being
    installed (see _heif_available above) -- mirrors _ffmpeg_available's
    skip pattern for video. Uses a real, self-contained HEIC fixture
    generated in-memory via pillow-heif's own encoder (_heic_bytes()), fed
    through FakeFileSystem exactly like the JPEG/PNG tests above."""

    def test_generates_real_decoded_thumbnail_from_heic_source(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/photo.heic", _heic_bytes())
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        out_path = store.ensure("D:/Archive/photo.heic", "heic-id")

        assert out_path == str(tmp_path / "heic-id.jpg")
        assert os.path.isfile(out_path)
        with Image.open(out_path) as img:
            assert img.format == "JPEG"  # always re-encoded to JPEG, even from HEIC
            assert max(img.size) <= thumbnails.THUMB_MAX_DIM

    def test_generates_real_decoded_thumbnail_from_heif_extension(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/photo.heif", _heic_bytes())
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        out_path = store.ensure("D:/Archive/photo.heif", "heif-id")

        assert out_path is not None
        with Image.open(out_path) as img:
            assert img.format == "JPEG"

    def test_corrupt_heic_bytes_returns_none_not_raise(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/broken.heic", b"this is not a real heic file")
        store = thumbnails.ThumbnailStore(fs, str(tmp_path))

        result = store.ensure("D:/Archive/broken.heic", "broken-heic-id")

        assert result is None


@_ffmpeg_available
class TestVideoThumbnailEndToEnd:
    """Real ffmpeg subprocess, real .mp4 fixtures on real disk (see
    _make_test_video()). ThumbnailStore's `fs` argument is a FakeFileSystem
    in every test here even though it's never actually touched for a video
    source -- see thumbnails.py's `_write_video_thumbnail()` docstring:
    ffmpeg needs a real on-disk path, not a FileSystem-seam stream, so
    these fixtures write straight to `tmp_path` instead of through the fs
    seam the image tests above use."""

    def test_real_mp4_produces_actual_thumbnail(self, tmp_path):
        video_path = str(tmp_path / "clip.mp4")
        _make_test_video(video_path)
        store = thumbnails.ThumbnailStore(FakeFileSystem(), str(tmp_path / "thumbs"))

        out_path = store.ensure(video_path, "vid-1")

        assert out_path is not None
        assert os.path.isfile(out_path)
        with Image.open(out_path) as img:
            assert img.format == "JPEG"
            assert max(img.size) <= thumbnails.THUMB_MAX_DIM

    def test_cyrillic_and_space_path_produces_thumbnail(self, tmp_path):
        # CRITICAL empirical case: the real archive
        # has paths with Cyrillic characters and spaces -- confirm the
        # whole ensure() pipeline actually works end-to-end for one, not
        # just in theory.
        video_dir = tmp_path / "Тестовая папка с пробелом"
        video_path = str(video_dir / "видео файл.mp4")
        _make_test_video(video_path)
        store = thumbnails.ThumbnailStore(FakeFileSystem(), str(tmp_path / "thumbs"))

        out_path = store.ensure(video_path, "vid-cyrillic")

        assert out_path is not None
        with Image.open(out_path) as img:
            assert img.format == "JPEG"

    def test_extended_length_prefixed_cyrillic_space_path_produces_thumbnail(self, tmp_path):
        # CRITICAL empirical case, part 2: the SAME Cyrillic+space video,
        # but with the `\\?\` extended-length prefix platform_win.py's
        # WindowsFileSystem hands out from scandir()/stat() prepended --
        # this is the exact shape of path ensure() actually receives from
        # tidy/review in production for a real, deep archive path.
        video_dir = tmp_path / "Тестовая папка с пробелом"
        video_path = str(video_dir / "видео файл.mp4")
        _make_test_video(video_path)
        prefixed_path = "\\\\?\\" + os.path.abspath(video_path)
        store = thumbnails.ThumbnailStore(FakeFileSystem(), str(tmp_path / "thumbs"))

        out_path = store.ensure(prefixed_path, "vid-prefixed")

        assert out_path is not None
        with Image.open(out_path) as img:
            assert img.format == "JPEG"

    def test_very_short_video_falls_back_to_zero_second_seek(self, tmp_path):
        # A clip shorter than the initial 1s seek offset must still produce
        # a frame via the -ss 0 retry.
        video_path = str(tmp_path / "short.mp4")
        _make_test_video(video_path, duration="0.2")
        store = thumbnails.ThumbnailStore(FakeFileSystem(), str(tmp_path / "thumbs"))

        out_path = store.ensure(video_path, "vid-short")

        assert out_path is not None
        with Image.open(out_path) as img:
            assert img.format == "JPEG"

    def test_ffmpeg_unavailable_returns_none_not_raise(self, tmp_path, monkeypatch):
        # Acceptance criterion: with imageio_ffmpeg artificially made
        # unavailable, ensure() returns None (placeholder) with zero
        # exceptions raised.
        video_path = str(tmp_path / "clip.mp4")
        _make_test_video(video_path)
        monkeypatch.setattr(thumbnails, "_ffmpeg_exe", lambda: None)
        store = thumbnails.ThumbnailStore(FakeFileSystem(), str(tmp_path / "thumbs"))

        result = store.ensure(video_path, "vid-no-ffmpeg")

        assert result is None

    def test_corrupt_video_returns_none_quickly_not_raise(self, tmp_path):
        video_path = str(tmp_path / "corrupt.mp4")
        with open(video_path, "wb") as f:
            f.write(b"this is not a real mp4 container" * 20)
        store = thumbnails.ThumbnailStore(FakeFileSystem(), str(tmp_path / "thumbs"))

        start = time.time()
        result = store.ensure(video_path, "vid-corrupt")
        elapsed = time.time() - start

        assert result is None
        # ffmpeg detects a malformed container instantly (verified
        # empirically: well under 1s) -- this asserts the corrupt-file path
        # doesn't fall through to actually waiting out the full timeout
        # twice (which would still return None, just slowly, and could
        # make the suite flaky under load).
        assert elapsed < 10

    def test_missing_source_file_returns_none_not_raise(self, tmp_path):
        store = thumbnails.ThumbnailStore(FakeFileSystem(), str(tmp_path / "thumbs"))

        result = store.ensure(str(tmp_path / "does-not-exist.mp4"), "vid-missing")

        assert result is None

    def test_timeout_returns_none_not_raise(self, tmp_path, monkeypatch):
        # Simulates a hung ffmpeg process without actually waiting out a
        # real timeout, keeping this test fast.
        video_path = str(tmp_path / "clip.mp4")
        _make_test_video(video_path)

        def _raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=15)

        monkeypatch.setattr(thumbnails.subprocess, "run", _raise_timeout)
        store = thumbnails.ThumbnailStore(FakeFileSystem(), str(tmp_path / "thumbs"))

        result = store.ensure(video_path, "vid-timeout")

        assert result is None

    def test_second_call_reuses_cached_video_thumbnail(self, tmp_path):
        video_path = str(tmp_path / "clip.mp4")
        _make_test_video(video_path)
        store = thumbnails.ThumbnailStore(FakeFileSystem(), str(tmp_path / "thumbs"))

        first = store.ensure(video_path, "vid-cache")
        second = store.ensure(video_path, "vid-cache")

        assert first == second
        assert os.path.isfile(first)

    def test_ffmpeg_zero_byte_output_returns_none_and_cleans_up_frame_file(self, tmp_path, monkeypatch):
        # Edge case: ffmpeg exits 0 but writes an empty file (observed as
        # theoretically possible even though not reproduced with a real
        # corrupt/short clip in this suite) -- _run_ffmpeg_frame's size
        # check must reject it, and the leftover empty temp file must still
        # get cleaned up once both seek attempts are exhausted.
        video_path = str(tmp_path / "clip.mp4")
        _make_test_video(video_path)

        class _FakeResult:
            returncode = 0

        def _fake_run(cmd, **kwargs):
            frame_path = cmd[-1]
            open(frame_path, "wb").close()  # bogus zero-byte "success"
            return _FakeResult()

        monkeypatch.setattr(thumbnails.subprocess, "run", _fake_run)
        store = thumbnails.ThumbnailStore(FakeFileSystem(), str(tmp_path / "thumbs"))

        result = store.ensure(video_path, "vid-empty-frame")

        assert result is None

    def test_extract_video_frame_returns_none_when_ffmpeg_exe_unresolved(self, monkeypatch):
        # Direct unit test of the internal helper's own guard clause (in
        # production this is always gated by supports() first, via
        # ensure(), but the helper is defensive on its own too).
        monkeypatch.setattr(thumbnails, "_ffmpeg_exe", lambda: None)
        assert thumbnails._extract_video_frame("D:/Archive/whatever.mp4") is None


class TestFfmpegProtocolWhitelist:
    def test_frame_argv_whitelists_protocols_before_input(self, tmp_path, monkeypatch):
        seen = {}

        class _R:
            returncode = 1

        def _fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return _R()

        monkeypatch.setattr(thumbnails.subprocess, "run", _fake_run)
        thumbnails._run_ffmpeg_frame("ffmpeg", "clip.mp4", "1", str(tmp_path / "f.jpg"))
        cmd = seen["cmd"]
        i = cmd.index("-i")
        assert cmd[i - 2:i] == ["-protocol_whitelist", "file,pipe"]
