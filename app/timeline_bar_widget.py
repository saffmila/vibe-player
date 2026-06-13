"""
Timeline bar UI (scrubber, loop in/out, export) for Vibe Player.

Hosts ``TimelineBarWidget`` and export dialogs on top of strip thumbnails.
"""

import json
import logging
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
import tkinter as tk
from functools import partial
from tkinter import filedialog, messagebox

import customtkinter as ctk
import cv2
from PIL import Image, ImageDraw, ImageTk

from bookmark_manager import BookmarkManager, DEFAULT_BOOKMARK_COLOR
from file_operations import (
    create_video_thumbnail,
    get_ffmpeg_path,
    get_video_duration_mediainfo,
    probe_first_video_stream,
)
from utils import Tooltip, create_menu, parse_srt_file


# Hide subprocess console windows on Windows (matches the pattern used in file_operations.py).
_SUBPROCESS_STARTUPINFO = None
if os.name == "nt":
    _SUBPROCESS_STARTUPINFO = subprocess.STARTUPINFO()
    _SUBPROCESS_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _SUBPROCESS_STARTUPINFO.wShowWindow = subprocess.SW_HIDE


EXPORT_DIALOG_WIDTH = 360
EXPORT_DIALOG_HEIGHT = 500
EXPORT_DIALOG_MIN_WIDTH = 330
EXPORT_DIALOG_MIN_HEIGHT = 440
EXPORT_DIALOG_SCREEN_MARGIN = 24


class VideoExportDialog(ctk.CTkToplevel):
    """
    Simple dialog for selecting video export preset or custom settings.
    """
    def __init__(
        self,
        parent,
        video_path,
        convert_callback,
        loop_start=None,
        loop_end=None,
        controller=None,
        segments=None,
        active_segment_index=None,
    ):
            """
            Initializes the export dialog. 
            Sets up the UI elements, variables, and dynamically adjusts window size.
            """
            super().__init__(parent)
            self.title("Export Video")
            
            self.loop_mode = False
            self.fill_timeline_gaps = True  # Enables continuous filmstrip by default

            self._target_geometry = (EXPORT_DIALOG_WIDTH, EXPORT_DIALOG_HEIGHT)
            self._apply_compact_geometry()
            self.video_path = video_path
            self.convert_callback = convert_callback
            self.loop_start = loop_start
            self.loop_end = loop_end
            self.controller = controller
            self.segments = list(segments or [])
            self.active_segment_index = active_segment_index if isinstance(active_segment_index, int) else None

            self.resizable(True, True)

            self.presets = {
                "MP4 1600x1200 HQ": { "ext": ".mp4", "width": 1600, "height": 1200, "fps": 30 },
                "MP4 1280x720":     { "ext": ".mp4", "width": 1280, "height": 720,  "fps": 30 },
                "AVI 640x480":      { "ext": ".avi", "width": 640,  "height": 480,  "fps": 25 },
            }

            self.preset_var = ctk.StringVar(value=list(self.presets.keys())[0])
            self.ext_var = ctk.StringVar(value=".mp4")
            self.width_var = ctk.StringVar(value="1600")
            self.height_var = ctk.StringVar(value="1200")
            self.fps_var = ctk.StringVar(value="30")
            self.sound_var = ctk.BooleanVar(value=True)
            self.lossless_container_var = ctk.StringVar(value="MKV (recommended)")

            # Source duration (used when no loop is provided so we can default end-time).
            try:
                self._source_duration = float(get_video_duration_mediainfo(video_path) or 0.0)
            except Exception:
                self._source_duration = 0.0

            self.export_mode_var = ctk.StringVar(value="active")

            # --- Segment-based export selection ---
            active_seg = self._get_active_segment()
            active_seg_len = 0.0
            if active_seg:
                active_seg_len = max(0.0, float(active_seg["end"]) - float(active_seg["start"]))
            seg_count = len(self.segments)
            total_seg_len = 0.0
            for seg in self.segments:
                try:
                    s = float(seg.get("start"))
                    e = float(seg.get("end"))
                except (TypeError, ValueError):
                    continue
                if e > s:
                    total_seg_len += (e - s)
            if not active_seg and seg_count > 0:
                self.export_mode_var.set("all_separate")

            ctk.CTkLabel(self, text="Export scope", text_color="#00bfff").pack(pady=(6, 0))
            export_scope_frame = ctk.CTkFrame(self)
            export_scope_frame.pack(pady=2, padx=8, fill="x")
            active_label = (
                f"Active loop / cut ({active_seg_len:.1f}s)"
                if active_seg
                else "Active loop / cut (none selected)"
            )
            self.active_cut_radio = ctk.CTkRadioButton(
                export_scope_frame,
                text=active_label,
                variable=self.export_mode_var,
                value="active",
                height=22,
            )
            self.active_cut_radio.pack(anchor="w", padx=8, pady=(5, 1))
            self.all_cuts_separate_radio = ctk.CTkRadioButton(
                export_scope_frame,
                text=f"All loops / cuts, separate files ({seg_count})",
                variable=self.export_mode_var,
                value="all_separate",
                height=22,
            )
            self.all_cuts_separate_radio.pack(anchor="w", padx=8, pady=(1, 1))
            self.all_cuts_merged_radio = ctk.CTkRadioButton(
                export_scope_frame,
                text=f"All loops / cuts, merged ({seg_count})",
                variable=self.export_mode_var,
                value="all_merged",
                height=22,
            )
            self.all_cuts_merged_radio.pack(anchor="w", padx=8, pady=(1, 5))
            self.export_duration_var = ctk.StringVar(value="")
            if not active_seg:
                self.active_cut_radio.configure(state="disabled")
                if seg_count <= 0:
                    self.all_cuts_separate_radio.configure(state="disabled")
                    self.all_cuts_merged_radio.configure(state="disabled")
            self._active_seg_len_for_ui = active_seg_len
            self._total_seg_len_for_ui = total_seg_len
            self.export_mode_var.trace_add("write", lambda *_: self._update_export_duration_label())

            # Pack this before the tab body so it keeps its requested height when
            # the dialog is opened compactly or resized smaller.
            button_bar = ctk.CTkFrame(self, fg_color="transparent")
            button_bar.pack(side="bottom", fill="x", padx=8, pady=8)
            self.export_duration_label = ctk.CTkLabel(
                button_bar,
                textvariable=self.export_duration_var,
                text_color="#bfc7d5",
                font=("", 10),
                anchor="w",
            )
            self.export_duration_label.pack(fill="x", pady=(0, 6))
            self.close_btn = ctk.CTkButton(
                button_bar, text="Close", width=90, height=28, command=self._on_close
            )
            self.close_btn.pack(side="left")
            self.start_btn = ctk.CTkButton(
                button_bar, text="Start Export", height=28, command=self.start_export
            )
            self.start_btn.pack(side="right", fill="x", expand=True, padx=(10, 0))

            # --- Export mode tabs ---
            self.tabs = ctk.CTkTabview(self, height=235)
            self.tabs.pack(pady=(6, 2), padx=8, fill="both", expand=True)
            lossless_tab = self.tabs.add("Lossless")
            custom_tab = self.tabs.add("Custom")

            self.lossless_hint = ctk.CTkLabel(
                lossless_tab,
                text="Loop / Cut points snap to nearest keyframe (LosslessCut-style). MKV is the safest container.",
                text_color="#888888",
                font=("", 10),
                justify="left",
                anchor="w",
                wraplength=300,
            )
            self.lossless_hint.pack(fill="x", padx=8, pady=(6, 4))

            self.lossless_container_menu = ctk.CTkOptionMenu(
                lossless_tab,
                variable=self.lossless_container_var,
                values=["MKV (recommended)", "Same as source"],
                height=28,
            )
            self.lossless_container_menu.pack(fill="x", padx=8, pady=(0, 6))

            ctk.CTkLabel(custom_tab, text="Choose preset:").pack(pady=(6, 3))
            self.preset_menu = ctk.CTkOptionMenu(
                custom_tab,
                variable=self.preset_var,
                values=list(self.presets.keys()),
                command=self.apply_preset,
                height=28,
            )
            self.preset_menu.pack(pady=(0, 4))

            form_frame = ctk.CTkFrame(custom_tab)
            form_frame.pack(pady=4, padx=8, fill="x")
            self.width_entry = self._add_entry(form_frame, "Width:", self.width_var)
            self.height_entry = self._add_entry(form_frame, "Height:", self.height_var)
            self.fps_entry = self._add_entry(form_frame, "FPS:", self.fps_var)

            self.supported_formats = [".mp4", ".avi", ".mkv", ".mov", ".webm"]
            ctk.CTkLabel(custom_tab, text="Output Format:").pack(pady=(5, 2))
            self.format_menu = ctk.CTkOptionMenu(
                custom_tab,
                variable=self.ext_var,
                values=self.supported_formats,
                height=28,
            )
            self.format_menu.pack(pady=(0, 5))

            ctk.CTkCheckBox(custom_tab, text="Include audio (not supported yet)", variable=self.sound_var, state="disabled").pack(pady=(0, 5))

            self.tabs.set("Lossless")
            self._update_export_duration_label()

            # Keep close/destroy handlers only.
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.bind("<Destroy>", self._on_destroy_event, add="+")

            self.apply_preset(self.preset_var.get())
            self.lift()
            self.focus_force()
            # NOTE: no grab_set() — the dialog stays open after a successful export
            # so the user can immediately re-export with different settings.
            self.transient(self.master)
            self._apply_compact_geometry()
            self.after(100, self._apply_compact_geometry)

    def _add_entry(self, frame, label, var):
        row = ctk.CTkFrame(frame)
        row.pack(fill="x", pady=1)
        ctk.CTkLabel(row, text=label, width=80, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(row, textvariable=var, height=28)
        entry.pack(side="left", fill="x", expand=True)
        return entry

    def _apply_compact_geometry(self):
        """Keep the export dialog compact and fully visible on high-DPI monitors."""
        try:
            width, height = self._target_geometry
            self.update_idletasks()

            margin = EXPORT_DIALOG_SCREEN_MARGIN
            screen_x, screen_y, screen_w, screen_h = self._get_dialog_work_area()
            max_w = max(EXPORT_DIALOG_MIN_WIDTH, screen_w - margin * 2)
            max_h = max(EXPORT_DIALOG_MIN_HEIGHT, screen_h - margin * 2)
            width = min(width, max_w)
            height = min(height, max_h)

            min_w = min(EXPORT_DIALOG_MIN_WIDTH, width)
            min_h = min(EXPORT_DIALOG_MIN_HEIGHT, height)
            self.minsize(min_w, min_h)
            self.maxsize(max_w, max_h)

            x = screen_x + max(0, (screen_w - width) // 2)
            y = screen_y + max(0, (screen_h - height) // 2)
            x = min(max(screen_x + margin, x), screen_x + max(margin, screen_w - width - margin))
            y = min(max(screen_y + margin, y), screen_y + max(margin, screen_h - height - margin))
            self.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
            self.update_idletasks()
            self._clamp_to_work_area(screen_x, screen_y, screen_w, screen_h)
        except Exception:
            self.geometry(f"{EXPORT_DIALOG_WIDTH}x{EXPORT_DIALOG_HEIGHT}")

    def _clamp_to_work_area(self, screen_x, screen_y, screen_w, screen_h):
        try:
            margin = EXPORT_DIALOG_SCREEN_MARGIN
            actual_w = max(self.winfo_width(), self.winfo_reqwidth())
            actual_h = max(self.winfo_height(), self.winfo_reqheight())
            x = self.winfo_rootx()
            y = self.winfo_rooty()
            max_x = screen_x + max(margin, screen_w - actual_w - margin)
            max_y = screen_y + max(margin, screen_h - actual_h - margin)
            x = min(max(screen_x + margin, x), max_x)
            y = min(max(screen_y + margin, y), max_y)
            self.geometry(f"+{int(x)}+{int(y)}")
        except Exception:
            pass

    def _get_dialog_work_area(self):
        """Return the visible work area in the same coordinate space Tk geometry uses."""
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                class MONITORINFO(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD),
                    ]

                parent = self.master if self.master is not None else self
                hwnd = parent.winfo_id() if parent.winfo_exists() else self.winfo_id()
                monitor = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if monitor and ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    monitor_rect = info.rcMonitor
                    work = info.rcWork
                    monitor_w = max(1, int(monitor_rect.right - monitor_rect.left))
                    monitor_h = max(1, int(monitor_rect.bottom - monitor_rect.top))
                    tk_w = max(1, int(self.winfo_screenwidth()))
                    tk_h = max(1, int(self.winfo_screenheight()))
                    scale_x = monitor_w / tk_w
                    scale_y = monitor_h / tk_h
                    if scale_x < 1.1:
                        scale_x = 1.0
                    if scale_y < 1.1:
                        scale_y = 1.0
                    return (
                        int(round(work.left / scale_x)),
                        int(round(work.top / scale_y)),
                        int(round((work.right - work.left) / scale_x)),
                        int(round((work.bottom - work.top) / scale_y)),
                    )
            except Exception:
                pass

        return (
            self.winfo_vrootx(),
            self.winfo_vrooty(),
            self.winfo_vrootwidth(),
            self.winfo_vrootheight(),
        )

    def _add_time_row(self, frame, label, var):
        row = ctk.CTkFrame(frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(row, text=label, width=120, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(row, textvariable=var, placeholder_text="HH:MM:SS.mmm")
        entry.pack(side="left", fill="x", expand=True)
        return entry

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        """Render seconds as HH:MM:SS.mmm (always 3-digit ms for precision)."""
        try:
            total = max(float(seconds), 0.0)
        except (TypeError, ValueError):
            total = 0.0
        hours = int(total // 3600)
        minutes = int((total % 3600) // 60)
        secs = total - hours * 3600 - minutes * 60
        return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"

    @staticmethod
    def _parse_time_str(value: str) -> float:
        """
        Accept either a plain number of seconds ('90.5') or HH:MM:SS / MM:SS / SS notation
        with optional fractional seconds. Returns seconds as float.
        Raises ValueError on bad input.
        """
        if value is None:
            raise ValueError("empty time")
        text = value.strip()
        if not text:
            raise ValueError("empty time")
        if ":" not in text:
            return float(text)
        parts = text.split(":")
        if len(parts) > 3:
            raise ValueError(f"invalid time: {value}")
        parts = [p.strip() for p in parts]
        seconds = float(parts[-1])
        minutes = int(parts[-2]) if len(parts) >= 2 else 0
        hours = int(parts[-3]) if len(parts) == 3 else 0
        if seconds < 0 or minutes < 0 or hours < 0:
            raise ValueError(f"negative time: {value}")
        return hours * 3600 + minutes * 60 + seconds

    def _get_active_segment(self):
        idx = self.active_segment_index
        if idx is None or not (0 <= idx < len(self.segments)):
            return None
        seg = self.segments[idx]
        if not isinstance(seg, dict):
            return None
        s = seg.get("start")
        e = seg.get("end")
        if s is None or e is None:
            return None
        try:
            s = float(s)
            e = float(e)
        except (TypeError, ValueError):
            return None
        if e <= s:
            return None
        return {"start": s, "end": e}

    def _update_export_duration_label(self):
        mode = self.export_mode_var.get()
        if mode == "active":
            duration = float(getattr(self, "_active_seg_len_for_ui", 0.0))
            self.export_duration_var.set(f"Duration: {duration:.1f}s (active cut)")
        else:
            duration = float(getattr(self, "_total_seg_len_for_ui", 0.0))
            self.export_duration_var.set(f"Duration: {duration:.1f}s (sum of all cuts)")

    def _on_close(self):
        self.destroy()

    def _on_destroy_event(self, event):
        # No periodic sync timers are used in segment mode.
        return

    def apply_preset(self, preset_name):
        preset = self.presets[preset_name]
        self.ext_var.set(preset["ext"])
        self.width_var.set(str(preset["width"]))
        self.height_var.set(str(preset["height"]))
        self.fps_var.set(str(preset["fps"]))

    def start_export(self):
        try:
            active_seg = self._get_active_segment()
            export_mode = self.export_mode_var.get()
            if export_mode == "active" and active_seg is None:
                messagebox.showerror("No active cut", "No active cut is selected for export.")
                return
            if export_mode in ("all_separate", "all_merged") and not self.segments:
                messagebox.showerror("No cuts", "There are no cuts to export.")
                return
            export_start = active_seg["start"] if (export_mode == "active" and active_seg) else None
            export_end = active_seg["end"] if (export_mode == "active" and active_seg) else None
            serializable_segments = []
            for seg in self.segments:
                s = seg.get("start")
                e = seg.get("end")
                if s is None or e is None:
                    continue
                try:
                    s = float(s)
                    e = float(e)
                except (TypeError, ValueError):
                    continue
                if e > s:
                    serializable_segments.append({"start": s, "end": e})

            selected_tab = self.tabs.get() if hasattr(self, "tabs") else "Lossless"
            is_lossless = selected_tab == "Lossless"

            if is_lossless:
                # Lossless mode: stream copy + keyframe cut. Container choice drives compatibility.
                source_ext = (os.path.splitext(self.video_path)[1] or ".mp4").lower()
                container_choice = self.lossless_container_var.get()
                out_ext = ".mkv" if container_choice.startswith("MKV") else source_ext
                settings = {
                    "mode": "original",
                    "ext": out_ext,
                    "start_time": export_start,
                    "end_time": export_end,
                    "export_mode": export_mode,
                    "segments": serializable_segments,
                }
            else:
                settings = {
                    "mode": "custom",
                    "ext": self.ext_var.get(),
                    "width": int(self.width_var.get()),
                    "height": int(self.height_var.get()),
                    "fps": float(self.fps_var.get()),
                    "start_time": export_start,
                    "end_time": export_end,
                    "export_mode": export_mode,
                    "segments": serializable_segments,
                }
            # Keep the dialog open after kicking off the export so the user can
            # tweak settings and re-export without re-opening the menu.
            self.convert_callback(self.video_path, settings)
        except Exception as e:
            messagebox.showerror("Invalid input", str(e))



class TimelineBarWidget(ctk.CTkFrame):
    def __init__(self, parent,  controller,video_path, timeline_manager, on_seek=None):
        super().__init__(parent)

        # ---- THREADING SETUP ----
        self.thumb_queue = queue.Queue()
        self.worker_thread = None
        
        self.video_path = video_path
        self.markers = []
        self.marker_types_visible = {"thumbnail": True, "subtitle": True, "tag": True, "bookmark": True}
        # Staggered bookmark labels: cycle through N horizontal rows so titles do not overlap.
        self.bookmark_label_rows = 3
        self.bookmark_row_height = 24
        # Vertical timeline layout (see _compute_timeline_y_layout).
        self.timeline_canvas_top_pad = 24
        # Loop/Cut strip height (see ``y_loop_top`` / ``y_loop_bottom`` vs ``thumb_y_top``).
        self.loop_cut_lane_height = 15
        # Space between bookmark tab tips and where the ruler stack begins (layout only).
        self.bookmark_to_axis_gap = 10
        # Ruler: ticks grow upward from ``thumb_y_top`` (base_y); major/minor heights in px.
        self.time_axis_major_tick_height = 30
        self.time_axis_minor_tick_height = 22
        # Time labels: anchor ``s`` at ``thumb_y_top -`` this offset.
        self.time_axis_label_offset_above_thumb_top = 32
        # Default bookmark color; custom colors override it per bookmark.
        self.bookmark_colors = [DEFAULT_BOOKMARK_COLOR]
        # Extra scrollable vertical space lets users position the timeline even
        # when the actual drawn content fits inside the current panel height.
        self.timeline_vertical_scroll_slack_ratio = 0.40
        self.zoom_factor = 1.0
        self.min_zoom = 0.2
        self.max_zoom = 5.0
        self.pan_offset = 0.0
        self._pan_start_x = None
        self._pan_start_offset = 0.0
        self._timeline_vscrollbar_visible = False
        self.BackroundColor = "#2b2b2b"
        self.thumb_TextColor = "#dddddd"
        self.timeline_manager = timeline_manager
        # Načteme aktuální velikost z manažeru
        self.THUMB_W, self.THUMB_H = self.timeline_manager.thumbnail_size
        # Přidáme proměnnou pro ukládání volby velikosti v menu
        initial_size_str = f"{self.THUMB_W}x{self.THUMB_H}"
        self.thumb_size_var = tk.StringVar(value=initial_size_str)
        self.parent = parent
        self.controller = controller
        self.num_thumbs = 5
        self.on_seek = on_seek
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)
        self.grid_columnconfigure(0, weight=1)
        self.loop_mode = False
        self.loop_drag = None
        self.segments = []
        self.active_segment_index = None

        # Playback / snapping state (must exist before create_widgets — toolbar reads these).
        self.current_time = 0
        self.snap_types = ["none", "tick", "thumb", "cut", "bookmark"]
        self.snap_type = "none"
        self.magnet_mode = False
        
        self.thumb_images = [] # This will now store PhotoImage objects.
        # self.THUMB_W, self.THUMB_H = 190, 130

        # Must exist before create_widgets — load_thumbnails() ends with redraw_timeline().
        self.marker_canvas_ids = {}

        self.create_widgets()
        self.toggle_all_label = tk.StringVar(value="Show No Markers")
        self.start_periodic_update()
        self._process_thumb_queue() # Start the queue checker loop

    def _get_active_segment(self):
        idx = getattr(self, "active_segment_index", None)
        if idx is None:
            return None
        if not (0 <= idx < len(self.segments)):
            self.active_segment_index = None
            return None
        seg = self.segments[idx]
        if not isinstance(seg, dict):
            return None
        return seg

    def _set_active_segment_bounds(self, start, end):
        duration = self._get_current_duration() or 0.0
        min_len = 0.1
        if duration > 0:
            start = max(0.0, min(float(start), duration))
            end = max(0.0, min(float(end), duration))
        else:
            start = max(0.0, float(start))
            end = max(0.0, float(end))
        if end < start:
            start, end = end, start
        if end - start < min_len:
            end = start + min_len
            if duration > 0 and end > duration:
                end = duration
                start = max(0.0, end - min_len)
        seg = self._get_active_segment()
        if seg is not None:
            old_start = seg.get("start")
            old_end = seg.get("end")
            seg["start"] = start
            seg["end"] = end
            logging.info(
                "[CUT_DEBUG] set_active_bounds idx=%s old=(%.3f, %.3f) new=(%.3f, %.3f)",
                self.active_segment_index,
                float(old_start) if old_start is not None else -1.0,
                float(old_end) if old_end is not None else -1.0,
                float(start),
                float(end),
            )

    def _log_segments_state(self, reason):
        try:
            compact = []
            for i, seg in enumerate(self.segments):
                s = seg.get("start")
                e = seg.get("end")
                if s is None or e is None:
                    compact.append(f"{i}:(None,None)")
                else:
                    compact.append(f"{i}:({float(s):.3f},{float(e):.3f})")
            logging.info(
                "[CUT_DEBUG] %s | active=%s | count=%s | %s",
                reason,
                self.active_segment_index,
                len(self.segments),
                " ".join(compact) if compact else "<empty>",
            )
        except Exception as e:
            logging.info(f"[CUT_DEBUG] {reason} | failed to serialize segments: {e}")

    @property
    def loop_start(self):
        seg = self._get_active_segment()
        if seg is None:
            return None
        return seg.get("start")

    @loop_start.setter
    def loop_start(self, value):
        if value is None:
            seg = self._get_active_segment()
            if seg is not None:
                seg["start"] = None
            return
        seg = self._get_active_segment()
        if seg is None:
            start = max(0.0, float(value))
            self.segments.append({"start": start, "end": start})
            self.active_segment_index = len(self.segments) - 1
            return
        end_val = seg.get("end")
        if end_val is None:
            end_val = float(value)
        self._set_active_segment_bounds(float(value), float(end_val))

    @property
    def loop_end(self):
        seg = self._get_active_segment()
        if seg is None:
            return None
        return seg.get("end")

    @loop_end.setter
    def loop_end(self, value):
        if value is None:
            seg = self._get_active_segment()
            if seg is not None:
                seg["end"] = None
            return
        seg = self._get_active_segment()
        if seg is None:
            end = max(0.0, float(value))
            self.segments.append({"start": 0.0, "end": end})
            self.active_segment_index = len(self.segments) - 1
            return
        start_val = seg.get("start")
        if start_val is None:
            start_val = float(value)
        self._set_active_segment_bounds(float(start_val), float(value))

    def toggle_all_markers(self):
        self.update_bookmarks()
        any_off = any(not var.get() for var in self.marker_vars.values())
        for key, var in self.marker_vars.items():
            var.set(any_off)
            self.marker_types_visible[key] = any_off
        self.toggle_all_label.set("Show No Markers" if any_off else "Show All Markers")
        self.redraw_timeline()

    def generate_thumbnail_at_time(self, timestamp: float):
        """Generate and save a thumbnail for the current video at the given timestamp (seconds)."""
        video_path = self.video_path
        if not video_path or not os.path.isfile(video_path):
            logging.warning("generate_thumbnail_at_time: no valid video path.")
            return

        ctrl = self.controller
        labels = getattr(ctrl, "thumbnail_labels", {}) or {}
        thumbnail_info = labels.get(video_path)
        if not thumbnail_info:
            nv = os.path.normcase(os.path.normpath(video_path))
            for key, info in labels.items():
                try:
                    if os.path.normcase(os.path.normpath(key)) == nv:
                        thumbnail_info = info
                        video_path = key
                        break
                except Exception:
                    continue
        if not thumbnail_info:
            logging.warning("generate_thumbnail_at_time: %s not found in thumbnail_labels.", video_path)
            return

        row = thumbnail_info["row"]
        col = thumbnail_info["col"]
        index = thumbnail_info["index"]

        if hasattr(ctrl, "database"):
            ctrl.database.set_thumbnail_timestamp(video_path, float(timestamp))

        if getattr(ctrl, "_vg_active", False):
            ctrl.refresh_single_thumbnail(
                video_path, overwrite=True, at_time=float(timestamp)
            )
            logging.info(
                "Thumbnail generated at %s for %s (virtual grid)",
                self.format_time(timestamp),
                os.path.basename(video_path),
            )
            return

        thumbnail = create_video_thumbnail(
            video_path=video_path,
            thumbnail_size=ctrl.thumbnail_size,
            thumbnail_format=ctrl.thumbnail_format,
            capture_method=ctrl.capture_method_var.get(),
            thumbnail_time=timestamp,
            cache_enabled=ctrl.cache_enabled,
            overwrite=True,
            cache_dir=ctrl.thumbnail_cache_path,
            database=getattr(ctrl, "database", None),
        )

        if not thumbnail:
            logging.warning("generate_thumbnail_at_time: thumbnail creation failed for %s", video_path)
            return

        ctrl.create_file_thumbnail(
            file_path=video_path,
            file_name=os.path.basename(video_path),
            row=row,
            col=col,
            index=index,
            thumbnail_time=timestamp,
            overwrite=True,
            target_frame=ctrl.regular_thumbnails_frame,
        )
        logging.info(f"Thumbnail generated at {self.format_time(timestamp)} for {os.path.basename(video_path)}")

    def show_context_menu(self, event):
        """Displays the right-click context menu on the timeline."""
        cx, cy = self._canvas_pointer_xy(event)
        clicked_time = self.get_time_at_x(cx)
        duration = self._get_current_duration()
        active_player = getattr(self.controller, "current_video_window", None)
        seg_hit = self._get_segment_hover_at(cx, cy)
        marker_under_cursor = None
        nearby_items = self.canvas.find_overlapping(cx - 2, cy - 2, cx + 2, cy + 2)
        for item_id in reversed(nearby_items):
            marker = getattr(self, "marker_canvas_ids", {}).get(item_id)
            if marker and marker.get("type") == "bookmark":
                marker_under_cursor = marker
                break

        menu = tk.Menu(
            self,
            tearoff=0,
            bg="#2b2b2b",
            fg="white",
            activebackground="#444",
            selectcolor="#d0d0d0",
        )
        if seg_hit is not None:
            seg_idx = int(seg_hit["index"])
            if 0 <= seg_idx < len(self.segments):
                self.active_segment_index = seg_idx

            def _delete_segment(idx=seg_idx):
                if not (0 <= idx < len(self.segments)):
                    return
                logging.info("[CUT_DEBUG] delete requested idx=%s", idx)
                del self.segments[idx]
                if not self.segments:
                    self.active_segment_index = None
                elif self.active_segment_index is None:
                    self.active_segment_index = 0
                elif self.active_segment_index == idx:
                    self.active_segment_index = min(idx, len(self.segments) - 1)
                elif self.active_segment_index > idx:
                    self.active_segment_index -= 1
                self.save_segments_for_path(self.video_path)
                self.redraw_timeline()
                self._log_segments_state(f"delete completed idx={idx}")

            menu.add_command(
                label=f"▶ Play Active Loop / Cut {seg_idx + 1}",
                command=lambda i=seg_idx: self._activate_and_preview_segment(i),
            )
            menu.add_command(label=f"🗑 Delete Loop / Cut {seg_idx + 1}", command=_delete_segment)
            menu.add_separator()

        # --- PLAYBACK ---
        menu.add_command(label=f"Play from here ({self.format_time(clicked_time)})",
                         command=lambda: self.seek_and_play(clicked_time))
        menu.add_separator()

        # --- THUMBNAIL ---
        has_thumb_info = bool(
            self.video_path and
            os.path.isfile(self.video_path) and
            getattr(self.controller, 'thumbnail_labels', {}).get(self.video_path)
        )
        menu.add_command(
            label=f"📷 Generate thumbnail at {self.format_time(clicked_time)}",
            command=lambda t=clicked_time: self.generate_thumbnail_at_time(t),
            state="normal" if has_thumb_info else "disabled",
        )
        menu.add_separator()

        # --- LOOPING ---
        if active_player:
            def cmd_set_loop_start(t=clicked_time):
                active_player.loop_active = True
                self.loop_mode = True
                if hasattr(self, "loop_button"):
                    self._apply_loop_button_style()
                
                if getattr(active_player, "loop_end", None) is None:
                    active_player.loop_end = getattr(self, "loop_end", None) or (min(t + 1.0, duration) if duration else t + 1.0)
                    
                active_player.set_loop_start_from_timeline(t)
                self.loop_start = active_player.loop_start
                self.loop_end = active_player.loop_end
                self.redraw_timeline()

            def cmd_set_loop_end(t=clicked_time):
                active_player.loop_active = True
                self.loop_mode = True
                if hasattr(self, "loop_button"):
                    self._apply_loop_button_style()
                
                if getattr(active_player, "loop_start", None) is None:
                    active_player.loop_start = getattr(self, "loop_start", None) or max(t - 1.0, 0.0)
                    
                active_player.set_loop_end_from_timeline(t)
                self.loop_start = active_player.loop_start
                self.loop_end = active_player.loop_end
                self.redraw_timeline()

                
            def cmd_activate_selection():
                # Vezme naši pasivní modrou selekci a fyzicky ji pošle do přehrávače
                active_player.loop_active = True
                self.loop_mode = True
                if hasattr(self, "loop_button"):
                    self._apply_loop_button_style()
                active_player.set_loop_start_from_timeline(self.loop_start)
                active_player.set_loop_end_from_timeline(self.loop_end)
                self.loop_start = getattr(active_player, "loop_start", self.loop_start)
                self.loop_end = getattr(active_player, "loop_end", self.loop_end)
                self.redraw_timeline()

            def cmd_toggle_loop():
                active_player.toggle_loop()
                self.loop_start = getattr(active_player, "loop_start", self.loop_start)
                self.loop_end = getattr(active_player, "loop_end", self.loop_end)
                if hasattr(self, "loop_button"):
                    self._apply_loop_button_style()
                self.redraw_timeline()

            menu.add_command(label="Set LOOP START", command=cmd_set_loop_start)
            menu.add_command(label="Set LOOP END", command=cmd_set_loop_end)
            
            # Pokud máme vybranou modrou selekci, nabídneme její zasmartování
            if getattr(self, "loop_start", None) is not None and getattr(active_player, "loop_start", None) is None:
                menu.add_command(label="🔁 Loop Current Selection", command=cmd_activate_selection)

            loop_state = "Disable" if getattr(active_player, "loop_active", False) else "Enable"
            if hasattr(active_player, "toggle_loop"):
                menu.add_command(label=f"{loop_state} LOOP", command=cmd_toggle_loop)

        menu.add_command(label="Clear All Cuts", command=self.clear_all_cuts)
        menu.add_separator()

        # --- EXPORT SELECTION ---
        loop_s = getattr(active_player, "loop_start", None) if active_player else getattr(self, "loop_start", None)
        loop_e = getattr(active_player, "loop_end", None) if active_player else getattr(self, "loop_end", None)
        
        # Pojistka pro pripad, ze prehravac ma smazany loop_start (ma hodnotu None), tak vezmeme lokální
        if loop_s is None and self.loop_start is not None: loop_s = self.loop_start
        if loop_e is None and self.loop_end is not None: loop_e = self.loop_end
        
        if loop_s is not None and loop_e is not None:
            menu.add_separator()
            menu.add_command(
                label=f"🎬 Export Current Loop / Cut ({self.format_time(loop_s)} - {self.format_time(loop_e)})",
                command=lambda s=loop_s, e=loop_e: self.open_export_dialog(self.video_path, s, e)
            )

        menu.add_separator()

        # --- BOOKMARKS ---
        bookmark_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)
        can_skip_prev = bool(bookmark_player and hasattr(bookmark_player, "skip_to_previous_bookmark"))
        can_skip_next = bool(bookmark_player and hasattr(bookmark_player, "skip_to_next_bookmark"))

        menu.add_command(
            label="Previous Bookmark (Alt+Left)",
            command=(lambda p=bookmark_player: p.skip_to_previous_bookmark()) if can_skip_prev else (lambda: None),
            state="normal" if can_skip_prev else "disabled",
        )
        menu.add_command(
            label="Next Bookmark (Alt+Right)",
            command=(lambda p=bookmark_player: p.skip_to_next_bookmark()) if can_skip_next else (lambda: None),
            state="normal" if can_skip_next else "disabled",
        )
        menu.add_command(label="Skip to Previous Cut (Ctrl+Left)", command=self.skip_to_previous_cut)
        menu.add_command(label="Skip to Next Cut (Ctrl+Right)", command=self.skip_to_next_cut)
        menu.add_separator()
        menu.add_command(label="Add Bookmark", command=lambda: self.add_bookmark_at(clicked_time))
        if marker_under_cursor:
            menu.add_command(
                label=f"Remove Bookmark: {marker_under_cursor['label']}",
                command=lambda m=marker_under_cursor: self.remove_bookmark_at(m),
            )
        has_bookmarks = any(m.get("type") == "bookmark" for m in getattr(self, "markers", []))
        if has_bookmarks:
            menu.add_command(label="Remove All Bookmarks", command=self.remove_all_bookmarks)

        can_bookmark_manager = bool(
            self.video_path
            and os.path.isfile(self.video_path)
            and hasattr(self.controller, "show_bookmark_manager")
        )
        menu.add_command(
            label="Show Bookmark Manager",
            command=lambda: self.controller.show_bookmark_manager(self.video_path),
            state="normal" if can_bookmark_manager else "disabled",
        )

        menu.add_separator()
        menu.add_command(label="Copy Timestamp", 
                         command=lambda: self.controller.clipboard_clear() or self.controller.clipboard_append(self.format_time(clicked_time)))

        menu.tk_popup(event.x_root, event.y_root)
   
   

    def add_bookmark_at(self, timestamp):
            """Adds bookmark even if the player window is closed."""
            # 🟢 Zjistíme, jestli máme přehrávač
            active_player = getattr(self.controller, "current_video_window", None) or getattr(
                self.controller, "active_player", None
            )
            default_name = f"Marker {len(self.markers) + 1}"

            def on_confirm(name):
                name = name.strip()
                if not name: return

                if active_player:
                    # SCÉNÁŘ A: Přehrávač běží (standardní cesta)
                    active_player.bookmarks.append({"name": name, "time": timestamp})
                    active_player.save_bookmarks()
                else:
                    # SCÉNÁŘ B: Přehrávač JE ZAVŘENÝ
                    # 1. Načteme stávající záložky z disku
                    current_bookmarks = self.load_bookmarks_for_path(self.video_path)
                    # 2. Přidáme novou
                    current_bookmarks.append({"name": name, "time": timestamp})
                    # 3. Uložíme je zpět (použijeme pomocnou funkci níže)
                    self.save_bookmarks_standalone(self.video_path, current_bookmarks)
                
                # V obou případech aktualizujeme Timeline
                self.update_bookmarks()
                self.redraw_timeline()
                self._refresh_bookmark_manager_if_open()
                if active_player and hasattr(active_player, "update_loop_bar_display"):
                    active_player.update_loop_bar_display()
                logging.info(f"Bookmark '{name}' added at {timestamp}s (Player active: {active_player is not None})")

            # Spustíme dialog
            if hasattr(self.controller, 'universal_dialog'):
                self.controller.universal_dialog(
                    title="Add Bookmark",
                    message=f"Name for bookmark at {self.format_time(timestamp)}:",
                    confirm_callback=on_confirm,
                    input_field=True,
                    default_input=default_name,
                    modal=False,
                )

    def save_bookmarks_standalone(self, video_path, bookmarks):
            """Saves bookmarks to the CORRECT _bookmarks.json file."""
            import json
            # OPRAVA: Musí se shodovat s load_bookmarks_for_path!
            json_path = os.path.splitext(video_path)[0] + "_bookmarks.json"
            try:
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(self._bookmarks_for_storage(bookmarks), f, indent=4, ensure_ascii=False)
                logging.info(f"[Timeline] Standalone save successful: {json_path}")
            except Exception as e:
                logging.error(f"Failed to save bookmarks standalone: {e}")

    def _bookmarks_for_storage(self, raw_bookmarks):
            """Normalize bookmark payload and drop legacy/auto display colors."""
            normalized = []
            for item in raw_bookmarks or []:
                if not isinstance(item, dict):
                    continue
                t = item.get("time") if item.get("time") is not None else item.get("timestamp")
                if t is None:
                    continue
                try:
                    timestamp = max(0.0, float(t))
                except (TypeError, ValueError):
                    continue
                name = item.get("name") if item.get("name") is not None else item.get("label", "")
                entry = {"name": str(name or ""), "time": timestamp}
                color = BookmarkManager._normalize_hex_color(item.get("color"))
                if BookmarkManager.is_custom_bookmark_color(color):
                    entry["color"] = color
                normalized.append(entry)
            return normalized

    def remove_bookmark_at(self, marker_to_remove):
            """Removes a specific bookmark even if player is closed."""
            active_player = getattr(self.controller, "current_video_window", None) or getattr(
                self.controller, "active_player", None
            )
            timestamp = marker_to_remove["timestamp"]

            if active_player:
                active_player.bookmarks, removed = self._remove_single_bookmark_from_list(
                    getattr(active_player, "bookmarks", []),
                    marker_to_remove,
                )
                active_player.save_bookmarks()
            else:
                # Práce bez přehrávače
                current_bookmarks = self.load_bookmarks_for_path(self.video_path)
                current_bookmarks, removed = self._remove_single_bookmark_from_list(
                    current_bookmarks,
                    marker_to_remove,
                )
                self.save_bookmarks_standalone(self.video_path, current_bookmarks)

            if not removed:
                logging.info("[Timeline] Bookmark remove did not find an exact match at %.3fs", timestamp)

            self.update_bookmarks()
            self.redraw_timeline()
            self._refresh_bookmark_manager_if_open()
            if active_player and hasattr(active_player, "update_loop_bar_display"):
                active_player.update_loop_bar_display()

    def _remove_single_bookmark_from_list(self, bookmarks, marker_to_remove):
            """Remove only the one bookmark represented by a timeline marker."""
            target_label = str(marker_to_remove.get("label", "")).strip()
            try:
                target_time = float(marker_to_remove["timestamp"])
            except (KeyError, TypeError, ValueError):
                return list(bookmarks or []), False

            normalized = list(bookmarks or [])
            fallback_index = None
            fallback_delta = None
            for idx, bookmark in enumerate(normalized):
                if not isinstance(bookmark, dict):
                    continue
                raw_time = bookmark.get("time") if bookmark.get("time") is not None else bookmark.get("timestamp")
                try:
                    bookmark_time = float(raw_time)
                except (TypeError, ValueError):
                    continue
                delta = abs(bookmark_time - target_time)
                if delta > 0.001:
                    continue

                bookmark_label = str(
                    bookmark.get("name") if bookmark.get("name") is not None else bookmark.get("label", "")
                ).strip()
                if bookmark_label == target_label:
                    del normalized[idx]
                    return normalized, True

                if fallback_index is None or delta < fallback_delta:
                    fallback_index = idx
                    fallback_delta = delta

            if fallback_index is not None:
                del normalized[fallback_index]
                return normalized, True

            return normalized, False

    def remove_all_bookmarks(self):
            """Removes all bookmarks for the current video."""
            if not self.video_path:
                return
            if not messagebox.askyesno("Remove all bookmarks", "Delete all bookmarks for this video?"):
                return

            active_player = getattr(self.controller, "current_video_window", None) or getattr(
                self.controller, "active_player", None
            )
            if active_player and hasattr(active_player, "bookmarks"):
                active_player.bookmarks = []
                if hasattr(active_player, "save_bookmarks"):
                    active_player.save_bookmarks()
            else:
                self.save_bookmarks_standalone(self.video_path, [])

            self.update_bookmarks()
            self.redraw_timeline()
            self._refresh_bookmark_manager_if_open()
            if active_player and hasattr(active_player, "update_loop_bar_display"):
                active_player.update_loop_bar_display()

    def _refresh_bookmark_manager_if_open(self):
            ctrl = getattr(self, "controller", None)
            if ctrl and hasattr(ctrl, "refresh_bookmark_manager_if_open"):
                try:
                    ctrl.refresh_bookmark_manager_if_open(self.video_path)
                except Exception as e:
                    logging.info("[Timeline] bookmark manager refresh failed: %s", e)

    def seek_and_play(self, timestamp):
            """Seeks to timestamp. Opens player if it's closed."""
            active_player = getattr(self.controller, "current_video_window", None)
            
            if not active_player:
                # 🟢 Přehrávač neběží -> Spustíme ho
                video_name = os.path.basename(self.video_path)
                self.controller.open_video_player(self.video_path, video_name)
                
                # Musíme chvilku počkat, než se okno zinicializuje, pak seekneme
                # 300ms by mělo stačit pro vytvoření instance přehrávače
                self.after(300, lambda: self._delayed_seek(timestamp))
            else:
                # Přehrávač běží -> Jen seekneme a hrajeme
                if self.on_seek:
                    self.on_seek(timestamp)
                if hasattr(active_player, 'play_video'):
                    active_player.play_video()

    def _delayed_seek(self, timestamp):
        """Helper for seek after player opens."""
        if self.on_seek:
            self.on_seek(timestamp)
        active_player = getattr(self.controller, "current_video_window", None)
        if active_player and hasattr(active_player, 'play_video'):
            active_player.play_video()

    def get_closest_marker(self, timestamp, threshold=2.0):
        if not hasattr(self, "markers") or not self.markers:
            return None
        bookmarks = [m for m in self.markers if m["type"] == "bookmark"]
        if not bookmarks: return None
        closest = min(bookmarks, key=lambda m: abs(m["timestamp"] - timestamp))
        if abs(closest["timestamp"] - timestamp) <= threshold:
            return closest
        return None


    def create_widgets(self):
        """
        Initializes and lays out the UI widgets for the timeline bar.
        This includes the canvas for the timeline display and control buttons.
        """
        duration = self.timeline_manager.get_video_duration(self.video_path)
        if duration is not None:
            self._cached_duration_value = duration
        else:
            self._cached_duration_value = 1  # Default duration if duration is not available.
        self._cached_duration_path = self.video_path

        # ---  INFO TOOLBAR ( GRID) ---
        # corner_radius=0 a padx=0 to nalepí úplně do stran
        # self.toolbar_frame = ctk.CTkFrame(self, height=28, fg_color="#1e3a5f", corner_radius=0)
        # self.toolbar_frame.grid(row=0, column=0, columnspan=6, sticky="new", padx=0, pady=0)
        # self.toolbar_frame.pack_propagate(False)  # Zabrání vertikálnímu roztahování rámečku!
        
        # self.info_label = ctk.CTkLabel(self.toolbar_frame, text="Loading info...", anchor="w", font=("Segoe UI", 12, "bold"))
        # self.info_label.pack(side="left", fill="x", expand=True, padx=10)

        # self.selection_label = ctk.CTkLabel(self.toolbar_frame, text="", anchor="e", font=("Segoe UI", 12, "bold"), text_color="#FFA500")
        # self.selection_label.pack(side="right", padx=10)
        # ------------------------------------
        # ------------------------------------
        
        
        # --- NOVÝ INFO TOOLBAR (Transparentní + Linka vespod) ---
        self.toolbar_frame = ctk.CTkFrame(self, height=28, fg_color="#222222", corner_radius=0)
        self.toolbar_frame.grid(row=0, column=0, columnspan=5, sticky="new", padx=0, pady=0)
        self.toolbar_frame.pack_propagate(False)  # Zabrání vertikálnímu roztahování rámečku
        
        # Tenká šedá linka na spodní hraně toolbaru (výška 1 pixel)
        self.separator_line = tk.Frame(self.toolbar_frame, bg="#444444", height=1)
        self.separator_line.pack(side="bottom", fill="x")

        # Standardní, méně výrazný font (stejný jako zbytek appky)
        toolbar_font = ("Segoe UI", 10)

        self.info_label = ctk.CTkLabel(self.toolbar_frame, text="Loading info...", anchor="w", font=toolbar_font)
        self.info_label.pack(side="left", fill="x", expand=True, padx=10, pady=(0, 2))

        self.zoom_label = ctk.CTkLabel(
            self.toolbar_frame,
            text=self._zoom_percent_text(),
            anchor="e",
            font=toolbar_font,
            text_color="#00bfff",
        )
        self.zoom_label.pack(side="right", padx=(6, 10), pady=(0, 2))

        self.selection_label = ctk.CTkLabel(self.toolbar_frame, text="", anchor="e", font=toolbar_font, text_color="#FFA500")
        self.selection_label.pack(side="right", padx=(10, 6), pady=(0, 2))

        # Timeline canvas + vertical scroll when staggered bookmarks exceed viewport height.
        self.canvas_frame = tk.Frame(self, bg="#222222")
        self.canvas_frame.grid(row=1, column=0, columnspan=5, sticky="nsew")
        self.canvas_frame.grid_columnconfigure(0, weight=1)
        self.canvas_frame.grid_rowconfigure(0, weight=1)
        self.canvas_frame.grid_columnconfigure(1, minsize=16, weight=0)

        self.canvas = tk.Canvas(
            self.canvas_frame,
            width=900,
            height=280,
            bg="#222",
            highlightthickness=0,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.configure(yscrollincrement=24)

        # Canvas must exist before Scrollbar(command=canvas.yview).
        self._timeline_vscrollbar = ctk.CTkScrollbar(
            self.canvas_frame,
            orientation="vertical",
            command=self._timeline_yview,
            width=14,
            fg_color="#1a1a1a",
            button_color="#555555",
            button_hover_color="#777777",
        )
        self._timeline_vscrollbar.grid(row=0, column=1, sticky="ns")
        self._timeline_vscrollbar_visible = True
        self.canvas.configure(yscrollcommand=self._on_timeline_yscroll)

        # Bind mouse events for interaction.
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.canvas.bind("<Button-1>", self.on_canvas_primary_click)
        self.canvas.bind("<Double-Button-1>", self.on_canvas_double_click)
        self.canvas.bind("<Shift-MouseWheel>", self.on_ctrl_mousewheel)
        self.canvas.bind("<Button-2>", self.on_pan_start)
        self.canvas.bind("<B2-Motion>", self.on_pan_drag)
        self.canvas.bind("<ButtonRelease-2>", self.on_pan_end)
        self.canvas.bind("<Shift-Button-1>", self.on_shift_click)
        self.canvas.bind("<Shift-B1-Motion>", self.on_canvas_drag)
        
        self.canvas.bind("<Button-3>", self.show_context_menu)
        self.canvas.bind("<Motion>", self.on_mouse_move)
        self.canvas.bind("<space>", self.on_space_press)
        self.canvas.bind("m", self._magnet_keyboard_toggle)
        self.canvas.bind("M", self._magnet_keyboard_toggle)
        self.canvas.bind("<Enter>", lambda e: self.canvas.focus_set())
        self.canvas.bind("<MouseWheel>", self._on_timeline_mousewheel, add="+")
        self.canvas_frame.bind("<MouseWheel>", self._on_timeline_mousewheel, add="+")
        self.canvas.bind("<Button-4>", self._on_timeline_mousewheel, add="+")
        self.canvas.bind("<Button-5>", self._on_timeline_mousewheel, add="+")
        self.canvas_frame.bind("<Button-4>", self._on_timeline_mousewheel, add="+")
        self.canvas_frame.bind("<Button-5>", self._on_timeline_mousewheel, add="+")

        # Define button styling.
        btn_style = {
            "font": ("Segoe UI", 9),
            "fg": "#dddddd",
            "bg": "#333333",
            "activebackground": "#444444",
            "activeforeground": "white",
            "relief": "flat",
            "borderwidth": 0,
            "highlightthickness": 0,
            "padx": 8,
            "pady": 3,
            "width": 14
        }

        # Shared highlight colors for toolbar toggles (Loop / Cut, Magnet).
        self._toolbar_toggle_bg_off = btn_style["bg"]
        self._toolbar_toggle_bg_on = "#1f538d"
        self._toolbar_toggle_active_off = btn_style["activebackground"]
        self._toolbar_toggle_active_on = "#2a6bb0"

        # Create and pack control buttons (row=2).
        # Loop/Cut group order: toggle | skip prev/next | add/remove segment | { } (edit active bounds only).
        self.loop_controls_frame = tk.Frame(self, bg="#333333")
        self.loop_controls_frame.grid(row=2, column=0, pady=(1, 1), padx=(4, 6), sticky="w")

        loop_btn_style = {k: v for k, v in btn_style.items() if k != "width"}
        loop_btn_style["padx"] = 8
        self.loop_button = tk.Button(
            self.loop_controls_frame, text="🔁 Loop/Cut", command=self.on_loop_button_click, **loop_btn_style
        )
        self.loop_button.pack(side="left", padx=(0, 2))
        Tooltip(self.loop_button, "Toggle Loop / Cut")
        self._apply_loop_button_style()

        icon_btn_style = dict(btn_style)
        icon_btn_style["width"] = 3
        icon_btn_style["padx"] = 5

        self._pack_loop_toolbar_separator(self.loop_controls_frame)

        self.prev_cut_button = tk.Button(self.loop_controls_frame, text="|◀", command=self.skip_to_previous_cut, **icon_btn_style)
        self.prev_cut_button.pack(side="left", padx=(1, 0))
        self.next_cut_button = tk.Button(self.loop_controls_frame, text="▶|", command=self.skip_to_next_cut, **icon_btn_style)
        self.next_cut_button.pack(side="left", padx=(1, 0))

        self._pack_loop_toolbar_separator(self.loop_controls_frame)

        self.add_segment_button = tk.Button(self.loop_controls_frame, text="+", command=self.add_segment_at_current_time, **icon_btn_style)
        self.add_segment_button.pack(side="left", padx=(1, 0))
        self.remove_segment_button = tk.Button(
            self.loop_controls_frame, text="-", command=self.delete_active_segment, **icon_btn_style
        )
        self.remove_segment_button.pack(side="left", padx=(1, 0))

        self._pack_loop_toolbar_separator(self.loop_controls_frame)

        self.start_cut_button = tk.Button(self.loop_controls_frame, text="{", command=self.on_start_cut_button_click, **icon_btn_style)
        self.start_cut_button.pack(side="left", padx=(1, 0))
        self.end_cut_button = tk.Button(self.loop_controls_frame, text="}", command=self.on_end_cut_button_click, **icon_btn_style)
        self.end_cut_button.pack(side="left", padx=(1, 0))

        self._loop_control_tooltips = [
            Tooltip(self.prev_cut_button, "Previous Cut"),
            Tooltip(self.next_cut_button, "Next Cut"),
            Tooltip(self.add_segment_button, "New Loop/Cut at playhead (1s)"),
            Tooltip(self.remove_segment_button, "Delete active Loop/Cut"),
            Tooltip(self.start_cut_button, "Set active start to current time"),
            Tooltip(self.end_cut_button, "Set active end to current time"),
        ]

        # Only the middle column grows; left/right stay content-sized and the right cluster stays east.
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(2, weight=0)

        self.right_controls_frame = tk.Frame(self, bg="#333333")
        self.right_controls_frame.grid(row=2, column=2, pady=(1, 1), padx=(2, 4), sticky="e")
        self.right_controls_frame.pack_propagate(True)

        # Do not use btn_style width=14 here — it forces a very wide Snap button and a fake gap before Magnet.
        snap_btn_style = {k: v for k, v in btn_style.items() if k != "width"}
        snap_btn_style["padx"] = 6
        self.snap_btn = tk.Button(self.right_controls_frame, text="📐 none", command=self.show_snap_menu, **snap_btn_style)
        self.snap_btn.pack(side="left", padx=(0, 2))
        self.snap_btn.bind("<Button-3>", self.show_snap_menu)
        Tooltip(self.snap_btn, "Cycle Snap Mode (none/tick/thumb/cut/bookmark)")

        magnet_btn_style = dict(btn_style)
        magnet_btn_style["width"] = 4
        magnet_btn_style["padx"] = 6
        self.magnet_btn = tk.Button(self.right_controls_frame, text="🧲", command=self.toggle_magnet, **magnet_btn_style)
        self.magnet_btn.pack(side="left", padx=(0, 2))
        Tooltip(self.magnet_btn, "Toggle Magnet (M)")
        self._apply_magnet_button_style()

        self.zoom_menu_btn = tk.Button(self.right_controls_frame, text="🔍", command=self.show_zoom_menu, **magnet_btn_style)
        self.zoom_menu_btn.pack(side="left", padx=(0, 2))
        self.zoom_menu_btn.bind("<Button-3>", self.show_zoom_menu)
        Tooltip(self.zoom_menu_btn, "Zoom options (Shift+Wheel)")

        # Separator after Snap+Magnet, before Options/Export.
        self.right_pair_separator = tk.Frame(self.right_controls_frame, bg="#666666", width=1)
        self.right_pair_separator.pack(side="left", fill="y", pady=3, padx=(4, 4))

        self.grid_rowconfigure(0, weight=0)     # Toolbar nahoře se NESMÍ natahovat
        self.grid_rowconfigure(1, weight=1)     # Canvas (timeline) uprostřed se MUSÍ natahovat
        self.grid_rowconfigure(2, weight=0)     # Tlačítka dole se NESMÍ natahovat

        options_btn_style = dict(btn_style)
        options_btn_style["width"] = 4
        options_btn_style["padx"] = 6
        self.options_btn = tk.Button(self.right_controls_frame, text="⚙", command=self.show_options_menu, **options_btn_style)
        self.options_btn.pack(side="left", padx=(0, 2))
        Tooltip(self.options_btn, "Timeline Options")

        self.export_action_separator = tk.Frame(self.right_controls_frame, bg="#444444", width=1)
        self.export_action_separator.pack(side="left", fill="y", pady=3, padx=(4, 4))

        self.convert_btn = tk.Button(
            self.right_controls_frame,
            text="⏎ Export",
            command=lambda: self.open_export_dialog(self.video_path, self.loop_start, self.loop_end),
            **btn_style,
        )
        self.convert_btn.pack(side="left", padx=(0, 0))
        
        self.load_thumbnails()  # Initial load
        self.redraw_timeline()  # Redraw the timeline
        
        # První aktualizace panelu
        self.update_info_toolbar()

   
            
            
    def _zoom_percent_text(self):
            return f"Zoom: {int(round(self.zoom_factor * 100))}%"

    def _update_zoom_label(self):
            if hasattr(self, "zoom_label"):
                self.zoom_label.configure(text=self._zoom_percent_text())

    def update_info_toolbar(self):
            """
            Updates the top info toolbar with the current video filename, format, 
            playback time, total duration, and current loop selection length.
            """
            if not hasattr(self, 'info_label') or not hasattr(self, 'selection_label'):
                return
            self._update_zoom_label()

            if not self.video_path:
                self.info_label.configure(text="No video selected")
                self.selection_label.configure(text="")
                return
            
            filename = os.path.basename(self.video_path)
            ext = os.path.splitext(filename)[1].upper().replace(".", "")
            
            # Duration fallback independent of segment selection
            dur = self._get_current_duration() or 0
            
            cur_time_str = self.format_time(self.current_time) if self.current_time else "00:00:00.000"
            dur_str = self.format_time(dur) if dur > 1 else "00:00:00.000"
            
            cur_time_short = cur_time_str.split('.')[0]
            dur_short = dur_str.split('.')[0]

            info_text = f"{filename} [{ext}]  •  {cur_time_short} / {dur_short}"
            self.info_label.configure(text=info_text)
            
            # --- NOVÁ LOGIKA PRO ZOBRAZENÍ SMYČKY (LOOP) ---
            active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)
            loop_active = getattr(active_player, "loop_active", False) if active_player else False
            
            active_seg = self._get_active_segment()
            if active_seg and active_seg.get("start") is not None and active_seg.get("end") is not None:
                seg_start = float(active_seg["start"])
                seg_end = float(active_seg["end"])
                sel_len = max(0, seg_end - seg_start)
                start_str = self.format_time(seg_start).split('.')[0]
                end_str = self.format_time(seg_end).split('.')[0]
                length_secs = int(round(sel_len))
                state_str = "ON" if loop_active else "OFF"
                color = "lime" if loop_active else "#FFA500"
                seg_idx = (self.active_segment_index or 0) + 1
                seg_count = len(self.segments)
                self.selection_label.configure(
                    text=f"Loop {state_str}  |  Segment {seg_idx}/{seg_count}  |  Range: {start_str} - {end_str}  |  Length: {length_secs}s",
                    text_color=color,
                )
            else:
                seg_count = len(self.segments)
                if seg_count:
                    self.selection_label.configure(text=f"No segment selected  |  Segments: {seg_count}", text_color="#aaaaaa")
                else:
                    self.selection_label.configure(text="No segment selected", text_color="#aaaaaa")
            
    def update_info_toolbarOld(self):
        """
        Updates the top info toolbar with the current video filename, format, 
        playback time, total duration, and current loop selection length.
        """
        if not hasattr(self, 'info_label') or not hasattr(self, 'selection_label'):
            return

        if not self.video_path:
            self.info_label.configure(text="No video selected")
            self.selection_label.configure(text="")
            return
        
        filename = os.path.basename(self.video_path)
        ext = os.path.splitext(filename)[1].upper().replace(".", "")
        
        # Format current time and total duration
        cur_time_str = self.format_time(self.current_time) if self.current_time else "00:00:00.000"
        dur_str = self.format_time(self.duration) if self.duration else "00:00:00.000"
        
        # Ořízneme milisekundy pro přehlednější zobrazení v toolbaru, pokud chceš
        cur_time_short = cur_time_str.split('.')[0]
        dur_short = dur_str.split('.')[0]

        info_text = f"{filename} [{ext}]  •  {cur_time_short} / {dur_short}"
        self.info_label.configure(text=info_text)
        
        # Calculate and display selection duration if loop markers are active
        if self.loop_start is not None and self.loop_end is not None:
            sel_len = max(0, self.loop_end - self.loop_start)
            sel_str = self.format_time(sel_len).split('.')[0] # again, hiding ms for cleanliness
            self.selection_label.configure(text=f"Selection: {sel_str}")
        else:
            self.selection_label.configure(text="")        
            
            
    def open_export_dialog(self, video_path, loop_start=None, loop_end=None):
            def run_export(path, settings):
                self.convert_video_format(path, settings)

            VideoExportDialog(
                self.master,
                video_path,
                convert_callback=run_export,
                loop_start=loop_start,
                loop_end=loop_end,
                controller=self.controller,
                segments=self.segments,
                active_segment_index=self.active_segment_index,
            )

  



    def show_options_menu(self):
        if not hasattr(self, "marker_vars"):
            self.marker_vars = {}

        menu = create_menu(self, self)

        marker_menu = create_menu(self, menu)
        marker_types = ["bookmark", "thumbnail", "subtitle", "tag"]

        for marker_type in marker_types:
            if marker_type not in self.marker_types_visible:
                self.marker_types_visible[marker_type] = True

            var = tk.BooleanVar(value=self.marker_types_visible[marker_type])
            self.marker_vars[marker_type] = var

            marker_menu.add_checkbutton(
                label=marker_type.capitalize(),
                variable=var,
                command=partial(self.toggle_marker_type, marker_type)
            )

        any_off = any(not var.get() for var in self.marker_vars.values())
        toggle_label = "Show All Markers" if any_off else "Show No Markers"
        marker_menu.add_command(label=toggle_label, command=self.toggle_all_markers)
        menu.add_cascade(label="Markers", menu=marker_menu)

        preferences_menu = create_menu(self, menu)
        thumbs_count_menu = create_menu(self, preferences_menu)

        if not hasattr(self, "num_thumbs_var"):
            self.num_thumbs_var = tk.IntVar(value=self.num_thumbs)

        for n in [5, 10, 15]:
            thumbs_count_menu.add_radiobutton(
                label=str(n), value=n, variable=self.num_thumbs_var, command=lambda nn=n: self.set_num_thumbs(nn)
            )

        preferences_menu.add_cascade(label="Thumb count", menu=thumbs_count_menu)
        
        # Add Filmstrip toggle to Preferences
        if not hasattr(self, "fill_timeline_gaps"):
            self.fill_timeline_gaps = True
            
        self.filmstrip_var = tk.BooleanVar(value=self.fill_timeline_gaps)
        preferences_menu.add_checkbutton(
            label="Continuous Filmstrip", 
            variable=self.filmstrip_var,
            command=self.toggle_filmstrip
        )
        
             # --- NOVÉ: Podmenu pro velikost náhledů ---
        thumb_size_menu = create_menu(self, preferences_menu)
        # Synchronizujeme proměnnou s aktuálním stavem
        initial_size_str = f"{self.THUMB_W}x{self.THUMB_H}"
        self.thumb_size_var.set(initial_size_str)
        
        sizes = ["320x240", "400x300", "460x320"]
        for size in sizes:
            thumb_size_menu.add_radiobutton(
                label=size,
                value=size,
                variable=self.thumb_size_var,
                command=self._on_thumb_size_change # Nová funkce, kterou voláme
            )
        
        preferences_menu.add_cascade(label="Thumb size", menu=thumb_size_menu)
        # --- KONEC NOVÉHO PODMENU --
        
        
        preferences_menu.add_command(label="Settings (TODO)")
        menu.add_cascade(label="Preferences", menu=preferences_menu)

        x = self.options_btn.winfo_rootx()
        y = self.options_btn.winfo_rooty() + self.options_btn.winfo_height()
        menu.tk_popup(x, y)


    def toggle_filmstrip(self):
        """
        Toggles the continuous filmstrip mode on and off.
        """
        self.fill_timeline_gaps = self.filmstrip_var.get()
        self.redraw_timeline()

    def _on_thumb_size_change(self):
            """
            Handler for when the user selects a new thumbnail size from the menu.
            """
            new_size_str = self.thumb_size_var.get()
            logging.info(f"User selected new thumbnail size: {new_size_str}")
            
            # 1. Řekneme manažerovi, aby používal novou velikost
            self.timeline_manager.set_thumbnail_size(new_size_str)
            
            # 2. Aktualizujeme lokální proměnné pro šířku a výšku
            try:
                width, height = map(int, new_size_str.split('x'))
                self.THUMB_W = width
                self.THUMB_H = height
            except ValueError:
                logging.error(f"Could not parse new thumb size: {new_size_str}")
                return

            # 3. Spustíme přegenerování náhledů
            # (předpokládáme, že máš logiku pro vyčištění cache, jak jsi říkal)
            if self.video_path:
                logging.info("Forcing thumbnail reload due to size change.")
                self.load_thumbnails()



    def on_pan_start(self, event):
        self._pan_start_x = self._canvas_pointer_x(event)
        self._pan_start_offset = self.pan_offset

    def on_pan_drag(self, event):
        dx = self._canvas_pointer_x(event) - self._pan_start_x
        rel_dx = dx / self.canvas.winfo_width()
        self.pan_offset = self._pan_start_offset + rel_dx / self.zoom_factor
        self.pan_offset = max(-0.5 * (self.zoom_factor - 1), min(0.5 * (self.zoom_factor - 1), self.pan_offset))
        self.redraw_timeline()

    def on_pan_end(self, event):
        self._pan_start_x = None

    def x_to_rel(self, x, x0, x1):
        scaled_rel = (x - x0) / (x1 - x0)
        center_rel = 0.5
        if self.zoom_factor != 0:
            rel = ((scaled_rel - center_rel - self.pan_offset) / self.zoom_factor) + center_rel
        else:
            rel = center_rel
        return max(0, min(1, rel))

    def time_to_x(self, timestamp):
        duration = self._get_current_duration()
        if duration <= 0:
            return 0

        x0, x1 = self.get_timeline_bounds()
        
        rel = min(timestamp, duration) / duration
        center_rel = 0.5
        scaled_rel = (rel - center_rel) * self.zoom_factor + center_rel + self.pan_offset
        
        x = x0 + scaled_rel * (x1 - x0)
        return x


    # Containers that are well known to fail or play black when remuxed lossless
    # into the same container (typically due to broken/missing PTS, weird stream layout, etc.).
    _FRAGILE_LOSSLESS_CONTAINERS = (
        ".mpg", ".mpeg", ".vob", ".m2v", ".m1v", ".ts", ".mts", ".m2ts",
    )

    def convert_video_format(self, input_path, settings):
        if not input_path or not os.path.isfile(input_path):
            messagebox.showerror("Error", "No video selected.")
            return

        mode = settings.get("mode", "custom")
        target_ext = settings["ext"]
        export_mode = settings.get("export_mode", "active")
        segments = settings.get("segments", []) or []

        # Proactive compatibility warning for known-fragile lossless targets.
        # We only ask when the user picked "Same as source" (i.e. target_ext == src_ext);
        # if they already chose MKV explicitly, this is silently skipped.
        if mode == "original":
            src_ext = (os.path.splitext(input_path)[1] or "").lower()
            if src_ext in self._FRAGILE_LOSSLESS_CONTAINERS and target_ext == src_ext:
                choice = messagebox.askyesnocancel(
                    "Container compatibility warning",
                    (
                        f"Saving a lossless cut as {src_ext} often produces a file "
                        f"that won't play (no readable video stream).\n\n"
                        f"MKV is a safer container that keeps the original quality.\n\n"
                        f"  Yes  -  Save as MKV (recommended)\n"
                        f"  No   -  Save as {src_ext} anyway\n"
                        f"  Cancel  -  Abort export"
                    ),
                )
                if choice is None:
                    logging.info("[Export][Lossless] User cancelled container-compat dialog.")
                    return
                if choice:
                    logging.info(
                        "[Export][Lossless] User accepted MKV fallback for fragile container '%s'.",
                        src_ext,
                    )
                    target_ext = ".mkv"
                    settings["ext"] = ".mkv"
                else:
                    logging.info(
                        "[Export][Lossless] User chose to keep fragile container '%s' anyway.",
                        src_ext,
                    )

        suffix = "_cut" if mode == "original" else "_export"
        base_name = os.path.splitext(os.path.basename(input_path))[0]

        save_path = filedialog.asksaveasfilename(
            defaultextension=target_ext,
            initialfile=f"{base_name}{suffix}{target_ext}",
            filetypes=[(f"{target_ext.upper()} files", f"*{target_ext}")],
        )
        if not save_path:
            return

        if export_mode == "all_separate":
            root, ext = os.path.splitext(save_path)
            if not ext:
                ext = target_ext
            segment_count = len([s for s in segments if s.get("end", 0) > s.get("start", 0)])
            if segment_count <= 0:
                messagebox.showerror("No cuts", "There are no valid cuts to export.")
                return
            save_path = [f"{root}_cut_{i + 1}{ext}" for i in range(segment_count)]

        video_name = os.path.basename(input_path)
        t = threading.Thread(
            target=self._convert_worker,
            args=(input_path, save_path, settings, video_name),
            daemon=True
        )
        t.start()

    def _convert_worker(self, input_path, save_path, settings, video_name):
        status_bar = getattr(self.controller, "status_bar", None)

        def set_status(msg):
            if status_bar:
                self.after(0, lambda m=msg: status_bar.set_action_message(m))

        export_mode = settings.get("export_mode", "active")
        segments = []
        for s in (settings.get("segments") or []):
            try:
                ss = float(s.get("start"))
                ee = float(s.get("end"))
            except (TypeError, ValueError, AttributeError):
                continue
            if ee > ss:
                segments.append({"start": ss, "end": ee})

        try:
            if settings.get("mode") == "original":
                if export_mode == "active":
                    self._convert_worker_lossless(input_path, save_path, settings, video_name, set_status, show_done=False)
                    self.after(0, lambda p=save_path: messagebox.showinfo("Done", f"Video saved to:\n{p}"))
                elif export_mode == "all_separate":
                    paths = save_path if isinstance(save_path, list) else []
                    for i, (seg, out_path) in enumerate(zip(segments, paths), start=1):
                        seg_settings = dict(settings)
                        seg_settings["start_time"] = float(seg["start"])
                        seg_settings["end_time"] = float(seg["end"])
                        set_status(f"Exporting cut {i}/{len(segments)} (lossless)...")
                        self._convert_worker_lossless(
                            input_path,
                            out_path,
                            seg_settings,
                            video_name,
                            set_status,
                            show_done=False,
                        )
                    self.after(
                        0,
                        lambda d=os.path.dirname(paths[0]) if paths else "": messagebox.showinfo(
                            "Done",
                            f"All cuts exported.\nFolder:\n{d}",
                        ),
                    )
                elif export_mode == "all_merged":
                    self._convert_worker_lossless_merged(input_path, save_path, settings, video_name, set_status, segments)
                else:
                    raise ValueError(f"Unknown export mode: {export_mode}")
            else:
                if export_mode == "active":
                    self._convert_worker_custom_segments(
                        input_path,
                        save_path,
                        settings,
                        video_name,
                        set_status,
                        [{"start": settings.get("start_time"), "end": settings.get("end_time")}],
                    )
                    self.after(0, lambda p=save_path: messagebox.showinfo("Done", f"Video saved to:\n{p}"))
                elif export_mode == "all_separate":
                    paths = save_path if isinstance(save_path, list) else []
                    for i, (seg, out_path) in enumerate(zip(segments, paths), start=1):
                        set_status(f"Exporting cut {i}/{len(segments)}...")
                        self._convert_worker_custom_segments(
                            input_path,
                            out_path,
                            settings,
                            video_name,
                            set_status,
                            [seg],
                        )
                    self.after(
                        0,
                        lambda d=os.path.dirname(paths[0]) if paths else "": messagebox.showinfo(
                            "Done",
                            f"All cuts exported.\nFolder:\n{d}",
                        ),
                    )
                elif export_mode == "all_merged":
                    self._convert_worker_custom_segments(
                        input_path,
                        save_path,
                        settings,
                        video_name,
                        set_status,
                        segments,
                    )
                    self.after(0, lambda p=save_path: messagebox.showinfo("Done", f"Merged video saved to:\n{p}"))
                else:
                    raise ValueError(f"Unknown export mode: {export_mode}")

            set_status(f"Export complete: {video_name}")
            if status_bar:
                self.after(5000, status_bar.clear_action_message)

        except Exception as e:
            logging.error(f"[Export] Error during export: {e}")
            set_status(f"Export failed: {e}")
            self.after(0, lambda err=str(e): messagebox.showerror("Export Error", err))

    def _convert_worker_custom_segments(self, input_path, save_path, settings, video_name, set_status, segments):
        cap = None
        out = None
        try:
            cap = cv2.VideoCapture(input_path)
            if not cap.isOpened():
                raise RuntimeError("Cannot open video file.")

            fourcc_map = {
                ".mp4": cv2.VideoWriter_fourcc(*"mp4v"),
                ".avi": cv2.VideoWriter_fourcc(*"XVID"),
                ".mov": cv2.VideoWriter_fourcc(*"mp4v"),
                ".mkv": cv2.VideoWriter_fourcc(*"mp4v"),
                ".webm": cv2.VideoWriter_fourcc(*"VP80"),
            }
            target_ext = (os.path.splitext(save_path)[1] or settings["ext"]).lower()
            fourcc = fourcc_map.get(target_ext, cv2.VideoWriter_fourcc(*"mp4v"))
            out = cv2.VideoWriter(save_path, fourcc, settings["fps"], (settings["width"], settings["height"]))
            if not out.isOpened():
                raise RuntimeError("Cannot open output writer.")

            fps_src = cap.get(cv2.CAP_PROP_FPS) or 25.0
            total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            full_duration = (total_frames / fps_src) if fps_src > 0 else 0
            clean_segments = []
            for seg in segments:
                s = seg.get("start")
                e = seg.get("end")
                s = 0.0 if s is None else float(s)
                e = full_duration if e is None else float(e)
                if e > s:
                    clean_segments.append({"start": s, "end": e})
            if not clean_segments:
                clean_segments = [{"start": 0.0, "end": full_duration if full_duration > 0 else 0.0}]

            total_ms = sum(max(0.0, seg["end"] - seg["start"]) for seg in clean_segments) * 1000.0
            total_ms = max(total_ms, 1.0)
            processed_ms = 0.0
            last_pct = -1

            for i, seg in enumerate(clean_segments, start=1):
                start_ms = seg["start"] * 1000.0
                end_ms = seg["end"] * 1000.0
                cap.set(cv2.CAP_PROP_POS_MSEC, start_ms)
                while True:
                    current_msec = cap.get(cv2.CAP_PROP_POS_MSEC)
                    if current_msec > end_ms:
                        break
                    ret, frame = cap.read()
                    if not ret:
                        break
                    resized = cv2.resize(frame, (settings["width"], settings["height"]))
                    out.write(resized)
                    pct = int(((processed_ms + max(0.0, current_msec - start_ms)) / total_ms) * 100)
                    pct = max(0, min(100, pct))
                    if pct != last_pct:
                        last_pct = pct
                        if len(clean_segments) > 1:
                            set_status(f"Exporting {video_name} (cut {i}/{len(clean_segments)})  {pct}%")
                        else:
                            set_status(f"Exporting video: {video_name}  {pct}%")
                processed_ms += max(0.0, end_ms - start_ms)
        finally:
            if cap is not None:
                cap.release()
            if out is not None:
                out.release()

    def _convert_worker_lossless(self, input_path, save_path, settings, video_name, set_status, show_done=True):
        """
        Lossless cut via FFmpeg stream copy (LosslessCut-style "keyframe cut").
        No re-encoding: bitrate, resolution, fps and codec are preserved.
        Cut points snap to the nearest preceding keyframe in the source.
        """
        status_bar = getattr(self.controller, "status_bar", None)
        proc = None
        stderr_thread = None
        stderr_lines: list[str] = []
        try:
            try:
                ffmpeg_bin = get_ffmpeg_path()
            except FileNotFoundError as fnf:
                self.after(0, lambda err=str(fnf): messagebox.showerror("Export Error", err))
                set_status(f"Export failed: {fnf}")
                return

            start_time = settings.get("start_time")
            end_time = settings.get("end_time")

            # Total duration of the segment we are exporting (for progress %).
            total_seconds = None
            if start_time is not None and end_time is not None and end_time > start_time:
                total_seconds = float(end_time) - float(start_time)
            else:
                try:
                    src_total = float(get_video_duration_mediainfo(input_path) or 0.0)
                except Exception:
                    src_total = 0.0
                if src_total > 0:
                    s = float(start_time) if start_time is not None else 0.0
                    e = float(end_time) if end_time is not None else src_total
                    total_seconds = max(e - s, 0.0) or None

            target_ext = (os.path.splitext(save_path)[1] or "").lower()

            vinfo = probe_first_video_stream(input_path)
            codec_name = (vinfo or {}).get("codec_name") or ""
            codec_name_lower = codec_name.lower()

            if not codec_name_lower:
                err_msg = (
                    "Lossless export needs a video stream, but none was detected in this file.\n"
                    "(ffprobe could not read a video track.)"
                )
                logging.error("[Export][Lossless] %s for %s", err_msg, input_path)
                self.after(0, lambda m=err_msg: messagebox.showerror("Export Error", m))
                set_status("Export failed: no video track")
                return

            # Input seek (-ss BEFORE -i) — fast and snaps to the nearest preceding keyframe.
            # This is what LosslessCut calls "Keyframe cut" and is the safe default for stream copy:
            # the output always starts on a real I-frame, so decoders never see mid-GOP garbage.
            #
            # +genpts on the INPUT generates PTS where the source has none — old MPEG-PS / VOB / TS
            # streams often lack PTS on non-key packets, and the Matroska muxer (and some others)
            # refuse to write packets with unknown timestamps ("Invalid argument", code -22).
            cmd = [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "warning",
                "-y",
                "-nostdin",
                "-fflags",
                "+genpts+igndts",
            ]
            if start_time is not None:
                cmd += ["-ss", f"{float(start_time):.3f}"]
            cmd += ["-i", input_path]
            if end_time is not None:
                # -t is interpreted relative to the seek point when -ss is before -i.
                duration = float(end_time) - float(start_time or 0.0)
                if duration > 0:
                    cmd += ["-t", f"{duration:.3f}"]

            # Map first video + all audio only (skip subtitles/extra video like cover art).
            cmd += [
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-ignore_unknown",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
            ]

            # Container-specific tweaks.
            if target_ext in (".mp4", ".mov", ".m4v", ".m4a"):
                # HEVC needs hvc1 (not hev1) for wide playback in DirectShow / Apple stacks.
                if codec_name_lower in ("hevc", "h265"):
                    cmd += ["-tag:v", "hvc1"]
                # moov at start: faster open + survives interrupted writes.
                cmd += ["-movflags", "+faststart"]

            if codec_name_lower == "vp9" and target_ext == ".mp4":
                logging.warning(
                    "[Export][Lossless] VP9 in MP4 has poor player support; MKV is recommended."
                )

            cmd += ["-progress", "pipe:1", save_path]

            logging.info(f"[Export][Lossless] {' '.join(cmd)}")
            set_status(f"Exporting (lossless): {video_name}  0%")

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                startupinfo=_SUBPROCESS_STARTUPINFO,
            )

            # Drain stderr in a background thread so the OS pipe buffer never fills
            # and blocks ffmpeg mid-write (which would leave the MP4 without a moov atom).
            def _drain_stderr(pipe, sink):
                try:
                    for line in pipe:
                        sink.append(line.rstrip("\r\n"))
                except Exception:
                    pass

            if proc.stderr is not None:
                stderr_thread = threading.Thread(
                    target=_drain_stderr,
                    args=(proc.stderr, stderr_lines),
                    daemon=True,
                )
                stderr_thread.start()

            last_pct = -1
            time_re = re.compile(r"^out_time_ms=(-?\d+)")
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                m = time_re.match(line)
                if m and total_seconds:
                    out_us = int(m.group(1))
                    if out_us < 0:
                        continue
                    out_seconds = out_us / 1_000_000.0
                    pct = int(out_seconds / total_seconds * 100)
                    pct = max(0, min(100, pct))
                    if pct != last_pct:
                        last_pct = pct
                        set_status(f"Exporting (lossless): {video_name}  {pct}%")
                elif line == "progress=end":
                    break

            return_code = proc.wait()
            if stderr_thread is not None:
                stderr_thread.join(timeout=5)

            if stderr_lines:
                # Always log the full stderr; it is invaluable for diagnosing broken outputs.
                logging.warning(
                    "[Export][Lossless] FFmpeg stderr (%d lines):\n%s",
                    len(stderr_lines),
                    "\n".join(stderr_lines[-50:]),
                )

            # Sanity-check the output: a successful copy should never be just a few KB.
            try:
                out_size = os.path.getsize(save_path) if os.path.isfile(save_path) else 0
            except OSError:
                out_size = 0

            if return_code != 0:
                err_msg = "\n".join(stderr_lines[-10:]) or f"FFmpeg exited with code {return_code}"
                logging.error(f"[Export][Lossless] FFmpeg failed (rc={return_code}): {err_msg}")
                set_status(f"Export failed: {video_name}")
                self.after(0, lambda msg=err_msg: messagebox.showerror("Export Error", msg))
                return

            if out_size < 64 * 1024:
                detail = "\n".join(stderr_lines[-10:]) or "(no stderr captured)"
                err_msg = (
                    f"Output file looks corrupted ({out_size} bytes).\n"
                    f"The source may be damaged or incompatible with stream copy.\n\n"
                    f"FFmpeg said:\n{detail}"
                )
                logging.error(f"[Export][Lossless] Suspicious output size: {out_size} bytes for {save_path}")
                set_status(f"Export failed: {video_name} (corrupted output)")
                self.after(0, lambda msg=err_msg: messagebox.showerror("Export Error", msg))
                return

            out_vinfo = probe_first_video_stream(save_path)
            if not out_vinfo or not out_vinfo.get("codec_name"):
                detail = "\n".join(stderr_lines[-15:]) or "(no FFmpeg stderr)"
                err_msg = (
                    "Export finished, but the output file has no readable video stream.\n"
                    "Try turning off lossless and re-encoding, or remux to MKV.\n\n"
                    f"FFmpeg stderr (last lines):\n{detail}"
                )
                logging.error("[Export][Lossless] Output has no video stream: %s", save_path)
                set_status(f"Export failed: {video_name} (no video in output)")
                self.after(0, lambda msg=err_msg: messagebox.showerror("Export Error", msg))
                return

            set_status(f"Export complete: {video_name}")
            if show_done:
                self.after(0, lambda: messagebox.showinfo("Done", f"Video saved to:\n{save_path}"))
            if status_bar:
                self.after(5000, status_bar.clear_action_message)

        except Exception as e:
            logging.error(f"[Export][Lossless] Error during export: {e}")
            set_status(f"Export failed: {e}")
            self.after(0, lambda err=str(e): messagebox.showerror("Export Error", err))
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            if stderr_thread is not None and stderr_thread.is_alive():
                stderr_thread.join(timeout=2)

    def _convert_worker_lossless_merged(self, input_path, save_path, settings, video_name, set_status, segments):
        temp_dir = tempfile.mkdtemp(prefix="vlc_player_merge_")
        temp_paths = []
        concat_path = os.path.join(temp_dir, "concat.txt")
        try:
            for i, seg in enumerate(segments, start=1):
                seg_path = os.path.join(temp_dir, f"temp_part{i}.mkv")
                seg_settings = dict(settings)
                seg_settings["start_time"] = float(seg["start"])
                seg_settings["end_time"] = float(seg["end"])
                set_status(f"Preparing merged cut {i}/{len(segments)}...")
                self._convert_worker_lossless(
                    input_path,
                    seg_path,
                    seg_settings,
                    video_name,
                    set_status,
                    show_done=False,
                )
                temp_paths.append(seg_path)

            with open(concat_path, "w", encoding="utf-8") as f:
                for p in temp_paths:
                    escaped = p.replace("\\", "/").replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            ffmpeg_bin = get_ffmpeg_path()
            cmd = [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "warning",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_path,
                "-c",
                "copy",
                save_path,
            ]
            set_status("Merging cuts into single output...")
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                startupinfo=_SUBPROCESS_STARTUPINFO,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or "FFmpeg concat failed.")
            self.after(0, lambda p=save_path: messagebox.showinfo("Done", f"Merged video saved to:\n{p}"))
        finally:
            for p in temp_paths:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            try:
                if os.path.exists(concat_path):
                    os.remove(concat_path)
            except OSError:
                pass
            try:
                if os.path.isdir(temp_dir):
                    os.rmdir(temp_dir)
            except OSError:
                pass


    def set_num_thumbs(self, num):
        self.load_thumbnails(num_thumbs=num)
        # self.redraw_timeline() # Redraw is now handled by the loader

    # --- NEW THREAD-SAFE THUMBNAIL LOADING ---
    def load_thumbnails(self, video_path=None, num_thumbs=None):
        video_changed = False
        if video_path is not None and video_path != self.video_path:
            self.save_segments_for_path(self.video_path)
            self.video_path = video_path  # nejdřív nastavíme nové video_path...
            self.clear_selection()        # ...pak clear_selection() dotáže duration správného videa
            self.load_segments_for_path(self.video_path)
            video_changed = True
        elif video_path is not None:
            self.video_path = video_path
            self.load_segments_for_path(self.video_path)

        # Grid selection (no main player) only hit this path — segments loaded above but
        # bookmarks/subtitles/DB thumbnail marker were only refreshed from open_player's
        # reload_all_markers_and_redraw; keep markers in sync whenever the timeline video changes.
        if video_path is not None and self.video_path and os.path.isfile(self.video_path):
            self.update_bookmarks()
            self.update_thumbnails()
            self.update_subtitles()
            if video_changed:
                ctrl = getattr(self, "controller", None)
                if ctrl and hasattr(ctrl, "refresh_bookmark_manager_if_open"):
                    ctrl.refresh_bookmark_manager_if_open(self.video_path)

        if num_thumbs is not None:
            self.num_thumbs = num_thumbs

        if self.worker_thread and self.worker_thread.is_alive():
            logging.info("Thumbnail generation is already in progress.")
            return

        self.thumb_images = []
        placeholder_img = Image.new("RGB", (self.THUMB_W, self.THUMB_H), (40, 40, 40))
        d = ImageDraw.Draw(placeholder_img)
        d.text((self.THUMB_W // 3, self.THUMB_H // 3), "...", fill=(150, 150, 150))
        placeholder_photo = ImageTk.PhotoImage(placeholder_img)
        
        for _ in range(self.num_thumbs):
            self.thumb_images.append((placeholder_photo, -1))
        
        self.redraw_timeline()

        self.worker_thread = threading.Thread(
            target=self._generate_thumbs_worker,
            args=(self.video_path, self.num_thumbs)
        )
        self.worker_thread.daemon = True
        self.worker_thread.start()
        
        
   

    def _generate_thumbs_worker(self, video_path, num_thumbs):
            logging.info(f"[WorkerThread] Starting thumbnail generation for {video_path}")
            thumbs_raw = self.timeline_manager.get_timeline_thumbnails(video_path, num_thumbs)
            
            for index, (thumb_path, timestamp) in enumerate(thumbs_raw):
                try:
                    if not thumb_path or not os.path.exists(thumb_path):
                        raise FileNotFoundError("Thumbnail path is missing.")
                    
                    img = Image.open(thumb_path)
                    # 🔥 TURBO TRIK 2: img.load() donutí Python dekomprimovat JPEG tady v pozadí!
                    # Zbaví hlavní vlákno zátěže a okno přestane drhnout.
                    img.load() 
                    
                    self.thumb_queue.put(('thumb', index, img, timestamp))
                except Exception as e:
                    logging.warning(f"[WorkerThread] Failed to generate thumb at index {index}. Reason: {e}")
                    self.thumb_queue.put(('error', index, None, timestamp))
            
            logging.info(f"[WorkerThread] Finished thumbnail generation.")
            self.thumb_queue.put(('done', None, None, None))



    def _process_thumb_queue(self):
            """
            Processes thumbnails from the background thread queue and updates the UI.
            Batching multiple updates into a single redraw for better performance.
            """
            needs_redraw = False
            try:
                # img.load() dekomprimuje JPEG v background threadu -> v main threadu je data už v RAM
                # ImageTk.PhotoImage() z předem načtených dat je rychlé, takže můžeme
                # zpracovat víc naráz bez znatelného záškubu.
                for _ in range(8):
                    try:
                        msg = self.thumb_queue.get_nowait()
                        # Unpack the message from the worker thread
                        msg_type, index, data, timestamp = msg 
                        
                        if msg_type == 'thumb':
                            # PhotoImage MUST be created in the main thread (here)
                            photo_image = ImageTk.PhotoImage(data)
                            if index < len(self.thumb_images):
                                # Store in our list so redraw_timeline can pick it up
                                self.thumb_images[index] = (photo_image, timestamp)
                                needs_redraw = True
                                
                        elif msg_type == 'error':
                            # Create an error placeholder if thumb generation failed
                            error_img = Image.new("RGB", (self.THUMB_W, self.THUMB_H), (64, 64, 64))
                            error_photo = ImageTk.PhotoImage(error_img)
                            if index < len(self.thumb_images):
                                self.thumb_images[index] = (error_photo, timestamp)
                                needs_redraw = True
                                
                        elif msg_type == 'done':
                            logging.info("[Timeline] Thumbnail generation worker finished.")
                            
                    except queue.Empty:
                        break # Queue is empty, exit the batch loop
            except Exception as e:
                logging.error(f"Error in _process_thumb_queue: {e}")
            
            # Překreslíme jen pokud přišel nový thumbnail
            if needs_redraw:
                self.redraw_timeline()

            # Schedule next queue check - 30ms je dostatečně rychlé a 2x méně CPU než 15ms
            self.after(30, self._process_thumb_queue)


    
    def _apply_zoom_step(self, direction):
        if direction > 0:
            self.zoom_factor = min(self.zoom_factor * 1.2, self.max_zoom)
        else:
            self.zoom_factor = max(self.zoom_factor / 1.2, self.min_zoom)
        logging.info(f"[DEBUG] Zoom changed to {self.zoom_factor:.2f}")
        self._update_zoom_label()
        self.redraw_timeline()

    def zoom_in(self):
        self._apply_zoom_step(1)

    def zoom_out(self):
        self._apply_zoom_step(-1)

    def on_ctrl_mousewheel(self, event):
        self._apply_zoom_step(1 if event.delta > 0 else -1)

    def reset_zoom(self):
        self.zoom_factor = 1.0
        self._update_zoom_label()
        self.redraw_timeline()

    def show_zoom_menu(self, event=None):
        menu = tk.Menu(
            self,
            tearoff=0,
            bg="#2b2b2b",
            fg="white",
            activebackground="#444",
            selectcolor="#d0d0d0",
        )
        menu.add_command(label="Zoom In", command=self.zoom_in)
        menu.add_command(label="Zoom Out", command=self.zoom_out)
        menu.add_separator()
        menu.add_command(label="Reset Zoom", command=self.reset_zoom)
        try:
            if event is not None:
                x, y = event.x_root, event.y_root
            else:
                x = self.zoom_menu_btn.winfo_rootx()
                y = self.zoom_menu_btn.winfo_rooty() + self.zoom_menu_btn.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def toggle_marker_type(self, marker_type):
        self.update_bookmarks()
        current = self.marker_types_visible.get(marker_type, True)
        self.marker_types_visible[marker_type] = not current
        self.redraw_timeline() 

    def _pack_loop_toolbar_separator(self, parent):
        """Thin floating separator between Loop/Cut control groups."""
        sep = tk.Frame(parent, bg="#666666", width=1)
        sep.pack(side="left", fill="y", pady=3, padx=(6, 6))

    def _get_edit_time_seconds(self):
        """Playback time for bound edits: prefer VLC player, else timeline playhead."""
        active_player = getattr(self.controller, "current_video_window", None) or getattr(
            self.controller, "active_player", None
        )
        if active_player and getattr(active_player, "player", None):
            try:
                return max(0.0, float(active_player.player.get_time()) / 1000.0)
            except Exception:
                pass
        return float(getattr(self, "current_time", 0.0) or 0.0)

    def add_segment_at_current_time(self, default_len=1.0):
        """Append a new segment at the current time (~default_len long) and make it active."""
        t = self._get_edit_time_seconds()
        duration = self._get_current_duration() or 0.0
        start = max(0.0, float(t))
        end = start + float(default_len)
        if duration > 0:
            end = min(end, duration)
            if end - start < 0.1:
                start = max(0.0, end - float(default_len))
                start = max(0.0, min(start, duration - 0.1))
                end = min(duration, start + max(0.1, float(default_len)))
        else:
            if end - start < 0.1:
                end = start + 0.1
        self.segments.append({"start": start, "end": end})
        self.active_segment_index = len(self.segments) - 1
        self._log_segments_state(f"toolbar add segment at {start:.3f}s len={end - start:.3f}s")
        self.save_segments_for_path(self.video_path)
        self._sync_active_segment_to_player()
        self.redraw_timeline()

    def delete_active_segment(self):
        """Remove only the currently active segment (toolbar -)."""
        idx = self.active_segment_index
        if idx is None or not (0 <= idx < len(self.segments)):
            return
        del self.segments[idx]
        if not self.segments:
            self.active_segment_index = None
        elif self.active_segment_index is None:
            self.active_segment_index = 0
        elif self.active_segment_index == idx:
            self.active_segment_index = min(idx, len(self.segments) - 1)
        elif self.active_segment_index > idx:
            self.active_segment_index -= 1
        self.save_segments_for_path(self.video_path)
        self._log_segments_state(f"toolbar delete active idx was {idx}")
        active_player = getattr(self.controller, "current_video_window", None) or getattr(
            self.controller, "active_player", None
        )
        if not self.segments:
            if active_player:
                active_player.loop_active = False
                active_player.loop_start = None
                active_player.loop_end = None
                if hasattr(active_player, "update_loop_bar_display"):
                    active_player.update_loop_bar_display()
            self._apply_loop_button_style()
        else:
            self._sync_active_segment_to_player()
            self._apply_loop_button_style()
        self.redraw_timeline()

    def on_loop_button_click(self):
        active_player = getattr(self.controller, "current_video_window", None)
        if active_player and hasattr(active_player, "toggle_loop"):
            active_player.toggle_loop()
            self.loop_start = getattr(active_player, "loop_start", self.loop_start)
            self.loop_end = getattr(active_player, "loop_end", self.loop_end)
            self._apply_loop_button_style()
            self.redraw_timeline()
        else:
            logging.warning("Could not toggle loop: No active player or toggle_loop method found.")

    def on_start_cut_button_click(self):
        """Move active segment start to current time only (does not create segments)."""
        seg = self._get_active_segment()
        if seg is None:
            logging.warning("No active Loop / Cut segment; use + to add one.")
            return
        t = self._get_edit_time_seconds()
        try:
            cur_end = float(seg.get("end", t))
        except (TypeError, ValueError):
            cur_end = t
        self._set_active_segment_bounds(t, cur_end)
        self.save_segments_for_path(self.video_path)
        self._sync_active_segment_to_player()
        self.redraw_timeline()

    def on_end_cut_button_click(self):
        """Move active segment end to current time only (does not create segments)."""
        seg = self._get_active_segment()
        if seg is None:
            logging.warning("No active Loop / Cut segment; use + to add one.")
            return
        t = self._get_edit_time_seconds()
        try:
            cur_start = float(seg.get("start", t))
        except (TypeError, ValueError):
            cur_start = t
        self._set_active_segment_bounds(cur_start, t)
        self.save_segments_for_path(self.video_path)
        self._sync_active_segment_to_player()
        self.redraw_timeline()

    def clear_all_cuts(self):
        self.segments = []
        self.active_segment_index = None
        active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)
        if active_player:
            active_player.loop_active = False
            active_player.loop_start = None
            active_player.loop_end = None
            if hasattr(active_player, "update_loop_bar_display"):
                active_player.update_loop_bar_display()
        self.loop_mode = False
        self._apply_loop_button_style()
        self.save_segments_for_path(self.video_path)
        self.redraw_timeline()

    def skip_to_previous_cut(self):
        if not self.segments:
            return
        bounds = sorted({
            float(t)
            for seg in self.segments
            if isinstance(seg, dict)
            for t in (seg.get("start"), seg.get("end"))
            if t is not None
        })
        if not bounds:
            return
        current = float(getattr(self, "current_time", 0.0) or 0.0)
        prev = [t for t in bounds if t < current - 0.05]
        if not prev:
            return
        target = prev[-1]
        if self.on_seek:
            self.on_seek(target)
        self.set_current_time(target)

    def skip_to_next_cut(self):
        if not self.segments:
            return
        bounds = sorted({
            float(t)
            for seg in self.segments
            if isinstance(seg, dict)
            for t in (seg.get("start"), seg.get("end"))
            if t is not None
        })
        if not bounds:
            return
        current = float(getattr(self, "current_time", 0.0) or 0.0)
        target = next((t for t in bounds if t > current + 0.05), None)
        if target is None:
            return
        if self.on_seek:
            self.on_seek(target)
        self.set_current_time(target)
                    
    def set_snap_type(self, snap_type):
        if snap_type not in self.snap_types:
            return
        self.snap_type = snap_type
        self.snap_btn.config(text=f"📐 {self.snap_type}")

    def show_snap_menu(self, event=None):
        menu = tk.Menu(
            self,
            tearoff=0,
            bg="#2b2b2b",
            fg="white",
            activebackground="#444",
            selectcolor="#d0d0d0",
        )
        snap_var = tk.StringVar(value=self.snap_type)
        for snap in self.snap_types:
            menu.add_radiobutton(
                label=snap.title(),
                value=snap,
                variable=snap_var,
                command=lambda s=snap: self.set_snap_type(s),
            )

        try:
            if event is not None:
                x, y = event.x_root, event.y_root
            else:
                x = self.snap_btn.winfo_rootx()
                y = self.snap_btn.winfo_rooty() + self.snap_btn.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def toggle_snap_type(self):
        # Kept for backward compatibility if called elsewhere.
        idx = self.snap_types.index(self.snap_type)
        idx = (idx + 1) % len(self.snap_types)
        self.set_snap_type(self.snap_types[idx])

    def _apply_loop_button_style(self):
        """Icon-only Loop / Cut: same blue highlight as Magnet when playback loop is active."""
        if not getattr(self, "loop_button", None):
            return
        if not hasattr(self, "_toolbar_toggle_bg_on"):
            return
        active_player = getattr(self.controller, "current_video_window", None) or getattr(
            self.controller, "active_player", None
        )
        loop_on = bool(getattr(active_player, "loop_active", False)) if active_player else False
        if loop_on:
            self.loop_button.config(
                bg=self._toolbar_toggle_bg_on,
                activebackground=self._toolbar_toggle_active_on,
            )
        else:
            self.loop_button.config(
                bg=self._toolbar_toggle_bg_off,
                activebackground=self._toolbar_toggle_active_off,
            )

    def _apply_magnet_button_style(self):
        """NLE-style pressed state: highlight background when magnet snapping is enabled."""
        if not getattr(self, "magnet_btn", None):
            return
        if self.magnet_mode:
            self.magnet_btn.config(
                bg=self._toolbar_toggle_bg_on,
                activebackground=self._toolbar_toggle_active_on,
            )
        else:
            self.magnet_btn.config(
                bg=self._toolbar_toggle_bg_off,
                activebackground=self._toolbar_toggle_active_off,
            )

    def _magnet_keyboard_toggle(self, _event=None):
        self.toggle_magnet()
        return "break"

    def toggle_magnet(self):
        self.magnet_mode = not self.magnet_mode
        self._apply_magnet_button_style()
                         
    def on_canvas_resize(self, event):
        logging.info(f"Canvas resized: {event.width}x{event.height}")
        self.redraw_timeline()

    def _compute_timeline_y_layout(self):
        """
        Vertical zones (top → bottom, Y increases downward):

        1) Bookmarks.
        2) Time-axis ticks + labels (ticks bottom on ``thumb_y_top`` = ``base_y``).
        3) Loop/Cut lane floating above thumbs: top ``base_y - 18``, bottom ``base_y - 4``.
        4) Thumbnails from ``base_y`` to ``y_bar_bot``.
        """
        top_pad = float(self.timeline_canvas_top_pad)
        n_rows = max(1, int(self.bookmark_label_rows))
        rh = float(self.bookmark_row_height)
        bookmark_band = (n_rows * rh) + 28.0
        marker_height = 18.0
        y_marker_top = top_pad + bookmark_band + 10.0
        y_marker_bot = y_marker_top + marker_height

        g_ba = float(self.bookmark_to_axis_gap)
        y_bar_top = y_marker_bot + g_ba

        major = float(self.time_axis_major_tick_height)
        # Major tick top = base_y - major; keep it below bookmarks with margin.
        base_y = y_marker_bot + g_ba + major + 8.0

        y_loop_top = base_y - 18.0
        y_loop_bottom = base_y - 4.0
        thumb_y_top = base_y

        padding_y = 12.0
        y_bar_bot = thumb_y_top + float(self.THUMB_H) + padding_y
        content_h = y_bar_bot + 8.0
        return {
            "y_marker_top": y_marker_top,
            "y_marker_bot": y_marker_bot,
            "y_loop_top": y_loop_top,
            "y_loop_bottom": y_loop_bottom,
            "y_bar_top": y_bar_top,
            "thumb_y_top": thumb_y_top,
            "y_bar_bot": y_bar_bot,
            "content_height": content_h,
        }

    def _update_timeline_vscrollbar_visibility(self):
        """Keep the timeline scrollbar visible; reset scroll only when content fits."""
        sb = getattr(self, "_timeline_vscrollbar", None)
        if sb is None:
            return
        try:
            if not self._timeline_vscrollbar_visible:
                sb.grid(row=0, column=1, sticky="ns")
                self._timeline_vscrollbar_visible = True
            needs_scroll = self._timeline_vscroll_needed()
            if not needs_scroll and self.canvas.yview() != (0.0, 1.0):
                self.canvas.yview_moveto(0)
        except tk.TclError:
            pass

    def _timeline_scrollregion_height(self, content_height=None):
        content_h = float(
            (getattr(self, "_timeline_content_height", 0) or 0)
            if content_height is None
            else content_height
        )
        vh = max(1, int(self.canvas.winfo_height()))
        if content_h <= 0:
            return float(vh)
        slack_h = vh * float(self.timeline_vertical_scroll_slack_ratio)
        return max(content_h + slack_h, vh + slack_h)

    def _timeline_vscroll_needed(self):
        self.canvas.update_idletasks()
        ch = self._timeline_scrollregion_height()
        bbox = self.canvas.bbox("all")
        if bbox:
            ch = max(ch, self._timeline_scrollregion_height(float(bbox[3] - bbox[1])))
        vh = max(1, int(self.canvas.winfo_height()))
        return ch > vh + 1

    def _timeline_yview(self, *args):
        self.canvas.yview(*args)
        self._update_timeline_vscrollbar_visibility()

    def _on_timeline_yscroll(self, first, last):
        sb = getattr(self, "_timeline_vscrollbar", None)
        if sb is not None:
            sb.set(first, last)
        self.after_idle(self._update_timeline_vscrollbar_visibility)

    def _on_timeline_mousewheel(self, event):
        if not self._timeline_vscroll_needed():
            return None
        if getattr(event, "num", None) == 4:
            direction = -1
        elif getattr(event, "num", None) == 5:
            direction = 1
        else:
            delta = getattr(event, "delta", 0)
            if not delta:
                return None
            direction = -1 if delta > 0 else 1
        self.canvas.yview_scroll(direction * 3, "units")
        return "break"

    def _canvas_pointer_x(self, event):
        """X in canvas coordinates (accounts for vertical scroll)."""
        return self.canvas.canvasx(event.x)

    def _canvas_pointer_xy(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)


    def on_space_press(self, event):
            """Toggles play/pause: prioritizing standalone player, then embedded preview."""
            # 1. Zkusíme najít velké samostatné okno
            standalone_player = getattr(self.controller, "current_video_window", None)
            
            if standalone_player:
                logging.info("[Timeline] Space: Toggling standalone player.")
                if hasattr(standalone_player, 'toggle_play'):
                    standalone_player.toggle_play()
                return

            # 2. Pokud velké okno není, zkusíme ovládat vložený náhled (Preview)
            # Ten je v controlleru obvykle pod 'active_player'
            preview_player = getattr(self.controller, "active_player", None)
            
            if preview_player:
                logging.info("[Timeline] Space: Toggling embedded preview.")
                if hasattr(preview_player, 'toggle_play'):
                    preview_player.toggle_play()
            else:
                logging.info("[Timeline] Space: No player found to toggle.")

    def on_shift_click(self, event):
        """
        Handles Shift + Left Mouse Button click to start a new selection.
        """
        logging.info("[DEBUG] on_shift_click triggered!")
        active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)

        self.canvas.focus_set()
        duration = self._get_current_duration()
        if duration <= 0: return

        cx = self._canvas_pointer_x(event)
        raw_time = self.get_time_at_x(cx)
        clicked_time = self.apply_snap_and_magnet(cx, raw_time, duration)

        self._selection_anchor = clicked_time
        
        # --- OPRAVA: Vymažeme loop z přehrávače, aby video neskákalo! ---
        if active_player:
            active_player.loop_active = False
            active_player.loop_start = None  # Schválně None
            active_player.loop_end = None    # Schválně None
        
        # Create a new active segment at clicked time.
        self.loop_mode = False
        self.segments.append({"start": clicked_time, "end": clicked_time})
        self.active_segment_index = len(self.segments) - 1
        self._log_segments_state(f"shift_click create at {clicked_time:.3f}s")

        self.loop_drag = "select"
        self.canvas.config(cursor="sb_h_double_arrow")
        
        if hasattr(self, "loop_button"):
            self._apply_loop_button_style()
        
        self.redraw_timeline()

    def on_canvas_primary_click(self, event):
        """
        Handles left-click on the timeline canvas.
        Prioritizes playhead drag, then loop/selection edges, then bookmarks, then seek.
        """
        self.canvas.focus_set()
        x, y = self._canvas_pointer_xy(event)
        active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)
        in_segment_strip_y = self._is_in_segment_strip_y(y)

        # 1. Segment drag/selection has priority over playhead, but only inside segment lane Y-range.
        active_seg = self._get_active_segment()
        if in_segment_strip_y and active_seg and active_seg.get("start") is not None and active_seg.get("end") is not None:
            ls = float(active_seg["start"])
            le = float(active_seg["end"])
            margin_drag = 16
            px_s = self.time_to_x(ls)
            px_e = self.time_to_x(le)

            if abs(x - px_s) < margin_drag:
                logging.info("[DEBUG] Active segment START drag.")
                self.loop_drag = "start"
                self.canvas.config(cursor="sb_h_double_arrow")
                return

            if abs(x - px_e) < margin_drag:
                logging.info("[DEBUG] Active segment END drag.")
                self.loop_drag = "end"
                self.canvas.config(cursor="sb_h_double_arrow")
                return

            if px_s < x < px_e:
                logging.info("[DEBUG] Active segment MOVE drag.")
                self.loop_drag = "move"
                x0, x1 = self.get_timeline_bounds()
                duration = self._get_current_duration() or 60
                clicked_time = self.x_to_rel(x, x0, x1) * duration
                self.drag_offset_time = clicked_time - ls
                self.canvas.config(cursor="fleur")
                return

        # If inactive segment was clicked, activate and allow immediate move-drag.
        if in_segment_strip_y:
            for i in range(len(self.segments) - 1, -1, -1):
                if i == self.active_segment_index:
                    continue
                seg = self.segments[i]
                s = seg.get("start")
                e = seg.get("end")
                if s is None or e is None:
                    continue
                x_s = self.time_to_x(float(s))
                x_e = self.time_to_x(float(e))
                if min(x_s, x_e) <= x <= max(x_s, x_e):
                    self.active_segment_index = i
                    self.loop_drag = "move_pending"
                    self._drag_pending_start_x = x
                    x0, x1 = self.get_timeline_bounds()
                    duration = self._get_current_duration() or 60
                    clicked_time = self.x_to_rel(x, x0, x1) * duration
                    self.drag_offset_time = clicked_time - float(s)
                    self.canvas.config(cursor="fleur")
                    self.redraw_timeline()
                    # Ensure player loop bar updates even on simple segment re-selection.
                    self._sync_active_segment_to_player()
                    self._log_segments_state(f"primary_click activate idx={i} x={x}")
                    return

        # 2. Fallback: playhead seek when no segment was hit.
        margin_drag_playhead = 10
        px_playhead = self.time_to_x(self.current_time)
        if abs(x - px_playhead) < margin_drag_playhead:
            logging.info("[DEBUG] Zachycen DRAG PLAYHEADU.")
            self.loop_drag = None
            self.on_timeline_click(event)
            return

        # 3. Kliknutí na marker (bookmark) - Skok na přesnou pozici
        # Hledáme objekty v těsné blízkosti kliknutí (rozsah 2px)
        cx, cy = self._canvas_pointer_xy(event)
        overlapping = self.canvas.find_overlapping(cx - 2, cy - 2, cx + 2, cy + 2)
        for item_id in overlapping:
            marker = getattr(self, "marker_canvas_ids", {}).get(item_id)
            if marker:
                ts = marker.get("timestamp")
                if ts is not None:
                    logging.info(f"[Timeline] Jumping to marker: {marker.get('label')} at {ts:.2f}s")
                    # Provedeme seek v přehrávači
                    if getattr(self, "on_seek", None):
                        self.on_seek(ts)
                    # Aktualizujeme vizuální pozici playheadu na timeline
                    self.set_current_time(ts)
                    return # Ukončíme, aby se neprovedl standardní seek mimo marker

        # 4. Standardní seek na timeline, pokud jsme neklikli na marker nebo kurzor
        self.loop_drag = None
        self.on_timeline_click(event)

    def on_canvas_double_click(self, event):
        """Double-click a segment to preview it as active loop."""
        cx, cy = self._canvas_pointer_xy(event)
        seg_hit = self._get_segment_hover_at(cx, cy)
        if seg_hit is None:
            # UX fallback: double-click outside segments seeks and starts playback.
            self.on_timeline_click(event)
            active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)
            if active_player is not None:
                if hasattr(active_player, "play_video"):
                    try:
                        active_player.play_video()
                    except Exception:
                        pass
                elif hasattr(active_player, "toggle_play"):
                    is_playing = bool(getattr(active_player, "is_playing", False))
                    if not is_playing:
                        try:
                            active_player.toggle_play()
                        except Exception:
                            pass
            return "break"

        idx = int(seg_hit["index"])
        if not (0 <= idx < len(self.segments)):
            return "break"

        self._activate_and_preview_segment(idx)
        self._log_segments_state(f"double_click preview idx={idx}")
        return "break"

    def _activate_and_preview_segment(self, idx):
        if not (0 <= idx < len(self.segments)):
            return

        self.active_segment_index = idx
        seg = self.segments[idx]
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        if end <= start:
            return

        self.loop_drag = None
        self.canvas.config(cursor="")
        active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)
        if active_player is not None:
            try:
                active_player.loop_active = True
                if hasattr(active_player, "set_loop_start_from_timeline"):
                    active_player.set_loop_start_from_timeline(start)
                else:
                    active_player.loop_start = start
                if hasattr(active_player, "set_loop_end_from_timeline"):
                    active_player.set_loop_end_from_timeline(end)
                else:
                    active_player.loop_end = end
            except Exception:
                active_player.loop_start = start
                active_player.loop_end = end
                active_player.loop_active = True

        if self.on_seek:
            self.on_seek(start)
        elif active_player and hasattr(active_player, "seek"):
            try:
                active_player.seek(start)
            except Exception:
                pass

        if active_player is not None:
            if hasattr(active_player, "play_video"):
                try:
                    active_player.play_video()
                except Exception:
                    pass
            elif hasattr(active_player, "toggle_play"):
                is_playing = bool(getattr(active_player, "is_playing", False))
                if not is_playing:
                    try:
                        active_player.toggle_play()
                    except Exception:
                        pass

        if hasattr(self, "loop_button"):
            self._apply_loop_button_style()
        self.redraw_timeline()
        self._log_segments_state(f"activate_preview idx={idx} start={start:.3f} end={end:.3f}")

    def _sync_active_segment_to_player(self):
        """Synchronize current active segment bounds into player loop visuals."""
        seg = self._get_active_segment()
        if not isinstance(seg, dict):
            return
        start = seg.get("start")
        end = seg.get("end")
        if start is None or end is None:
            return
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            return
        if end <= start:
            return
        active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)
        if active_player is None:
            return
        try:
            active_player.loop_start = start
            active_player.loop_end = end
            active_player.loop_active = True
            if hasattr(active_player, "update_loop_bar_display"):
                active_player.update_loop_bar_display()
            if hasattr(self, "loop_button"):
                self._apply_loop_button_style()
        except Exception:
            return

    

    def draw_markers(self, x0, x1, y_timeline, duration):
        self.marker_canvas_ids.clear()
        marker_spacing = {}
        for marker in self.markers:
            if not self.marker_types_visible.get(marker["type"], True):
                continue
            rel = marker["timestamp"] / duration
            x = x0 + rel * (x1 - x0)
            y_top = y_timeline - 32
            y_bot = y_timeline - 6
            
            color = marker.get("color", DEFAULT_BOOKMARK_COLOR)
            rect_id = self.canvas.create_rectangle(x-6, y_top, x+6, y_bot, fill=color, outline="", tags="marker")
            arrow_id = self.canvas.create_polygon([x-6, y_bot, x+6, y_bot, x, y_bot+9], fill=color, outline="", tags="marker")
            x_int = int(x)
            offset = marker_spacing.get(x_int, 0)
            marker_spacing[x_int] = offset + 1
            label_id = self.canvas.create_text(x, y_top-10-offset*14, text=marker["label"], fill="#222" if marker["type"]=="subtitle" else "#FFF", font=("Arial", 9), anchor="s", tags="marker")
            self.marker_canvas_ids[rect_id] = marker
            self.marker_canvas_ids[arrow_id] = marker
            self.marker_canvas_ids[label_id] = marker


    def draw_thumbnails(self, x0, x1, y_bar_top, thumb_height, num_main):
        """
        Draws thumbnails as a filmstrip. Skip placeholders (timestamp -1) to keep the UI clean while loading.
        """
        thumb_centers = []
        if not self.thumb_images:
            return thumb_centers

        # Filter out placeholders (those with timestamp -1)
        valid_thumbs = [t for t in self.thumb_images if t[1] != -1]
        
        # --- FIX: If no real thumbs are loaded yet, just show the clean background ---
        if not valid_thumbs:
            return thumb_centers

        # If gaps filling is OFF, use sparse drawing but ONLY for valid thumbs
        if not getattr(self, 'fill_timeline_gaps', True):
            for i, (tk_img, timestamp) in enumerate(self.thumb_images):
                if timestamp == -1: continue # Skip placeholders even in sparse mode
                
                if num_main > 1:
                    rel = i / (num_main - 1)
                else:
                    rel = 0.5
                
                center_rel = 0.5
                scaled_rel = (rel - center_rel) * self.zoom_factor + center_rel + self.pan_offset
                x_thumb = x0 + scaled_rel * (x1 - x0)
                y_thumb = y_bar_top + thumb_height // 2
                thumb_centers.append((x_thumb, timestamp))
                self.canvas.create_image(x_thumb, y_thumb, image=tk_img, anchor="center", tags=f"thumb_{i}")
            return thumb_centers

        # --- FILMSTRIP LOGIC (remains same, but now only runs if valid_thumbs exist) ---
        tw = self.THUMB_W
        duration = self._get_current_duration() #
        px_start = self.time_to_x(0)
        px_end = self.time_to_x(duration)
        canvas_w = self.canvas.winfo_width()
        
        if tw <= 0: return thumb_centers
        
        total_tiles = int(math.ceil((px_end - px_start) / tw))
        
        for i in range(total_tiles):
            x_tile = px_start + (i * tw)
            if x_tile + tw < 0: continue
            if x_tile > canvas_w: break
                
            target_time = self.get_time_at_x(x_tile + tw / 2)
            best_img, best_ts = min(valid_thumbs, key=lambda t: abs(t[1] - target_time))
            
            self.canvas.create_image(x_tile, y_bar_top, image=best_img, anchor="nw", tags="thumb")
            thumb_centers.append((x_tile + tw / 2, best_ts))

        return thumb_centers

    

  
  
    def draw_overlays(self, x0, x1, y_loop_top, y_loop_bottom, y_bar_top, y_bar_bot, duration, num_main):
        """
        Loop/Cut segments (drawn after time-axis ticks so they overlap tick lower segments),
        segment labels, and playhead. Ticks/labels are drawn in ``draw_time_axis_ticks`` after
        thumbnails. ``y_bar_top`` is kept for callers. Bookmarks are separate.
        """
        active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)
        is_repeating = bool(active_player and getattr(active_player, "loop_active", False))
        strip_h = max(1, int(round(float(y_loop_bottom) - float(y_loop_top))))
        y_strip_top = float(y_loop_top)
        y_strip_bottom = float(y_loop_bottom)
        self._segment_strip_top = y_strip_top
        self._segment_strip_bottom = y_strip_bottom

        if not hasattr(self, "_active_selection_images_tk"):
            self._active_selection_images_tk = []
        self._active_selection_images_tk.clear()

        # Draw inactive segments first (muted rectangles, no drag borders).
        for i, seg in enumerate(self.segments):
            if i == self.active_segment_index:
                continue
            start_time = seg.get("start")
            end_time = seg.get("end")
            if start_time is None or end_time is None:
                continue
            x_s = self.time_to_x(float(start_time))
            x_e = self.time_to_x(float(end_time))
            left, right = min(x_s, x_e), max(x_s, x_e)
            if right - left <= 0:
                continue
            self.canvas.create_rectangle(
                left,
                y_strip_top,
                right,
                y_strip_bottom,
                fill="#444455",
                outline="",
                tags="segment_inactive",
            )
            seg_len = max(0.0, float(end_time) - float(start_time))
            seg_w = right - left
            x_center = left + (seg_w / 2.0)
            if seg_w < 40:
                label = f"C{i + 1}"
            else:
                label = f"Loop/Cut {i + 1} ({seg_len:.1f}s)"
            canvas_w = max(1, self.canvas.winfo_width())
            x_center = min(max(x_center, 20), canvas_w - 20)
            y_lbl = (y_strip_top + y_strip_bottom) / 2.0
            self.canvas.create_text(
                x_center,
                y_lbl,
                text=label,
                fill="#97a6bf",
                font=("Segoe UI", 9, "bold"),
                anchor="center",
                tags="segment_label",
            )

        # Active segment keeps existing loop visuals.
        active_seg = self._get_active_segment()
        start_time = active_seg.get("start") if active_seg else None
        end_time = active_seg.get("end") if active_seg else None

        if is_repeating and start_time is not None and end_time is not None:
            player_s = getattr(active_player, "loop_start", None) if active_player else None
            player_e = getattr(active_player, "loop_end", None) if active_player else None
            if player_s is not None and player_e is not None:
                # IMPORTANT: redraw must never mutate segment data from player loop state.
                # We keep segment bounds as source of truth and only use player loop for playback.
                start_time = float(start_time)
                end_time = float(end_time)

        if start_time is not None and end_time is not None:
            x_s = self.time_to_x(float(start_time))
            x_e = self.time_to_x(float(end_time))
            left, right = min(x_s, x_e), max(x_s, x_e)

            try:
                rect_w = int(right - left)
                if rect_w > 0:
                    alpha = 160
                    r, g, b = (255, 165, 0) if is_repeating else (0, 191, 255)
                    pixel_img = Image.new("RGBA", (1, 1), (r, g, b, alpha))
                    final_overlay_img = pixel_img.resize((rect_w, strip_h), Image.NEAREST)
                    p_img = ImageTk.PhotoImage(final_overlay_img)
                    self._active_selection_images_tk.append(p_img)
                    self.canvas.create_image(left, y_strip_top, image=p_img, anchor="nw", tags="loop_rect_alpha")
            except Exception:
                self.canvas.create_rectangle(
                    left, y_strip_top, right, y_strip_bottom, fill="#444", tags="loop_rect",
                )

            border_col = "#FFA500" if is_repeating else "#00bfff"
            self.canvas.create_line(
                left, y_strip_top, left, y_strip_bottom, fill=border_col, width=2, tags="loop_bar",
            )
            self.canvas.create_line(
                right, y_strip_top, right, y_strip_bottom, fill=border_col, width=2, tags="loop_bar",
            )
            seg_len = max(0.0, float(end_time) - float(start_time))
            seg_w = right - left
            x_center = left + (seg_w / 2.0)
            if seg_w < 40:
                active_label = f"C{(self.active_segment_index or 0) + 1}"
            else:
                active_label = f"Loop/Cut {(self.active_segment_index or 0) + 1} ({seg_len:.1f}s)"
            canvas_w = max(1, self.canvas.winfo_width())
            x_center = min(max(x_center, 20), canvas_w - 20)
            y_lbl = (y_strip_top + y_strip_bottom) / 2.0
            self.canvas.create_text(
                x_center,
                y_lbl,
                text=active_label,
                fill="#d7ecff" if not is_repeating else "#ffd39b",
                font=("Segoe UI", 9, "bold"),
                anchor="center",
                tags="segment_label",
            )

        # --- YELLOW PLAYHEAD (Extended to bottom) ---
        if hasattr(self, "current_time") and duration > 0:
            cx = self.time_to_x(self.current_time)
            cursor_color = "#27C5F5"

            y_playhead_top = max(2.0, float(self.timeline_canvas_top_pad) * 0.5)

            self.canvas.create_line(cx, y_playhead_top, cx, y_bar_bot, fill=cursor_color, width=2, tags="time_cursor")

            self.canvas.create_polygon([
                cx - 8, y_playhead_top - 10, cx + 8, y_playhead_top - 10,
                cx + 8, y_playhead_top + 2, cx, y_playhead_top + 10, cx - 8, y_playhead_top + 2,
            ], fill=cursor_color, outline="black", width=1, tags="time_cursor")
        
   
                
                
    def draw_timeline_background(self, x0, x1, y_bar_bot):
        """Full-width background for the scrollable timeline height."""
        canvas_w = max(1, self.canvas.winfo_width())
        content_h = float(getattr(self, "_timeline_content_height", y_bar_bot) or y_bar_bot)
        self.canvas.create_rectangle(
            0, 0, canvas_w, content_h,
            fill="#1e1e21", outline="", tags="bg",
        )

    def draw_time_axis_ticks(self, x0, x1, base_y, duration, num_main):
        """
        Ruler ticks: bottom on ``base_y`` (thumbnail row top). Major ``base_y-30``…``base_y``,
        minor ``base_y-22``…``base_y``. Time text anchor ``s`` near ``base_y-32``.
        Call after thumbnails so ticks meet the filmstrip; Loop/Cut is drawn later in overlays.
        """
        if num_main <= 1:
            return

        tick_y_bottom = float(base_y)
        major_h = float(getattr(self, "time_axis_major_tick_height", 30))
        minor_h = float(getattr(self, "time_axis_minor_tick_height", 22))
        label_off = float(getattr(self, "time_axis_label_offset_above_thumb_top", 32))
        label_y = tick_y_bottom - label_off
        # Keep label above major tick tops with a sliver of air.
        label_y = min(label_y, tick_y_bottom - major_h - 2)

        for i in range(num_main):
            rel = i / (num_main - 1)
            center_rel = 0.5
            scaled_rel = (rel - center_rel) * self.zoom_factor + center_rel + self.pan_offset
            x_tick = x0 + scaled_rel * (x1 - x0)

            self.canvas.create_line(
                x_tick,
                tick_y_bottom - major_h,
                x_tick,
                tick_y_bottom,
                fill="#555555",
                width=2,
                tags="grid",
            )

            timestamp = duration * i / (num_main - 1)
            label = self.format_time(int(timestamp))
            self.canvas.create_text(
                x_tick,
                label_y,
                text=label,
                fill="#999999",
                font=("Segoe UI", 9, "bold"),
                anchor="s",
                tags="grid",
            )

        num_subdivs = 10
        for i in range(num_main - 1):
            rel_start = i / (num_main - 1)
            rel_end = (i + 1) / (num_main - 1)
            center_rel = 0.5
            scaled_start = (rel_start - center_rel) * self.zoom_factor + center_rel + self.pan_offset
            scaled_end = (rel_end - center_rel) * self.zoom_factor + center_rel + self.pan_offset
            x_start = x0 + scaled_start * (x1 - x0)
            x_end = x0 + scaled_end * (x1 - x0)

            for s in range(1, num_subdivs):
                sub_rel = s / num_subdivs
                xx = x_start + (x_end - x_start) * sub_rel
                self.canvas.create_line(
                    xx,
                    tick_y_bottom - minor_h,
                    xx,
                    tick_y_bottom,
                    fill="#3d3d42",
                    width=1,
                    tags="grid",
                )
                
   
        


    def redraw_timeline(self, only_thumbs=False):
        """Redraws timeline with correct duration, dynamically scaling track background."""
        w = self.canvas.winfo_width()
        x0, x1 = self.get_timeline_bounds()
        
        # 🟢 OPRAVA: Vynutíme zjištění reálné délky, aby markery nebyly mimo
        duration = self._get_current_duration()
        if duration <= 1: # Pokud je to pořád 1, zkusíme manažera
            duration = self.timeline_manager.get_video_duration(self.video_path) or 1

        num_main = self.num_thumbs

        layout = self._compute_timeline_y_layout()
        self._timeline_layout = layout
        self._timeline_content_height = layout["content_height"]

        y_marker_top = layout["y_marker_top"]
        y_marker_bot = layout["y_marker_bot"]
        y_loop_top = layout["y_loop_top"]
        y_loop_bottom = layout["y_loop_bottom"]
        y_bar_top = layout["y_bar_top"]
        thumb_y_top = layout["thumb_y_top"]
        y_bar_bot = layout["y_bar_bot"]

        if not only_thumbs:
            self.canvas.delete("all")
            self.draw_timeline_background(x0, x1, y_bar_bot)
        else:
            self.canvas.delete("thumb")
            self.canvas.delete("marker")
            self.canvas.delete("bookmark_stem")

        self.thumb_centers = self.draw_thumbnails(x0, x1, thumb_y_top, self.THUMB_H, num_main)

        if not only_thumbs:
            self.draw_time_axis_ticks(x0, x1, thumb_y_top, duration, num_main)
            self.draw_markers_above_thumbs(
                x0, x1, y_marker_top, y_marker_bot, duration, phase="stems",
            )
            self.draw_overlays(
                x0, x1, y_loop_top, y_loop_bottom, y_bar_top, y_bar_bot, duration, num_main,
            )
            self.draw_markers_above_thumbs(
                x0, x1, y_marker_top, y_marker_bot, duration, phase="shapes",
            )
        else:
            self.draw_markers_above_thumbs(
                x0, x1, y_marker_top, y_marker_bot, duration, phase="stems",
            )
            self.draw_markers_above_thumbs(
                x0, x1, y_marker_top, y_marker_bot, duration, phase="shapes",
            )

        self.canvas.tag_raise("loop_rect")
        self.canvas.tag_raise("loop_rect_alpha")
        self.canvas.tag_raise("loop_bar")
        self.canvas.tag_raise("segment_label")
        self.canvas.tag_raise("marker")
        self.canvas.tag_raise("time_cursor")

        # Bookmark stems pass behind tick labels and tick marks.
        try:
            gitems = self.canvas.find_withtag("grid")
            if gitems:
                self.canvas.tag_lower("bookmark_stem", gitems[0])
        except tk.TclError:
            pass

        vw = max(1, int(self.canvas.winfo_width()))
        self.canvas.configure(scrollregion=(0, 0, vw, self._timeline_scrollregion_height()))
        self.after_idle(self._update_timeline_vscrollbar_visibility)

        self.update_info_toolbar()

    def redraw_timelineOld(self, only_thumbs=False):
        """Redraws timeline with correct duration and layering."""
        w = self.canvas.winfo_width()
        x0, x1 = self.get_timeline_bounds()
        
        # 🟢 OPRAVA: Vynutíme zjištění reálné délky, aby markery nebyly mimo
        duration = self._get_current_duration()
        if duration <= 1: # Pokud je to pořád 1, zkusíme manažera
            duration = self.timeline_manager.get_video_duration(self.video_path) or 1

        num_main = self.num_thumbs
        layout = self._compute_timeline_y_layout()
        y_marker_top = layout["y_marker_top"]
        y_marker_bot = layout["y_marker_bot"]
        y_loop_top = layout["y_loop_top"]
        y_loop_bottom = layout["y_loop_bottom"]
        y_bar_top = layout["y_bar_top"]
        thumb_y_top = layout["thumb_y_top"]
        y_bar_bot = layout["y_bar_bot"]
        self._timeline_layout = layout
        self._timeline_content_height = layout["content_height"]

        if not only_thumbs:
            self.canvas.delete("all")
            self.draw_timeline_background(x0, x1, y_bar_bot)
            self.thumb_centers = self.draw_thumbnails(x0, x1, thumb_y_top, self.THUMB_H, num_main)
            self.draw_time_axis_ticks(x0, x1, thumb_y_top, duration, num_main)
            self.draw_markers_above_thumbs(
                x0, x1, y_marker_top, y_marker_bot, duration, phase="stems",
            )
            self.draw_overlays(
                x0, x1, y_loop_top, y_loop_bottom, y_bar_top, y_bar_bot, duration, num_main,
            )
            self.draw_markers_above_thumbs(
                x0, x1, y_marker_top, y_marker_bot, duration, phase="shapes",
            )
        else:
            self.canvas.delete("thumb")
            self.canvas.delete("marker")
            self.canvas.delete("bookmark_stem")
            self.thumb_centers = self.draw_thumbnails(x0, x1, thumb_y_top, self.THUMB_H, num_main)
            self.draw_markers_above_thumbs(
                x0, x1, y_marker_top, y_marker_bot, duration, phase="stems",
            )
            self.draw_markers_above_thumbs(
                x0, x1, y_marker_top, y_marker_bot, duration, phase="shapes",
            )

        vw = max(1, int(self.canvas.winfo_width()))
        self.canvas.configure(scrollregion=(0, 0, vw, self._timeline_scrollregion_height()))
        self.after_idle(self._update_timeline_vscrollbar_visibility)
        
        # --- Z-INDEX MAGIE ---
        self.canvas.tag_raise("loop_rect")
        self.canvas.tag_raise("loop_rect_alpha")
        self.canvas.tag_raise("loop_bar")
        self.canvas.tag_raise("segment_label")
        self.canvas.tag_raise("marker")
        self.canvas.tag_raise("time_cursor")




    def apply_snap_and_magnet(self, x, raw_time, duration):
            """
            Calculates the exact timestamp based on active SNAP and MAGNET modes.
            Fixed "NoneType" crash and added sub-ticks for precise grid snapping.
            """
            if duration <= 0:
                return raw_time
                
            snap_points = []
            
            # 🧲 1. MAGNET: Přitahuje k markerům (záložky, tagy, titulky)
            if self.magnet_mode and hasattr(self, 'markers'):
                for m in self.markers:
                    if self.marker_types_visible.get(m.get("type"), True):
                        # 🔥 OPRAVA 1: Zamezení pádu, pokud je timestamp prázdný (None)
                        ts = m.get("timestamp")
                        if ts is not None:
                            snap_points.append(ts)
                        
            # 📐 2. SNAP: Přitahuje k mřížce (ticks) nebo středům náhledů (thumb)
            if self.snap_type == "thumb":
                for img_tuple in self.thumb_images:
                    if isinstance(img_tuple, tuple) and len(img_tuple) == 2 and img_tuple[1] != -1:
                        if img_tuple[1] is not None:
                            snap_points.append(img_tuple[1])
                            
            elif self.snap_type == "tick":
                if self.num_thumbs > 1:
                    # Hlavní ticky
                    for i in range(self.num_thumbs):
                        snap_points.append((i * duration) / (self.num_thumbs - 1))
                    
                    # 🔥 OPRAVA 2: Malé subdivizní ticky (viditelné čárky na mřížce)
                    num_subdivs = 10
                    for i in range(self.num_thumbs - 1):
                        start_time = (i * duration) / (self.num_thumbs - 1)
                        end_time = ((i + 1) * duration) / (self.num_thumbs - 1)
                        for s in range(1, num_subdivs):
                            sub_time = start_time + (end_time - start_time) * (s / num_subdivs)
                            snap_points.append(sub_time)
            elif self.snap_type == "cut":
                # Snap to existing Loop / Cut boundaries for precise segment editing.
                for seg in self.segments:
                    if not isinstance(seg, dict):
                        continue
                    for t in (seg.get("start"), seg.get("end")):
                        if t is not None:
                            snap_points.append(float(t))
            elif self.snap_type == "bookmark":
                # Snap only to bookmark markers.
                for m in getattr(self, "markers", []):
                    if m.get("type") != "bookmark":
                        continue
                    if not self.marker_types_visible.get("bookmark", True):
                        continue
                    t = m.get("timestamp")
                    if t is None:
                        continue
                    try:
                        snap_points.append(float(t))
                    except (TypeError, ValueError):
                        continue

            # Pojistka pro jistotu, odfiltrujeme případné nesmysly
            snap_points = [p for p in snap_points if p is not None]

            # Pokud není k čemu se chytit (všechno je vypnuté), vrátíme volný čas
            if not snap_points:
                return raw_time

            # Najdeme nejbližší časový bod z těch, co jsme posbírali
            closest_time = min(snap_points, key=lambda t: abs(t - raw_time))
            
            # Zjistíme vzdálenost kurzoru myši od tohoto bodu v pixelech na obrazovce
            px_closest = self.time_to_x(closest_time)
            pixel_distance = abs(x - px_closest)
            
            # Gravitace - jak blízko (v pixelech) musí myš být, aby se kurzor přisál
            snap_threshold = 15 
            if pixel_distance <= snap_threshold:
                return closest_time
                
            return raw_time



    def get_time_at_x(self, x):
        """
        Calculates video timestamp for a given pixel X coordinate.
        Uses a mathematical formula instead of a heavy lookup table.
        """
        x0, x1 = self.get_timeline_bounds()
        if x1 <= x0: 
            return 0
        
        duration = self._get_current_duration()
        # Convert pixel to relative 0.0 - 1.0 position
        rel = self.x_to_rel(x, x0, x1)
        
        return rel * duration


    def load_bookmarks_for_path(self, video_path):
            if not video_path: return []
            # Sjednoceno na _bookmarks.json
            bookmarks_file = os.path.splitext(video_path)[0] + "_bookmarks.json"
            if os.path.exists(bookmarks_file):
                try:
                    with open(bookmarks_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        return data if isinstance(data, list) else []
                except Exception as e:
                    logging.error(f"Error loading bookmarks: {e}")
            return []

    def _timeline_norm_path(self, path):
        if not path:
            return ""
        try:
            return os.path.normcase(os.path.normpath(os.path.abspath(path)))
        except (OSError, ValueError, TypeError):
            try:
                return os.path.normcase(os.path.normpath(str(path)))
            except Exception:
                return ""

    def _load_legacy_player_bookmarks_json(self, video_path):
        """Same path as VideoPlayer: ``bookmarks/<basename>.json`` (cwd-relative)."""
        if not video_path:
            return []
        legacy = os.path.join("bookmarks", os.path.basename(video_path) + ".json")
        if not os.path.isfile(legacy):
            return []
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            logging.info("[Timeline] Legacy bookmarks file unreadable %s: %s", legacy, e)
            return []

    def _segments_file_for_path(self, video_path):
        if not video_path:
            return None
        return os.path.splitext(video_path)[0] + "_segments.json"

    def save_segments_for_path(self, video_path):
        segments_file = self._segments_file_for_path(video_path)
        if not segments_file:
            return
        try:
            payload_segments = []
            for seg in self.segments:
                start = seg.get("start")
                end = seg.get("end")
                if start is None or end is None:
                    continue
                payload_segments.append({"start": float(start), "end": float(end)})
            payload = {
                "version": 1,
                "active_segment_index": self.active_segment_index,
                "segments": payload_segments,
            }
            with open(segments_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            self._log_segments_state(f"saved to {os.path.basename(segments_file)}")
        except Exception as e:
            logging.error(f"[Timeline] Failed to save segments: {e}")

    def load_segments_for_path(self, video_path):
        segments_file = self._segments_file_for_path(video_path)
        if not segments_file or not os.path.exists(segments_file):
            self.segments = []
            self.active_segment_index = None
            return
        try:
            with open(segments_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            raw_segments = payload.get("segments", []) if isinstance(payload, dict) else []
            duration = self.timeline_manager.get_video_duration(video_path) or 0.0
            cleaned = []
            for seg in raw_segments:
                if not isinstance(seg, dict):
                    continue
                start = seg.get("start")
                end = seg.get("end")
                if start is None or end is None:
                    continue
                try:
                    start = float(start)
                    end = float(end)
                except (TypeError, ValueError):
                    continue
                if duration > 0:
                    start = max(0.0, min(start, duration))
                    end = max(0.0, min(end, duration))
                else:
                    start = max(0.0, start)
                    end = max(0.0, end)
                if end < start:
                    start, end = end, start
                if end - start < 0.1:
                    end = start + 0.1
                    if duration > 0 and end > duration:
                        end = duration
                        start = max(0.0, end - 0.1)
                cleaned.append({"start": start, "end": end})
            self.segments = cleaned
            idx = payload.get("active_segment_index") if isinstance(payload, dict) else None
            if isinstance(idx, int) and 0 <= idx < len(self.segments):
                self.active_segment_index = idx
            elif self.segments:
                self.active_segment_index = 0
            else:
                self.active_segment_index = None
            self._log_segments_state(f"loaded from {os.path.basename(segments_file)}")
        except Exception as e:
            logging.error(f"[Timeline] Failed to load segments: {e}")
            self.segments = []
            self.active_segment_index = None
        
    def set_video_window(self, video_window):
        self.video_window = video_window
        
    def clear_selection(self):
        """Vymaže vizuální selekci z timeline (volá se při přepnutí videa)."""
        self.loop_mode = False
        self.segments = []
        self.active_segment_index = None
        self.loop_drag = None
        new_duration = self.timeline_manager.get_video_duration(self.video_path) or 1
        self._cached_duration_value = new_duration
        self._cached_duration_path = self.video_path
        if hasattr(self, "loop_button"):
            self._apply_loop_button_style()
        self._log_segments_state("clear_selection")

    def reload_all_markers_and_redraw(self, video_path=None):
        old_path = self.video_path
        if video_path is None:
            video_path = self.video_path
        video_changed = video_path != old_path
        if video_changed:
            self.save_segments_for_path(old_path)
        self.video_path = video_path
        self.load_segments_for_path(video_path)
        self.update_bookmarks()
        self.update_thumbnails()
        self.update_subtitles()
        if video_changed and video_path and os.path.isfile(video_path):
            ctrl = getattr(self, "controller", None)
            if ctrl and hasattr(ctrl, "refresh_bookmark_manager_if_open"):
                ctrl.refresh_bookmark_manager_if_open(video_path)

    def update_thumbnails(self):
        self.markers = [m for m in self.markers if m.get("type") != "thumbnail"]
        if not hasattr(self, "video_path") or not self.video_path or not os.path.isfile(self.video_path):
            return

        try:
            thumb = self.controller.database.get_single_thumbnail(self.video_path)
        except Exception as e:
            logging.info(f"[ERROR] Failed to load thumbnail from DB: {e}")
            thumb = None

        if thumb and "timestamp" in thumb:
            try:
                ts = float(thumb["timestamp"])
                self.markers.append({"type": "thumbnail", "timestamp": ts, "label": "Preview Frame", "color": "#00BFFF"})
            except (TypeError, ValueError):
                logging.warning(f"[Timeline] Invalid thumbnail timestamp: {thumb['timestamp']!r}")

    def update_subtitles(self):
        self.markers = [m for m in self.markers if m.get("type") != "subtitle"]
        if not hasattr(self, "video_path") or not self.video_path or not os.path.isfile(self.video_path):
            return

        try:
            subtitles = []
            if hasattr(self.controller, "current_video_window"):
                window = self.controller.current_video_window
                if hasattr(window, "subtitles") and isinstance(window.subtitles, list):
                    subtitles = window.subtitles
            if not subtitles:
                srt_path = os.path.splitext(self.video_path)[0] + ".srt"
                if os.path.exists(srt_path):
                    from utils import parse_srt_file
                    subtitles = parse_srt_file(srt_path)

            for s in subtitles:
                short_text = s.get("text", "").split("\n")[0][:20]
                timestamp = s.get("start")
                if timestamp is not None:
                    self.markers.append({"type": "subtitle", "timestamp": timestamp, "label": short_text, "color": "#32CD32"})
        except Exception as e:
            logging.info(f"[ERROR] Failed to load subtitle markers: {e}")

    def update_bookmarks(self):
        """Reload bookmarks from player memory, sidecar JSON, and app ``bookmarks/`` folder."""
        if not self.video_path or not os.path.exists(self.video_path):
            return

        self.markers = [m for m in self.markers if m.get("type") != "bookmark"]

        merged_raw = []
        seen = set()

        def add_raw_list(raw):
            for b in raw or []:
                if not isinstance(b, dict):
                    continue
                t = b.get("time") if b.get("time") is not None else b.get("timestamp")
                if t is None:
                    continue
                try:
                    tf = float(t)
                except (TypeError, ValueError):
                    continue
                name = str(b.get("name") if b.get("name") is not None else b.get("label", ""))
                key = (round(tf, 2), name.lower())
                if key in seen:
                    continue
                seen.add(key)
                merged_raw.append(b)

        want = self._timeline_norm_path(self.video_path)
        player = getattr(self.controller, "current_video_window", None) or getattr(
            self.controller, "active_player", None
        )
        if player and want and self._timeline_norm_path(getattr(player, "video_path", None) or "") == want:
            add_raw_list(getattr(player, "bookmarks", []))

        add_raw_list(self.load_bookmarks_for_path(self.video_path))
        add_raw_list(self._load_legacy_player_bookmarks_json(self.video_path))

        for b in merged_raw:
            t = b.get("time") if b.get("time") is not None else b.get("timestamp")
            n = b.get("name") if b.get("name") is not None else b.get("label", "Marker")
            if t is not None:
                marker = {
                    "type": "bookmark",
                    "timestamp": float(t),
                    "label": str(n),
                }
                raw_color = b.get("color")
                custom_color = BookmarkManager._normalize_hex_color(raw_color)
                if BookmarkManager.is_custom_bookmark_color(custom_color):
                    marker["color"] = custom_color
                self.markers.append(marker)

        n_bm = len([m for m in self.markers if m.get("type") == "bookmark"])
        logging.info("[Timeline] Loaded %d bookmarks (merged sources).", n_bm)
            
            
    def draw_markers_above_thumbs(self, x0, x1, y_marker_top, y_marker_bot, duration, phase="both"):
            """
            Draw bookmark/other markers. ``phase``:
            - ``stems`` — dashed guides only (under loop/cut layer);
            - ``shapes`` — tabs + text only;
            - ``both`` — single-pass draw (unused in main redraw; kept for compatibility).
            """
            if not duration or duration <= 0:
                return

            if phase in ("shapes", "both"):
                self.marker_canvas_ids.clear()

            y_marker_top = float(y_marker_top)
            y_marker_bot = float(y_marker_bot)

            # Staggered bookmark labels sit above their tabs; row 0 anchor just above ``y_marker_top``.
            marker_spacing = {} # Pro skládání popisků nad sebe při kolizi X (ne-bookmark markery)

            bookmark_stagger_index = 0

            for marker in self.markers:
                if not self.marker_types_visible.get(marker.get("type"), True):
                    continue

                ts = marker.get("timestamp")
                if ts is None:
                    continue
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    logging.warning(f"[Timeline] Skipping marker with invalid timestamp: {ts!r}")
                    continue

                rel = ts / duration
                center_rel = 0.5
                scaled_rel = (rel - center_rel) * self.zoom_factor + center_rel + self.pan_offset
                x = x0 + scaled_rel * (x1 - x0)

                is_bookmark = marker.get("type") == "bookmark"
                if is_bookmark:
                    n_rows = max(1, int(self.bookmark_label_rows))
                    level = bookmark_stagger_index % n_rows
                    bookmark_stagger_index += 1
                    custom_color = marker.get("color")
                    if BookmarkManager.is_custom_bookmark_color(custom_color):
                        current_color = BookmarkManager._normalize_hex_color(custom_color)
                    else:
                        current_color = DEFAULT_BOOKMARK_COLOR
                else:
                    level = 0
                    current_color = None

                if x0 <= x <= x1:
                    bookmark_y_shift = -3.0 if is_bookmark else 0.0
                    marker_top_y = y_marker_top + bookmark_y_shift
                    marker_bot_y = y_marker_bot + bookmark_y_shift
                    if phase in ("both", "stems") and is_bookmark and level > 0:
                        base_text_y = marker_top_y - 12.0
                        base_b = base_text_y
                        text_y_for_stem = base_b - level * float(self.bookmark_row_height)
                        self.canvas.create_line(
                            x,
                            text_y_for_stem,
                            x,
                            marker_bot_y,
                            fill=current_color,
                            width=1,
                            dash=(2, 3),
                            tags="bookmark_stem",
                        )

                    if phase == "stems":
                        continue

                    color = current_color if is_bookmark else marker.get("color", "#FFA500")

                    if is_bookmark:
                        base_text_y = marker_top_y - 12.0
                        base_b = base_text_y
                        text_y_pos = base_b - level * float(self.bookmark_row_height)
                    else:
                        x_int = int(x / 10)
                        offset = marker_spacing.get(x_int, 0)
                        marker_spacing[x_int] = offset + 1
                        y_text_stack = offset * 15
                        text_y_pos = y_marker_top - 10 - y_text_stack

                    rect_id = self.canvas.create_rectangle(x - 6, marker_top_y, x + 6, marker_bot_y - 5,
                                                 fill=color, outline="", tags="marker")
                    arrow_id = self.canvas.create_polygon([x - 6, marker_bot_y - 5, x + 6, marker_bot_y - 5, x, marker_bot_y],
                                               fill=color, outline="", tags="marker")

                    full_label = str(marker.get("label", ""))
                    if len(full_label) > 12:
                        short_label = full_label[:12] + "..."
                    else:
                        short_label = full_label

                    text_id = self.canvas.create_text(
                        x,
                        text_y_pos,
                        text=short_label,
                        fill=color,
                        font=("Segoe UI", 9, "bold"),
                        anchor="s",
                        tags="marker",
                    )

                    self.marker_canvas_ids[rect_id] = marker
                    self.marker_canvas_ids[arrow_id] = marker
                    self.marker_canvas_ids[text_id] = marker
        
    
    

    def get_timeline_bounds(self):
        w = self.canvas.winfo_width()
        margin_x = int(0.06 * w)
        x0 = margin_x
        x1 = w - margin_x
        return x0, x1

    def start_periodic_update(self):
        self._periodic_update()

    def _periodic_update(self):
        """
        Periodically checks the active player's time and updates the timeline.
        This loop is optimized to only perform actions if the panel is expanded (visible).
        """
        # --- POJISTKA PROTI PÁDU (Crash Fix) ---
        # Pokud už widget neexistuje (aplikace se zavírá), okamžitě skonči.
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        # ---------------------------------------
       
        # Check if the parent (TogglePanelFrame) exists and is expanded.
        if not hasattr(self.parent, 'expanded') or not self.parent.expanded:
            # Just reschedule the check for later
            self.after(300, self._periodic_update)
            return 

          # The code below will ONLY run if the timeline is visible:
        active_player = getattr(self.controller, "active_player", None)
        
        # --- FIX: Kontrola viditelnosti ZDROJOVÉHO přehrávače ---
        # Pokud je to "embed" (náhled v InfoPanelu), musíme ověřit, zda je InfoPanel viditelný.
        # winfo_viewable() vrátí 0, pokud je widget (nebo jeho rodič) skrytý (pack_forget).
        if active_player and getattr(active_player, "embed", False):
            if hasattr(active_player, "video_window") and not active_player.video_window.winfo_viewable():
                # Přehrávač je sice aktivní, ale schovaný -> neposouvat kurzor
                self.after(300, self._periodic_update)
                return
        
      
        # Bezpečné získání cesty (ošetření proti None)
        video_path = getattr(active_player, "video_path", None) if active_player else None
        
        # Pokud není aktivní player nebo video, jen čekáme
        if not active_player or not video_path or not os.path.isfile(video_path):
            self.after(300, self._periodic_update)
            return

        current_time = active_player.get_current_time() if hasattr(active_player, "get_current_time") else None
        if current_time is not None:
            self.set_current_time(current_time)
        
        # Reschedule the next check (runs faster when active)
        self.after(100, self._periodic_update)

    def set_current_time(self, timestamp):
        """
        Updates the playhead position smoothly during video playback.
        Calculates pixel difference and moves the cursor items, avoiding redraws.
        """
        # Spočítáme, kde byl kurzor předtím
        old_cx = self.time_to_x(self.current_time)
        
        # Aktualizujeme čas a zjistíme, kde má být teď
        self.current_time = timestamp
        new_cx = self.time_to_x(timestamp)
        
        # Rozdíl v pixelech
        dx = new_cx - old_cx
        
        # Najdeme všechny prvky kurzoru (čára i ten červený trojúhelníček nahoře)
        cursor_items = self.canvas.find_withtag("time_cursor")
        
        if cursor_items:
            # Jen je "šoupneme" o vypočtený rozdíl pixelů doprava/doleva
            self.canvas.move("time_cursor", dx, 0)
        else:
            # Pokud kurzor vůbec neexistuje (např. úplně první spuštění), překreslíme
            self.redraw_timeline()
        self.update_info_toolbar()


    def _get_current_duration(self):
            """
            Retrieves video duration.
            Forces a refresh if cache is empty or belongs to a different video.
            """
            cached = getattr(self, '_cached_duration_value', 0)
            cached_path = getattr(self, '_cached_duration_path', None)

            # Refresh when cache is zero OR when it was computed for a different video
            if cached <= 0 or cached_path != self.video_path:
                if hasattr(self, 'timeline_manager') and self.video_path:
                    d = self.timeline_manager.get_video_duration(self.video_path)
                    if d > 0:
                        self._cached_duration_value = d
                        self._cached_duration_path = self.video_path
                        return d

            return max(cached, 1)  # vracíme aspoň 1, aby se nedělilo nulou

    def on_timeline_click(self, event):
            """
            Handles mouse clicks on the timeline to seek video position.
            Uses math calculation and applies Snap/Magnet logic.
            """
            duration = self._get_current_duration()
            if duration <= 0: 
                return

            # 1. Calculate raw time directly from X coordinate
            cx = self._canvas_pointer_x(event)
            raw_time = self.get_time_at_x(cx)
            
            # 2. 🔥 ZDE SE APLIKUJE PŘITAŽLIVOST
            clicked_time = self.apply_snap_and_magnet(cx, raw_time, duration)
            
            if self.on_seek:
                self.on_seek(clicked_time)
            
            # Manually update playhead position for instant feedback
            self.set_current_time(clicked_time)


        
        
    # def on_timeline_click(self, event):
        # x = event.x
        # x0, x1 = self.get_timeline_bounds()
        # duration = self._get_current_duration()
        # relative_position = self.x_to_rel(x, x0, x1)
        # timestamp = relative_position * duration
        # logging.info(f"[TimelineBarWidget] Clicked timeline (duration: {duration:.2f}s) at x={event.x}, time={timestamp:.2f}s")
        # if self.on_seek:
            # self.on_seek(timestamp)
    
    def get_snap_times(self, duration, num_main, num_subdivs):
        snap_times = []
        if num_main > 1:
            for i in range(num_main - 1):
                t_start = duration * i / (num_main - 1)
                t_end = duration * (i + 1) / (num_main - 1)
                if i == 0:
                    snap_times.append(t_start)
                for s in range(1, num_subdivs):
                    t_sub = t_start + s * (t_end - t_start) / num_subdivs
                    snap_times.append(t_sub)
                snap_times.append(t_end)
        return snap_times
        
        
    def on_canvas_drag(self, event):
        """Handles mouse drag events for timeline playhead, and moving/resizing selections."""
        x = self._canvas_pointer_x(event)
        x0, x1 = self.get_timeline_bounds()
        duration = self._get_current_duration() or 60
        raw_time = self.x_to_rel(x, x0, x1) * duration
        new_time = self.apply_snap_and_magnet(x, raw_time, duration)

        active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)
        is_active = active_player and getattr(active_player, "loop_active", False)

        active_seg = self._get_active_segment()

        if self.loop_drag == "select":
            if active_seg is None:
                self.on_timeline_click(event)
                return
            new_start = min(self._selection_anchor, new_time)
            new_end = max(self._selection_anchor, new_time)
            self._set_active_segment_bounds(new_start, new_end)
            if is_active:
                active_player.loop_start = self.loop_start
                active_player.loop_end = self.loop_end
            self.redraw_timeline()
            return

        if self.loop_drag == "start":
            if active_seg is None or self.loop_end is None:
                return
            new_start = min(new_time, self.loop_end - 0.1)
            self._set_active_segment_bounds(new_start, self.loop_end)
            if is_active:
                if getattr(active_player, "loop_end", None) is None: active_player.loop_end = self.loop_end
                active_player.set_loop_start_from_timeline(self.loop_start)
            self.redraw_timeline()
            return

        elif self.loop_drag == "end":
            if active_seg is None or self.loop_start is None:
                return
            new_end = max(new_time, self.loop_start + 0.1)
            self._set_active_segment_bounds(self.loop_start, new_end)
            if is_active:
                if getattr(active_player, "loop_start", None) is None: active_player.loop_start = self.loop_start
                active_player.set_loop_end_from_timeline(self.loop_end)
            self.redraw_timeline()
            return

        elif self.loop_drag == "move_pending":
            start_x = getattr(self, "_drag_pending_start_x", x)
            if abs(x - start_x) < 6:
                return
            self.loop_drag = "move"
            logging.info("[CUT_DEBUG] move_pending -> move (dx=%s)", abs(x - start_x))

        if self.loop_drag == "move":
            if active_seg is None or self.loop_start is None or self.loop_end is None:
                return
            loop_duration = self.loop_end - self.loop_start
            new_start = raw_time - self.drag_offset_time
            new_end = new_start + loop_duration

            if new_start < 0:
                new_start = 0
                new_end = loop_duration
            if new_end > duration:
                new_end = duration
                new_start = duration - loop_duration

            self._set_active_segment_bounds(new_start, new_end)

            if is_active:
                if getattr(active_player, "loop_start", None) is None: active_player.loop_start = self.loop_start
                if getattr(active_player, "loop_end", None) is None: active_player.loop_end = self.loop_end
                active_player.set_loop_start_from_timeline(self.loop_start)
                active_player.set_loop_end_from_timeline(self.loop_end)
            self.redraw_timeline()
            return

        self.on_timeline_click(event)

      

    def on_canvas_release(self, event):
        if self.loop_drag == "select":
            seg = self._get_active_segment()
            if seg and seg.get("start") is not None and seg.get("end") is not None:
                seg_len = float(seg["end"]) - float(seg["start"])
                if seg_len < 0.1:
                    duration = self._get_current_duration() or 0.0
                    default_len = 1.0
                    new_start = float(seg["start"])
                    new_end = new_start + default_len
                    if duration > 0 and new_end > duration:
                        new_end = duration
                        new_start = max(0.0, new_end - default_len)
                    self._set_active_segment_bounds(new_start, new_end)
                    self.redraw_timeline()
        if self.loop_drag == "move_pending":
            # Simple click selected the segment; do not move it.
            self._log_segments_state("release move_pending (no move)")
            self.loop_drag = None
            self.canvas.config(cursor="")
            return
        if self.loop_drag:
            self._log_segments_state(f"release drag={self.loop_drag}")
            active_player = getattr(self.controller, "current_video_window", None)
            if active_player and hasattr(active_player, "update_loop_bar_display"):
                active_player.update_loop_bar_display()
                logging.info("[DEBUG] Loop bar ve video přehrávači byl aktualizován.")
            self.save_segments_for_path(self.video_path)
        self.loop_drag = None
        self.canvas.config(cursor="")

    def on_thumb_click_event(self, timestamp, event):
        self.on_thumb_click(timestamp)

    def on_timeline_drag(self, event):
        self.on_timeline_click(event)

    def format_time(self, seconds):
        m, s = divmod(seconds, 60)
        return f"{int(m):02}:{int(s):02}"

    def on_thumb_click(self, timestamp):
        logging.info(f"Clicked at {timestamp}s")
        if self.on_seek:
            logging.info("clicked ON SEEK")
            self.on_seek(timestamp)
            
    def on_close(self):
        self.save_segments_for_path(self.video_path)
        if hasattr(self.parent, "timeline_window"):
            self.parent.timeline_window = None
        self.destroy()


    def on_mouse_move(self, event):
            """Checks if mouse is over a marker and shows the full tooltip."""
            cx, cy = self._canvas_pointer_xy(event)
            items = self.canvas.find_overlapping(cx - 2, cy - 2, cx + 2, cy + 2)
            
            found_marker = None
            for item_id in items:
                if item_id in self.marker_canvas_ids:
                    found_marker = self.marker_canvas_ids[item_id]
                    break
            
            if found_marker:
                self.show_marker_tooltip(cx, cy, found_marker)
                self.canvas.config(cursor="hand2")
            else:
                seg_hit = self._get_segment_hover_at(cx, cy)
                if seg_hit is not None:
                    self.show_segment_tooltip(cx, cy, seg_hit)
                    self.canvas.config(cursor="hand2")
                else:
                    self.hide_marker_tooltip()
                    # Vrátíme kurzor do původního stavu (pokud zrovna netaháme smyčku)
                    if not self.loop_drag:
                        self.canvas.config(cursor="")

    def _get_segment_hover_at(self, x, y):
        if not self._is_in_segment_strip_y(y):
            return None
        for i in range(len(self.segments) - 1, -1, -1):
            seg = self.segments[i]
            start_time = seg.get("start")
            end_time = seg.get("end")
            if start_time is None or end_time is None:
                continue
            x_s = self.time_to_x(float(start_time))
            x_e = self.time_to_x(float(end_time))
            left, right = min(x_s, x_e), max(x_s, x_e)
            if left <= x <= right:
                return {
                    "index": i,
                    "start": float(start_time),
                    "end": float(end_time),
                }
        return None

    def _is_in_segment_strip_y(self, y):
        strip_top = getattr(self, "_segment_strip_top", None)
        strip_bottom = getattr(self, "_segment_strip_bottom", None)
        if strip_top is None or strip_bottom is None:
            return False
        return strip_top <= y <= strip_bottom

    def show_marker_tooltip(self, x, y, marker):
        """Displays a neat tooltip on the canvas with full text."""
        self.canvas.delete("marker_tooltip")
        
        full_text = f"{marker['label']} [{self.format_time(marker['timestamp'])}]"
        
        # Vytvoření textu (trochu posunutý od kurzoru)
        txt_id = self.canvas.create_text(x + 15, y - 15, text=full_text, fill="white", 
                                        anchor="sw", font=("Segoe UI", 10, "bold"), tags="marker_tooltip")
        
        # Vytvoření pozadí pod textem (bublina)
        bbox = self.canvas.bbox(txt_id)
        if bbox:
            bg_id = self.canvas.create_rectangle(bbox[0]-5, bbox[1]-2, bbox[2]+5, bbox[3]+2, 
                                                 fill="#333", outline="#FFA500", tags="marker_tooltip")
            self.canvas.tag_lower(bg_id, txt_id)

    def show_segment_tooltip(self, x, y, seg_hit):
        self.canvas.delete("marker_tooltip")
        idx = int(seg_hit["index"])
        start = float(seg_hit["start"])
        end = float(seg_hit["end"])
        dur = max(0.0, end - start)
        full_text = (
            f"Loop/Cut {idx + 1}\n"
            f"Start: {self.format_time(start)}\n"
            f"End: {self.format_time(end)}\n"
            f"Duration: {dur:.1f}s"
        )
        txt_id = self.canvas.create_text(
            x + 15,
            y - 15,
            text=full_text,
            fill="white",
            anchor="sw",
            font=("Segoe UI", 10, "bold"),
            tags="marker_tooltip",
        )
        bbox = self.canvas.bbox(txt_id)
        if bbox:
            bg_id = self.canvas.create_rectangle(
                bbox[0] - 5,
                bbox[1] - 2,
                bbox[2] + 5,
                bbox[3] + 2,
                fill="#333",
                outline="#00bfff",
                tags="marker_tooltip",
            )
            self.canvas.tag_lower(bg_id, txt_id)

    def hide_marker_tooltip(self):
        """Cleans up the tooltip from canvas."""
        self.canvas.delete("marker_tooltip")
