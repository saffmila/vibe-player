"""
Merge several whole videos into one file (library multi-select → RMB).

UI mirrors the export dialog (Lossless / Custom). FFmpeg does the work:
lossless uses concat demuxer + stream copy; custom re-encodes to a common size/fps.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from file_operations import (
    get_ffmpeg_path,
    get_ffprobe_path,
    get_video_duration_mediainfo,
    probe_first_video_stream,
)

_SUBPROCESS_STARTUPINFO = None
if os.name == "nt":
    _SUBPROCESS_STARTUPINFO = subprocess.STARTUPINFO()
    _SUBPROCESS_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _SUBPROCESS_STARTUPINFO.wShowWindow = subprocess.SW_HIDE

MERGE_DIALOG_WIDTH = 400
MERGE_DIALOG_HEIGHT = 520
MERGE_DIALOG_MIN_WIDTH = 360
MERGE_DIALOG_MIN_HEIGHT = 460
MERGE_DIALOG_SCREEN_MARGIN = 24


def open_merge_videos_dialog(parent, video_paths, controller=None):
    """Open the merge dialog for the given video paths (selection order)."""
    paths = [os.path.normpath(p) for p in (video_paths or []) if p and os.path.isfile(p)]
    if len(paths) < 2:
        messagebox.showinfo("Merge Videos", "Select at least two video files to merge.")
        return None
    return VideoMergeDialog(parent, paths, controller=controller)


class VideoMergeDialog(ctk.CTkToplevel):
    """Lossless / Custom merge of multiple whole video files."""

    def __init__(self, parent, video_paths, controller=None):
        super().__init__(parent)
        self.title("Merge Videos")
        self.controller = controller
        self.video_paths = list(video_paths)
        self._target_geometry = (MERGE_DIALOG_WIDTH, MERGE_DIALOG_HEIGHT)
        self.resizable(True, True)

        self.presets = {
            "MP4 1600x1200 HQ": {"ext": ".mp4", "width": 1600, "height": 1200, "fps": 30},
            "MP4 1280x720": {"ext": ".mp4", "width": 1280, "height": 720, "fps": 30},
            "AVI 640x480": {"ext": ".avi", "width": 640, "height": 480, "fps": 25},
        }
        self.preset_var = ctk.StringVar(value=list(self.presets.keys())[0])
        self.ext_var = ctk.StringVar(value=".mp4")
        self.width_var = ctk.StringVar(value="1600")
        self.height_var = ctk.StringVar(value="1200")
        self.fps_var = ctk.StringVar(value="30")
        self.sound_var = ctk.BooleanVar(value=True)
        self.lossless_container_var = ctk.StringVar(value="MKV (recommended)")

        button_bar = ctk.CTkFrame(self, fg_color="transparent")
        button_bar.pack(side="bottom", fill="x", padx=8, pady=8)
        self.status_var = ctk.StringVar(value=self._duration_summary())
        ctk.CTkLabel(
            button_bar,
            textvariable=self.status_var,
            text_color="#bfc7d5",
            font=("", 10),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))
        ctk.CTkButton(button_bar, text="Close", width=90, height=28, command=self.destroy).pack(
            side="left"
        )
        ctk.CTkButton(
            button_bar, text="Start Merge", height=28, command=self.start_merge
        ).pack(side="right", fill="x", expand=True, padx=(10, 0))

        ctk.CTkLabel(self, text="Merge order", text_color="#00bfff").pack(pady=(8, 0))
        list_frame = ctk.CTkFrame(self)
        list_frame.pack(pady=4, padx=8, fill="both", expand=False)

        self.listbox = tk.Listbox(
            list_frame,
            height=min(8, max(3, len(self.video_paths))),
            activestyle="dotbox",
            exportselection=False,
            bg="#2b2b2b",
            fg="#e8e8e8",
            selectbackground="#1f6aa5",
            highlightthickness=0,
            borderwidth=0,
        )
        self.listbox.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        for path in self.video_paths:
            self.listbox.insert(tk.END, os.path.basename(path))

        order_btns = ctk.CTkFrame(list_frame, fg_color="transparent")
        order_btns.pack(side="right", padx=6, pady=6)
        ctk.CTkButton(order_btns, text="▲", width=36, height=28, command=self._move_up).pack(
            pady=(0, 4)
        )
        ctk.CTkButton(order_btns, text="▼", width=36, height=28, command=self._move_down).pack()

        self.tabs = ctk.CTkTabview(self, height=220)
        self.tabs.pack(pady=(6, 2), padx=8, fill="both", expand=True)
        lossless_tab = self.tabs.add("Lossless")
        custom_tab = self.tabs.add("Custom")

        ctk.CTkLabel(
            lossless_tab,
            text=(
                "Joins files with stream copy (fast). Works best when all videos "
                "share the same codec, resolution, and frame rate. MKV is safest."
            ),
            text_color="#888888",
            font=("", 10),
            justify="left",
            anchor="w",
            wraplength=340,
        ).pack(fill="x", padx=8, pady=(6, 4))
        ctk.CTkOptionMenu(
            lossless_tab,
            variable=self.lossless_container_var,
            values=["MKV (recommended)", "Same as first file"],
            height=28,
        ).pack(fill="x", padx=8, pady=(0, 6))

        ctk.CTkLabel(custom_tab, text="Choose preset:").pack(pady=(6, 3))
        ctk.CTkOptionMenu(
            custom_tab,
            variable=self.preset_var,
            values=list(self.presets.keys()),
            command=self.apply_preset,
            height=28,
        ).pack(pady=(0, 4))

        form_frame = ctk.CTkFrame(custom_tab)
        form_frame.pack(pady=4, padx=8, fill="x")
        self._add_entry(form_frame, "Width:", self.width_var)
        self._add_entry(form_frame, "Height:", self.height_var)
        self._add_entry(form_frame, "FPS:", self.fps_var)

        self.supported_formats = [".mp4", ".avi", ".mkv", ".mov", ".webm"]
        ctk.CTkLabel(custom_tab, text="Output Format:").pack(pady=(5, 2))
        ctk.CTkOptionMenu(
            custom_tab,
            variable=self.ext_var,
            values=self.supported_formats,
            height=28,
        ).pack(pady=(0, 5))
        ctk.CTkCheckBox(custom_tab, text="Include audio", variable=self.sound_var).pack(
            pady=(0, 5)
        )

        self.tabs.set("Lossless")
        self.apply_preset(self.preset_var.get())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.transient(self.master)
        self.lift()
        self.focus_force()
        self._apply_compact_geometry()
        self.after(100, self._apply_compact_geometry)

    def _add_entry(self, frame, label, var):
        row = ctk.CTkFrame(frame)
        row.pack(fill="x", pady=1)
        ctk.CTkLabel(row, text=label, width=80, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(row, textvariable=var, height=28)
        entry.pack(side="left", fill="x", expand=True)
        return entry

    def _duration_summary(self):
        total = 0.0
        for path in self.video_paths:
            try:
                total += float(get_video_duration_mediainfo(path) or 0.0)
            except Exception:
                pass
        n = len(self.video_paths)
        if total > 0:
            return f"{n} videos · ~{total:.1f}s total"
        return f"{n} videos"

    def apply_preset(self, preset_name):
        preset = self.presets[preset_name]
        self.ext_var.set(preset["ext"])
        self.width_var.set(str(preset["width"]))
        self.height_var.set(str(preset["height"]))
        self.fps_var.set(str(preset["fps"]))

    def _selected_index(self):
        sel = self.listbox.curselection()
        return int(sel[0]) if sel else None

    def _refresh_listbox(self, select_index=None):
        self.listbox.delete(0, tk.END)
        for path in self.video_paths:
            self.listbox.insert(tk.END, os.path.basename(path))
        if select_index is not None and 0 <= select_index < len(self.video_paths):
            self.listbox.selection_set(select_index)
            self.listbox.activate(select_index)
        self.status_var.set(self._duration_summary())

    def _move_up(self):
        idx = self._selected_index()
        if idx is None or idx <= 0:
            return
        self.video_paths[idx - 1], self.video_paths[idx] = (
            self.video_paths[idx],
            self.video_paths[idx - 1],
        )
        self._refresh_listbox(idx - 1)

    def _move_down(self):
        idx = self._selected_index()
        if idx is None or idx >= len(self.video_paths) - 1:
            return
        self.video_paths[idx + 1], self.video_paths[idx] = (
            self.video_paths[idx],
            self.video_paths[idx + 1],
        )
        self._refresh_listbox(idx + 1)

    def _apply_compact_geometry(self):
        try:
            width, height = self._target_geometry
            self.update_idletasks()
            margin = MERGE_DIALOG_SCREEN_MARGIN
            screen_w = self.winfo_screenwidth()
            screen_h = self.winfo_screenheight()
            max_w = max(MERGE_DIALOG_MIN_WIDTH, screen_w - margin * 2)
            max_h = max(MERGE_DIALOG_MIN_HEIGHT, screen_h - margin * 2)
            width = min(width, max_w)
            height = min(height, max_h)
            self.minsize(min(MERGE_DIALOG_MIN_WIDTH, width), min(MERGE_DIALOG_MIN_HEIGHT, height))
            x = max(margin, (screen_w - width) // 2)
            y = max(margin, (screen_h - height) // 2)
            self.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
        except Exception:
            self.geometry(f"{MERGE_DIALOG_WIDTH}x{MERGE_DIALOG_HEIGHT}")

    def start_merge(self):
        if len(self.video_paths) < 2:
            messagebox.showerror("Merge Videos", "Need at least two videos.")
            return

        selected_tab = self.tabs.get() if hasattr(self, "tabs") else "Lossless"
        is_lossless = selected_tab == "Lossless"

        try:
            if is_lossless:
                first_ext = (os.path.splitext(self.video_paths[0])[1] or ".mp4").lower()
                container_choice = self.lossless_container_var.get()
                out_ext = ".mkv" if container_choice.startswith("MKV") else first_ext
                settings = {"mode": "original", "ext": out_ext}
            else:
                settings = {
                    "mode": "custom",
                    "ext": self.ext_var.get(),
                    "width": int(self.width_var.get()),
                    "height": int(self.height_var.get()),
                    "fps": float(self.fps_var.get()),
                    "include_audio": bool(self.sound_var.get()),
                }
                if settings["width"] <= 0 or settings["height"] <= 0 or settings["fps"] <= 0:
                    raise ValueError("Width, height, and FPS must be positive.")
        except Exception as e:
            messagebox.showerror("Invalid input", str(e))
            return

        target_ext = settings["ext"]
        base = os.path.splitext(os.path.basename(self.video_paths[0]))[0]
        initial_dir = os.path.dirname(self.video_paths[0]) or None
        save_path = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=target_ext,
            initialdir=initial_dir,
            initialfile=f"{base}_merged{target_ext}",
            filetypes=[(f"{target_ext.upper()} files", f"*{target_ext}")],
        )
        if not save_path:
            return

        paths = list(self.video_paths)
        controller = self.controller
        root = self.master

        def run():
            _merge_worker(paths, save_path, settings, controller, root)

        threading.Thread(target=run, daemon=True).start()


def _ui_call(root, fn):
    if root is not None:
        try:
            root.after(0, fn)
            return
        except Exception:
            pass
    fn()


def _set_status(controller, root, msg):
    status_bar = getattr(controller, "status_bar", None) if controller else None
    if not status_bar:
        return

    def apply():
        try:
            status_bar.set_action_message(msg)
        except Exception:
            pass

    _ui_call(root, apply)


def _clear_status_later(controller, root, delay_ms=5000):
    status_bar = getattr(controller, "status_bar", None) if controller else None
    if not status_bar or root is None:
        return

    def clear():
        try:
            status_bar.clear_action_message()
        except Exception:
            pass

    try:
        root.after(delay_ms, clear)
    except Exception:
        pass


def _has_audio_stream(path):
    try:
        ffprobe_bin = get_ffprobe_path()
    except FileNotFoundError:
        return False
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "json",
        path,
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            startupinfo=_SUBPROCESS_STARTUPINFO,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False
        data = json.loads(result.stdout)
        return bool(data.get("streams"))
    except Exception:
        return False


def _custom_video_filter(settings):
    width = int(settings["width"])
    height = int(settings["height"])
    fps = float(settings["fps"])
    return f"scale={width}:{height}:flags=lanczos,fps={fps:g},format=yuv420p"


def _custom_codec_args(save_path, settings):
    target_ext = (os.path.splitext(save_path)[1] or settings["ext"]).lower()
    if target_ext == ".webm":
        return ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-pix_fmt", "yuv420p"]
    if target_ext == ".avi":
        return ["-c:v", "mpeg4", "-q:v", "3", "-pix_fmt", "yuv420p"]
    args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
    if target_ext in (".mp4", ".mov", ".m4v"):
        args += ["-movflags", "+faststart"]
    return args


def _custom_audio_args(save_path, settings):
    target_ext = (os.path.splitext(save_path)[1] or settings["ext"]).lower()
    if target_ext == ".webm":
        return ["-c:a", "libopus", "-b:a", "160k"]
    if target_ext == ".avi":
        return ["-c:a", "libmp3lame", "-b:a", "192k"]
    return ["-c:a", "aac", "-b:a", "192k"]


def _total_duration(paths):
    total = 0.0
    for path in paths:
        try:
            total += float(get_video_duration_mediainfo(path) or 0.0)
        except Exception:
            pass
    return total


def _run_ffmpeg_with_progress(cmd, save_path, set_status, total_seconds, label):
    proc = None
    stderr_thread = None
    stderr_lines: list[str] = []
    try:
        logging.info("[Merge] %s", " ".join(cmd))
        set_status(f"{label}  0%")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            startupinfo=_SUBPROCESS_STARTUPINFO,
        )

        def _drain_stderr(pipe, sink):
            try:
                for line in pipe:
                    sink.append(line.rstrip("\r\n"))
            except Exception:
                pass

        if proc.stderr is not None:
            stderr_thread = threading.Thread(
                target=_drain_stderr, args=(proc.stderr, stderr_lines), daemon=True
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
            if m and total_seconds > 0:
                out_us = int(m.group(1))
                if out_us < 0:
                    continue
                pct = int((out_us / 1_000_000.0) / total_seconds * 100)
                pct = max(0, min(100, pct))
                if pct != last_pct:
                    last_pct = pct
                    set_status(f"{label}  {pct}%")
            elif line == "progress=end":
                break

        return_code = proc.wait()
        if stderr_thread is not None:
            stderr_thread.join(timeout=5)

        if stderr_lines:
            logging.warning(
                "[Merge] FFmpeg stderr (%d lines):\n%s",
                len(stderr_lines),
                "\n".join(stderr_lines[-50:]),
            )

        if return_code != 0:
            detail = "\n".join(stderr_lines[-12:]) or f"FFmpeg exited with code {return_code}"
            raise RuntimeError(detail)

        out_size = os.path.getsize(save_path) if os.path.isfile(save_path) else 0
        if out_size < 1024:
            detail = "\n".join(stderr_lines[-12:]) or "(no FFmpeg stderr)"
            raise RuntimeError(f"Output file looks empty or corrupted ({out_size} bytes).\n{detail}")

        out_vinfo = probe_first_video_stream(save_path)
        if not out_vinfo or not out_vinfo.get("codec_name"):
            detail = "\n".join(stderr_lines[-12:]) or "(no FFmpeg stderr)"
            raise RuntimeError(
                f"Merge finished, but the output has no readable video stream.\n{detail}"
            )
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        if stderr_thread is not None and stderr_thread.is_alive():
            stderr_thread.join(timeout=2)


def _merge_lossless(paths, save_path, set_status):
    ffmpeg_bin = get_ffmpeg_path()
    temp_dir = tempfile.mkdtemp(prefix="vlc_player_merge_files_")
    concat_path = os.path.join(temp_dir, "concat.txt")
    try:
        with open(concat_path, "w", encoding="utf-8") as f:
            for p in paths:
                escaped = os.path.abspath(p).replace("\\", "/").replace("'", r"'\''")
                f.write(f"file '{escaped}'\n")

        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-nostdin",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_path,
            "-c",
            "copy",
            "-progress",
            "pipe:1",
            save_path,
        ]
        total = _total_duration(paths)
        _run_ffmpeg_with_progress(
            cmd, save_path, set_status, total, f"Merging {len(paths)} videos (lossless)"
        )
    finally:
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


def _build_custom_merge_command(ffmpeg_bin, paths, save_path, settings, include_audio):
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-nostdin",
    ]
    for path in paths:
        cmd += ["-i", path]

    vf = _custom_video_filter(settings)
    filter_parts = []
    video_labels = []
    audio_labels = []
    for idx in range(len(paths)):
        vlabel = f"v{idx}"
        filter_parts.append(f"[{idx}:v:0]{vf}[{vlabel}]")
        video_labels.append(f"[{vlabel}]")
        if include_audio:
            alabel = f"a{idx}"
            # Normalize sample rate so concat does not fail across mismatched audio.
            filter_parts.append(
                f"[{idx}:a:0]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS[{alabel}]"
            )
            audio_labels.append(f"[{alabel}]")

    n = len(paths)
    if include_audio:
        concat_inputs = "".join(f"{video_labels[i]}{audio_labels[i]}" for i in range(n))
        filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[v][a]")
    else:
        filter_parts.append(f"{''.join(video_labels)}concat=n={n}:v=1:a=0[v]")

    cmd += ["-filter_complex", ";".join(filter_parts), "-map", "[v]"]
    if include_audio:
        cmd += ["-map", "[a]"]
    cmd += [
        *_custom_codec_args(save_path, settings),
        *(_custom_audio_args(save_path, settings) if include_audio else ["-an"]),
        "-progress",
        "pipe:1",
        save_path,
    ]
    return cmd


def _merge_custom(paths, save_path, settings, set_status):
    ffmpeg_bin = get_ffmpeg_path()
    want_audio = bool(settings.get("include_audio", True))
    include_audio = want_audio and all(_has_audio_stream(p) for p in paths)
    if want_audio and not include_audio:
        logging.info("[Merge] Not all inputs have audio; merging video only.")

    cmd = _build_custom_merge_command(ffmpeg_bin, paths, save_path, settings, include_audio)
    total = _total_duration(paths)
    _run_ffmpeg_with_progress(
        cmd, save_path, set_status, total, f"Merging {len(paths)} videos"
    )


def _show_merge_done(controller, root, save_path):
    """Refresh/select in grid when possible, then offer Play."""
    if controller is not None and hasattr(controller, "reveal_merged_file"):
        try:
            controller.reveal_merged_file(save_path)
        except Exception:
            logging.exception("[Merge] reveal_merged_file failed")

    play = messagebox.askyesno(
        "Done",
        f"Merged video saved to:\n{save_path}\n\nPlay it now?",
        parent=root,
    )
    if play and controller is not None and hasattr(controller, "open_video_player"):
        try:
            controller.open_video_player(save_path, os.path.basename(save_path))
        except Exception:
            logging.exception("[Merge] open_video_player failed")


def _merge_worker(paths, save_path, settings, controller, root):
    def set_status(msg):
        _set_status(controller, root, msg)

    try:
        if settings.get("mode") == "original":
            _merge_lossless(paths, save_path, set_status)
        else:
            _merge_custom(paths, save_path, settings, set_status)

        set_status(f"Merge complete: {os.path.basename(save_path)}")
        _clear_status_later(controller, root)
        _ui_call(
            root,
            lambda p=save_path: _show_merge_done(controller, root, p),
        )
    except Exception as e:
        logging.error("[Merge] Error: %s", e)
        set_status(f"Merge failed: {e}")
        hint = ""
        if settings.get("mode") == "original":
            hint = (
                "\n\nTip: Lossless merge needs matching codecs/resolution/fps. "
                "Try the Custom tab to re-encode."
            )
        _ui_call(
            root,
            lambda err=str(e), h=hint: messagebox.showerror("Merge Error", f"{err}{h}"),
        )
