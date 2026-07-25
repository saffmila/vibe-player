"""
Modal dialogs for resizing images (single viewer + batch from thumbnail grid).

Uses CustomTkinter to match the rest of the app preferences / dialogs.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk
from PIL import Image as PILImage

from image_loader import load_pil_frames


# Display label -> PIL resampling filter
RESAMPLE_OPTIONS: dict[str, int] = {
    "Lanczos (High Quality)": PILImage.LANCZOS,
    "Bilinear": PILImage.BILINEAR,
    "Bicubic": PILImage.BICUBIC,
    "Nearest": PILImage.NEAREST,
}

_UNIT_PIXELS = "Pixels (px)"
_UNIT_PERCENT = "Percentage (%)"


def prepare_image_for_save(im: PILImage.Image, path: str) -> PILImage.Image:
    """Convert modes that the target format cannot store (e.g. JPEG + alpha)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg") and im.mode in ("RGBA", "P", "LA"):
        bg = PILImage.new("RGB", im.size, (255, 255, 255))
        rgba = im.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return im


def webp_file_is_lossless(path: str) -> bool:
    """Return True when the WebP file uses lossless coding (VP8L).

    Pillow often omits ``info['lossless']`` on decode, so we sniff the RIFF
    chunk: ``VP8L`` = lossless; ``VP8 `` = lossy. Animated/extended files may
    contain both — treat presence of ``VP8L`` and absence of lossy ``VP8 `` as
    lossless.
    """
    try:
        with open(path, "rb") as f:
            # Header + enough for typical chunk table
            data = f.read(256 * 1024)
        if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            return False
        has_vp8l = b"VP8L" in data
        has_vp8 = b"VP8 " in data  # lossy bitstream (space-padded FourCC)
        if has_vp8l and not has_vp8:
            return True
        if has_vp8:
            return False
        # Fallback: Pillow flag when present
        with PILImage.open(path) as im:
            return bool(im.info.get("lossless"))
    except Exception:
        return False


def image_reencode_is_lossy(path: str) -> bool:
    """
    True when rotate/flip via Pillow will re-encode with possible quality loss.

    PNG/BMP/TIFF and lossless WebP are treated as safe (pixel-preserving codecs).
    JPEG and lossy WebP need a confirm before overwrite.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".bmp", ".tif", ".tiff"):
        return False
    if ext in (".jpg", ".jpeg"):
        return True
    if ext == ".webp":
        return not webp_file_is_lossless(path)
    if ext == ".gif":
        # Palette re-save is usually fine; still not a guaranteed bit-copy.
        return False
    return True


def save_pil_frames(frames, path: str, durations=None, *, prefer_lossless: bool | None = None) -> None:
    """Write one or more PIL frames to ``path`` (animated GIF/WebP when applicable)."""
    if not frames:
        return
    first = prepare_image_for_save(frames[0], path)
    ext = os.path.splitext(path)[1].lower()
    animated = len(frames) > 1 and ext in (".gif", ".webp")
    save_kw: dict = {}
    if animated:
        rest = [prepare_image_for_save(f, path) for f in frames[1:]]
        save_kw.update(save_all=True, append_images=rest, loop=0)
        if durations:
            save_kw["duration"] = list(durations)[: len(frames)]

    if ext in (".jpg", ".jpeg"):
        # High-quality re-encode (not true lossless jpegtran).
        save_kw.update(quality=95, subsampling=0, optimize=True)
    elif ext == ".webp":
        lossless = prefer_lossless if prefer_lossless is not None else False
        if lossless:
            save_kw.update(lossless=True, quality=100, method=6)
        else:
            save_kw.update(quality=90, method=4)
    elif ext == ".png":
        save_kw.update(compress_level=6)

    first.save(path, **save_kw)


def compute_resize_size(
    orig_w: int,
    orig_h: int,
    *,
    unit: str,
    width_val: float,
    height_val: float,
    lock_aspect: bool,
) -> tuple[int, int]:
    """Map dialog values to a target pixel size for one image."""
    ow, oh = max(1, int(orig_w)), max(1, int(orig_h))
    if unit == _UNIT_PERCENT:
        wp = float(width_val)
        hp = float(height_val) if not lock_aspect else wp
        w = max(1, int(round(ow * wp / 100.0)))
        h = max(1, int(round(oh * hp / 100.0)))
        return w, h
    # Absolute pixels — same target width; height follows each file's aspect when locked.
    w = max(1, int(round(float(width_val))))
    if lock_aspect:
        h = max(1, int(round(w * (oh / ow))))
    else:
        h = max(1, int(round(float(height_val))))
    return w, h


class ResizeImageDialog(ctk.CTkToplevel):
    """
    Modal resize dialog for a single image (viewer).

    On Apply, calls ``on_apply(width, height, resample_filter)`` with absolute
    pixel dimensions and a PIL resampling constant, then closes.
    """

    def __init__(
        self,
        parent,
        *,
        orig_width: int,
        orig_height: int,
        on_apply: Callable[[int, int, int], None],
        title: str = "Resize Image",
    ):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.result: Optional[tuple[int, int, int]] = None
        self._on_apply = on_apply
        self._orig_w = max(1, int(orig_width))
        self._orig_h = max(1, int(orig_height))
        self._aspect = self._orig_w / self._orig_h
        self._lock_aspect = True
        self._syncing = False
        self._unit = _UNIT_PIXELS

        try:
            self.transient(parent.winfo_toplevel())
        except Exception:
            pass

        mp = (self._orig_w * self._orig_h) / 1_000_000.0
        pad = {"padx": 16, "pady": 8}

        ctk.CTkLabel(
            self,
            text=f"Original: {self._orig_w} × {self._orig_h} px ({mp:.2f} MP)",
            font=ctk.CTkFont(size=13),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(16, 4))

        unit_row = ctk.CTkFrame(self, fg_color="transparent")
        unit_row.pack(fill="x", **pad)
        ctk.CTkLabel(unit_row, text="Units:", width=70, anchor="w").pack(side="left")
        self._unit_var = tk.StringVar(value=_UNIT_PIXELS)
        self._unit_menu = ctk.CTkOptionMenu(
            unit_row,
            variable=self._unit_var,
            values=[_UNIT_PIXELS, _UNIT_PERCENT],
            command=self._on_unit_change,
            width=160,
            height=28,
            corner_radius=6,
        )
        self._unit_menu.pack(side="left", padx=(8, 0))

        size_row = ctk.CTkFrame(self, fg_color="transparent")
        size_row.pack(fill="x", **pad)

        ctk.CTkLabel(size_row, text="Width:", width=55, anchor="w").pack(side="left")
        self._w_var = tk.StringVar(value=str(self._orig_w))
        self._w_entry = ctk.CTkEntry(
            size_row, textvariable=self._w_var, width=90, height=28, corner_radius=6, justify="center"
        )
        self._w_entry.pack(side="left", padx=(4, 8))
        self._w_entry.bind("<KeyRelease>", lambda e: self._on_width_edit())
        self._w_entry.bind("<FocusOut>", lambda e: self._on_width_edit())

        self._lock_btn = ctk.CTkButton(
            size_row,
            text="🔗",
            width=36,
            height=28,
            corner_radius=6,
            fg_color="gray30",
            hover_color="gray25",
            command=self._toggle_lock,
        )
        self._lock_btn.pack(side="left", padx=4)

        ctk.CTkLabel(size_row, text="Height:", width=55, anchor="w").pack(side="left", padx=(8, 0))
        self._h_var = tk.StringVar(value=str(self._orig_h))
        self._h_entry = ctk.CTkEntry(
            size_row, textvariable=self._h_var, width=90, height=28, corner_radius=6, justify="center"
        )
        self._h_entry.pack(side="left", padx=(4, 0))
        self._h_entry.bind("<KeyRelease>", lambda e: self._on_height_edit())
        self._h_entry.bind("<FocusOut>", lambda e: self._on_height_edit())

        method_row = ctk.CTkFrame(self, fg_color="transparent")
        method_row.pack(fill="x", **pad)
        ctk.CTkLabel(method_row, text="Resample:", width=70, anchor="w").pack(side="left")
        self._method_var = tk.StringVar(value="Lanczos (High Quality)")
        ctk.CTkOptionMenu(
            method_row,
            variable=self._method_var,
            values=list(RESAMPLE_OPTIONS.keys()),
            width=200,
            height=28,
            corner_radius=6,
        ).pack(side="left", padx=(8, 0))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(12, 16))
        ctk.CTkButton(
            btn_row,
            text="Cancel",
            width=100,
            height=30,
            corner_radius=6,
            fg_color="gray30",
            hover_color="gray25",
            command=self._on_cancel,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btn_row,
            text="Apply",
            width=100,
            height=30,
            corner_radius=6,
            command=self._on_ok,
        ).pack(side="right")

        self.bind("<Escape>", lambda e: self._on_cancel())
        self.bind("<Return>", lambda e: self._on_ok())

        self.update_idletasks()
        self._center_on_parent(parent)
        self.lift()
        self.focus_force()
        self.after(10, self._grab_modal)
        self.after(30, lambda: self._w_entry.focus_set())

    def _grab_modal(self):
        try:
            self.grab_set()
        except Exception:
            pass

    def _center_on_parent(self, parent):
        try:
            self.update_idletasks()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            w = self.winfo_reqwidth()
            h = self.winfo_reqheight()
            x = px + max(0, (pw - w) // 2)
            y = py + max(0, (ph - h) // 2)
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _toggle_lock(self):
        self._lock_aspect = not self._lock_aspect
        self._lock_btn.configure(text="🔗" if self._lock_aspect else "🔓")
        if self._lock_aspect:
            self._on_width_edit()

    def _on_unit_change(self, value: str):
        if self._syncing:
            return
        self._syncing = True
        try:
            w_px, h_px = self._current_pixels()
            self._unit = value
            if value == _UNIT_PERCENT:
                self._w_var.set(str(round(100.0 * w_px / self._orig_w, 2)))
                self._h_var.set(str(round(100.0 * h_px / self._orig_h, 2)))
            else:
                self._w_var.set(str(w_px))
                self._h_var.set(str(h_px))
        finally:
            self._syncing = False

    def _parse_positive(self, text: str) -> Optional[float]:
        try:
            v = float(str(text).strip().replace(",", "."))
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        return v

    def _current_pixels(self) -> tuple[int, int]:
        wv = self._parse_positive(self._w_var.get())
        hv = self._parse_positive(self._h_var.get())
        if self._unit == _UNIT_PERCENT:
            w = int(round(self._orig_w * ((wv or 100.0) / 100.0)))
            h = int(round(self._orig_h * ((hv or 100.0) / 100.0)))
        else:
            w = int(round(wv or self._orig_w))
            h = int(round(hv or self._orig_h))
        return max(1, w), max(1, h)

    def _on_width_edit(self):
        if self._syncing or not self._lock_aspect:
            return
        wv = self._parse_positive(self._w_var.get())
        if wv is None:
            return
        self._syncing = True
        try:
            if self._unit == _UNIT_PERCENT:
                self._h_var.set(self._w_var.get().strip())
            else:
                h = max(1, int(round(wv / self._aspect)))
                self._h_var.set(str(h))
        finally:
            self._syncing = False

    def _on_height_edit(self):
        if self._syncing or not self._lock_aspect:
            return
        hv = self._parse_positive(self._h_var.get())
        if hv is None:
            return
        self._syncing = True
        try:
            if self._unit == _UNIT_PERCENT:
                self._w_var.set(self._h_var.get().strip())
            else:
                w = max(1, int(round(hv * self._aspect)))
                self._w_var.set(str(w))
        finally:
            self._syncing = False

    def _on_cancel(self):
        self.result = None
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _on_ok(self):
        w, h = self._current_pixels()
        if w < 1 or h < 1:
            return
        if w > 50000 or h > 50000:
            return
        label = self._method_var.get()
        filt = RESAMPLE_OPTIONS.get(label, PILImage.LANCZOS)
        self.result = (w, h, filt)
        try:
            self._on_apply(w, h, filt)
        finally:
            try:
                self.grab_release()
            except Exception:
                pass
            self.destroy()

    def force_close(self):
        """Close without applying (used when navigating away after confirm)."""
        self.result = None
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            if self.winfo_exists():
                self.destroy()
        except Exception:
            pass


class BatchResizeImageDialog(ctk.CTkToplevel):
    """
    Resize dialog for multiple files from the thumbnail grid.

    Defaults to percentage so mixed resolutions stay proportional.
    ``on_apply(unit, width_val, height_val, lock_aspect, resample_filter)``.
    """

    def __init__(
        self,
        parent,
        *,
        paths: list[str],
        on_apply: Callable[[str, float, float, bool, int], None],
        title: str = "Resize Images",
    ):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self._paths = list(paths)
        self._on_apply = on_apply
        self._lock_aspect = True
        self._syncing = False
        self._unit = _UNIT_PERCENT
        self._ref_w, self._ref_h = 1920, 1080
        if self._paths:
            try:
                from image_loader import get_pil_image_size

                self._ref_w, self._ref_h = get_pil_image_size(self._paths[0])
            except Exception:
                pass
        self._aspect = self._ref_w / max(1, self._ref_h)

        try:
            self.transient(parent.winfo_toplevel())
        except Exception:
            pass

        n = len(self._paths)
        names = [os.path.basename(p) for p in self._paths[:3]]
        extra = n - len(names)
        sample = ", ".join(names) + (f" +{extra} more" if extra > 0 else "")

        pad = {"padx": 16, "pady": 8}
        ctk.CTkLabel(
            self,
            text=f"Apply to {n} images",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(16, 2))
        ctk.CTkLabel(
            self,
            text=sample,
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
            wraplength=420,
        ).pack(fill="x", padx=16, pady=(0, 8))

        unit_row = ctk.CTkFrame(self, fg_color="transparent")
        unit_row.pack(fill="x", **pad)
        ctk.CTkLabel(unit_row, text="Units:", width=70, anchor="w").pack(side="left")
        self._unit_var = tk.StringVar(value=_UNIT_PERCENT)
        ctk.CTkOptionMenu(
            unit_row,
            variable=self._unit_var,
            values=[_UNIT_PERCENT, _UNIT_PIXELS],
            command=self._on_unit_change,
            width=160,
            height=28,
            corner_radius=6,
        ).pack(side="left", padx=(8, 0))

        size_row = ctk.CTkFrame(self, fg_color="transparent")
        size_row.pack(fill="x", **pad)
        ctk.CTkLabel(size_row, text="Width:", width=55, anchor="w").pack(side="left")
        self._w_var = tk.StringVar(value="100")
        self._w_entry = ctk.CTkEntry(
            size_row, textvariable=self._w_var, width=90, height=28, corner_radius=6, justify="center"
        )
        self._w_entry.pack(side="left", padx=(4, 8))
        self._w_entry.bind("<KeyRelease>", lambda e: self._on_width_edit())
        self._w_entry.bind("<FocusOut>", lambda e: self._on_width_edit())

        self._lock_btn = ctk.CTkButton(
            size_row,
            text="🔗",
            width=36,
            height=28,
            corner_radius=6,
            fg_color="gray30",
            hover_color="gray25",
            command=self._toggle_lock,
        )
        self._lock_btn.pack(side="left", padx=4)

        ctk.CTkLabel(size_row, text="Height:", width=55, anchor="w").pack(side="left", padx=(8, 0))
        self._h_var = tk.StringVar(value="100")
        self._h_entry = ctk.CTkEntry(
            size_row, textvariable=self._h_var, width=90, height=28, corner_radius=6, justify="center"
        )
        self._h_entry.pack(side="left", padx=(4, 0))
        self._h_entry.bind("<KeyRelease>", lambda e: self._on_height_edit())
        self._h_entry.bind("<FocusOut>", lambda e: self._on_height_edit())

        method_row = ctk.CTkFrame(self, fg_color="transparent")
        method_row.pack(fill="x", **pad)
        ctk.CTkLabel(method_row, text="Resample:", width=70, anchor="w").pack(side="left")
        self._method_var = tk.StringVar(value="Lanczos (High Quality)")
        ctk.CTkOptionMenu(
            method_row,
            variable=self._method_var,
            values=list(RESAMPLE_OPTIONS.keys()),
            width=200,
            height=28,
            corner_radius=6,
        ).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(
            self,
            text=(
                "Percentage keeps each image’s aspect when linked. "
                "Pixels applies the same absolute size to every file."
            ),
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
            wraplength=420,
            justify="left",
        ).pack(fill="x", padx=16, pady=(0, 4))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(12, 16))
        ctk.CTkButton(
            btn_row,
            text="Cancel",
            width=100,
            height=30,
            corner_radius=6,
            fg_color="gray30",
            hover_color="gray25",
            command=self._on_cancel,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btn_row,
            text="Apply",
            width=100,
            height=30,
            corner_radius=6,
            command=self._on_ok,
        ).pack(side="right")

        self.bind("<Escape>", lambda e: self._on_cancel())
        self.bind("<Return>", lambda e: self._on_ok())
        self.update_idletasks()
        self._center_on_parent(parent)
        self.lift()
        self.focus_force()
        self.after(10, lambda: self.grab_set())
        self.after(30, lambda: self._w_entry.focus_set())

    def _center_on_parent(self, parent):
        try:
            self.update_idletasks()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            w = self.winfo_reqwidth()
            h = self.winfo_reqheight()
            self.geometry(f"+{px + max(0, (pw - w) // 2)}+{py + max(0, (ph - h) // 2)}")
        except Exception:
            pass

    def _toggle_lock(self):
        self._lock_aspect = not self._lock_aspect
        self._lock_btn.configure(text="🔗" if self._lock_aspect else "🔓")
        if self._lock_aspect:
            self._on_width_edit()

    def _parse_positive(self, text: str) -> Optional[float]:
        try:
            v = float(str(text).strip().replace(",", "."))
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        return v

    def _on_unit_change(self, value: str):
        if self._syncing:
            return
        self._syncing = True
        try:
            self._unit = value
            if value == _UNIT_PERCENT:
                self._w_var.set("100")
                self._h_var.set("100")
            else:
                self._w_var.set(str(self._ref_w))
                self._h_var.set(str(self._ref_h))
        finally:
            self._syncing = False

    def _on_width_edit(self):
        if self._syncing or not self._lock_aspect:
            return
        wv = self._parse_positive(self._w_var.get())
        if wv is None:
            return
        self._syncing = True
        try:
            if self._unit == _UNIT_PERCENT:
                self._h_var.set(self._w_var.get().strip())
            else:
                self._h_var.set(str(max(1, int(round(wv / self._aspect)))))
        finally:
            self._syncing = False

    def _on_height_edit(self):
        if self._syncing or not self._lock_aspect:
            return
        hv = self._parse_positive(self._h_var.get())
        if hv is None:
            return
        self._syncing = True
        try:
            if self._unit == _UNIT_PERCENT:
                self._w_var.set(self._h_var.get().strip())
            else:
                self._w_var.set(str(max(1, int(round(hv * self._aspect)))))
        finally:
            self._syncing = False

    def _on_cancel(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _on_ok(self):
        wv = self._parse_positive(self._w_var.get())
        hv = self._parse_positive(self._h_var.get())
        if wv is None or hv is None:
            return
        if self._unit == _UNIT_PIXELS and (wv > 50000 or hv > 50000):
            return
        filt = RESAMPLE_OPTIONS.get(self._method_var.get(), PILImage.LANCZOS)
        unit = self._unit_var.get()
        try:
            self._on_apply(unit, float(wv), float(hv), self._lock_aspect, filt)
        finally:
            try:
                self.grab_release()
            except Exception:
                pass
            self.destroy()


def open_resize_image_dialog(parent, *, orig_width: int, orig_height: int, on_apply):
    """Convenience helper — creates and returns the dialog instance."""
    return ResizeImageDialog(
        parent,
        orig_width=orig_width,
        orig_height=orig_height,
        on_apply=on_apply,
    )


def open_batch_resize_dialog(parent, paths: list[str], on_apply):
    """Open batch resize for ``paths`` (2+ images)."""
    return BatchResizeImageDialog(parent, paths=paths, on_apply=on_apply)


def resize_image_file(
    path: str,
    *,
    unit: str,
    width_val: float,
    height_val: float,
    lock_aspect: bool,
    resample_filter,
) -> tuple[int, int]:
    """
    Resize one image on disk in place. Returns ``(new_w, new_h)``.

    Animated GIF/WebP: every frame is resized with the same box.
    """
    frames, durations = load_pil_frames(path)
    if not frames:
        raise ValueError("no frames")
    ow, oh = frames[0].size
    nw, nh = compute_resize_size(
        ow,
        oh,
        unit=unit,
        width_val=width_val,
        height_val=height_val,
        lock_aspect=lock_aspect,
    )
    if nw > 50000 or nh > 50000:
        raise ValueError("target size too large")
    filt = resample_filter if resample_filter is not None else PILImage.LANCZOS
    out = [im.resize((nw, nh), filt) for im in frames]
    save_pil_frames(out, path, durations)
    return nw, nh


# Discrete file transforms (grid RMB / batch). Keys match start_image_transform_from_grid.
IMAGE_TRANSFORM_OPS = {
    "rotate_left": lambda im: im.rotate(90, expand=True),
    "rotate_right": lambda im: im.rotate(-90, expand=True),
    "flip_h": lambda im: im.transpose(PILImage.FLIP_LEFT_RIGHT),
    "flip_v": lambda im: im.transpose(PILImage.FLIP_TOP_BOTTOM),
}

IMAGE_TRANSFORM_LABELS = {
    "rotate_left": "Rotate Left",
    "rotate_right": "Rotate Right",
    "flip_h": "Flip Horizontal",
    "flip_v": "Flip Vertical",
}


def transform_image_file(path: str, op: str) -> None:
    """Apply rotate/flip to one image on disk (all frames when animated).

    Uses lossless WebP/PNG-friendly save settings when the source allows it.
    JPEG / lossy WebP are high-quality re-encodes (not jpegtran-lossless).
    """
    fn = IMAGE_TRANSFORM_OPS.get(op)
    if fn is None:
        raise ValueError(f"unknown transform: {op}")
    ext = os.path.splitext(path)[1].lower()
    prefer_lossless = None
    if ext == ".webp":
        prefer_lossless = webp_file_is_lossless(path)
    frames, durations = load_pil_frames(path)
    if not frames:
        raise ValueError("no frames")
    out = [fn(im) for im in frames]
    save_pil_frames(out, path, durations, prefer_lossless=prefer_lossless)
