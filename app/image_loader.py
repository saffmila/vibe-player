"""
Load user media images as PIL Images.

Standard formats go through Pillow; PSD/PSB are composited via psd-tools
into a flat RGB/RGBA preview suitable for viewing and thumbnails.

Use ``get_pil_image_size`` for dimension-only lookups — it reads headers
(or PSD metadata) without decoding/compositing pixel data.
"""

from __future__ import annotations

import logging
import os

from PIL import Image

PSD_FORMATS = (".psd", ".psb")


def is_psd_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in PSD_FORMATS


def get_pil_image_size(path: str, *, apply_exif: bool = True) -> tuple[int, int]:
    """Return ``(width, height)`` without decoding full pixel data when possible.

    PSD/PSB use document size from the file header (no layer composite).
    For JPEG EXIF orientation 5–8, width/height are swapped without loading pixels.
    """
    if is_psd_path(path):
        from psd_tools import PSDImage

        psd = PSDImage.open(path)
        return int(psd.width), int(psd.height)

    with Image.open(path) as im:
        w, h = im.size
        if apply_exif:
            try:
                # Avoid ImageOps.exif_transpose — without an orientation tag Pillow
                # returns image.copy(), which forces a full pixel decode.
                orientation = im.getexif().get(0x0112)
                if orientation in (5, 6, 7, 8):
                    w, h = h, w
            except Exception:
                pass
        return w, h


def load_pil_image(path: str) -> Image.Image:
    """Open an image file and return a PIL Image with pixels loaded.

    For PSD/PSB, returns a composited flatten suitable for display — not
    editable layers. Raises on failure (same as ``Image.open`` for other formats).

    Always detach from the filesystem handle after load — leaving Image.open()
    open locks the path on Windows and freezes shutil.move / deletes.

    Animated GIF/WebP: returns the first frame only (thumbnails / static preview).
    Use ``load_pil_frames`` when the viewer should play the animation.
    """
    if is_psd_path(path):
        return _load_psd(path)
    with Image.open(path) as image:
        image.load()
        return image.copy()


def load_pil_frames(path: str) -> tuple[list[Image.Image], list[int]]:
    """Load display frames for the image viewer.

    Returns ``(frames, durations_ms)``. Static images (and PSD) yield one frame
    and duration ``0`` (caller should not animate). Animated GIF/WebP yield every
    frame; durations are clamped to a usable playback range.

    Detaches from the filesystem handle after load (same Windows lock concern as
    ``load_pil_image``).
    """
    if is_psd_path(path):
        return [_load_psd(path)], [0]

    with Image.open(path) as image:
        n_frames = int(getattr(image, "n_frames", 1) or 1)
        animated = bool(getattr(image, "is_animated", False)) and n_frames > 1
        if not animated:
            image.load()
            return [image.copy()], [0]

        frames: list[Image.Image] = []
        durations: list[int] = []
        for i in range(n_frames):
            image.seek(i)
            # RGBA keeps transparency stable across frames for Tk PhotoImage.
            frame = image.convert("RGBA")
            frames.append(frame.copy())
            raw_ms = image.info.get("duration", 100)
            try:
                ms = int(raw_ms)
            except (TypeError, ValueError):
                ms = 100
            # 0 often means "default"; very low values thrash the UI timer.
            if ms <= 0:
                ms = 100
            durations.append(max(20, ms))
        return frames, durations


def _load_psd(path: str) -> Image.Image:
    from psd_tools import PSDImage

    psd = PSDImage.open(path)
    image = None
    try:
        # Prefer embedded preview / lightweight composite when available.
        image = psd.composite()
    except Exception as exc:
        logging.warning("PSD composite failed for %s: %s; trying topil()", path, exc)
        try:
            image = psd.topil()
        except Exception:
            image = None

    if image is None:
        raise OSError(f"Could not decode PSD/PSB: {path}")

    # Detach from the PSD file handle so callers can treat it like any PIL image.
    return image.copy()
