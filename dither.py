"""Ordered (4x4 Bayer) dithering for e-ink.

Every pixel is compared with a fixed 4x4 pattern of thresholds instead of spreading error to
its neighbours (Floyd-Steinberg). The result is a regular, stable texture: the same grey
always gives the same dots, so a mirrored screen doesn't shimmer when something else
changes, partial refreshes ghost less, and the PNGs are smaller.
"""

import numpy as np
from PIL import Image

# Classic 4x4 Bayer matrix: 16 thresholds spread so neighbouring dots fill in evenly.
BAYER4 = np.array([[0, 8, 2, 10],
                   [12, 4, 14, 6],
                   [3, 11, 1, 9],
                   [15, 7, 13, 5]], dtype=np.float32)


def _pattern(h, w):
    """Thresholds in (0, 1) tiled over an h x w image."""
    reps = (-(-h // 4), -(-w // 4))
    return ((np.tile(BAYER4, reps)[:h, :w] + 0.5) / 16.0)


def bw(img):
    """Grayscale -> black and white ("1" mode) with the 4x4 pattern."""
    a = np.asarray(img.convert("L"), dtype=np.float32) / 255.0
    out = (a > _pattern(*a.shape)).astype(np.uint8) * 255
    return Image.fromarray(out, "L").convert("1")


def levels(img, n=16):
    """Grayscale -> n grey levels (the Kindle panel has 16), dithering between neighbouring
    levels with the 4x4 pattern. Returns an "L" image using only those n values."""
    step = 255.0 / (n - 1)
    v = np.asarray(img.convert("L"), dtype=np.float32) / step
    base = np.floor(v)
    q = base + ((v - base) > _pattern(*v.shape))
    out = np.clip(q, 0, n - 1) * step
    return Image.fromarray(np.round(out).astype(np.uint8), "L")
