"""Tests for the OS-agnostic-testable parts of platform_win.py:
to_extended() string logic and attribute-bit decoding given a raw int.

WindowsFileSystem itself (real os.scandir/os.stat/ctypes calls, junctions,
hard links, offline files) is integration-only and NOT covered here -- it
cannot be exercised without a real Windows filesystem with those special
objects present. See platform_win.py's module docstring.
"""
import pytest

from tidy.fs import platform_win


class TestToExtended:
    def test_plain_drive_path_gets_prefixed(self):
        assert platform_win.to_extended("D:\\ARCHIVE\\file.jpg") == "\\\\?\\D:\\ARCHIVE\\file.jpg"

    def test_cyrillic_drive_path_gets_prefixed(self):
        path = "D:\\Фото\\Мій архів\\фото.jpg"
        assert platform_win.to_extended(path) == "\\\\?\\" + path

    def test_unc_path_gets_unc_prefix(self):
        assert (
            platform_win.to_extended("\\\\server\\share\\file.jpg")
            == "\\\\?\\UNC\\server\\share\\file.jpg"
        )

    def test_already_prefixed_backslash_path_is_unchanged(self):
        already = "\\\\?\\D:\\ARCHIVE\\file.jpg"
        assert platform_win.to_extended(already) == already

    def test_already_unc_prefixed_backslash_path_is_unchanged(self):
        already = "\\\\?\\UNC\\server\\share\\file.jpg"
        assert platform_win.to_extended(already) == already

    # Regression (Cofnij tab): fs.scandir() yields an
    # ALREADY-prefixed entry.path, and batches.py's pure-string _join() (like
    # tidy/moves/applier's) appends a filename with "/" regardless of the base's own
    # separator style. The prefix disables Win32's normal "/"->"\\"
    # conversion (module docstring), so an already-prefixed path is NOT
    # exempt from normalization -- only from re-prefixing. Before this fix,
    # to_extended() returned such a path completely unchanged, so
    # os.path.exists()/os.path.exists()-based lookups (GET /api/batches)
    # silently found nothing on a real archive despite the file being
    # there -- FakeFileSystem never reproduces the prefix, so no test caught
    # this until this one exercises it directly.
    def test_already_prefixed_path_with_forward_slash_gets_normalized(self):
        already = "\\\\?\\D:\\ARCHIVE\\!MOVES\\2026-08-17_22-31/manifest.csv"
        assert (
            platform_win.to_extended(already)
            == "\\\\?\\D:\\ARCHIVE\\!MOVES\\2026-08-17_22-31\\manifest.csv"
        )

    def test_already_unc_prefixed_path_with_forward_slash_gets_normalized(self):
        already = "\\\\?\\UNC\\server\\share/sub/file.jpg"
        assert (
            platform_win.to_extended(already)
            == "\\\\?\\UNC\\server\\share\\sub\\file.jpg"
        )

    # Regression: the \\?\ prefix disables Win32's normal /-to-\ separator
    # conversion entirely -- a forward slash surviving into a prefixed path
    # makes os.rename()/os.makedirs() fail with WinError 123 on real
    # Windows (verified by hand; tidy/moves/applier's pure-string path helpers
    # build destinations with "/" regardless of the input's own style).
    def test_forward_slash_path_is_backslash_normalized(self):
        assert (
            platform_win.to_extended("D:/ARCHIVE/sub/file.jpg")
            == "\\\\?\\D:\\ARCHIVE\\sub\\file.jpg"
        )

    def test_mixed_separator_path_is_backslash_normalized(self):
        assert (
            platform_win.to_extended("D:\\ARCHIVE/sub\\file.jpg")
            == "\\\\?\\D:\\ARCHIVE\\sub\\file.jpg"
        )

    def test_forward_slash_unc_path_gets_unc_prefix_and_normalizes(self):
        assert (
            platform_win.to_extended("//server/share/file.jpg")
            == "\\\\?\\UNC\\server\\share\\file.jpg"
        )

    def test_long_path_over_260_chars_gets_prefixed(self):
        long_name = "a" * 300
        path = f"D:\\ARCHIVE\\{long_name}.jpg"
        result = platform_win.to_extended(path)
        assert result.startswith("\\\\?\\")
        assert len(result) > 260

    # Regression: `\\?\` disables Win32 path normalization, so it only
    # works on an already-absolute path -- a relative root silently
    # resolved to nothing on disk (`\\?\.` / `\\?\sub`) instead of raising,
    # so the failure was easy to miss in a console summary.
    def test_relative_path_raises_value_error(self):
        with pytest.raises(ValueError):
            platform_win.to_extended(".")

    def test_bare_relative_name_raises_value_error(self):
        with pytest.raises(ValueError):
            platform_win.to_extended("sub")


class TestAttributeDecoding:
    def test_is_reparse_point_true_when_bit_set(self):
        assert platform_win.is_reparse_point(0x400) is True

    def test_is_reparse_point_false_when_bit_absent(self):
        assert platform_win.is_reparse_point(0x20) is False

    def test_is_reparse_point_true_combined_with_other_bits(self):
        assert platform_win.is_reparse_point(0x20 | 0x400) is True

    def test_is_directory_attr_true_when_bit_set(self):
        assert platform_win.is_directory_attr(0x10) is True

    def test_is_directory_attr_false_when_bit_absent(self):
        assert platform_win.is_directory_attr(0x20) is False
