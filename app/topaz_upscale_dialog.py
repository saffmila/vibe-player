"""
topaz_upscale_dialog.py — Compact Topaz Photo AI CLI upscale options.
"""

from __future__ import annotations

import os
from tkinter import filedialog, messagebox

import customtkinter as ctk

from promo_banner import PROMO_STRIP_DIALOG_W, attach_promo_strip, sync_promo_strip
from topaz_config import (
    DEFAULT_TOPAZ_MODEL,
    DEFAULT_TOPAZ_SCALE,
    TOPAZ_UPSCALE_MODELS,
    runtime_status as topaz_runtime_status,
)

_STATUS_BG = ("gray86", "#1e1e1e")
_DIALOG_W = PROMO_STRIP_DIALOG_W
_DIALOG_H = 420
_SCALE_LABELS = ("2×", "4×", "6×")
_SCALE_VALUES = {"2×": 2, "4×": 4, "6×": 6}


class TopazUpscaleDialog(ctk.CTkToplevel):
    """Topaz Photo AI options → on_confirm(paths, options)."""

    def __init__(self, parent, paths: list[str], on_confirm, controller=None):
        super().__init__(parent)
        self.title("Photo AI Upscale")
        self.paths = list(paths or [])
        self.on_confirm = on_confirm
        self.controller = controller
        self.result = None
        self._ready = False

        self.minsize(_DIALOG_W, 320)
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        scale_saved = DEFAULT_TOPAZ_SCALE
        if controller is not None:
            try:
                scale_saved = int(
                    getattr(controller, "topaz_scale", DEFAULT_TOPAZ_SCALE) or 4
                )
            except Exception:
                scale_saved = 4
        scale_label = "4×"
        for lab, val in _SCALE_VALUES.items():
            if val == scale_saved:
                scale_label = lab
                break
        self.scale_var = ctk.StringVar(value=scale_label)

        topaz_model = DEFAULT_TOPAZ_MODEL
        if controller is not None:
            topaz_model = (
                str(
                    getattr(controller, "topaz_model", DEFAULT_TOPAZ_MODEL)
                    or DEFAULT_TOPAZ_MODEL
                ).strip()
                or DEFAULT_TOPAZ_MODEL
            )
        if topaz_model not in TOPAZ_UPSCALE_MODELS:
            models = list(TOPAZ_UPSCALE_MODELS) + [topaz_model]
        else:
            models = list(TOPAZ_UPSCALE_MODELS)
        self.topaz_model_var = ctk.StringVar(value=topaz_model)

        default_out = os.path.dirname(self.paths[0]) if self.paths else os.getcwd()
        self.out_dir_var = ctk.StringVar(value=default_out)
        self.suffix_var = ctk.StringVar(
            value=str(getattr(controller, "topaz_suffix", "") or "").strip()
            if controller
            else ""
        )
        self.status_var = ctk.StringVar(value="Checking…")

        attach_promo_strip(
            self,
            "strip_seedvr.png",
            dialog_width=_DIALOG_W,
            controller=self.controller,
        )

        btn = ctk.CTkFrame(self, fg_color="transparent")
        btn.pack(side="bottom", fill="x", padx=16, pady=14)
        ctk.CTkButton(btn, text="Cancel", width=100, command=self.destroy).pack(
            side="left"
        )
        self.start_btn = ctk.CTkButton(btn, text="Start", command=self._start)
        self.start_btn.pack(side="right", fill="x", expand=True, padx=(10, 0))

        ctk.CTkLabel(
            self,
            text="Topaz Photo AI",
            text_color="#00bfff",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(10, 4))

        status_wrap = ctk.CTkFrame(self, fg_color="transparent")
        status_wrap.pack(fill="x", padx=16, pady=(0, 8))
        box = ctk.CTkFrame(
            status_wrap,
            fg_color=_STATUS_BG,
            corner_radius=6,
            border_width=1,
            border_color=("gray70", "#1a1a1a"),
        )
        box.pack(fill="x")
        ctk.CTkLabel(
            box,
            textvariable=self.status_var,
            wraplength=_DIALOG_W - 60,
            justify="left",
            text_color="#b0b0b0",
            anchor="w",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=10, pady=10)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        row_s = ctk.CTkFrame(body, fg_color="transparent")
        row_s.pack(fill="x", pady=4)
        ctk.CTkLabel(row_s, text="Scale:", width=110, anchor="w").pack(side="left")
        ctk.CTkOptionMenu(
            row_s,
            variable=self.scale_var,
            values=list(_SCALE_LABELS),
            height=28,
            width=100,
        ).pack(side="left")

        row_m = ctk.CTkFrame(body, fg_color="transparent")
        row_m.pack(fill="x", pady=4)
        ctk.CTkLabel(row_m, text="Model:", width=110, anchor="w").pack(side="left")
        ctk.CTkOptionMenu(
            row_m, variable=self.topaz_model_var, values=models, height=28
        ).pack(side="left", fill="x", expand=True)

        out_row = ctk.CTkFrame(body, fg_color="transparent")
        out_row.pack(fill="x", pady=(10, 4))
        ctk.CTkLabel(out_row, text="Output folder:", width=110, anchor="w").pack(
            side="left"
        )
        ctk.CTkEntry(out_row, textvariable=self.out_dir_var, height=28).pack(
            side="left", fill="x", expand=True, padx=(0, 6)
        )
        ctk.CTkButton(
            out_row, text="…", width=36, height=28, command=self._browse
        ).pack(side="left")

        suf_row = ctk.CTkFrame(body, fg_color="transparent")
        suf_row.pack(fill="x", pady=4)
        ctk.CTkLabel(suf_row, text="Suffix:", width=110, anchor="w").pack(side="left")
        ctk.CTkEntry(suf_row, textvariable=self.suffix_var, height=28).pack(
            side="left", fill="x", expand=True
        )
        ctk.CTkLabel(
            suf_row,
            text="(empty = _topaz)",
            text_color="#888",
            font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=(8, 0))

        self.lift()
        self.focus_force()
        self.after_idle(lambda: sync_promo_strip(self))
        self.after(80, lambda: sync_promo_strip(self))
        self.after(40, self._refresh_status)
        self.after(60, self._fit_and_center)

    def _fit_and_center(self) -> None:
        try:
            self.update_idletasks()
            req_h = int(self.winfo_reqheight() or 0)
            screen_h = int(self.winfo_screenheight() or 900)
            max_h = max(360, screen_h - 100)
            h = max(380, min(max_h, max(req_h + 20, 420)))
            self.geometry(f"{_DIALOG_W}x{h}")
            self.update_idletasks()
            parent = self.master
            px = int(parent.winfo_rootx())
            py = int(parent.winfo_rooty())
            pw = int(parent.winfo_width())
            ph = int(parent.winfo_height())
            w = int(self.winfo_width())
            hh = int(self.winfo_height())
            x = px + max(0, (pw - w) // 2)
            y = py + max(0, (ph - hh) // 2)
            self.geometry(f"{_DIALOG_W}x{h}+{x}+{y}")
            sync_promo_strip(self)
        except Exception:
            pass

    def _browse(self) -> None:
        path = filedialog.askdirectory(initialdir=self.out_dir_var.get() or os.getcwd())
        if path:
            self.out_dir_var.set(path)

    def _refresh_status(self) -> None:
        n = len(self.paths)
        status = topaz_runtime_status()
        self._ready = bool(status.get("ready"))
        parts = [f"{n} image(s)", "Engine: Topaz Photo AI (tpai.exe)"]
        if self._ready:
            ver = status.get("version") or "?"
            parts.append(f"Found v{ver}")
            parts.append(str(status.get("exe") or ""))
            parts.append("Scale uses Autopilot factor (tpai CLI bug workaround).")
        else:
            parts.append(status.get("message") or "Not installed.")
        self.status_var.set("\n".join(parts))
        self.start_btn.configure(state="normal" if self._ready else "disabled")

    def _start(self) -> None:
        if not self._ready:
            return
        out_dir = (self.out_dir_var.get() or "").strip()
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showwarning(
                "Photo AI",
                "Choose a valid output folder.",
                parent=self,
            )
            return

        suffix = (self.suffix_var.get() or "").strip()
        options = {
            "backend_id": "topaz",
            "output_dir": out_dir,
            "scale": _SCALE_VALUES.get(self.scale_var.get(), 4),
            "model": self.topaz_model_var.get(),
            "suffix": suffix or "_topaz",
            "output_format": "png",
        }

        if self.controller is not None:
            try:
                setattr(self.controller, "topaz_scale", options["scale"])
                setattr(self.controller, "topaz_model", options["model"])
                if suffix:
                    setattr(self.controller, "topaz_suffix", suffix)
            except Exception:
                pass

        self.result = {"paths": list(self.paths), "options": options}
        if callable(self.on_confirm):
            self.on_confirm(list(self.paths), options)
        self.destroy()
