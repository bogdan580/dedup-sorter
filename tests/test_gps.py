"""Tests for gps.py (EXIF GPS coordinate extraction)."""
import io

from PIL import ExifTags, Image

from tidy.media import gps
from fakes import FakeFileSystem


def _jpeg_bytes_with_gps(lat_dms=None, lat_ref="N", lon_dms=None, lon_ref="E", size=(20, 15)) -> bytes:
    """Writes GPS coordinates into the real GPS IFD (tag 0x8825/GPSInfo),
    exactly where real cameras/phones put them -- mirrors
    test_timeline.py's _jpeg_bytes_with_exif() SubIFD lesson: a fixture
    that wrote these flat at the top level would validate a bug, not the
    real structure."""
    buf = io.BytesIO()
    image = Image.new("RGB", size, (100, 150, 200))
    if lat_dms is not None:
        exif = Image.Exif()
        exif[ExifTags.IFD.GPSInfo] = {
            1: lat_ref, 2: lat_dms,
            3: lon_ref, 4: lon_dms,
        }
        image.save(buf, "JPEG", exif=exif.tobytes())
    else:
        image.save(buf, "JPEG")
    return buf.getvalue()


class TestExtractGps:
    def test_reads_coordinates_from_real_gps_sub_ifd_structure(self):
        fs = FakeFileSystem()
        fs.add_file(
            "D:/Archive/photo.jpg",
            _jpeg_bytes_with_gps((50, 6, 0.0), "N", (19, 56, 0.0), "E"),
        )

        result = gps.extract_gps(fs, "D:/Archive/photo.jpg")

        assert result is not None
        lat, lon = result
        assert lat == 50.1
        assert lon == 19.933333333333334

    def test_south_and_west_refs_negate_the_value(self):
        fs = FakeFileSystem()
        fs.add_file(
            "D:/Archive/photo.jpg",
            _jpeg_bytes_with_gps((23, 30, 0.0), "S", (46, 37, 0.0), "W"),
        )

        lat, lon = gps.extract_gps(fs, "D:/Archive/photo.jpg")

        assert lat < 0
        assert lon < 0

    def test_bytes_ref_values_are_handled(self):
        # Some writers store the ref as raw bytes (b"N") rather than str --
        # Pillow's own Exif dict can round-trip either way depending on how
        # the source encoder wrote it.
        fs = FakeFileSystem()
        buf = io.BytesIO()
        image = Image.new("RGB", (20, 15), (1, 2, 3))
        exif = Image.Exif()
        exif[ExifTags.IFD.GPSInfo] = {1: b"N", 2: (10, 0, 0.0), 3: b"E", 4: (20, 0, 0.0)}
        image.save(buf, "JPEG", exif=exif.tobytes())
        fs.add_file("D:/Archive/photo.jpg", buf.getvalue())

        result = gps.extract_gps(fs, "D:/Archive/photo.jpg")

        assert result == (10.0, 20.0)

    def test_no_gps_ifd_returns_none(self):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/no_gps.jpg", _jpeg_bytes_with_gps(None))

        assert gps.extract_gps(fs, "D:/Archive/no_gps.jpg") is None

    def test_partial_gps_tags_returns_none(self):
        # Latitude present, longitude missing -- must not crash on a
        # malformed/partial writer, just report "no usable GPS".
        fs = FakeFileSystem()
        buf = io.BytesIO()
        image = Image.new("RGB", (20, 15), (1, 2, 3))
        exif = Image.Exif()
        exif[ExifTags.IFD.GPSInfo] = {1: "N", 2: (10, 0, 0.0)}
        image.save(buf, "JPEG", exif=exif.tobytes())
        fs.add_file("D:/Archive/partial.jpg", buf.getvalue())

        assert gps.extract_gps(fs, "D:/Archive/partial.jpg") is None

    def test_non_image_file_returns_none(self):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/not_an_image.jpg", b"not-an-image")

        assert gps.extract_gps(fs, "D:/Archive/not_an_image.jpg") is None

    def test_read_error_returns_none(self):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/locked.jpg", _jpeg_bytes_with_gps((10, 0, 0.0), "N", (20, 0, 0.0), "E"))
        fs.set_open_error("D:/Archive/locked.jpg", PermissionError("locked"))

        assert gps.extract_gps(fs, "D:/Archive/locked.jpg") is None

    def test_out_of_range_coordinates_returns_none(self):
        # A malformed/corrupt DMS tuple could decode to a nonsensical
        # decimal degree value outside the physically valid range --
        # treated as unusable rather than propagated as a bogus location.
        fs = FakeFileSystem()
        buf = io.BytesIO()
        image = Image.new("RGB", (20, 15), (1, 2, 3))
        exif = Image.Exif()
        exif[ExifTags.IFD.GPSInfo] = {1: "N", 2: (999, 0, 0.0), 3: "E", 4: (20, 0, 0.0)}
        image.save(buf, "JPEG", exif=exif.tobytes())
        fs.add_file("D:/Archive/bad_coords.jpg", buf.getvalue())

        assert gps.extract_gps(fs, "D:/Archive/bad_coords.jpg") is None
