"""
Shared utilities for Vibe Player.

Provides ``ThumbnailCache``, video metadata helpers (``get_video_size``,
``extract_video_info``), Tk menu helpers, and SRT subtitle parsing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tkinter as tk
from tkinter import Menu
from typing import Any, Callable

import cv2
from pymediainfo import MediaInfo

_OPENCV_FRAGILE_VIDEO_EXTS = (
    ".wmv",
    ".avi",
    ".mpg",
    ".mpeg",
    ".vob",
    ".m2v",
    ".m1v",
    ".ts",
    ".mts",
    ".m2ts",
)


class Tooltip:
    """Lightweight tooltip for compact icon controls."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None
        self.widget.bind("<Enter>", self._show, add="+")
        self.widget.bind("<Leave>", self._hide, add="+")
        self.widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, _event=None):
        if self.tip_window is not None:
            return

        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.withdraw()
        tw.overrideredirect(True)
        tw.configure(bg="#0f1115", highlightbackground="#6b7280", highlightthickness=1)
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass

        tk.Label(
            tw,
            text=self.text,
            bg="#0f1115",
            fg="#ffffff",
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=4,
            font=("Segoe UI", 9),
        ).pack()
        tw.update_idletasks()

        x, y = self._position_for(tw.winfo_reqwidth(), tw.winfo_reqheight())
        tw.geometry(f"+{x}+{y}")
        tw.deiconify()
        tw.lift()

    def _hide(self, _event=None):
        if self.tip_window is not None:
            self.tip_window.destroy()
            self.tip_window = None

    def _position_for(self, tip_width: int, tip_height: int) -> tuple[int, int]:
        widget_x = self.widget.winfo_rootx()
        widget_y = self.widget.winfo_rooty()
        widget_w = self.widget.winfo_width()
        widget_h = self.widget.winfo_height()

        monitor_x, monitor_y, monitor_w, monitor_h = self._monitor_bounds(
            widget_x + widget_w // 2,
            widget_y + widget_h // 2,
        )
        margin = 8

        x = widget_x + (widget_w - tip_width) // 2
        min_x = monitor_x + margin
        max_x = monitor_x + monitor_w - tip_width - margin
        if max_x < min_x:
            max_x = min_x
        x = max(min_x, min(x, max_x))

        above_y = widget_y - tip_height - 6
        below_y = widget_y + widget_h + 6
        monitor_bottom = monitor_y + monitor_h
        if above_y >= monitor_y + margin:
            y = above_y
        elif below_y + tip_height <= monitor_bottom - margin:
            y = below_y
        else:
            y = max(monitor_y + margin, min(below_y, monitor_bottom - tip_height - margin))
        return int(x), int(y)

    def _monitor_bounds(self, x: int, y: int) -> tuple[int, int, int, int]:
        try:
            from screeninfo import get_monitors

            monitors = get_monitors()
            for monitor in monitors:
                if monitor.x <= x < monitor.x + monitor.width and monitor.y <= y < monitor.y + monitor.height:
                    return monitor.x, monitor.y, monitor.width, monitor.height
            if monitors:
                nearest = min(
                    monitors,
                    key=lambda m: (
                        max(m.x - x, 0, x - (m.x + m.width)) ** 2
                        + max(m.y - y, 0, y - (m.y + m.height)) ** 2
                    ),
                )
                return nearest.x, nearest.y, nearest.width, nearest.height
        except Exception:
            pass

        try:
            return (
                self.widget.winfo_vrootx(),
                self.widget.winfo_vrooty(),
                self.widget.winfo_vrootwidth(),
                self.widget.winfo_vrootheight(),
            )
        except tk.TclError:
            return 0, 0, self.widget.winfo_screenwidth(), self.widget.winfo_screenheight()


def _get_local_ffprobe_path() -> str:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(repo_root, "tools", "ffmpeg", "bin", "ffprobe.exe"),
        os.path.join(repo_root, "tools", "ffmpeg", "ffprobe.exe"),
        os.path.abspath(os.path.join("tools", "ffmpeg", "bin", "ffprobe.exe")),
        os.path.abspath(os.path.join("tools", "ffmpeg", "ffprobe.exe")),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return "ffprobe"


class ThumbnailCache:
    """Singleton in-memory cache for video/image thumbnails with hit/miss statistics."""

    _instance: ThumbnailCache | None = None

    def __new__(cls, *args: Any, **kwargs: Any) -> ThumbnailCache:
        if not cls._instance:
            cls._instance = super(ThumbnailCache, cls).__new__(cls, *args, **kwargs)
            cls._instance.cache = {}
            cls._instance.stats = {"hits": 0, "misses": 0}
            cls._instance.cache_read_times: list[float] = []
        return cls._instance

    def get(self, path: str, memory_cache: bool = True) -> Any:
        """Return cached thumbnail if present, else None."""
        if memory_cache:
            if path in self.cache:
                self.stats["hits"] += 1
                return self.cache[path]
            self.stats["misses"] += 1
        return None

    def set(self, path: str, thumbnail: Any, memory_cache: bool = True) -> None:
        """Store a thumbnail in the memory cache."""
        if memory_cache:
            self.cache[path] = thumbnail

    def clear(self) -> None:
        """Clear the memory cache and reset statistics."""
        self.cache.clear()
        self.reset_stats()
        logging.info("ThumbnailCache memory cache cleared.")

    def discard_under_directory(
        self, folder_path: str, normalize_path: Callable[[str], str]
    ) -> None:
        """Remove cached thumbnails whose path lies under folder_path (inclusive)."""
        if not folder_path:
            return
        try:
            norm_root = normalize_path(folder_path)
        except Exception:
            norm_root = os.path.normcase(os.path.normpath(folder_path))
        prefix = norm_root + os.sep
        removed = 0
        for key in list(self.cache.keys()):
            sk = str(key)
            base_path = sk.split("\x00", 1)[0]
            try:
                nk = normalize_path(base_path)
            except Exception:
                nk = os.path.normcase(os.path.normpath(base_path))
            if nk == norm_root or nk.startswith(prefix):
                self.cache.pop(key, None)
                removed += 1
        if removed:
            logging.info(
                "ThumbnailCache discarded %d in-memory entries under %s",
                removed,
                folder_path,
            )

    def cache_stats(self) -> dict[str, Any]:
        """Return cache statistics for debugging."""
        memory_usage_kb = sum(
            thumb.nbytes / 1024
            for thumb in self.cache.values()
            if hasattr(thumb, "nbytes")
        )
        return {
            "hits": self.stats["hits"],
            "misses": self.stats["misses"],
            "memory_usage_kb": memory_usage_kb,
            "avg_read_time_ms": (
                (sum(self.cache_read_times) / len(self.cache_read_times) * 1000)
                if self.cache_read_times
                else 0
            ),
        }

    def reset_stats(self) -> None:
        """Reset hit/miss counters."""
        self.stats = {"hits": 0, "misses": 0}


def get_video_size(video_path: str) -> tuple[int | None, int | None]:
    """
    Return ``(width, height)`` for a video file.

    Uses ffprobe first, then OpenCV as a fallback.
    """
    try:
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        cmd = [
            _get_local_ffprobe_path(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            video_path,
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            startupinfo=startupinfo,
            timeout=2,
        )

        if result.stdout:
            info = json.loads(result.stdout)
            if "streams" in info and len(info["streams"]) > 0:
                width = int(info["streams"][0].get("width", 0))
                height = int(info["streams"][0].get("height", 0))
                if width > 0 and height > 0:
                    return width, height
    except Exception:
        pass

    if os.path.splitext(video_path)[1].lower() in _OPENCV_FRAGILE_VIDEO_EXTS:
        return None, None

    cap = None
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open video file: {video_path}")

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return width, height
    except Exception as e:
        logging.error("Error getting video size for %s: %s", video_path, e)
        return None, None
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


def create_menu(app: Any, parent: Any) -> Menu:
    """
    Build a themed Tkinter menu and wrap add_* so menu use triggers the app's
    interaction guard when present.
    """
    menu = Menu(parent, tearoff=0)
    menu.configure(
        background=app.BackroundColor,
        foreground=app.thumb_TextColor,
        activebackground="#404040",
        activeforeground="white",
        relief="sunken",
        activeborderwidth=0,
        borderwidth=0,
        font=("Helvetica", 12),
        # Checkmarks / radio dots stay visible on dark menu backgrounds (Windows Tk).
        selectcolor="#d0d0d0",
    )

    def wrap_method(original_method: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            original_command = kwargs.get("command")
            if original_command:
                def wrapped_command(*a: Any, **kw: Any) -> Any:
                    if hasattr(app, "_mark_menu_interaction"):
                        app._mark_menu_interaction()
                    return original_command(*a, **kw)

                kwargs["command"] = wrapped_command
            return original_method(*args, **kwargs)

        return wrapper

    menu.add_command = wrap_method(menu.add_command)
    menu.add_checkbutton = wrap_method(menu.add_checkbutton)
    menu.add_radiobutton = wrap_method(menu.add_radiobutton)

    return menu


def extract_video_info(file_path: str) -> dict[str, Any]:
    """Extract video/audio metadata (codec, fps, bitrate, color, audio) via pymediainfo."""
    result: dict[str, Any] = {
        "width": None,
        "height": None,
        "duration": None,
        "codec": None,
        "fps": None,
        "bitrate": None,
        "compression": None,
        "bit_depth": None,
        "color_space": None,
        "chroma_subsampling": None,
        "audio_codec": None,
        "audio_channels": None,
        "audio_sample_rate": None,
        "audio_bitrate": None,
        "scan_type": None,
        "encoder_library": None,
        "compression_ratio": None,
    }

    try:
        info = MediaInfo.parse(file_path)
        general_track = next((t for t in info.tracks if t.track_type == "General"), None)
        video_track = next((t for t in info.tracks if t.track_type == "Video"), None)
        audio_track = next((t for t in info.tracks if t.track_type == "Audio"), None)

        duration = getattr(general_track, "duration", None) if general_track else None
        if not duration and video_track:
            duration = getattr(video_track, "duration", None)
        if duration:
            try:
                result["duration"] = float(duration) / 1000.0
            except (TypeError, ValueError):
                pass

        if video_track:
            width = getattr(video_track, "width", None)
            height = getattr(video_track, "height", None)
            result["width"] = int(width) if width else None
            result["height"] = int(height) if height else None
            result["codec"] = video_track.codec_id or video_track.codec or video_track.format
            result["fps"] = float(video_track.frame_rate) if video_track.frame_rate else None
            result["bitrate"] = int(video_track.bit_rate) if video_track.bit_rate else None
            result["compression"] = video_track.compression_mode or None

            bit_depth = getattr(video_track, "bit_depth", None)
            result["bit_depth"] = int(bit_depth) if bit_depth else None
            result["color_space"] = (
                getattr(video_track, "color_primaries", None)
                or getattr(video_track, "colour_primaries", None)
            )
            result["chroma_subsampling"] = getattr(video_track, "chroma_subsampling", None)

            result["scan_type"] = getattr(video_track, "scan_type", None)
            result["encoder_library"] = (
                getattr(video_track, "writing_library", None)
                or getattr(video_track, "encoded_library", None)
            )

            try:
                w = int(video_track.width or 0)
                h = int(video_track.height or 0)
                fps = result["fps"] or 0
                depth = result["bit_depth"] or 8
                actual_br = result["bitrate"] or 0
                if w and h and fps and actual_br:
                    uncompressed = w * h * fps * depth * 1.5
                    result["compression_ratio"] = round(uncompressed / actual_br, 1)
            except Exception:
                pass

        if audio_track:
            result["audio_codec"] = (
                getattr(audio_track, "codec_id", None)
                or getattr(audio_track, "commercial_name", None)
                or getattr(audio_track, "format", None)
            )
            ch = getattr(audio_track, "channel_s", None)
            if ch is not None:
                ch = int(ch)
                result["audio_channels"] = {
                    1: "Mono",
                    2: "Stereo",
                    6: "5.1",
                    8: "7.1",
                }.get(ch, f"{ch} ch")
            sr = getattr(audio_track, "sampling_rate", None)
            result["audio_sample_rate"] = f"{int(sr) // 1000} kHz" if sr else None
            result["audio_bitrate"] = int(audio_track.bit_rate) if audio_track.bit_rate else None

    except Exception as e:
        logging.error("Failed to extract media info from %s: %s", file_path, e)

    return result


def parse_srt_file(path: str) -> list[dict[str, Any]]:
    """Parse a simple SRT file into a list of dicts with ``start`` (seconds) and ``text``."""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    pattern = re.compile(
        r"(\d+)\s+(\d{2}:\d{2}:\d{2},\d{3}) --> .*?\s+(.*?)\s*(?=\d+\s+\d{2}|\Z)",
        re.DOTALL,
    )
    subtitles: list[dict[str, Any]] = []
    for match in pattern.finditer(content):
        start_time_str = match.group(2).replace(",", ".")
        text = match.group(3).strip()
        h, m, s = map(float, start_time_str.split(":"))
        start_seconds = h * 3600 + m * 60 + s
        subtitles.append({"start": start_seconds, "text": text})
    return subtitles
