"""Perceptual hash: 64-bit dHash + Hamming distance. Built on Pillow +
numpy only -- both already installed, no new dependency.

Operates on an already-decoded `PIL.Image.Image`, not a path/stream: the
caller (series.py) opens each candidate once and reuses the same Image for
dHash, sharpness.py's Laplacian variance, resolution and EXIF presence --
avoiding three separate decodes of the same file.
"""
# Series rules: docs/business/rules/events-and-series.md
from __future__ import annotations

import numpy as np
from PIL import Image

# 9 columns x 8 rows: each row contributes 8 adjacent-pixel comparisons,
# 8 rows x 8 comparisons = 64 bits total.
_HASH_WIDTH = 9
_HASH_HEIGHT = 8


def compute_dhash(image: Image.Image) -> int:
    """Difference hash: shrink to 9x8 grayscale, compare each pixel to its
    right neighbor, pack the 64 booleans into one int (row-major, MSB
    first). Two images of the same scene -- different compression, minor
    exposure/crop differences -- produce hashes with a small Hamming
    distance; unrelated images produce roughly-random, far-apart hashes."""
    grayscale = image.convert("L").resize((_HASH_WIDTH, _HASH_HEIGHT), Image.LANCZOS)
    pixels = np.asarray(grayscale, dtype=np.int16)

    diff = pixels[:, 1:] > pixels[:, :-1]  # shape (8, 8): 64 booleans

    value = 0
    for bit in diff.flatten():
        value = (value << 1) | int(bit)
    return value


def hamming_distance(a: int, b: int) -> int:
    """Number of differing bits between two dHash values."""
    return bin(a ^ b).count("1")
