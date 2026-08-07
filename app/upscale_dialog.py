"""
upscale_dialog.py — Options dialog for offline AI upscale backends.

Basic view: scale / prescale / save format (image) or MP4 (video) / output folder.
Advanced view: backend, GPU, model, VRAM, batch / overlap / chunk / VAE tiles, paths.
"""

from __future__ import annotations

import os
import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from seedvr2_config import (
    BATCH_SIZE_AUTO,
    BATCH_SIZE_LABELS,
    CHUNK_SIZE_AUTO,
    CHUNK_SIZE_LABELS,
    DEFAULT_DIT_MODEL,
    KEY_ADVANCED_OPEN,
    KEY_BATCH_SIZE,
    KEY_CHUNK_SIZE,
    KEY_CUDA_DEVICE,
    KEY_DIT_MODEL,
    KEY_KEEP_VRAM,
    KEY_OUTPUT_FORMAT,
    KEY_PRESCALE_CUSTOM,
    KEY_PRESCALE_MODE,
    KEY_RUNNER_DIR,
    KEY_TEMPORAL_OVERLAP,
    KEY_UNIFORM_BATCH,
    KEY_VAE_DECODE_TILE,
    KEY_VAE_ENCODE_TILE,
    KEY_VAE_TILED,
    KEY_WEIGHTS_DIR,
    OUTPUT_FORMAT_JPEG,
    OUTPUT_FORMAT_LABELS,
    OUTPUT_FORMAT_PNG,
    PRESCALE_MODE_CUSTOM,
    PRESCALE_MODE_LABELS,
    PRESCALE_MODE_OFF,
    TEMPORAL_OVERLAP_DEFAULT,
    VAE_DECODE_TILE_DEFAULT,
    VAE_ENCODE_TILE_DEFAULT,
    VAE_TILE_CHOICES,
    default_setup_runner_dir,
    default_weights_dir,
    list_cuda_gpus,
    list_dit_models,
    load_seedvr2_settings,
    resolve_prescale_long_edge,
    save_seedvr2_settings,
)
from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS


UPSCALE_DIALOG_WIDTH = 640
UPSCALE_BASIC_HEIGHT = 380
UPSCALE_ADVANCED_EXTRA = 520
UPSCALE_ADVANCED_EXTRA_VIDEO = 620


class SeedVR2RunnerInstallDialog(ctk.CTkToplevel):
    """Explain-then-confirm dialog before downloading the SeedVR2 runner."""

    _WIDTH = 560
    _HEIGHT = 620

    def __init__(self, parent, initial_dir: str):
        super().__init__(parent)
        self.title("Install SeedVR2 runner")
        self.result_path: str | None = None
        self._path = (initial_dir or "").strip() or default_setup_runner_dir()

        # Size first — CTk reqheight is unreliable before the window is mapped,
        # and locking a tiny geometry crops all content under the title.
        self.geometry(f"{self._WIDTH}x{self._HEIGHT}")
        self.minsize(self._WIDTH, 520)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        from seedvr2_config import COMFY_REPO_URL
        from seedvr2_runner_setup import (
            SEEDVR2_RUNNER_REF,
            SEEDVR2_RUNNER_REPO,
            SEEDVR2_SETUP_DISK_ESTIMATE,
            SEEDVR2_TORCH_INDEX,
        )

        # Buttons first (bottom) so they stay visible even if content is tall.
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=18, pady=(8, 16))
        ctk.CTkButton(
            btn_row,
            text="Cancel",
            width=110,
            fg_color=("gray75", "#3a3a3a"),
            hover_color=("gray65", "#4a4a4a"),
            command=self._cancel,
        ).pack(side="right")
        ctk.CTkButton(
            btn_row,
            text="Install",
            width=120,
            command=self._install,
        ).pack(side="right", padx=(0, 8))

        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(side="top", fill="both", expand=True, padx=4, pady=(4, 0))

        ctk.CTkLabel(
            scroll,
            text="Install SeedVR2 runner",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#00bfff",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 6))

        ctk.CTkLabel(
            scroll,
            text=(
                "This installs the offline engine used by SeedVR 2 Upscale "
                "(image/video). Vibe Player will call it locally — nothing is "
                "uploaded."
            ),
            wraplength=500,
            justify="left",
            anchor="w",
            text_color="#dddddd",
        ).pack(fill="x", padx=14, pady=(0, 12))

        body = ctk.CTkFrame(scroll, fg_color=("gray90", "#2a2a2a"), corner_radius=8)
        body.pack(fill="x", padx=14, pady=(0, 12))

        rows = [
            ("What", f"ComfyUI-SeedVR2 CLI ({SEEDVR2_RUNNER_REF}) + Python .venv"),
            ("Includes", "PyTorch (CUDA) and SeedVR2 Python dependencies"),
            ("From", f"GitHub: {SEEDVR2_RUNNER_REPO}"),
            ("Also from", "PyTorch CUDA wheels (download.pytorch.org)"),
            ("Disk space", f"About {SEEDVR2_SETUP_DISK_ESTIMATE} (mostly PyTorch)"),
            ("Time", "Several minutes on a typical connection"),
            ("Network", "Required for the download"),
        ]
        for i, (label, value) in enumerate(rows):
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(
                fill="x",
                padx=12,
                pady=(10 if i == 0 else 4, 4 if i < len(rows) - 1 else 10),
            )
            ctk.CTkLabel(
                row,
                text=label,
                width=88,
                anchor="nw",
                text_color="#888888",
                font=ctk.CTkFont(size=12, weight="bold"),
            ).pack(side="left", anchor="n")
            ctk.CTkLabel(
                row,
                text=value,
                wraplength=380,
                justify="left",
                anchor="w",
                text_color="#eeeeee",
            ).pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            scroll,
            text="Install location",
            anchor="w",
            text_color="#aaaaaa",
        ).pack(fill="x", padx=14, pady=(0, 4))

        path_row = ctk.CTkFrame(scroll, fg_color="transparent")
        path_row.pack(fill="x", padx=14, pady=(0, 6))
        self.path_var = ctk.StringVar(value=self._path)
        self.path_entry = ctk.CTkEntry(path_row, textvariable=self.path_var)
        self.path_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            path_row, text="Change…", width=90, command=self._change_folder
        ).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(
            scroll,
            text=f"Source: {COMFY_REPO_URL}\nTorch index: {SEEDVR2_TORCH_INDEX}",
            wraplength=500,
            justify="left",
            anchor="w",
            text_color="#777777",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=14, pady=(4, 16))

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.after(20, self._center_on_parent)
        try:
            self.focus_force()
        except Exception:
            pass

    def _center_on_parent(self):
        try:
            self.update_idletasks()
            self.geometry(f"{self._WIDTH}x{self._HEIGHT}")
            parent = self.master
            if parent is not None and parent.winfo_exists():
                px = parent.winfo_rootx()
                py = parent.winfo_rooty()
                pw = parent.winfo_width()
                ph = parent.winfo_height()
                x = px + max(0, (pw - self._WIDTH) // 2)
                y = py + max(0, (ph - self._HEIGHT) // 2)
                self.geometry(f"{self._WIDTH}x{self._HEIGHT}+{x}+{y}")
        except Exception:
            pass

    def _change_folder(self):
        current = (self.path_var.get() or "").strip() or default_setup_runner_dir()
        try:
            os.makedirs(current, exist_ok=True)
        except OSError:
            current = default_setup_runner_dir()
        chosen = filedialog.askdirectory(
            parent=self,
            initialdir=current,
            title="Choose folder for SeedVR2 runner",
        )
        if chosen:
            self.path_var.set(chosen)

    def _cancel(self):
        self.result_path = None
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _install(self):
        path = (self.path_var.get() or "").strip()
        if not path:
            messagebox.showwarning(
                "Install SeedVR2 runner",
                "Choose an install location first.",
                parent=self,
            )
            return
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "Install SeedVR2 runner",
                f"Cannot create folder:\n{path}\n\n{exc}",
                parent=self,
            )
            return
        self.result_path = path
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def show_and_wait(self) -> str | None:
        """Block until closed; return chosen path or None if cancelled."""
        self.wait_window(self)
        return self.result_path


class UpscaleOptionsDialog(ctk.CTkToplevel):
    """Pick scale / output options; advanced SeedVR paths live behind a toggle."""

    def __init__(self, parent, backends, default_paths, on_confirm, controller=None):
        super().__init__(parent)
        self.title("Upscale")
        self.controller = controller
        self.backends = list(backends or [])
        self.paths = list(default_paths or [])
        self.on_confirm = on_confirm
        self.result = None
        self._advanced_open = False

        self.resizable(True, True)
        self.minsize(480, 320)
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
        initial_gpu = self._gpu_label_by_index.get(
            saved_cuda, self._gpu_labels[0] if self._gpu_labels else "cuda:0"
        )
        self.gpu_var = ctk.StringVar(value=initial_gpu)

        n = len(self.paths)
        self._n_img = sum(
            1 for p in self.paths if os.path.splitext(p)[1].lower() in IMAGE_FORMATS
        )
        self._n_vid = sum(
            1 for p in self.paths if os.path.splitext(p)[1].lower() in VIDEO_FORMATS
        )
        # Fallback: anything not image counts as video-ish for UI.
        if self._n_img + self._n_vid < n:
            self._n_vid = n - self._n_img
        self._has_video = self._n_vid > 0
        self._has_image = self._n_img > 0
        self._video_only = self._has_video and not self._has_image
        if n <= 1:
            header = "SeedVR 2 Upscale — Single File"
            kind = "video" if self._n_vid else "image"
            subtitle = f"1 {kind} selected" if n == 1 else "No files selected"
        else:
            header = f"SeedVR 2 Upscale — Batch Mode ({n} items selected)"
            parts = []
            if self._n_img:
                parts.append(f"{self._n_img} image(s)")
            if self._n_vid:
                parts.append(f"{self._n_vid} video(s)")
            subtitle = ", ".join(parts) if parts else f"{n} files selected"
            if self._n_vid:
                subtitle += " — video jobs can take a long time"
        self.title(header)
        ctk.CTkLabel(self, text=header, text_color="#00bfff").pack(pady=(12, 4))
        ctk.CTkLabel(self, text=subtitle, text_color="#aaaaaa").pack(pady=(0, 8))

        # --- Basic options (always visible) ---
        basic = ctk.CTkFrame(self, fg_color="transparent")
        basic.pack(fill="x", padx=16, pady=4)
        basic.grid_columnconfigure(1, weight=1)
        self._basic = basic

        def _path_row(parent, row: int, label: str, variable: ctk.StringVar, browse_cmd):
            ctk.CTkLabel(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
            row_fr = ctk.CTkFrame(parent, fg_color="transparent")
            row_fr.grid(row=row, column=1, sticky="ew", pady=4, padx=(8, 0))
            ctk.CTkEntry(row_fr, textvariable=variable, width=280).pack(
                side="left", fill="x", expand=True
            )
            ctk.CTkButton(row_fr, text="…", width=36, command=browse_cmd).pack(
                side="left", padx=(6, 0)
            )

        ctk.CTkLabel(basic, text="Scale").grid(row=0, column=0, sticky="w", pady=4)
        ctk.CTkOptionMenu(
            basic, variable=self.scale_var, values=["2", "4"], width=320
        ).grid(row=0, column=1, sticky="ew", pady=4, padx=(8, 0))

        saved_mode = str(cfg.get(KEY_PRESCALE_MODE) or PRESCALE_MODE_OFF).lower()
        label_by_mode = {mode: label for label, mode in PRESCALE_MODE_LABELS}
        mode_by_label = {label: mode for label, mode in PRESCALE_MODE_LABELS}
        self._prescale_mode_by_label = mode_by_label
        self._prescale_label_by_mode = label_by_mode
        self.prescale_var = ctk.StringVar(
            value=label_by_mode.get(saved_mode, label_by_mode[PRESCALE_MODE_OFF])
        )
        self.prescale_custom_var = ctk.StringVar(
            value=str(int(cfg.get(KEY_PRESCALE_CUSTOM) or 1280))
        )

        ctk.CTkLabel(basic, text="Prescale").grid(row=1, column=0, sticky="w", pady=4)
        ctk.CTkOptionMenu(
            basic,
            variable=self.prescale_var,
            values=[label for label, _mode in PRESCALE_MODE_LABELS],
            command=lambda _v: self._on_prescale_mode_changed(),
            width=320,
        ).grid(row=1, column=1, sticky="ew", pady=4, padx=(8, 0))

        self.prescale_custom_row = ctk.CTkFrame(basic, fg_color="transparent")
        self.prescale_custom_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=2)
        ctk.CTkLabel(self.prescale_custom_row, text="Long edge (px)").pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkEntry(
            self.prescale_custom_row,
            textvariable=self.prescale_custom_var,
            width=100,
        ).pack(side="left")
        ctk.CTkLabel(
            self.prescale_custom_row,
            text="Downscale only if larger",
            text_color="#888888",
        ).pack(side="left", padx=(10, 0))

        saved_fmt = str(cfg.get(KEY_OUTPUT_FORMAT) or OUTPUT_FORMAT_PNG).lower()
        if saved_fmt in ("jpeg", OUTPUT_FORMAT_JPEG):
            saved_fmt = OUTPUT_FORMAT_JPEG
        else:
            saved_fmt = OUTPUT_FORMAT_PNG
        self._fmt_label_by_mode = {mode: label for label, mode in OUTPUT_FORMAT_LABELS}
        self._fmt_mode_by_label = {label: mode for label, mode in OUTPUT_FORMAT_LABELS}
        self.output_format_var = ctk.StringVar(
            value=self._fmt_label_by_mode.get(
                saved_fmt, self._fmt_label_by_mode[OUTPUT_FORMAT_PNG]
            )
        )

        # Save-as adapts: images → PNG/JPEG; videos → MP4; mixed → both.
        self._save_as_label = ctk.CTkLabel(basic, text="Save as")
        self._save_as_label.grid(row=3, column=0, sticky="w", pady=4)
        self._save_as_image_menu = ctk.CTkOptionMenu(
            basic,
            variable=self.output_format_var,
            values=[label for label, _m in OUTPUT_FORMAT_LABELS],
            width=320,
        )
        self._save_as_video_label = ctk.CTkLabel(
            basic,
            text="MP4 (video output)",
            text_color="#cccccc",
            anchor="w",
        )
        self._save_as_mixed_label = ctk.CTkLabel(
            basic,
            text="Images: format below · Videos: always MP4",
            text_color="#888888",
            anchor="w",
        )
        self._layout_save_as_row()

        _path_row(basic, 4, "Output folder", self.output_dir_var, self._browse_output)
        self._on_prescale_mode_changed()

        # --- Action bar ---
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(side="bottom", fill="x", padx=16, pady=12)
        ctk.CTkButton(buttons, text="Cancel", width=100, command=self._on_close).pack(
            side="right"
        )
        ctk.CTkButton(buttons, text="Start", width=100, command=self._confirm).pack(
            side="right", padx=(0, 8)
        )
        self.advanced_btn = ctk.CTkButton(
            buttons,
            text="Advanced Settings ▼",
            width=160,
            command=self._toggle_advanced,
        )
        self.advanced_btn.pack(side="left")

        # --- Collapsible advanced panel ---
        self.advanced = ctk.CTkFrame(self, fg_color=("gray90", "gray17"), corner_radius=8)
        adv = ctk.CTkFrame(self.advanced, fg_color="transparent")
        adv.pack(fill="both", expand=True, padx=12, pady=10)
        adv.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(adv, text="Backend").grid(row=0, column=0, sticky="w", pady=4)
        self.backend_menu = ctk.CTkOptionMenu(
            adv,
            variable=self.backend_var,
            values=names,
            command=lambda _v: self._refresh_status(),
            width=320,
        )
        self.backend_menu.grid(row=0, column=1, sticky="ew", pady=4, padx=(8, 0))

        ctk.CTkLabel(adv, text="GPU").grid(row=1, column=0, sticky="w", pady=4)
        self.gpu_menu = ctk.CTkOptionMenu(
            adv,
            variable=self.gpu_var,
            values=self._gpu_labels or ["cuda:0"],
            width=320,
        )
        self.gpu_menu.grid(row=1, column=1, sticky="ew", pady=4, padx=(8, 0))

        ctk.CTkLabel(adv, text="Model").grid(row=2, column=0, sticky="w", pady=4)
        self.model_var = ctk.StringVar(value="")
        self.model_menu = ctk.CTkOptionMenu(
            adv,
            variable=self.model_var,
            values=["(no models found)"],
            width=420,
        )
        self.model_menu.grid(row=2, column=1, sticky="ew", pady=4, padx=(8, 0))
        self._reload_model_list(prefer=cfg.get(KEY_DIT_MODEL) or DEFAULT_DIT_MODEL)

        self.keep_vram_var = ctk.BooleanVar(value=bool(cfg.get(KEY_KEEP_VRAM)))
        ctk.CTkCheckBox(
            adv,
            text="Keep model in VRAM (until app exit)",
            variable=self.keep_vram_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 2))

        self.vae_tiled_var = ctk.BooleanVar(value=bool(cfg.get(KEY_VAE_TILED)))
        ctk.CTkCheckBox(
            adv,
            text="Low VRAM (tiled VAE encode/decode)",
            variable=self.vae_tiled_var,
            command=self._on_vae_tiled_changed,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 4))
        self._vae_tiled_warn_job = None

        # --- Processing knobs (batch / overlap / chunk / tiles) ---
        self._batch_label_by_val = {v: lab for lab, v in BATCH_SIZE_LABELS}
        self._batch_val_by_label = {lab: v for lab, v in BATCH_SIZE_LABELS}
        saved_batch = int(cfg.get(KEY_BATCH_SIZE) or BATCH_SIZE_AUTO)
        self.batch_var = ctk.StringVar(
            value=self._batch_label_by_val.get(
                saved_batch, self._batch_label_by_val[BATCH_SIZE_AUTO]
            )
        )
        ctk.CTkLabel(adv, text="Batch size").grid(row=5, column=0, sticky="w", pady=4)
        ctk.CTkOptionMenu(
            adv,
            variable=self.batch_var,
            values=[lab for lab, _v in BATCH_SIZE_LABELS],
            width=320,
        ).grid(row=5, column=1, sticky="ew", pady=4, padx=(8, 0))

        self.uniform_batch_var = ctk.BooleanVar(
            value=True if KEY_UNIFORM_BATCH not in cfg else bool(cfg.get(KEY_UNIFORM_BATCH))
        )
        ctk.CTkCheckBox(
            adv,
            text="Uniform batch size (pad last batch — better for video)",
            variable=self.uniform_batch_var,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 4))

        self._video_opts_start_row = 7
        saved_overlap = int(cfg.get(KEY_TEMPORAL_OVERLAP) or TEMPORAL_OVERLAP_DEFAULT)
        self.overlap_var = ctk.StringVar(value=str(max(0, min(16, saved_overlap))))
        self._overlap_label = ctk.CTkLabel(adv, text="Temporal overlap")
        self._overlap_label.grid(row=7, column=0, sticky="w", pady=4)
        self._overlap_menu = ctk.CTkOptionMenu(
            adv,
            variable=self.overlap_var,
            values=[str(i) for i in range(0, 9)],
            width=320,
        )
        self._overlap_menu.grid(row=7, column=1, sticky="ew", pady=4, padx=(8, 0))

        self._chunk_label_by_val = {v: lab for lab, v in CHUNK_SIZE_LABELS}
        self._chunk_val_by_label = {lab: v for lab, v in CHUNK_SIZE_LABELS}
        saved_chunk = int(cfg.get(KEY_CHUNK_SIZE) or CHUNK_SIZE_AUTO)
        self.chunk_var = ctk.StringVar(
            value=self._chunk_label_by_val.get(
                saved_chunk, self._chunk_label_by_val[CHUNK_SIZE_AUTO]
            )
        )
        self._chunk_label = ctk.CTkLabel(adv, text="Chunk size")
        self._chunk_label.grid(row=8, column=0, sticky="w", pady=4)
        self._chunk_menu = ctk.CTkOptionMenu(
            adv,
            variable=self.chunk_var,
            values=[lab for lab, _v in CHUNK_SIZE_LABELS],
            width=320,
        )
        self._chunk_menu.grid(row=8, column=1, sticky="ew", pady=4, padx=(8, 0))

        tile_vals = [str(v) for v in VAE_TILE_CHOICES]
        enc = int(cfg.get(KEY_VAE_ENCODE_TILE) or VAE_ENCODE_TILE_DEFAULT)
        dec = int(cfg.get(KEY_VAE_DECODE_TILE) or VAE_DECODE_TILE_DEFAULT)
        if enc not in VAE_TILE_CHOICES:
            enc = VAE_ENCODE_TILE_DEFAULT
        if dec not in VAE_TILE_CHOICES:
            dec = VAE_DECODE_TILE_DEFAULT
        self.encode_tile_var = ctk.StringVar(value=str(enc))
        self.decode_tile_var = ctk.StringVar(value=str(dec))
        self._encode_tile_label = ctk.CTkLabel(adv, text="VAE encode tile")
        self._encode_tile_label.grid(row=9, column=0, sticky="w", pady=4)
        self._encode_tile_menu = ctk.CTkOptionMenu(
            adv, variable=self.encode_tile_var, values=tile_vals, width=320
        )
        self._encode_tile_menu.grid(row=9, column=1, sticky="ew", pady=4, padx=(8, 0))
        self._decode_tile_label = ctk.CTkLabel(adv, text="VAE decode tile")
        self._decode_tile_label.grid(row=10, column=0, sticky="w", pady=4)
        self._decode_tile_menu = ctk.CTkOptionMenu(
            adv, variable=self.decode_tile_var, values=tile_vals, width=320
        )
        self._decode_tile_menu.grid(row=10, column=1, sticky="ew", pady=4, padx=(8, 0))

        _path_row(adv, 11, "Weights folder", self.weights_dir_var, self._browse_weights)
        _path_row(adv, 12, "Runner folder", self.runner_dir_var, self._browse_runner)

        link_row = ctk.CTkFrame(adv, fg_color="transparent")
        link_row.grid(row=13, column=0, columnspan=2, sticky="ew", pady=(20, 10))
        link_btns = ctk.CTkFrame(link_row, fg_color="transparent")
        link_btns.pack(anchor="center")
        ctk.CTkButton(
            link_btns, text="Open weights folder", width=150, command=self._open_weights_folder
        ).pack(side="left")
        ctk.CTkButton(
            link_btns, text="Download weights…", width=150, command=self._open_download_url
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            link_btns, text="Install runner…", width=130, command=self._setup_runner
        ).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(
            adv,
            textvariable=self.status_var,
            wraplength=560,
            justify="left",
            text_color="#cccccc",
            anchor="w",
        ).grid(row=14, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self._autosave_job = None
        self._autosave_enabled = False
        self.weights_dir_var.trace_add("write", lambda *_: self._on_paths_changed())
        self.runner_dir_var.trace_add("write", lambda *_: self._on_paths_changed())
        self.gpu_var.trace_add("write", lambda *_: self._schedule_autosave())
        self.model_var.trace_add("write", lambda *_: self._schedule_autosave())
        self.keep_vram_var.trace_add("write", lambda *_: self._schedule_autosave())
        self.vae_tiled_var.trace_add("write", lambda *_: self._schedule_autosave())
        self.output_format_var.trace_add("write", lambda *_: self._schedule_autosave())
        self.prescale_custom_var.trace_add("write", lambda *_: self._schedule_autosave())
        self.batch_var.trace_add("write", lambda *_: self._schedule_autosave())
        self.uniform_batch_var.trace_add("write", lambda *_: self._schedule_autosave())
        self.overlap_var.trace_add("write", lambda *_: self._schedule_autosave())
        self.chunk_var.trace_add("write", lambda *_: self._schedule_autosave())
        self.encode_tile_var.trace_add("write", lambda *_: self._schedule_autosave())
        self.decode_tile_var.trace_add("write", lambda *_: self._schedule_autosave())
        self._refresh_status()
        self._update_video_advanced_visibility()
        self._on_vae_tiled_changed()
        self._autosave_enabled = True

        want_open = bool(cfg.get(KEY_ADVANCED_OPEN))
        if want_open:
            self._set_advanced_open(True, persist=False, resize=False)
            self.geometry(
                f"{UPSCALE_DIALOG_WIDTH}x{UPSCALE_BASIC_HEIGHT + self._advanced_extra_height()}"
            )
        else:
            self._set_advanced_open(False, persist=False, resize=False)
            self.geometry(f"{UPSCALE_DIALOG_WIDTH}x{UPSCALE_BASIC_HEIGHT}")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._center_on_parent)

    def _advanced_extra_height(self) -> int:
        return UPSCALE_ADVANCED_EXTRA_VIDEO if self._has_video else UPSCALE_ADVANCED_EXTRA

    def _layout_save_as_row(self):
        """Show image format menu and/or MP4 hint depending on selection."""
        for w in (
            getattr(self, "_save_as_image_menu", None),
            getattr(self, "_save_as_video_label", None),
            getattr(self, "_save_as_mixed_label", None),
        ):
            if w is None:
                continue
            try:
                w.grid_remove()
            except Exception:
                pass
        if self._video_only:
            self._save_as_label.configure(text="Save as")
            self._save_as_video_label.grid(
                row=3, column=1, sticky="ew", pady=4, padx=(8, 0)
            )
        elif self._has_video and self._has_image:
            self._save_as_label.configure(text="Images as")
            self._save_as_image_menu.grid(
                row=3, column=1, sticky="ew", pady=4, padx=(8, 0)
            )
        else:
            self._save_as_label.configure(text="Save as")
            self._save_as_image_menu.grid(
                row=3, column=1, sticky="ew", pady=4, padx=(8, 0)
            )

    def _update_video_advanced_visibility(self):
        """Show temporal overlap / chunk only when selection includes video."""
        widgets = (
            getattr(self, "_overlap_label", None),
            getattr(self, "_overlap_menu", None),
            getattr(self, "_chunk_label", None),
            getattr(self, "_chunk_menu", None),
        )
        for w in widgets:
            if w is None:
                continue
            try:
                if self._has_video:
                    w.grid()
                else:
                    w.grid_remove()
            except Exception:
                pass

    def _on_vae_tiled_changed(self, *_args):
        tiled = bool(self.vae_tiled_var.get())
        for w in (
            getattr(self, "_encode_tile_label", None),
            getattr(self, "_encode_tile_menu", None),
            getattr(self, "_decode_tile_label", None),
            getattr(self, "_decode_tile_menu", None),
        ):
            if w is None:
                continue
            try:
                if tiled:
                    w.grid()
                else:
                    w.grid_remove()
            except Exception:
                pass
        # Soft warn when turning tiling off for high output short-edge.
        if (
            not tiled
            and getattr(self, "_autosave_enabled", False)
            and self._should_warn_untiled_high_res()
        ):
            job = getattr(self, "_vae_tiled_warn_job", None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
            self._vae_tiled_warn_job = self.after(50, self._warn_untiled_high_res)
        self._schedule_autosave()

    def _warn_untiled_high_res(self):
        self._vae_tiled_warn_job = None
        if bool(self.vae_tiled_var.get()):
            return
        messagebox.showwarning(
            "Upscale",
            "Low VRAM is off and output short edge is about 1440px or larger.\n\n"
            "That can run out of VRAM. If it does: lower batch size, "
            "re-enable Low VRAM, or use smaller VAE tiles.",
            parent=self,
        )

    def _probe_media_size(self, path: str) -> tuple[int, int] | None:
        """Return (width, height) for image/video when cheaply available."""
        if not path or not os.path.isfile(path):
            return None
        ext = os.path.splitext(path)[1].lower()
        if ext in IMAGE_FORMATS:
            try:
                from PIL import Image

                with Image.open(path) as im:
                    w, h = im.size
                if w > 0 and h > 0:
                    return int(w), int(h)
            except Exception:
                pass
        try:
            import cv2

            cap = cv2.VideoCapture(path)
            try:
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            finally:
                cap.release()
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass
        return None

    def _estimated_output_short_edge(self) -> int:
        """Best-effort output short edge (input short × scale). 0 if unknown."""
        try:
            scale = int(float(self.scale_var.get().strip() or "2"))
        except (TypeError, ValueError):
            scale = 2
        scale = max(1, scale)
        for path in self.paths or []:
            size = self._probe_media_size(path)
            if size:
                return min(size) * scale
        return 0

    def _should_warn_untiled_high_res(self) -> bool:
        if bool(self.vae_tiled_var.get()):
            return False
        short = self._estimated_output_short_edge()
        if short >= 1440:
            return True
        # Unknown size + 4× often lands at/above 1440 short edge.
        try:
            scale = int(float(self.scale_var.get().strip() or "2"))
        except (TypeError, ValueError):
            scale = 2
        return short <= 0 and scale >= 4

    def _set_advanced_open(self, open_: bool, *, persist: bool = True, resize: bool = True):
        """Show or hide the advanced panel and optionally resize the window."""
        self._advanced_open = bool(open_)
        if self._advanced_open:
            # Pack above the bottom action bar.
            self.advanced.pack(fill="both", expand=True, padx=16, pady=(4, 0), before=self.advanced_btn.master)
            self.advanced_btn.configure(text="Advanced Settings ▲")
            if resize:
                self.update_idletasks()
                self.geometry(
                    f"{UPSCALE_DIALOG_WIDTH}x{UPSCALE_BASIC_HEIGHT + self._advanced_extra_height()}"
                )
        else:
            self.advanced.pack_forget()
            self.advanced_btn.configure(text="Advanced Settings ▼")
            if resize:
                self.update_idletasks()
                self.geometry(f"{UPSCALE_DIALOG_WIDTH}x{UPSCALE_BASIC_HEIGHT}")
        if persist:
            try:
                save_seedvr2_settings(advanced_open=self._advanced_open)
            except Exception:
                pass

    def _toggle_advanced(self):
        self._set_advanced_open(not self._advanced_open, persist=True, resize=True)

    def _selected_output_format(self) -> str:
        if self._video_only:
            return "mp4"
        label = (self.output_format_var.get() or "").strip()
        mode = self._fmt_mode_by_label.get(label, OUTPUT_FORMAT_PNG)
        return OUTPUT_FORMAT_JPEG if mode == OUTPUT_FORMAT_JPEG else OUTPUT_FORMAT_PNG

    def _selected_batch_size(self) -> int:
        label = (self.batch_var.get() or "").strip()
        return int(self._batch_val_by_label.get(label, BATCH_SIZE_AUTO))

    def _selected_chunk_size(self) -> int:
        label = (self.chunk_var.get() or "").strip()
        return int(self._chunk_val_by_label.get(label, CHUNK_SIZE_AUTO))

    def _selected_temporal_overlap(self) -> int:
        try:
            return max(0, min(16, int(self.overlap_var.get().strip())))
        except (TypeError, ValueError):
            return TEMPORAL_OVERLAP_DEFAULT

    def _selected_encode_tile(self) -> int:
        try:
            v = int(self.encode_tile_var.get().strip())
            return v if v in VAE_TILE_CHOICES else VAE_ENCODE_TILE_DEFAULT
        except (TypeError, ValueError):
            return VAE_ENCODE_TILE_DEFAULT

    def _selected_decode_tile(self) -> int:
        try:
            v = int(self.decode_tile_var.get().strip())
            return v if v in VAE_TILE_CHOICES else VAE_DECODE_TILE_DEFAULT
        except (TypeError, ValueError):
            return VAE_DECODE_TILE_DEFAULT

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
        self._schedule_autosave()

    def _schedule_autosave(self, delay_ms: int = 400):
        """Debounced persist of SeedVR settings (paths, GPU, model, toggles)."""
        if not getattr(self, "_autosave_enabled", False):
            return
        job = getattr(self, "_autosave_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        try:
            self._autosave_job = self.after(delay_ms, self._autosave_now)
        except Exception:
            self._autosave_job = None

    def _autosave_now(self):
        self._autosave_job = None
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self._persist_settings()

    def _flush_autosave(self):
        """Cancel debounce and write settings immediately."""
        job = getattr(self, "_autosave_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
            self._autosave_job = None
        if getattr(self, "_autosave_enabled", False):
            self._persist_settings()

    def _on_close(self):
        self._flush_autosave()
        try:
            self.destroy()
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
        self._schedule_autosave()

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
        chosen = filedialog.askdirectory(
            parent=self, initialdir=initial, title="SeedVR2 weights folder"
        )
        if chosen:
            self.weights_dir_var.set(chosen)
            self._flush_autosave()

    def _browse_runner(self):
        initial = self.runner_dir_var.get() or None
        chosen = filedialog.askdirectory(
            parent=self,
            initialdir=initial,
            title="SeedVR2 runner folder (inference_cli.py)",
        )
        if chosen:
            self.runner_dir_var.set(chosen)
            self._flush_autosave()

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

    def _persist_settings(self):
        """Write current SeedVR dialog options to settings.json (silent)."""
        weights = (self.weights_dir_var.get() or "").strip() or default_weights_dir()
        runner = (self.runner_dir_var.get() or "").strip()
        cuda = self._selected_cuda_index()
        dit = self._selected_dit_filename()
        # Persist image format even in video-only (last preference); ignore mp4 sentinel.
        out_fmt = self._selected_output_format()
        if out_fmt == "mp4":
            label = (self.output_format_var.get() or "").strip()
            mode = self._fmt_mode_by_label.get(label, OUTPUT_FORMAT_PNG)
            out_fmt = (
                OUTPUT_FORMAT_JPEG if mode == OUTPUT_FORMAT_JPEG else OUTPUT_FORMAT_PNG
            )
        save_seedvr2_settings(
            weights_dir=weights,
            runner_dir=runner,
            cuda_device=cuda,
            dit_model=dit,
            keep_vram=bool(self.keep_vram_var.get()),
            vae_tiled=bool(self.vae_tiled_var.get()),
            output_format=out_fmt,
            prescale_mode=self._selected_prescale_mode(),
            prescale_custom=self._selected_prescale_custom(),
            advanced_open=self._advanced_open,
            batch_size=self._selected_batch_size(),
            uniform_batch=bool(self.uniform_batch_var.get()),
            temporal_overlap=self._selected_temporal_overlap(),
            chunk_size=self._selected_chunk_size(),
            vae_encode_tile=self._selected_encode_tile(),
            vae_decode_tile=self._selected_decode_tile(),
        )
        if self.controller is not None:
            setattr(self.controller, "seedvr2_weights_dir", weights)
            setattr(self.controller, "seedvr2_runner_dir", runner)
            setattr(self.controller, "seedvr2_cuda_device", cuda)
            setattr(self.controller, "seedvr2_dit_model", dit)
            setattr(self.controller, "seedvr2_keep_vram", bool(self.keep_vram_var.get()))
            setattr(self.controller, "seedvr2_vae_tiled", bool(self.vae_tiled_var.get()))
            setattr(self.controller, "seedvr2_output_format", out_fmt)
            setattr(self.controller, "seedvr2_prescale_mode", self._selected_prescale_mode())
            setattr(self.controller, "seedvr2_prescale_custom", self._selected_prescale_custom())
            if not self.keep_vram_var.get():
                try:
                    from seedvr2_worker_host import shutdown_seedvr2_worker_host

                    shutdown_seedvr2_worker_host()
                except Exception:
                    pass
        backend = self._selected_backend()
        self._apply_paths_to_backend(backend)
        self._refresh_status()

    def _save_paths_only(self, quiet: bool = True):
        """Compat alias — settings always persist silently now."""
        self._persist_settings()

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

    def _setup_runner(self):
        """Explain-then-install flow for the ComfyUI-SeedVR2 CLI runner."""
        initial = (self.runner_dir_var.get() or "").strip() or default_setup_runner_dir()
        confirm = SeedVR2RunnerInstallDialog(self, initial_dir=initial)
        chosen = confirm.show_and_wait()
        if not chosen:
            return

        from gui_elements import open_file_op_progress_dialog
        from seedvr2_runner_setup import setup_seedvr2_runner

        progress = open_file_op_progress_dialog(
            self,
            title="Install SeedVR2 runner",
            total=5,
            action_label="Install",
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
            result = setup_seedvr2_runner(
                chosen,
                progress_cb=_on_progress,
                should_stop=lambda: bool(getattr(progress, "cancelled", False)),
            )

            def _done():
                try:
                    progress.close()
                except Exception:
                    pass
                if result.get("ok"):
                    path = result.get("path") or chosen
                    self.runner_dir_var.set(path)
                    self._flush_autosave()
                    messagebox.showinfo(
                        "Install SeedVR2 runner",
                        result.get("message") or f"Runner ready:\n{path}",
                        parent=self,
                    )
                elif result.get("error") == "aborted":
                    messagebox.showinfo(
                        "Install SeedVR2 runner",
                        "Installation cancelled.",
                        parent=self,
                    )
                else:
                    messagebox.showerror(
                        "Install SeedVR2 runner",
                        result.get("message") or "Installation failed.",
                        parent=self,
                    )

            try:
                self.after(0, _done)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _confirm(self):
        backend = self._selected_backend()
        self._flush_autosave()
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
        batch_size = self._selected_batch_size()
        options = {
            "scale": scale,
            "output_dir": out_dir,
            "suffix": f"_{getattr(backend, 'id', 'upscale')}",
            "cuda_device": cuda,
            "dit_model": dit,
            "keep_vram": bool(self.keep_vram_var.get()),
            "vae_tiled": bool(self.vae_tiled_var.get()),
            "output_format": self._selected_output_format(),
            "prescale_mode": mode,
            "prescale_custom": custom_px,
            "prescale_long_edge": long_edge,
            "batch_size": batch_size if batch_size > 0 else None,
            "uniform_batch_size": bool(self.uniform_batch_var.get()),
            "vae_encode_tile_size": self._selected_encode_tile(),
            "vae_decode_tile_size": self._selected_decode_tile(),
        }
        if self._has_video:
            options["temporal_overlap"] = self._selected_temporal_overlap()
            chunk = self._selected_chunk_size()
            options["chunk_size"] = chunk if chunk > 0 else None
        self.result = {
            "backend_id": getattr(backend, "id", "upscale"),
            "paths": list(self.paths),
            "options": options,
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
