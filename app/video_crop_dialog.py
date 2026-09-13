"""
Interactive video crop dialog (OpenCV preview + image-crop HUD overlay).

Preview reuses ``OpenCvClip`` from video compare (Tk canvas, not VLC).
Crop interaction reuses ``CropModeController`` / ``CropOverlayHUD``.
Apply re-encodes via FFmpeg ``crop=w:h:x:y`` (shared filter path in video_merge).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Optional

import customtkinter as ctk
import cv2
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image as PILImage
from PIL import ImageTk

from image_crop_hud import CropModeController
from opencv_video_clip import OpenCvClip
from video_encode_settings import DEFAULT_AUDIO_BITRATE, DEFAULT_VIDEO_QUALITY
from video_merge import _clear_status_later, _set_status, _ui_call
from vtp_constants import VIDEO_FORMATS

_BG = "#1a1a1a"
_HUD_BG = "#252525"


def _apply_dark_titlebar(window) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        window.update_idletasks()
        wid = int(window.winfo_id())
        hwnd = ctypes.windll.user32.GetAncestor(wid, 2) or wid
        use_dark = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            20,
            ctypes.byref(use_dark),
            ctypes.sizeof(use_dark),
        )
    except Exception:
        logging.debug("[VideoCrop] dark titlebar failed", exc_info=True)


def _format_clock(seconds: float) -> str:
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        s = 0
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def _even_crop_dict(rect) -> dict:
    """Clamp crop box to even dimensions for yuv420p."""
    x0, y0, x1, y1 = (int(v) for v in rect)
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    w = max(2, x1 - x0)
    h = max(2, y1 - y0)
    w -= w % 2
    h -= h % 2
    x0 -= x0 % 2
    y0 -= y0 % 2
    return {"w": w, "h": h, "x": max(0, x0), "y": max(0, y0)}


class _CropFrameHost:
    """
    Duck-typed host for ``CropModeController``.

    Exposes the same surface the image crop HUD expects (Tk canvas + PIL frame),
    without ImageViewerLegacy animation / save plumbing.
    """

    def __init__(self, dialog: "VideoCropDialog"):
        self._dialog = dialog
        self.image_window = dialog
        self.canvas = dialog.canvas
        self.canvas_image = None
        self.original_image: Optional[PILImage.Image] = None
        self.image_path = dialog.video_path
        self.zoom_factor = 1.0
        # HUD is docked under the canvas (not overlaid) — allow full-frame crop.
        self.crop_hud_overlays_canvas = False
        self._anim_frames = []
        self._anim_durations = []
        self.crop_hud_kwargs = {
            "enable_rotate": False,
            "show_apply_menu": False,
            "primary_apply_label": "Save…",
            "compact": True,
        }
        self.crop = CropModeController(self)

    def _is_animated(self) -> bool:
        return False

    def _stop_animation(self) -> None:
        return None

    def _start_animation_if_needed(self) -> None:
        return None

    def _map_anim_frames(self, fn) -> None:
        return None

    def _request_cancel_crop(self) -> None:
        self._dialog.close()

    def apply_video_crop(self, mode: str, rect) -> None:
        self._dialog.apply_crop(rect)

    def _apply_loaded_frames(self, frames, durations, reset_title: bool = False) -> None:
        if not frames:
            return
        self.original_image = frames[0]
        self._anim_frames = list(frames)
        self._anim_durations = list(durations or [0] * len(frames))
        self._dialog._paint_pil(self.original_image)

    def update_image(self, center: bool = True, refresh_overlays: bool = True) -> None:
        if self.original_image is not None:
            self._dialog._paint_pil(self.original_image)
        if refresh_overlays:
            self._refresh_overlays()

    def _refresh_overlays(self) -> None:
        if self.crop.active:
            self.crop.redraw()


class VideoCropDialog(ctk.CTkToplevel):
    """Fullscreen-ish crop UI: OpenCV frame preview + shared crop HUD."""

    def __init__(
        self,
        parent,
        video_path: str,
        *,
        controller=None,
        start_time_s: float | None = None,
    ):
        super().__init__(parent)
        self.video_path = os.path.normpath(video_path)
        self.controller = controller
        self.title(f"Crop Video — {os.path.basename(self.video_path)}")
        self.configure(fg_color=_BG)

        self._clip: Optional[OpenCvClip] = None
        self._photo = None
        self._scrubbing = False
        self._busy = False
        self._closed = False

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())

        # Pack bottom first so it keeps a tight requested height (body expands).
        self._bottom = ctk.CTkFrame(self, fg_color=_HUD_BG, corner_radius=0, height=44)
        self._bottom.pack(side="bottom", fill="x")
        try:
            self._bottom.pack_propagate(False)
        except Exception:
            pass

        self._hud_dock = ctk.CTkFrame(self._bottom, fg_color=_HUD_BG, corner_radius=0)
        self._hud_dock.pack(fill="both", expand=True)

        self._body = ctk.CTkFrame(self, fg_color=_BG, corner_radius=0)
        self._body.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(self._body, bg="black", highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self._time_var = tk.StringVar(value="00:00")
        self._seek_var = tk.DoubleVar(value=0.0)
        self._dur_var = tk.StringVar(value="00:00")
        self._scrub_mounted = False

        self._host = _CropFrameHost(self)
        self._host.image_window = self._hud_dock

        self.bind("<Return>", self._on_return_apply)
        self.bind("<KP_Enter>", self._on_return_apply)

        try:
            self.state("zoomed")
        except Exception:
            try:
                self.attributes("-zoomed", True)
            except Exception:
                self.geometry("1280x800")

        self.after(10, _apply_dark_titlebar, self)
        self.after(20, lambda: self._boot(start_time_s))

    def _boot(self, start_time_s: float | None) -> None:
        try:
            clip = OpenCvClip(self.video_path, log_tag="VideoCrop")
        except Exception as e:
            logging.exception("[VideoCrop] open failed")
            messagebox.showerror("Crop Video", f"Could not open video:\n{e}", parent=self)
            self.close()
            return
        if clip.cap is None or clip.frame_bgr is None:
            messagebox.showerror(
                "Crop Video",
                "Could not decode a preview frame for this video.",
                parent=self,
            )
            clip.close()
            self.close()
            return

        self._clip = clip
        self._dur_var.set(_format_clock(clip.duration))
        if start_time_s is not None and start_time_s > 0:
            clip.seek_time(float(start_time_s))
            self._seek_var.set(clip.ratio)
        self._show_current_frame(enter_crop=True)

    def _pil_from_bgr(self, frame_bgr) -> Optional[PILImage.Image]:
        if frame_bgr is None:
            return None
        try:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            return PILImage.fromarray(rgb)
        except Exception:
            logging.debug("[VideoCrop] BGR→PIL failed", exc_info=True)
            return None

    def _paint_pil(self, pil: PILImage.Image) -> None:
        if self._closed or pil is None:
            return
        try:
            cw = max(1, int(self.canvas.winfo_width()))
            ch = max(1, int(self.canvas.winfo_height()))
        except tk.TclError:
            return
        if cw < 4 or ch < 4:
            return

        src_w, src_h = pil.size
        if src_w < 1 or src_h < 1:
            return
        scale = min(cw / float(src_w), ch / float(src_h))
        vw = max(1, int(round(src_w * scale)))
        vh = max(1, int(round(src_h * scale)))
        try:
            resized = pil.resize((vw, vh), PILImage.BILINEAR)
            photo = ImageTk.PhotoImage(resized)
        except Exception:
            logging.debug("[VideoCrop] paint resize failed", exc_info=True)
            return

        self._photo = photo  # keep ref
        item = self._host.canvas_image
        try:
            if item is None:
                item = self.canvas.create_image(
                    cw // 2, ch // 2, image=photo, anchor="center", tags="frame"
                )
                self._host.canvas_image = item
            else:
                self.canvas.coords(item, cw // 2, ch // 2)
                self.canvas.itemconfigure(item, image=photo)
            self.canvas.tag_lower("frame")
        except tk.TclError:
            return

    def _show_current_frame(self, *, enter_crop: bool = False) -> None:
        clip = self._clip
        if clip is None or clip.frame_bgr is None:
            return
        pil = self._pil_from_bgr(clip.frame_bgr)
        if pil is None:
            return
        self._host.original_image = pil
        self._host._anim_frames = [pil]
        self._host._anim_durations = [0]
        self._time_var.set(_format_clock(clip.time_s))
        self._paint_pil(pil)

        if enter_crop and not self._host.crop.active:
            # Wait for canvas geometry so bbox mapping works.
            self.after(50, self._enter_crop_safe)
        elif self._host.crop.active:
            # Keep crop geometry; refresh overlay on new frame (same size).
            if self._host.crop._base_frames is not None:
                self._host.crop._base_frames = [pil.copy()]
                self._host.crop._base_size = pil.size
            self._host.crop.redraw()

    def _mount_scrub_into_hud(self, hud) -> None:
        """Put the timeline scrubber into the HUD center slot (one compact row)."""
        if self._scrub_mounted or hud is None:
            return
        center = getattr(hud, "center_bar", None)
        if center is None:
            return
        self._scrub_mounted = True

        ctk.CTkLabel(
            center,
            textvariable=self._time_var,
            text_color="#cccccc",
            font=ctk.CTkFont(size=11),
            width=48,
        ).pack(side="left", padx=(0, 6))

        self._seek = ctk.CTkSlider(
            center,
            from_=0.0,
            to=1.0,
            variable=self._seek_var,
            command=self._on_seek,
            height=14,
        )
        self._seek.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._seek.bind("<ButtonPress-1>", lambda _e: setattr(self, "_scrubbing", True))
        self._seek.bind("<ButtonRelease-1>", self._on_seek_release)

        ctk.CTkLabel(
            center,
            textvariable=self._dur_var,
            text_color="#888888",
            font=ctk.CTkFont(size=11),
            width=48,
        ).pack(side="left")

    def _enter_crop_safe(self) -> None:
        if self._closed or self._host.crop.active:
            return
        if self._host.original_image is None:
            return
        try:
            self.update_idletasks()
            self._paint_pil(self._host.original_image)
            self._host.crop.enter()
            # Image crop uses place() over the viewer; here the HUD must sit in the
            # bottom dock so Cancel / Save stay visible and don't clamp the rect.
            hud = self._host.crop.hud
            if hud is not None:
                try:
                    hud.place_forget()
                except tk.TclError:
                    pass
                hud.pack(fill="both", expand=True)
                self._mount_scrub_into_hud(hud)
                try:
                    hud.lift()
                except tk.TclError:
                    pass
            self._host.crop.rect = self._host.crop._clamp_rect(*self._host.crop.rect)
            self._host.crop.redraw()
        except Exception:
            logging.exception("[VideoCrop] enter crop failed")

    def _on_return_apply(self, _event=None):
        if self._closed or self._busy:
            return "break"
        crop = self._host.crop
        if crop.active and crop.rect is not None:
            crop.apply("overwrite")
        return "break"

    def _on_canvas_configure(self, _event=None) -> None:
        if self._host.original_image is not None:
            self._paint_pil(self._host.original_image)
            if self._host.crop.active:
                self._host.crop.redraw()

    def _on_seek(self, value) -> None:
        clip = self._clip
        if clip is None or self._busy:
            return
        try:
            ratio = float(value)
        except (TypeError, ValueError):
            return
        if not clip.seek_ratio(ratio):
            return
        self._show_current_frame()

    def _on_seek_release(self, _event=None) -> None:
        self._scrubbing = False
        self._on_seek(self._seek_var.get())

    def apply_crop(self, rect) -> None:
        if self._busy:
            return
        if not rect:
            messagebox.showwarning("Crop Video", "No crop selection.", parent=self)
            return
        crop = _even_crop_dict(rect)
        if crop["w"] < 2 or crop["h"] < 2:
            messagebox.showwarning("Crop Video", "Crop region is too small.", parent=self)
            return

        src = self.video_path
        base = os.path.splitext(os.path.basename(src))[0]
        ext = os.path.splitext(src)[1].lower() or ".mp4"
        if ext not in (".mp4", ".mkv", ".mov", ".webm", ".avi"):
            ext = ".mp4"

        save_path = filedialog.asksaveasfilename(
            parent=self,
            title="Save cropped video",
            defaultextension=ext,
            initialdir=os.path.dirname(src) or None,
            initialfile=f"{base}_crop{ext}",
            filetypes=[
                ("MP4", "*.mp4"),
                ("MKV", "*.mkv"),
                ("MOV", "*.mov"),
                ("WebM", "*.webm"),
                ("AVI", "*.avi"),
                ("All files", "*.*"),
            ],
        )
        if not save_path:
            return

        settings = {
            "mode": "custom",
            "ext": os.path.splitext(save_path)[1].lower() or ext,
            "keep_size": True,
            "include_audio": True,
            "video_quality": DEFAULT_VIDEO_QUALITY,
            "audio_bitrate": DEFAULT_AUDIO_BITRATE,
            "crop": crop,
        }

        self._busy = True
        controller = self.controller
        root = self.master

        def worker():
            from video_convert import _convert_custom

            def set_status(msg):
                _set_status(controller, root, msg)

            def _done(p=save_path):
                if controller is not None and hasattr(controller, "reveal_merged_file"):
                    try:
                        controller.reveal_merged_file(p)
                    except Exception:
                        logging.exception("[VideoCrop] reveal failed")
                play = messagebox.askyesno(
                    "Crop Video",
                    f"Cropped video saved to:\n{p}\n\nPlay it now?",
                    parent=root,
                )
                if play and controller is not None and hasattr(
                    controller, "open_video_player"
                ):
                    try:
                        controller.open_video_player(p, os.path.basename(p))
                    except Exception:
                        logging.exception("[VideoCrop] open_video_player failed")

            try:
                set_status(
                    f"Cropping: {os.path.basename(src)} → {crop['w']}×{crop['h']}"
                )
                _convert_custom(src, save_path, settings, set_status)
                set_status(f"Crop complete: {os.path.basename(save_path)}")
                _clear_status_later(controller, root)
                _ui_call(root, _done)
                _ui_call(root, self.close)
            except Exception as e:
                logging.error("[VideoCrop] encode failed: %s", e)
                set_status(f"Crop failed: {e}")
                _ui_call(
                    root,
                    lambda err=str(e): messagebox.showerror(
                        "Crop Video", f"Encode failed:\n{err}", parent=self
                    ),
                )
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True, name="video-crop").start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._scrub_mounted = False
        try:
            if self._host.crop.active:
                self._host.crop.exit(restore=False)
        except Exception:
            pass
        clip = self._clip
        self._clip = None
        if clip is not None:
            try:
                clip.close()
            except Exception:
                pass
        try:
            self.destroy()
        except tk.TclError:
            pass


def _current_player_time_s(controller, video_path: str) -> float | None:
    """If the main VLC player is on this file, return its time in seconds."""
    if controller is None:
        return None
    player = (
        getattr(controller, "current_video_window", None)
        or getattr(controller, "active_player", None)
        or getattr(controller, "video_player", None)
    )
    if player is None:
        return None
    try:
        playing = getattr(player, "video_path", None)
        if playing and os.path.normcase(os.path.normpath(playing)) != os.path.normcase(
            os.path.normpath(video_path)
        ):
            return None
        vlc_player = getattr(player, "player", None)
        if vlc_player is None:
            return None
        ms = int(vlc_player.get_time())
        if ms < 0:
            return None
        return ms / 1000.0
    except Exception:
        return None


def open_video_crop_dialog(parent, video_path, controller=None) -> Optional[VideoCropDialog]:
    """Open crop UI for a single existing video file."""
    if not video_path:
        messagebox.showinfo("Crop Video", "Select a video file to crop.", parent=parent)
        return None
    path = os.path.normpath(str(video_path))
    if not os.path.isfile(path):
        messagebox.showerror("Crop Video", f"File not found:\n{path}", parent=parent)
        return None
    if not path.lower().endswith(VIDEO_FORMATS):
        messagebox.showinfo("Crop Video", "Selected file is not a supported video.", parent=parent)
        return None

    existing = getattr(parent, "_video_crop_dialog", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.lift()
                existing.focus_force()
                return existing
        except Exception:
            pass

    start_t = _current_player_time_s(controller, path)
    dlg = VideoCropDialog(parent, path, controller=controller, start_time_s=start_t)
    parent._video_crop_dialog = dlg

    def _forget(_e=None, d=dlg):
        try:
            if getattr(parent, "_video_crop_dialog", None) is d:
                parent._video_crop_dialog = None
        except Exception:
            pass

    dlg.bind("<Destroy>", _forget, add="+")
    return dlg
