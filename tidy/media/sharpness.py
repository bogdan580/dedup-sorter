"""Sharpness metric: variance of the Laplacian.

Plain numpy convolution -- no scipy/opencv dependency. A blurry/
motion-shaken photo has a smooth intensity surface (small second
derivatives everywhere -> low variance); a sharp photo has strong edges
(large second derivatives at edges -> high variance). Used as one term in
series.py's keeper-suggestion ranking.
"""
# Series keeper rules: docs/business/rules/events-and-series.md
from __future__ import annotations

import numpy as np
from PIL import Image

_LAPLACIAN_KERNEL = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float64)

# See: docs/business/rules/events-and-series.md
_DEFAULT_MAX_DIM = 512


def _convolve2d_same(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """'same'-mode 2D convolution via edge-padding + shifted-slice sums --
    avoids adding scipy as a dependency just for a single 3x3 kernel."""
    kh, kw = kernel.shape
    pad_h, pad_w = kh // 2, kw // 2
    padded = np.pad(image, ((pad_h, pad_h), (pad_w, pad_w)), mode="edge")
    out = np.zeros_like(image, dtype=np.float64)
    for i in range(kh):
        for j in range(kw):
            weight = kernel[i, j]
            if weight == 0:
                continue
            out += weight * padded[i:i + image.shape[0], j:j + image.shape[1]]
    return out


def laplacian_variance(image: Image.Image, *, max_dim: int = _DEFAULT_MAX_DIM) -> float:
    grayscale = image.convert("L")
    grayscale.thumbnail((max_dim, max_dim))
    pixels = np.asarray(grayscale, dtype=np.float64)

    laplacian = _convolve2d_same(pixels, _LAPLACIAN_KERNEL)
    return float(laplacian.var())
