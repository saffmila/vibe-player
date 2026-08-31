"""
rife_dialog.py — Compact options dialog for offline RIFE interpolation.
"""

from __future__ import annotations

import os
from tkinter import filedialog, messagebox

import customtkinter as ctk

from promo_banner import PROMO_STRIP_DIALOG_W, attach_promo_strip, sync_promo_strip
from rife_config import PACK_MISSING_MESSAGE, runtime_status
from video_encode_settings import RIFE_MODE_LABELS, RIFE_MULT_LABELS, make_info_box, set_info_text

# Match promo strip comfort width so the banner isn't squeezed.
_RIFE_DIALOG_W = PROMO_STRIP_DIALOG_W
_RIFE_DIALOG_H = 460


class RifeOptionsDialog(ctk.CTkToplevel):
    """Ask for multiplier / mode / output path, then call on_confirm(paths, options)."""

    def __init__(self, parent, paths: list[str], on_confirm, controller=None):
        super().__init__(parent)
        self.title("RIFE Interpolate")
        self.paths = list(paths or [])
        self.on_confirm = on_confirm
        self.controller = controller
        self.result = None

        self.geometry(f"{_RIFE_DIALOG_W}x{_RIFE_DIALOG_H}")
        self.minsize(_RIFE_DIALOG_W, 400)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.mult_var = ctk.StringVar(value="2×")
        self.mode_var = ctk.StringVar(value=RIFE_MODE_LABELS[0])
        self.audio_var = ctk.BooleanVar(value=True)
        default_out = os.path.dirname(self.paths[0]) if self.paths else os.getcwd()
        self.out_dir_var = ctk.StringVar(value=default_out)

        attach_promo_strip(
            self,
            "strip_rife.png",
            dialog_width=_RIFE_DIALOG_W,
            controller=self.controller,
        )

        ctk.CTkLabel(
            self,
            text="RIFE frame interpolation",
            text_color="#00bfff",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(10, 4))

        status = runtime_status()
        status_text = (
            "Optional pack ready."
            if status.get("ready")
            else (status.get("message") or PACK_MISSING_MESSAGE)
        )
        info = make_info_box(self, wraplength=_RIFE_DIALOG_W - 60, icon="✨")
        info.pack(fill="x", padx=16, pady=(0, 10))
        set_info_text(
            info,
            f"{len(self.paths)} video(s)\n{status_text}",
        )
        self._pack_ready = bool(status.get("ready"))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        row1 = ctk.CTkFrame(body, fg_color="transparent")
        row1.pack(fill="x", pady=4)
        ctk.CTkLabel(row1, text="Multiplier:", width=100, anchor="w").pack(side="left")
        ctk.CTkOptionMenu(
            row1, variable=self.mult_var, values=list(RIFE_MULT_LABELS), height=28
        ).pack(side="left", fill="x", expand=True)

        row2 = ctk.CTkFrame(body, fg_color="transparent")
        row2.pack(fill="x", pady=4)
        ctk.CTkLabel(row2, text="Mode:", width=100, anchor="w").pack(side="left")
        ctk.CTkOptionMenu(
            row2, variable=self.mode_var, values=list(RIFE_MODE_LABELS), height=28
        ).pack(side="left", fill="x", expand=True)

        ctk.CTkCheckBox(body, text="Include audio", variable=self.audio_var).pack(
            anchor="w", pady=(6, 8)
        )

        out_row = ctk.CTkFrame(body, fg_color="transparent")
        out_row.pack(fill="x", pady=4)
        ctk.CTkLabel(out_row, text="Output folder:", width=100, anchor="w").pack(side="left")
        ctk.CTkEntry(out_row, textvariable=self.out_dir_var, height=28).pack(
            side="left", fill="x", expand=True, padx=(0, 6)
        )
        ctk.CTkButton(out_row, text="…", width=36, height=28, command=self._browse).pack(
            side="left"
        )

        btn = ctk.CTkFrame(self, fg_color="transparent")
        btn.pack(side="bottom", fill="x", padx=16, pady=14)
        ctk.CTkButton(btn, text="Cancel", width=100, command=self.destroy).pack(side="left")
        self.start_btn = ctk.CTkButton(btn, text="Start", command=self._start)
        self.start_btn.pack(side="right", fill="x", expand=True, padx=(10, 0))
        if not self._pack_ready:
            self.start_btn.configure(state="disabled")

        self.lift()
        self.focus_force()
        self.after_idle(lambda: sync_promo_strip(self))
        self.after(80, lambda: sync_promo_strip(self))

    def _browse(self):
        path = filedialog.askdirectory(initialdir=self.out_dir_var.get() or None)
        if path:
            self.out_dir_var.set(path)

    def _start(self):
        if not self._pack_ready:
            messagebox.showwarning("RIFE pack missing", PACK_MISSING_MESSAGE, parent=self)
            return
        out_dir = (self.out_dir_var.get() or "").strip()
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showerror("Output folder", "Choose a valid output folder.", parent=self)
            return
        mult_raw = (self.mult_var.get() or "2×").strip()
        mult = 4 if mult_raw.startswith("4") else 2
        mode_label = self.mode_var.get() or RIFE_MODE_LABELS[0]
        mode = "slowmo" if "slow" in mode_label.lower() else "fps"
        options = {
            "multiplier": mult,
            "mode": mode,
            "include_audio": bool(self.audio_var.get()),
            "output_dir": out_dir,
        }
        self.result = options
        self.destroy()
        if self.on_confirm:
            self.on_confirm(self.paths, options)
