"""Post-processing for BiRefNet alpha masks (threshold, morph, feather)."""

from __future__ import annotations

from PIL import Image, ImageFilter


def post_process_mask(
    mask: Image.Image,
    *,
    threshold_pct: int = 0,
    feather_px: int = 0,
    morph: int = 0,
) -> Image.Image:
    """
    Refine a grayscale mask (0–255).

    ``threshold_pct`` 0 = off; higher = harder cut (soft remap above threshold).
    ``feather_px`` 0–5 Gaussian blur radius on the mask.
    ``morph`` -1 erode, 0 none, +1 dilate (3×3, ~1 px).
    """
    m = mask.convert("L")
    t_pct = max(0, min(100, int(threshold_pct or 0)))
    if t_pct > 0:
        import numpy as np

        arr = np.asarray(m, dtype=np.float32) / 255.0
        t = t_pct / 100.0
        arr = np.clip((arr - t) / max(1e-6, 1.0 - t), 0.0, 1.0)
        m = Image.fromarray((arr * 255.0).astype(np.uint8), mode="L")

    morph_i = int(morph or 0)
    if morph_i > 0:
        m = m.filter(ImageFilter.MaxFilter(3))
    elif morph_i < 0:
        m = m.filter(ImageFilter.MinFilter(3))

    feather = max(0, min(5, int(feather_px or 0)))
    if feather > 0:
        m = m.filter(ImageFilter.GaussianBlur(radius=float(feather)))

    return m
