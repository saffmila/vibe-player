"""
birefnet_preview_hook.py — Progress-dialog preview for BiRefNet results.

Shows the full processed image **fit** inside the preview pane (letterboxed).
"""

from __future__ import annotations

import os

from PIL import Image

from seedvr2_preview_hook import PREVIEW_CROP_H, PREVIEW_CROP_W

_CHECKER = ((0xC8, 0xC8, 0xC8), (0x96, 0x96, 0x96))
_PREVIEW_BG = (40, 40, 40)


def _flatten_for_preview(im: Image.Image, *, bg_mode: str) -> Image.Image:
    """RGBA → checkerboard RGB; RGB stays RGB."""
    mode = (bg_mode or "transparent").strip().lower()
    if im.mode == "RGBA" and mode != "color":
        w, h = im.size
        cell = max(8, min(24, min(w, h) // 16))
        base = Image.new("RGB", (w, h))
        px = base.load()
        light, dark = _CHECKER
        for y in range(h):
            for x in range(w):
                px[x, y] = light if ((x // cell) + (y // cell)) % 2 == 0 else dark
        base.paste(im, mask=im.split()[3])
        return base
    return im.convert("RGB")


def fit_image_to_preview(
    im: Image.Image,
    *,
    pane_w: int = PREVIEW_CROP_W,
    pane_h: int = PREVIEW_CROP_H,
    bg: tuple[int, int, int] = _PREVIEW_BG,
) -> Image.Image:
    """Scale image to fit inside pane (letterbox), preserving aspect ratio."""
    w, h = im.size
    if w <= 0 or h <= 0:
        return Image.new("RGB", (pane_w, pane_h), bg)
    scale = min(pane_w / w, pane_h / h)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    fitted = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (pane_w, pane_h), bg)
    canvas.paste(fitted, ((pane_w - nw) // 2, (pane_h - nh) // 2))
    return canvas


def write_file_preview(
    image_path: str,
    preview_path: str,
    *,
    bg_mode: str = "transparent",
    pane_w: int = PREVIEW_CROP_W,
    pane_h: int = PREVIEW_CROP_H,
) -> bool:
    """Write a letterboxed JPEG preview (full image visible in the pane)."""
    if not image_path or not preview_path or not os.path.isfile(image_path):
        return False
    try:
        with Image.open(image_path) as im:
            rgb = _flatten_for_preview(im, bg_mode=bg_mode)
            preview = fit_image_to_preview(rgb, pane_w=pane_w, pane_h=pane_h)
        os.makedirs(os.path.dirname(preview_path) or ".", exist_ok=True)
        tmp = f"{preview_path}.tmp.jpg"
        preview.save(tmp, format="JPEG", quality=92, optimize=True)
        try:
            os.replace(tmp, preview_path)
        except OSError:
            if os.path.isfile(preview_path):
                os.remove(preview_path)
            os.rename(tmp, preview_path)
        return True
    except Exception:
        return False


def write_input_preview(
    source_path: str,
    preview_path: str,
    *,
    pane_w: int = PREVIEW_CROP_W,
    pane_h: int = PREVIEW_CROP_H,
) -> bool:
    """Source / before preview (checkerboard if the file has alpha)."""
    return write_file_preview(
        source_path,
        preview_path,
        bg_mode="transparent",
        pane_w=pane_w,
        pane_h=pane_h,
    )


def write_result_preview(
    result_path: str,
    preview_path: str,
    *,
    bg_mode: str = "transparent",
    pane_w: int = PREVIEW_CROP_W,
    pane_h: int = PREVIEW_CROP_H,
) -> bool:
    """Processed / after preview."""
    return write_file_preview(
        result_path,
        preview_path,
        bg_mode=bg_mode,
        pane_w=pane_w,
        pane_h=pane_h,
    )
