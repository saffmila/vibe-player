"""
Convert a whole video file (library RMB → Convert Video…).

Uses shared encode settings UI (video_encode_settings). No cuts or loops —
the entire source is written to a new file. Multi-select opens a batch queue
(same settings applied to each file, sequential).
"""

from __future__ import annotations

import copy
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
from video_encode_settings import CUSTOM_SCROLL_HEIGHT, VideoEncodeSettingsPanel
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

_convert_batch_lock = threading.Lock()
_convert_batch_running = False


def open_convert_video_dialog(parent, video_paths, controller=None):
    """Open the convert dialog for one or more video paths."""
    if isinstance(video_paths, (str, os.PathLike)):
        paths = [os.path.normpath(str(video_paths))]
    else:
        paths = []
        seen = set()
        for p in video_paths or []:
            if not p:
                continue
            p = os.path.normpath(str(p))
            key = os.path.normcase(p)
            if key in seen:
                continue
            if os.path.isfile(p):
                seen.add(key)
                paths.append(p)
    if not paths:
        messagebox.showinfo("Convert Video", "Select a video file to convert.")
        return None
    return VideoConvertDialog(parent, paths, controller=controller)


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


def _unique_output_path(
    input_path: str,
    ext: str,
    suffix: str,
    output_dir: str | None = None,
) -> str:
    """Build ``name{suffix}{ext}`` in ``output_dir`` (or next to source); add _2, _3… if taken."""
    folder = output_dir or os.path.dirname(input_path) or "."
    base = os.path.splitext(os.path.basename(input_path))[0]
    ext = ext if ext.startswith(".") else f".{ext}"
    candidate = os.path.join(folder, f"{base}{suffix}{ext}")
    if not os.path.exists(candidate):
        return candidate
    n = 2
    while True:
        candidate = os.path.join(folder, f"{base}{suffix}_{n}{ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _settings_for_input(base_settings: dict, input_path: str) -> dict:
    """Per-file settings copy (lossless same-as-source ext; keep_size needs no dims)."""
    settings = copy.deepcopy(base_settings)
    if settings.get("mode") == "original" and settings.get("ext") == "__source__":
        settings["ext"] = (os.path.splitext(input_path)[1] or ".mp4").lower()
    return settings


class VideoConvertDialog(ctk.CTkToplevel):
    """Lossless remux or Custom re-encode of one or more whole video files."""

    def __init__(self, parent, video_paths, controller=None):
        super().__init__(parent)
        if isinstance(video_paths, (str, os.PathLike)):
            video_paths = [video_paths]
        self.video_paths = [os.path.normpath(p) for p in video_paths if p]
        self.video_path = self.video_paths[0]
        self.is_batch = len(self.video_paths) > 1
        self.controller = controller
        self.resizable(True, True)

        if self.is_batch:
            self.title(f"Convert Video — Batch ({len(self.video_paths)})")
        else:
            self.title("Convert Video")

        self._source_width, self._source_height, self._source_fps = _probe_source_props(
            self.video_path
        )
        self.lossless_container_var = ctk.StringVar(value="MKV (recommended)")
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
        start_label = "Start Batch" if self.is_batch else "Start Convert"
        ctk.CTkButton(
            self._button_bar, text=start_label, height=28, command=self.start_convert
        ).pack(side="right", fill="x", expand=True, padx=(10, 0))

        if self.is_batch:
            header = f"Convert Video — Batch Mode ({len(self.video_paths)} videos)"
            subtitle = f"{len(self.video_paths)} videos selected"
        else:
            header = "Source"
            subtitle = os.path.basename(self.video_path)

        self._source_lbl = ctk.CTkLabel(self, text=header, text_color="#00bfff")
        self._source_lbl.pack(pady=(8, 0))
        self._src_frame = ctk.CTkFrame(self)
        self._src_frame.pack(pady=4, padx=8, fill="x")
        ctk.CTkLabel(
            self._src_frame,
            text=subtitle,
            anchor="w",
            justify="left",
            wraplength=360,
            text_color=("#666666", "#aaaaaa") if self.is_batch else None,
        ).pack(fill="x", padx=8, pady=(6, 2 if self.is_batch else 6))
        if self.is_batch:
            preview_names = [os.path.basename(p) for p in self.video_paths[:3]]
            preview = "\n".join(preview_names)
            ctk.CTkLabel(
                self._src_frame,
                text=preview,
                anchor="w",
                justify="left",
                wraplength=360,
                font=("", 10),
            ).pack(fill="x", padx=8, pady=(0, 2))
            extra = len(self.video_paths) - 3
            if extra > 0:
                more_lbl = ctk.CTkLabel(
                    self._src_frame,
                    text=f"… and {extra} more",
                    text_color="#5dade2",
                    font=ctk.CTkFont(size=10, underline=True),
                    anchor="w",
                    cursor="hand2",
                )
                more_lbl.pack(fill="x", padx=8, pady=(0, 6))
                more_lbl.bind("<Button-1>", lambda _e: self._show_batch_file_list())
            else:
                # Keep padding consistent when everything fits in the preview.
                ctk.CTkFrame(self._src_frame, fg_color="transparent", height=4).pack()

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

        self._custom_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._encode_panel = VideoEncodeSettingsPanel(
            self._custom_frame,
            source_width=self._source_width,
            source_height=self._source_height,
            source_fps=self._source_fps,
            scroll_height=CUSTOM_SCROLL_HEIGHT,
        )
        self._encode_panel.pack(fill="both", expand=True)

        self._show_mode("Custom")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.transient(self.master)
        self.lift()
        self.focus_force()
        self._schedule_fit_window()

    def _show_batch_file_list(self):
        """Open a small window with the full batch file list (no scrollbar in main dialog)."""
        win = ctk.CTkToplevel(self)
        win.title(f"Batch queue ({len(self.video_paths)})")
        win.transient(self)
        win.geometry("420x320")
        win.minsize(320, 200)
        ctk.CTkLabel(
            win,
            text=f"{len(self.video_paths)} videos selected",
            text_color="#00bfff",
        ).pack(pady=(10, 4), padx=10, anchor="w")
        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        for path in self.video_paths:
            ctk.CTkLabel(
                scroll,
                text=os.path.basename(path),
                anchor="w",
                justify="left",
                wraplength=360,
                font=("", 11),
            ).pack(fill="x", pady=1)
        ctk.CTkButton(win, text="Close", height=28, command=win.destroy).pack(
            fill="x", padx=10, pady=(0, 10)
        )
        win.lift()
        win.focus_force()

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
            h = min(chrome + body, 420 if self.is_batch else 340)
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
        if self.is_batch:
            return f"{len(self.video_paths)} videos queued"
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

    def start_convert(self):
        global _convert_batch_running
        is_lossless = self.mode_var.get() == "Lossless"

        try:
            if is_lossless:
                container_choice = self.lossless_container_var.get()
                if container_choice.startswith("MKV"):
                    out_ext = ".mkv"
                else:
                    # Per-file source extension resolved in _settings_for_input.
                    out_ext = "__source__"
                settings = {"mode": "original", "ext": out_ext}
            else:
                settings = self._encode_panel.get_custom_settings()
        except Exception as e:
            messagebox.showerror("Invalid input", str(e), parent=self)
            return

        # Fragile MPEG-TS-style remux warning (once for the batch).
        if settings.get("mode") == "original" and settings.get("ext") == "__source__":
            fragile = [
                p
                for p in self.video_paths
                if (os.path.splitext(p)[1] or "").lower() in _FRAGILE_LOSSLESS_CONTAINERS
            ]
            if fragile:
                sample = os.path.splitext(fragile[0])[1].lower()
                choice = messagebox.askyesnocancel(
                    "Container compatibility warning",
                    (
                        f"{len(fragile)} file(s) use a fragile container "
                        f"(e.g. {sample}). Lossless remux to the same type often "
                        f"won't play.\n\n"
                        f"MKV is safer and keeps original quality.\n\n"
                        f"  Yes  -  Save fragile files as MKV\n"
                        f"  No   -  Keep original containers anyway\n"
                        f"  Cancel  -  Abort"
                    ),
                    parent=self,
                )
                if choice is None:
                    return
                if choice:
                    settings["fragile_force_mkv"] = True

        suffix = "_remux" if settings.get("mode") == "original" else "_converted"
        controller = self.controller
        root = self.master

        if not self.is_batch:
            path = self.video_paths[0]
            file_settings = _settings_for_input(settings, path)
            if file_settings.get("fragile_force_mkv") and (
                os.path.splitext(path)[1] or ""
            ).lower() in _FRAGILE_LOSSLESS_CONTAINERS:
                file_settings["ext"] = ".mkv"
            target_ext = file_settings["ext"]
            base = os.path.splitext(os.path.basename(path))[0]
            initial_dir = os.path.dirname(path) or None
            save_path = filedialog.asksaveasfilename(
                parent=self,
                defaultextension=target_ext,
                initialdir=initial_dir,
                initialfile=f"{base}{suffix}{target_ext}",
                filetypes=[(f"{target_ext.upper()} files", f"*{target_ext}")],
            )
            if not save_path:
                return

            def run_one():
                _convert_worker(path, save_path, file_settings, controller, root)

            threading.Thread(target=run_one, daemon=True, name="video-convert").start()
            return

        with _convert_batch_lock:
            already_running = _convert_batch_running
        if already_running:
            messagebox.showinfo(
                "Convert Video",
                "A convert batch is already running. Wait for it to finish.",
                parent=self,
            )
            return

        initial_dir = os.path.dirname(self.video_paths[0]) or None
        out_dir = filedialog.askdirectory(
            parent=self,
            initialdir=initial_dir,
            title="Choose output folder for converted videos",
        )
        if not out_dir:
            return
        out_dir = os.path.normpath(out_dir)
        if not os.path.isdir(out_dir):
            messagebox.showerror(
                "Convert Video",
                f"Output folder does not exist:\n{out_dir}",
                parent=self,
            )
            return

        with _convert_batch_lock:
            if _convert_batch_running:
                messagebox.showinfo(
                    "Convert Video",
                    "A convert batch is already running. Wait for it to finish.",
                    parent=self,
                )
                return
            _convert_batch_running = True

        jobs = []
        for path in self.video_paths:
            file_settings = _settings_for_input(settings, path)
            if file_settings.get("fragile_force_mkv") and (
                os.path.splitext(path)[1] or ""
            ).lower() in _FRAGILE_LOSSLESS_CONTAINERS:
                file_settings["ext"] = ".mkv"
            file_settings.pop("fragile_force_mkv", None)
            save_path = _unique_output_path(
                path, file_settings["ext"], suffix, output_dir=out_dir
            )
            jobs.append((path, save_path, file_settings))

        self.status_var.set(f"Batch started ({len(jobs)} → {out_dir})…")

        def run_batch():
            _convert_batch_worker(jobs, controller, root, output_dir=out_dir)

        threading.Thread(target=run_batch, daemon=True, name="video-convert-batch").start()


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


def _show_batch_done(
    controller,
    root,
    ok_paths: list[str],
    errors: list[tuple[str, str]],
    output_dir: str | None = None,
):
    if ok_paths and controller is not None and hasattr(controller, "reveal_merged_file"):
        try:
            controller.reveal_merged_file(ok_paths[-1])
        except Exception:
            logging.exception("[Convert] reveal_merged_file failed")

    lines = [f"Convert batch finished: {len(ok_paths)} ok, {len(errors)} failed."]
    if output_dir:
        lines.append("")
        lines.append(f"Output folder:\n{output_dir}")
    if errors:
        lines.append("")
        for name, err in errors[:8]:
            lines.append(f"• {name}: {err}")
        if len(errors) > 8:
            lines.append(f"… and {len(errors) - 8} more")
    messagebox.showinfo("Convert Batch", "\n".join(lines), parent=root)


def _convert_lossless(input_path, save_path, set_status, progress_prefix: str = ""):
    ffmpeg_bin = get_ffmpeg_path()
    video_name = os.path.basename(input_path)
    label = f"{progress_prefix}Converting (lossless): {video_name}"

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

    _run_ffmpeg_with_progress(cmd, save_path, set_status, total_seconds, label)


def _convert_custom(input_path, save_path, settings, set_status, progress_prefix: str = ""):
    ffmpeg_bin = get_ffmpeg_path()
    video_name = os.path.basename(input_path)
    label = f"{progress_prefix}Converting: {video_name}"
    want_audio = bool(settings.get("include_audio", True))
    include_audio = want_audio and _has_audio_stream(input_path)
    if want_audio and not include_audio:
        logging.info("[Convert] Source has no audio; converting video only.")

    try:
        total_seconds = float(get_video_duration_mediainfo(input_path) or 0.0)
    except Exception:
        total_seconds = 0.0

    # keep_size: native resolution/fps; otherwise scale from settings.
    if settings.get("keep_size"):
        w, h, _fps = _probe_source_props(input_path)
        if not w or not h:
            raise RuntimeError(
                "Could not read source resolution for original-size convert."
            )

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
        _custom_video_filter(settings),
        "-map",
        "0:v:0",
        *_custom_codec_args(save_path, settings),
    ]
    if include_audio:
        cmd += ["-map", "0:a?", *_custom_audio_args(save_path, settings)]
    else:
        cmd += ["-an"]
    cmd += ["-progress", "pipe:1", save_path]

    _run_ffmpeg_with_progress(cmd, save_path, set_status, total_seconds, label)


def _run_one_convert(input_path, save_path, settings, set_status, progress_prefix: str = ""):
    if settings.get("mode") == "original":
        _convert_lossless(input_path, save_path, set_status, progress_prefix)
    else:
        _convert_custom(input_path, save_path, settings, set_status, progress_prefix)


def _convert_worker(input_path, save_path, settings, controller, root):
    def set_status(msg):
        _set_status(controller, root, msg)

    try:
        _run_one_convert(input_path, save_path, settings, set_status)
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
                "\n\nTip: If remux fails, try Custom to re-encode, "
                "or choose MKV as the container."
            )
        _ui_call(
            root,
            lambda err=str(e), h=hint: messagebox.showerror("Convert Error", f"{err}{h}"),
        )


def _convert_batch_worker(jobs, controller, root, output_dir: str | None = None):
    global _convert_batch_running

    def set_status(msg):
        _set_status(controller, root, msg)

    ok_paths: list[str] = []
    errors: list[tuple[str, str]] = []
    total = len(jobs)

    try:
        for idx, (input_path, save_path, settings) in enumerate(jobs, start=1):
            prefix = f"[{idx}/{total}] "
            name = os.path.basename(input_path)
            try:
                set_status(f"{prefix}Starting: {name}")
                _run_one_convert(input_path, save_path, settings, set_status, prefix)
                ok_paths.append(save_path)
            except Exception as e:
                logging.error("[Convert][Batch] %s failed: %s", name, e)
                errors.append((name, str(e)))
                set_status(f"{prefix}Failed: {name}")

        summary = f"Convert batch: {len(ok_paths)} ok, {len(errors)} failed"
        set_status(summary)
        _clear_status_later(controller, root)
        _ui_call(
            root,
            lambda ok=list(ok_paths), err=list(errors), od=output_dir: _show_batch_done(
                controller, root, ok, err, output_dir=od
            ),
        )
    finally:
        with _convert_batch_lock:
            _convert_batch_running = False
