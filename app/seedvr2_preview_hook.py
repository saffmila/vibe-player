"""
seedvr2_preview_hook.py — Source preview helper for SeedVR2 upscale UI.

Writes a 1:1 center crop of the current input image so the progress dialog
can show which file is being processed (especially useful in batches).

No monkeypatches / mid-inference hooks — image upscale has no progressive
frames to display until the final save.
"""

from __future__ import annotations

import logging
import os
from typing import Tuple

# Match FileOpProgressDialog preview label size (pixel-perfect crop).
PREVIEW_CROP_W = 480
PREVIEW_CROP_H = 270


def center_crop_box(
    width: int,
    height: int,
    crop_w: int = PREVIEW_CROP_W,
    crop_h: int = PREVIEW_CROP_H,
) -> Tuple[int, int, int, int]:
    """Return PIL-style (left, top, right, bottom) for a 1:1 center crop."""
    cw = max(1, int(crop_w))
    ch = max(1, int(crop_h))
    if width <= cw and height <= ch:
        return 0, 0, width, height
    x0 = max(0, (width - cw) // 2)
    y0 = max(0, (height - ch) // 2)
    return x0, y0, min(width, x0 + cw), min(height, y0 + ch)


def write_source_preview(
    input_path: str,
    preview_path: str,
    crop_w: int = PREVIEW_CROP_W,
    crop_h: int = PREVIEW_CROP_H,
) -> bool:
    """
    Write a 1:1 center-crop JPEG of ``input_path`` to ``preview_path``.

    Images only (video upscale is disabled). Returns True on success.
    """
    if not input_path or not preview_path or not os.path.isfile(input_path):
        return False
    try:
        from PIL import Image

        with Image.open(input_path) as im:
            rgb = im.convert("RGB")
            box = center_crop_box(rgb.width, rgb.height, crop_w, crop_h)
            crop = rgb.crop(box)

        os.makedirs(os.path.dirname(preview_path) or ".", exist_ok=True)
        tmp = f"{preview_path}.tmp.jpg"
        crop.save(tmp, format="JPEG", quality=92, optimize=True)
        try:
            os.replace(tmp, preview_path)
        except OSError:
            if os.path.isfile(preview_path):
                os.remove(preview_path)
            os.rename(tmp, preview_path)
        return True
    except Exception as exc:
        logging.debug("[SeedVR2 Preview] source preview failed: %s", exc)
        try:
            tmp = f"{preview_path}.tmp.jpg"
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False
