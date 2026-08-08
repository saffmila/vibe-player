"""
Convert a whole video file (library RMB → Convert Video…).

UI mirrors Merge / Export (Lossless remux / Custom re-encode). No cuts or loops —
the entire source is written to a new file.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from tkinter import filedialog, messagebox

import customtkinter as ctk

from file_operations import (
    get_ffmpeg_path,
    get_ffprobe_path,
    get_video_duration_mediainfo,
    probe_first_video_stream,
)
from video_merge import (
    _clear_status_later,
    _custom_audio_args,
    _custom_codec_args,
    _custom_video_filter,
    _has_audio_stream,
    _run_ffmpeg_with_progress,
    _set_status,
    _ui_call,
)

CONVERT_DIALOG_WIDTH = 420
CONVERT_DIALOG_MIN_WIDTH = 380
CONVERT_DIALOG_SCREEN_MARGIN = 24
CONVERT_DIALOG_MAX_HEIGHT = 780
CUSTOM_SCROLL_HEIGHT = 420

PRESET_CUSTOM = "Custom…"
VIDEO_QUALITY_LEVELS = ("Low", "Medium", "High")
AUDIO_BITRATE_LEVELS = ("96k", "128k", "192k", "256k")
DEFAULT_VIDEO_QUALITY = "High"
DEFAULT_AUDIO_BITRATE = "192k"

# SeedVR-like section cards
_UI_SECTION_BG = ("gray88", "#2a2a2a")
_UI_SECTION_BORDER = ("gray70", "#3d3d3d")
_UI_SECTION_TITLE = "#8ab4c8"
_UI_INFO_BG = ("gray85", "#0c0c0c")
_UI_INFO_TEXT = ("#555555", "#9aa3ad")

_ENTRY_TEXT_LOCKED = ("#7a7a7a", "#7a7a7a")
_ENTRY_FG_LOCKED = ("#d0d0d0", "#252525")
_ENTRY_TEXT_EDIT = ("gray14", "#DCE4EE")
_ENTRY_FG_EDIT = ("#F9F9FA", "#343638")

PRESET_INFO = {
    "MP4 · original size": (
        "MP4 · H.264 · keeps source size & FPS\n"
        "High quality re-encode · General use / archive"
    ),
    "WebM · original size": (
        "WebM · VP9 · keeps source size & FPS\n"
        "Smaller files · Web delivery"
    ),
    "MP4 1920x1080": (
        "MP4 · 1920×1080 @ 30 fps · H.264\n"
        "Full HD · TV / desktop / YouTube"
    ),
    "MP4 1600x1200 HQ": (
        "MP4 · 1600×1200 @ 30 fps · H.264\n"
        "4:3 HQ · Legacy displays / kiosk"
    ),
    "MP4 1280x720": (
        "MP4 · 1280×720 @ 30 fps · H.264\n"
        "HD 720p · Laptop / tablet / web"
    ),
    "MP4 854x480": (
        "MP4 · 854×480 @ 30 fps · H.264\n"
        "SD · Small screens / low bandwidth"
    ),
    "AVI 640x480": (
        "AVI · 640×480 @ 25 fps · MPEG-4\n"
        "Legacy / compatibility"
    ),
    PRESET_CUSTOM: (
        "Manual settings — edit video & audio below\n"
        "Choose format, size, quality and audio bitrate"
    ),
}

AUDIO_INFO = {
    "96k": "96 kbps — compact, speech / background",
    "128k": "128 kbps — light stereo / web",
    "192k": "192 kbps — solid stereo for most uses",
    "256k": "256 kbps — higher fidelity music / archive",
}

_FRAGILE_LOSSLESS_CONTAINERS = (
    ".mpg",
    ".mpeg",
    ".vob",
    ".m2v",
    ".m1v",
    ".ts",
    ".mts",
    ".m2ts",
)

_SUBPROCESS_STARTUPINFO = None
if os.name == "nt":
    _SUBPROCESS_STARTUPINFO = subprocess.STARTUPINFO()
    _SUBPROCESS_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _SUBPROCESS_STARTUPINFO.wShowWindow = subprocess.SW_HIDE


def open_convert_video_dialog(parent, video_path, controller=None):
    """Open the convert dialog for a single video path."""
    path = os.path.normpath(video_path) if video_path else ""
    if not path or not os.path.isfile(path):
        messagebox.showinfo("Convert Video", "Select a video file to convert.")
        return None
    return VideoConvertDialog(parent, path, controller=controller)


def _parse_frame_rate(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("0/0", "N/A"):
        return None
    try:
        if "/" in text:
            num_s, den_s = text.split("/", 1)
            num = float(num_s)
            den = float(den_s)
            if den == 0:
                return None
            rate = num / den
        else:
            rate = float(text)
        if rate <= 0 or rate > 240:
            return None
        return rate
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _probe_source_props(video_path: str) -> tuple[int | None, int | None, float | None]:
    """Return (width, height, fps) from ffprobe, or (None, None, None)."""
    try:
        ffprobe = get_ffprobe_path()
    except FileNotFoundError:
        return None, None, None
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate",
        "-of",
        "json",
        video_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            startupinfo=_SUBPROCESS_STARTUPINFO,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None, None, None
        data = json.loads(result.stdout)
        streams = data.get("streams") or []
        if not streams:
            return None, None, None
        stream = streams[0]
        width = stream.get("width")
        height = stream.get("height")
        try:
            width = int(width) if width else None
        except (TypeError, ValueError):
            width = None
        try:
            height = int(height) if height else None
        except (TypeError, ValueError):
            height = None
        fps = _parse_frame_rate(stream.get("avg_frame_rate")) or _parse_frame_rate(
            stream.get("r_frame_rate")
        )
        return width, height, fps
    except Exception:
        logging.exception("[Convert] Failed to probe source props for %s", video_path)
        return None, None, None


class VideoConvertDialog(ctk.CTkToplevel):
    """Lossless remux or Custom re-encode of one whole video file."""

    def __init__(self, parent, video_path, controller=None):
        super().__init__(parent)
        self.title("Convert Video")
        self.controller = controller
        self.video_path = os.path.normpath(video_path)
        self.resizable(True, True)

        self._source_width, self._source_height, self._source_fps = _probe_source_props(
            self.video_path
        )

        # Named presets freeze W/H/FPS. ``keep_size`` re-encodes without rescale.
        self.presets = {
            "MP4 · original size": {"ext": ".mp4", "keep_size": True},
            "WebM · original size": {"ext": ".webm", "keep_size": True},
            "MP4 1920x1080": {"ext": ".mp4", "width": 1920, "height": 1080, "fps": 30},
            "MP4 1600x1200 HQ": {"ext": ".mp4", "width": 1600, "height": 1200, "fps": 30},
            "MP4 1280x720": {"ext": ".mp4", "width": 1280, "height": 720, "fps": 30},
            "MP4 854x480": {"ext": ".mp4", "width": 854, "height": 480, "fps": 30},
            "AVI 640x480": {"ext": ".avi", "width": 640, "height": 480, "fps": 25},
        }
        self._preset_values = list(self.presets.keys()) + [PRESET_CUSTOM]
        self.preset_var = ctk.StringVar(value="MP4 · original size")
        self.ext_var = ctk.StringVar(value=".mp4")
        self.width_var = ctk.StringVar(value="")
        self.height_var = ctk.StringVar(value="")
        self.fps_var = ctk.StringVar(value="")
        self.sound_var = ctk.BooleanVar(value=True)
        self.video_quality_var = ctk.StringVar(value=DEFAULT_VIDEO_QUALITY)
        self.audio_bitrate_var = ctk.StringVar(value=DEFAULT_AUDIO_BITRATE)
        self.lossless_container_var = ctk.StringVar(value="MKV (recommended)")
        self._dim_entries: list[ctk.CTkEntry] = []
        self._fit_token = 0

        self._button_bar = ctk.CTkFrame(self, fg_color="transparent")
        self._button_bar.pack(side="bottom", fill="x", padx=8, pady=8)
        self.status_var = ctk.StringVar(value=self._duration_summary())
        ctk.CTkLabel(
            self._button_bar,
            textvariable=self.status_var,
            text_color="#bfc7d5",
            font=("", 10),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))
        ctk.CTkButton(
            self._button_bar, text="Close", width=90, height=28, command=self.destroy
        ).pack(side="left")
        ctk.CTkButton(
            self._button_bar, text="Start Convert", height=28, command=self.start_convert
        ).pack(side="right", fill="x", expand=True, padx=(10, 0))

        self._source_lbl = ctk.CTkLabel(self, text="Source", text_color="#00bfff")
        self._source_lbl.pack(pady=(8, 0))
        self._src_frame = ctk.CTkFrame(self)
        self._src_frame.pack(pady=4, padx=8, fill="x")
        ctk.CTkLabel(
            self._src_frame,
            text=os.path.basename(self.video_path),
            anchor="w",
            justify="left",
            wraplength=320,
        ).pack(fill="x", padx=8, pady=6)

        # SeedVR-style mode switch: pack/forget frames (CTkTabview left empty gaps).
        self.mode_var = ctk.StringVar(value="Custom")
        self._mode_seg = ctk.CTkSegmentedButton(
            self,
            values=["Lossless", "Custom"],
            variable=self.mode_var,
            command=self._on_mode_changed,
            height=28,
        )
        self._mode_seg.pack(fill="x", padx=8, pady=(8, 4))

        self._lossless_frame = ctk.CTkFrame(self)
        self._lossless_hint = ctk.CTkLabel(
            self._lossless_frame,
            text=(
                "Remux with stream copy (fast, same quality). Changes container only — "
                "not resolution or codec. MKV is the safest target."
            ),
            text_color="#888888",
            font=("", 10),
            justify="left",
            anchor="w",
            wraplength=360,
        )
        self._lossless_hint.pack(fill="x", padx=8, pady=(8, 4))
        self._lossless_container_menu = ctk.CTkOptionMenu(
            self._lossless_frame,
            variable=self.lossless_container_var,
            values=["MKV (recommended)", "Same as source"],
            height=28,
        )
        self._lossless_container_menu.pack(fill="x", padx=8, pady=(0, 10))

        # Custom: SeedVR-style scroll + 3 section cards (Preset / Video / Audio).
        self._custom_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._custom_scroll = ctk.CTkScrollableFrame(
            self._custom_frame,
            height=CUSTOM_SCROLL_HEIGHT,
            fg_color="transparent",
        )
        self._custom_scroll.pack(fill="both", expand=True, padx=(0, 2))

        # --- Section 1: Preset ---
        preset_card, preset_body = self._make_section(self._custom_scroll, "Preset")
        preset_card.pack(fill="x", pady=(0, 8))
        self._preset_menu = ctk.CTkOptionMenu(
            preset_body,
            variable=self.preset_var,
            values=self._preset_values,
            command=self.apply_preset,
            height=28,
        )
        self._preset_menu.pack(fill="x", pady=(0, 6))
        self._preset_info = self._make_info_box(preset_body)
        self._preset_info.pack(fill="x", pady=(0, 2))

        # --- Section 2: Video ---
        video_card, video_body = self._make_section(self._custom_scroll, "Video")
        video_card.pack(fill="x", pady=(0, 8))
        self._size_form = ctk.CTkFrame(video_body, fg_color="transparent")
        self._add_entry(self._size_form, "Width:", self.width_var)
        self._add_entry(self._size_form, "Height:", self.height_var)
        self._add_entry(self._size_form, "FPS:", self.fps_var)
        self._size_form.pack(fill="x", pady=(0, 4))

        self.supported_formats = [".mp4", ".avi", ".mkv", ".mov", ".webm"]
        self._format_row = ctk.CTkFrame(video_body, fg_color="transparent")
        ctk.CTkLabel(self._format_row, text="Format:", width=100, anchor="w").pack(
            side="left"
        )
        self._format_menu = ctk.CTkOptionMenu(
            self._format_row,
            variable=self.ext_var,
            values=self.supported_formats,
            height=28,
        )
        self._format_menu.pack(side="left", fill="x", expand=True)
        self._format_row.pack(fill="x", pady=(2, 4))

        q_row = ctk.CTkFrame(video_body, fg_color="transparent")
        ctk.CTkLabel(q_row, text="Video quality:", width=100, anchor="w").pack(
            side="left"
        )
        self._quality_menu = ctk.CTkOptionMenu(
            q_row,
            variable=self.video_quality_var,
            values=list(VIDEO_QUALITY_LEVELS),
            height=28,
            command=lambda _v: self._refresh_preset_info(),
        )
        self._quality_menu.pack(side="left", fill="x", expand=True)
        q_row.pack(fill="x", pady=(2, 2))

        # --- Section 3: Audio ---
        audio_card, audio_body = self._make_section(self._custom_scroll, "Audio")
        audio_card.pack(fill="x", pady=(0, 4))
        a_row = ctk.CTkFrame(audio_body, fg_color="transparent")
        ctk.CTkLabel(a_row, text="Audio bitrate:", width=100, anchor="w").pack(
            side="left"
        )
        self._audio_bitrate_menu = ctk.CTkOptionMenu(
            a_row,
            variable=self.audio_bitrate_var,
            values=list(AUDIO_BITRATE_LEVELS),
            height=28,
            command=lambda _v: self._refresh_audio_info(),
        )
        self._audio_bitrate_menu.pack(side="left", fill="x", expand=True)
        a_row.pack(fill="x", pady=(0, 4))
        self._audio_check = ctk.CTkCheckBox(
            audio_body, text="Include audio", variable=self.sound_var
        )
        self._audio_check.pack(anchor="w", pady=(2, 6))
        self._audio_info = self._make_info_box(audio_body)
        self._audio_info.pack(fill="x", pady=(0, 2))

        self._show_mode("Custom")
        self.apply_preset(self.preset_var.get())
        self._refresh_audio_info()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.transient(self.master)
        self.lift()
        self.focus_force()
        self._schedule_fit_window()

    def _make_section(self, parent, title: str):
        """SeedVR-style rounded card with title; returns (card, body)."""
        card = ctk.CTkFrame(
            parent,
            fg_color=_UI_SECTION_BG,
            corner_radius=8,
            border_width=1,
            border_color=_UI_SECTION_BORDER,
        )
        ctk.CTkLabel(
            card,
            text=title,
            text_color=_UI_SECTION_TITLE,
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=10, pady=(8, 0))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=10, pady=(4, 10))
        return card, body

    def _make_info_box(self, parent):
        """Dark rounded info panel (like SeedVR status/info)."""
        box = ctk.CTkFrame(
            parent,
            fg_color=_UI_INFO_BG,
            corner_radius=8,
            border_width=0,
        )
        label = ctk.CTkLabel(
            box,
            text="",
            text_color=_UI_INFO_TEXT,
            font=("", 10),
            justify="left",
            anchor="w",
            wraplength=340,
        )
        label.pack(fill="x", padx=10, pady=8)
        box._info_label = label  # type: ignore[attr-defined]
        return box

    def _set_info_text(self, box, text: str):
        label = getattr(box, "_info_label", None)
        if label is not None:
            label.configure(text=text)

    def _refresh_preset_info(self):
        name = self.preset_var.get()
        text = PRESET_INFO.get(name) or PRESET_INFO[PRESET_CUSTOM]
        if name == PRESET_CUSTOM:
            q = self.video_quality_var.get() or DEFAULT_VIDEO_QUALITY
            ext = self.ext_var.get() or ".mp4"
            text = (
                f"Manual · {ext} · video quality {q}\n"
                "Edit size, format, quality and audio below"
            )
        self._set_info_text(self._preset_info, text)

    def _refresh_audio_info(self):
        br = self.audio_bitrate_var.get() or DEFAULT_AUDIO_BITRATE
        self._set_info_text(
            self._audio_info, AUDIO_INFO.get(br, AUDIO_INFO[DEFAULT_AUDIO_BITRATE])
        )

    def _add_entry(self, frame, label, var):
        row = ctk.CTkFrame(frame, fg_color="transparent")
        row.pack(fill="x", pady=1)
        ctk.CTkLabel(row, text=label, width=100, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(row, textvariable=var, height=28)
        entry.pack(side="left", fill="x", expand=True)
        self._dim_entries.append(entry)
        return entry

    def _set_dim_fields_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        text_color = _ENTRY_TEXT_EDIT if enabled else _ENTRY_TEXT_LOCKED
        fg_color = _ENTRY_FG_EDIT if enabled else _ENTRY_FG_LOCKED
        for entry in self._dim_entries:
            try:
                entry.configure(
                    state=state,
                    text_color=text_color,
                    fg_color=fg_color,
                )
            except Exception:
                entry.configure(state=state)

    def _set_format_enabled(self, enabled: bool):
        self._format_menu.configure(state="normal" if enabled else "disabled")

    def _set_quality_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self._quality_menu.configure(state=state)
        self._audio_bitrate_menu.configure(state=state)

    def _on_mode_changed(self, mode: str | None = None):
        self._show_mode(mode or self.mode_var.get())
        self._schedule_fit_window()

    def _show_mode(self, mode: str):
        self._lossless_frame.pack_forget()
        self._custom_frame.pack_forget()
        if mode == "Lossless":
            self._lossless_frame.pack(fill="x", padx=8, pady=(0, 2))
        else:
            self._custom_frame.pack(fill="both", expand=True, padx=8, pady=(0, 2))
        try:
            self.update_idletasks()
        except Exception:
            pass

    def _schedule_fit_window(self):
        self._fit_token += 1
        token = self._fit_token
        try:
            self.after_idle(lambda t=token: self._fit_window(t))
            self.after(40, lambda t=token: self._fit_window(t))
            self.after(100, lambda t=token: self._fit_window(t))
        except Exception:
            self._fit_window(token)

    def _widget_req_h(self, widget, pad: int = 0) -> int:
        if widget is None:
            return 0
        try:
            if not widget.winfo_ismapped():
                return 0
            return int(widget.winfo_reqheight()) + int(pad)
        except Exception:
            return 0

    def _fit_window(self, token: int | None = None):
        """Lossless = tight content; Custom = chrome + fixed scroll viewport."""
        if token is not None and token != self._fit_token:
            return
        try:
            self.update_idletasks()
        except Exception:
            return

        mode = self.mode_var.get()
        chrome = 10
        for w, pad in (
            (self._source_lbl, 4),
            (self._src_frame, 6),
            (self._mode_seg, 6),
            (self._button_bar, 12),
        ):
            chrome += self._widget_req_h(w, pad)

        try:
            screen_h = int(self.winfo_screenheight())
            max_h = min(
                CONVERT_DIALOG_MAX_HEIGHT,
                max(400, screen_h - CONVERT_DIALOG_SCREEN_MARGIN * 2),
            )
        except Exception:
            max_h = CONVERT_DIALOG_MAX_HEIGHT

        if mode == "Lossless":
            body = self._widget_req_h(self._lossless_frame, 4) or 100
            h = min(chrome + body, 340)
            h = max(h, 280)
        else:
            h = chrome + CUSTOM_SCROLL_HEIGHT + 8
            h = max(520, min(h, max_h))

        width = CONVERT_DIALOG_WIDTH
        try:
            self.minsize(CONVERT_DIALOG_MIN_WIDTH, 260)
            screen_w = self.winfo_screenwidth()
            margin = CONVERT_DIALOG_SCREEN_MARGIN
            x = max(margin, (screen_w - width) // 2)
            y = max(margin, (self.winfo_screenheight() - h) // 2)
            geo = f"{int(width)}x{int(h)}+{int(x)}+{int(y)}"
            self.geometry(geo)
            self.after(30, lambda t=token, g=geo, hh=h: self._force_height(t, g, hh))
            self.after(90, lambda t=token, g=geo, hh=h: self._force_height(t, g, hh))
        except Exception:
            try:
                self.geometry(f"{CONVERT_DIALOG_WIDTH}x{h}")
            except Exception:
                pass

    def _force_height(self, token: int | None, geometry: str, h: int):
        if token is not None and token != self._fit_token:
            return
        try:
            self.minsize(CONVERT_DIALOG_MIN_WIDTH, 260)
            self.geometry(geometry)
            self.update_idletasks()
            if int(self.winfo_height()) > h + 16:
                self.geometry(geometry)
        except Exception:
            pass

    def _duration_summary(self):
        name = os.path.basename(self.video_path)
        try:
            duration = float(get_video_duration_mediainfo(self.video_path) or 0.0)
        except Exception:
            duration = 0.0
        bits = [name]
        if self._source_width and self._source_height:
            bits.append(f"{self._source_width}x{self._source_height}")
        if duration > 0:
            bits.append(f"~{duration:.1f}s")
        return " · ".join(bits)

    def _fill_source_dims(self):
        w, h, fps = self._source_width, self._source_height, self._source_fps
        self.width_var.set(str(w) if w else "")
        self.height_var.set(str(h) if h else "")
        if fps:
            self.fps_var.set(f"{fps:g}")
        else:
            self.fps_var.set("")

    def _quality_settings(self, *, from_custom_ui: bool) -> dict:
        if from_custom_ui:
            return {
                "video_quality": self.video_quality_var.get() or DEFAULT_VIDEO_QUALITY,
                "audio_bitrate": self.audio_bitrate_var.get() or DEFAULT_AUDIO_BITRATE,
            }
        return {
            "video_quality": DEFAULT_VIDEO_QUALITY,
            "audio_bitrate": DEFAULT_AUDIO_BITRATE,
        }

    def apply_preset(self, preset_name):
        """Named presets lock size/format/quality; only Custom… edits them."""
        is_custom = preset_name == PRESET_CUSTOM or preset_name not in self.presets
        if is_custom:
            self._set_dim_fields_enabled(True)
            self._set_format_enabled(True)
            self._set_quality_enabled(True)
            self._refresh_preset_info()
            self._refresh_audio_info()
            self._schedule_fit_window()
            return

        preset = self.presets[preset_name]
        self._set_dim_fields_enabled(True)
        self.ext_var.set(preset["ext"])
        if preset.get("keep_size"):
            self._fill_source_dims()
        else:
            self.width_var.set(str(preset["width"]))
            self.height_var.set(str(preset["height"]))
            self.fps_var.set(str(preset["fps"]))
        self.video_quality_var.set(DEFAULT_VIDEO_QUALITY)
        self.audio_bitrate_var.set(DEFAULT_AUDIO_BITRATE)
        self._set_dim_fields_enabled(False)
        self._set_format_enabled(False)
        self._set_quality_enabled(False)
        self._refresh_preset_info()
        self._refresh_audio_info()
        self._schedule_fit_window()

    def start_convert(self):
        is_lossless = self.mode_var.get() == "Lossless"

        try:
            if is_lossless:
                source_ext = (os.path.splitext(self.video_path)[1] or ".mp4").lower()
                container_choice = self.lossless_container_var.get()
                out_ext = ".mkv" if container_choice.startswith("MKV") else source_ext
                settings = {"mode": "original", "ext": out_ext}
            else:
                preset_name = self.preset_var.get()
                preset = self.presets.get(preset_name) if preset_name != PRESET_CUSTOM else None
                keep_size = bool(preset and preset.get("keep_size"))
                from_custom_ui = preset_name == PRESET_CUSTOM or preset is None
                quality = self._quality_settings(from_custom_ui=from_custom_ui)
                if keep_size:
                    if not self._source_width or not self._source_height:
                        raise ValueError(
                            "Could not read source resolution. Choose Custom… and enter size manually."
                        )
                    settings = {
                        "mode": "custom",
                        "ext": self.ext_var.get(),
                        "keep_size": True,
                        "include_audio": bool(self.sound_var.get()),
                        **quality,
                    }
                else:
                    settings = {
                        "mode": "custom",
                        "ext": self.ext_var.get(),
                        "width": int(self.width_var.get()),
                        "height": int(self.height_var.get()),
                        "fps": float(self.fps_var.get()),
                        "include_audio": bool(self.sound_var.get()),
                        **quality,
                    }
                    if settings["width"] <= 0 or settings["height"] <= 0 or settings["fps"] <= 0:
                        raise ValueError("Width, height, and FPS must be positive.")
        except Exception as e:
            messagebox.showerror("Invalid input", str(e), parent=self)
            return

        target_ext = settings["ext"]
        if settings.get("mode") == "original":
            src_ext = (os.path.splitext(self.video_path)[1] or "").lower()
            if src_ext in _FRAGILE_LOSSLESS_CONTAINERS and target_ext == src_ext:
                choice = messagebox.askyesnocancel(
                    "Container compatibility warning",
                    (
                        f"Saving a lossless remux as {src_ext} often produces a file "
                        f"that won't play (no readable video stream).\n\n"
                        f"MKV is a safer container that keeps the original quality.\n\n"
                        f"  Yes  -  Save as MKV (recommended)\n"
                        f"  No   -  Save as {src_ext} anyway\n"
                        f"  Cancel  -  Abort"
                    ),
                    parent=self,
                )
                if choice is None:
                    return
                if choice:
                    target_ext = ".mkv"
                    settings["ext"] = ".mkv"

        base = os.path.splitext(os.path.basename(self.video_path))[0]
        suffix = "_remux" if settings.get("mode") == "original" else "_converted"
        initial_dir = os.path.dirname(self.video_path) or None
        save_path = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=target_ext,
            initialdir=initial_dir,
            initialfile=f"{base}{suffix}{target_ext}",
            filetypes=[(f"{target_ext.upper()} files", f"*{target_ext}")],
        )
        if not save_path:
            return

        input_path = self.video_path
        controller = self.controller
        root = self.master

        def run():
            _convert_worker(input_path, save_path, settings, controller, root)

        threading.Thread(target=run, daemon=True, name="video-convert").start()


def _show_convert_done(controller, root, save_path):
    """Refresh/select in grid when possible, then offer Play."""
    if controller is not None and hasattr(controller, "reveal_merged_file"):
        try:
            controller.reveal_merged_file(save_path)
        except Exception:
            logging.exception("[Convert] reveal_merged_file failed")

    play = messagebox.askyesno(
        "Done",
        f"Converted video saved to:\n{save_path}\n\nPlay it now?",
        parent=root,
    )
    if play and controller is not None and hasattr(controller, "open_video_player"):
        try:
            controller.open_video_player(save_path, os.path.basename(save_path))
        except Exception:
            logging.exception("[Convert] open_video_player failed")


def _convert_lossless(input_path, save_path, set_status):
    ffmpeg_bin = get_ffmpeg_path()
    video_name = os.path.basename(input_path)

    vinfo = probe_first_video_stream(input_path)
    codec_name = ((vinfo or {}).get("codec_name") or "").lower()
    if not codec_name:
        raise RuntimeError(
            "Lossless convert needs a video stream, but none was detected in this file."
        )

    try:
        total_seconds = float(get_video_duration_mediainfo(input_path) or 0.0)
    except Exception:
        total_seconds = 0.0

    target_ext = (os.path.splitext(save_path)[1] or "").lower()
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-nostdin",
        "-fflags",
        "+genpts+igndts",
        "-i",
        input_path,
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
    if target_ext in (".mp4", ".mov", ".m4v", ".m4a"):
        if codec_name in ("hevc", "h265"):
            cmd += ["-tag:v", "hvc1"]
        cmd += ["-movflags", "+faststart"]
    if codec_name == "vp9" and target_ext == ".mp4":
        logging.warning(
            "[Convert][Lossless] VP9 in MP4 has poor player support; MKV is recommended."
        )
    cmd += ["-progress", "pipe:1", save_path]

    _run_ffmpeg_with_progress(
        cmd,
        save_path,
        set_status,
        total_seconds,
        f"Converting (lossless): {video_name}",
    )


def _convert_custom(input_path, save_path, settings, set_status):
    ffmpeg_bin = get_ffmpeg_path()
    video_name = os.path.basename(input_path)
    want_audio = bool(settings.get("include_audio", True))
    include_audio = want_audio and _has_audio_stream(input_path)
    if want_audio and not include_audio:
        logging.info("[Convert] Source has no audio; converting video only.")

    try:
        total_seconds = float(get_video_duration_mediainfo(input_path) or 0.0)
    except Exception:
        total_seconds = 0.0

    # Original size: re-encode only (no scale / fps filter) so resolution & rate stay native.
    if settings.get("keep_size"):
        vf = "format=yuv420p"
    else:
        vf = _custom_video_filter(settings)

    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-nostdin",
        "-i",
        input_path,
        "-vf",
        vf,
        "-map",
        "0:v:0",
        *_custom_codec_args(save_path, settings),
    ]
    if include_audio:
        cmd += ["-map", "0:a?", *_custom_audio_args(save_path, settings)]
    else:
        cmd += ["-an"]
    cmd += ["-progress", "pipe:1", save_path]

    _run_ffmpeg_with_progress(
        cmd,
        save_path,
        set_status,
        total_seconds,
        f"Converting: {video_name}",
    )


def _convert_worker(input_path, save_path, settings, controller, root):
    def set_status(msg):
        _set_status(controller, root, msg)

    try:
        if settings.get("mode") == "original":
            _convert_lossless(input_path, save_path, set_status)
        else:
            _convert_custom(input_path, save_path, settings, set_status)

        set_status(f"Convert complete: {os.path.basename(save_path)}")
        _clear_status_later(controller, root)
        _ui_call(
            root,
            lambda p=save_path: _show_convert_done(controller, root, p),
        )
    except Exception as e:
        logging.error("[Convert] Error: %s", e)
        set_status(f"Convert failed: {e}")
        hint = ""
        if settings.get("mode") == "original":
            hint = (
                "\n\nTip: If remux fails, try the Custom tab to re-encode, "
                "or choose MKV as the container."
            )
        _ui_call(
            root,
            lambda err=str(e), h=hint: messagebox.showerror("Convert Error", f"{err}{h}"),
        )
