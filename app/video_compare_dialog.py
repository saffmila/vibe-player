"""
Side-by-Side video comparison dialog (OpenCV backend).

Same visual shell as image compare (fullscreen + bottom HUD, Lightroom-style
Reference / Target navigation). Playback is silent OpenCV dual-decode — not VLC
(main app player stays on VLC). Seek/zoom are frame-accurate and synced.
"""

from __future__ import annotations

import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import customtkinter as ctk
import cv2
import numpy as np
import tkinter as tk
from tkinter import messagebox
from PIL import Image as PILImage
from PIL import ImageTk

from utils import get_video_size
from vtp_constants import VIDEO_FORMATS

_BG = "#1a1a1a"
_HUD_BG = "#252525"
_BTN_FG = "gray30"
_BTN_HOVER = "gray25"
_CORNER = 6
_CTRL_H = 28
_LAYOUT_FIT = "Fit"
_LAYOUT_1TO1 = "1:1"
_ZOOM_LABELS = ("1×", "2×", "3×", "4×", "8×")
_ZOOM_FACTORS = {"1×": 1.0, "2×": 2.0, "3×": 3.0, "4×": 4.0, "8×": 8.0}
# During play/scrub: resize via this long-side budget, then upscale to Fit box
# (on-screen frame size stays Fit; CPU work drops a lot on laptop).
_FAST_LONG_SIDE = 720


def _format_bytes(n: int) -> str:
    """Formats an integer byte count into a human-readable string (KB, MB, GB)."""
    n = max(0, int(n))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return f"{n} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _format_clock(seconds: float) -> str:
    """Formats a float representing seconds into a MM:SS or H:MM:SS clock string."""
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        s = 0
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def _meta_line(path: str, *, width: int = 0, height: int = 0) -> str:
    """Constructs a metadata string containing file name, dimensions, size, and format."""
    name = os.path.basename(path)
    try:
        size = _format_bytes(os.path.getsize(path))
    except OSError:
        size = "?"
    w, h = width, height
    if w <= 0 or h <= 0:
        try:
            gw, gh = get_video_size(path)
            if gw and gh:
                w, h = int(gw), int(gh)
        except Exception:
            pass
    dims = f"{w}×{h}" if w and h else "?×?"
    ext = os.path.splitext(path)[1].lstrip(".").upper() or "VIDEO"
    return f"{name}  ·  {dims}  ·  {size}  ·  {ext}"


class _OpenCvClip:
    """Silent OpenCV reader with reliable ratio seek (no audio)."""

    def __init__(self, path: str):
        """Initializes the clip, sets up defaults, and opens the video file."""
        self.path = os.path.normpath(path)
        self.cap: Optional[cv2.VideoCapture] = None
        self.fps = 25.0
        self.frame_count = 0
        self.duration = 0.0
        self.width = 0
        self.height = 0
        self.frame_idx = 0
        self.frame_bgr: Optional[np.ndarray] = None
        self._open(self.path)

    def _open(self, path: str) -> bool:
        """
        Opens the video file and initializes properties.
        Attempts to use hardware acceleration to offload the CPU during decoding.
        """
        self.close()
        self.path = os.path.normpath(path)

        # Define parameters to request hardware acceleration
        params = [
            cv2.CAP_PROP_HW_ACCELERATION, 
            cv2.VIDEO_ACCELERATION_ANY
        ]

        # Try to open the video with hardware acceleration enabled
        cap = cv2.VideoCapture(self.path, cv2.CAP_FFMPEG, params)

        # Fallback to default CPU decoding if HW acceleration is not supported
        if not cap.isOpened():
            cap = cv2.VideoCapture(self.path)

        if not cap.isOpened():
            self.cap = None
            logging.error("[VideoCompare] OpenCV failed to open %s", self.path)
            return False

        self.cap = cap
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        self.fps = fps if fps > 1e-3 else 25.0
        self.frame_count = max(0, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

        if self.frame_count > 0:
            self.duration = self.frame_count / self.fps
        else:
            self.duration = 0.0

        self.frame_idx = 0
        self.frame_bgr = None
        
        # Read the first frame to initialize
        self.read_at_index(0)
        return True

    def close(self) -> None:
        """Releases the OpenCV VideoCapture object and clears the frame buffer."""
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = None
        self.frame_bgr = None

    def reopen(self, path: str) -> bool:
        """Closes the current clip and attempts to open a new path."""
        return self._open(path)

    @property
    def ratio(self) -> float:
        """Calculates the playback progress as a float between 0.0 and 1.0."""
        if self.frame_count <= 1:
            return 0.0
        return max(0.0, min(1.0, self.frame_idx / float(self.frame_count - 1)))

    @property
    def time_s(self) -> float:
        """Returns the timeline time in seconds based on frame index and source fps."""
        if self.fps > 1e-6:
            return self.frame_idx / self.fps
        if self.duration > 0 and self.frame_count > 1:
            return self.ratio * self.duration
        return 0.0

    def read_at_index(self, idx: int) -> bool:
        """Decodes and retrieves a specific frame by its index."""
        if self.cap is None:
            return False
        n = max(1, self.frame_count)
        idx = int(max(0, min(n - 1, idx)))
        try:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))
            ok, frame = self.cap.read()
        except Exception:
            logging.debug("[VideoCompare] read_at_index failed", exc_info=True)
            return False
        if not ok or frame is None:
            return False
        self.frame_bgr = frame
        self.frame_idx = idx
        if self.width <= 0 or self.height <= 0:
            self.height, self.width = frame.shape[:2]
        return True

    def seek_ratio(self, ratio: float) -> bool:
        """Seeks to a specific playback percentage (0.0 to 1.0)."""
        ratio = max(0.0, min(1.0, float(ratio)))
        if self.frame_count <= 1:
            return self.read_at_index(0)
        idx = int(round(ratio * (self.frame_count - 1)))
        return self.seek_index(idx)

    def seek_index(self, idx: int) -> bool:
        """Prefer sequential reads when scrubbing/playing forward (fast on H.264)."""
        if self.cap is None:
            return False
        n = max(1, self.frame_count)
        idx = int(max(0, min(n - 1, idx)))
        if idx == self.frame_idx and self.frame_bgr is not None:
            return True
        # Small forward steps: decode sequentially (avoids expensive keyframe seeks).
        if idx > self.frame_idx and (idx - self.frame_idx) <= 45:
            while self.frame_idx < idx:
                if not self.advance_one():
                    return self.read_at_index(idx)
            return True
        return self.read_at_index(idx)

    def seek_time(self, seconds: float) -> bool:
        """Seeks to a specific time in seconds."""
        if self.fps > 1e-6:
            return self.seek_index(int(round(float(seconds) * self.fps)))
        if self.duration > 0:
            return self.seek_ratio(float(seconds) / self.duration)
        return False

    def advance_one(self) -> bool:
        """Decodes the immediately following frame (useful for normal playback speed)."""
        if self.cap is None:
            return False
        if self.frame_count > 0 and self.frame_idx >= self.frame_count - 1:
            return False
        try:
            ok, frame = self.cap.read()
        except Exception:
            return False
        if not ok or frame is None:
            return False
        self.frame_bgr = frame
        self.frame_idx = min(self.frame_idx + 1, max(0, self.frame_count - 1))
        return True


def _viewport_size(
    src_w: int,
    src_h: int,
    pane_w: int,
    pane_h: int,
    layout: str,
) -> tuple[int, int]:
    """Fixed on-screen frame size (independent of zoom)."""
    if src_w <= 0 or src_h <= 0 or pane_w <= 0 or pane_h <= 0:
        return 1, 1
    if layout == _LAYOUT_1TO1:
        # Native pixels; canvas clips if larger than pane.
        return max(1, int(src_w)), max(1, int(src_h))
    # Fit = contain: largest AR-preserving rect inside pane (touches edges on one axis).
    scale = min(pane_w / float(src_w), pane_h / float(src_h))
    return max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale)))


def _paint_frame(
    frame_bgr: np.ndarray,
    pane_w: int,
    pane_h: int,
    zoom: float,
    layout: str,
    *,
    fast: bool = False,
) -> Optional[PILImage.Image]:
    """
    1) Compute fixed viewport (Fit or 1:1) from full-frame size.
    2) Center-crop source by zoom.
    3) Scale crop to fill that same viewport (frame size does not change with zoom).

    ``fast`` may use a cheaper intermediate size, then upscale back to the full
    viewport — on-screen Fit box stays the same, only sharpness drops a bit.
    """
    if frame_bgr is None or pane_w < 2 or pane_h < 2:
        return None
    src_h, src_w = frame_bgr.shape[:2]
    if src_w <= 0 or src_h <= 0:
        return None

    vw, vh = _viewport_size(src_w, src_h, pane_w, pane_h, layout)

    z = max(1.0, float(zoom or 1.0))
    if z > 1.01:
        cw = max(2, int(round(src_w / z)))
        ch = max(2, int(round(src_h / z)))
        cw = min(cw, src_w)
        ch = min(ch, src_h)
        x = max(0, (src_w - cw) // 2)
        y = max(0, (src_h - ch) // 2)
        crop = frame_bgr[y : y + ch, x : x + cw]
    else:
        crop = frame_bgr

    try:
        if fast and max(vw, vh) > _FAST_LONG_SIDE:
            s = _FAST_LONG_SIDE / float(max(vw, vh))
            iw = max(1, int(round(vw * s)))
            ih = max(1, int(round(vh * s)))
            mid = cv2.resize(crop, (iw, ih), interpolation=cv2.INTER_LINEAR)
            resized = cv2.resize(mid, (vw, vh), interpolation=cv2.INTER_LINEAR)
        else:
            interp = cv2.INTER_LINEAR if fast or z > 1.01 else cv2.INTER_AREA
            resized = cv2.resize(crop, (vw, vh), interpolation=interp)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        return PILImage.fromarray(rgb)
    except Exception:
        logging.debug("[VideoCompare] paint resize failed", exc_info=True)
        return None


def _paint_frame_job(args: tuple) -> Optional[PILImage.Image]:
    """Thread-pool entry (picklable-ish via plain tuple)."""
    frame_bgr, pane_w, pane_h, zoom, layout, fast = args
    return _paint_frame(frame_bgr, pane_w, pane_h, zoom, layout, fast=fast)


class VideoCompareDialog(ctk.CTkToplevel):
    """Fullscreen Side-by-Side video compare (Reference | Target) via OpenCV."""

    def __init__(self, parent, paths: list[str], *, title: str = "Compare Videos"):
        """Initializes the comparison dialog and configures the basic GUI layout."""
        super().__init__(parent)
        self.title(title)
        self.paths = [os.path.normpath(p) for p in paths if p and os.path.isfile(p)]
        if len(self.paths) < 2:
            self.destroy()
            raise ValueError("VideoCompareDialog requires at least two existing video paths")

        self._controller = parent
        self._ref_index = 0
        self._target_index = 1
        self._sync = True
        self._closing = False
        self.is_fullscreen = False
        self._windowed_geometry: Optional[str] = None
        self._scrubbing = False
        self._tick_after: Optional[str] = None
        self._layout_mode = _LAYOUT_FIT
        self._zoom_factor = 1.0
        self._want_playing = False
        self._play_anchor_frame = 0
        self._play_anchor_perf = 0.0
        self._paint_in_progress = False

        self._ref_clip: Optional[_OpenCvClip] = None
        self._target_clip: Optional[_OpenCvClip] = None
        self._photo_left = None
        self._photo_right = None
        self._item_left = None
        self._item_right = None
        self._paint_pool = ThreadPoolExecutor(max_workers=2)
        self._paint_gen = 0
        self._pane_size_left = (2, 2)
        self._pane_size_right = (2, 2)

        try:
            self.transient(parent.winfo_toplevel())
        except Exception:
            pass

        self.configure(fg_color="#1e1e1e")
        self.minsize(720, 480)

        self._build_body()
        self._build_hud()

        self.bind("<Escape>", self._on_escape)
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<Left>", lambda e: self._prev_target())
        self.bind("<Right>", lambda e: self._next_target())
        self.bind("<space>", lambda e: self._toggle_play_synced())
        self.bind("<j>", lambda e: self._nudge_seek(-5.0))
        self.bind("<J>", lambda e: self._nudge_seek(-5.0))
        self.bind("<l>", lambda e: self._nudge_seek(5.0))
        self.bind("<L>", lambda e: self._nudge_seek(5.0))
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._place_near_parent(parent)
        try:
            self.attributes("-alpha", 0.0)
            self.withdraw()
        except Exception:
            pass
        self.after(40, self._enter_fullscreen_initial)
        self.after(60, self._create_clips)

    # ------------------------------------------------------------------ UI
    def _place_near_parent(self, parent) -> None:
        """Positions the dialog dynamically near the parent window on the screen."""
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

    def _btn(self, parent, text, cmd, width=70):
        """Creates a customized button for the HUD."""
        return ctk.CTkButton(
            parent,
            text=text,
            width=width,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color=_BTN_FG,
            hover_color=_BTN_HOVER,
            command=cmd,
        )

    def _build_hud(self) -> None:
        """Builds the bottom Heads Up Display containing playback and navigation controls."""
        self._hud = ctk.CTkFrame(self, fg_color=_HUD_BG, corner_radius=0, height=96)
        self._hud.pack(fill="x", side="bottom")
        self._hud.pack_propagate(False)

        row1 = ctk.CTkFrame(self._hud, fg_color="transparent")
        row1.pack(fill="x", padx=12, pady=(8, 2))
        row2 = ctk.CTkFrame(self._hud, fg_color="transparent")
        row2.pack(fill="x", padx=12, pady=(2, 8))

        self._play_btn = self._btn(row1, "▶", self._toggle_play_synced, 40)
        self._play_btn.pack(side="left", padx=(0, 8))

        self._sync_var = tk.BooleanVar(value=True)
        self._sync_cb = ctk.CTkCheckBox(
            row1,
            text="Sync ▶",
            variable=self._sync_var,
            command=self._on_sync_toggle,
            font=ctk.CTkFont(size=12),
            checkbox_width=18,
            checkbox_height=18,
        )
        self._sync_cb.pack(side="left", padx=(0, 10))

        self._time_label = ctk.CTkLabel(
            row1, text="00:00 / 00:00", font=ctk.CTkFont(size=12), width=110, anchor="w"
        )
        self._time_label.pack(side="left", padx=(0, 8))

        self._scrub_var = tk.DoubleVar(value=0.0)
        self._scrub = ctk.CTkSlider(
            row1,
            from_=0.0,
            to=100.0,
            variable=self._scrub_var,
            number_of_steps=1000,
            height=16,
            command=self._on_scrub,
        )
        self._scrub.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._scrub.bind("<ButtonPress-1>", self._scrub_press)
        self._scrub.bind("<ButtonRelease-1>", self._scrub_release)

        ctk.CTkLabel(
            row1, text="Silent", font=ctk.CTkFont(size=12), text_color="#aaaaaa"
        ).pack(side="left", padx=(0, 4))

        self._swap_btn = self._btn(row2, "⇄", self._swap, 36)
        self._swap_btn.pack(side="left", padx=(0, 8))

        self._layout_var = tk.StringVar(value=_LAYOUT_FIT)
        self._layout_btn = ctk.CTkSegmentedButton(
            row2,
            values=[_LAYOUT_FIT, _LAYOUT_1TO1],
            variable=self._layout_var,
            command=self._on_layout_change,
            height=_CTRL_H,
            font=ctk.CTkFont(size=12),
            width=110,
        )
        self._layout_btn.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(row2, text="Zoom", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 4)
        )
        self._zoom_var = tk.StringVar(value="1×")
        self._zoom_menu = ctk.CTkOptionMenu(
            row2,
            values=list(_ZOOM_LABELS),
            variable=self._zoom_var,
            command=self._on_zoom_change,
            width=72,
            height=_CTRL_H,
            font=ctk.CTkFont(size=12),
            fg_color=_BTN_FG,
            button_color=_BTN_FG,
            button_hover_color=_BTN_HOVER,
            dropdown_fg_color=_HUD_BG,
        )
        self._zoom_menu.pack(side="left", padx=(0, 8))

        self._fs_btn = self._btn(row2, "Window", self.toggle_fullscreen, 72)
        self._fs_btn.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            row2,
            text="Close",
            width=64,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color="#1f6aa5",
            hover_color="#144870",
            command=self._close,
        ).pack(side="right", padx=(8, 0))

        self._nav_label = ctk.CTkLabel(row2, text="", font=ctk.CTkFont(size=12))
        self._nav_label.pack(side="right", padx=(8, 0))

        self._ref_btn = ctk.CTkButton(
            row2,
            text="Set as Reference",
            width=130,
            height=_CTRL_H,
            corner_radius=_CORNER,
            fg_color="#1f6aa5",
            hover_color="#144870",
            command=self._set_as_reference,
        )
        self._ref_btn.pack(side="right", padx=(8, 2))

        self._next_btn = self._btn(row2, "Next Target ▶", self._next_target, 120)
        self._next_btn.pack(side="right", padx=2)
        self._prev_btn = self._btn(row2, "◀ Prev Target", self._prev_target, 120)
        self._prev_btn.pack(side="right", padx=2)

    def _build_body(self) -> None:
        """Constructs the split-pane area for displaying the left and right video streams."""
        self._body = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self._body.pack(fill="both", expand=True, side="top")

        self._sbs = ctk.CTkFrame(self._body, fg_color="transparent")
        self._sbs.pack(fill="both", expand=True)

        self._left_pane, self._left_meta, self._left_canvas = self._make_pane(self._sbs, "left")
        self._right_pane, self._right_meta, self._right_canvas = self._make_pane(
            self._sbs, "right"
        )
        self._left_pane.pack(side="left", fill="both", expand=True, padx=(0, 1))
        self._right_pane.pack(side="left", fill="both", expand=True, padx=(1, 0))

    def _make_pane(self, parent, side: str):
        """Helper to create a single video pane containing a metadata label and a Tk Canvas."""
        pane = ctk.CTkFrame(parent, fg_color=_BG, corner_radius=0)
        meta = ctk.CTkLabel(
            pane,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color="#aaaaaa",
            height=22,
        )
        meta.pack(fill="x", padx=8, pady=(4, 0))
        canvas = tk.Canvas(pane, bg="black", highlightthickness=0, bd=0, cursor="hand2")
        canvas.pack(fill="both", expand=True)
        canvas.bind("<Button-1>", lambda e, s=side: self._on_pane_click(s))
        canvas.bind("<Configure>", lambda e, s=side: self._on_pane_configure(s))
        return pane, meta, canvas

    # ----------------------------------------------------- clips
    def _create_clips(self) -> None:
        """Initializes the background decoders for both the Reference and Target videos."""
        if self._closing:
            return
        try:
            self._ref_clip = _OpenCvClip(self._left_path())
            self._target_clip = _OpenCvClip(self._right_path())
        except Exception:
            logging.exception("[VideoCompare] Failed to open clips")
            messagebox.showerror(
                "Compare Videos",
                "Could not open videos with OpenCV.",
                parent=self,
            )
            self._close()
            return
        if self._ref_clip.cap is None or self._target_clip.cap is None:
            messagebox.showerror(
                "Compare Videos",
                "OpenCV could not decode one or both videos.",
                parent=self,
            )
            self._close()
            return
        logging.info(
            "[VideoCompare] OpenCV backend ready fps=%.3f (async paint+frame-skip)",
            float(getattr(self._ref_clip, "fps", 0.0) or 0.0),
        )
        self._update_meta()
        self._update_nav_controls()
        self._paint_all()
        self._update_time_ui()
        self._schedule_tick()

    # ----------------------------------------------------- fullscreen
    def _enter_fullscreen_initial(self) -> None:
        """Transitions into fullscreen mode shortly after the dialog is mapped."""
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
        """Toggles the window state between fullscreen and windowed modes."""
        self.set_fullscreen(not self.is_fullscreen)
        return "break"

    def set_fullscreen(self, enabled: bool) -> None:
        """Applies or removes the Tkinter fullscreen attributes and forces repainting."""
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
        self.after(80, self._paint_all)

    def _on_escape(self, event=None):
        """Handles the Escape key press by closing the dialog."""
        self._close()
        return "break"

    def _close(self) -> None:
        """Safely shuts down the dialog, kills the decoding threads, and releases resources."""
        if self._closing:
            return
        self._closing = True
        self._want_playing = False
        if self._tick_after is not None:
            try:
                self.after_cancel(self._tick_after)
            except Exception:
                pass
            self._tick_after = None
        for clip in (self._ref_clip, self._target_clip):
            if clip is not None:
                try:
                    clip.close()
                except Exception:
                    pass
        self._ref_clip = None
        self._target_clip = None
        try:
            self._paint_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            if self.is_fullscreen:
                self.attributes("-fullscreen", False)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    # -------------------------------------------------- reference / target
    def _left_path(self) -> str:
        """Returns the file path for the Reference video."""
        return self.paths[self._ref_index]

    def _right_path(self) -> str:
        """Returns the file path for the currently selected Target video."""
        return self.paths[self._target_index]

    def _candidate_indices(self) -> list[int]:
        """Provides a list of all available paths excluding the current Reference."""
        return [i for i in range(len(self.paths)) if i != self._ref_index]

    def _update_nav_controls(self) -> None:
        """Updates the state and text of the Next/Prev target navigation buttons."""
        cands = self._candidate_indices()
        multi = len(cands) > 1
        state = "normal" if multi else "disabled"
        self._prev_btn.configure(state=state)
        self._next_btn.configure(state=state)
        self._ref_btn.configure(state="normal" if len(self.paths) > 1 else "disabled")
        n = len(self.paths)
        try:
            pos = cands.index(self._target_index) + 1
        except ValueError:
            pos = 1
        if multi:
            self._nav_label.configure(text=f"Target {pos}/{len(cands)}  ({n} videos)")
        else:
            self._nav_label.configure(text="2 videos")

    def _update_meta(self) -> None:
        """Updates the text labels above each pane with metadata info."""
        rc, tc = self._ref_clip, self._target_clip
        lp, rp = self._left_path(), self._right_path()
        self._left_meta.configure(
            text=f"Reference  ·  {_meta_line(lp, width=getattr(rc, 'width', 0), height=getattr(rc, 'height', 0))}"
        )
        self._right_meta.configure(
            text=f"Target  ·  {_meta_line(rp, width=getattr(tc, 'width', 0), height=getattr(tc, 'height', 0))}"
        )

    def _load_target(self, keep_playing: bool = False) -> None:
        """Loads a new video into the right pane and attempts to resync timeline."""
        if not self._target_clip:
            return
        was = self._want_playing and keep_playing
        self._want_playing = False
        ratio = self._ref_clip.ratio if (self._sync and self._ref_clip) else 0.0
        if not self._target_clip.reopen(self._right_path()):
            messagebox.showerror(
                "Compare Videos",
                f"Could not open:\n{self._right_path()}",
                parent=self,
            )
            return
        if self._sync:
            self._target_clip.seek_ratio(ratio)
        else:
            self._target_clip.seek_ratio(0.0)
        self._update_meta()
        self._paint_all()
        self._update_time_ui()
        if was:
            self._start_playing()

    def _load_both(self, *, pause: bool = True) -> None:
        """Re-initializes decoders for both panes from scratch."""
        was = self._want_playing and not pause
        self._want_playing = False
        if self._ref_clip:
            self._ref_clip.reopen(self._left_path())
        if self._target_clip:
            self._target_clip.reopen(self._right_path())
        if self._ref_clip:
            self._ref_clip.seek_ratio(0.0)
        if self._target_clip:
            self._target_clip.seek_ratio(0.0)
        self._update_meta()
        self._paint_all()
        self._update_time_ui()
        if was:
            self._start_playing()

    def _swap(self) -> None:
        """Swaps the Reference and Target video streams with each other."""
        ratio = self._ref_clip.ratio if self._ref_clip else 0.0
        was = self._want_playing
        self._want_playing = False
        self._ref_index, self._target_index = self._target_index, self._ref_index
        self._update_nav_controls()
        self._load_both(pause=not was)
        if self._ref_clip:
            self._ref_clip.seek_ratio(ratio)
        if self._target_clip:
            self._target_clip.seek_ratio(ratio)
        self._paint_all()
        self._update_time_ui()
        if was:
            self._start_playing()

    def _prev_target(self) -> None:
        """Cycles to the previous available Target video in the selection list."""
        cands = self._candidate_indices()
        if len(cands) < 2:
            return
        try:
            pos = cands.index(self._target_index)
        except ValueError:
            pos = 0
        self._target_index = cands[(pos - 1) % len(cands)]
        self._update_nav_controls()
        self._load_target(keep_playing=False)

    def _next_target(self) -> None:
        """Cycles to the next available Target video in the selection list."""
        cands = self._candidate_indices()
        if len(cands) < 2:
            return
        try:
            pos = cands.index(self._target_index)
        except ValueError:
            pos = -1
        self._target_index = cands[(pos + 1) % len(cands)]
        self._update_nav_controls()
        self._load_target(keep_playing=False)

    def _set_as_reference(self) -> None:
        """Promotes the current Target video to become the Reference video."""
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
        self._load_both(pause=True)

    # ------------------------------------------------------- layout / zoom / paint
    def _on_layout_change(self, value: str) -> None:
        """Handles switching between Fit and 1:1 view modes."""
        self._layout_mode = value if value in (_LAYOUT_FIT, _LAYOUT_1TO1) else _LAYOUT_FIT
        self._paint_all(fast=False)

    def _on_zoom_change(self, label: str) -> None:
        """Handles selection from the zoom dropdown menu."""
        self._zoom_factor = float(_ZOOM_FACTORS.get(label, 1.0))
        self._paint_all(fast=False)

    def _on_pane_configure(self, side: str) -> None:
        """Records pane sizing changes to determine optimal frame scales."""
        if self._closing:
            return
        canvas = self._left_canvas if side == "left" else self._right_canvas
        try:
            size = (max(1, int(canvas.winfo_width())), max(1, int(canvas.winfo_height())))
        except Exception:
            return
        if side == "left":
            self._pane_size_left = size
        else:
            self._pane_size_right = size
        self._paint_all(fast=False)

    def _show_pil_on_canvas(
        self, pil: Optional[PILImage.Image], canvas: tk.Canvas, which: str
    ) -> None:
        """Pushes a decoded PIL image onto the Tkinter canvas safely."""
        if pil is None:
            return
        try:
            cw = max(1, int(canvas.winfo_width()))
            ch = max(1, int(canvas.winfo_height()))
        except Exception:
            return
        photo = ImageTk.PhotoImage(pil)
        if which == "left":
            self._photo_left = photo
            item = self._item_left
        else:
            self._photo_right = photo
            item = self._item_right
        if item is None:
            item = canvas.create_image(
                cw // 2, ch // 2, image=photo, anchor="center", tags="frame"
            )
            if which == "left":
                self._item_left = item
            else:
                self._item_right = item
        else:
            try:
                canvas.coords(item, cw // 2, ch // 2)
                canvas.itemconfigure(item, image=photo)
            except Exception:
                canvas.delete("frame")
                item = canvas.create_image(
                    cw // 2, ch // 2, image=photo, anchor="center", tags="frame"
                )
                if which == "left":
                    self._item_left = item
                else:
                    self._item_right = item

    def _paint_side(
        self,
        clip: Optional[_OpenCvClip],
        canvas: tk.Canvas,
        which: str,
        *,
        fast: bool = False,
    ) -> None:
        """Processes and draws a single side (Left/Right) directly on the main thread."""
        if clip is None or clip.frame_bgr is None:
            return
        try:
            cw = max(1, int(canvas.winfo_width()))
            ch = max(1, int(canvas.winfo_height()))
        except Exception:
            return
        if cw < 4 or ch < 4:
            return
        pil = _paint_frame(
            clip.frame_bgr,
            cw,
            ch,
            self._zoom_factor,
            self._layout_mode,
            fast=fast,
        )
        self._show_pil_on_canvas(pil, canvas, which)

    def _paint_all(self, *, fast: bool = False) -> None:
        """
        Paints frames to both canvases. Uses asynchronous rendering and 
        frame skipping for 'fast' (scrubbing/playback) updates to prevent GUI freezing.
        """
        if self._closing:
            return

        # FRAME SKIP: Drop incoming requests if the previous frame is still rendering
        if fast and getattr(self, "_paint_in_progress", False):
            return

        if not fast:
            self._paint_side(self._ref_clip, self._left_canvas, "left", fast=False)
            self._paint_side(self._target_clip, self._right_canvas, "right", fast=False)
            return

        # Prepare parallel CPU resize jobs for both panes
        jobs = []
        sides = (
            ("left", self._ref_clip, self._left_canvas, self._pane_size_left),
            ("right", self._target_clip, self._right_canvas, self._pane_size_right),
        )
        
        for which, clip, canvas, cached in sides:
            if clip is None or clip.frame_bgr is None:
                continue
            try:
                cw, ch = cached
                if cw < 4 or ch < 4:
                    cw = max(1, int(canvas.winfo_width()))
                    ch = max(1, int(canvas.winfo_height()))
            except Exception:
                continue
                
            if cw < 4 or ch < 4:
                continue
                
            # Create a copy so OpenCV can continue decoding into its buffer
            frame = clip.frame_bgr
            try:
                frame = frame.copy()
            except Exception:
                pass
                
            jobs.append(
                (which, canvas, (frame, cw, ch, self._zoom_factor, self._layout_mode, True))
            )
            
        if not jobs:
            return
            
        self._paint_gen += 1
        gen = self._paint_gen
        
        # Lock the paint loop to prevent overlapping renders
        self._paint_in_progress = True

        # Submit jobs to the existing thread pool
        futures = [
            (which, canvas, self._paint_pool.submit(_paint_frame_job, args))
            for which, canvas, args in jobs
        ]

        def _wait_and_update():
            """
            Background thread to wait for image processing.
            Crucial: This unblocks the main Tkinter event loop.
            """
            results = []
            for which, canvas, fut in futures:
                if self._closing or gen != self._paint_gen:
                    self._paint_in_progress = False
                    return
                try:
                    # Blocking call happens here on the background thread
                    pil = fut.result(timeout=0.5) 
                    results.append((which, canvas, pil))
                except Exception:
                    continue
                    
            def _apply_to_gui():
                """Applies the finished PIL images to the Canvas on the main thread."""
                self._paint_in_progress = False
                if self._closing or gen != self._paint_gen:
                    return
                for which, canvas, pil in results:
                    self._show_pil_on_canvas(pil, canvas, which)

            # Schedule the UI update safely on the main thread
            self.after(0, _apply_to_gui)

        # Fire and forget the waiting thread
        threading.Thread(target=_wait_and_update, daemon=True).start()

    # ------------------------------------------------------- playback
    def _on_sync_toggle(self) -> None:
        """Handles the Sync checkbox toggle during playback/scrubbing."""
        self._sync = bool(self._sync_var.get())
        if self._sync and self._ref_clip and self._target_clip:
            self._sync_target_to_ref_time()
            self._paint_all()
            if self._want_playing:
                self._arm_play_clock()

    def _on_pane_click(self, side: str) -> None:
        """Clicking on either video pane toggles Play/Pause."""
        if self._closing:
            return
        self._toggle_play_synced()

    def _toggle_play_synced(self, event=None):
        """Toggles the global playback state and updates the UI button."""
        if self._closing:
            return "break"
        if self._want_playing:
            self._stop_playing()
        else:
            self._start_playing()
        return "break"

    def _arm_play_clock(self) -> None:
        """Sets the baseline wall-clock time and frame index to measure playback against."""
        self._play_anchor_frame = int(self._ref_clip.frame_idx if self._ref_clip else 0)
        self._play_anchor_perf = time.perf_counter()

    def _ref_fps(self) -> float:
        """Extracts the frames per second of the Reference video."""
        fps = float(getattr(self._ref_clip, "fps", 0.0) or 0.0)
        if fps < 1.0:
            fps = 25.0
        # Guard absurd metadata.
        return max(1.0, min(120.0, fps))

    def _tick_delay_ms(self) -> int:
        """Schedules the next main loop pump corresponding to the source framerate."""
        return max(8, int(round(1000.0 / self._ref_fps())))

    def _start_playing(self) -> None:
        """Commences synchronized playback simulation."""
        if not self._ref_clip:
            return
        # Restart from beginning if at end.
        if self._ref_clip.ratio >= 0.999:
            self._seek_both_ratio(0.0)
            self._paint_all()
        self._want_playing = True
        self._arm_play_clock()
        self._refresh_play_button()

    def _stop_playing(self) -> None:
        """Halts playback and forces a high-quality frame paint."""
        self._want_playing = False
        self._refresh_play_button()
        # One HQ paint when paused.
        self._paint_all(fast=False)

    def _refresh_play_button(self) -> None:
        """Updates the text inside the Play/Pause button on the HUD."""
        try:
            self._play_btn.configure(text="❚❚" if self._want_playing else "▶")
        except Exception:
            pass

    def _sync_target_to_ref_time(self) -> None:
        """Aligns the Target clip to the exact same timeline second as Reference."""
        ref, tgt = self._ref_clip, self._target_clip
        if not ref or not tgt:
            return
        t = float(ref.time_s)
        if tgt.fps > 1e-6:
            want = int(round(t * tgt.fps))
            behind = want - tgt.frame_idx
            if behind == 0 and tgt.frame_bgr is not None:
                return
            if 0 < behind <= 8:
                while tgt.frame_idx < want:
                    if not tgt.advance_one():
                        break
                return
            tgt.seek_index(want)
            return
        if tgt.duration > 0:
            tgt.seek_time(min(t, tgt.duration))
        else:
            tgt.seek_ratio(ref.ratio)

    def _seek_both_ratio(self, ratio: float) -> None:
        """Scrub both Reference and Target clips to the given percentage."""
        ratio = max(0.0, min(1.0, float(ratio)))
        if self._ref_clip:
            self._ref_clip.seek_ratio(ratio)
        if self._sync and self._target_clip:
            self._sync_target_to_ref_time()
        elif self._target_clip and not self._sync:
            pass
        if self._want_playing:
            self._arm_play_clock()

    def _advance_ref_to_frame(self, target_idx: int) -> bool:
        """Steps Reference forward; drops frames to maintain correct wall-clock speed."""
        ref = self._ref_clip
        if not ref:
            return False
        n = max(1, ref.frame_count)
        target_idx = int(max(0, min(n - 1, target_idx)))
        behind = target_idx - ref.frame_idx
        if behind <= 0:
            return True
        # Far behind (UI stall): jump once rather than decoding hundreds of frames.
        if behind > 60:
            return ref.seek_index(target_idx)
        while ref.frame_idx < target_idx:
            if not ref.advance_one():
                return False
        return True

    def _on_scrub(self, value) -> None:
        """Event fired continuously as the slider is dragged."""
        if self._closing or not self._scrubbing:
            return
        ratio = max(0.0, min(1.0, float(value) / 100.0))
        self._seek_both_ratio(ratio)
        self._paint_all(fast=True)
        self._update_time_ui(force_ratio=ratio)

    def _scrub_press(self, _event=None):
        """Signals the start of a manual scrub gesture."""
        self._scrubbing = True

    def _scrub_release(self, _event=None):
        """Finalizes a scrub gesture, ensuring high-quality final frames are drawn."""
        try:
            self._on_scrub(self._scrub_var.get())
        finally:
            self._scrubbing = False
        self._paint_all(fast=False)
        self._update_time_ui()

    def _nudge_seek(self, delta_s: float):
        """Jumps timeline position forward/backward via keyboard shortcuts."""
        if self._closing or not self._ref_clip:
            return "break"
        dur = max(0.001, float(self._ref_clip.duration or 0.0))
        t = max(0.0, min(dur, self._ref_clip.time_s + delta_s))
        ratio = t / dur
        self._seek_both_ratio(ratio)
        self._paint_all()
        self._update_time_ui(force_ratio=ratio)
        return "break"

    def _update_time_ui(self, force_ratio: Optional[float] = None) -> None:
        """Updates the numeric clock string and moves the scrub slider thumb."""
        clip = self._ref_clip
        if not clip:
            return
        dur = float(clip.duration or 0.0)
        if force_ratio is not None:
            ratio = force_ratio
        else:
            ratio = clip.ratio
        cur = ratio * dur if dur > 0 else float(clip.time_s)
        try:
            self._time_label.configure(
                text=f"{_format_clock(cur)} / {_format_clock(dur)}"
            )
        except Exception:
            pass
        if not self._scrubbing:
            try:
                self._scrub_var.set(max(0.0, min(100.0, ratio * 100.0)))
            except Exception:
                pass

    def _schedule_tick(self) -> None:
        """Schedules the core playback loop task on the UI thread."""
        if self._closing:
            return
        self._tick()

    def _tick(self) -> None:
        """
        The central playback pump.
        Compares wall-clock time to decoder position, fetches next frames,
        requests paints, and schedules itself recursively.
        """
        if self._closing:
            return
        try:
            if self._want_playing and not self._scrubbing and self._ref_clip:
                fps = self._ref_fps()
                n = max(1, self._ref_clip.frame_count)
                elapsed = time.perf_counter() - self._play_anchor_perf
                target_idx = self._play_anchor_frame + int(elapsed * fps)
                if target_idx >= n - 1:
                    self._ref_clip.seek_index(n - 1)
                    if self._sync:
                        self._sync_target_to_ref_time()
                    self._paint_all(fast=True)
                    self._update_time_ui()
                    self._stop_playing()
                else:
                    if target_idx > self._ref_clip.frame_idx:
                        ok = self._advance_ref_to_frame(target_idx)
                        if not ok:
                            self._stop_playing()
                        else:
                            if self._sync:
                                self._sync_target_to_ref_time()
                            elif self._target_clip:
                                self._target_clip.seek_time(self._ref_clip.time_s)
                            self._paint_all(fast=True)
                            self._update_time_ui()
            self._refresh_play_button()
        except Exception:
            logging.exception("[VideoCompare] tick failed")
        try:
            self._tick_after = self.after(self._tick_delay_ms(), self._tick)
        except Exception:
            self._tick_after = None


def open_video_compare_dialog(parent, paths: list[str]) -> Optional[VideoCompareDialog]:
    """Factory: open video compare for ``paths`` (needs ≥2 existing video files)."""
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
        if not np.lower().endswith(VIDEO_FORMATS):
            continue
        seen.add(key)
        clean.append(np)
    if len(clean) < 2:
        try:
            messagebox.showinfo(
                "Compare Videos",
                "Select at least two videos to compare.",
                parent=parent,
            )
        except Exception:
            pass
        return None
    try:
        dlg = VideoCompareDialog(parent, clean)
    except ValueError:
        return None

    existing = getattr(parent, "_video_compare_dialogs", None)
    if existing is None:
        existing = []
        parent._video_compare_dialogs = existing

    def _forget(d=dlg):
        try:
            parent._video_compare_dialogs = [
                x for x in getattr(parent, "_video_compare_dialogs", []) if x is not d
            ]
        except Exception:
            pass

    existing.append(dlg)
    dlg.bind("<Destroy>", lambda e: _forget() if e.widget is dlg else None)
    return dlg