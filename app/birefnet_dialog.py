"""
birefnet_dialog.py — Options dialog for BiRefNet background removal (images).

Install / readiness status is shown inline (SeedVR Upscale-style status box).
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox

import customtkinter as ctk

from birefnet_config import (
    BIREFNET_DISK_ESTIMATE,
    BIREFNET_HF_URL,
    BIREFNET_MODEL_VARIANTS,
    DEFAULT_MODEL_VARIANT,
    GPU_PACK_MISSING_MESSAGE,
    default_birefnet_dir,
    normalize_hex_color,
    resolve_model_variant,
    runtime_status,
    weights_status,
)
from birefnet_weights_setup import download_recommended_weights
from seedvr2_config import list_cuda_gpus

_STATUS_BG = ("gray85", "#0c0c0c")
_SECTION_BG = ("gray88", "#2a2a2a")
_SECTION_BORDER = ("gray70", "#3d3d3d")
_SECTION_TITLE = "#8ab4c8"
_ROW_PY = 8
_SECTION_GAP = 16
_INNER_PAD = 12
_MORPH_OPTIONS: tuple[tuple[str, int], ...] = (
    ("None", 0),
    ("Erode 1px", -1),
    ("Dilate 1px", 1),
)


def _section_card(parent, title: str) -> tuple[ctk.CTkFrame, ctk.CTkFrame]:
    """Bordered section card (Upscale-dialog style). Returns (outer, inner)."""
    outer = ctk.CTkFrame(
        parent,
        fg_color=_SECTION_BG,
        corner_radius=8,
        border_width=1,
        border_color=_SECTION_BORDER,
    )
    outer.pack(fill="x", pady=(0, _SECTION_GAP))
    ctk.CTkLabel(
        outer,
        text=title,
        text_color=_SECTION_TITLE,
        font=ctk.CTkFont(size=12, weight="bold"),
        anchor="w",
    ).pack(fill="x", padx=_INNER_PAD, pady=(10, 6))
    inner = ctk.CTkFrame(outer, fg_color="transparent")
    inner.pack(fill="x", padx=_INNER_PAD, pady=(0, 12))
    return outer, inner


class BirefnetOptionsDialog(ctk.CTkToplevel):
    """Output folder + inline weight install status; calls on_confirm(paths, options)."""

    def __init__(self, parent, paths: list[str], on_confirm, controller=None):
        super().__init__(parent)
        self.title("Remove Background")
        self.paths = list(paths or [])
        self.on_confirm = on_confirm
        self.controller = controller
        self.result = None

        self._install_running = False
        self._install_cancel = False
        self._install_thread: threading.Thread | None = None
        self._advanced_open = False

        self.geometry("480x600")
        self.minsize(460, 480)
        self.resizable(False, True)
        self.transient(parent)
        self.grab_set()

        default_out = os.path.dirname(self.paths[0]) if self.paths else os.getcwd()
        self.out_dir_var = ctk.StringVar(value=default_out)
        saved_suffix = "_nobg"
        if controller is not None:
            saved_suffix = (
                str(getattr(controller, "birefnet_suffix", "_nobg") or "_nobg").strip()
                or "_nobg"
            )
        self.suffix_var = ctk.StringVar(value=saved_suffix)
        self.status_var = ctk.StringVar(value="Checking…")

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 10))
        ctk.CTkLabel(
            header,
            text="BiRefNet — remove background",
            text_color="#00bfff",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(
            header,
            text=f"{len(self.paths)} image(s) selected · FP16 @ 1024 → PNG",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
        ).pack(fill="x")

        # Footer pinned first so it stays visible when the window is resized.
        btn = ctk.CTkFrame(self, fg_color="transparent")
        btn.pack(side="bottom", fill="x", padx=16, pady=(8, 14))
        self.cancel_btn = ctk.CTkButton(btn, text="Cancel", width=100, command=self._on_cancel)
        self.cancel_btn.pack(side="left")
        self.start_btn = ctk.CTkButton(btn, text="Start", command=self._start)
        self.start_btn.pack(side="right", fill="x", expand=True, padx=(10, 0))

        # --- Status (fixed section, always visible) ---
        status_outer = ctk.CTkFrame(
            self,
            fg_color=_SECTION_BG,
            corner_radius=8,
            border_width=1,
            border_color=_SECTION_BORDER,
        )
        status_outer.pack(fill="x", padx=16, pady=(0, _SECTION_GAP))
        ctk.CTkLabel(
            status_outer,
            text="Status",
            text_color=_SECTION_TITLE,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=_INNER_PAD, pady=(10, 6))
        status_inner = ctk.CTkFrame(status_outer, fg_color="transparent")
        status_inner.pack(fill="x", padx=_INNER_PAD, pady=(0, 12))

        self._status_box = ctk.CTkFrame(
            status_inner,
            fg_color=_STATUS_BG,
            corner_radius=6,
            border_width=1,
            border_color=("gray70", "#1a1a1a"),
        )
        self._status_box.pack(fill="x")
        ctk.CTkLabel(
            self._status_box,
            textvariable=self.status_var,
            wraplength=400,
            justify="left",
            text_color="#b0b0b0",
            anchor="w",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=10, pady=10)

        self._progress = ctk.CTkProgressBar(status_inner, height=8)
        self._progress.set(0)
        # Hidden until a download starts.

        body = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        saved_cuda = "0"
        if controller is not None:
            saved_cuda = str(getattr(controller, "birefnet_cuda_device", "0") or "0").strip() or "0"
        self._gpu_list = list_cuda_gpus()
        self._gpu_labels = [g["label"] for g in self._gpu_list]
        self._gpu_label_by_index = {
            str(g["index"]): g["label"] for g in self._gpu_list
        }
        initial_gpu = self._gpu_label_by_index.get(
            saved_cuda,
            self._gpu_labels[0] if self._gpu_labels else "cuda:0",
        )
        self.gpu_var = ctk.StringVar(value=initial_gpu)

        _options_card, options = _section_card(body, "Output")

        gpu_row = ctk.CTkFrame(options, fg_color="transparent")
        gpu_row.pack(fill="x", pady=(0, _ROW_PY))
        ctk.CTkLabel(gpu_row, text="GPU:", width=110, anchor="w").pack(side="left")
        self.gpu_menu = ctk.CTkOptionMenu(
            gpu_row,
            variable=self.gpu_var,
            values=self._gpu_labels or ["cuda:0"],
            height=28,
        )
        self.gpu_menu.pack(side="left", fill="x", expand=True)

        suf_row = ctk.CTkFrame(options, fg_color="transparent")
        suf_row.pack(fill="x", pady=(0, _ROW_PY))
        ctk.CTkLabel(suf_row, text="Filename suffix:", width=110, anchor="w").pack(side="left")
        self._suffix_entry = ctk.CTkEntry(
            suf_row, textvariable=self.suffix_var, height=28
        )
        self._suffix_entry.pack(side="left", fill="x", expand=True)

        # --- Background: transparent vs solid color ---
        bg_section = ctk.CTkFrame(options, fg_color="transparent")
        bg_section.pack(fill="x", pady=(0, _ROW_PY))
        ctk.CTkLabel(
            bg_section,
            text="Background:",
            width=110,
            anchor="w",
        ).pack(side="left", anchor="n", pady=(2, 0))

        bg_col = ctk.CTkFrame(bg_section, fg_color="transparent")
        bg_col.pack(side="left", fill="x", expand=True)

        saved_bg_mode = "transparent"
        saved_bg_color = "#FFFFFF"
        if controller is not None:
            mode = str(getattr(controller, "birefnet_bg_mode", "transparent") or "transparent").strip().lower()
            saved_bg_mode = "color" if mode == "color" else "transparent"
            saved_bg_color = (
                normalize_hex_color(getattr(controller, "birefnet_bg_color", None))
                or "#FFFFFF"
            )
        self._bg_mode_var = ctk.StringVar(value=saved_bg_mode)
        mode_row = ctk.CTkFrame(bg_col, fg_color="transparent")
        mode_row.pack(fill="x", pady=(0, 6))
        ctk.CTkRadioButton(
            mode_row,
            text="Transparent",
            variable=self._bg_mode_var,
            value="transparent",
            command=self._on_bg_mode_change,
        ).pack(side="left")
        ctk.CTkRadioButton(
            mode_row,
            text="Solid color",
            variable=self._bg_mode_var,
            value="color",
            command=self._on_bg_mode_change,
        ).pack(side="left", padx=(16, 0))

        color_row = ctk.CTkFrame(bg_col, fg_color="transparent")
        color_row.pack(fill="x")
        self._bg_swatch = ctk.CTkButton(
            color_row,
            text="",
            width=32,
            height=28,
            corner_radius=4,
            fg_color=saved_bg_color,
            hover_color=saved_bg_color,
            command=self._pick_bg_color,
        )
        self._bg_swatch.pack(side="left")
        self._bg_color_var = ctk.StringVar(value=saved_bg_color)
        self._bg_hex_entry = ctk.CTkEntry(
            color_row,
            textvariable=self._bg_color_var,
            width=88,
            height=28,
        )
        self._bg_hex_entry.pack(side="left", padx=(8, 0))
        self._bg_pick_btn = ctk.CTkButton(
            color_row,
            text="Pick…",
            width=64,
            height=28,
            command=self._pick_bg_color,
        )
        self._bg_pick_btn.pack(side="left", padx=(8, 0))
        self._bg_hex_entry.bind("<KeyRelease>", lambda _e: self._sync_bg_swatch())

        out_row = ctk.CTkFrame(options, fg_color="transparent")
        out_row.pack(fill="x", pady=(0, _ROW_PY))
        ctk.CTkLabel(out_row, text="Output folder:", width=110, anchor="w").pack(side="left")
        self._out_entry = ctk.CTkEntry(
            out_row, textvariable=self.out_dir_var, height=28
        )
        self._out_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._browse_btn = ctk.CTkButton(
            out_row, text="…", width=36, height=28, command=self._browse
        )
        self._browse_btn.pack(side="left")

        tools_row = ctk.CTkFrame(options, fg_color="transparent")
        tools_row.pack(fill="x", pady=(4, 0))
        self._install_btn = ctk.CTkButton(
            tools_row,
            text="Install weights…",
            width=130,
            height=28,
            command=self._install_weights,
        )
        self._install_btn.pack(side="left")
        self._refresh_btn = ctk.CTkButton(
            tools_row,
            text="Refresh status",
            width=110,
            height=28,
            fg_color="gray30",
            hover_color="gray25",
            command=lambda: self._refresh_status(deep=True),
        )
        self._refresh_btn.pack(side="left", padx=(8, 0))

        # --- Advanced (separate card) ---
        adv_outer = ctk.CTkFrame(
            body,
            fg_color=_SECTION_BG,
            corner_radius=8,
            border_width=1,
            border_color=_SECTION_BORDER,
        )
        adv_outer.pack(fill="x", pady=(0, _SECTION_GAP))
        adv_head = ctk.CTkFrame(adv_outer, fg_color="transparent")
        adv_head.pack(fill="x", padx=_INNER_PAD, pady=(10, 8))
        ctk.CTkLabel(
            adv_head,
            text="Advanced",
            text_color=_SECTION_TITLE,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).pack(side="left")
        self.advanced_btn = ctk.CTkButton(
            adv_head,
            text="Show ▼",
            width=88,
            height=26,
            fg_color="gray30",
            hover_color="gray25",
            command=self._toggle_advanced,
        )
        self.advanced_btn.pack(side="right")

        adv_inner = ctk.CTkFrame(adv_outer, fg_color="transparent")
        adv_inner.pack(fill="x", padx=_INNER_PAD, pady=(0, 12))
        self._advanced_frame = ctk.CTkFrame(adv_inner, fg_color="transparent")
        # Packed when Advanced is open.

        saved_variant = DEFAULT_MODEL_VARIANT
        saved_feather = 0
        saved_threshold = 0
        saved_morph = 0
        if controller is not None:
            saved_variant = resolve_model_variant(
                getattr(controller, "birefnet_model_variant", DEFAULT_MODEL_VARIANT)
            )
            saved_feather = int(getattr(controller, "birefnet_mask_feather", 0) or 0)
            saved_threshold = int(getattr(controller, "birefnet_mask_threshold", 0) or 0)
            saved_morph = int(getattr(controller, "birefnet_mask_morph", 0) or 0)

        self._variant_keys = list(BIREFNET_MODEL_VARIANTS.keys())
        self._variant_labels = [
            BIREFNET_MODEL_VARIANTS[k]["label"] for k in self._variant_keys
        ]
        initial_variant_label = BIREFNET_MODEL_VARIANTS[saved_variant]["label"]
        self.model_variant_var = ctk.StringVar(value=initial_variant_label)
        self._mask_feather_var = tk.IntVar(value=max(0, min(5, saved_feather)))
        self._mask_threshold_var = tk.IntVar(value=max(0, min(100, saved_threshold)))
        morph_label = next(
            (lbl for lbl, val in _MORPH_OPTIONS if val == saved_morph),
            "None",
        )
        self._mask_morph_var = ctk.StringVar(value=morph_label)

        model_row = ctk.CTkFrame(self._advanced_frame, fg_color="transparent")
        model_row.pack(fill="x", pady=(0, _ROW_PY))
        ctk.CTkLabel(model_row, text="Model:", width=110, anchor="w").pack(side="left")
        self._model_menu = ctk.CTkOptionMenu(
            model_row,
            variable=self.model_variant_var,
            values=self._variant_labels,
            height=28,
            command=lambda _v: self._refresh_status(deep=True),
        )
        self._model_menu.pack(side="left", fill="x", expand=True)

        feather_row = ctk.CTkFrame(self._advanced_frame, fg_color="transparent")
        feather_row.pack(fill="x", pady=(0, _ROW_PY))
        ctk.CTkLabel(feather_row, text="Edge feather:", width=110, anchor="w").pack(
            side="left"
        )
        self._feather_slider = ctk.CTkSlider(
            feather_row,
            from_=0,
            to=5,
            number_of_steps=5,
            variable=self._mask_feather_var,
            width=180,
            command=lambda _v: self._feather_value.configure(
                text=str(int(self._mask_feather_var.get()))
            ),
        )
        self._feather_slider.pack(side="left", padx=(0, 8))
        self._feather_value = ctk.CTkLabel(
            feather_row, text=str(int(self._mask_feather_var.get())), width=24
        )
        self._feather_value.pack(side="left")
        ctk.CTkLabel(
            feather_row,
            text="px blur on mask",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
        ).pack(side="left", padx=(8, 0))

        thresh_row = ctk.CTkFrame(self._advanced_frame, fg_color="transparent")
        thresh_row.pack(fill="x", pady=(0, _ROW_PY))
        ctk.CTkLabel(thresh_row, text="Mask threshold:", width=110, anchor="w").pack(
            side="left"
        )
        self._threshold_slider = ctk.CTkSlider(
            thresh_row,
            from_=0,
            to=100,
            number_of_steps=100,
            variable=self._mask_threshold_var,
            width=180,
            command=lambda _v: self._threshold_value.configure(
                text=str(int(self._mask_threshold_var.get()))
            ),
        )
        self._threshold_slider.pack(side="left", padx=(0, 8))
        self._threshold_value = ctk.CTkLabel(
            thresh_row, text=str(int(self._mask_threshold_var.get())), width=28
        )
        self._threshold_value.pack(side="left")
        ctk.CTkLabel(
            thresh_row,
            text="% (0 = off)",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
        ).pack(side="left", padx=(8, 0))

        morph_row = ctk.CTkFrame(self._advanced_frame, fg_color="transparent")
        morph_row.pack(fill="x", pady=(0, _ROW_PY))
        ctk.CTkLabel(morph_row, text="Edge adjust:", width=110, anchor="w").pack(
            side="left"
        )
        self._morph_menu = ctk.CTkOptionMenu(
            morph_row,
            variable=self._mask_morph_var,
            values=[lbl for lbl, _ in _MORPH_OPTIONS],
            height=28,
        )
        self._morph_menu.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            self._advanced_frame,
            text=f"Models download separately (~{BIREFNET_DISK_ESTIMATE} each).",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
        ).pack(fill="x", pady=(8, 0))

        ctk.CTkLabel(
            self._advanced_frame,
            text=f"Source: {BIREFNET_HF_URL}",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(4, 0))

        self._runtime_ready = False
        self._weights_ready = False
        self._runtime_error: str | None = None
        self._on_bg_mode_change()
        if controller is not None and bool(getattr(controller, "birefnet_advanced_open", False)):
            self._set_advanced_open(True, resize=False)
        self.after(50, lambda: self._refresh_status(deep=True))

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.lift()
        self.focus_force()

    def _set_form_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for w in (
            self.gpu_menu,
            self._suffix_entry,
            self._out_entry,
            self._browse_btn,
            self._install_btn,
            self._refresh_btn,
            self.advanced_btn,
            self.start_btn,
        ):
            try:
                w.configure(state=state)
            except Exception:
                pass
        if enabled:
            self._on_bg_mode_change()
            self._set_advanced_controls_enabled(self._advanced_open)
        else:
            for w in (self._bg_swatch, self._bg_hex_entry, self._bg_pick_btn):
                try:
                    w.configure(state="disabled")
                except Exception:
                    pass
        if enabled and self._runtime_ready:
            self.start_btn.configure(state="normal")
        elif not self._runtime_ready:
            self.start_btn.configure(state="disabled")

    def _set_advanced_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for w in (
            getattr(self, "_model_menu", None),
            getattr(self, "_feather_slider", None),
            getattr(self, "_threshold_slider", None),
            getattr(self, "_morph_menu", None),
        ):
            if w is None:
                continue
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _toggle_advanced(self) -> None:
        self._set_advanced_open(not self._advanced_open)

    def _set_advanced_open(self, open_: bool, *, resize: bool = True) -> None:
        self._advanced_open = bool(open_)
        if self._advanced_open:
            self._advanced_frame.pack(fill="x", pady=(0, 4))
            self.advanced_btn.configure(text="Hide ▲")
        else:
            self._advanced_frame.pack_forget()
            self.advanced_btn.configure(text="Show ▼")
        if self.controller is not None:
            self.controller.birefnet_advanced_open = self._advanced_open
        if resize:
            self.update_idletasks()

    def _model_variant_key(self) -> str:
        label = (self.model_variant_var.get() or "").strip()
        for key in self._variant_keys:
            if BIREFNET_MODEL_VARIANTS[key]["label"] == label:
                return key
        return DEFAULT_MODEL_VARIANT

    def _mask_morph_value(self) -> int:
        label = (self._mask_morph_var.get() or "None").strip()
        for lbl, val in _MORPH_OPTIONS:
            if lbl == label:
                return val
        return 0

    def _cuda_index_from_ui(self) -> str:
        label = (self.gpu_var.get() or "").strip()
        for gpu in getattr(self, "_gpu_list", []):
            if gpu.get("label") == label:
                return str(gpu.get("index", 0))
        return "0"

    def _on_bg_mode_change(self) -> None:
        if self._install_running:
            return
        solid = self._bg_mode_var.get() == "color"
        color_state = "normal" if solid else "disabled"
        for w in (self._bg_swatch, self._bg_hex_entry, self._bg_pick_btn):
            try:
                w.configure(state=color_state)
            except Exception:
                pass
        if solid:
            if not normalize_hex_color(self._bg_color_var.get()):
                self._bg_color_var.set("#FFFFFF")
            self._sync_bg_swatch()
        else:
            try:
                self._bg_swatch.configure(
                    fg_color=("#555555", "#555555"),
                    hover_color=("#666666", "#666666"),
                    text="∅",
                    text_color=("gray80", "gray80"),
                )
            except Exception:
                pass

    def _sync_bg_swatch(self) -> None:
        if self._bg_mode_var.get() != "color":
            return
        hex_color = normalize_hex_color(self._bg_color_var.get()) or "#FFFFFF"
        self._bg_color_var.set(hex_color)
        try:
            self._bg_swatch.configure(
                fg_color=hex_color,
                hover_color=hex_color,
                text="",
            )
        except Exception:
            pass

    def _pick_bg_color(self) -> None:
        if self._install_running or self._bg_mode_var.get() != "color":
            return
        initial = normalize_hex_color(self._bg_color_var.get()) or "#FFFFFF"
        try:
            self.grab_release()
        except Exception:
            pass
        result = colorchooser.askcolor(
            color=initial,
            title="Background color",
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
        chosen = normalize_hex_color(result[1])
        if not chosen:
            return
        self._bg_color_var.set(chosen)
        self._sync_bg_swatch()

    def _show_progress(self, show: bool) -> None:
        if show:
            self._progress.pack(fill="x", pady=(10, 0))
        else:
            self._progress.pack_forget()
            self._progress.set(0)

    def _refresh_status(self, *, deep: bool = False) -> None:
        from birefnet_config import python_deps_status

        try:
            deps = python_deps_status()
            rt = runtime_status(deep=deep)
            variant = self._model_variant_key()
            wt = weights_status(model_variant=variant)
            self._runtime_ready = bool(rt.get("ready"))
            self._weights_ready = bool(wt.get("ready"))
            self._runtime_error = None

            parts: list[str] = []
            if not deps.get("ready"):
                parts.append(deps.get("message") or "Missing Python dependencies.")
                self._runtime_error = deps.get("message")
            elif not rt.get("ready"):
                parts.append(rt.get("message") or GPU_PACK_MISSING_MESSAGE)
                self._runtime_error = rt.get("message") or GPU_PACK_MISSING_MESSAGE
            else:
                torch_ver = rt.get("torch")
                device = rt.get("device")
                if torch_ver:
                    line = f"GPU runtime OK · torch {torch_ver}"
                    if device:
                        line += f" · {device}"
                    parts.append(line)
                else:
                    parts.append("GPU runtime OK.")

            if wt.get("ready"):
                parts.append(f"Weights OK ({BIREFNET_MODEL_VARIANTS[variant]['label']})")
                parts.append(f"  {wt.get('path')}")
                parts.append("Ready to start.")
            else:
                parts.append(f"Weights: {BIREFNET_MODEL_VARIANTS[variant]['label']} not installed.")
                parts.append(f"Folder: {wt.get('path') or default_birefnet_dir()}")
                if rt.get("ready"):
                    parts.append(
                        f"Use Install weights… ({BIREFNET_DISK_ESTIMATE}) or Start will download first."
                    )

            self.status_var.set("\n".join(parts))

            if not self._install_running:
                self._install_btn.configure(state="normal")
                self.start_btn.configure(
                    state="normal" if self._runtime_ready else "disabled"
                )
        except Exception as exc:
            self._runtime_ready = False
            self._weights_ready = False
            self._runtime_error = str(exc)
            self.status_var.set(f"Status check failed:\n{exc}")
            if not self._install_running:
                self._install_btn.configure(state="normal")
                self.start_btn.configure(state="disabled")

    def _browse(self) -> None:
        if self._install_running:
            return
        path = filedialog.askdirectory(initialdir=self.out_dir_var.get() or None)
        if path:
            self.out_dir_var.set(path)

    def _on_cancel(self) -> None:
        if self._install_running:
            self._install_cancel = True
            self.status_var.set(
                self.status_var.get() + "\n\nCancelling download…"
            )
            return
        self.destroy()

    def _install_weights(self) -> None:
        if self._install_running:
            return
        variant = self._model_variant_key()
        wt = weights_status(model_variant=variant)
        if wt.get("ready"):
            messagebox.showinfo(
                "BiRefNet",
                f"{BIREFNET_MODEL_VARIANTS[variant]['label']} weights are already installed.\n\n"
                f"{wt.get('path')}\n\n"
                "Use Refresh status if the panel looks stale.",
                parent=self,
            )
            return
        if not self._runtime_ready:
            messagebox.showwarning(
                "GPU pack",
                (
                    "PyTorch CUDA is required to download model weights.\n\n"
                    f"{GPU_PACK_MISSING_MESSAGE}\n\n"
                    "If weights are already on disk for this variant, use Refresh status."
                ),
                parent=self,
            )
            return

        ok = messagebox.askyesno(
            "Install BiRefNet weights",
            (
                f"Download {BIREFNET_MODEL_VARIANTS[variant]['label']} weights?\n\n"
                f"Source: {wt.get('download_url')}\n"
                f"Destination: {default_birefnet_dir()}\n"
                f"Size: about {BIREFNET_DISK_ESTIMATE}\n\n"
                "Progress will show in this dialog."
            ),
            parent=self,
        )
        if not ok:
            return

        self._begin_install()

    def _begin_install(self) -> None:
        self._install_running = True
        self._install_cancel = False
        self._set_form_enabled(False)
        self.cancel_btn.configure(text="Stop download")
        self._install_btn.configure(state="disabled")
        self._show_progress(True)
        variant = self._model_variant_key()
        label = BIREFNET_MODEL_VARIANTS[variant]["label"]
        self.status_var.set(
            f"Downloading {label} weights…\nDestination: {default_birefnet_dir()}"
        )

        def _progress(step: int, total: int, detail: str) -> None:
            frac = (step / total) if total else 0.0

            def _ui() -> None:
                if not self.winfo_exists():
                    return
                self._progress.set(max(0.0, min(1.0, frac)))
                self.status_var.set(
                    f"Downloading {label} weights…\n"
                    f"{detail}\n"
                    f"Destination: {default_birefnet_dir()}"
                )

            self.after(0, _ui)

        def _should_stop() -> bool:
            return self._install_cancel

        def _worker() -> None:
            err: str | None = None
            cancelled = False
            try:
                download_recommended_weights(
                    model_variant=variant,
                    progress_cb=_progress,
                    should_stop=_should_stop,
                )
            except InterruptedError:
                cancelled = True
            except Exception as exc:
                err = str(exc)
            self.after(0, lambda: self._install_finished(cancelled=cancelled, err=err))

        self._install_thread = threading.Thread(
            target=_worker, daemon=True, name="birefnet-weights"
        )
        self._install_thread.start()

    def _install_finished(self, *, cancelled: bool, err: str | None) -> None:
        self._install_running = False
        self._install_thread = None
        self._show_progress(False)
        self.cancel_btn.configure(text="Cancel")
        self._set_form_enabled(True)
        self._refresh_status(deep=True)

        if cancelled:
            self.status_var.set(
                self.status_var.get() + "\n\nDownload cancelled."
            )
            return
        if err:
            messagebox.showerror("BiRefNet", err, parent=self)
            return
        variant = self._model_variant_key()
        if weights_status(model_variant=variant).get("ready"):
            messagebox.showinfo(
                "BiRefNet",
                f"Weights ready:\n{weights_status(model_variant=variant).get('path')}",
                parent=self,
            )

    def _start(self) -> None:
        if self._install_running:
            messagebox.showinfo(
                "Remove Background",
                "Wait for the weight download to finish (or Stop download).",
                parent=self,
            )
            return
        if not self._runtime_ready:
            detail = (self._runtime_error or self.status_var.get() or "").strip()
            if not detail or detail == "Checking…":
                self._refresh_status(deep=True)
            if not self._runtime_ready:
                messagebox.showwarning(
                    "Remove Background",
                    (self._runtime_error or self.status_var.get() or GPU_PACK_MISSING_MESSAGE).strip(),
                    parent=self,
                )
                return
        out_dir = (self.out_dir_var.get() or "").strip()
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showerror("Output folder", "Choose a valid output folder.", parent=self)
            return
        suffix = (self.suffix_var.get() or "_nobg").strip() or "_nobg"
        bg_mode = self._bg_mode_var.get() or "transparent"
        bg_color = normalize_hex_color(self._bg_color_var.get()) or "#FFFFFF"
        cuda = self._cuda_index_from_ui()
        variant = self._model_variant_key()
        if self.controller is not None:
            self.controller.birefnet_cuda_device = cuda
            self.controller.birefnet_model_variant = variant
            self.controller.birefnet_mask_feather = int(self._mask_feather_var.get())
            self.controller.birefnet_mask_threshold = int(self._mask_threshold_var.get())
            self.controller.birefnet_mask_morph = self._mask_morph_value()
            self.controller.birefnet_advanced_open = bool(self._advanced_open)
            self.controller.birefnet_suffix = suffix
            self.controller.birefnet_bg_mode = bg_mode
            self.controller.birefnet_bg_color = bg_color
            try:
                if hasattr(self.controller, "save_preferences"):
                    self.controller.save_preferences()
            except Exception:
                pass
        options = {
            "output_dir": out_dir,
            "suffix": suffix,
            "unload_after": True,
            "bg_mode": bg_mode,
            "bg_color": bg_color,
            "cuda_device": cuda,
            "model_variant": variant,
            "mask_feather": int(self._mask_feather_var.get()),
            "mask_threshold": int(self._mask_threshold_var.get()),
            "mask_morph": self._mask_morph_value(),
        }
        self.result = options
        self.destroy()
        if self.on_confirm:
            self.on_confirm(self.paths, options)
