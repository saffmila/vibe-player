"""
Load user media images as PIL Images.

Standard formats go through Pillow; PSD/PSB are composited via psd-tools
into a flat RGB/RGBA preview suitable for viewing and thumbnails.

Affinity documents (``.af``, ``.afphoto``, ``.afdesign``, ``.afpub``) are
proprietary; we extract the largest embedded PNG preview (same approach as
Explorer/Quick Look thumbnails) — no layer support.

Use ``get_pil_image_size`` for dimension-only lookups — it reads headers
(or PSD/Affinity metadata) without decoding/compositing pixel data.
"""

from __future__ import annotations

import io
import logging
import mmap
import os
import struct

from PIL import Image

PSD_FORMATS = (".psd", ".psb")
# Affinity Photo/Designer/Publisher (+ Affinity 2 unified ``.af``).
AFFINITY_FORMATS = (".af", ".afphoto", ".afdesign", ".afpub")

# Real frame timelines. MPO / multi-resolution JPEG also report ``is_animated``
# with ``n_frames > 1``, but the extra "frames" are just smaller previews
# (DJI 8K + 960×540) — playing them looks like flicker + HUD size jumps.
_ANIMATION_FORMATS = frozenset({"GIF", "WEBP", "PNG"})

_AFFINITY_MAGIC = b"\x00\xffKA"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"IEND"


def is_psd_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in PSD_FORMATS


def is_affinity_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in AFFINITY_FORMATS


def get_pil_image_size(path: str, *, apply_exif: bool = True) -> tuple[int, int]:
    """Return ``(width, height)`` without decoding full pixel data when possible.

    PSD/PSB use document size from the file header (no layer composite).
    Affinity uses the largest embedded PNG preview's IHDR.
    For JPEG EXIF orientation 5–8, width/height are swapped without loading pixels.
    """
    if is_psd_path(path):
        from psd_tools import PSDImage

        psd = PSDImage.open(path)
        return int(psd.width), int(psd.height)

    if is_affinity_path(path):
        preview = _extract_affinity_preview(path)
        return int(preview.width), int(preview.height)

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
    editable layers. Affinity returns the embedded flattened PNG preview.
    Raises on failure (same as ``Image.open`` for other formats).

    Always detach from the filesystem handle after load — leaving Image.open()
    open locks the path on Windows and freezes shutil.move / deletes.

    Animated GIF/WebP: returns the first frame only (thumbnails / static preview).
    Use ``load_pil_frames`` when the viewer should play the animation.
    """
    if is_psd_path(path):
        return _load_psd(path)
    if is_affinity_path(path):
        return _load_affinity(path)
    with Image.open(path) as image:
        image.load()
        return image.copy()


def load_pil_frames(path: str) -> tuple[list[Image.Image], list[int]]:
    """Load display frames for the image viewer.

    Returns ``(frames, durations_ms)``. Static images (PSD/Affinity) yield one
    frame and duration ``0`` (caller should not animate). Animated GIF/WebP/PNG
    yield every frame; durations are clamped to a usable playback range.

    Multi-picture containers such as MPO (common for DJI JPEGs) report multiple
    frames but are not animations — only the largest still is returned.

    Detaches from the filesystem handle after load (same Windows lock concern as
    ``load_pil_image``).
    """
    if is_psd_path(path):
        return [_load_psd(path)], [0]
    if is_affinity_path(path):
        return [_load_affinity(path)], [0]

    with Image.open(path) as image:
        n_frames = int(getattr(image, "n_frames", 1) or 1)
        fmt = (image.format or "").upper()
        is_timeline = (
            bool(getattr(image, "is_animated", False))
            and n_frames > 1
            and fmt in _ANIMATION_FORMATS
        )

        if is_timeline:
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

        if n_frames > 1:
            # MPO / multi-res still: keep the highest-resolution picture only.
            best: Image.Image | None = None
            best_area = -1
            for i in range(n_frames):
                image.seek(i)
                image.load()
                area = int(image.size[0]) * int(image.size[1])
                if area > best_area:
                    best_area = area
                    best = image.copy()
            if best is not None:
                logging.info(
                    "[image_loader] %s has %d still(s) (format=%s); using %dx%d",
                    os.path.basename(path),
                    n_frames,
                    fmt or "?",
                    best.size[0],
                    best.size[1],
                )
                return [best], [0]

        image.load()
        return [image.copy()], [0]


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


class _AffinityPreview:
    __slots__ = ("width", "height", "data")

    def __init__(self, width: int, height: int, data: bytes):
        self.width = width
        self.height = height
        self.data = data


def _load_affinity(path: str) -> Image.Image:
    preview = _extract_affinity_preview(path)
    with Image.open(io.BytesIO(preview.data)) as image:
        image.load()
        return image.copy()


def _extract_affinity_preview(path: str) -> _AffinityPreview:
    """Return the largest embedded PNG preview from an Affinity document."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise OSError(f"Could not read Affinity file: {path}") from exc
    if size < 8:
        raise OSError(f"Not an Affinity file (too small): {path}")

    with open(path, "rb") as f:
        # mmap keeps large documents off the Python heap while we scan.
        if size == 0:
            raise OSError(f"Empty Affinity file: {path}")
        try:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return _scan_affinity_pngs(mm, path)
        except (ValueError, OSError):
            # Empty / unmappable — fall back to a plain read.
            data = f.read()
            return _scan_affinity_pngs(data, path)


def _scan_affinity_pngs(data: bytes | mmap.mmap, path: str) -> _AffinityPreview:
    if len(data) < 4 or bytes(data[:4]) != _AFFINITY_MAGIC:
        # Some exporters omit / alter magic; still try PNG scan if extension matches.
        logging.debug("Affinity magic missing for %s — scanning embedded PNGs anyway", path)

    best: _AffinityPreview | None = None
    best_area = -1
    search = 0
    length = len(data)
    sig_len = len(_PNG_SIG)

    while True:
        start = data.find(_PNG_SIG, search)
        if start < 0:
            break
        # Always advance past this signature so a bad candidate cannot wedge.
        search = start + sig_len

        # IHDR follows immediately: len(4) + "IHDR"(4) + width(4) + height(4)
        # width/height sit at signature + 16 / + 20.
        if start + 24 > length:
            continue
        # Verify IHDR type tag
        if bytes(data[start + 12 : start + 16]) != b"IHDR":
            continue
        width = struct.unpack(">I", bytes(data[start + 16 : start + 20]))[0]
        height = struct.unpack(">I", bytes(data[start + 20 : start + 24]))[0]
        if width == 0 or height == 0 or width > 100_000 or height > 100_000:
            continue

        iend_at = data.find(_PNG_IEND, start)
        if iend_at < 0:
            continue
        # IEND type + 4-byte CRC
        end = iend_at + len(_PNG_IEND) + 4
        if end > length:
            continue

        area = int(width) * int(height)
        if area <= best_area:
            continue
        png_bytes = bytes(data[start:end])
        best = _AffinityPreview(width, height, png_bytes)
        best_area = area

    if best is None:
        raise OSError(f"No embedded PNG preview in Affinity file: {path}")
    return best
