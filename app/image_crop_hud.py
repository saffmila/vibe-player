"""
Inline crop / rotate overlay + bottom HUD toolbar for ``ImageViewerLegacy``.

Crop geometry is stored in the *current working image* pixel space (after any
preview rotation) and remapped to the canvas on every pan / zoom / resize.

Rotation is Photoshop-style: the crop box stays axis-aligned; the image rotates
underneath. Interactive preview uses a throttled BILINEAR rotate; Apply uses
LANCZOS from the snapshotted base frames, then crops.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image as PILImage


# Aspect preset label -> width/height ratio (None = free).
# "Original" is resolved at runtime from the *base* (unrotated) image size.
ASPECT_ORIGINAL = "original"

ASPECT_PRESETS: dict[str, Optional[float | str]] = {
    "Free": None,
    "Original": ASPECT_ORIGINAL,
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
_EDGE_HIT = 12  # canvas px — full edge strip (not only handle centers)
_MIN_CROP_PX = 8
_CROP_TAG = "crop_overlay"
_PREVIEW_THROTTLE_MS = 16  # aim for snappy interactive rotate
_PREVIEW_MAX_SIDE = 1280  # downscale proxy for live rotate (Apply stays full-res)
_TOOL_CROP = "crop"
_TOOL_ROTATE = "rotate"

# Match main app toolbar button look (gui_elements: gray30 / CTk blue).
_HUD_BG = "#252525"
_BTN_FG = "gray30"
_BTN_HOVER = "gray25"
_BTN_PRIMARY = "#1f6aa5"
_BTN_PRIMARY_HOVER = "#144870"
_ENTRY_FG = "#2b2b2b"
_CORNER = 6
_CTRL_H = 28


def _normalize_angle_deg(angle: float) -> float:
    """Map degrees into (-180, 180]."""
    a = float(angle) % 360.0
    if a > 180.0:
        a -= 360.0
    if a <= -180.0:
        a += 360.0
    return a


class CropOverlayHUD(ctk.CTkFrame):
    """Bottom toolbar: size | mode+angle (center) | cancel/apply."""

    def __init__(
        self,
        master,
        *,
        on_mode_change: Callable[[str], None],
        on_size_change: Callable[[int, int], None],
        on_aspect_change: Callable[[str], None],
        on_swap: Callable[[], None],
        on_angle_change: Callable[[float], None],
        on_rotate_90: Callable[[int], None],
        on_angle_reset: Callable[[], None],
        on_cancel: Callable[[], None],
        on_apply_overwrite: Callable[[], None],
        on_apply_copy: Callable[[], None],
        on_apply_clipboard: Callable[[], None],
    ):
        super().__init__(master, fg_color=_HUD_BG, corner_radius=0, height=52)
        self._on_mode_change = on_mode_change
        self._on_size_change = on_size_change
        self._on_aspect_change = on_aspect_change
        self._on_swap = on_swap
        self._on_angle_change = on_angle_change
        self._on_rotate_90 = on_rotate_90
        self._on_angle_reset = on_angle_reset
        self._on_cancel = on_cancel
        self._on_apply_overwrite = on_apply_overwrite
        self._on_apply_copy = on_apply_copy
        self._on_apply_clipboard = on_apply_clipboard
        self._syncing = False

        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=12, pady=8)

        # Three zones: geometry | mode+angle (centered) | actions
        left_bar = ctk.CTkFrame(inner, fg_color="transparent")
        right_bar = ctk.CTkFrame(inner, fg_color="transparent")
        center_bar = ctk.CTkFrame(inner, fg_color="transparent")
        left_bar.pack(side="left")
        right_bar.pack(side="right")
        center_bar.pack(side="left", expand=True, fill="x", padx=12)

        # --- Left: size / aspect ---
        ctk.CTkLabel(left_bar, text="W", text_color="#cccccc", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 4)
        )
        self.width_var = tk.StringVar(value="0")
        self.width_entry = ctk.CTkEntry(
            left_bar,
            textvariable=self.width_var,
            width=64,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_ENTRY_FG,
            border_width=0,
            justify="center",
            font=ctk.CTkFont(size=12),
        )
        self.width_entry.pack(side="left", padx=(0, 6))
        self.width_entry.bind("<Return>", self._commit_size)
        self.width_entry.bind("<FocusOut>", self._commit_size)

        ctk.CTkLabel(left_bar, text="×", text_color="#888888", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 6)
        )

        ctk.CTkLabel(left_bar, text="H", text_color="#cccccc", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 4)
        )
        self.height_var = tk.StringVar(value="0")
        self.height_entry = ctk.CTkEntry(
            left_bar,
            textvariable=self.height_var,
            width=64,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_ENTRY_FG,
            border_width=0,
            justify="center",
            font=ctk.CTkFont(size=12),
        )
        self.height_entry.pack(side="left", padx=(0, 10))
        self.height_entry.bind("<Return>", self._commit_size)
        self.height_entry.bind("<FocusOut>", self._commit_size)

        self.aspect_var = tk.StringVar(value="Free")
        self.aspect_menu = ctk.CTkOptionMenu(
            left_bar,
            variable=self.aspect_var,
            values=list(ASPECT_PRESETS.keys()),
            command=self._aspect_chosen,
            width=100,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_FG,
            button_color=_BTN_FG,
            button_hover_color=_BTN_HOVER,
            dropdown_fg_color=_ENTRY_FG,
            font=ctk.CTkFont(size=12),
        )
        self.aspect_menu.pack(side="left", padx=(0, 6))

        self.swap_btn = ctk.CTkButton(
            left_bar,
            text="↔",
            command=self._on_swap,
            width=32,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_FG,
            hover_color=_BTN_HOVER,
            font=ctk.CTkFont(size=14),
        )
        self.swap_btn.pack(side="left")

        # --- Center: mode + angle (cluster centered in remaining space) ---
        center_inner = ctk.CTkFrame(center_bar, fg_color="transparent")
        center_inner.pack(anchor="center")

        ctk.CTkLabel(
            center_inner, text="Mode", text_color="#aaaaaa", font=ctk.CTkFont(size=12)
        ).pack(side="left", padx=(0, 6))

        self.mode_seg = ctk.CTkSegmentedButton(
            center_inner,
            values=["Crop", "Rotate"],
            command=self._mode_chosen,
            height=_CTRL_H,
            font=ctk.CTkFont(size=12),
            selected_color=_BTN_PRIMARY,
            selected_hover_color=_BTN_PRIMARY_HOVER,
            unselected_color=_BTN_FG,
            unselected_hover_color=_BTN_HOVER,
        )
        self.mode_seg.set("Crop")
        self.mode_seg.pack(side="left", padx=(0, 10))

        ctk.CTkFrame(center_inner, width=1, height=22, fg_color="#555555").pack(
            side="left", padx=(0, 10)
        )

        self.rot_left_btn = ctk.CTkButton(
            center_inner,
            text="↶ 90°",
            command=lambda: self._on_rotate_90(-1),
            width=58,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_FG,
            hover_color=_BTN_HOVER,
            font=ctk.CTkFont(size=11),
        )
        self.rot_left_btn.pack(side="left", padx=(0, 4))

        self.rot_right_btn = ctk.CTkButton(
            center_inner,
            text="↷ 90°",
            command=lambda: self._on_rotate_90(1),
            width=58,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_FG,
            hover_color=_BTN_HOVER,
            font=ctk.CTkFont(size=11),
        )
        self.rot_right_btn.pack(side="left", padx=(0, 8))

        self.angle_var = tk.StringVar(value="0.0")
        self.angle_entry = ctk.CTkEntry(
            center_inner,
            textvariable=self.angle_var,
            width=58,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_ENTRY_FG,
            border_width=0,
            justify="center",
            font=ctk.CTkFont(size=12),
        )
        self.angle_entry.pack(side="left", padx=(0, 2))
        self.angle_entry.bind("<Return>", self._commit_angle)
        self.angle_entry.bind("<FocusOut>", self._commit_angle)

        ctk.CTkLabel(
            center_inner, text="°", text_color="#aaaaaa", font=ctk.CTkFont(size=12)
        ).pack(side="left", padx=(0, 6))

        self.reset_angle_btn = ctk.CTkButton(
            center_inner,
            text="0°",
            command=self._on_angle_reset,
            width=36,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_FG,
            hover_color=_BTN_HOVER,
            font=ctk.CTkFont(size=11),
        )
        self.reset_angle_btn.pack(side="left")

        # --- Right: Cancel + Apply ---
        self.cancel_btn = ctk.CTkButton(
            right_bar,
            text="Cancel",
            command=self._on_cancel,
            width=70,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_FG,
            hover_color=_BTN_HOVER,
            font=ctk.CTkFont(size=12),
        )
        self.cancel_btn.pack(side="left", padx=(0, 8))

        apply_wrap = ctk.CTkFrame(right_bar, fg_color="transparent")
        apply_wrap.pack(side="left")

        self.apply_btn = ctk.CTkButton(
            apply_wrap,
            text="Apply",
            command=self._on_apply_overwrite,
            width=64,
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
            command=self._toggle_apply_panel,
            width=28,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_PRIMARY,
            hover_color=_BTN_PRIMARY_HOVER,
            font=ctk.CTkFont(size=11),
        )
        self.menu_btn.pack(side="left")

        # Custom drop-up (not tk.Menu): avoids Windows grab / click-through bugs
        # that re-open the menu instead of showing the Save dialog.
        self._apply_panel = None
        self._menu_guard = False

    def _mode_chosen(self, value: str):
        mode = _TOOL_ROTATE if str(value).lower().startswith("rot") else _TOOL_CROP
        self._on_mode_change(mode)

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

    def _commit_angle(self, event=None):
        if self._syncing:
            return "break" if event and getattr(event, "keysym", "") == "Return" else None
        try:
            raw = self.angle_var.get().strip().replace(",", ".")
            angle = float(raw)
        except ValueError:
            return "break" if event else None
        self._on_angle_change(_normalize_angle_deg(angle))
        return "break" if event and getattr(event, "keysym", "") == "Return" else None

    def _dismiss_apply_panel(self):
        pop = self._apply_panel
        self._apply_panel = None
        if pop is None:
            return
        try:
            pop.destroy()
        except Exception:
            pass

    def _unlock_menu(self):
        self._menu_guard = False
        try:
            self.menu_btn.configure(state="normal")
        except Exception:
            pass

    def _run_apply_action(self, fn: Callable[[], None]):
        """Close the drop-up, then run ``fn`` after the UI has settled."""
        if self._menu_guard:
            return
        self._menu_guard = True
        try:
            self.menu_btn.configure(state="disabled")
        except Exception:
            pass
        self._dismiss_apply_panel()

        def _go():
            win = self.winfo_toplevel()
            was_topmost = False
            try:
                # Topmost viewer often hides / blocks the native Save dialog on Windows.
                was_topmost = bool(win.attributes("-topmost"))
                if was_topmost:
                    win.attributes("-topmost", False)
                    win.update_idletasks()
            except Exception:
                was_topmost = False
            try:
                fn()
            except Exception as e:
                logging.info("Crop apply panel action failed: %s", e)
            finally:
                if was_topmost:
                    try:
                        win.attributes("-topmost", True)
                    except Exception:
                        pass
                # Keep ▴ disabled briefly so the dismissing click cannot reopen it.
                self.after(200, self._unlock_menu)

        self.after(60, _go)

    def _toggle_apply_panel(self):
        """Show / hide the Apply options panel above the button."""
        if self._menu_guard:
            return
        if self._apply_panel is not None:
            self._dismiss_apply_panel()
            return

        self.update_idletasks()
        pop = tk.Toplevel(self)
        pop.withdraw()
        pop.overrideredirect(True)
        try:
            pop.attributes("-topmost", True)
        except Exception:
            pass
        pop.configure(bg="#2d2d2d")

        wrap = ctk.CTkFrame(pop, fg_color=_ENTRY_FG, corner_radius=6, border_width=1, border_color="#555555")
        wrap.pack(fill="both", expand=True, padx=1, pady=1)

        items = (
            ("Apply (Overwrite)", self._on_apply_overwrite),
            ("Save as Copy", self._on_apply_copy),
            ("Copy to Clipboard", self._on_apply_clipboard),
        )
        for label, fn in items:
            ctk.CTkButton(
                wrap,
                text=label,
                command=lambda f=fn: self._run_apply_action(f),
                height=28,
                corner_radius=4,
                fg_color="transparent",
                hover_color=_BTN_PRIMARY,
                anchor="w",
                font=ctk.CTkFont(size=12),
            ).pack(fill="x", padx=4, pady=2)

        pop.update_idletasks()
        pw = max(168, wrap.winfo_reqwidth() + 4)
        ph = max(96, wrap.winfo_reqheight() + 4)
        x = self.apply_btn.winfo_rootx()
        y = max(0, self.apply_btn.winfo_rooty() - ph - 4)
        pop.geometry(f"{pw}x{ph}+{x}+{y}")
        pop.deiconify()
        pop.lift()
        self._apply_panel = pop

        def _on_escape(_event=None):
            self._dismiss_apply_panel()
            return "break"

        pop.bind("<Escape>", _on_escape)
        # Click outside → dismiss (bind once on the viewer window).
        try:
            top = self.winfo_toplevel()
            top.bind("<ButtonPress-1>", self._on_outside_apply_panel, add="+")
        except Exception:
            pass

    def _on_outside_apply_panel(self, event):
        pop = self._apply_panel
        if pop is None:
            return
        xr, yr = event.x_root, event.y_root
        # Ignore clicks on ▴ (toggle closes/opens itself).
        try:
            bx = self.menu_btn.winfo_rootx()
            by = self.menu_btn.winfo_rooty()
            bw = self.menu_btn.winfo_width()
            bh = self.menu_btn.winfo_height()
            if bx <= xr <= bx + bw and by <= yr <= by + bh:
                return
        except Exception:
            pass
        try:
            px = pop.winfo_rootx()
            py = pop.winfo_rooty()
            pw = pop.winfo_width()
            ph = pop.winfo_height()
            if px <= xr <= px + pw and py <= yr <= py + ph:
                return
        except Exception:
            pass
        self._dismiss_apply_panel()

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

    def set_angle(self, degrees: float):
        """Update the angle field without firing angle-change callbacks."""
        self._syncing = True
        try:
            self.angle_var.set(f"{float(degrees):.1f}")
        finally:
            self._syncing = False

    def set_tool_mode(self, mode: str):
        """Sync segmented control without re-firing the mode callback."""
        label = "Rotate" if mode == _TOOL_ROTATE else "Crop"
        try:
            self.mode_seg.set(label)
        except Exception:
            pass


class CropModeController:
    """
    Owns crop / rotate state and canvas interaction for ``ImageViewerLegacy``.

    The viewer must expose: ``image_window``, ``canvas``, ``canvas_image``,
    ``original_image``, ``_anim_frames``, ``_anim_durations``, ``_is_animated``,
    ``_stop_animation``, ``_start_animation_if_needed``, ``_map_anim_frames``,
    ``_apply_loaded_frames``, ``update_image``, ``image_path``,
    ``_refresh_overlays``, ``zoom_factor``.
    """

    def __init__(self, viewer):
        self.v = viewer
        self.active = False
        self.rect = None  # (x0, y0, x1, y1) in working-image pixels
        self.aspect_label = "Free"
        self.angle = 0.0  # degrees, clockwise-positive
        self.tool_mode = _TOOL_CROP  # "crop" | "rotate"
        self.hud: Optional[CropOverlayHUD] = None
        self._drag = None  # dict describing current pointer drag
        self._was_animated = False
        self._base_frames = None  # unrotated snapshot while in crop mode
        self._base_durations = None
        self._base_size = (0, 0)
        self._fast_base = None  # downscaled proxy for interactive rotate
        self._preview_after = None
        self._preview_pending = False
        self._preview_busy = False
        self._last_preview_angle = None
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

        frames = list(getattr(v, "_anim_frames", None) or [v.original_image])
        self._base_frames = [f.copy() for f in frames]
        self._base_durations = list(
            getattr(v, "_anim_durations", None) or [0] * len(self._base_frames)
        )
        self._base_size = self._base_frames[0].size
        self._fast_base = self._make_fast_base(self._base_frames[0])
        self._last_preview_angle = 0.0

        self.active = True
        self.aspect_label = "Free"
        self.angle = 0.0
        self.tool_mode = _TOOL_CROP
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
            on_mode_change=self._on_hud_mode,
            on_size_change=self._on_hud_size,
            on_aspect_change=self._on_hud_aspect,
            on_swap=self._on_hud_swap,
            on_angle_change=self._on_hud_angle,
            on_rotate_90=self._on_hud_rotate_90,
            on_angle_reset=self._on_hud_angle_reset,
            on_cancel=self.exit,
            on_apply_overwrite=lambda: self.apply("overwrite"),
            on_apply_copy=lambda: self.apply("copy"),
            on_apply_clipboard=lambda: self.apply("clipboard"),
        )
        self.hud.place(relx=0.0, rely=1.0, relwidth=1.0, anchor="sw")
        self.hud.set_size_fields(rw, rh)
        self.hud.set_aspect("Free")
        self.hud.set_angle(0.0)
        self.hud.set_tool_mode(_TOOL_CROP)
        self.hud.lift()

        self.redraw()
        v._refresh_overlays()

    def exit(self, *, restore: bool = True):
        if not self.active:
            return
        v = self.v
        self.active = False
        self._drag = None
        self.rect = None
        self._cancel_preview_timer()

        canvas = v.canvas
        canvas.delete(_CROP_TAG)
        canvas.config(cursor="arrow")

        if self.hud is not None:
            try:
                dismiss = getattr(self.hud, "_dismiss_apply_panel", None)
                if callable(dismiss):
                    dismiss()
                self.hud.place_forget()
                self.hud.destroy()
            except tk.TclError:
                pass
            self.hud = None

        if restore and self._base_frames:
            try:
                v._apply_loaded_frames(
                    self._base_frames, self._base_durations, reset_title=False
                )
                v.update_image(center=True, refresh_overlays=False)
            except Exception as e:
                logging.info("Crop exit restore failed: %s", e)

        self._base_frames = None
        self._base_durations = None
        self._fast_base = None
        self._last_preview_angle = None
        self.angle = 0.0
        self.tool_mode = _TOOL_CROP

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
        """Clamp a box into the image, preserving size (may shift — for move/center)."""
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

    def _clamp_resize(self, x0, y0, x1, y1, mode: str):
        """Clamp a resize so the opposite edge/corner stays pinned when possible."""
        iw, ih = self.v.original_image.size
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        w = max(_MIN_CROP_PX, min(x1 - x0, float(iw)))
        h = max(_MIN_CROP_PX, min(y1 - y0, float(ih)))

        pin_left = mode in ("e", "ne", "se")
        pin_right = mode in ("w", "nw", "sw")
        pin_top = mode in ("s", "se", "sw")
        pin_bottom = mode in ("n", "ne", "nw")

        if pin_left:
            x0 = max(0.0, min(x0, iw - w))
            x1 = x0 + w
        elif pin_right:
            x1 = max(w, min(x1, float(iw)))
            x0 = x1 - w
        else:
            x0 = max(0.0, min(x0, iw - w))
            x1 = x0 + w

        if pin_top:
            y0 = max(0.0, min(y0, ih - h))
            y1 = y0 + h
        elif pin_bottom:
            y1 = max(h, min(y1, float(ih)))
            y0 = y1 - h
        else:
            y0 = max(0.0, min(y0, ih - h))
            y1 = y0 + h

        return (int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1)))

    def _aspect_ratio(self) -> Optional[float]:
        preset = ASPECT_PRESETS.get(self.aspect_label)
        if preset is None:
            return None
        if preset == ASPECT_ORIGINAL:
            iw, ih = self._base_size if self._base_size[0] else self.v.original_image.size
            if iw <= 0 or ih <= 0:
                return None
            return iw / float(ih)
        return float(preset)

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

        if anchor == "center":
            return self._clamp_rect(nx0, ny0, nx0 + w, ny0 + h)
        return self._clamp_resize(nx0, ny0, nx0 + w, ny0 + h, anchor)

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

        outline = _BTN_PRIMARY if self.tool_mode == _TOOL_ROTATE else "#ffffff"
        canvas.create_rectangle(
            cx0, cy0, cx1, cy1,
            outline=outline, width=2, tags=_CROP_TAG,
        )
        # Secondary contrast stroke.
        canvas.create_rectangle(
            cx0, cy0, cx1, cy1,
            outline="#000000", width=1, dash=(4, 2), tags=_CROP_TAG,
        )

        if self.tool_mode == _TOOL_ROTATE:
            self._draw_rotate_chrome(canvas, cx0, cy0, cx1, cy1)
        else:
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

    def _draw_rotate_chrome(self, canvas, cx0, cy0, cx1, cy1):
        """Center pivot + circular corner cues for Rotate tool mode."""
        mx = (cx0 + cx1) / 2.0
        my = (cy0 + cy1) / 2.0
        # Subtle guide circle through the nearer crop half-size.
        radius = max(18.0, min(abs(cx1 - cx0), abs(cy1 - cy0)) * 0.28)
        canvas.create_oval(
            mx - radius, my - radius, mx + radius, my + radius,
            outline=_BTN_PRIMARY, width=1, dash=(3, 3), tags=_CROP_TAG,
        )
        # Center pivot.
        pr = 5
        canvas.create_oval(
            mx - pr, my - pr, mx + pr, my + pr,
            fill=_BTN_PRIMARY, outline="#ffffff", width=1, tags=_CROP_TAG,
        )
        # Corner rotate dots (discoverability — not tiny outside-only zones).
        for hx, hy in (
            (cx0, cy0), (cx1, cy0), (cx1, cy1), (cx0, cy1),
        ):
            canvas.create_oval(
                hx - 6, hy - 6, hx + 6, hy + 6,
                fill="#1a1a1a", outline=_BTN_PRIMARY, width=2, tags=_CROP_TAG,
            )

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
        """Return handle name, ``move``, ``rotate``, or None for a canvas-space point.

        Crop mode: edges/corners resize; interior moves. No hidden rotate zones.
        Rotate mode: press on the image (or crop) starts rotation around crop center.
        """
        if self.rect is None:
            return None

        if self.tool_mode == _TOOL_ROTATE:
            bbox = self._image_canvas_bbox()
            if not bbox:
                return None
            ix0, iy0, ix1, iy1 = bbox
            if min(ix0, ix1) <= cx <= max(ix0, ix1) and min(iy0, iy1) <= cy <= max(iy0, iy1):
                return "rotate"
            return None

        x0, y0, x1, y1 = self.rect
        c0x, c0y = self.img_to_canvas(x0, y0)
        c1x, c1y = self.img_to_canvas(x1, y1)
        left, right = (c0x, c1x) if c0x <= c1x else (c1x, c0x)
        top, bottom = (c0y, c1y) if c0y <= c1y else (c1y, c0y)
        bw = max(1.0, right - left)
        bh = max(1.0, bottom - top)
        # Keep a usable interior for move on tiny crops.
        edge_x = min(_EDGE_HIT, bw / 3.0)
        edge_y = min(_EDGE_HIT, bh / 3.0)

        near_l = abs(cx - left) <= edge_x
        near_r = abs(cx - right) <= edge_x
        near_t = abs(cy - top) <= edge_y
        near_b = abs(cy - bottom) <= edge_y
        along_x = (left - edge_x) <= cx <= (right + edge_x)
        along_y = (top - edge_y) <= cy <= (bottom + edge_y)

        if near_t and near_l and along_x and along_y:
            return "nw"
        if near_t and near_r and along_x and along_y:
            return "ne"
        if near_b and near_r and along_x and along_y:
            return "se"
        if near_b and near_l and along_x and along_y:
            return "sw"
        if near_t and left <= cx <= right:
            return "n"
        if near_b and left <= cx <= right:
            return "s"
        if near_l and top <= cy <= bottom:
            return "w"
        if near_r and top <= cy <= bottom:
            return "e"

        # Interior only (outside the edge strip) → move.
        if (left + edge_x) < cx < (right - edge_x) and (top + edge_y) < cy < (bottom - edge_y):
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
            "orig_rect": tuple(self.rect) if self.rect else None,
            "start_angle": self.angle,
        }
        if hit == "rotate" and self.rect is not None:
            x0, y0, x1, y1 = self.rect
            rcx, rcy = self.img_to_canvas((x0 + x1) / 2.0, (y0 + y1) / 2.0)
            # Angle from canvas coords so expand=True preview size changes don't skew atan2.
            self._drag["pivot_canvas"] = (rcx, rcy)
            self._drag["start_atan"] = math.atan2(cy - rcy, cx - rcx)
        return "break"

    def _on_drag(self, event):
        if not self.active or not self._drag:
            return
        cx, cy = self._event_canvas(event)
        ix, iy = self.canvas_to_img(cx, cy)
        mode = self._drag["mode"]
        six, siy = self._drag["start_img"]
        dx, dy = ix - six, iy - siy

        if mode == "rotate":
            pivot = self._drag.get("pivot_canvas")
            start_atan = self._drag.get("start_atan")
            if pivot is None or start_atan is None:
                return "break"
            rcx, rcy = pivot
            cur_atan = math.atan2(cy - rcy, cx - rcx)
            # atan2 grows counter-clockwise; HUD angle is clockwise-positive.
            delta_ccw = math.degrees(cur_atan - start_atan)
            self.set_angle(self._drag["start_angle"] - delta_ccw, preview=True)
            return "break"

        ox0, oy0, ox1, oy1 = self._drag["orig_rect"]

        if mode == "move":
            w, h = ox1 - ox0, oy1 - oy0
            self.rect = self._clamp_rect(ox0 + dx, oy0 + dy, ox0 + dx + w, oy0 + dy + h)
        elif self._aspect_ratio() is not None:
            # Locked aspect (Original / 16:9 / …): scale about the crop center so
            # the selection does not translate while resizing (Free keeps opposite-edge pin).
            self.rect = self._scale_rect_about_center(
                ox0, oy0, ox1, oy1, mode=mode, ix=ix, iy=iy, dx=dx, dy=dy
            )
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
            self.rect = self._clamp_resize(x0, y0, x1, y1, mode)

        self.redraw()
        return "break"

    def _scale_rect_about_center(
        self, ox0, oy0, ox1, oy1, *, mode: str, ix: float, iy: float, dx: float, dy: float
    ):
        """Resize with locked aspect while keeping the crop center fixed."""
        ratio = self._aspect_ratio()
        if ratio is None or ratio <= 0:
            return self._clamp_rect(ox0, oy0, ox1, oy1)

        iw, ih = self.v.original_image.size
        cx = (ox0 + ox1) / 2.0
        cy = (oy0 + oy1) / 2.0

        if mode in ("e", "w"):
            if mode == "e":
                half_w = max(_MIN_CROP_PX / 2.0, (ox1 + dx) - cx)
            else:
                half_w = max(_MIN_CROP_PX / 2.0, cx - (ox0 + dx))
            w = half_w * 2.0
            h = w / ratio
        elif mode in ("n", "s"):
            if mode == "s":
                half_h = max(_MIN_CROP_PX / 2.0, (oy1 + dy) - cy)
            else:
                half_h = max(_MIN_CROP_PX / 2.0, cy - (oy0 + dy))
            h = half_h * 2.0
            w = h * ratio
        else:
            # Corners: scale from the larger axis delta so the handle tracks the pointer.
            half_w = max(_MIN_CROP_PX / 2.0, abs(ix - cx))
            half_h = max(_MIN_CROP_PX / 2.0, abs(iy - cy))
            w_cand = half_w * 2.0
            h_cand = half_h * 2.0
            # Pick the candidate that stays outside the pointer along both axes.
            if w_cand / ratio >= h_cand:
                w, h = w_cand, w_cand / ratio
            else:
                h, w = h_cand, h_cand * ratio

        # Fit inside the image while preserving aspect and center when possible.
        max_w = float(iw)
        max_h = float(ih)
        if w > max_w:
            w = max_w
            h = w / ratio
        if h > max_h:
            h = max_h
            w = h * ratio
        if w > max_w:
            w = max_w
            h = w / ratio

        return self._clamp_rect(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)

    def _on_release(self, event):
        if not self.active:
            return
        was_rotate = bool(self._drag and self._drag.get("mode") == "rotate")
        self._drag = None
        if was_rotate:
            # Settled preview: allow refresh even if angle unchanged (NEAREST → BILINEAR).
            self._last_preview_angle = None
            self._refresh_rotated_preview(force=True)
        return "break"

    def _on_motion(self, event):
        if not self.active or self._drag:
            return
        cx, cy = self._event_canvas(event)
        hit = self._hit_test(cx, cy)
        if self.tool_mode == _TOOL_ROTATE:
            cursor = "exchange" if hit == "rotate" else "arrow"
        else:
            cursors = {
                "nw": "top_left_corner", "ne": "top_right_corner",
                "sw": "bottom_left_corner", "se": "bottom_right_corner",
                "n": "top_side", "s": "bottom_side",
                "e": "right_side", "w": "left_side",
                "move": "fleur",
            }
            cursor = cursors.get(hit, "arrow")
        try:
            self.v.canvas.config(cursor=cursor)
        except tk.TclError:
            self.v.canvas.config(cursor="crosshair" if hit == "rotate" else "arrow")

    # ------------------------------------------------------------------ HUD callbacks

    def _on_hud_mode(self, mode: str):
        mode = _TOOL_ROTATE if mode == _TOOL_ROTATE else _TOOL_CROP
        if mode == self.tool_mode:
            return
        self.tool_mode = mode
        self._drag = None
        if self.hud is not None:
            self.hud.set_tool_mode(mode)
        try:
            self.v.canvas.config(cursor="arrow")
        except tk.TclError:
            pass
        self.redraw()

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
        # Re-lock when the new preset has a fixed ratio (incl. Original).
        if new_label != "Free" and self._aspect_ratio() is not None:
            self.rect = self._apply_aspect_to_rect(*self.rect, anchor="center")
        if self.hud:
            self.hud.set_aspect(self.aspect_label)
        self.redraw()

    def _on_hud_angle(self, degrees: float):
        self.set_angle(degrees, preview=True)

    def _on_hud_rotate_90(self, direction: int):
        """direction: -1 = counter-clockwise (↶), +1 = clockwise (↷)."""
        step = 90.0 if direction >= 0 else -90.0
        self.set_angle(self.angle + step, preview=True)

    def _on_hud_angle_reset(self):
        self.set_angle(0.0, preview=True)

    # ------------------------------------------------------------------ rotation preview

    @staticmethod
    def _make_fast_base(im: PILImage.Image) -> PILImage.Image:
        """Downscale a copy for interactive rotate (keeps Apply on full-res base)."""
        proxy = im.copy()
        # Flatten rare modes; RGB/RGBA rotate much faster than P/CMYK.
        if proxy.mode not in ("RGB", "RGBA"):
            proxy = proxy.convert("RGBA" if "A" in (proxy.mode or "") else "RGB")
        proxy.thumbnail((_PREVIEW_MAX_SIDE, _PREVIEW_MAX_SIDE), PILImage.BILINEAR)
        return proxy

    def set_angle(self, degrees: float, *, preview: bool = True):
        """Set clockwise-positive angle (degrees) and optionally refresh preview."""
        self.angle = _normalize_angle_deg(degrees)
        if self.hud is not None:
            self.hud.set_angle(self.angle)
        if preview:
            self._schedule_rotated_preview()

    def _cancel_preview_timer(self):
        job = self._preview_after
        self._preview_after = None
        self._preview_pending = False
        if job is None:
            return
        try:
            self.v.image_window.after_cancel(job)
        except Exception:
            pass

    def _schedule_rotated_preview(self):
        if not self.active:
            return
        self._preview_pending = True
        if self._preview_after is not None or self._preview_busy:
            return
        try:
            self._preview_after = self.v.image_window.after(
                _PREVIEW_THROTTLE_MS, self._preview_timer_fire
            )
        except Exception:
            self._preview_after = None
            self._refresh_rotated_preview(force=True)

    def _preview_timer_fire(self):
        self._preview_after = None
        if not self.active:
            self._preview_pending = False
            return
        self._preview_pending = False
        self._refresh_rotated_preview(force=True)
        if self._preview_pending:
            self._schedule_rotated_preview()

    @staticmethod
    def _rotate_pil(im: PILImage.Image, angle_cw: float, *, resample) -> PILImage.Image:
        """Rotate clockwise by ``angle_cw`` degrees with expand=True."""
        if abs(angle_cw) < 1e-6:
            return im.copy()
        # Pillow positive angle is counter-clockwise.
        fill = (0, 0, 0, 0) if "A" in (im.mode or "") else (0, 0, 0)
        try:
            return im.rotate(
                -float(angle_cw),
                expand=True,
                resample=resample,
                fillcolor=fill,
            )
        except TypeError:
            # Older Pillow without fillcolor.
            return im.rotate(-float(angle_cw), expand=True, resample=resample)

    def _remap_rect_to_new_size(self, old_size, old_rect, new_size):
        """Keep crop center fraction + pixel size when the working image changes."""
        if not old_rect:
            return
        ow, oh = old_size
        nw, nh = new_size
        if ow <= 0 or oh <= 0 or nw <= 0 or nh <= 0:
            return
        x0, y0, x1, y1 = old_rect
        cx = ((x0 + x1) / 2.0) / ow
        cy = ((y0 + y1) / 2.0) / oh
        # Scale crop size with the working image so proxy↔full stays proportional.
        sx = nw / float(ow)
        sy = nh / float(oh)
        w = min(max(_MIN_CROP_PX, (x1 - x0) * sx), nw)
        h = min(max(_MIN_CROP_PX, (y1 - y0) * sy), nh)
        ncx, ncy = cx * nw, cy * nh
        self.rect = self._clamp_rect(ncx - w / 2.0, ncy - h / 2.0, ncx + w / 2.0, ncy + h / 2.0)

    def _refresh_rotated_preview(self, *, force: bool = False):
        """Install a rotated working image into the viewer for live preview."""
        if not self.active or not self._base_frames:
            return
        if not force and self._preview_after is not None:
            return
        if self._preview_busy:
            self._preview_pending = True
            return

        v = self.v
        old_size = v.original_image.size
        old_rect = self.rect
        angle = self.angle
        # Skip duplicate work (common when HUD + canvas both push the same angle).
        if (
            self._last_preview_angle is not None
            and abs(self._last_preview_angle - angle) < 0.05
            and abs(angle) > 1e-6
        ):
            if self.hud is not None:
                self.hud.set_angle(angle)
            return

        dragging = bool(self._drag and self._drag.get("mode") == "rotate")
        self._preview_busy = True
        try:
            if abs(angle) < 1e-6:
                frames = [f.copy() for f in self._base_frames]
                durations = list(self._base_durations or [0] * len(frames))
            else:
                src = self._fast_base or self._base_frames[0]
                # NEAREST while dragging = much snappier; BILINEAR when settled.
                resample = PILImage.NEAREST if dragging else PILImage.BILINEAR
                preview = self._rotate_pil(src, angle, resample=resample)
                frames = [preview]
                durations = [0]
            v._apply_loaded_frames(frames, durations, reset_title=False)
            new_size = v.original_image.size
            self._remap_rect_to_new_size(old_size, old_rect, new_size)
            # Keep on-screen size stable when swapping full-res ↔ rotate proxy.
            if (
                old_size
                and new_size
                and old_size[0] > 0
                and new_size[0] > 0
                and old_size != new_size
            ):
                try:
                    v.zoom_factor = (old_size[0] * float(v.zoom_factor)) / float(new_size[0])
                except Exception:
                    pass
            # Avoid re-centering every motion tick (layout thrash); center only when settled.
            v.update_image(
                high_quality=False,
                center=not dragging,
                refresh_overlays=False,
            )
            self._last_preview_angle = angle
            self.redraw()
        except Exception as e:
            logging.info("Crop rotate preview failed: %s", e)
        finally:
            self._preview_busy = False

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

    def _build_output_frames(self):
        """Rotate base frames (LANCZOS) then crop — used for Apply / Copy / Clipboard."""
        box = self._crop_box()
        if box is None or not self._base_frames:
            return None
        angle = self.angle
        # Box is in the *current working* image space (often a downscaled rotate proxy).
        work_w, work_h = self.v.original_image.size
        out = []
        for src in self._base_frames:
            im = self._rotate_pil(src, angle, resample=PILImage.LANCZOS)
            x0, y0, x1, y1 = box
            if im.size != (work_w, work_h) and work_w > 0 and work_h > 0:
                sx = im.size[0] / float(work_w)
                sy = im.size[1] / float(work_h)
                x0 = int(round(x0 * sx))
                y0 = int(round(y0 * sy))
                x1 = int(round(x1 * sx))
                y1 = int(round(y1 * sy))
            iw, ih = im.size
            x0 = max(0, min(x0, iw - 1))
            y0 = max(0, min(y0, ih - 1))
            x1 = max(x0 + 1, min(x1, iw))
            y1 = max(y0 + 1, min(y1, ih))
            out.append(im.crop((x0, y0, x1, y1)))
        return out

    def _cropped_frames(self):
        return self._build_output_frames()

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
            src = getattr(v, "image_path", None) or ""
            initial_dir = os.path.dirname(src) if src else None
            base = os.path.splitext(os.path.basename(src))[0] if src else "crop"
            # Prefer a stable parent; image_window may be borderless/topmost.
            parent = v.image_window
            try:
                if not parent.winfo_viewable():
                    parent = self.hud or parent
            except Exception:
                pass
            save_path = filedialog.asksaveasfilename(
                parent=parent,
                title="Save crop as copy",
                defaultextension=".png",
                filetypes=[
                    ("PNG files", "*.png"),
                    ("JPEG files", "*.jpg;*.jpeg"),
                    ("WebP files", "*.webp"),
                    ("All Files", "*.*"),
                ],
                initialdir=initial_dir or None,
                initialfile=f"{base}_crop.png",
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
        if self._base_durations:
            durations = list(self._base_durations)
        if len(durations) < len(frames):
            durations.extend([100] * (len(frames) - len(durations)))
        self.exit(restore=False)
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
