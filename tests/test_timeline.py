"""Tests for timeline.py (EXIF/filename/mtime timestamp fallback chain)."""
import io
from datetime import datetime

from PIL import ExifTags, Image

from tidy.media import timeline
from fakes import FakeFileSystem


def _jpeg_bytes_with_exif(datetime_original=None, size=(20, 15)) -> bytes:
    """Writes DateTimeOriginal into the Exif SubIFD (tag 0x8769/34665),
    exactly where real cameras/phones put it -- NOT the top-level IFD0
    dict (`exif[36867] = ...` would write there instead, which is what
    the original version of this fixture did, and which is why the first
    version of timeline.py's real bug -- reading only the top level --
    passed every test here while failing on every real photo)."""
    buf = io.BytesIO()
    image = Image.new("RGB", size, (100, 150, 200))
    if datetime_original is not None:
        exif = Image.Exif()
        exif[ExifTags.IFD.Exif] = {36867: datetime_original}
        image.save(buf, "JPEG", exif=exif.tobytes())
    else:
        image.save(buf, "JPEG")
    return buf.getvalue()


class TestExtractTimestampExifSource:
    def test_uses_exif_datetime_original_from_real_sub_ifd_structure(self):
        # Regression: real cameras/phones write DateTimeOriginal into the
        # Exif SubIFD, not the top-level IFD0 dict image.getexif() returns
        # directly -- _jpeg_bytes_with_exif() builds EXIF the same way, so
        # this genuinely exercises get_ifd(), not just a same-place
        # write-then-read round trip.
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/random_name.jpg", _jpeg_bytes_with_exif("2021:04:25 01:05:18"))

        result = timeline.extract_timestamp(fs, "D:/Archive/random_name.jpg", "random_name.jpg", 0)

        assert result.when == datetime(2021, 4, 25, 1, 5, 18)
        assert result.source == timeline.SOURCE_EXIF

    def test_falls_back_to_non_standard_top_level_datetime_original(self):
        # A minority of tools write DateTimeOriginal directly at the top
        # level instead of the real Exif SubIFD -- extract_timestamp()
        # still finds it via its fallback, it's just not the primary path.
        fs = FakeFileSystem()
        buf = io.BytesIO()
        image = Image.new("RGB", (20, 15), (1, 2, 3))
        exif = Image.Exif()
        exif[36867] = "2019:06:01 08:30:00"
        image.save(buf, "JPEG", exif=exif.tobytes())
        fs.add_file("D:/Archive/nonstandard.jpg", buf.getvalue())

        result = timeline.extract_timestamp(fs, "D:/Archive/nonstandard.jpg", "nonstandard.jpg", 0)

        assert result.when == datetime(2019, 6, 1, 8, 30, 0)
        assert result.source == timeline.SOURCE_EXIF

    def test_malformed_exif_datetime_falls_through(self):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/IMG_20210425_010518.jpg", _jpeg_bytes_with_exif("not-a-date"))

        result = timeline.extract_timestamp(
            fs, "D:/Archive/IMG_20210425_010518.jpg", "IMG_20210425_010518.jpg", 0
        )

        assert result.source == timeline.SOURCE_FILENAME
        assert result.when == datetime(2021, 4, 25, 1, 5, 18)


class TestExtractTimestampFilenameSource:
    def test_img_underscore_pattern(self):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/IMG_20210425_010518.jpg", _jpeg_bytes_with_exif(None))

        result = timeline.extract_timestamp(
            fs, "D:/Archive/IMG_20210425_010518.jpg", "IMG_20210425_010518.jpg", 0
        )

        assert result.source == timeline.SOURCE_FILENAME
        assert result.when == datetime(2021, 4, 25, 1, 5, 18)

    def test_vid_no_separator_pattern(self):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/VID20210425010518.mp4", b"not-an-image")

        result = timeline.extract_timestamp(
            fs, "D:/Archive/VID20210425010518.mp4", "VID20210425010518.mp4", 0
        )

        assert result.source == timeline.SOURCE_FILENAME
        assert result.when == datetime(2021, 4, 25, 1, 5, 18)

    def test_invalid_date_in_filename_falls_through_to_mtime(self):
        fs = FakeFileSystem()
        # 99th day-of-month doesn't exist -- must not raise, must fall
        # through to mtime instead.
        fs.add_file("D:/Archive/IMG_20219999_010518.jpg", b"not-an-image")

        result = timeline.extract_timestamp(
            fs, "D:/Archive/IMG_20219999_010518.jpg", "IMG_20219999_010518.jpg", 1_700_000_000_000_000_000
        )

        assert result.source == timeline.SOURCE_MTIME


class TestFilenamePatternsMeasuredInTheArchive:
    """Every name here is a real one from this archive's `!NEW` folder.

    They were found by measuring which files fell through to mtime: 118 of
    1581, most of them carrying a perfectly readable date the old
    digits-only pattern could not see. mtime is the COPY date, so each of
    those was being filed under the wrong year -- one of them,
    "2021-02-15 194231.jpg", sat three weeks away from the trip it was
    actually taken on."""

    def _from_name(self, name):
        fs = FakeFileSystem()
        fs.add_file(f"D:/Archive/{name}", b"not-an-image")
        return timeline.extract_timestamp(
            fs, f"D:/Archive/{name}", name, 1_700_000_000_000_000_000,
        )

    def test_dashed_date_with_space_before_time(self):
        result = self._from_name("2021-02-15 194231.jpg")
        assert result.source == timeline.SOURCE_FILENAME
        assert result.when == datetime(2021, 2, 15, 19, 42, 31)

    def test_dashes_throughout_with_millisecond_tail(self):
        # The millisecond tail is why the old trailing (?!\\d) guard refused
        # this one: "230123" is followed by "475", not by a non-digit.
        result = self._from_name("2025-05-12-230123475.mp4")
        assert result.source == timeline.SOURCE_FILENAME
        assert result.when == datetime(2025, 5, 12, 23, 1, 23)

    def test_underscores_between_every_field(self):
        result = self._from_name("2020_05_06_08_50_23~2.mp4")
        assert result.source == timeline.SOURCE_FILENAME
        assert result.when == datetime(2020, 5, 6, 8, 50, 23)

    def test_date_only_name_lands_at_midnight(self):
        result = self._from_name("2021-02-15.jpg")
        assert result.source == timeline.SOURCE_FILENAME
        assert result.when == datetime(2021, 2, 15, 0, 0, 0)

    def test_messenger_id_is_not_a_date(self):
        # "received_280487600480313.jpeg" -- a 15-digit id. Reading a date
        # out of an id is worse than falling back to mtime: mtime is at
        # least the day the file arrived, while an id yields a random year.
        assert timeline._from_filename("received_280487600480313.jpeg") is None

    def test_telegram_id_is_not_a_date(self):
        assert timeline._from_filename("2_5316582463433089328.MP4") is None

    def test_epoch_milliseconds_are_not_a_date(self):
        # "1780592864140.png" parses as year 1780 without a year range.
        assert timeline._from_filename("1780592864140.png") is None

    def test_mixed_separators_are_rejected(self):
        # The separator is back-referenced, so half-punctuated digits read
        # as an id rather than a date.
        assert timeline._from_filename("2021-0215 194231.jpg") is None

    def test_leading_id_does_not_hide_a_real_date_later_in_the_name(self):
        result = self._from_name("IMG_1234_20210425_010518.jpg")
        assert result.source == timeline.SOURCE_FILENAME
        assert result.when == datetime(2021, 4, 25, 1, 5, 18)


class TestExtractTimestampMtimeFallback:
    def test_unreadable_non_image_with_no_filename_pattern_uses_mtime(self):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/DSC0001.jpg", b"not-an-image")
        mtime_ns = 1_650_000_000_000_000_000

        result = timeline.extract_timestamp(fs, "D:/Archive/DSC0001.jpg", "DSC0001.jpg", mtime_ns)

        assert result.source == timeline.SOURCE_MTIME
        assert result.when == datetime.fromtimestamp(mtime_ns / 1_000_000_000)

    def test_source_read_error_falls_back_to_mtime(self):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/locked.jpg", _jpeg_bytes_with_exif("2021:04:25 01:05:18"))
        fs.set_open_error("D:/Archive/locked.jpg", PermissionError("locked"))
        mtime_ns = 1_650_000_000_000_000_000

        result = timeline.extract_timestamp(fs, "D:/Archive/locked.jpg", "locked.jpg", mtime_ns)

        assert result.source == timeline.SOURCE_MTIME
