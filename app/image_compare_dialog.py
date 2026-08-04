"""
Dual-mode image comparison dialog (Side-by-Side / Split Slider).

Opens borderless fullscreen (like the image viewer) with a compact bottom HUD.
Navigation is Lightroom-style: left = Reference (fixed), right = Target
(Prev/Next Target). ``Set as Reference`` promotes the target to the left pane.

Both images share one display frame ``(max_w × max_h)``. Each image is
letterboxed into that frame (aspect preserved) so Split Slider stays aligned
without stretching portrait/landscape pairs.
"""

from __future__ import annotations

import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
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
# Coalesce rapid wheel zooms so N notches ≠ N full resizes.
_ZOOM_COALESCE_MS = 16
# HQ was ~1.5s LANCZOS on 9.5k — delay longer and avoid LANCZOS on huge sources.
_HQ_DELAY_MS = 480
_HQ_LANCZOS_MAX_SIDE = 4096
_MIP_MIN_SIDE = 1024
_MIP_MAX_LEVELS = 5
_BG_RGB = (0x1A, 0x1A, 0x1A)
_BG_RGBA = (0x1A, 0x1A, 0x1A, 255)
# Prefer painting the whole frame when cheap — enables butter-smooth pan via coords.
_FULL_FRAME_PIXEL_BUDGET = 3_500_000
# Extra tile margin (fraction of canvas) so zoomed pan can slide without re-crop.
_PAN_OVERSCAN_FRAC = 0.45
_PAN_OVERSCAN_MIN = 96
# Refresh tile when visible area gets this close to the overscan edge.
_PAN_EDGE_MARGIN = 48
# Perf traces → app.log as [COMPARE-PERF]. Left off (hot-path logging cost).
# Re-enable: set True or VIBE_COMPARE_PERF=1
_COMPARE_PERF = False
# _COMPARE_PERF = os.environ.get("VIBE_COMPARE_PERF", "0").strip().lower() not in (
#     "0",
#     "false",
#     "no",
#     "off",
# )


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
        self._left_mips: list[PILImage.Image] = []
        self._right_mips: list[PILImage.Image] = []
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
        self._scaled_size: Optional[tuple[int, int]] = None
        self._hq_after: Optional[str] = None
        self._redraw_after: Optional[str] = None
        self._redraw_fast_pending = False
        # True when PhotoImages cover the full scaled frame (pan = canvas.coords only).
        self._pan_by_coords = True
        self._pane_covers_full = True
        self._fit_pending = True
        self._fit_retries = 0
        self._split_layouting = False
        self.is_fullscreen = False
        self._windowed_geometry: Optional[str] = None
        self._closing = False
        # Perf: wheel→paint latency + pan move aggregates (see [COMPARE-PERF] logs).
        self._perf_zoom_burst_t0: Optional[float] = None
        self._perf_zoom_notches = 0
        self._perf_zoom_from_z: Optional[float] = None
        self._perf_pan_n = 0
        self._perf_pan_sum_ms = 0.0
        self._perf_pan_max_ms = 0.0
        self._perf_pan_path = ""
        self._perf_parts: list[str] = []
        self._pending_redraw_reason = "redraw"
        self._paint_pool = ThreadPoolExecutor(max_workers=2)
        self._split_layout_key: Optional[tuple[int, int, int]] = None
        # Pan tiles: (fx0, fy0, tw, th, origin_x, origin_y) in frame-display pixels.
        self._tile_meta_left = None
        self._tile_meta_right = None
        self._tile_meta_split_l = None
        self._tile_meta_split_r = None

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

        # Map off-screen / invisible first, then fullscreen — avoids a windowed flash.
        self._place_near_parent(parent)
        try:
            self.attributes("-alpha", 0.0)
            self.withdraw()
        except Exception:
            pass
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
        self._mode_btn = ctk.CTkSegmentedButton(
            inner,
            values=[_MODE_SIDE, _MODE_SPLIT],
            variable=self._mode_var,
            command=self._on_mode_change,
            height=_CTRL_H,
            font=ctk.CTkFont(size=12),
        )
        self._mode_btn.pack(side="left", padx=(0, 10))

        self._sync_var = tk.BooleanVar(value=True)
        self._sync_cb = ctk.CTkCheckBox(
            inner,
            text="Sync Zoom & Pan",
            variable=self._sync_var,
            command=self._on_sync_toggle,
            font=ctk.CTkFont(size=12),
            checkbox_width=18,
            checkbox_height=18,
        )
        self._sync_cb.pack(side="left", padx=(0, 8))

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

        self._swap_btn = _btn("⇄", self._swap, 36)
        self._swap_btn.pack(side="left", padx=(0, 8))
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
        # Canvas size changed — refresh tiles (overscan depends on canvas dims).
        self._split_layout_key = None
        self._schedule_redraw(fast=True, coalesce=True)

    def _initial_load(self) -> None:
        try:
            self.deiconify()
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
        try:
            self.deiconify()
            self.attributes("-alpha", 1.0)
            self.lift()
            self.focus_force()
        except Exception:
            pass

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
            try:
                self.deiconify()
            except Exception:
                pass
            self.attributes("-fullscreen", True)
            try:
                self.attributes("-alpha", 1.0)
            except Exception:
                pass
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
        self._cancel_coalesced_redraw()
        if self._hq_after is not None:
            try:
                self.after_cancel(self._hq_after)
            except Exception:
                pass
            self._hq_after = None
        try:
            self._paint_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
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

    def _pil_for_compare(self, path: str) -> PILImage.Image:
        """Load as RGB when opaque (faster resize/PhotoImage than RGBA)."""
        im = load_pil_image(path)
        has_alpha = (
            im.mode in ("RGBA", "LA")
            or (im.mode == "P" and "transparency" in getattr(im, "info", {}))
        )
        return im.convert("RGBA" if has_alpha else "RGB")

    def _build_mips(self, image: PILImage.Image) -> list[PILImage.Image]:
        """Power-of-two downscales for cheap zoomed-out / fit renders."""
        levels = [image]
        cur = image
        t0 = time.perf_counter() if _COMPARE_PERF else 0.0
        while max(cur.size) > _MIP_MIN_SIDE and len(levels) < _MIP_MAX_LEVELS:
            nw = max(1, cur.size[0] // 2)
            nh = max(1, cur.size[1] // 2)
            cur = cur.resize((nw, nh), PILImage.BOX)
            levels.append(cur)
        if _COMPARE_PERF:
            sizes = "→".join(f"{im.size[0]}x{im.size[1]}" for im in levels)
            self._perf_log(
                "mips",
                f"{(time.perf_counter() - t0) * 1000:.0f}ms levels={len(levels)} {sizes}",
            )
        return levels

    def _load_compare(
        self, *, fit: bool = False, reload_left: bool = True, reload_right: bool = True
    ) -> None:
        lp, rp = self._left_path(), self._right_path()
        try:
            if reload_left:
                self._left_pil = self._pil_for_compare(lp)
                self._left_mips = self._build_mips(self._left_pil)
            if reload_right:
                self._right_pil = self._pil_for_compare(rp)
                self._right_mips = self._build_mips(self._right_pil)
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
            # Split always shares one view — hide sync (CTk disabled still looks blue).
            self._sync = True
            self._sync_var.set(True)
            try:
                self._sync_cb.pack_forget()
            except Exception:
                pass
        else:
            self._split_frame.pack_forget()
            self._sbs.pack(fill="both", expand=True)
            try:
                self._sync_cb.configure(state="normal")
                self._sync_cb.pack(
                    side="left", padx=(0, 8), before=self._swap_btn
                )
            except Exception:
                pass
        # Geometry (esp. split clip places) is not valid until idle — don't paint 1×1.
        try:
            self.update_idletasks()
        except Exception:
            pass
        self.after_idle(self._request_fit)

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
        """1:1 in the shared frame (each image letterboxed, aspect preserved)."""
        pane = "split" if self._mode == _MODE_SPLIT else "left"
        view = _ViewState(1.0, 0, 0)
        self._center_view(pane, view)
        if not self._sync and self._mode == _MODE_SIDE:
            self._center_view("right", _ViewState(1.0, 0, 0))
        self._schedule_redraw(fast=False)

    def _fit_window(self) -> None:
        """Fit the shared frame into the pane; never upscale past 1:1."""
        if self._mode == _MODE_SPLIT:
            widget = self._split_area
            pane = "split"
        else:
            widget = self._left_canvas
            pane = "left"
        if widget is None or self._left_pil is None or self._right_pil is None:
            return
        bw, bh = self._match_native_size()
        cw = max(1, widget.winfo_width())
        ch = max(1, widget.winfo_height())
        z = min(1.0, cw / float(bw), ch / float(bh))
        z = max(_MIN_ZOOM, min(_MAX_ZOOM, z))
        self._center_view(pane, _ViewState(z, 0, 0))
        if not self._sync and self._mode == _MODE_SIDE:
            self._center_view("right", _ViewState(z, 0, 0))
        self._schedule_redraw(fast=False)

    def _match_native_size(self) -> tuple[int, int]:
        """Shared compare frame: union of both native sizes (no stretch)."""
        lw, lh = self._left_pil.size
        rw, rh = self._right_pil.size
        return max(lw, rw), max(lh, rh)

    def _display_size_for_zoom(self, zoom: float) -> tuple[int, int]:
        """On-screen size of the shared frame at ``zoom`` (1.0 = native frame)."""
        bw, bh = self._match_native_size()
        return (
            max(1, int(round(bw * zoom))),
            max(1, int(round(bh * zoom))),
        )

    def _center_view(self, pane: str, view: _ViewState) -> None:
        widget = self._canvas_for(pane)
        if widget is None or self._left_pil is None or self._right_pil is None:
            self._set_view(pane, view)
            return
        cw = max(1, widget.winfo_width())
        ch = max(1, widget.winfo_height())
        dw, dh = self._display_size_for_zoom(view.zoom)
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

    # ---------------------------------------------------------- perf debug
    def _perf_log(self, kind: str, msg: str) -> None:
        if not _COMPARE_PERF:
            return
        logging.info("[COMPARE-PERF] %s | %s", kind, msg)

    def _perf_ctx(self) -> str:
        """Compact context string for zoom/pan logs."""
        try:
            lw = lh = rw = rh = 0
            if self._left_pil is not None:
                lw, lh = self._left_pil.size
            if self._right_pil is not None:
                rw, rh = self._right_pil.size
            bw, bh = self._match_native_size() if self._left_pil and self._right_pil else (0, 0)
            view = self._shared if self._sync or self._mode == _MODE_SPLIT else self._left_view
            z = view.zoom
            dw, dh = self._display_size_for_zoom(z) if bw else (0, 0)
            if self._mode == _MODE_SPLIT:
                cw = self._split_area.winfo_width()
                ch = self._split_area.winfo_height()
            else:
                cw = self._left_canvas.winfo_width()
                ch = self._left_canvas.winfo_height()
            vp = not self._pan_by_coords
            return (
                f"mode={self._mode} sync={int(self._sync)} "
                f"L={lw}x{lh} R={rw}x{rh} frame={bw}x{bh} "
                f"zoom={z:.4f} disp={dw}x{dh}({dw * dh / 1e6:.2f}MP) "
                f"canvas={cw}x{ch} path={'viewport' if vp else 'full-fit'} "
                f"pan_coords={int(self._pan_by_coords)}"
            )
        except Exception as e:
            return f"ctx_err={e}"

    def _perf_resample_name(self, resample) -> str:
        names = {
            PILImage.NEAREST: "NEAREST",
            PILImage.BILINEAR: "BILINEAR",
            PILImage.BICUBIC: "BICUBIC",
            PILImage.LANCZOS: "LANCZOS",
            PILImage.BOX: "BOX",
        }
        return names.get(resample, str(resample))

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
        old_dw, old_dh = self._display_size_for_zoom(old_z)
        new_dw, new_dh = self._display_size_for_zoom(new_z)
        # Keep the matched-frame point under the cursor stable.
        u = (cx - view.pan_x) / float(old_dw) if old_dw else 0.5
        v = (cy - view.pan_y) / float(old_dh) if old_dh else 0.5
        new_view = _ViewState(new_z, cx - u * new_dw, cy - v * new_dh)
        self._set_view(pane, new_view)
        if _COMPARE_PERF:
            now = time.perf_counter()
            if self._perf_zoom_burst_t0 is None:
                self._perf_zoom_burst_t0 = now
                self._perf_zoom_from_z = old_z
                self._perf_zoom_notches = 0
            self._perf_zoom_notches += 1
        self._schedule_redraw(fast=True, coalesce=True)

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
        if _COMPARE_PERF:
            self._perf_pan_n = 0
            self._perf_pan_sum_ms = 0.0
            self._perf_pan_max_ms = 0.0
            self._perf_pan_paint_n = 0
            self._perf_pan_path = "coords" if self._pan_by_coords else "viewport-redraw"
            self._perf_log("pan-start", self._perf_ctx())
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
        t0 = time.perf_counter() if _COMPARE_PERF else 0.0
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
        if _COMPARE_PERF:
            # Coalesced viewport pan: this is only schedule cost; paint logs separately.
            dt = (time.perf_counter() - t0) * 1000.0
            self._perf_pan_n += 1
            self._perf_pan_sum_ms += dt
            if dt > self._perf_pan_max_ms:
                self._perf_pan_max_ms = dt
            self._perf_pan_path = "coords" if self._pan_by_coords else "viewport-redraw"
            if dt >= 8.0:
                self._perf_log(
                    "pan-move-slow",
                    f"{dt:.1f}ms path={self._perf_pan_path} | {self._perf_ctx()}",
                )

    def _pan_end(self, event=None) -> None:
        if _COMPARE_PERF and self._perf_pan_n:
            avg = self._perf_pan_sum_ms / max(1, self._perf_pan_n)
            self._perf_log(
                "pan-end",
                f"moves={self._perf_pan_n} avg={avg:.2f}ms max={self._perf_pan_max_ms:.1f}ms "
                f"path={self._perf_pan_path} | {self._perf_ctx()}",
            )
        self._panning = False
        self._pan_pane = None

    def _reposition_views(self) -> None:
        """Fast pan: slide cached tiles; re-crop only when the visible area leaves the tile."""
        if self._mode == _MODE_SPLIT:
            ok = self._nudge_split_tiles()
        else:
            ok = self._nudge_sbs_tiles()
        if not ok:
            self._schedule_redraw(fast=True, coalesce=True)

    def _nudge_sbs_tiles(self) -> bool:
        lv = self._view_for("left")
        rv = self._view_for("right")
        try:
            lcw = max(1, self._left_canvas.winfo_width())
            lch = max(1, self._left_canvas.winfo_height())
            rcw = max(1, self._right_canvas.winfo_width())
            rch = max(1, self._right_canvas.winfo_height())
        except Exception:
            return False
        ok_l = self._nudge_tile(
            self._left_canvas, self._item_left, self._tile_meta_left, lv, lcw, lch
        )
        ok_r = self._nudge_tile(
            self._right_canvas, self._item_right, self._tile_meta_right, rv, rcw, rch
        )
        return bool(ok_l and ok_r)

    def _nudge_split_tiles(self) -> bool:
        view = self._shared
        sx = self._split_x()
        meta_r = self._tile_meta_split_r
        # Stale right origin after slider move → would yank the image horizontally.
        if meta_r is not None and abs(meta_r[4] + float(sx)) > 0.5:
            return False
        try:
            cw = max(1, self._split_area.winfo_width())
            ch = max(1, self._split_area.winfo_height())
            lc_w = max(1, sx)
            lc_h = max(1, ch)
            rc_w = max(1, cw - sx)
            rc_h = max(1, ch)
        except Exception:
            return False
        ok_l = self._nudge_tile(
            self._split_left_cv,
            self._split_item_left,
            self._tile_meta_split_l,
            view,
            lc_w,
            lc_h,
        )
        ok_r = self._nudge_tile(
            self._split_right_cv,
            self._split_item_right,
            self._tile_meta_split_r,
            view,
            rc_w,
            rc_h,
        )
        return bool(ok_l and ok_r)

    def _nudge_tile(
        self, canvas, item, meta, view: _ViewState, canvas_w: int, canvas_h: int
    ) -> bool:
        """Move a pan tile by coords if the visible region still fits inside it."""
        if item is None or meta is None or view is None:
            return False
        tile_fx0, tile_fy0, tile_w, tile_h, origin_x, origin_y = meta
        bw, bh = self._match_native_size()
        z = float(view.zoom) if view.zoom else 1.0
        dw = max(1, int(round(bw * z)))
        dh = max(1, int(round(bh * z)))
        frame_x = view.pan_x + origin_x
        frame_y = view.pan_y + origin_y
        vis_l = max(0.0, -frame_x)
        vis_t = max(0.0, -frame_y)
        vis_r = min(float(dw), canvas_w - frame_x)
        vis_b = min(float(dh), canvas_h - frame_y)
        m = float(_PAN_EDGE_MARGIN)
        if (
            vis_l < tile_fx0 + m
            or vis_t < tile_fy0 + m
            or vis_r > tile_fx0 + tile_w - m
            or vis_b > tile_fy0 + tile_h - m
        ):
            return False
        try:
            canvas.coords(item, frame_x + tile_fx0, frame_y + tile_fy0)
        except Exception:
            return False
        return True

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
            sx = self._split_x()
            # Move clips immediately; never nudge with stale origin_x (causes L/R wobble).
            self._layout_split_clips(nudge=False)
            self._retarget_split_right_origin(sx)
            self._schedule_redraw(fast=True, coalesce=True)
        elif self._panning:
            self._pan_move(event, "split")

    def _split_release(self, event=None) -> None:
        if self._dragging_split:
            self._dragging_split = False
            self._split_layout_key = None
            self._schedule_redraw(fast=True, coalesce=False)
        else:
            self._pan_end(event)

    def _layout_split_clips(self, *, nudge: bool = False) -> None:
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
            self._split_layout_key = (cw, ch, sx)
            # Nudge only when right tile origin still matches this sx.
            if nudge:
                self._nudge_split_tiles()
        finally:
            self._split_layouting = False

    def _retarget_split_right_origin(self, sx: int) -> None:
        """Keep right tile aligned after slider move without waiting for re-rasterize."""
        meta = self._tile_meta_split_r
        item = self._split_item_right
        if meta is None or item is None:
            return
        fx0, fy0, tw, th, old_ox, oy = meta
        new_ox = -float(sx)
        if abs(old_ox - new_ox) < 1e-9:
            return
        self._tile_meta_split_r = (fx0, fy0, tw, th, new_ox, oy)
        view = self._shared
        try:
            self._split_right_cv.coords(
                item,
                view.pan_x + new_ox + fx0,
                view.pan_y + oy + fy0,
            )
        except Exception:
            pass

    def _reposition_split_images(self) -> None:
        """Compatibility wrapper — prefer tile nudge."""
        self._nudge_split_tiles()

    # ------------------------------------------------------------- redraw
    def _invalidate_scale_cache(self) -> None:
        self._scaled_left = None
        self._scaled_right = None
        self._scaled_zoom_left = None
        self._scaled_zoom_right = None
        self._scaled_size = None

    def _cancel_coalesced_redraw(self) -> None:
        if self._redraw_after is not None:
            try:
                self.after_cancel(self._redraw_after)
            except Exception:
                pass
            self._redraw_after = None

    def _schedule_redraw(
        self, *, fast: bool, coalesce: bool = False, reason: str = "redraw"
    ) -> None:
        if self._hq_after is not None:
            try:
                self.after_cancel(self._hq_after)
            except Exception:
                pass
            self._hq_after = None
        self._invalidate_scale_cache()
        if coalesce:
            # Keep latest view; flush at most once per _ZOOM_COALESCE_MS.
            self._redraw_fast_pending = fast
            # Prefer zoom/pan labels if a burst is active.
            if self._perf_zoom_burst_t0 is not None:
                self._pending_redraw_reason = "zoom"
            elif self._panning:
                self._pending_redraw_reason = "pan"
            else:
                self._pending_redraw_reason = reason
            if self._redraw_after is None:
                self._redraw_after = self.after(
                    _ZOOM_COALESCE_MS, self._flush_coalesced_redraw
                )
            return
        self._cancel_coalesced_redraw()
        self._redraw(
            resample=self._interactive_resample() if fast else self._hq_resample(),
            reason=reason,
        )
        if fast:
            self._hq_after = self.after(_HQ_DELAY_MS, self._redraw_hq)

    def _flush_coalesced_redraw(self) -> None:
        self._redraw_after = None
        if self._closing:
            return
        fast = self._redraw_fast_pending
        reason = getattr(self, "_pending_redraw_reason", None) or "coalesce"
        self._redraw(
            resample=self._interactive_resample() if fast else self._hq_resample(),
            reason=reason,
        )
        if fast:
            self._hq_after = self.after(_HQ_DELAY_MS, self._redraw_hq)

    def _interactive_resample(self):
        # BOX via _resize_filtered for big downscales; BILINEAR near 1:1.
        return PILImage.BILINEAR

    def _hq_resample(self):
        """LANCZOS only for moderate sources — logs showed ~1.5s on 9.5k."""
        try:
            sides = []
            for im in (self._left_pil, self._right_pil):
                if im is not None:
                    sides.extend(im.size)
            if sides and max(sides) > _HQ_LANCZOS_MAX_SIDE:
                return PILImage.BILINEAR
        except Exception:
            pass
        return PILImage.LANCZOS

    def _redraw_hq(self) -> None:
        self._hq_after = None
        self._invalidate_scale_cache()
        self._redraw(resample=self._hq_resample(), reason="hq")

    def _redraw(self, *, resample, reason: str = "redraw") -> None:
        if self._closing or self._left_pil is None or self._right_pil is None:
            return
        if not self._left_mips:
            self._left_mips = self._build_mips(self._left_pil)
        if not self._right_mips:
            self._right_mips = self._build_mips(self._right_pil)
        self._pan_by_coords = True
        self._pane_covers_full = True
        self._perf_parts = []
        t0 = time.perf_counter()
        if self._mode == _MODE_SPLIT:
            self._redraw_split(resample=resample)
        else:
            self._redraw_side_by_side(resample=resample)
        # Pan always uses tile nudge (full-frame or overscan); re-crop only at edges.
        self._pan_by_coords = True
        if not _COMPARE_PERF:
            self._perf_zoom_burst_t0 = None
            self._perf_zoom_notches = 0
            return
        total_ms = (time.perf_counter() - t0) * 1000.0
        latency_ms = None
        notches = self._perf_zoom_notches
        from_z = self._perf_zoom_from_z
        if self._perf_zoom_burst_t0 is not None and reason == "zoom":
            latency_ms = (time.perf_counter() - self._perf_zoom_burst_t0) * 1000.0
        log_it = reason in ("zoom", "hq") or total_ms >= 8.0
        if reason == "pan":
            self._perf_pan_paint_n = getattr(self, "_perf_pan_paint_n", 0) + 1
            log_it = total_ms >= 8.0 or (self._perf_pan_paint_n % 5 == 1)
        if log_it:
            parts = " ".join(self._perf_parts) if self._perf_parts else "-"
            lat = (
                f" wheel→paint={latency_ms:.0f}ms notches={notches}"
                if latency_ms is not None
                else ""
            )
            zspan = ""
            if from_z is not None and reason == "zoom":
                view = (
                    self._shared
                    if self._sync or self._mode == _MODE_SPLIT
                    else self._left_view
                )
                zspan = f" z={from_z:.4f}→{view.zoom:.4f}"
            self._perf_log(
                reason,
                f"total={total_ms:.1f}ms resample={self._perf_resample_name(resample)}"
                f"{lat}{zspan} | {parts} | {self._perf_ctx()}",
            )
        if reason == "zoom":
            self._perf_zoom_burst_t0 = None
            self._perf_zoom_notches = 0
            self._perf_zoom_from_z = None

    def _resize_filtered(
        self, image: PILImage.Image, size: tuple[int, int], resample
    ) -> PILImage.Image:
        """Resize with a fast BOX path for large interactive downscales."""
        tw, th = size
        if tw < 1 or th < 1:
            return image
        if image.size == (tw, th):
            return image
        sw, sh = image.size
        if resample != PILImage.LANCZOS and (tw * 2 < sw or th * 2 < sh):
            return image.resize((tw, th), PILImage.BOX)
        return image.resize((tw, th), resample)

    def _pick_mip(
        self,
        mips: list[PILImage.Image],
        full_box: tuple[int, int, int, int],
        out_w: int,
        out_h: int,
    ) -> tuple[PILImage.Image, int, tuple[int, int, int, int]]:
        """Choose coarsest mip that still has enough pixels for out_w×out_h."""
        full = mips[0]
        fw = max(1, full.size[0])
        src_w = max(1, full_box[2] - full_box[0])
        src_h = max(1, full_box[3] - full_box[1])
        best_i = 0
        for i, mip in enumerate(mips):
            scale = fw / float(max(1, mip.size[0]))
            mip_w = src_w / scale
            mip_h = src_h / scale
            if mip_w >= out_w * 0.95 and mip_h >= out_h * 0.95:
                best_i = i
            else:
                break
        mip = mips[best_i]
        scale = fw / float(max(1, mip.size[0]))
        box = (
            max(0, int(math.floor(full_box[0] / scale))),
            max(0, int(math.floor(full_box[1] / scale))),
            min(mip.size[0], int(math.ceil(full_box[2] / scale))),
            min(mip.size[1], int(math.ceil(full_box[3] / scale))),
        )
        if box[2] <= box[0]:
            box = (box[0], box[1], min(mip.size[0], box[0] + 1), box[3])
        if box[3] <= box[1]:
            box = (box[0], box[1], box[2], min(mip.size[1], box[1] + 1))
        return mip, best_i, box

    def _prepare_viewport_pil(
        self,
        mips: list[PILImage.Image],
        view: _ViewState,
        canvas_w: int,
        canvas_h: int,
        resample,
        *,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
        tag: str = "vp",
    ):
        """
        Pure-PIL viewport decode (thread-safe).

        Paints either the full frame (when cheap) or a visible+overscan tile so
        pan can slide via canvas.coords without re-cropping every move.

        Returns ``(pil, place_x, place_y, tile_meta, detail, prep_ms)`` or ``None``.
        ``tile_meta`` = ``(fx0, fy0, tw, th, origin_x, origin_y)`` in frame-display px.
        """
        if not mips:
            return None
        full = mips[0]
        iw, ih = full.size
        bw, bh = self._match_native_size()
        z = float(view.zoom) if view.zoom else 1.0
        dw = max(1, int(round(bw * z)))
        dh = max(1, int(round(bh * z)))
        frame_x = view.pan_x + origin_x
        frame_y = view.pan_y + origin_y

        # Visible shared-frame ∩ canvas
        f_l = max(0.0, frame_x)
        f_t = max(0.0, frame_y)
        f_r = min(float(canvas_w), frame_x + dw)
        f_b = min(float(canvas_h), frame_y + dh)
        if f_r <= f_l + 1e-6 or f_b <= f_t + 1e-6:
            return None

        budget = max(_FULL_FRAME_PIXEL_BUDGET, int(canvas_w) * int(canvas_h) * 3)
        if dw * dh <= budget:
            # Whole frame fits budget — smoothest pan path.
            t_l, t_t = frame_x, frame_y
            t_r, t_b = frame_x + float(dw), frame_y + float(dh)
        else:
            mx = max(float(_PAN_OVERSCAN_MIN), canvas_w * _PAN_OVERSCAN_FRAC)
            my = max(float(_PAN_OVERSCAN_MIN), canvas_h * _PAN_OVERSCAN_FRAC)
            t_l = max(frame_x, f_l - mx)
            t_t = max(frame_y, f_t - my)
            t_r = min(frame_x + dw, f_r + mx)
            t_b = min(frame_y + dh, f_b + my)

        out_w = max(1, int(math.ceil(t_r - t_l)))
        out_h = max(1, int(math.ceil(t_b - t_t)))
        tile_fx0 = t_l - frame_x
        tile_fy0 = t_t - frame_y
        tile_meta = (tile_fx0, tile_fy0, float(out_w), float(out_h), origin_x, origin_y)

        # Contain image in frame (uniform scale + centered letterbox).
        uniform = min(bw / float(iw), bh / float(ih))
        fit_w = iw * uniform
        fit_h = ih * uniform
        ox = (bw - fit_w) / 2.0
        oy = (bh - fit_h) / 2.0
        content_x = frame_x + ox * z
        content_y = frame_y + oy * z
        content_r = content_x + fit_w * z
        content_b = content_y + fit_h * z

        bg = _BG_RGBA if full.mode == "RGBA" else _BG_RGB
        tile = PILImage.new(full.mode, (out_w, out_h), bg)

        t0 = time.perf_counter()
        level = -1
        box = (0, 0, 0, 0)
        paste_w = paste_h = 0
        use_box = False

        c_l = max(t_l, content_x)
        c_t = max(t_t, content_y)
        c_r = min(t_r, content_r)
        c_b = min(t_b, content_b)
        if c_r > c_l + 1e-6 and c_b > c_t + 1e-6:
            inv = 1.0 / (uniform * z)
            sx0 = (c_l - content_x) * inv
            sy0 = (c_t - content_y) * inv
            sx1 = (c_r - content_x) * inv
            sy1 = (c_b - content_y) * inv
            full_box = (
                max(0, min(iw - 1, int(math.floor(sx0)))),
                max(0, min(ih - 1, int(math.floor(sy0)))),
                max(1, min(iw, int(math.ceil(sx1)))),
                max(1, min(ih, int(math.ceil(sy1)))),
            )
            if full_box[2] <= full_box[0]:
                full_box = (full_box[0], full_box[1], full_box[0] + 1, full_box[3])
            if full_box[3] <= full_box[1]:
                full_box = (full_box[0], full_box[1], full_box[2], full_box[1] + 1)

            paste_w = max(1, int(math.ceil(c_r - c_l)))
            paste_h = max(1, int(math.ceil(c_b - c_t)))
            mip, level, box = self._pick_mip(mips, full_box, paste_w, paste_h)
            cropped = mip.crop(box)
            sw, sh = cropped.size
            use_box = resample != PILImage.LANCZOS and (
                paste_w * 2 < sw or paste_h * 2 < sh
            )
            scaled = self._resize_filtered(cropped, (paste_w, paste_h), resample)
            paste_x = int(round(c_l - t_l))
            paste_y = int(round(c_t - t_t))
            if paste_x < 0:
                scaled = scaled.crop((-paste_x, 0, scaled.size[0], scaled.size[1]))
                paste_x = 0
            if paste_y < 0:
                scaled = scaled.crop((0, -paste_y, scaled.size[0], scaled.size[1]))
                paste_y = 0
            if paste_x < out_w and paste_y < out_h:
                tile.paste(scaled, (paste_x, paste_y))

        t2 = time.perf_counter()
        filt = "BOX" if use_box else self._perf_resample_name(resample)
        detail = (
            f"{tag}:mip{level} crop={(t2 - t0) * 1000:.1f}ms"
            f"({box[2] - box[0]}x{box[3] - box[1]}) "
            f"→{paste_w}x{paste_h}/{filt} tile={out_w}x{out_h}"
        )
        return (
            tile,
            t_l,
            t_t,
            tile_meta,
            detail,
            (t2 - t0) * 1000.0,
        )

    def _commit_photo(
        self,
        canvas: tk.Canvas,
        prepared,
        tag: str,
        *,
        item=None,
    ):
        """Main-thread PhotoImage + canvas item from prepare result.

        Reuses ``item`` via itemconfig when possible to avoid delete→blank→create flicker.
        Returns ``(photo, item, tile_meta)``.
        """
        if prepared is None:
            canvas.delete("all")
            return None, None, None
        pil_im, place_x, place_y, tile_meta, detail, prep_ms = prepared
        t0 = time.perf_counter()
        photo = ImageTk.PhotoImage(pil_im)
        photo_ms = (time.perf_counter() - t0) * 1000.0
        if item is not None:
            try:
                canvas.itemconfig(item, image=photo)
                canvas.coords(item, place_x, place_y)
            except Exception:
                item = None
        if item is None:
            canvas.delete("all")
            item = canvas.create_image(place_x, place_y, anchor="nw", image=photo)
        if _COMPARE_PERF:
            self._perf_parts.append(f"{detail} PhotoImage={photo_ms:.1f}ms")
        return photo, item, tile_meta

    def _redraw_side_by_side(self, *, resample) -> None:
        """Prepare L/R in parallel (PIL), then PhotoImage on the UI thread."""
        lv = self._view_for("left")
        rv = self._view_for("right")
        try:
            lcw = max(1, self._left_canvas.winfo_width())
            lch = max(1, self._left_canvas.winfo_height())
            rcw = max(1, self._right_canvas.winfo_width())
            rch = max(1, self._right_canvas.winfo_height())
        except Exception:
            return

        def _left():
            return self._prepare_viewport_pil(
                self._left_mips, lv, lcw, lch, resample, tag="l"
            )

        def _right():
            return self._prepare_viewport_pil(
                self._right_mips, rv, rcw, rch, resample, tag="r"
            )

        try:
            fl = self._paint_pool.submit(_left)
            fr = self._paint_pool.submit(_right)
            left_p = fl.result()
            right_p = fr.result()
        except Exception:
            logging.exception("Compare parallel prepare failed; serial fallback")
            left_p = _left()
            right_p = _right()

        self._photo_left, self._item_left, self._tile_meta_left = self._commit_photo(
            self._left_canvas, left_p, "l", item=self._item_left
        )
        self._photo_right, self._item_right, self._tile_meta_right = self._commit_photo(
            self._right_canvas, right_p, "r", item=self._item_right
        )
        self._scaled_zoom_left = lv.zoom
        self._scaled_zoom_right = rv.zoom
        self._scaled_size = self._display_size_for_zoom(lv.zoom)

    def _redraw_pane(
        self, canvas: tk.Canvas, image: PILImage.Image, pane: str, resample
    ) -> None:
        """Single-pane path (kept for pan_start fallback)."""
        mips = self._left_mips if pane == "left" else self._right_mips
        if not mips and image is not None:
            mips = [image]
        view = self._view_for(pane)
        try:
            cw = max(1, canvas.winfo_width())
            ch = max(1, canvas.winfo_height())
        except Exception:
            return
        prepared = self._prepare_viewport_pil(
            mips, view, cw, ch, resample, tag=pane[:1]
        )
        prev = self._item_left if pane == "left" else self._item_right
        photo, item, meta = self._commit_photo(
            canvas, prepared, pane[:1], item=prev
        )
        if pane == "left":
            self._photo_left = photo
            self._item_left = item
            self._tile_meta_left = meta
            self._scaled_zoom_left = view.zoom
        else:
            self._photo_right = photo
            self._item_right = item
            self._tile_meta_right = meta
            self._scaled_zoom_right = view.zoom
        self._scaled_size = self._display_size_for_zoom(view.zoom)

    def _redraw_split(self, *, resample=PILImage.BILINEAR) -> None:
        """Paint both sides; clip frames handle the slider without re-encode."""
        try:
            cw = max(1, self._split_area.winfo_width())
            ch = max(1, self._split_area.winfo_height())
        except Exception:
            return
        if cw < _MIN_CANVAS_PX or ch < _MIN_CANVAS_PX:
            # Not mapped yet — keep fit pending so configure/retry can repaint.
            self._fit_pending = True
            return
        view = self._shared
        sx = self._split_x()
        layout_key = (cw, ch, sx)
        # Avoid place()+stale-nudge on every pan paint.
        if self._split_layout_key != layout_key or self._split_item_left is None:
            self._layout_split_clips(nudge=False)
            try:
                self.update_idletasks()
            except Exception:
                pass
        # Use place() geometry only — winfo can lag by 1px and wobble letterboxed images.
        lc_w = max(1, sx)
        lc_h = max(1, ch)
        rc_w = max(1, cw - sx)
        rc_h = max(1, ch)

        def _left():
            return self._prepare_viewport_pil(
                self._left_mips, view, lc_w, lc_h, resample, tag="L"
            )

        def _right():
            return self._prepare_viewport_pil(
                self._right_mips,
                view,
                rc_w,
                rc_h,
                resample,
                origin_x=-float(sx),
                tag="R",
            )

        try:
            fl = self._paint_pool.submit(_left)
            fr = self._paint_pool.submit(_right)
            left_p = fl.result()
            right_p = fr.result()
        except Exception:
            left_p = _left()
            right_p = _right()

        self._photo_split_l, self._split_item_left, self._tile_meta_split_l = (
            self._commit_photo(
                self._split_left_cv, left_p, "L", item=self._split_item_left
            )
        )
        self._photo_split_r, self._split_item_right, self._tile_meta_split_r = (
            self._commit_photo(
                self._split_right_cv, right_p, "R", item=self._split_item_right
            )
        )
        self._scaled_zoom_left = view.zoom
        self._scaled_zoom_right = view.zoom
        self._scaled_size = self._display_size_for_zoom(view.zoom)



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
    if _COMPARE_PERF:
        logging.info(
            "[COMPARE-PERF] compare opened (%d images) — look for [COMPARE-PERF] lines "
            "(zoom / pan-end / pan-move-slow). Disable: VIBE_COMPARE_PERF=0",
            len(clean),
        )
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
