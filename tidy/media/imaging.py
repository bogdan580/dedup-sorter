"""Shared Pillow-availability flag and decode-error tuple for modules that
open an image and must never raise just because one source file is
corrupt/unreadable/huge.
"""
from __future__ import annotations

from typing import Tuple

try:
    from PIL import Image, UnidentifiedImageError
    PILLOW_AVAILABLE = True
# See: docs/technical/dependencies.md
    DECODE_ERRORS: Tuple[type, ...] = (
        OSError, UnidentifiedImageError, ValueError, MemoryError, Image.DecompressionBombError,
    )
except ImportError:  # pragma: no cover - Pillow is a stated requirement
    PILLOW_AVAILABLE = False
    DECODE_ERRORS = (OSError, ValueError, MemoryError)
