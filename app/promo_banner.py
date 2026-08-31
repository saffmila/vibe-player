"""
promo_banner.py — Promo strips for SeedVR / RIFE dialogs.

Canvas + ImageTk, redrawn on ``<Configure>`` to the real pixel width.
Optional side / top padding (Preferences → Debug). Masters stay 976x132.
"""

from __future__ import annotations

import logging
import os
import tkinter as tk

_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
_NATIVE_W = 976
_NATIVE_H = 132

# Fixed dialog geometry width for SeedVR / RIFE.
PROMO_STRIP_DIALOG_W = 720
# Defaults — also Preferences → Debug (run_debug.bat / --debug).
DEFAULT_PROMO_STRIP_PAD_X = 4
DEFAULT_PROMO_STRIP_PAD_TOP = 4


def strip_height_for_width(width: int) -> int:
    w = max(120, int(width))
    return max(36, int(round(w * _NATIVE_H / _NATIVE_W)))


def resolve_promo_pads(controller=None) -> tuple[int, int]:
    """Return (pad_x, pad_top) clamped to 0..40."""
    pad_x = DEFAULT_PROMO_STRIP_PAD_X
    pad_top = DEFAULT_PROMO_STRIP_PAD_TOP
    if controller is not None:
        try:
            pad_x = int(getattr(controller, "promo_strip_pad_x", pad_x))
        except (TypeError, ValueError):
            pad_x = DEFAULT_PROMO_STRIP_PAD_X
        try:
            pad_top = int(getattr(controller, "promo_strip_pad_top", pad_top))
        except (TypeError, ValueError):
            pad_top = DEFAULT_PROMO_STRIP_PAD_TOP
    return max(0, min(40, pad_x)), max(0, min(40, pad_top))


def _bg_hex(host) -> str:
    try:
        raw = host.cget("fg_color")
        if isinstance(raw, (tuple, list)):
            raw = raw[-1]
        if isinstance(raw, str) and raw.startswith("#"):
            return raw
    except Exception:
        pass
    return "#1a1a1a"


def _bake_pixels(pil_full, pixel_w: int):
    """Resize master art to exact canvas pixel width (and proportional height)."""
    from PIL import Image, ImageTk

    w = max(120, int(pixel_w))
    h = strip_height_for_width(w)
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
    if pil_full.size != (w, h):
        pil = pil_full.resize((w, h), resample)
    else:
        pil = pil_full
    return ImageTk.PhotoImage(pil), w, h


def attach_promo_strip(
    host,
    asset_name: str,
    *,
    dialog_width: int,
    pad_x: int = DEFAULT_PROMO_STRIP_PAD_X,
    pad_top: int = DEFAULT_PROMO_STRIP_PAD_TOP,
    controller=None,
):
    """Pack a promo strip with side/top padding; image fills the inner canvas."""
    if controller is not None:
        pad_x, pad_top = resolve_promo_pads(controller)
    else:
        pad_x = max(0, min(40, int(pad_x)))
        pad_top = max(0, min(40, int(pad_top)))

    path = os.path.join(_ASSETS_DIR, asset_name)
    if not os.path.isfile(path):
        logging.debug("[promo] missing strip: %s", path)
        return None
    try:
        from PIL import Image

        pil_full = Image.open(path).convert("RGBA")
    except Exception:
        logging.debug("[promo] failed to load %s", path, exc_info=True)
        return None

    inner_w = max(120, int(dialog_width) - 2 * pad_x)
    photo, w, h = _bake_pixels(pil_full, inner_w)
    bg = _bg_hex(host)
    canvas = tk.Canvas(host, height=h, bd=0, highlightthickness=0, bg=bg)
    canvas.pack(side="top", fill="x", padx=pad_x, pady=(pad_top, 0))
    canvas.create_image(0, 0, anchor="nw", image=photo, tags="strip")

    host._promo_strip_pil_full = pil_full
    host._promo_strip_photo = photo
    host._promo_strip_lbl = canvas
    host._promo_strip_canvas = canvas
    host._promo_strip_pixel_w = w
    host._promo_strip_pad_x = pad_x
    host._promo_strip_pad_top = pad_top

    def _on_configure(event):
        if event.widget is not canvas or event.width < 80:
            return
        # event.width is already the inset canvas width (after padx).
        sync_promo_strip(host, pixel_width=int(event.width))

    canvas.bind("<Configure>", _on_configure)
    try:
        host.after_idle(lambda: sync_promo_strip(host))
        host.after(80, lambda: sync_promo_strip(host))
        host.after(200, lambda: sync_promo_strip(host))
    except Exception:
        pass
    return canvas


def sync_promo_strip(host, pixel_width: int | None = None) -> None:
    """Fit strip bitmap 1:1 to canvas pixel width (width + height together)."""
    pil_full = getattr(host, "_promo_strip_pil_full", None)
    canvas = getattr(host, "_promo_strip_canvas", None)
    if pil_full is None or canvas is None:
        return
    try:
        if pixel_width is None:
            host.update_idletasks()
            pixel_width = int(canvas.winfo_width() or 0)
            if pixel_width < 80:
                pad_x = int(getattr(host, "_promo_strip_pad_x", 0) or 0)
                pixel_width = max(120, int(host.winfo_width() or 0) - 2 * pad_x)
        if pixel_width < 80:
            return
        prev = int(getattr(host, "_promo_strip_pixel_w", 0) or 0)
        if abs(pixel_width - prev) < 2:
            return

        photo, w, h = _bake_pixels(pil_full, pixel_width)
        canvas.configure(height=h)
        canvas.delete("strip")
        canvas.create_image(0, 0, anchor="nw", image=photo, tags="strip")
        host._promo_strip_photo = photo
        host._promo_strip_pixel_w = w
    except Exception:
        logging.debug("[promo] sync failed", exc_info=True)
