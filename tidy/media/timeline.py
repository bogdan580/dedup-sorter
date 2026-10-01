"""Best-effort photo/video timestamp extraction: EXIF DateTimeOriginal ->
filename pattern -> mtime fallback chain. EXIF alone does not cover the
whole archive, so the filename and mtime fallbacks keep every candidate
participating in series.py's time-based clustering even when EXIF is
missing or stripped.

Reads through the `FileSystem` seam (`fs.open_read`), exactly like
hashing.py/thumbnails.py, so this is fully unit-testable against
FakeFileSystem.
"""
# Timestamps and series rules: docs/business/rules/events-and-series.md
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from tidy.fs.fsmodel import FileSystem
from tidy.media.imaging import DECODE_ERRORS, PILLOW_AVAILABLE

if PILLOW_AVAILABLE:
    from PIL import ExifTags, Image

# See: docs/business/rules/events-and-series.md
_EXIF_DATETIME_ORIGINAL_TAG = 36867
_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"

SOURCE_EXIF = "exif"
SOURCE_FILENAME = "filename"
SOURCE_MTIME = "mtime"


@dataclass(frozen=True)
class Timestamp:
    when: datetime
    source: str  # SOURCE_EXIF | SOURCE_FILENAME | SOURCE_MTIME


# See: docs/business/rules/events-and-series.md
_FILENAME_PATTERN = re.compile(
    r"(?<!\d)"
    r"(\d{4})([-_.]?)(\d{2})\2(\d{2})"          # YYYY?MM?DD
    r"(?:"
    r"[ T_-]?"
    r"(\d{2})([-_.:]?)(\d{2})\6(\d{2})"         # HH?MM?SS
    r"(?:[.,]?\d{1,3})?"                        # optional millisecond tail
    r")?"
    r"(?!\d)"
)

# See: docs/business/rules/events-and-series.md
_MIN_FILENAME_YEAR = 1995
_MAX_FILENAME_YEAR = 2100


def _from_exif(fs: FileSystem, path: str) -> Optional[datetime]:
    if not PILLOW_AVAILABLE:
        return None
    try:
        with fs.open_read(path) as stream:
            with Image.open(stream) as image:
                top_level = image.getexif()
# See: docs/business/rules/events-and-series.md
                raw = top_level.get_ifd(ExifTags.IFD.Exif).get(_EXIF_DATETIME_ORIGINAL_TAG)
                if raw is None:
                    raw = top_level.get(_EXIF_DATETIME_ORIGINAL_TAG)
    except DECODE_ERRORS:
        return None
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw.strip(), _EXIF_DATETIME_FORMAT)
    except ValueError:
        return None


def _from_filename(name: str) -> Optional[datetime]:
    """First plausible date in `name`, with the time defaulting to midnight
    when the name carries a date only ("2021-02-15.jpg").

    Scans every match rather than taking the first: a name like
    "IMG_1234_20210425_010518" has a leading run that can look date-shaped,
    and rejecting the whole name because of it would be the mtime fallback
    all over again."""
    for match in _FILENAME_PATTERN.finditer(name):
        year, _, month, day, hour, _, minute, second = match.groups()
        if not (_MIN_FILENAME_YEAR <= int(year) <= _MAX_FILENAME_YEAR):
            continue
        try:
            return datetime(
                int(year), int(month), int(day),
                int(hour or 0), int(minute or 0), int(second or 0),
            )
        except ValueError:
            continue  # digits matched the shape but aren't a real date/time
    return None


def _from_mtime(mtime_ns: int) -> datetime:
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000)


def extract_timestamp(fs: FileSystem, path: str, name: str, mtime_ns: int) -> Timestamp:
    """Best-effort timestamp for one file, trying EXIF, then the filename
    pattern, then mtime -- in that order. Never raises:
    an unreadable/corrupt/non-image source just falls through to the next
    source in the chain, and mtime is always available as the final
    fallback."""
    exif_when = _from_exif(fs, path)
    if exif_when is not None:
        return Timestamp(when=exif_when, source=SOURCE_EXIF)

    filename_when = _from_filename(name)
    if filename_when is not None:
        return Timestamp(when=filename_when, source=SOURCE_FILENAME)

    return Timestamp(when=_from_mtime(mtime_ns), source=SOURCE_MTIME)
