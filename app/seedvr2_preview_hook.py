"""
seedvr2_preview_hook.py — Source preview helper for SeedVR2 upscale UI.

Writes a 1:1 center crop of the current input (image or first video frame)
so the progress dialog can show which file is being processed.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Tuple

from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS

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


def _save_rgb_crop(rgb, preview_path: str, crop_w: int, crop_h: int) -> bool:
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


def write_source_preview(
    input_path: str,
    preview_path: str,
    crop_w: int = PREVIEW_CROP_W,
    crop_h: int = PREVIEW_CROP_H,
) -> bool:
    """
    Write a 1:1 center-crop JPEG of ``input_path`` to ``preview_path``.

    Images via PIL; videos via a single FFmpeg frame grab. Returns True on success.
    """
    if not input_path or not preview_path or not os.path.isfile(input_path):
        return False
    ext = os.path.splitext(input_path)[1].lower()
    try:
        from PIL import Image

        if ext in IMAGE_FORMATS:
            with Image.open(input_path) as im:
                return _save_rgb_crop(im.convert("RGB"), preview_path, crop_w, crop_h)

        if ext in VIDEO_FORMATS:
            from file_operations import get_ffmpeg_path

            ffmpeg = get_ffmpeg_path()
            if not ffmpeg or not os.path.isfile(ffmpeg):
                return False
            tmp_frame = f"{preview_path}.frame.jpg"
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                "0",
                "-i",
                input_path,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                tmp_frame,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0 or not os.path.isfile(tmp_frame):
                return False
            try:
                with Image.open(tmp_frame) as im:
                    return _save_rgb_crop(im.convert("RGB"), preview_path, crop_w, crop_h)
            finally:
                try:
                    os.remove(tmp_frame)
                except OSError:
                    pass

        return False
    except Exception as exc:
        logging.debug("[SeedVR2 Preview] source preview failed: %s", exc)
        try:
            tmp = f"{preview_path}.tmp.jpg"
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False
