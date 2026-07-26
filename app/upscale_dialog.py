"""
upscale_dialog.py — Options dialog for offline AI upscale backends.
"""

from __future__ import annotations

import os
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from seedvr2_config import (
    DEFAULT_DIT_MODEL,
    KEY_CUDA_DEVICE,
    KEY_DIT_MODEL,
    KEY_KEEP_VRAM,
    KEY_PRESCALE_CUSTOM,
    KEY_PRESCALE_MODE,
    KEY_RUNNER_DIR,
    KEY_WEIGHTS_DIR,
    PRESCALE_MODE_CUSTOM,
    PRESCALE_MODE_LABELS,
    PRESCALE_MODE_OFF,
    default_weights_dir,
    list_cuda_gpus,
    list_dit_models,
    load_seedvr2_settings,
    resolve_prescale_long_edge,
    save_seedvr2_settings,
)


UPSCALE_DIALOG_WIDTH = 620
UPSCALE_DIALOG_HEIGHT = 680


class UpscaleOptionsDialog(ctk.CTkToplevel):
    """Pick backend, scale, paths, and output folder for offline upscale."""

    def __init__(self, parent, backends, default_paths, on_confirm, controller=None):
        super().__init__(parent)
        self.title("Upscale")
        self.controller = controller
        self.backends = list(backends or [])
        self.paths = list(default_paths or [])
        self.on_confirm = on_confirm
        self.result = None

        self.geometry(f"{UPSCALE_DIALOG_WIDTH}x{UPSCALE_DIALOG_HEIGHT}")
        self.resizable(True, True)
        self.minsize(480, 420)
        self.transient(parent)
        self.grab_set()

        if not self.backends:
            messagebox.showerror("Upscale", "No upscale backends are available.", parent=self)
            self.after(10, self.destroy)
            return

        cfg = load_seedvr2_settings()
        names = [getattr(b, "name", getattr(b, "id", "Upscaler")) for b in self.backends]
        self.backend_var = ctk.StringVar(value=names[0])
        self.scale_var = ctk.StringVar(value="2")
        first_dir = os.path.dirname(self.paths[0]) if self.paths else ""
        self.output_dir_var = ctk.StringVar(value=first_dir)
        self.weights_dir_var = ctk.StringVar(value=cfg.get(KEY_WEIGHTS_DIR) or default_weights_dir())
        self.runner_dir_var = ctk.StringVar(value=cfg.get(KEY_RUNNER_DIR) or "")
        self.status_var = ctk.StringVar(value="")

        self._gpu_list = list_cuda_gpus()
        self._gpu_labels = [g["label"] for g in self._gpu_list]
        saved_cuda = str(cfg.get(KEY_CUDA_DEVICE) or "0").strip()
        self._gpu_label_by_index = {str(g["index"]): g["label"] for g in self._gpu_list}
        initial_gpu = self._gpu_label_by_index.get(saved_cuda, self._gpu_labels[0] if self._gpu_labels else "cuda:0")
        self.gpu_var = ctk.StringVar(value=initial_gpu)

        ctk.CTkLabel(self, text="Offline AI upscale", text_color="#00bfff").pack(pady=(12, 4))
        n = len(self.paths)
        ctk.CTkLabel(
            self,
            text=f"{n} file{'s' if n != 1 else ''} selected",
            text_color="#aaaaaa",
        ).pack(pady=(0, 8))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=16, pady=4)

        def _path_row(row: int, label: str, variable: ctk.StringVar, browse_cmd):
            ctk.CTkLabel(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            row_fr = ctk.CTkFrame(form, fg_color="transparent")
            row_fr.grid(row=row, column=1, sticky="ew", pady=4, padx=(8, 0))
            ctk.CTkEntry(row_fr, textvariable=variable, width=280).pack(
                side="left", fill="x", expand=True
            )
            ctk.CTkButton(row_fr, text="…", width=36, command=browse_cmd).pack(
                side="left", padx=(6, 0)
            )

        ctk.CTkLabel(form, text="Backend").grid(row=0, column=0, sticky="w", pady=4)
        self.backend_menu = ctk.CTkOptionMenu(
            form,
            variable=self.backend_var,
            values=names,
            command=lambda _v: self._refresh_status(),
            width=320,
        )
        self.backend_menu.grid(row=0, column=1, sticky="ew", pady=4, padx=(8, 0))

        ctk.CTkLabel(form, text="GPU").grid(row=1, column=0, sticky="w", pady=4)
        self.gpu_menu = ctk.CTkOptionMenu(
            form,
            variable=self.gpu_var,
            values=self._gpu_labels or ["cuda:0"],
            width=320,
        )
        self.gpu_menu.grid(row=1, column=1, sticky="ew", pady=4, padx=(8, 0))

        ctk.CTkLabel(form, text="Model").grid(row=2, column=0, sticky="w", pady=4)
        self.model_var = ctk.StringVar(value="")
        self.model_menu = ctk.CTkOptionMenu(
            form,
            variable=self.model_var,
            values=["(no models found)"],
            width=420,
        )
        self.model_menu.grid(row=2, column=1, sticky="ew", pady=4, padx=(8, 0))
        self._reload_model_list(prefer=cfg.get(KEY_DIT_MODEL) or DEFAULT_DIT_MODEL)

        ctk.CTkLabel(form, text="Scale").grid(row=3, column=0, sticky="w", pady=4)
        ctk.CTkOptionMenu(
            form,
            variable=self.scale_var,
            values=["2", "4"],
            width=320,
        ).grid(row=3, column=1, sticky="ew", pady=4, padx=(8, 0))

        # Prescale: downscale long edge before SeedVR (clears soft/compressed detail).
        saved_mode = str(cfg.get(KEY_PRESCALE_MODE) or PRESCALE_MODE_OFF).lower()
        label_by_mode = {mode: label for label, mode in PRESCALE_MODE_LABELS}
        mode_by_label = {label: mode for label, mode in PRESCALE_MODE_LABELS}
        self._prescale_mode_by_label = mode_by_label
        self._prescale_label_by_mode = label_by_mode
        initial_prescale = label_by_mode.get(
            saved_mode, label_by_mode[PRESCALE_MODE_OFF]
        )
        self.prescale_var = ctk.StringVar(value=initial_prescale)
        self.prescale_custom_var = ctk.StringVar(
            value=str(int(cfg.get(KEY_PRESCALE_CUSTOM) or 1280))
        )

        ctk.CTkLabel(form, text="Prescale").grid(row=4, column=0, sticky="w", pady=4)
        ctk.CTkOptionMenu(
            form,
            variable=self.prescale_var,
            values=[label for label, _mode in PRESCALE_MODE_LABELS],
            command=lambda _v: self._on_prescale_mode_changed(),
            width=320,
        ).grid(row=4, column=1, sticky="ew", pady=4, padx=(8, 0))

        self.prescale_custom_row = ctk.CTkFrame(form, fg_color="transparent")
        self.prescale_custom_row.grid(row=5, column=0, columnspan=2, sticky="ew", pady=2)
        ctk.CTkLabel(self.prescale_custom_row, text="Long edge (px)").pack(
            side="left", padx=(0, 8)
        )
        self.prescale_custom_entry = ctk.CTkEntry(
            self.prescale_custom_row,
            textvariable=self.prescale_custom_var,
            width=100,
        )
        self.prescale_custom_entry.pack(side="left")
        ctk.CTkLabel(
            self.prescale_custom_row,
            text="Downscale only if larger",
            text_color="#888888",
        ).pack(side="left", padx=(10, 0))

        _path_row(6, "Output folder", self.output_dir_var, self._browse_output)
        _path_row(7, "Weights folder", self.weights_dir_var, self._browse_weights)
        _path_row(8, "Runner folder", self.runner_dir_var, self._browse_runner)

        self.keep_vram_var = ctk.BooleanVar(value=bool(cfg.get(KEY_KEEP_VRAM)))
        ctk.CTkCheckBox(
            form,
            text="Keep model in VRAM (until app exit)",
            variable=self.keep_vram_var,
        ).grid(row=9, column=0, columnspan=2, sticky="w", pady=(8, 4))

        form.grid_columnconfigure(1, weight=1)
        self._on_prescale_mode_changed()

        ctk.CTkLabel(
            self,
            textvariable=self.status_var,
            wraplength=480,
            justify="left",
            text_color="#cccccc",
        ).pack(fill="x", padx=16, pady=(10, 4))

        link_row = ctk.CTkFrame(self, fg_color="transparent")
        link_row.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkButton(
            link_row,
            text="Open weights folder",
            width=150,
            command=self._open_weights_folder,
        ).pack(side="left")
        ctk.CTkButton(
            link_row,
            text="Download weights…",
            width=150,
            command=self._open_download_url,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            link_row,
            text="Get runner…",
            width=120,
            command=self._open_runner_url,
        ).pack(side="left", padx=(8, 0))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(side="bottom", fill="x", padx=16, pady=12)
        ctk.CTkButton(buttons, text="Cancel", width=100, command=self.destroy).pack(
            side="right"
        )
        ctk.CTkButton(buttons, text="Start", width=100, command=self._confirm).pack(
            side="right", padx=(0, 8)
        )
        ctk.CTkButton(
            buttons, text="Save paths", width=100, command=self._save_paths_only
        ).pack(side="left")

        self.weights_dir_var.trace_add("write", lambda *_: self._on_paths_changed())
        self.runner_dir_var.trace_add("write", lambda *_: self._on_paths_changed())
        self._refresh_status()
        self.after(50, self._center_on_parent)

    def _selected_prescale_mode(self) -> str:
        label = (self.prescale_var.get() or "").strip()
        return self._prescale_mode_by_label.get(label, PRESCALE_MODE_OFF)

    def _selected_prescale_custom(self) -> int:
        try:
            return max(256, min(8192, int(float(self.prescale_custom_var.get().strip()))))
        except (TypeError, ValueError):
            return 1280

    def _on_prescale_mode_changed(self, *_args):
        is_custom = self._selected_prescale_mode() == PRESCALE_MODE_CUSTOM
        try:
            if is_custom:
                self.prescale_custom_row.grid()
            else:
                self.prescale_custom_row.grid_remove()
        except Exception:
            pass

    def _selected_backend(self):
        label = self.backend_var.get()
        for backend in self.backends:
            if getattr(backend, "name", None) == label or getattr(backend, "id", None) == label:
                return backend
        return self.backends[0]

    def _reload_model_list(self, prefer: str | None = None):
        weights = (self.weights_dir_var.get() or "").strip() or default_weights_dir()
        models = list_dit_models(weights)
        self._dit_models = models
        self._dit_label_to_file = {m["label"]: m["filename"] for m in models}
        self._dit_file_to_label = {m["filename"]: m["label"] for m in models}
        labels = [m["label"] for m in models] or ["(no DiT models in weights folder)"]
        prefer = prefer or (self._selected_dit_filename() if hasattr(self, "model_var") else "")
        if prefer and prefer in self._dit_file_to_label:
            initial = self._dit_file_to_label[prefer]
        else:
            initial = labels[0]
        self.model_menu.configure(values=labels)
        self.model_var.set(initial)

    def _selected_dit_filename(self) -> str:
        label = (self.model_var.get() or "").strip()
        mapped = getattr(self, "_dit_label_to_file", {}).get(label)
        if mapped:
            return mapped
        # Already a raw filename
        if label and not label.startswith("("):
            return label
        return DEFAULT_DIT_MODEL

    def _apply_paths_to_backend(self, backend):
        """Push dialog paths into the live backend instance."""
        weights = (self.weights_dir_var.get() or "").strip() or default_weights_dir()
        runner = (self.runner_dir_var.get() or "").strip()
        backend.weights_dir = Path(weights)
        if hasattr(backend, "runner_dir"):
            backend.runner_dir = runner
        if hasattr(backend, "dit_model"):
            backend.dit_model = self._selected_dit_filename()

    def _on_paths_changed(self):
        self._reload_model_list(prefer=self._selected_dit_filename())
        self._refresh_status()

    def _refresh_status(self):
        backend = self._selected_backend()
        self._apply_paths_to_backend(backend)
        runtime = backend.runtime_status() if hasattr(backend, "runtime_status") else {"ready": True}
        weights = backend.weights_status() if hasattr(backend, "weights_status") else {"ready": True}
        runner = backend.runner_status() if hasattr(backend, "runner_status") else {"ready": True}
        dit = self._selected_dit_filename()
        parts = []
        if not runtime.get("ready"):
            parts.append(runtime.get("message") or "GPU pack missing.")
        if not weights.get("ready"):
            parts.append(weights.get("message") or "Weights missing.")
            if weights.get("path"):
                parts.append(f"Weights: {weights.get('path')}")
        elif weights.get("path"):
            parts.append(f"Weights OK: {weights.get('path')}")
        if dit and not dit.startswith("("):
            parts.append(f"Model: {dit}")
        if not runner.get("ready"):
            parts.append(runner.get("message") or "Runner missing.")
        elif runner.get("cli"):
            parts.append(f"Runner OK: {runner.get('cli')}")
        if runtime.get("ready") and weights.get("ready") and runner.get("ready"):
            parts.append("Ready to start.")
        self.status_var.set("\n".join(parts))

    def _browse_output(self):
        initial = self.output_dir_var.get() or None
        chosen = filedialog.askdirectory(parent=self, initialdir=initial, title="Output folder")
        if chosen:
            self.output_dir_var.set(chosen)

    def _browse_weights(self):
        initial = self.weights_dir_var.get() or default_weights_dir()
        chosen = filedialog.askdirectory(parent=self, initialdir=initial, title="SeedVR2 weights folder")
        if chosen:
            self.weights_dir_var.set(chosen)
            self._reload_model_list()
            self._save_paths_only(quiet=True)
            self._refresh_status()

    def _browse_runner(self):
        initial = self.runner_dir_var.get() or None
        chosen = filedialog.askdirectory(
            parent=self,
            initialdir=initial,
            title="SeedVR2 runner folder (inference_cli.py)",
        )
        if chosen:
            self.runner_dir_var.set(chosen)
            self._save_paths_only(quiet=True)
            self._refresh_status()

    def _selected_cuda_index(self) -> str:
        label = (self.gpu_var.get() or "").strip()
        for gpu in getattr(self, "_gpu_list", []):
            if gpu.get("label") == label:
                return str(gpu.get("index", 0))
        if label.lower().startswith("cuda:"):
            try:
                return str(int(label.split(":", 1)[1].split()[0]))
            except ValueError:
                pass
        return "0"

    def _save_paths_only(self, quiet: bool = False):
        weights = (self.weights_dir_var.get() or "").strip() or default_weights_dir()
        runner = (self.runner_dir_var.get() or "").strip()
        cuda = self._selected_cuda_index()
        dit = self._selected_dit_filename()
        save_seedvr2_settings(
            weights_dir=weights,
            runner_dir=runner,
            cuda_device=cuda,
            dit_model=dit,
            keep_vram=bool(self.keep_vram_var.get()),
            prescale_mode=self._selected_prescale_mode(),
            prescale_custom=self._selected_prescale_custom(),
        )
        if self.controller is not None:
            setattr(self.controller, "seedvr2_weights_dir", weights)
            setattr(self.controller, "seedvr2_runner_dir", runner)
            setattr(self.controller, "seedvr2_cuda_device", cuda)
            setattr(self.controller, "seedvr2_dit_model", dit)
            setattr(self.controller, "seedvr2_keep_vram", bool(self.keep_vram_var.get()))
            setattr(self.controller, "seedvr2_prescale_mode", self._selected_prescale_mode())
            setattr(self.controller, "seedvr2_prescale_custom", self._selected_prescale_custom())
            # Turning keep-VRAM off → stop persistent worker and free GPU memory.
            if not self.keep_vram_var.get():
                try:
                    from seedvr2_worker_host import shutdown_seedvr2_worker_host

                    shutdown_seedvr2_worker_host()
                except Exception:
                    pass
        backend = self._selected_backend()
        self._apply_paths_to_backend(backend)
        if not quiet:
            messagebox.showinfo("Upscale", "Settings saved.", parent=self)
        self._refresh_status()

    def _open_weights_folder(self):
        path = (self.weights_dir_var.get() or "").strip() or default_weights_dir()
        os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)
        except Exception as exc:
            messagebox.showerror("Upscale", f"Cannot open folder:\n{exc}", parent=self)

    def _open_download_url(self):
        backend = self._selected_backend()
        weights = backend.weights_status() if hasattr(backend, "weights_status") else {}
        url = weights.get("download_url")
        if not url:
            messagebox.showinfo("Upscale", "No download URL for this backend.", parent=self)
            return
        webbrowser.open(url)

    def _open_runner_url(self):
        backend = self._selected_backend()
        runner = backend.runner_status() if hasattr(backend, "runner_status") else {}
        url = runner.get("download_url") or "https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler"
        webbrowser.open(url)

    def _confirm(self):
        backend = self._selected_backend()
        self._save_paths_only(quiet=True)
        self._apply_paths_to_backend(backend)

        runtime = backend.runtime_status() if hasattr(backend, "runtime_status") else {"ready": True}
        if not runtime.get("ready"):
            messagebox.showwarning(
                "Upscale",
                runtime.get("message") or "GPU pack / runtime not available.",
                parent=self,
            )
            return
        weights = backend.weights_status() if hasattr(backend, "weights_status") else {"ready": True}
        if not weights.get("ready"):
            messagebox.showwarning(
                "Upscale",
                weights.get("message") or "Weights not found.",
                parent=self,
            )
            return
        dit = self._selected_dit_filename()
        weights_root = Path((self.weights_dir_var.get() or "").strip() or default_weights_dir())
        if not (weights_root / dit).is_file():
            messagebox.showwarning(
                "Upscale",
                f"Selected model not found:\n{weights_root / dit}",
                parent=self,
            )
            return
        runner = backend.runner_status() if hasattr(backend, "runner_status") else {"ready": True}
        if not runner.get("ready"):
            messagebox.showwarning(
                "Upscale",
                runner.get("message") or "Runner not configured.",
                parent=self,
            )
            return

        try:
            scale = int(self.scale_var.get())
        except ValueError:
            scale = 2
        mode = self._selected_prescale_mode()
        custom_px = self._selected_prescale_custom()
        if mode == PRESCALE_MODE_CUSTOM:
            raw = (self.prescale_custom_var.get() or "").strip()
            try:
                custom_px = max(256, min(8192, int(float(raw))))
            except (TypeError, ValueError):
                messagebox.showwarning(
                    "Upscale",
                    "Custom prescale long edge must be a number (256–8192).",
                    parent=self,
                )
                return
            self.prescale_custom_var.set(str(custom_px))
        long_edge = resolve_prescale_long_edge(mode, custom_px)
        out_dir = (self.output_dir_var.get() or "").strip() or None
        cuda = self._selected_cuda_index()
        self.result = {
            "backend_id": getattr(backend, "id", "upscale"),
            "paths": list(self.paths),
            "options": {
                "scale": scale,
                "output_dir": out_dir,
                "suffix": f"_{getattr(backend, 'id', 'upscale')}",
                "cuda_device": cuda,
                "dit_model": dit,
                "keep_vram": bool(self.keep_vram_var.get()),
                "prescale_mode": mode,
                "prescale_custom": custom_px,
                "prescale_long_edge": long_edge,
            },
        }
        if callable(self.on_confirm):
            self.on_confirm(self.result)
        self.destroy()

    def _center_on_parent(self):
        try:
            self.update_idletasks()
            parent = self.master
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            w = self.winfo_width()
            h = self.winfo_height()
            x = px + max(0, (pw - w) // 2)
            y = py + max(0, (ph - h) // 2)
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass
