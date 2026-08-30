"""
Canvas Size dialog for thumbnail grid (same behavior as batch convert → Canvas).

Places each image on a fixed-size canvas with anchor + background padding/clipping.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk
from tkinter import colorchooser

from batch_processing_dialog import (
    ANCHOR_KEYS,
    AnchorGridWidget,
    _CANVAS_BG_PRESETS,
    apply_canvas_size,
)
from image_loader import get_pil_image_size, load_pil_frames
from image_resize_dialog import save_pil_frames


class CanvasSizeImageDialog(ctk.CTkToplevel):
    """
    Canvas size for one or more files from the thumbnail grid.

    ``on_apply(canvas_w, canvas_h, bg_color, anchor)``.
    """

    def __init__(
        self,
        parent,
        *,
        paths: list[str],
        on_apply: Callable[[int, int, str, str], None],
    ):
        super().__init__(parent)
        n = len(paths)
        self.title("Canvas Size" if n <= 1 else f"Canvas Size ({n} images)")
        self.resizable(False, False)
        self._paths = list(paths)
        self._on_apply = on_apply

        self._ref_w, self._ref_h = 1920, 1080
        if self._paths:
            try:
                self._ref_w, self._ref_h = get_pil_image_size(self._paths[0])
            except Exception:
                pass

        try:
            self.transient(parent.winfo_toplevel())
        except Exception:
            pass

        pad = {"padx": 16, "pady": 8}
        if n > 1:
            names = [os.path.basename(p) for p in self._paths[:3]]
            extra = n - len(names)
            sample = ", ".join(names) + (f" +{extra} more" if extra > 0 else "")
            ctk.CTkLabel(
                self,
                text=f"Apply to {n} images",
                font=ctk.CTkFont(size=14, weight="bold"),
                anchor="w",
            ).pack(fill="x", padx=16, pady=(16, 2))
            ctk.CTkLabel(
                self,
                text=sample,
                font=ctk.CTkFont(size=11),
                text_color=("gray40", "gray65"),
                anchor="w",
                wraplength=420,
            ).pack(fill="x", padx=16, pady=(0, 8))
        else:
            name = os.path.basename(self._paths[0]) if self._paths else "Image"
            ctk.CTkLabel(
                self,
                text="Canvas Size",
                font=ctk.CTkFont(size=14, weight="bold"),
                anchor="w",
            ).pack(fill="x", padx=16, pady=(16, 2))
            ctk.CTkLabel(
                self,
                text=name,
                font=ctk.CTkFont(size=11),
                text_color=("gray40", "gray65"),
                anchor="w",
                wraplength=420,
            ).pack(fill="x", padx=16, pady=(0, 2))
            ctk.CTkLabel(
                self,
                text=f"Current size: {self._ref_w} × {self._ref_h} px",
                font=ctk.CTkFont(size=11),
                text_color=("gray50", "gray55"),
                anchor="w",
            ).pack(fill="x", padx=16, pady=(0, 8))

        size_row = ctk.CTkFrame(self, fg_color="transparent")
        size_row.pack(fill="x", **pad)
        ctk.CTkLabel(size_row, text="Width:", width=55, anchor="w").pack(side="left")
        self._w_var = tk.StringVar(value=str(self._ref_w))
        self._w_entry = ctk.CTkEntry(
            size_row,
            textvariable=self._w_var,
            width=90,
            height=28,
            corner_radius=6,
            justify="center",
        )
        self._w_entry.pack(side="left", padx=(4, 12))
        ctk.CTkLabel(size_row, text="Height:", width=55, anchor="w").pack(side="left")
        self._h_var = tk.StringVar(value=str(self._ref_h))
        self._h_entry = ctk.CTkEntry(
            size_row,
            textvariable=self._h_var,
            width=90,
            height=28,
            corner_radius=6,
            justify="center",
        )
        self._h_entry.pack(side="left", padx=(4, 0))

        anchor_row = ctk.CTkFrame(self, fg_color="transparent")
        anchor_row.pack(fill="x", **pad)
        ctk.CTkLabel(anchor_row, text="Anchor:", width=70, anchor="w").pack(
            side="left", anchor="n", pady=4
        )
        self._anchor = AnchorGridWidget(anchor_row, value="center")
        self._anchor.pack(side="left", padx=(4, 0))

        ctk.CTkLabel(
            self,
            text="Pin where the image sits when the canvas grows or shrinks.",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
            wraplength=420,
            justify="left",
        ).pack(fill="x", padx=16, pady=(0, 4))

        bg_row = ctk.CTkFrame(self, fg_color="transparent")
        bg_row.pack(fill="x", **pad)
        ctk.CTkLabel(bg_row, text="Background:", width=90, anchor="w").pack(side="left")
        self._bg_preset = tk.StringVar(value="Black")
        ctk.CTkOptionMenu(
            bg_row,
            variable=self._bg_preset,
            values=list(_CANVAS_BG_PRESETS.keys()),
            command=self._on_bg_preset,
            width=140,
            height=28,
            corner_radius=6,
        ).pack(side="left", padx=(8, 8))
        self._bg_hex = tk.StringVar(value="#000000")
        self._bg_swatch = ctk.CTkButton(
            bg_row,
            text="",
            width=28,
            height=28,
            corner_radius=6,
            fg_color="#000000",
            hover_color="#000000",
            command=self._pick_bg_color,
        )
        self._bg_swatch.pack(side="left", padx=(0, 6))
        self._bg_entry = ctk.CTkEntry(
            bg_row,
            textvariable=self._bg_hex,
            width=90,
            height=28,
            corner_radius=6,
            justify="center",
            state="disabled",
        )
        self._bg_entry.pack(side="left")
        ctk.CTkButton(
            bg_row,
            text="Pick…",
            width=56,
            height=28,
            corner_radius=6,
            fg_color="gray30",
            hover_color="gray25",
            command=self._pick_bg_color,
        ).pack(side="left", padx=(6, 0))

        ctk.CTkLabel(
            self,
            text="Smaller canvas clips; larger canvas pads with the background.",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
            wraplength=420,
            justify="left",
        ).pack(fill="x", padx=16, pady=(0, 4))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(12, 16))
        ctk.CTkButton(
            btn_row,
            text="Cancel",
            width=100,
            height=30,
            corner_radius=6,
            fg_color="gray30",
            hover_color="gray25",
            command=self._on_cancel,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btn_row,
            text="Apply",
            width=100,
            height=30,
            corner_radius=6,
            command=self._on_ok,
        ).pack(side="right")

        self._on_bg_preset(self._bg_preset.get())
        self.bind("<Escape>", lambda _e: self._on_cancel())
        self.bind("<Return>", lambda _e: self._on_ok())
        self.update_idletasks()
        self._center_on_parent(parent)
        self.lift()
        self.focus_force()
        self.after(10, lambda: self.grab_set())
        self.after(30, lambda: self._w_entry.focus_set())

    def _center_on_parent(self, parent):
        try:
            self.update_idletasks()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            w = self.winfo_reqwidth()
            h = self.winfo_reqheight()
            self.geometry(f"+{px + max(0, (pw - w) // 2)}+{py + max(0, (ph - h) // 2)}")
        except Exception:
            pass

    @staticmethod
    def _parse_positive(text: str) -> Optional[int]:
        try:
            v = float(str(text).strip().replace(",", "."))
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        return int(round(v))

    @staticmethod
    def _normalize_hex_color(value: str) -> Optional[str]:
        s = (value or "").strip()
        if not s:
            return None
        if s.lower() in ("transparent", "none"):
            return "transparent"
        if s.startswith("#"):
            body = s[1:]
        else:
            body = s
        if len(body) == 3:
            body = "".join(ch * 2 for ch in body)
        if len(body) != 6:
            return None
        try:
            int(body, 16)
        except ValueError:
            return None
        return "#" + body.upper()

    def _sync_bg_swatch(self):
        preset = self._bg_preset.get()
        if preset == "Transparent":
            try:
                self._bg_swatch.configure(
                    fg_color=("#555555", "#555555"),
                    hover_color=("#666666", "#666666"),
                    text="∅",
                    text_color=("gray80", "gray80"),
                )
            except Exception:
                pass
            return
        hex_color = self._normalize_hex_color(self._bg_hex.get()) or "#808080"
        try:
            self._bg_swatch.configure(
                fg_color=hex_color,
                hover_color=hex_color,
                text="",
            )
        except Exception:
            pass

    def _pick_bg_color(self):
        if self._bg_preset.get() != "Custom…":
            self._bg_preset.set("Custom…")
            self._on_bg_preset("Custom…")
        initial = self._normalize_hex_color(self._bg_hex.get()) or "#808080"
        try:
            self.grab_release()
        except Exception:
            pass
        result = colorchooser.askcolor(
            color=initial,
            title="Canvas background color",
            parent=self,
        )
        try:
            if self.winfo_exists():
                self.grab_set()
                self.lift()
        except Exception:
            pass
        if not result or not result[1]:
            return
        chosen = self._normalize_hex_color(result[1])
        if not chosen:
            return
        self._bg_hex.set(chosen)
        self._sync_bg_swatch()

    def _on_bg_preset(self, value: str):
        if value == "Custom…":
            try:
                self._bg_entry.configure(state="normal")
            except Exception:
                pass
            if not self._normalize_hex_color(self._bg_hex.get()):
                self._bg_hex.set("#808080")
            self._sync_bg_swatch()
            return
        if value == "Transparent":
            try:
                self._bg_entry.configure(state="disabled")
            except Exception:
                pass
            self._sync_bg_swatch()
            return
        preset_hex = _CANVAS_BG_PRESETS.get(value, "#000000")
        if preset_hex not in ("transparent", "custom"):
            self._bg_hex.set(preset_hex)
        try:
            self._bg_entry.configure(state="disabled")
        except Exception:
            pass
        self._sync_bg_swatch()

    def _resolve_bg_color(self) -> Optional[str]:
        preset = self._bg_preset.get()
        if preset == "Transparent":
            return "transparent"
        if preset == "Custom…":
            bg = (self._bg_hex.get() or "").strip() or "#000000"
            normalized = self._normalize_hex_color(bg)
            return normalized or "#000000"
        return str(_CANVAS_BG_PRESETS.get(preset, "#000000"))

    def _on_cancel(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _on_ok(self):
        wv = self._parse_positive(self._w_var.get())
        hv = self._parse_positive(self._h_var.get())
        if wv is None or hv is None:
            return
        if wv > 50000 or hv > 50000:
            return
        anchor = self._anchor.get()
        if anchor not in ANCHOR_KEYS:
            anchor = "center"
        bg = self._resolve_bg_color()
        if not bg:
            return
        try:
            self._on_apply(int(wv), int(hv), bg, anchor)
        finally:
            try:
                self.grab_release()
            except Exception:
                pass
            self.destroy()


def open_canvas_size_dialog(parent, paths: list[str], on_apply):
    """Open canvas size for ``paths`` (one or more images)."""
    return CanvasSizeImageDialog(parent, paths=paths, on_apply=on_apply)


def canvas_size_image_file(
    path: str,
    *,
    canvas_w: int,
    canvas_h: int,
    bg_color: str,
    anchor: str,
) -> tuple[int, int]:
    """Apply canvas size to one image on disk (all frames when animated)."""
    frames, durations = load_pil_frames(path)
    if not frames:
        raise ValueError("no frames")
    if canvas_w > 50000 or canvas_h > 50000:
        raise ValueError("target size too large")
    out = [
        apply_canvas_size(im, canvas_w, canvas_h, bg_color=bg_color, anchor=anchor)
        for im in frames
    ]
    save_pil_frames(out, path, durations)
    return canvas_w, canvas_h
