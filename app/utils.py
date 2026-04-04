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
from tkinter import Menu
from typing import Any

import cv2
from pymediainfo import MediaInfo


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
            "ffprobe",
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
        video_track = next((t for t in info.tracks if t.track_type == "Video"), None)
        audio_track = next((t for t in info.tracks if t.track_type == "Audio"), None)

        if video_track:
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
