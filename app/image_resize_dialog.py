"""
Modal dialog for resizing the current image in the image viewer.

Uses CustomTkinter to match the rest of the app preferences / dialogs.
"""

from __future__ import annotations

from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk
from PIL import Image as PILImage


# Display label -> PIL resampling filter
RESAMPLE_OPTIONS: dict[str, int] = {
    "Lanczos (High Quality)": PILImage.LANCZOS,
    "Bilinear": PILImage.BILINEAR,
    "Bicubic": PILImage.BICUBIC,
    "Nearest": PILImage.NEAREST,
}

_UNIT_PIXELS = "Pixels (px)"
_UNIT_PERCENT = "Percentage (%)"


class ResizeImageDialog(ctk.CTkToplevel):
    """
    Modal resize dialog.

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
        # Convert current fields between px and % when switching units.
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
        """Best-effort pixel size from the current field values."""
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
                # Same scale on both axes when locked.
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
        # Soft ceiling to avoid accidental multi-gigapixel allocs.
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


def open_resize_image_dialog(parent, *, orig_width: int, orig_height: int, on_apply):
    """Convenience helper — creates and returns the dialog instance."""
    return ResizeImageDialog(
        parent,
        orig_width=orig_width,
        orig_height=orig_height,
        on_apply=on_apply,
    )
