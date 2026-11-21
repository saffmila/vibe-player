"""
fast_upscale_dialog.py — Compact Spandrel (single-pass) upscale options.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from promo_banner import PROMO_STRIP_DIALOG_W, attach_promo_strip, sync_promo_strip
from seedvr2_config import list_cuda_gpus
from spandrel_config import (
    OPENMODELDB_URL,
    default_upscale_dir,
    guess_scale_from_name,
    list_spandrel_models,
    resolve_models_dir,
    runtime_status as spandrel_runtime_status,
    weights_status as spandrel_weights_status,
)

_STATUS_BG = ("gray86", "#1e1e1e")
_UI_BTN_H = 28
_DIALOG_W = PROMO_STRIP_DIALOG_W
# Fallback before first tight `_fit_and_center` pass.
_DIALOG_H = 540
_TILE_LABELS = ("256", "512", "768", "1024")
_FACTOR_LABELS = ("Model", "2×", "4×", "Custom")
_FACTOR_TO_SCALE = {"2×": 2.0, "4×": 4.0}


class FastUpscaleDialog(ctk.CTkToplevel):
    """Spandrel options → on_confirm(paths, options)."""

    def __init__(self, parent, paths: list[str], on_confirm, controller=None):
        super().__init__(parent)
        self.title("Fast Upscale")
        self.paths = list(paths or [])
        self.on_confirm = on_confirm
        self.controller = controller
        self.result = None
        self._ready = False
        self._runtime_ok = False
        self._install_running = False
        self._all_models: list[dict] = []
        self._filtered: list[dict] = []

        self.geometry(f"{_DIALOG_W}x{_DIALOG_H}")
        self.minsize(_DIALOG_W, 420)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        saved_dir = ""
        if controller is not None:
            saved_dir = str(getattr(controller, "spandrel_models_dir", "") or "").strip()
        models_dir = str(resolve_models_dir(saved_dir or None))
        self.models_dir_var = ctk.StringVar(value=models_dir)

        self.filter_var = ctk.StringVar(
            value=str(getattr(controller, "spandrel_model_filter", "") or "").strip()
            if controller
            else ""
        )

        factor = "Model"
        if controller is not None:
            saved_factor = str(
                getattr(controller, "spandrel_scale_mode", "Model") or "Model"
            ).strip()
            if saved_factor in _FACTOR_LABELS:
                factor = saved_factor
        self.factor_var = ctk.StringVar(value=factor)

        custom_scale = "3"
        if controller is not None:
            try:
                custom_scale = str(
                    float(getattr(controller, "spandrel_custom_scale", 3) or 3)
                ).rstrip("0").rstrip(".")
            except Exception:
                custom_scale = "3"
        self.custom_scale_var = ctk.StringVar(value=custom_scale or "3")

        saved_model = ""
        if controller is not None:
            saved_model = str(getattr(controller, "spandrel_model", "") or "").strip()
        self.model_var = ctk.StringVar(value=saved_model)
        self._count_var = ctk.StringVar(value="")

        tiling_on = False
        if controller is not None:
            tiling_on = bool(getattr(controller, "spandrel_tiling", False))
        self.tiling_var = ctk.BooleanVar(value=tiling_on)
        tile = "512"
        if controller is not None:
            try:
                tile = str(int(getattr(controller, "spandrel_tile_size", 512) or 512))
            except Exception:
                tile = "512"
        if tile not in _TILE_LABELS:
            tile = "512"
        self.tile_var = ctk.StringVar(value=tile)

        gpus = list_cuda_gpus()
        gpu_labels = [g.get("label") or f"cuda:{g.get('index', 0)}" for g in gpus] or [
            "cuda:0"
        ]
        self._gpu_indices = {
            (g.get("label") or f"cuda:{g.get('index', 0)}"): str(g.get("index", 0))
            for g in gpus
        }
        cuda_saved = "0"
        if controller is not None:
            cuda_saved = str(getattr(controller, "spandrel_cuda_device", "0") or "0")
        initial_gpu = gpu_labels[0]
        for label, idx in self._gpu_indices.items():
            if idx == cuda_saved:
                initial_gpu = label
                break
        self.gpu_var = ctk.StringVar(value=initial_gpu)

        default_out = os.path.dirname(self.paths[0]) if self.paths else os.getcwd()
        self.out_dir_var = ctk.StringVar(value=default_out)
        self.suffix_var = ctk.StringVar(
            value=str(getattr(controller, "fast_upscale_suffix", "") or "").strip()
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

        ctk.CTkLabel(
            self,
            text="Fast Upscale (Spandrel)",
            text_color="#00bfff",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(10, 4))

        status_wrap = ctk.CTkFrame(self, fg_color="transparent")
        status_wrap.pack(fill="x", padx=16, pady=(0, 6))
        self._status_box = ctk.CTkFrame(
            status_wrap,
            fg_color=_STATUS_BG,
            corner_radius=6,
            border_width=1,
            border_color=("gray70", "#1a1a1a"),
        )
        self._status_box.pack(fill="x")
        ctk.CTkLabel(
            self._status_box,
            textvariable=self.status_var,
            wraplength=_DIALOG_W - 60,
            justify="left",
            text_color="#b0b0b0",
            anchor="w",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=10, pady=8)

        tools = ctk.CTkFrame(status_wrap, fg_color="transparent")
        tools.pack(fill="x", pady=(6, 0))
        self._install_btn = ctk.CTkButton(
            tools,
            text="Install spandrel…",
            width=140,
            height=_UI_BTN_H,
            command=self._install_spandrel,
        )
        self._install_btn.pack(side="left")
        self._pick_btn = ctk.CTkButton(
            tools,
            text="Pick model folder…",
            width=150,
            height=_UI_BTN_H,
            fg_color="gray30",
            hover_color="gray25",
            command=self._pick_models_folder,
        )
        self._pick_btn.pack(side="left", padx=(8, 0))
        self._refresh_btn = ctk.CTkButton(
            tools,
            text="Refresh",
            width=90,
            height=_UI_BTN_H,
            fg_color="gray30",
            hover_color="gray25",
            command=self._refresh_status,
        )
        self._refresh_btn.pack(side="left", padx=(8, 0))

        self._folder_label = ctk.CTkLabel(
            status_wrap,
            textvariable=self.models_dir_var,
            wraplength=_DIALOG_W - 40,
            justify="left",
            text_color="#8ab4c8",
            anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self._folder_label.pack(fill="x", pady=(4, 0))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="x", expand=False, padx=16, pady=(0, 4))

        row_f = ctk.CTkFrame(body, fg_color="transparent")
        row_f.pack(fill="x", pady=4)
        ctk.CTkLabel(row_f, text="Scale:", width=110, anchor="w").pack(side="left")
        self._factor_menu = ctk.CTkOptionMenu(
            row_f,
            variable=self.factor_var,
            values=list(_FACTOR_LABELS),
            height=28,
            width=110,
            command=lambda _v: self._on_factor_change(),
        )
        self._factor_menu.pack(side="left")
        self._custom_entry = ctk.CTkEntry(
            row_f, textvariable=self.custom_scale_var, width=64, height=28
        )
        self._custom_hint = ctk.CTkLabel(
            row_f,
            text="×  (Model = native network scale)",
            text_color="#888",
            font=ctk.CTkFont(size=11),
        )
        self._custom_hint.pack(side="left", padx=(10, 0))

        row_filter = ctk.CTkFrame(body, fg_color="transparent")
        row_filter.pack(fill="x", pady=(6, 2))
        ctk.CTkLabel(row_filter, text="Filter:", width=110, anchor="w").pack(side="left")
        self._filter_entry = ctk.CTkEntry(
            row_filter,
            textvariable=self.filter_var,
            height=28,
            placeholder_text="name contains… (e.g. PurePhoto, SPAN, Nomos)",
        )
        self._filter_entry.pack(side="left", fill="x", expand=True)
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        self.custom_scale_var.trace_add("write", lambda *_: self._apply_filter())
        ctk.CTkLabel(
            row_filter,
            textvariable=self._count_var,
            width=70,
            anchor="e",
            text_color="#888",
            font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=(8, 0))

        # Fixed-height list — do not expand or it pushes Output/Start off-screen.
        list_wrap = ctk.CTkFrame(
            body,
            fg_color=("gray90", "#1a1a1a"),
            corner_radius=6,
            border_width=1,
            border_color=("gray70", "#333"),
            height=168,
        )
        list_wrap.pack(fill="x", pady=(4, 6))
        list_wrap.pack_propagate(False)
        self._listbox = tk.Listbox(
            list_wrap,
            activestyle="dotbox",
            exportselection=False,
            highlightthickness=0,
            borderwidth=0,
            font=("Segoe UI", 10),
            bg="#1a1a1a",
            fg="#e0e0e0",
            selectbackground="#1f6aa5",
            selectforeground="#ffffff",
            height=8,
        )
        scroll = tk.Scrollbar(list_wrap, orient="vertical", command=self._listbox.yview)
        self._listbox.configure(yscrollcommand=scroll.set)
        self._listbox.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        scroll.pack(side="right", fill="y", padx=(0, 4), pady=6)
        self._listbox.bind("<<ListboxSelect>>", self._on_list_select)
        self._listbox.bind("<Double-Button-1>", lambda _e: self._start())

        row_t = ctk.CTkFrame(body, fg_color="transparent")
        row_t.pack(fill="x", pady=4)
        self._tiling_cb = ctk.CTkCheckBox(
            row_t,
            text="Tiling",
            variable=self.tiling_var,
            width=90,
            command=self._on_tiling_toggle,
        )
        self._tiling_cb.pack(side="left")
        self._tile_menu = ctk.CTkOptionMenu(
            row_t, variable=self.tile_var, values=list(_TILE_LABELS), height=28, width=100
        )
        self._tile_menu.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(row_t, text="GPU:", width=50, anchor="e").pack(
            side="left", padx=(12, 4)
        )
        self._gpu_menu = ctk.CTkOptionMenu(
            row_t, variable=self.gpu_var, values=gpu_labels, height=28
        )
        self._gpu_menu.pack(side="left", fill="x", expand=True)

        out_row = ctk.CTkFrame(body, fg_color="transparent")
        out_row.pack(fill="x", pady=(8, 4))
        ctk.CTkLabel(out_row, text="Output folder:", width=110, anchor="w").pack(
            side="left"
        )
        self._out_entry = ctk.CTkEntry(out_row, textvariable=self.out_dir_var, height=28)
        self._out_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._browse_btn = ctk.CTkButton(
            out_row, text="…", width=36, height=28, command=self._browse
        )
        self._browse_btn.pack(side="left")

        suf_row = ctk.CTkFrame(body, fg_color="transparent")
        suf_row.pack(fill="x", pady=4)
        ctk.CTkLabel(suf_row, text="Suffix:", width=110, anchor="w").pack(side="left")
        self._suffix_entry = ctk.CTkEntry(
            suf_row, textvariable=self.suffix_var, height=28
        )
        self._suffix_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            suf_row,
            text="(empty = _spandrel)",
            text_color="#888",
            font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=(8, 0))

        # Document flow (not side=bottom) — no empty gap above actions.
        btn = ctk.CTkFrame(self, fg_color="transparent")
        btn.pack(fill="x", padx=16, pady=(8, 14))
        ctk.CTkButton(btn, text="Cancel", width=100, command=self.destroy).pack(
            side="left"
        )
        self.start_btn = ctk.CTkButton(btn, text="Start", command=self._start)
        self.start_btn.pack(side="right", fill="x", expand=True, padx=(10, 0))

        self._on_factor_change(apply_filter=False)
        self._on_tiling_toggle()
        self.lift()
        self.focus_force()
        self.after_idle(lambda: sync_promo_strip(self))
        self.after(80, lambda: sync_promo_strip(self))
        self.after(40, self._refresh_status)
        self.after(60, self._fit_and_center)

    def _fit_and_center(self) -> None:
        """Size tightly to content reqheight and center on parent.

        CTk ``geometry()`` expects *logical* (pre window-scaling) sizes, while
        ``winfo_reqheight`` / screen metrics are already scaled pixels.
        """
        try:
            self.update_idletasks()
            req_h = int(self.winfo_reqheight() or 0)
            screen_h = int(self.winfo_screenheight() or 900)
            try:
                scale = float(self._get_window_scaling() or 1.0)
            except Exception:
                scale = 1.0
            if scale <= 0:
                scale = 1.0
            max_h_logical = max(240, int((screen_h - 80) / scale))
            h_logical = min(
                max_h_logical,
                max(int((req_h + 12) / scale), 280),
            )
            self.geometry(f"{_DIALOG_W}x{h_logical}")
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
            if y + hh > screen_h - 40:
                y = max(20, screen_h - hh - 40)
            self.geometry(f"{_DIALOG_W}x{h_logical}+{x}+{y}")
            sync_promo_strip(self)
        except Exception:
            pass

    def _on_tiling_toggle(self) -> None:
        try:
            self._tile_menu.configure(
                state="normal" if self.tiling_var.get() else "disabled"
            )
        except Exception:
            pass

    def _models_dir(self) -> str:
        return str(resolve_models_dir(self.models_dir_var.get()))

    def _target_scale(self) -> float | None:
        mode = self.factor_var.get()
        if mode == "Model":
            return None
        if mode in _FACTOR_TO_SCALE:
            return _FACTOR_TO_SCALE[mode]
        raw = (self.custom_scale_var.get() or "").strip().replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value > 0 else None

    def _on_factor_change(self, *, apply_filter: bool = True) -> None:
        if self.factor_var.get() == "Custom":
            if not self._custom_entry.winfo_ismapped():
                self._custom_hint.pack_forget()
                self._custom_entry.pack(side="left", padx=(8, 0))
                self._custom_hint.pack(side="left", padx=(8, 0))
        else:
            self._custom_entry.pack_forget()
            if not self._custom_hint.winfo_ismapped():
                self._custom_hint.pack(side="left", padx=(10, 0))
        if apply_filter:
            self._apply_filter()

    def _browse(self) -> None:
        path = filedialog.askdirectory(initialdir=self.out_dir_var.get() or os.getcwd())
        if path:
            self.out_dir_var.set(path)

    def _pick_models_folder(self) -> None:
        if self._install_running:
            return
        initial = self.models_dir_var.get() or str(default_upscale_dir())
        path = filedialog.askdirectory(
            title="Select Spandrel models folder",
            initialdir=initial if os.path.isdir(initial) else os.getcwd(),
        )
        if not path:
            return
        self.models_dir_var.set(str(Path(path).resolve()))
        if self.controller is not None:
            try:
                setattr(self.controller, "spandrel_models_dir", self.models_dir_var.get())
            except Exception:
                pass
        self._refresh_status()

    def _set_form_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for w in (
            self._install_btn,
            self._pick_btn,
            self._refresh_btn,
            self._factor_menu,
            self._filter_entry,
            self._tiling_cb,
            self._tile_menu,
            self._gpu_menu,
            self._out_entry,
            self._browse_btn,
            self._suffix_entry,
            self.start_btn,
        ):
            try:
                w.configure(state=state)
            except Exception:
                pass
        try:
            self._listbox.configure(state=state)
        except Exception:
            pass
        if enabled:
            self._on_tiling_toggle()
            self._update_action_states()

    def _install_spandrel(self) -> None:
        if self._install_running:
            return
        rt = spandrel_runtime_status(deep=False)
        if rt.get("ready"):
            messagebox.showinfo(
                "Spandrel",
                "Spandrel is already installed.",
                parent=self,
            )
            return
        ok = messagebox.askyesno(
            "Install spandrel",
            (
                "Install the Python package 'spandrel' into this app environment?\n\n"
                "Needed for RealPLKSR / SPAN / DAT / ESRGAN single-pass upscale.\n"
                "Uses the same pip target as BiRefNet / Autotag (app venv)."
            ),
            parent=self,
        )
        if not ok:
            return

        from gui_elements import open_file_op_progress_dialog
        from spandrel_setup import install_spandrel

        self._install_running = True
        self._set_form_enabled(False)
        progress = open_file_op_progress_dialog(
            self,
            title="Install spandrel",
            total=1,
            action_label="Installing",
            topmost=True,
            show_preview=False,
        )

        def _on_progress(step: int, total: int, detail: str):
            try:
                self.after(
                    0,
                    lambda s=step, t=total, d=detail: progress.set_progress(
                        s, t, detail=d, phase="load"
                    ),
                )
            except Exception:
                pass

        def _worker():
            result = install_spandrel(
                progress_cb=_on_progress,
                should_stop=lambda: bool(getattr(progress, "cancelled", False)),
            )

            def _done():
                self._install_running = False
                try:
                    progress.close()
                except Exception:
                    pass
                self._set_form_enabled(True)
                self._refresh_status()
                if result.get("ok"):
                    messagebox.showinfo(
                        "Install spandrel",
                        result.get("message") or "Spandrel is ready.",
                        parent=self,
                    )
                elif result.get("cancelled") or result.get("error") == "aborted":
                    messagebox.showinfo(
                        "Install spandrel",
                        "Install cancelled.",
                        parent=self,
                    )
                else:
                    messagebox.showerror(
                        "Install spandrel",
                        result.get("message") or "Install failed.",
                        parent=self,
                    )

            try:
                self.after(0, _done)
            except Exception:
                pass

        threading.Thread(
            target=_worker, daemon=True, name="spandrel-install"
        ).start()

    def _on_list_select(self, _event=None) -> None:
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._filtered):
            self.model_var.set(self._model_key(self._filtered[idx]))
            self._update_action_states()

    def _model_key(self, item: dict) -> str:
        """Stable id for selection: prefer relative path inside the models folder."""
        return str(item.get("rel") or item.get("label") or item.get("filename") or "")

    def _update_action_states(self) -> None:
        model = self.model_var.get()
        has_model = bool(model) and not model.startswith("(") and bool(self._filtered)
        self._ready = has_model and bool(self._all_models)
        # Start stays clickable when a model is selected; runtime checked on click /
        # Install button handles missing spandrel (BiRefNet / RIFE pattern).
        try:
            self.start_btn.configure(
                state="normal" if (self._ready and not self._install_running) else "disabled"
            )
        except Exception:
            pass
        try:
            if self._runtime_ok:
                self._install_btn.configure(
                    text="Spandrel OK",
                    state="disabled",
                    fg_color="gray30",
                    hover_color="gray25",
                )
            else:
                self._install_btn.configure(
                    text="Install spandrel…",
                    state="normal" if not self._install_running else "disabled",
                )
        except Exception:
            pass

    def _apply_filter(self) -> None:
        needle = (self.filter_var.get() or "").strip().lower()
        want = self._target_scale()
        mode = self.factor_var.get()

        filtered: list[dict] = []
        for item in self._all_models:
            name = item.get("filename") or ""
            rel = str(item.get("rel") or item.get("label") or name)
            hay = f"{name} {rel}".lower()
            if needle and needle not in hay:
                continue
            if mode != "Model" and want is not None:
                guessed = item.get("scale")
                if guessed is None:
                    guessed = guess_scale_from_name(name)
                if guessed is not None and abs(float(guessed) - float(want)) > 0.05:
                    continue
            filtered.append(item)

        self._filtered = filtered
        current = self.model_var.get()
        self._listbox.delete(0, tk.END)
        select_idx = 0
        for i, item in enumerate(filtered):
            rel = self._model_key(item)
            label = rel
            scale = item.get("scale")
            if scale:
                label = f"{label}   [{scale:g}×]"
            self._listbox.insert(tk.END, label)
            if (
                rel == current
                or item.get("filename") == current
                or item.get("path") == current
            ):
                select_idx = i
        total = len(self._all_models)
        shown = len(filtered)
        self._count_var.set(f"{shown}/{total}")

        if filtered:
            self._listbox.selection_clear(0, tk.END)
            self._listbox.selection_set(select_idx)
            self._listbox.see(select_idx)
            self.model_var.set(self._model_key(filtered[select_idx]))
        else:
            if not self._all_models:
                self.model_var.set("(no models in folder)")
            else:
                self.model_var.set("(no match)")

        self._update_action_states()

    def _refresh_status(self) -> None:
        n = len(self.paths)
        models_dir = self._models_dir()
        self.models_dir_var.set(models_dir)
        rt = spandrel_runtime_status(deep=False)
        self._runtime_ok = bool(rt.get("ready"))
        wt = spandrel_weights_status(models_dir=models_dir)
        self._all_models = wt.get("models") or list_spandrel_models(models_dir)

        parts = [f"{n} image(s)", "Engine: Spandrel"]
        if not self._runtime_ok:
            parts.append("⚠ " + (rt.get("message") or "Runtime not ready."))
            parts.append("Use Install spandrel… (same as BiRefNet pip into app venv).")
        elif not self._all_models:
            parts.append(wt.get("message") or "No models found.")
        else:
            parts.append(f"{len(self._all_models)} model(s) in folder (incl. subfolders)")
            parts.append(f"Browse: {OPENMODELDB_URL}")
        self.status_var.set("\n".join(parts))
        self._apply_filter()
        # Status box height can change after model scan — keep Output/Suffix visible.
        self.after(30, self._fit_and_center)

    def _start(self) -> None:
        if self._install_running or not self._ready:
            return
        out_dir = (self.out_dir_var.get() or "").strip()
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showwarning(
                "Fast Upscale",
                "Choose a valid output folder.",
                parent=self,
            )
            return

        rt = spandrel_runtime_status(deep=True)
        if not rt.get("ready") and not rt.get("cpu_fallback"):
            if rt.get("error") == "spandrel_missing":
                if messagebox.askyesno(
                    "Fast Upscale",
                    (
                        "Python package 'spandrel' is not installed.\n\n"
                        "Install it now into the app environment?"
                    ),
                    parent=self,
                ):
                    self._install_spandrel()
                return
            messagebox.showwarning(
                "Fast Upscale",
                rt.get("message") or "Spandrel / PyTorch runtime is not ready.",
                parent=self,
            )
            return

        model = self.model_var.get()
        if not model or model.startswith("("):
            messagebox.showwarning(
                "Fast Upscale",
                "Select a model from the list (use Filter if the folder is large).",
                parent=self,
            )
            return

        mode = self.factor_var.get()
        target = self._target_scale()
        if mode == "Custom" and target is None:
            messagebox.showwarning(
                "Fast Upscale",
                "Enter a valid custom scale (e.g. 3 or 1.5).",
                parent=self,
            )
            return

        suffix = (self.suffix_var.get() or "").strip()
        gpu_label = self.gpu_var.get()
        cuda = self._gpu_indices.get(gpu_label, "0")
        models_dir = self._models_dir()
        tiling = bool(self.tiling_var.get())
        tile_size = int(self.tile_var.get() or 512) if tiling else 0
        options = {
            "backend_id": "spandrel",
            "output_dir": out_dir,
            "model": model,
            "models_dir": models_dir,
            "tile_size": tile_size,
            "overlap": 32 if tiling else 0,
            "cuda_device": cuda,
            "target_scale": target,
            "suffix": suffix or "_spandrel",
            "output_format": "png",
            "unload_after": True,
        }

        if self.controller is not None:
            try:
                setattr(self.controller, "fast_upscale_backend", "spandrel")
                setattr(self.controller, "spandrel_models_dir", models_dir)
                setattr(self.controller, "spandrel_model", options["model"])
                setattr(self.controller, "spandrel_tiling", tiling)
                setattr(
                    self.controller,
                    "spandrel_tile_size",
                    int(self.tile_var.get() or 512),
                )
                setattr(self.controller, "spandrel_cuda_device", options["cuda_device"])
                setattr(self.controller, "spandrel_scale_mode", mode)
                setattr(self.controller, "spandrel_model_filter", self.filter_var.get())
                if mode == "Custom" and target is not None:
                    setattr(self.controller, "spandrel_custom_scale", target)
                if suffix:
                    setattr(self.controller, "fast_upscale_suffix", suffix)
            except Exception:
                pass

        self.result = {"paths": list(self.paths), "options": options}
        if callable(self.on_confirm):
            self.on_confirm(list(self.paths), options)
        self.destroy()
