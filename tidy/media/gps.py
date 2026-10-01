"""GPS coordinate extraction from EXIF.

Real cameras/phones store GPS tags in a separate "GPS IFD" (pointed to by
IFD0 tag 0x8825/GPSInfo), not the flat top-level dict `image.getexif()`
returns directly. Reads through the `FileSystem` seam, so this is fully
unit-testable against FakeFileSystem.
"""
# Event rules: docs/business/rules/events-and-series.md
from __future__ import annotations

from typing import Optional, Tuple

from tidy.fs.fsmodel import FileSystem
from tidy.media.imaging import DECODE_ERRORS, PILLOW_AVAILABLE

if PILLOW_AVAILABLE:
    from PIL import ExifTags, Image

# See: docs/business/rules/events-and-series.md
_GPS_LATITUDE_REF = 1
_GPS_LATITUDE = 2
_GPS_LONGITUDE_REF = 3
_GPS_LONGITUDE = 4


def _ref_str(value) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="ignore").strip("\x00").upper()
    return str(value).strip().upper()


def _dms_to_decimal(dms, ref: str) -> float:
    degrees, minutes, seconds = (float(component) for component in dms)
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


def extract_gps(fs: FileSystem, path: str) -> Optional[Tuple[float, float]]:
    """Best-effort (lat, lon) in decimal degrees, or None if the file has
    no Pillow-readable GPS EXIF (missing tags, no Pillow, undecodable/
    corrupt file, malformed coordinate data). Never raises."""
    if not PILLOW_AVAILABLE:
        return None
    try:
        with fs.open_read(path) as stream:
            with Image.open(stream) as image:
                gps_ifd = image.getexif().get_ifd(ExifTags.IFD.GPSInfo)
    except DECODE_ERRORS:
        return None
    if not gps_ifd:
        return None

    lat = gps_ifd.get(_GPS_LATITUDE)
    lat_ref = gps_ifd.get(_GPS_LATITUDE_REF)
    lon = gps_ifd.get(_GPS_LONGITUDE)
    lon_ref = gps_ifd.get(_GPS_LONGITUDE_REF)
    if lat is None or lon is None or lat_ref is None or lon_ref is None:
        return None

    try:
        latitude = _dms_to_decimal(lat, _ref_str(lat_ref))
        longitude = _dms_to_decimal(lon, _ref_str(lon_ref))
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        return None

    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    return (latitude, longitude)
