"""
Inline crop overlay + bottom HUD toolbar for ``ImageViewerLegacy``.

Crop geometry is stored in original-image pixel space and remapped to the
canvas on every pan / zoom / resize. The toolbar is a CTk frame placed at
the bottom of the viewer window (works windowed and overrideredirect fullscreen).
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image as PILImage


# Aspect preset label -> width/height ratio (None = free).
ASPECT_PRESETS: dict[str, Optional[float]] = {
    "Free": None,
    "1:1": 1.0,
    "16:9": 16.0 / 9.0,
    "4:3": 4.0 / 3.0,
    "9:16": 9.0 / 16.0,
    "3:2": 3.0 / 2.0,
}

# Known landscape/portrait pairs for the swap button.
_ASPECT_SWAP_PAIRS = {
    "16:9": "9:16",
    "9:16": "16:9",
    "1:1": "1:1",
    "Free": "Free",
}

_HANDLE_SIZE = 7  # canvas pixels
_HANDLE_HIT = 8
_MIN_CROP_PX = 8
_CROP_TAG = "crop_overlay"

# Match main app toolbar button look (gui_elements: gray30 / CTk blue).
_HUD_BG = "#252525"
_BTN_FG = "gray30"
_BTN_HOVER = "gray25"
_BTN_PRIMARY = "#1f6aa5"
_BTN_PRIMARY_HOVER = "#144870"
_ENTRY_FG = "#2b2b2b"
_CORNER = 6
_CTRL_H = 28


class CropOverlayHUD(ctk.CTkFrame):
    """Bottom toolbar: resolution, aspect, swap, cancel, apply (+ drop-up menu)."""

    def __init__(
        self,
        master,
        *,
        on_size_change: Callable[[int, int], None],
        on_aspect_change: Callable[[str], None],
        on_swap: Callable[[], None],
        on_cancel: Callable[[], None],
        on_apply_overwrite: Callable[[], None],
        on_apply_copy: Callable[[], None],
        on_apply_clipboard: Callable[[], None],
    ):
        super().__init__(master, fg_color=_HUD_BG, corner_radius=0, height=48)
        self._on_size_change = on_size_change
        self._on_aspect_change = on_aspect_change
        self._on_swap = on_swap
        self._on_cancel = on_cancel
        self._on_apply_overwrite = on_apply_overwrite
        self._on_apply_copy = on_apply_copy
        self._on_apply_clipboard = on_apply_clipboard
        self._syncing = False

        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=14, pady=8)

        ctk.CTkLabel(inner, text="W", text_color="#cccccc", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 6)
        )
        self.width_var = tk.StringVar(value="0")
        self.width_entry = ctk.CTkEntry(
            inner,
            textvariable=self.width_var,
            width=72,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_ENTRY_FG,
            border_width=0,
            justify="center",
            font=ctk.CTkFont(size=12),
        )
        self.width_entry.pack(side="left", padx=(0, 10))
        self.width_entry.bind("<Return>", self._commit_size)
        self.width_entry.bind("<FocusOut>", self._commit_size)

        ctk.CTkLabel(inner, text="×", text_color="#888888", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 10)
        )

        ctk.CTkLabel(inner, text="H", text_color="#cccccc", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 6)
        )
        self.height_var = tk.StringVar(value="0")
        self.height_entry = ctk.CTkEntry(
            inner,
            textvariable=self.height_var,
            width=72,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_ENTRY_FG,
            border_width=0,
            justify="center",
            font=ctk.CTkFont(size=12),
        )
        self.height_entry.pack(side="left", padx=(0, 14))
        self.height_entry.bind("<Return>", self._commit_size)
        self.height_entry.bind("<FocusOut>", self._commit_size)

        self.aspect_var = tk.StringVar(value="Free")
        self.aspect_menu = ctk.CTkOptionMenu(
            inner,
            variable=self.aspect_var,
            values=list(ASPECT_PRESETS.keys()),
            command=self._aspect_chosen,
            width=96,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_FG,
            button_color=_BTN_FG,
            button_hover_color=_BTN_HOVER,
            dropdown_fg_color=_ENTRY_FG,
            font=ctk.CTkFont(size=12),
        )
        self.aspect_menu.pack(side="left", padx=(0, 10))

        self.swap_btn = ctk.CTkButton(
            inner,
            text="↔",
            command=self._on_swap,
            width=36,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_FG,
            hover_color=_BTN_HOVER,
            font=ctk.CTkFont(size=14),
        )
        self.swap_btn.pack(side="left", padx=(0, 14))

        ctk.CTkFrame(inner, width=1, height=22, fg_color="#555555").pack(
            side="left", padx=(0, 14)
        )

        self.cancel_btn = ctk.CTkButton(
            inner,
            text="Cancel",
            command=self._on_cancel,
            width=78,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_FG,
            hover_color=_BTN_HOVER,
            font=ctk.CTkFont(size=12),
        )
        self.cancel_btn.pack(side="left", padx=(0, 10))

        apply_wrap = ctk.CTkFrame(inner, fg_color="transparent")
        apply_wrap.pack(side="left", padx=(0, 4))

        self.apply_btn = ctk.CTkButton(
            apply_wrap,
            text="Crop",
            command=self._on_apply_overwrite,
            width=72,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_PRIMARY,
            hover_color=_BTN_PRIMARY_HOVER,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.apply_btn.pack(side="left", padx=(0, 4))

        self.menu_btn = ctk.CTkButton(
            apply_wrap,
            text="▴",
            command=self._post_apply_menu,
            width=32,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_PRIMARY,
            hover_color=_BTN_PRIMARY_HOVER,
            font=ctk.CTkFont(size=11),
        )
        self.menu_btn.pack(side="left")

        self._apply_menu = tk.Menu(
            self,
            tearoff=0,
            bg="#2d2d2d",
            fg="white",
            activebackground=_BTN_PRIMARY,
            activeforeground="white",
            font=("Segoe UI", 9),
        )
        self._apply_menu.add_command(label="Apply (Overwrite)", command=self._on_apply_overwrite)
        self._apply_menu.add_command(label="Save as Copy", command=self._on_apply_copy)
        self._apply_menu.add_command(label="Copy to Clipboard", command=self._on_apply_clipboard)

    def _aspect_chosen(self, value: str):
        self._on_aspect_change(value)

    def _commit_size(self, event=None):
        if self._syncing:
            return "break" if event and getattr(event, "keysym", "") == "Return" else None
        try:
            w = int(self.width_var.get().strip())
            h = int(self.height_var.get().strip())
        except ValueError:
            return "break" if event else None
        if w < _MIN_CROP_PX or h < _MIN_CROP_PX:
            return "break" if event else None
        self._on_size_change(w, h)
        return "break" if event and getattr(event, "keysym", "") == "Return" else None

    def _post_apply_menu(self):
        self.update_idletasks()
        x = self.apply_btn.winfo_rootx()
        y = self.apply_btn.winfo_rooty()
        try:
            self._apply_menu.tk_popup(x, y - 4)
            self.update_idletasks()
            mh = self._apply_menu.winfo_reqheight()
            self._apply_menu.unpost()
            self._apply_menu.tk_popup(x, max(0, y - mh - 2))
        finally:
            self._apply_menu.grab_release()

    def set_size_fields(self, width: int, height: int):
        """Update W/H entries without firing size-change callbacks."""
        self._syncing = True
        try:
            self.width_var.set(str(int(width)))
            self.height_var.set(str(int(height)))
        finally:
            self._syncing = False

    def set_aspect(self, label: str):
        if label in ASPECT_PRESETS:
            self.aspect_var.set(label)


class CropModeController:
    """
    Owns crop state and canvas interaction for an ``ImageViewerLegacy`` instance.

    The viewer must expose: ``image_window``, ``canvas``, ``canvas_image``,
    ``original_image``, ``_anim_frames``, ``_anim_durations``, ``_is_animated``,
    ``_stop_animation``, ``_start_animation_if_needed``, ``_map_anim_frames``,
    ``update_image``, ``image_path``, ``_refresh_overlays``, ``zoom_factor``.
    """

    def __init__(self, viewer):
        self.v = viewer
        self.active = False
        self.rect = None  # (x0, y0, x1, y1) in image pixels, x1>x0, y1>y0
        self.aspect_label = "Free"
        self.hud: Optional[CropOverlayHUD] = None
        self._drag = None  # dict describing current pointer drag
        self._was_animated = False
        # Bind once; handlers no-op while inactive (avoids stacking add="+" on re-enter).
        canvas = viewer.canvas
        canvas.bind("<ButtonPress-1>", self._on_press, add="+")
        canvas.bind("<B1-Motion>", self._on_drag, add="+")
        canvas.bind("<ButtonRelease-1>", self._on_release, add="+")
        canvas.bind("<Motion>", self._on_motion, add="+")

    # ------------------------------------------------------------------ enter / exit

    def enter(self):
        if self.active:
            return
        v = self.v
        iw, ih = v.original_image.size
        if iw < _MIN_CROP_PX or ih < _MIN_CROP_PX:
            return

        self.active = True
        self.aspect_label = "Free"
        # Default: 80% centered box.
        rw, rh = max(_MIN_CROP_PX, int(iw * 0.8)), max(_MIN_CROP_PX, int(ih * 0.8))
        x0 = (iw - rw) // 2
        y0 = (ih - rh) // 2
        self.rect = (x0, y0, x0 + rw, y0 + rh)

        self._was_animated = v._is_animated()
        if self._was_animated:
            v._stop_animation()

        self.hud = CropOverlayHUD(
            v.image_window,
            on_size_change=self._on_hud_size,
            on_aspect_change=self._on_hud_aspect,
            on_swap=self._on_hud_swap,
            on_cancel=self.exit,
            on_apply_overwrite=lambda: self.apply("overwrite"),
            on_apply_copy=lambda: self.apply("copy"),
            on_apply_clipboard=lambda: self.apply("clipboard"),
        )
        self.hud.place(relx=0.0, rely=1.0, relwidth=1.0, anchor="sw")
        self.hud.set_size_fields(rw, rh)
        self.hud.set_aspect("Free")
        self.hud.lift()

        self.redraw()
        v._refresh_overlays()

    def exit(self):
        if not self.active:
            return
        v = self.v
        self.active = False
        self._drag = None
        self.rect = None

        canvas = v.canvas
        canvas.delete(_CROP_TAG)
        canvas.config(cursor="arrow")

        if self.hud is not None:
            try:
                self.hud.place_forget()
                self.hud.destroy()
            except tk.TclError:
                pass
            self.hud = None

        if self._was_animated and v._is_animated():
            v._start_animation_if_needed()
        self._was_animated = False
        v._refresh_overlays()

    # ------------------------------------------------------------------ coords

    def _image_canvas_bbox(self):
        return self.v.canvas.bbox(self.v.canvas_image)

    def img_to_canvas(self, ix: float, iy: float):
        bbox = self._image_canvas_bbox()
        if not bbox:
            return 0.0, 0.0
        x0, y0, x1, y1 = bbox
        iw, ih = self.v.original_image.size
        if iw <= 0 or ih <= 0:
            return x0, y0
        cx = x0 + (ix / iw) * (x1 - x0)
        cy = y0 + (iy / ih) * (y1 - y0)
        return cx, cy

    def canvas_to_img(self, cx: float, cy: float):
        bbox = self._image_canvas_bbox()
        if not bbox:
            return 0.0, 0.0
        x0, y0, x1, y1 = bbox
        iw, ih = self.v.original_image.size
        bw, bh = (x1 - x0), (y1 - y0)
        if bw <= 0 or bh <= 0 or iw <= 0 or ih <= 0:
            return 0.0, 0.0
        ix = (cx - x0) / bw * iw
        iy = (cy - y0) / bh * ih
        return ix, iy

    def _clamp_rect(self, x0, y0, x1, y1):
        iw, ih = self.v.original_image.size
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        # Keep size, shift into bounds when possible.
        w = max(_MIN_CROP_PX, x1 - x0)
        h = max(_MIN_CROP_PX, y1 - y0)
        w = min(w, iw)
        h = min(h, ih)
        x0 = max(0, min(x0, iw - w))
        y0 = max(0, min(y0, ih - h))
        return (int(round(x0)), int(round(y0)), int(round(x0 + w)), int(round(y0 + h)))

    def _aspect_ratio(self) -> Optional[float]:
        return ASPECT_PRESETS.get(self.aspect_label)

    def _apply_aspect_to_rect(self, x0, y0, x1, y1, *, anchor="center"):
        """Force current aspect onto a box; keep center unless anchor is a handle name."""
        ratio = self._aspect_ratio()
        if ratio is None:
            return self._clamp_rect(x0, y0, x1, y1)

        iw, ih = self.v.original_image.size
        w = max(_MIN_CROP_PX, abs(x1 - x0))
        h = max(_MIN_CROP_PX, abs(y1 - y0))
        # Fit to ratio using the larger dimension that still fits.
        if w / h > ratio:
            w = h * ratio
        else:
            h = w / ratio
        w = min(w, iw)
        h = min(h, ih)
        # Re-fit if the other dim overflowed.
        if w / ratio > ih:
            h = ih
            w = h * ratio
        if h * ratio > iw:
            w = iw
            h = w / ratio

        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0

        if anchor == "center":
            nx0, ny0 = cx - w / 2, cy - h / 2
        elif anchor in ("nw",):
            nx0, ny0 = x0, y0
        elif anchor in ("ne",):
            nx0, ny0 = x1 - w, y0
        elif anchor in ("se",):
            nx0, ny0 = x1 - w, y1 - h
        elif anchor in ("sw",):
            nx0, ny0 = x0, y1 - h
        elif anchor == "n":
            nx0, ny0 = cx - w / 2, y0
        elif anchor == "s":
            nx0, ny0 = cx - w / 2, y1 - h
        elif anchor == "e":
            nx0, ny0 = x1 - w, cy - h / 2
        elif anchor == "w":
            nx0, ny0 = x0, cy - h / 2
        else:
            nx0, ny0 = cx - w / 2, cy - h / 2

        return self._clamp_rect(nx0, ny0, nx0 + w, ny0 + h)

    # ------------------------------------------------------------------ draw

    def redraw(self):
        if not self.active or self.rect is None:
            return
        v = self.v
        canvas = v.canvas
        canvas.delete(_CROP_TAG)

        bbox = self._image_canvas_bbox()
        if not bbox:
            return
        img_x0, img_y0, img_x1, img_y1 = bbox
        x0, y0, x1, y1 = self.rect
        cx0, cy0 = self.img_to_canvas(x0, y0)
        cx1, cy1 = self.img_to_canvas(x1, y1)

        # Dim outside crop (four stippled rects covering the image area).
        dim = dict(fill="#000000", outline="", stipple="gray50", tags=_CROP_TAG)
        canvas.create_rectangle(img_x0, img_y0, img_x1, cy0, **dim)  # top
        canvas.create_rectangle(img_x0, cy1, img_x1, img_y1, **dim)  # bottom
        canvas.create_rectangle(img_x0, cy0, cx0, cy1, **dim)  # left
        canvas.create_rectangle(cx1, cy0, img_x1, cy1, **dim)  # right

        canvas.create_rectangle(
            cx0, cy0, cx1, cy1,
            outline="#ffffff", width=2, tags=_CROP_TAG,
        )
        # Secondary contrast stroke.
        canvas.create_rectangle(
            cx0, cy0, cx1, cy1,
            outline="#000000", width=1, dash=(4, 2), tags=_CROP_TAG,
        )

        handles = self._handle_centers_canvas()
        for name, (hx, hy) in handles.items():
            canvas.create_rectangle(
                hx - _HANDLE_SIZE / 2, hy - _HANDLE_SIZE / 2,
                hx + _HANDLE_SIZE / 2, hy + _HANDLE_SIZE / 2,
                fill="#ffffff", outline="#000000", width=1,
                tags=(_CROP_TAG, f"crop_handle_{name}"),
            )

        canvas.tag_raise(_CROP_TAG)
        if self.hud is not None:
            self.hud.set_size_fields(x1 - x0, y1 - y0)
            self.hud.lift()

    def _handle_centers_canvas(self):
        x0, y0, x1, y1 = self.rect
        mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        pts = {
            "nw": (x0, y0), "n": (mx, y0), "ne": (x1, y0),
            "e": (x1, my), "se": (x1, y1), "s": (mx, y1),
            "sw": (x0, y1), "w": (x0, my),
        }
        return {k: self.img_to_canvas(*p) for k, p in pts.items()}

    def _hit_test(self, cx, cy):
        """Return 'nw'|...|'move'|None for a canvas-space point."""
        if self.rect is None:
            return None
        for name, (hx, hy) in self._handle_centers_canvas().items():
            if abs(cx - hx) <= _HANDLE_HIT and abs(cy - hy) <= _HANDLE_HIT:
                return name
        x0, y0, x1, y1 = self.rect
        ix, iy = self.canvas_to_img(cx, cy)
        if x0 <= ix <= x1 and y0 <= iy <= y1:
            return "move"
        return None

    # ------------------------------------------------------------------ pointer

    def _event_canvas(self, event):
        return self.v.canvas.canvasx(event.x), self.v.canvas.canvasy(event.y)

    def _on_press(self, event):
        if not self.active:
            return
        cx, cy = self._event_canvas(event)
        hit = self._hit_test(cx, cy)
        if hit is None:
            return
        ix, iy = self.canvas_to_img(cx, cy)
        self._drag = {
            "mode": hit,
            "start_img": (ix, iy),
            "orig_rect": tuple(self.rect),
        }
        return "break"

    def _on_drag(self, event):
        if not self.active or not self._drag:
            return
        cx, cy = self._event_canvas(event)
        ix, iy = self.canvas_to_img(cx, cy)
        mode = self._drag["mode"]
        ox0, oy0, ox1, oy1 = self._drag["orig_rect"]
        six, siy = self._drag["start_img"]
        dx, dy = ix - six, iy - siy

        if mode == "move":
            w, h = ox1 - ox0, oy1 - oy0
            self.rect = self._clamp_rect(ox0 + dx, oy0 + dy, ox0 + dx + w, oy0 + dy + h)
        else:
            x0, y0, x1, y1 = ox0, oy0, ox1, oy1
            if "w" in mode:
                x0 = ox0 + dx
            if "e" in mode:
                x1 = ox1 + dx
            if "n" in mode:
                y0 = oy0 + dy
            if "s" in mode:
                y1 = oy1 + dy
            # Edge-only handles: keep opposite side fixed, apply aspect via width or height.
            if mode in ("n", "s", "e", "w") and self._aspect_ratio() is not None:
                ratio = self._aspect_ratio()
                if mode in ("n", "s"):
                    h = abs(y1 - y0)
                    w = h * ratio
                    cx = (ox0 + ox1) / 2.0
                    x0, x1 = cx - w / 2, cx + w / 2
                else:
                    w = abs(x1 - x0)
                    h = w / ratio
                    cy = (oy0 + oy1) / 2.0
                    y0, y1 = cy - h / 2, cy + h / 2
                self.rect = self._clamp_rect(x0, y0, x1, y1)
            elif self._aspect_ratio() is not None:
                self.rect = self._apply_aspect_to_rect(x0, y0, x1, y1, anchor=mode)
            else:
                if abs(x1 - x0) < _MIN_CROP_PX:
                    if "w" in mode:
                        x0 = x1 - _MIN_CROP_PX
                    else:
                        x1 = x0 + _MIN_CROP_PX
                if abs(y1 - y0) < _MIN_CROP_PX:
                    if "n" in mode:
                        y0 = y1 - _MIN_CROP_PX
                    else:
                        y1 = y0 + _MIN_CROP_PX
                self.rect = self._clamp_rect(x0, y0, x1, y1)

        self.redraw()
        return "break"

    def _on_release(self, event):
        if not self.active:
            return
        self._drag = None
        return "break"

    def _on_motion(self, event):
        if not self.active or self._drag:
            return
        cx, cy = self._event_canvas(event)
        hit = self._hit_test(cx, cy)
        cursors = {
            "nw": "top_left_corner", "ne": "top_right_corner",
            "sw": "bottom_left_corner", "se": "bottom_right_corner",
            "n": "top_side", "s": "bottom_side",
            "e": "right_side", "w": "left_side",
            "move": "fleur",
        }
        self.v.canvas.config(cursor=cursors.get(hit, "arrow"))

    # ------------------------------------------------------------------ HUD callbacks

    def _on_hud_size(self, w: int, h: int):
        if not self.active or self.rect is None:
            return
        iw, ih = self.v.original_image.size
        w = max(_MIN_CROP_PX, min(w, iw))
        h = max(_MIN_CROP_PX, min(h, ih))
        ratio = self._aspect_ratio()
        if ratio is not None:
            # Prefer width; derive height from aspect, then clamp.
            h = max(_MIN_CROP_PX, int(round(w / ratio)))
            if h > ih:
                h = ih
                w = max(_MIN_CROP_PX, int(round(h * ratio)))
            if w > iw:
                w = iw
                h = max(_MIN_CROP_PX, int(round(w / ratio)))
        x0, y0, x1, y1 = self.rect
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        self.rect = self._clamp_rect(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        self.redraw()

    def _on_hud_aspect(self, label: str):
        if label not in ASPECT_PRESETS:
            return
        self.aspect_label = label
        if self.rect is None:
            return
        x0, y0, x1, y1 = self.rect
        self.rect = self._apply_aspect_to_rect(x0, y0, x1, y1, anchor="center")
        if self.hud:
            self.hud.set_aspect(label)
        self.redraw()

    def _on_hud_swap(self):
        if self.rect is None:
            return
        x0, y0, x1, y1 = self.rect
        w, h = x1 - x0, y1 - y0
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        # Swap W↔H around center; flip known aspect pairs (16:9 ↔ 9:16).
        new_label = _ASPECT_SWAP_PAIRS.get(self.aspect_label)
        if new_label is None:
            # No inverse preset (e.g. 4:3 / 3:2) — keep swapped box, unlock aspect.
            new_label = "Free"
        self.aspect_label = new_label
        self.rect = self._clamp_rect(cx - h / 2, cy - w / 2, cx + h / 2, cy + w / 2)
        if new_label != "Free" and ASPECT_PRESETS.get(new_label) is not None:
            self.rect = self._apply_aspect_to_rect(*self.rect, anchor="center")
        if self.hud:
            self.hud.set_aspect(self.aspect_label)
        self.redraw()

    # ------------------------------------------------------------------ apply

    def _crop_box(self):
        if not self.rect:
            return None
        x0, y0, x1, y1 = self.rect
        iw, ih = self.v.original_image.size
        x0 = max(0, min(int(x0), iw - 1))
        y0 = max(0, min(int(y0), ih - 1))
        x1 = max(x0 + 1, min(int(x1), iw))
        y1 = max(y0 + 1, min(int(y1), ih))
        return (x0, y0, x1, y1)

    def _cropped_frames(self):
        box = self._crop_box()
        if box is None:
            return None
        frames = list(getattr(self.v, "_anim_frames", None) or [self.v.original_image])
        return [im.crop(box) for im in frames]

    @staticmethod
    def _prepare_for_save(im: PILImage.Image, path: str) -> PILImage.Image:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".jpg", ".jpeg") and im.mode in ("RGBA", "P", "LA"):
            bg = PILImage.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            return bg
        return im

    def _save_frames(self, frames, path: str):
        if not frames:
            return
        durations = list(getattr(self.v, "_anim_durations", None) or [])
        first = self._prepare_for_save(frames[0], path)
        ext = os.path.splitext(path)[1].lower()
        if len(frames) > 1 and ext in (".gif", ".webp"):
            rest = [self._prepare_for_save(f, path) for f in frames[1:]]
            save_kw = dict(save_all=True, append_images=rest, loop=0)
            if durations:
                save_kw["duration"] = durations[: len(frames)]
            first.save(path, **save_kw)
        else:
            first.save(path)

    def apply(self, mode: str = "overwrite"):
        """mode: 'overwrite' | 'copy' | 'clipboard'."""
        frames = self._cropped_frames()
        if not frames:
            return
        v = self.v

        if mode == "clipboard":
            try:
                self._copy_image_to_clipboard(frames[0])
                logging.info("Cropped image copied to clipboard.")
            except Exception as e:
                logging.info("Failed to copy crop to clipboard: %s", e)
                messagebox.showerror("Clipboard", f"Failed to copy:\n{e}", parent=v.image_window)
            return

        if mode == "copy":
            save_path = filedialog.asksaveasfilename(
                parent=v.image_window,
                defaultextension=".png",
                filetypes=[
                    ("PNG files", "*.png"),
                    ("JPEG files", "*.jpg;*.jpeg"),
                    ("WebP files", "*.webp"),
                    ("All Files", "*.*"),
                ],
                initialfile=os.path.splitext(os.path.basename(v.image_path))[0] + "_crop.png",
            )
            if not save_path:
                return
            try:
                self._save_frames(frames, save_path)
            except Exception as e:
                messagebox.showerror("Save", f"Failed to save:\n{e}", parent=v.image_window)
            return

        # overwrite
        if not messagebox.askyesno(
            "Overwrite",
            f"Overwrite the original file?\n\n{v.image_path}",
            parent=v.image_window,
        ):
            return
        try:
            self._save_frames(frames, v.image_path)
        except Exception as e:
            messagebox.showerror("Overwrite", f"Failed to save:\n{e}", parent=v.image_window)
            return

        # Install cropped frames into the viewer and leave crop mode.
        durations = list(getattr(v, "_anim_durations", None) or [0] * len(frames))
        if len(durations) < len(frames):
            durations.extend([100] * (len(frames) - len(durations)))
        self.exit()
        v._apply_loaded_frames(frames, durations[: len(frames)], reset_title=False)
        v.update_image(center=True)
        v._start_animation_if_needed()
        setattr(v, "_image_dirty", False)
        self._refresh_browser_thumbnail(v.image_path)

    def _refresh_browser_thumbnail(self, file_path: str):
        """Ask the main app to regenerate the grid thumb for the overwritten file."""
        refresh = getattr(getattr(self.v, "controller", None), "refresh_single_thumbnail", None)
        if not callable(refresh):
            return
        try:
            refresh(file_path, overwrite=True)
        except Exception as e:
            logging.info("Thumbnail refresh after crop failed: %s", e)

    def _copy_image_to_clipboard(self, image: PILImage.Image):
        """Windows-friendly BMP clipboard (same approach as ImageViewerLegacy)."""
        import io

        output = io.BytesIO()
        image.convert("RGB").save(output, "BMP")
        data = output.getvalue()[14:]
        output.close()
        win = self.v.image_window
        win.clipboard_clear()
        try:
            win.clipboard_append(data)
        except Exception:
            # Fallback: CF_DIB via windll when Tk rejects binary append.
            try:
                import win32clipboard  # type: ignore

                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
                win32clipboard.CloseClipboard()
            except Exception:
                # Last resort: keep Tk path even if odd on some hosts.
                win.clipboard_append(data)
        win.update()
