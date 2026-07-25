"""
Dual-mode image comparison dialog (Side-by-Side / Split Slider).

Opens borderless fullscreen (like the image viewer) with a compact bottom HUD.
Navigation is Lightroom-style: left = Reference (fixed), right = Target
(Prev/Next Target). ``Set as Reference`` promotes the target to the left pane.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox
from PIL import Image as PILImage
from PIL import ImageTk

from image_loader import load_pil_image

_MODE_SIDE = "Side-by-Side"
_MODE_SPLIT = "Split Slider"
_ZOOM_STEP = 1.15
_MIN_ZOOM = 0.05
_MAX_ZOOM = 32.0
_BG = "#1a1a1a"
_DIVIDER = "#00d4ff"
_HUD_BG = "#252525"
_BTN_FG = "gray30"
_BTN_HOVER = "gray25"
_CORNER = 6
_CTRL_H = 28
# Ignore Tk's pre-map 1×1 canvas when fitting.
_MIN_CANVAS_PX = 64


def _format_bytes(n: int) -> str:
    n = max(0, int(n))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return f"{n} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _meta_line(path: str, image: PILImage.Image) -> str:
    name = os.path.basename(path)
    w, h = image.size
    try:
        size = _format_bytes(os.path.getsize(path))
    except OSError:
        size = "?"
    fmt = (image.format or os.path.splitext(path)[1].lstrip(".").upper() or "?")
    return f"{name}  ·  {w}×{h}  ·  {size}  ·  {fmt}"


@dataclass
class _ViewState:
    """Absolute pixel scale + top-left image offset in canvas coordinates."""

    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0


class ImageCompareDialog(ctk.CTkToplevel):
    """
    Fullscreen compare window with Side-by-Side and Split Slider modes.

    Left pane is the Reference; right pane is the Target. Prev/Next move only
    the Target through the selection. Set as Reference promotes the Target.
    """

    def __init__(self, parent, paths: list[str], *, title: str = "Compare Images"):
        super().__init__(parent)
        self.title(title)
        self.paths = [os.path.normpath(p) for p in paths if p and os.path.isfile(p)]
        if len(self.paths) < 2:
            self.destroy()
            raise ValueError("ImageCompareDialog requires at least two existing image paths")

        # Lightroom-style: fixed reference + browsable target.
        self._ref_index = 0
        self._target_index = 1
        self._sync = True
        self._mode = _MODE_SIDE
        self._shared = _ViewState()
        self._left_view = _ViewState()
        self._right_view = _ViewState()
        self._split_ratio = 0.5
        self._dragging_split = False
        self._panning = False
        self._pan_pane: Optional[str] = None
        self._pan_last: tuple[int, int] = (0, 0)
        self._left_pil: Optional[PILImage.Image] = None
        self._right_pil: Optional[PILImage.Image] = None
        self._photo_left = None
        self._photo_right = None
        self._photo_split_l = None
        self._photo_split_r = None
        self._item_left = None
        self._item_right = None
        self._split_item_left = None
        self._split_item_right = None
        # Cached scaled PIL at current zoom — pan reuses these (no resize each drag).
        self._scaled_left: Optional[PILImage.Image] = None
        self._scaled_right: Optional[PILImage.Image] = None
        self._scaled_zoom_left: Optional[float] = None
        self._scaled_zoom_right: Optional[float] = None
        self._hq_after: Optional[str] = None
        self._fit_pending = True
        self._fit_retries = 0
        self._split_layouting = False
        self.is_fullscreen = False
        self._windowed_geometry: Optional[str] = None
        self._closing = False

        try:
            self.transient(parent.winfo_toplevel())
        except Exception:
            pass

        self.configure(fg_color="#1e1e1e")
        self.minsize(720, 480)

        # Image area first (fills remaining space); HUD pinned to bottom.
        self._build_body()
        self._build_hud()

        self.bind("<Escape>", self._on_escape)
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<Left>", lambda e: self._prev_target())
        self.bind("<Right>", lambda e: self._next_target())
        self.bind("<plus>", lambda e: self._zoom_in())
        self.bind("<minus>", lambda e: self._zoom_out())
        self.bind("<KP_Add>", lambda e: self._zoom_in())
        self.bind("<KP_Subtract>", lambda e: self._zoom_out())
        self.protocol("WM_DELETE_WINDOW", self._close)

        # Map windowed briefly, then go fullscreen so monitor detection is stable.
        self._place_near_parent(parent)
        self.after(40, self._enter_fullscreen_initial)
        self.after(50, self._initial_load)

    # ------------------------------------------------------------------ UI
    def _place_near_parent(self, parent) -> None:
        """Rough windowed placement using Tk logical pixels (not screeninfo physical)."""
        try:
            sw = int(parent.winfo_screenwidth())
            sh = int(parent.winfo_screenheight())
            px = parent.winfo_rootx() + max(1, parent.winfo_width()) // 2
            py = parent.winfo_rooty() + max(1, parent.winfo_height()) // 2
        except Exception:
            sw = int(self.winfo_screenwidth())
            sh = int(self.winfo_screenheight())
            px, py = sw // 2, sh // 2
        w = max(800, min(1400, sw - 80))
        h = max(600, min(900, sh - 80))
        x = max(0, min(sw - w, px - w // 2))
        y = max(0, min(sh - h, py - h // 2))
        geom = f"{w}x{h}+{x}+{y}"
        self.geometry(geom)
        self._windowed_geometry = geom

    def _build_hud(self) -> None:
        self._hud = ctk.CTkFrame(self, fg_color=_HUD_BG, corner_radius=0, height=52)
        self._hud.pack(fill="x", side="bottom")
        self._hud.pack_propagate(False)

        inner = ctk.CTkFrame(self._hud, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=12, pady=8)

        self._mode_var = tk.StringVar(value=_MODE_SIDE)
        ctk.CTkSegmentedButton(
            inner,
            values=[_MODE_SIDE, _MODE_SPLIT],
            variable=self._mode_var,
            command=self._on_mode_change,
            height=_CTRL_H,
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(0, 10))

        self._sync_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            inner,
            text="Sync Zoom & Pan",
            variable=self._sync_var,
            command=self._on_sync_toggle,
            font=ctk.CTkFont(size=12),
            checkbox_width=18,
            checkbox_height=18,
        ).pack(side="left", padx=(0, 8))

        def _btn(text, cmd, width=70):
            return ctk.CTkButton(
                inner,
                text=text,
                width=width,
                height=_CTRL_H,
                corner_radius=_CORNER,
                fg_color=_BTN_FG,
                hover_color=_BTN_HOVER,
                command=cmd,
            )

        _btn("⇄", self._swap, 36).pack(side="left", padx=(0, 8))
        _btn("−", self._zoom_out, 36).pack(side="left", padx=1)
        _btn("+", self._zoom_in, 36).pack(side="left", padx=1)
        _btn("100%", self._actual_size, 56).pack(side="left", padx=1)
        _btn("Fit", self._request_fit, 48).pack(side="left", padx=(1, 8))

        self._fs_btn = _btn("Window", self.toggle_fullscreen, 72)
        self._fs_btn.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            inner,
            text="Close",
            width=64,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color="#1f6aa5",
            hover_color="#144870",
            command=self._close,
        ).pack(side="right", padx=(8, 0))

        self._nav_label = ctk.CTkLabel(inner, text="", font=ctk.CTkFont(size=12))
        self._nav_label.pack(side="right", padx=(8, 0))

        self._ref_btn = ctk.CTkButton(
            inner,
            text="Set as Reference",
            width=130,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color="#1f6aa5",
            hover_color="#144870",
            command=self._set_as_reference,
        )
        self._ref_btn.pack(side="right", padx=(8, 2))

        self._next_btn = _btn("Next Target ▶", self._next_target, 120)
        self._next_btn.pack(side="right", padx=2)
        self._prev_btn = _btn("◀ Prev Target", self._prev_target, 120)
        self._prev_btn.pack(side="right", padx=2)

    def _build_body(self) -> None:
        self._body = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self._body.pack(fill="both", expand=True, side="top")

        self._sbs = ctk.CTkFrame(self._body, fg_color="transparent")
        self._left_pane = self._make_pane(self._sbs, "left")
        self._right_pane = self._make_pane(self._sbs, "right")
        self._left_pane.pack(side="left", fill="both", expand=True, padx=(0, 1))
        self._right_pane.pack(side="left", fill="both", expand=True, padx=(1, 0))

        self._split_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        self._split_meta = ctk.CTkLabel(
            self._split_frame,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color="#aaaaaa",
            height=22,
        )
        self._split_meta.pack(fill="x", padx=8, pady=(4, 0))

        # Clip-frame split: slider drag only resizes places — no PIL re-composite.
        self._split_area = tk.Frame(self._split_frame, bg=_BG)
        self._split_area.pack(fill="both", expand=True)
        self._split_area.bind("<Configure>", self._on_split_area_configure)

        self._split_left_clip = tk.Frame(self._split_area, bg=_BG)
        self._split_left_cv = tk.Canvas(
            self._split_left_clip, bg=_BG, highlightthickness=0, bd=0
        )
        self._split_left_cv.pack(fill="both", expand=True)

        self._split_right_clip = tk.Frame(self._split_area, bg=_BG)
        self._split_right_cv = tk.Canvas(
            self._split_right_clip, bg=_BG, highlightthickness=0, bd=0
        )
        self._split_right_cv.pack(fill="both", expand=True)

        # Drag handle (thin bar). Wider hit target via bindings on area too.
        self._split_handle = tk.Frame(
            self._split_area, bg=_DIVIDER, cursor="sb_h_double_arrow", width=4
        )
        self._split_grip = tk.Frame(
            self._split_area, bg=_DIVIDER, cursor="sb_h_double_arrow", width=8, height=56
        )

        for w in (
            self._split_left_cv,
            self._split_right_cv,
            self._split_area,
            self._split_handle,
            self._split_grip,
        ):
            self._bind_split_input(w)

        # Keep a size probe alias used by fit/zoom helpers.
        self._split_canvas = self._split_area

        self._sbs.pack(fill="both", expand=True)

    def _make_pane(self, parent, side: str) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="#141414", corner_radius=0)
        meta = ctk.CTkLabel(
            frame,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color="#aaaaaa",
            height=22,
        )
        meta.pack(fill="x", padx=8, pady=(4, 0))
        canvas = tk.Canvas(frame, bg=_BG, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        self._bind_canvas(canvas, side)
        if side == "left":
            self._left_meta = meta
            self._left_canvas = canvas
        else:
            self._right_meta = meta
            self._right_canvas = canvas
        return frame

    def _bind_canvas(self, canvas: tk.Canvas, pane: str) -> None:
        canvas.bind("<MouseWheel>", lambda e, p=pane: self._on_wheel(e, p))
        canvas.bind("<Button-4>", lambda e, p=pane: self._on_wheel_linux(e, p, 1))
        canvas.bind("<Button-5>", lambda e, p=pane: self._on_wheel_linux(e, p, -1))
        canvas.bind("<ButtonPress-2>", lambda e, p=pane: self._pan_start(e, p))
        canvas.bind("<B2-Motion>", lambda e, p=pane: self._pan_move(e, p))
        canvas.bind("<ButtonRelease-2>", self._pan_end)
        if pane != "split":
            canvas.bind("<ButtonPress-1>", lambda e, p=pane: self._pan_start(e, p))
            canvas.bind("<B1-Motion>", lambda e, p=pane: self._pan_move(e, p))
            canvas.bind("<ButtonRelease-1>", self._pan_end)
        canvas.bind("<Configure>", self._on_canvas_configure)

    def _bind_split_input(self, widget) -> None:
        widget.bind("<MouseWheel>", lambda e: self._on_wheel(e, "split"))
        widget.bind("<Button-4>", lambda e: self._on_wheel_linux(e, "split", 1))
        widget.bind("<Button-5>", lambda e: self._on_wheel_linux(e, "split", -1))
        widget.bind("<ButtonPress-2>", lambda e: self._pan_start(e, "split"))
        widget.bind("<B2-Motion>", lambda e: self._pan_move(e, "split"))
        widget.bind("<ButtonRelease-2>", self._pan_end)
        widget.bind("<ButtonPress-1>", self._split_press)
        widget.bind("<B1-Motion>", self._split_drag)
        widget.bind("<ButtonRelease-1>", self._split_release)

    def _on_canvas_configure(self, event=None) -> None:
        if self._closing or self._split_layouting:
            return
        if self._fit_pending:
            self._try_fit_when_ready()
        else:
            self._schedule_redraw(fast=True)

    def _on_split_area_configure(self, event=None) -> None:
        if self._closing or self._split_layouting:
            return
        if self._mode != _MODE_SPLIT:
            return
        if self._fit_pending:
            self._try_fit_when_ready()
            return
        # Size change: keep images, just re-clip; full redraw only if photos missing.
        if self._split_item_left is None or self._split_item_right is None:
            self._schedule_redraw(fast=True)
        else:
            self._layout_split_clips()

    def _initial_load(self) -> None:
        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass
        self._load_compare(fit=True)
        self._update_nav_controls()

    # ----------------------------------------------------- fullscreen
    def _enter_fullscreen_initial(self) -> None:
        if self._closing or self.is_fullscreen:
            return
        self.set_fullscreen(True)

    def toggle_fullscreen(self, event=None):
        self.set_fullscreen(not self.is_fullscreen)
        return "break"

    def set_fullscreen(self, enabled: bool) -> None:
        """Match video player: Tk ``-fullscreen`` (correct on HiDPI / multi-monitor)."""
        enabled = bool(enabled)
        if enabled == self.is_fullscreen:
            return
        try:
            self.update_idletasks()
        except Exception:
            pass

        if enabled:
            try:
                self._windowed_geometry = self.geometry()
            except tk.TclError:
                pass
            # Avoid state("zoomed") + -fullscreen together — same note as video_operations.
            try:
                if str(self.state()) == "zoomed":
                    self.state("normal")
            except Exception:
                pass
            self.is_fullscreen = True
            self.attributes("-fullscreen", True)
            self._fs_btn.configure(text="Window")
            self._fit_pending = True
            self.after(100, self._try_fit_when_ready)
        else:
            self.is_fullscreen = False
            try:
                self.attributes("-fullscreen", False)
            except Exception:
                pass
            geom = self._windowed_geometry
            if geom:
                self.geometry(geom)
            self._fs_btn.configure(text="Fullscreen")
            self._fit_pending = True
            self.after(100, self._try_fit_when_ready)

    def _on_escape(self, event=None):
        # Esc always closes compare (dedicated session); F11 toggles windowed.
        self._close()
        return "break"

    def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            if self.is_fullscreen:
                self.attributes("-fullscreen", False)
        except Exception:
            pass
        self.destroy()

    # -------------------------------------------------- reference / target
    def _left_path(self) -> str:
        return self.paths[self._ref_index]

    def _right_path(self) -> str:
        return self.paths[self._target_index]

    def _candidate_indices(self) -> list[int]:
        """All selection indices except the current reference."""
        return [i for i in range(len(self.paths)) if i != self._ref_index]

    def _update_nav_controls(self) -> None:
        cands = self._candidate_indices()
        multi = len(cands) > 1
        state = "normal" if multi else "disabled"
        self._prev_btn.configure(state=state)
        self._next_btn.configure(state=state)
        # Set as Reference is useful whenever target != ref (always true by design).
        self._ref_btn.configure(state="normal" if len(self.paths) > 1 else "disabled")
        n = len(self.paths)
        try:
            pos = cands.index(self._target_index) + 1
        except ValueError:
            pos = 1
        if multi:
            self._nav_label.configure(
                text=f"Target {pos}/{len(cands)}  ({n} images)"
            )
        else:
            self._nav_label.configure(text=f"2 images")

    def _meta_prefix(self, role: str, path: str, image: PILImage.Image) -> str:
        return f"{role}  ·  {_meta_line(path, image)}"

    def _load_compare(
        self, *, fit: bool = False, reload_left: bool = True, reload_right: bool = True
    ) -> None:
        lp, rp = self._left_path(), self._right_path()
        try:
            if reload_left:
                self._left_pil = load_pil_image(lp).convert("RGBA")
            if reload_right:
                self._right_pil = load_pil_image(rp).convert("RGBA")
        except Exception as e:
            logging.exception("Failed to load compare images")
            self._left_meta.configure(text=f"Error: {e}")
            return

        if self._left_pil is None or self._right_pil is None:
            return

        self._left_meta.configure(
            text=self._meta_prefix("Reference", lp, self._left_pil)
        )
        self._right_meta.configure(
            text=self._meta_prefix("Target", rp, self._right_pil)
        )
        self._split_meta.configure(
            text=(
                f"{self._meta_prefix('Reference', lp, self._left_pil)}"
                f"   |   {self._meta_prefix('Target', rp, self._right_pil)}"
            )
        )
        if fit:
            self._request_fit()
        else:
            self._schedule_redraw(fast=False)

    def _swap(self) -> None:
        """Swap Reference and Target (indices + optional unsynced views)."""
        self._ref_index, self._target_index = self._target_index, self._ref_index
        if not self._sync:
            self._left_view, self._right_view = self._right_view, self._left_view
        self._update_nav_controls()
        self._load_compare(fit=False)

    def _prev_target(self) -> None:
        cands = self._candidate_indices()
        if len(cands) < 2:
            return
        try:
            pos = cands.index(self._target_index)
        except ValueError:
            pos = 0
        self._target_index = cands[(pos - 1) % len(cands)]
        self._update_nav_controls()
        # Keep zoom/pan — only the right pane changes.
        self._load_compare(fit=False, reload_left=False, reload_right=True)

    def _next_target(self) -> None:
        cands = self._candidate_indices()
        if len(cands) < 2:
            return
        try:
            pos = cands.index(self._target_index)
        except ValueError:
            pos = -1
        self._target_index = cands[(pos + 1) % len(cands)]
        self._update_nav_controls()
        self._load_compare(fit=False, reload_left=False, reload_right=True)

    def _set_as_reference(self) -> None:
        """Promote Target → Reference; Target advances to the next candidate."""
        if len(self.paths) < 2:
            return
        old_target = self._target_index
        self._ref_index = old_target
        n = len(self.paths)
        new_target = None
        for step in range(1, n):
            j = (old_target + step) % n
            if j != self._ref_index:
                new_target = j
                break
        if new_target is None:
            return
        self._target_index = new_target
        self._update_nav_controls()
        self._load_compare(fit=False)

    # ------------------------------------------------------- mode / sync
    def _on_mode_change(self, value: str) -> None:
        self._mode = value
        if value == _MODE_SPLIT:
            self._sbs.pack_forget()
            self._split_frame.pack(fill="both", expand=True)
        else:
            self._split_frame.pack_forget()
            self._sbs.pack(fill="both", expand=True)
        self._request_fit()

    def _on_sync_toggle(self) -> None:
        self._sync = bool(self._sync_var.get())
        if self._sync:
            src = self._left_view if self._mode == _MODE_SIDE else self._shared
            self._shared = _ViewState(src.zoom, src.pan_x, src.pan_y)
            self._left_view = _ViewState(src.zoom, src.pan_x, src.pan_y)
            self._right_view = _ViewState(src.zoom, src.pan_x, src.pan_y)
        self._schedule_redraw(fast=False)

    def _view_for(self, pane: str) -> _ViewState:
        if self._sync or pane == "split":
            return self._shared
        return self._left_view if pane == "left" else self._right_view

    def _set_view(self, pane: str, view: _ViewState) -> None:
        if self._sync or pane == "split":
            self._shared = view
            self._left_view = _ViewState(view.zoom, view.pan_x, view.pan_y)
            self._right_view = _ViewState(view.zoom, view.pan_x, view.pan_y)
        elif pane == "left":
            self._left_view = view
        else:
            self._right_view = view

    # ---------------------------------------------------------- zoom / pan
    def _request_fit(self) -> None:
        self._fit_pending = True
        self._fit_retries = 0
        self._try_fit_when_ready()

    def _canvas_ready(self, widget) -> bool:
        try:
            return (
                widget.winfo_width() >= _MIN_CANVAS_PX
                and widget.winfo_height() >= _MIN_CANVAS_PX
            )
        except Exception:
            return False

    def _active_canvases_ready(self) -> bool:
        if self._mode == _MODE_SPLIT:
            return self._canvas_ready(self._split_area)
        return self._canvas_ready(self._left_canvas) and self._canvas_ready(
            self._right_canvas
        )

    def _try_fit_when_ready(self) -> None:
        if self._closing or not self._fit_pending:
            return
        if self._left_pil is None or self._right_pil is None:
            return
        if not self._active_canvases_ready():
            self._fit_retries += 1
            if self._fit_retries < 40:
                self.after(50, self._try_fit_when_ready)
            return
        self._fit_pending = False
        self._fit_window()

    def _zoom_in(self) -> None:
        self._apply_zoom_factor(None, _ZOOM_STEP, center=True)

    def _zoom_out(self) -> None:
        self._apply_zoom_factor(None, 1.0 / _ZOOM_STEP, center=True)

    def _actual_size(self) -> None:
        pane = "split" if self._mode == _MODE_SPLIT else "left"
        view = _ViewState(1.0, 0, 0)
        self._center_view(pane, view)
        if not self._sync and self._mode == _MODE_SIDE:
            self._center_view("right", _ViewState(1.0, 0, 0))
        self._schedule_redraw(fast=False)

    def _fit_window(self) -> None:
        if self._mode == _MODE_SPLIT:
            self._fit_canvas(self._split_canvas, "split", self._left_pil, self._right_pil)
        elif self._sync:
            z1 = self._fit_zoom(self._left_canvas, self._left_pil)
            z2 = self._fit_zoom(self._right_canvas, self._right_pil)
            z = min(z1, z2) if z1 and z2 else (z1 or z2 or 1.0)
            self._center_view("left", _ViewState(z, 0, 0))
            # Re-center shared transform using left pane size (both panes equal in SBS).
            self._center_view("left", self._shared)
        else:
            self._fit_canvas(self._left_canvas, "left", self._left_pil)
            self._fit_canvas(self._right_canvas, "right", self._right_pil)
        self._schedule_redraw(fast=False)

    def _fit_zoom(self, widget, image: Optional[PILImage.Image]) -> float:
        if image is None:
            return 1.0
        cw = max(1, widget.winfo_width())
        ch = max(1, widget.winfo_height())
        iw, ih = image.size
        if iw < 1 or ih < 1:
            return 1.0
        return max(_MIN_ZOOM, min(_MAX_ZOOM, min(cw / float(iw), ch / float(ih))))

    def _fit_canvas(
        self,
        widget,
        pane: str,
        image: Optional[PILImage.Image],
        image2: Optional[PILImage.Image] = None,
    ) -> None:
        if image is None:
            return
        z = self._fit_zoom(widget, image)
        if image2 is not None:
            z = min(z, self._fit_zoom(widget, image2))
        self._center_view(pane, _ViewState(z, 0, 0))

    def _center_view(self, pane: str, view: _ViewState) -> None:
        widget = self._canvas_for(pane)
        if pane == "right":
            image = self._right_pil
        else:
            image = self._left_pil
        if widget is None or image is None:
            self._set_view(pane, view)
            return
        cw = max(1, widget.winfo_width())
        ch = max(1, widget.winfo_height())
        dw = image.size[0] * view.zoom
        dh = image.size[1] * view.zoom
        view.pan_x = (cw - dw) / 2.0
        view.pan_y = (ch - dh) / 2.0
        self._set_view(pane, view)

    def _canvas_for(self, pane: str):
        if pane == "left":
            return self._left_canvas
        if pane == "right":
            return self._right_canvas
        if pane == "split":
            return self._split_area
        return None

    def _apply_zoom_factor(
        self, pane: Optional[str], factor: float, *, center: bool = False, event=None
    ) -> None:
        if pane is None:
            pane = "split" if self._mode == _MODE_SPLIT else "left"
        view = self._view_for(pane)
        old_z = view.zoom
        new_z = max(_MIN_ZOOM, min(_MAX_ZOOM, old_z * factor))
        if abs(new_z - old_z) < 1e-9:
            return
        widget = self._canvas_for(pane)
        if widget is None:
            return
        if event is not None and pane == "split":
            cx = self._event_x_in_split_area(event)
            cy = self._event_y_in_split_area(event)
        elif event is not None:
            cx, cy = event.x, event.y
        else:
            cx = widget.winfo_width() / 2
            cy = widget.winfo_height() / 2
        img_x = (cx - view.pan_x) / old_z
        img_y = (cy - view.pan_y) / old_z
        new_view = _ViewState(new_z, cx - img_x * new_z, cy - img_y * new_z)
        self._set_view(pane, new_view)
        self._schedule_redraw(fast=True)

    def _on_wheel(self, event, pane: str) -> str:
        factor = _ZOOM_STEP if event.delta > 0 else 1.0 / _ZOOM_STEP
        self._apply_zoom_factor(pane, factor, event=event)
        return "break"

    def _on_wheel_linux(self, event, pane: str, direction: int) -> str:
        factor = _ZOOM_STEP if direction > 0 else 1.0 / _ZOOM_STEP
        self._apply_zoom_factor(pane, factor, event=event)
        return "break"

    def _pan_start(self, event, pane: str) -> None:
        self._panning = True
        self._pan_pane = pane
        if pane == "split":
            self._pan_last = (
                self._event_x_in_split_area(event),
                self._event_y_in_split_area(event),
            )
        else:
            self._pan_last = (event.x, event.y)
        if self._mode == _MODE_SIDE:
            if self._item_left is None or self._item_right is None:
                self._redraw(resample=PILImage.BILINEAR)
        elif self._split_item_left is None or self._split_item_right is None:
            self._redraw(resample=PILImage.BILINEAR)

    def _pan_move(self, event, pane: str) -> None:
        if not self._panning:
            return
        pane = self._pan_pane or pane
        if pane == "split":
            x = self._event_x_in_split_area(event)
            y = self._event_y_in_split_area(event)
        else:
            x, y = event.x, event.y
        dx = x - self._pan_last[0]
        dy = y - self._pan_last[1]
        self._pan_last = (x, y)
        view = self._view_for(pane)
        self._set_view(pane, _ViewState(view.zoom, view.pan_x + dx, view.pan_y + dy))
        self._reposition_views()

    def _pan_end(self, event=None) -> None:
        self._panning = False
        self._pan_pane = None

    def _reposition_views(self) -> None:
        """Fast pan path: move canvas images only (no resize / composite)."""
        if self._mode == _MODE_SPLIT:
            self._reposition_split_images()
            return
        lv = self._view_for("left")
        rv = self._view_for("right")
        if self._item_left is not None:
            try:
                self._left_canvas.coords(self._item_left, lv.pan_x, lv.pan_y)
            except Exception:
                pass
        if self._item_right is not None:
            try:
                self._right_canvas.coords(self._item_right, rv.pan_x, rv.pan_y)
            except Exception:
                pass

    # ------------------------------------------------------- split slider
    def _event_x_in_split_area(self, event) -> int:
        try:
            return int(event.x_root - self._split_area.winfo_rootx())
        except Exception:
            return int(getattr(event, "x", 0))

    def _event_y_in_split_area(self, event) -> int:
        try:
            return int(event.y_root - self._split_area.winfo_rooty())
        except Exception:
            return int(getattr(event, "y", 0))

    def _split_x(self) -> int:
        cw = max(1, self._split_area.winfo_width())
        return int(max(8, min(cw - 8, cw * self._split_ratio)))

    def _split_press(self, event) -> None:
        sx = self._split_x()
        x = self._event_x_in_split_area(event)
        if abs(x - sx) <= 12:
            self._dragging_split = True
        else:
            self._pan_start(event, "split")

    def _split_drag(self, event) -> None:
        if self._dragging_split:
            cw = max(1, self._split_area.winfo_width())
            x = self._event_x_in_split_area(event)
            self._split_ratio = max(0.05, min(0.95, x / float(cw)))
            self._layout_split_clips()
        elif self._panning:
            self._pan_move(event, "split")

    def _split_release(self, event=None) -> None:
        if self._dragging_split:
            self._dragging_split = False
            self._layout_split_clips()
        else:
            self._pan_end(event)

    def _layout_split_clips(self) -> None:
        """Resize clip frames + move handle — no image re-encode."""
        try:
            cw = max(1, self._split_area.winfo_width())
            ch = max(1, self._split_area.winfo_height())
        except Exception:
            return
        if cw < 8 or ch < 8:
            return
        sx = self._split_x()
        self._split_layouting = True
        try:
            self._split_left_clip.place(x=0, y=0, width=sx, height=ch)
            self._split_right_clip.place(x=sx, y=0, width=max(1, cw - sx), height=ch)
            self._split_handle.place(x=max(0, sx - 2), y=0, width=4, height=ch)
            gy = max(0, (ch - 56) // 2)
            self._split_grip.place(x=max(0, sx - 4), y=gy, width=8, height=56)
            self._split_handle.lift()
            self._split_grip.lift()
            self._reposition_split_images()
        finally:
            self._split_layouting = False

    def _reposition_split_images(self) -> None:
        view = self._shared
        sx = self._split_x()
        if self._split_item_left is not None:
            try:
                self._split_left_cv.coords(
                    self._split_item_left, view.pan_x, view.pan_y
                )
            except Exception:
                pass
        if self._split_item_right is not None:
            try:
                # Right clip starts at sx — shift image into shared coordinates.
                self._split_right_cv.coords(
                    self._split_item_right, view.pan_x - sx, view.pan_y
                )
            except Exception:
                pass

    # ------------------------------------------------------------- redraw
    def _invalidate_scale_cache(self) -> None:
        self._scaled_left = None
        self._scaled_right = None
        self._scaled_zoom_left = None
        self._scaled_zoom_right = None

    def _schedule_redraw(self, *, fast: bool) -> None:
        if self._hq_after is not None:
            try:
                self.after_cancel(self._hq_after)
            except Exception:
                pass
            self._hq_after = None
        # Zoom / fit changes need a fresh scale cache.
        self._invalidate_scale_cache()
        self._redraw(resample=PILImage.BILINEAR if fast else PILImage.LANCZOS)
        if fast:
            self._hq_after = self.after(120, self._redraw_hq)

    def _redraw_hq(self) -> None:
        self._hq_after = None
        self._invalidate_scale_cache()
        self._redraw(resample=PILImage.LANCZOS)

    def _redraw(self, *, resample) -> None:
        if self._closing or self._left_pil is None or self._right_pil is None:
            return
        if self._mode == _MODE_SPLIT:
            self._redraw_split(resample=resample)
        else:
            self._redraw_pane(self._left_canvas, self._left_pil, "left", resample)
            self._redraw_pane(self._right_canvas, self._right_pil, "right", resample)

    def _scaled(self, image: PILImage.Image, zoom: float, resample) -> PILImage.Image:
        w = max(1, int(round(image.size[0] * zoom)))
        h = max(1, int(round(image.size[1] * zoom)))
        if w == image.size[0] and h == image.size[1]:
            return image
        return image.resize((w, h), resample)

    def _redraw_pane(
        self, canvas: tk.Canvas, image: PILImage.Image, pane: str, resample
    ) -> None:
        view = self._view_for(pane)
        canvas.delete("all")
        try:
            scaled = self._scaled(image, view.zoom, resample)
            photo = ImageTk.PhotoImage(scaled)
        except Exception:
            logging.exception("Compare pane render failed")
            return
        if pane == "left":
            self._photo_left = photo
            self._scaled_left = scaled
            self._scaled_zoom_left = view.zoom
            self._item_left = canvas.create_image(
                view.pan_x, view.pan_y, anchor="nw", image=photo
            )
        else:
            self._photo_right = photo
            self._scaled_right = scaled
            self._scaled_zoom_right = view.zoom
            self._item_right = canvas.create_image(
                view.pan_x, view.pan_y, anchor="nw", image=photo
            )

    def _redraw_split(self, *, resample=PILImage.BILINEAR) -> None:
        """Build PhotoImages once; clip frames handle the slider at 60fps cheaply."""
        try:
            cw = max(1, self._split_area.winfo_width())
            ch = max(1, self._split_area.winfo_height())
        except Exception:
            return
        if cw < _MIN_CANVAS_PX or ch < _MIN_CANVAS_PX:
            return
        view = self._shared
        try:
            left_s = self._scaled(self._left_pil, view.zoom, resample)
            right_s = self._scaled(self._right_pil, view.zoom, resample)
            photo_l = ImageTk.PhotoImage(left_s)
            photo_r = ImageTk.PhotoImage(right_s)
        except Exception:
            logging.exception("Compare split render failed")
            return

        self._photo_split_l = photo_l
        self._photo_split_r = photo_r
        self._scaled_left = left_s
        self._scaled_right = right_s
        self._scaled_zoom_left = view.zoom
        self._scaled_zoom_right = view.zoom

        self._split_left_cv.delete("all")
        self._split_right_cv.delete("all")
        sx = self._split_x()
        self._split_item_left = self._split_left_cv.create_image(
            view.pan_x, view.pan_y, anchor="nw", image=photo_l
        )
        self._split_item_right = self._split_right_cv.create_image(
            view.pan_x - sx, view.pan_y, anchor="nw", image=photo_r
        )
        self._layout_split_clips()


def open_image_compare_dialog(parent, paths: list[str]) -> Optional[ImageCompareDialog]:
    """Factory: open compare dialog for ``paths`` (needs ≥2 existing files)."""
    clean = []
    seen = set()
    for p in paths or []:
        if not p:
            continue
        np = os.path.normpath(str(p))
        key = os.path.normcase(np)
        if key in seen:
            continue
        if not os.path.isfile(np):
            continue
        seen.add(key)
        clean.append(np)
    if len(clean) < 2:
        try:
            messagebox.showinfo(
                "Compare Images",
                "Select at least two images to compare.",
                parent=parent,
            )
        except Exception:
            pass
        return None
    try:
        dlg = ImageCompareDialog(parent, clean)
    except ValueError:
        return None
    existing = getattr(parent, "_image_compare_dialogs", None)
    if existing is None:
        existing = []
        parent._image_compare_dialogs = existing

    def _forget(d=dlg):
        try:
            parent._image_compare_dialogs = [
                x for x in getattr(parent, "_image_compare_dialogs", []) if x is not d
            ]
        except Exception:
            pass

    existing.append(dlg)
    dlg.bind("<Destroy>", lambda e: _forget() if e.widget is dlg else None)
    return dlg
