"""
spandrel_preview_window.py — External crop preview for Fast Upscale.

Single-canvas split slider. Compare vs Original (NEAREST source) or vs a
second model (A|B). Status + progress live in the footer so the image stays
readable.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import tkinter as tk
from typing import Callable

import customtkinter as ctk
from PIL import Image, ImageTk

_BG = "#1a1a1a"
_DIVIDER = "#00d4ff"
_CROP_SRC = 256  # source crop edge (px); 4× → ~1024 preview
_DEBOUNCE_MS = 280
_WIN_W = 900
_WIN_H = 640
_HANDLE_HIT = 12
_STATUS_IDLE = "#9aa0a6"
_STATUS_BUSY = "#ffb020"
_STATUS_OK = "#8ab4c8"
_STATUS_ERR = "#f28b82"
class SpandrelPreviewWindow(ctk.CTkToplevel):
    """
    Live crop preview for one source image + Spandrel model(s).

    ``get_job`` must return a dict with keys:
      source_path, model, models_dir, cuda_device
      optional: model_b, target_scale
    or None when not ready.
    """

    def __init__(
        self,
        parent,
        *,
        get_job: Callable[[], dict | None],
        on_close: Callable[[], None] | None = None,
        on_preview_timing: Callable[[str, float], None] | None = None,
    ):
        super().__init__(parent)
        self.title("Fast Upscale Preview")
        self.get_job = get_job
        self.on_close = on_close
        self.on_preview_timing = on_preview_timing

        self.geometry(f"{_WIN_W}x{_WIN_H}")
        self.minsize(640, 480)
        self.resizable(True, True)
        self.transient(parent)

        self._source_path: str | None = None
        self._source: Image.Image | None = None
        self._cx = 0.5
        self._cy = 0.5
        self._drag_last: tuple[int, int] | None = None
        self._gen = 0
        self._debounce_after = None
        self._timer_after = None
        self._busy = False
        self._t0 = 0.0
        self._split_ratio = 0.5
        self._dragging_split = False
        self._panning = False
        self._photo_after: ImageTk.PhotoImage | None = None
        self._photo_before_clip: ImageTk.PhotoImage | None = None
        self._last_before: Image.Image | None = None
        self._last_after: Image.Image | None = None
        self._disp_before: Image.Image | None = None
        self._disp_after: Image.Image | None = None
        self._img_ox = 0
        self._img_oy = 0
        self._left_is_sr = False  # True when left pane is model A in A/B mode

        self._status = ctk.StringVar(value="Ready")
        self._left_caption = ctk.StringVar(value="Original")
        self._right_caption = ctk.StringVar(value="Upscaled")

        # ---- header (compact) ----
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 2))
        ctk.CTkLabel(
            head,
            text="Crop preview",
            text_color="#00bfff",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            head,
            text="drag line = split · drag image = pan",
            text_color="#666",
            font=ctk.CTkFont(size=11),
            anchor="e",
        ).pack(side="right")

        # Captions above each half: Original|A, or A|B when a 2nd model is selected.
        # (Compare toggle hidden — mode follows whether model_b is set.)
        cap_row = ctk.CTkFrame(self, fg_color="transparent")
        cap_row.pack(fill="x", padx=12, pady=(4, 2))
        cap_row.grid_columnconfigure(0, weight=1, uniform="cap")
        cap_row.grid_columnconfigure(1, weight=1, uniform="cap")
        ctk.CTkLabel(
            cap_row,
            textvariable=self._left_caption,
            text_color="#c0c0c0",
            font=ctk.CTkFont(size=11),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkLabel(
            cap_row,
            textvariable=self._right_caption,
            text_color="#c0c0c0",
            font=ctk.CTkFont(size=11),
            anchor="e",
        ).grid(row=0, column=1, sticky="ew", padx=(6, 0))

        # ---- canvas ----
        self._canvas = tk.Canvas(self, bg=_BG, highlightthickness=0, bd=0, cursor="fleur")
        self._canvas.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind("<ButtonPress-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._canvas.bind("<Motion>", self._on_motion)

        # ---- footer status + actions ----
        status_row = ctk.CTkFrame(self, fg_color="transparent")
        status_row.pack(fill="x", padx=12, pady=(0, 4))
        self._status_lbl = ctk.CTkLabel(
            status_row,
            textvariable=self._status,
            text_color=_STATUS_IDLE,
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        )
        self._status_lbl.pack(fill="x")

        self._progress = ctk.CTkProgressBar(self, height=8, progress_color=_STATUS_BUSY)
        self._progress.pack(fill="x", padx=12, pady=(0, 8))
        self._progress.set(0)
        self._progress.configure(mode="determinate")

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(
            foot,
            text="Refresh",
            width=100,
            fg_color="gray30",
            hover_color="gray25",
            command=lambda: self.schedule_refresh(immediate=True),
        ).pack(side="left")
        ctk.CTkButton(foot, text="Close", width=100, command=self.destroy).pack(
            side="right"
        )

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.after(40, lambda: self.schedule_refresh(immediate=True))

    def destroy(self):
        self._gen += 1
        self._stop_timer()
        if self._debounce_after is not None:
            try:
                self.after_cancel(self._debounce_after)
            except Exception:
                pass
            self._debounce_after = None
        try:
            if callable(self.on_close):
                self.on_close()
        except Exception:
            pass
        try:
            super().destroy()
        except Exception:
            pass

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        self._stop_timer()
        try:
            if callable(self.on_close):
                self.on_close()
        except Exception:
            pass

    def set_compare_mode(self, mode: str = "", *, refresh: bool = True) -> None:
        """Compat for dialog — mode is auto from model_b; just refresh."""
        _ = mode
        if refresh:
            self.schedule_refresh(immediate=True)

    # ------------------------------------------------------------------ status
    def _set_status(self, text: str, *, kind: str = "idle") -> None:
        colors = {
            "idle": _STATUS_IDLE,
            "busy": _STATUS_BUSY,
            "ok": _STATUS_OK,
            "err": _STATUS_ERR,
        }
        self._status.set(text)
        try:
            self._status_lbl.configure(text_color=colors.get(kind, _STATUS_IDLE))
        except Exception:
            pass

    def _start_timer(self) -> None:
        self._t0 = time.perf_counter()
        self._stop_timer()
        try:
            self._progress.configure(mode="indeterminate")
            self._progress.start()
        except Exception:
            pass
        self._tick_timer()

    def _stop_timer(self) -> None:
        if self._timer_after is not None:
            try:
                self.after_cancel(self._timer_after)
            except Exception:
                pass
            self._timer_after = None
        try:
            self._progress.stop()
            self._progress.configure(mode="determinate")
            self._progress.set(0)
        except Exception:
            pass

    def _tick_timer(self) -> None:
        if not self._busy or not self.winfo_exists():
            return
        elapsed = time.perf_counter() - self._t0
        self._set_status(f"UPSCALING crop…  {elapsed:.1f}s", kind="busy")
        self._timer_after = self.after(100, self._tick_timer)

    def _emit_timing(self, model_key: str, seconds: float) -> None:
        if not model_key or seconds <= 0:
            return
        cb = self.on_preview_timing
        if not callable(cb):
            return
        try:
            cb(model_key, float(seconds))
        except Exception:
            logging.debug("[SpandrelPreview] timing callback failed", exc_info=True)

    # ------------------------------------------------------------------- split
    def _area_size(self) -> tuple[int, int]:
        try:
            cw = int(self._canvas.winfo_width())
            ch = int(self._canvas.winfo_height())
        except Exception:
            return 1, 1
        return max(1, cw), max(1, ch)

    def _split_x(self) -> int:
        cw, _ = self._area_size()
        return int(max(8, min(cw - 8, cw * self._split_ratio)))

    def _on_canvas_configure(self, _event=None) -> None:
        if self._dragging_split:
            return
        self._repaint_display()

    def _on_motion(self, event) -> None:
        if self._dragging_split or self._panning:
            return
        try:
            self._canvas.configure(
                cursor="sb_h_double_arrow"
                if abs(event.x - self._split_x()) <= _HANDLE_HIT
                else "fleur"
            )
        except Exception:
            pass

    def _on_press(self, event) -> None:
        if abs(event.x - self._split_x()) <= _HANDLE_HIT:
            self._dragging_split = True
            self._panning = False
            self._drag_last = None
            try:
                self._canvas.configure(cursor="sb_h_double_arrow")
            except Exception:
                pass
        else:
            self._dragging_split = False
            self._panning = True
            self._drag_last = (event.x_root, event.y_root)

    def _on_drag(self, event) -> None:
        if self._dragging_split:
            cw, _ = self._area_size()
            self._split_ratio = max(0.05, min(0.95, event.x / float(cw)))
            self._redraw_split(fast=True)
            return
        if not self._panning or self._drag_last is None or self._source is None:
            return
        dx = event.x_root - self._drag_last[0]
        dy = event.y_root - self._drag_last[1]
        self._drag_last = (event.x_root, event.y_root)
        w, h = self._source.size
        cw, ch = self._area_size()
        side = min(_CROP_SRC, w, h)
        sens_x = side / max(cw, 1)
        sens_y = side / max(ch, 1)
        self._cx = max(0.0, min(1.0, self._cx - (dx * sens_x) / max(w, 1)))
        self._cy = max(0.0, min(1.0, self._cy - (dy * sens_y) / max(h, 1)))
        try:
            crop = self._source.crop(self._crop_box())
            # Live left side only while panning; right catches up on release.
            if self._last_after is not None:
                left = crop.resize(self._last_after.size, Image.Resampling.NEAREST)
                self._last_before = left
                self._paint_pair(left, self._last_after)
            else:
                self._last_before = crop
                self._paint_pair(crop, crop)
        except Exception:
            pass

    def _on_release(self, _event) -> None:
        was_pan = self._panning
        self._dragging_split = False
        self._panning = False
        self._drag_last = None
        if was_pan:
            self.schedule_refresh(immediate=False)

    # ------------------------------------------------------------------ paint
    def _fit_pair(
        self, before: Image.Image, after: Image.Image
    ) -> tuple[Image.Image, Image.Image]:
        cw, ch = self._area_size()
        box = (max(32, cw), max(32, ch))
        fitted_after = after.copy()
        fitted_after.thumbnail(box, Image.Resampling.LANCZOS)
        # Left: NEAREST for source crop honesty; LANCZOS when left is model B.
        resample = (
            Image.Resampling.LANCZOS if self._left_is_sr else Image.Resampling.NEAREST
        )
        fitted_before = before.resize(fitted_after.size, resample)
        return fitted_before, fitted_after

    def _paint_pair(self, before: Image.Image, after: Image.Image) -> None:
        if before.size != after.size:
            before = before.resize(after.size, Image.Resampling.NEAREST)
        self._disp_before, self._disp_after = self._fit_pair(before, after)
        iw, ih = self._disp_after.size
        cw, ch = self._area_size()
        self._img_ox = max(0, (cw - iw) // 2)
        self._img_oy = max(0, (ch - ih) // 2)
        self._photo_after = ImageTk.PhotoImage(self._disp_after)
        self._redraw_split(fast=False)

    def _redraw_split(self, *, fast: bool = False) -> None:
        if self._disp_after is None or self._disp_before is None:
            return
        if self._photo_after is None:
            self._photo_after = ImageTk.PhotoImage(self._disp_after)

        cw, ch = self._area_size()
        sx = self._split_x()
        ox, oy = self._img_ox, self._img_oy
        iw, ih = self._disp_after.size

        left_end = max(0, min(iw, sx - ox))
        self._canvas.delete("all")
        self._canvas.create_image(ox, oy, image=self._photo_after, anchor="nw", tags="img")
        if left_end > 0:
            clip = self._disp_before.crop((0, 0, left_end, ih))
            self._photo_before_clip = ImageTk.PhotoImage(clip)
            self._canvas.create_image(
                ox, oy, image=self._photo_before_clip, anchor="nw", tags="img"
            )
        self._canvas.create_line(sx, 0, sx, ch, fill=_DIVIDER, width=2, tags="split")
        gy0 = max(0, (ch - 56) // 2)
        self._canvas.create_rectangle(
            sx - 4,
            gy0,
            sx + 4,
            gy0 + 56,
            fill=_DIVIDER,
            outline=_DIVIDER,
            tags="split",
        )
        _ = fast

    def _repaint_display(self) -> None:
        if self._last_after is None or self._last_before is None:
            return
        try:
            left = self._last_before
            if left.size != self._last_after.size:
                left = left.resize(self._last_after.size, Image.Resampling.NEAREST)
            self._paint_pair(left, self._last_after)
        except Exception:
            pass

    # ----------------------------------------------------------------- source
    def schedule_refresh(self, *, immediate: bool = False) -> None:
        if not self.winfo_exists():
            return
        if self._debounce_after is not None:
            try:
                self.after_cancel(self._debounce_after)
            except Exception:
                pass
            self._debounce_after = None
        delay = 0 if immediate else _DEBOUNCE_MS
        self._debounce_after = self.after(delay, self._refresh_now)

    def _ensure_source(self, path: str) -> bool:
        if self._source is not None and self._source_path == path:
            return True
        try:
            with Image.open(path) as im:
                im.load()
                self._source = im.convert("RGB")
            self._source_path = path
            self._cx = 0.5
            self._cy = 0.5
            return True
        except Exception as exc:
            logging.error("[SpandrelPreview] load failed: %s", exc)
            self._source = None
            self._source_path = None
            self._set_status(f"Cannot open image: {exc}", kind="err")
            return False

    def _crop_box(self) -> tuple[int, int, int, int]:
        assert self._source is not None
        w, h = self._source.size
        side = min(_CROP_SRC, w, h)
        cx = int(self._cx * w)
        cy = int(self._cy * h)
        x0 = max(0, min(w - side, cx - side // 2))
        y0 = max(0, min(h - side, cy - side // 2))
        return x0, y0, x0 + side, y0 + side

    @staticmethod
    def _apply_target_scale(
        src_size: tuple[int, int],
        out: Image.Image,
        target_scale: float | None,
    ) -> Image.Image:
        if target_scale is None or target_scale <= 0:
            return out
        tw = max(1, int(round(src_size[0] * float(target_scale))))
        th = max(1, int(round(src_size[1] * float(target_scale))))
        if out.size == (tw, th):
            return out
        return out.resize((tw, th), Image.Resampling.LANCZOS)

    @staticmethod
    def _short_name(model: str) -> str:
        return os.path.basename(str(model).replace("\\", "/").split("/")[-1])

    def _run_one(
        self,
        *,
        model: str,
        models_dir,
        cuda,
        crop: Image.Image,
        target_scale: float | None,
    ) -> tuple[Image.Image, int, float]:
        from spandrel_config import resolve_model_path
        from spandrel_pipeline import get_engine

        resolved = resolve_model_path(model, models_dir=models_dir)
        if resolved is None:
            raise RuntimeError(f"Model not found: {model}")
        engine = get_engine()
        t0 = time.perf_counter()
        if not engine.load_model(str(resolved), cuda_device=cuda):
            raise RuntimeError(f"Could not load model: {model}")
        out = engine.upscale(crop, tile_size=0, overlap=0)
        model_scale = int(getattr(engine, "scale", 0) or 0)
        out = self._apply_target_scale(crop.size, out, target_scale)
        elapsed = time.perf_counter() - t0
        return out, model_scale, elapsed

    def _refresh_now(self) -> None:
        self._debounce_after = None
        if not self.winfo_exists():
            return
        job = self.get_job() if callable(self.get_job) else None
        if not job:
            self._set_status("Select a model in Fast Upscale…", kind="idle")
            return
        path = str(job.get("source_path") or "")
        model = str(job.get("model") or "")
        model_b = str(job.get("model_b") or "").strip()
        models_dir = job.get("models_dir")
        cuda = job.get("cuda_device")
        target_scale = job.get("target_scale")
        try:
            target_scale = float(target_scale) if target_scale is not None else None
            if target_scale is not None and target_scale <= 0:
                target_scale = None
        except (TypeError, ValueError):
            target_scale = None

        if not path or not os.path.isfile(path):
            self._set_status("No source image.", kind="err")
            return
        if not model or model.startswith("("):
            self._set_status("Select a model…", kind="idle")
            return
        if not self._ensure_source(path):
            return

        # Auto: 2nd model selected → A|B; otherwise Original|A (no toggle).
        use_other = bool(model_b) and model_b != model

        box = self._crop_box()
        crop = self._source.crop(box)

        name_a = self._short_name(model)
        name_b = self._short_name(model_b) if model_b else ""
        if use_other:
            # Previous (A) left · new (B) right.
            self._left_is_sr = True
            self._left_caption.set(f"A · {name_a}")
            self._right_caption.set(f"B · {name_b}")
        else:
            # First pick / solo: Original | new model (shown as A on the right).
            self._left_is_sr = False
            self._left_caption.set("Original")
            self._right_caption.set(f"A · {name_a}")

        if self._last_after is not None and self._last_after.size[0] > 0:
            placeholder = crop.resize(self._last_after.size, Image.Resampling.NEAREST)
            try:
                self._paint_pair(placeholder, self._last_after)
            except Exception:
                self._paint_pair(crop, crop)
        else:
            try:
                self._paint_pair(crop, crop)
            except Exception:
                pass

        self._busy = True
        self._gen += 1
        gen = self._gen
        self._start_timer()
        compare_other = use_other

        def _worker():
            out_a = None
            out_b = None
            scale_a = 0
            t_a = 0.0
            t_b = 0.0
            err = None
            try:
                out_a, scale_a, t_a = self._run_one(
                    model=model,
                    models_dir=models_dir,
                    cuda=cuda,
                    crop=crop,
                    target_scale=target_scale,
                )
                if compare_other:
                    out_b, _scale_b, t_b = self._run_one(
                        model=model_b,
                        models_dir=models_dir,
                        cuda=cuda,
                        crop=crop,
                        target_scale=target_scale,
                    )
                    # Match pixel grid if models differ slightly after fit.
                    if out_b.size != out_a.size:
                        out_b = out_b.resize(out_a.size, Image.Resampling.LANCZOS)
            except Exception as exc:
                logging.error("[SpandrelPreview] %s", exc, exc_info=True)
                err = str(exc)

            def _done():
                if not self.winfo_exists() or gen != self._gen:
                    return
                self._busy = False
                self._stop_timer()
                if err or out_a is None:
                    self._set_status(err or "Preview failed.", kind="err")
                    return

                self._emit_timing(model, t_a)
                if compare_other and out_b is not None:
                    self._emit_timing(model_b, t_b)
                    # Left = A, right = B (_paint_pair left strip / full right).
                    self._last_before = out_a
                    self._last_after = out_b
                    self._paint_pair(out_a, out_b)
                    msg = (
                        f"A {t_a:.1f}s · B {t_b:.1f}s · "
                        f"{box[2] - box[0]}×{box[3] - box[1]} → "
                        f"{out_a.size[0]}×{out_a.size[1]}"
                    )
                else:
                    left = crop.resize(out_a.size, Image.Resampling.NEAREST)
                    self._last_before = left
                    self._last_after = out_a
                    self._paint_pair(left, out_a)
                    scale_note = ""
                    if target_scale is not None:
                        scale_note = f" → {target_scale:g}× out"
                    elif scale_a:
                        scale_note = f" ({scale_a}×)"
                    msg = (
                        f"Done in {t_a:.1f}s · "
                        f"{box[2] - box[0]}×{box[3] - box[1]} → "
                        f"{out_a.size[0]}×{out_a.size[1]}"
                        + scale_note
                    )

                self._set_status(msg, kind="ok")

            try:
                self.after(0, _done)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="spandrel-preview").start()
