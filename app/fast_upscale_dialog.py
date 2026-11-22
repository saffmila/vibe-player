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
    list_spandrel_models,
    resolve_models_dir,
    runtime_status as spandrel_runtime_status,
    weights_status as spandrel_weights_status,
)
from spandrel_favorites import (
    ensure_builtin_presets,
    find_preset,
    is_builtin_name,
    is_builtin_preset,
    model_stem_suffix,
    preset_menu_label,
    remove_preset,
    upsert_preset,
)

_PRESET_SEP = "────────"

_STATUS_BG = ("gray86", "#1e1e1e")
_UI_BTN_H = 28
_DIALOG_W = PROMO_STRIP_DIALOG_W
# Fallback before first tight `_fit_and_center` pass (status + preset/scale/format).
_DIALOG_H = 520
_TILE_CAP_LABELS = ("Auto", "256", "512", "768", "1024")
_TILE_CAP_VALUES = {256, 512, 768, 1024}
_FACTOR_LABELS = ("Model", "2×", "4×", "Custom")
_FACTOR_TO_SCALE = {"2×": 2.0, "4×": 4.0}
_FORMAT_LABELS = ("PNG", "JPEG", "TIFF", "WebP")
_FORMAT_TO_EXT = {
    "PNG": "png",
    "JPEG": "jpg",
    "TIFF": "tiff",
    "WebP": "webp",
}
_EXT_TO_FORMAT = {ext: label for label, ext in _FORMAT_TO_EXT.items()}
_PRESET_CURRENT = "(current)"
_SORT_LABELS = ("Name", "Rating", "Speed")
_SORT_DEFAULT = "Name"
_DEFAULT_JPEG_QUALITY = 92
_DEFAULT_PNG_COMPRESS = 3


def _normalize_model_meta(raw) -> dict:
    """Sanitize prefs dict: model_key → {rating, preview_s, full_s}."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for key, val in raw.items():
        k = str(key or "").strip()
        if not k or not isinstance(val, dict):
            continue
        entry: dict = {}
        try:
            rating = int(val.get("rating", 0) or 0)
            entry["rating"] = max(0, min(5, rating))
        except Exception:
            entry["rating"] = 0
        for field in ("preview_s", "full_s"):
            try:
                sec = float(val.get(field) or 0)
                if sec > 0:
                    entry[field] = round(sec, 2)
            except Exception:
                pass
        out[k] = entry
    return out


def _stars(rating: int) -> str:
    r = max(0, min(5, int(rating or 0)))
    if r <= 0:
        return ""
    return "★" * r


def _saved_max_tile_label(controller) -> str:
    """Session ceiling: Auto, or 256/512/768/1024.

    Older prefs stored a tiling checkbox. An explicit tile from that checkbox
    becomes the ceiling. Tiling left off stays Auto.
    """
    if controller is None:
        return "Auto"
    raw = getattr(controller, "spandrel_max_tile", None)
    if raw is None and bool(getattr(controller, "spandrel_tiling", False)):
        raw = getattr(controller, "spandrel_tile_size", 0)
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        value = 0
    return str(value) if value in _TILE_CAP_VALUES else "Auto"


def _max_tile_cap(label: str) -> int:
    try:
        value = int(label or 0)
    except (TypeError, ValueError):
        return 0
    return value if value in _TILE_CAP_VALUES else 0


def _short_folder(path: str) -> str:
    """Basename of an existing output folder, for the always-visible job line."""
    raw = (path or "").strip()
    if not raw or not os.path.isdir(raw):
        return "(no folder)"
    show = os.path.basename(os.path.normpath(raw)) or os.path.normpath(raw)
    if len(show) > 36:
        show = show[:16] + "…" + show[-16:]
    return show


def _time_suffix(meta: dict | None) -> str:
    if not meta:
        return ""
    full = meta.get("full_s")
    preview = meta.get("preview_s")
    try:
        if full and float(full) > 0:
            return f"{float(full):.1f}s"
    except Exception:
        pass
    try:
        if preview and float(preview) > 0:
            return f"{float(preview):.1f}s·p"
    except Exception:
        pass
    return ""


class FastUpscaleDialog(ctk.CTkToplevel):
    """Spandrel options → on_confirm(paths, options)."""

    def __init__(self, parent, paths: list[str], on_confirm, controller=None):
        super().__init__(parent)
        # CTk paints the titlebar by withdrawing and then showing the window
        # again a few milliseconds later. That flash is an empty frame whenever
        # the show wins the race against widget layout. Stay invisible until
        # _reveal(), including through that titlebar pass.
        self._shown = False
        try:
            self.attributes("-alpha", 0.0)
        except Exception:
            pass
        self.withdraw()
        self.title("Upscale")
        self.paths = list(paths or [])
        self.on_confirm = on_confirm
        self.controller = controller
        self.result = None
        self._ready = False
        self._runtime_ok = False
        self._install_running = False
        self._all_models: list[dict] = []
        self._filtered: list[dict] = []
        self._preview_win = None
        self._model_a = ""
        self._model_b = ""
        self._model_meta = _normalize_model_meta(
            getattr(controller, "spandrel_model_meta", None) if controller else None
        )
        self._favorites = ensure_builtin_presets(
            getattr(controller, "spandrel_favorite_presets", None) if controller else None
        )
        self._persist_favorites()
        self._preset_label_to_name: dict[str, str] = {}
        self._suspend_preset_sync = False
        self._advanced_open = False
        self._status_checked = False
        self._rate_menu = None
        self._save_menu = None
        self._summary_flash_token = 0
        self._manage_win = None

        self.geometry(f"{_DIALOG_W}x{_DIALOG_H}")
        self.minsize(_DIALOG_W, 500)
        self.resizable(False, False)
        self.transient(parent)

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
        self._model_a = saved_model
        self._count_var = ctk.StringVar(value="")
        sort_mode = _SORT_DEFAULT
        if controller is not None:
            saved_sort = str(
                getattr(controller, "spandrel_model_sort", _SORT_DEFAULT) or _SORT_DEFAULT
            ).strip()
            if saved_sort in _SORT_LABELS:
                sort_mode = saved_sort
        self.sort_mode_var = ctk.StringVar(value=sort_mode)

        self.tile_var = ctk.StringVar(value=_saved_max_tile_label(controller))

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
        show_prev = False
        if controller is not None:
            show_prev = bool(getattr(controller, "spandrel_show_preview", False))
        self.show_preview_var = ctk.BooleanVar(value=show_prev)
        fmt = "JPEG"
        if controller is not None:
            saved_fmt = str(
                getattr(controller, "spandrel_output_format", "jpg") or "jpg"
            ).strip().lower()
            for label, ext in _FORMAT_TO_EXT.items():
                if saved_fmt in (ext, label.lower(), "jpeg" if ext == "jpg" else ext):
                    fmt = label
                    break
        self.format_var = ctk.StringVar(value=fmt)
        jpeg_q = _DEFAULT_JPEG_QUALITY
        png_c = _DEFAULT_PNG_COMPRESS
        if controller is not None:
            try:
                jpeg_q = max(
                    1,
                    min(
                        100,
                        int(
                            getattr(
                                controller,
                                "spandrel_output_quality",
                                _DEFAULT_JPEG_QUALITY,
                            )
                            or _DEFAULT_JPEG_QUALITY
                        ),
                    ),
                )
            except (TypeError, ValueError):
                jpeg_q = _DEFAULT_JPEG_QUALITY
            try:
                png_c = max(
                    0,
                    min(
                        9,
                        int(
                            getattr(
                                controller,
                                "spandrel_png_compress",
                                _DEFAULT_PNG_COMPRESS,
                            )
                            or _DEFAULT_PNG_COMPRESS
                        ),
                    ),
                )
            except (TypeError, ValueError):
                png_c = _DEFAULT_PNG_COMPRESS
        self._jpeg_quality = jpeg_q
        self._webp_quality = jpeg_q
        self._png_compress = png_c
        self.compress_var = ctk.IntVar(value=jpeg_q)
        self.status_var = ctk.StringVar(value="Checking…")

        attach_promo_strip(
            self,
            "strip_openModels.png",
            dialog_width=_DIALOG_W,
            controller=self.controller,
        )

        ctk.CTkLabel(
            self,
            text="Upscale",
            text_color="#00bfff",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(10, 4))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="x", expand=False, padx=16, pady=(0, 4))
        self._body = body

        # Shown only when Spandrel is missing or the folder has no models.
        self._alert_var = ctk.StringVar(value="")
        self._alert_wrap = ctk.CTkFrame(body, fg_color="transparent")
        ctk.CTkLabel(
            self._alert_wrap,
            textvariable=self._alert_var,
            wraplength=_DIALOG_W - 64,
            justify="left",
            text_color="#e0b060",
            anchor="w",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x")

        self._tools = ctk.CTkFrame(body, fg_color="transparent")
        self._install_btn = ctk.CTkButton(
            self._tools,
            text="Install spandrel…",
            width=140,
            height=_UI_BTN_H,
            command=self._install_spandrel,
        )
        self._install_btn.pack(side="left")
        self._pick_btn = ctk.CTkButton(
            self._tools,
            text="Pick model folder…",
            width=150,
            height=_UI_BTN_H,
            fg_color="gray30",
            hover_color="gray25",
            command=self._pick_models_folder,
        )
        self._pick_btn.pack(side="left", padx=(8, 0))
        self._refresh_btn = ctk.CTkButton(
            self._tools,
            text="Refresh",
            width=90,
            height=_UI_BTN_H,
            fg_color="gray30",
            hover_color="gray25",
            command=self._refresh_status,
        )
        self._refresh_btn.pack(side="left", padx=(8, 0))

        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        self.custom_scale_var.trace_add(
            "write",
            lambda *_: (
                self._on_factor_change(apply_filter=False),
                self._notify_preview(),
            ),
        )

        # Engine status on the simple page (above presets).
        self._engine_block = ctk.CTkFrame(body, fg_color="transparent")
        self._engine_block.pack(fill="x", pady=(0, 8))
        self._status_box = ctk.CTkFrame(
            self._engine_block,
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
        self._folder_label = ctk.CTkLabel(
            self._engine_block,
            textvariable=self.models_dir_var,
            wraplength=_DIALOG_W - 40,
            justify="left",
            text_color="#8ab4c8",
            anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self._folder_label.pack(fill="x", pady=(4, 0))

        # Simple view: preset + scale + format. Model filenames under Advanced.
        preset_row = ctk.CTkFrame(body, fg_color="transparent")
        preset_row.pack(fill="x", pady=(4, 2))
        self._preset_row = preset_row
        ctk.CTkLabel(preset_row, text="Preset:", width=110, anchor="w").pack(side="left")
        self.preset_var = ctk.StringVar(value=_PRESET_CURRENT)
        self._preset_menu = ctk.CTkOptionMenu(
            preset_row,
            variable=self.preset_var,
            values=[_PRESET_CURRENT],
            height=28,
            command=self._on_preset_picked,
        )
        self._preset_menu.pack(side="left", fill="x", expand=True)
        self._preset_save_btn = ctk.CTkButton(
            preset_row,
            text="Save…",
            width=72,
            height=28,
            fg_color="gray30",
            hover_color="gray25",
            command=self._popup_save_menu,
        )
        self._preset_save_btn.pack(side="left", padx=(8, 0))
        self._preset_manage_btn = ctk.CTkButton(
            preset_row,
            text="Manage…",
            width=88,
            height=28,
            fg_color="gray30",
            hover_color="gray25",
            command=self._popup_manage_menu,
        )
        self._preset_manage_btn.pack(side="left", padx=(6, 0))

        # One blank row under Preset so the simple form breathes.
        _preset_gap = ctk.CTkFrame(body, fg_color="transparent", height=28)
        _preset_gap.pack(fill="x")
        _preset_gap.pack_propagate(False)

        # Simple mode basics: scale + format (model filenames stay in Advanced).
        row_f = ctk.CTkFrame(body, fg_color="transparent")
        row_f.pack(fill="x", pady=(0, 0))
        ctk.CTkLabel(row_f, text="Output scale:", width=110, anchor="w").pack(
            side="left"
        )
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
            text="native / fit after SR",
            text_color="#888",
            font=ctk.CTkFont(size=11),
        )
        self._custom_hint.pack(side="left", padx=(10, 0))

        fmt_row = ctk.CTkFrame(body, fg_color="transparent")
        fmt_row.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(fmt_row, text="Format:", width=110, anchor="w").pack(side="left")
        self._format_menu = ctk.CTkOptionMenu(
            fmt_row,
            variable=self.format_var,
            values=list(_FORMAT_LABELS),
            height=28,
            width=110,
            command=self._on_format_change,
        )
        self._format_menu.pack(side="left")
        self._compress_label = ctk.CTkLabel(
            fmt_row,
            text="Quality",
            text_color="#888",
            font=ctk.CTkFont(size=11),
            width=72,
            anchor="e",
        )
        self._compress_label.pack(side="left", padx=(10, 6))
        self._compress_slider = ctk.CTkSlider(
            fmt_row,
            from_=1,
            to=100,
            number_of_steps=99,
            variable=self.compress_var,
            width=140,
            command=self._on_compress_slide,
        )
        self._compress_slider.pack(side="left", fill="x", expand=True)
        self._compress_value = ctk.CTkLabel(
            fmt_row,
            text=str(jpeg_q),
            text_color="#888",
            font=ctk.CTkFont(size=11),
            width=28,
            anchor="w",
        )
        self._compress_value.pack(side="left", padx=(6, 0))

        self._job_summary_var = ctk.StringVar(value="")
        summary_row = ctk.CTkFrame(body, fg_color="transparent")
        summary_row.pack(fill="x", pady=(2, 2))
        ctk.CTkLabel(
            summary_row,
            textvariable=self._job_summary_var,
            text_color="#888",
            font=ctk.CTkFont(size=11),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        self._preview_cb = ctk.CTkCheckBox(
            summary_row,
            text="Show preview",
            variable=self.show_preview_var,
            command=self._on_preview_toggle,
            font=ctk.CTkFont(size=11),
            text_color="#8a8a8a",
            checkbox_width=15,
            checkbox_height=15,
        )
        self._preview_cb.pack(side="right", padx=(8, 0))

        ctk.CTkFrame(body, fg_color="transparent", height=10).pack(fill="x")

        # Tile / GPU / paths + model list — hidden until Advanced.
        self._advanced_block = ctk.CTkFrame(body, fg_color="transparent")

        row_t = ctk.CTkFrame(self._advanced_block, fg_color="transparent")
        row_t.pack(fill="x", pady=(2, 4))
        ctk.CTkLabel(row_t, text="Max tile:", width=110, anchor="w").pack(side="left")
        self._tile_menu = ctk.CTkOptionMenu(
            row_t,
            variable=self.tile_var,
            values=list(_TILE_CAP_LABELS),
            height=28,
            width=110,
            command=lambda _v: self._sync_preset_choice(),
        )
        self._tile_menu.pack(side="left")
        ctk.CTkLabel(
            row_t,
            text="ceiling — Auto fits VRAM",
            text_color="#888",
            font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=(10, 0))

        row_gpu = ctk.CTkFrame(self._advanced_block, fg_color="transparent")
        row_gpu.pack(fill="x", pady=4)
        ctk.CTkLabel(row_gpu, text="GPU:", width=110, anchor="w").pack(side="left")
        self._gpu_menu = ctk.CTkOptionMenu(
            row_gpu, variable=self.gpu_var, values=gpu_labels, height=28
        )
        self._gpu_menu.pack(side="left", fill="x", expand=True)

        out_row = ctk.CTkFrame(self._advanced_block, fg_color="transparent")
        out_row.pack(fill="x", pady=4)
        ctk.CTkLabel(out_row, text="Output folder:", width=110, anchor="w").pack(
            side="left"
        )
        self._out_entry = ctk.CTkEntry(out_row, textvariable=self.out_dir_var, height=28)
        self._out_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._browse_btn = ctk.CTkButton(
            out_row, text="…", width=36, height=28, command=self._browse
        )
        self._browse_btn.pack(side="left")

        suf_row = ctk.CTkFrame(self._advanced_block, fg_color="transparent")
        suf_row.pack(fill="x", pady=4)
        ctk.CTkLabel(suf_row, text="Suffix:", width=110, anchor="w").pack(side="left")
        self._suffix_entry = ctk.CTkEntry(
            suf_row, textvariable=self.suffix_var, height=28
        )
        self._suffix_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            suf_row,
            text="(empty = model name)",
            text_color="#888",
            font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(
            self._advanced_block,
            text="Model",
            text_color="#00bfff",
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).pack(fill="x", pady=(10, 2))

        row_filter = ctk.CTkFrame(self._advanced_block, fg_color="transparent")
        row_filter.pack(fill="x", pady=(2, 2))
        self._filter_row = row_filter
        ctk.CTkLabel(row_filter, text="Filter:", width=110, anchor="w").pack(side="left")
        self._filter_entry = ctk.CTkEntry(
            row_filter,
            textvariable=self.filter_var,
            height=28,
            placeholder_text="name contains… (e.g. PurePhoto, SPAN, Nomos, 4x)",
        )
        self._filter_entry.pack(side="left", fill="x", expand=True)
        self._sort_menu = ctk.CTkOptionMenu(
            row_filter,
            variable=self.sort_mode_var,
            values=list(_SORT_LABELS),
            height=28,
            width=110,
            corner_radius=14,
            fg_color=("gray75", "#2a2a2a"),
            button_color=("gray65", "#333333"),
            button_hover_color=("gray55", "#3a3a3a"),
            dropdown_fg_color=("gray80", "#2a2a2a"),
            command=lambda _v: self._on_sort_change(),
        )
        self._sort_menu.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            row_filter,
            textvariable=self._count_var,
            width=56,
            anchor="e",
            text_color="#888",
            font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=(8, 0))

        # Fixed-height list — do not expand or it pushes Output/Start off-screen.
        list_wrap = ctk.CTkFrame(
            self._advanced_block,
            fg_color=("gray90", "#1a1a1a"),
            corner_radius=6,
            border_width=1,
            border_color=("gray70", "#333"),
            height=168,
        )
        list_wrap.pack(fill="x", pady=(2, 4))
        list_wrap.pack_propagate(False)
        self._list_wrap = list_wrap
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
            selectmode="multiple",
            height=8,
        )
        scroll = tk.Scrollbar(list_wrap, orient="vertical", command=self._listbox.yview)
        self._listbox.configure(yscrollcommand=scroll.set)
        self._listbox.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        scroll.pack(side="right", fill="y", padx=(0, 4), pady=6)
        # Custom A/B slots (not free multi-select).
        self._listbox.bind("<Button-1>", self._on_list_lmb)
        self._listbox.bind("<Control-Button-1>", self._on_list_ctrl)
        self._listbox.bind("<Double-Button-1>", self._on_list_double)
        self._listbox.bind("<Button-3>", self._on_list_rmb)
        # Block default selection so Ctrl/LMB handlers own A/B.
        self._listbox.bind("<<ListboxSelect>>", lambda _e: "break")

        hint_row = ctk.CTkFrame(self._advanced_block, fg_color="transparent")
        hint_row.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(
            hint_row,
            text="LMB = one model · Ctrl+LMB = add / remove · RMB = rate",
            text_color="#666",
            font=ctk.CTkFont(size=11),
            anchor="w",
        ).pack(side="left")

        # Install / Pick folder / Refresh — Advanced when healthy, alert when not.
        self._engine_tools_host = ctk.CTkFrame(
            self._advanced_block, fg_color="transparent"
        )
        self._engine_tools_host.pack(fill="x", pady=(8, 0))
        self._tools.pack(in_=self._engine_tools_host, fill="x")

        # Document flow (not side=bottom) — no empty gap above actions.
        btn = ctk.CTkFrame(self, fg_color="transparent")
        btn.pack(fill="x", padx=16, pady=(8, 14))
        self.advanced_btn = ctk.CTkButton(
            btn,
            text="Advanced ▼",
            width=120,
            height=_UI_BTN_H,
            fg_color="gray30",
            hover_color="gray25",
            command=self._toggle_advanced,
        )
        self.advanced_btn.pack(side="left")
        ctk.CTkButton(btn, text="Cancel", width=100, command=self.destroy).pack(
            side="right"
        )
        self.start_btn = ctk.CTkButton(btn, text="Start", command=self._start)
        self.start_btn.pack(side="right", fill="x", expand=True, padx=(10, 6))

        self.out_dir_var.trace_add("write", lambda *_: self._refresh_job_summary())
        self._rebuild_preset_menu()
        self._on_factor_change(apply_filter=False)
        self._on_format_change(self.format_var.get())
        self._on_tiling_toggle()
        want_advanced = bool(
            getattr(self.controller, "spandrel_advanced_open", False)
            if self.controller is not None
            else False
        )
        self._set_advanced_open(want_advanced, refit=False)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        # Fill the model list while the window is still invisible, then show
        # it only after CTk's titlebar redraw has finished.
        try:
            self._refresh_status(refit=False)
        except Exception:
            pass
        self.after(40, self._reveal)

    def resizable(self, width=None, height=None):
        result = super().resizable(width, height)
        # CTk schedules another titlebar show ~10ms after resizable().
        if width is not None and not self._shown:
            self.after(12, self._hold_hidden)
        return result

    def _hold_hidden(self) -> None:
        if self._shown:
            return
        try:
            self.withdraw()
            self.attributes("-alpha", 0.0)
        except Exception:
            pass

    def _reveal(self) -> None:
        if self._shown:
            return
        self._shown = True
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        try:
            self._fit_and_center()
            self.deiconify()
            self.update_idletasks()
            self._fit_and_center()
            try:
                self.attributes("-alpha", 1.0)
            except Exception:
                pass
            self.lift()
            self.focus_force()
            try:
                self.grab_set()
            except Exception:
                pass
        except Exception:
            try:
                self.deiconify()
                self.attributes("-alpha", 1.0)
            except Exception:
                pass
        self.after_idle(lambda: sync_promo_strip(self))
        self.after(80, lambda: sync_promo_strip(self))
        self.after(200, self._sync_preview_from_pref)

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
        self._sync_preset_choice()

    def _toggle_advanced(self) -> None:
        self._set_advanced_open(not self._advanced_open)

    def _set_advanced_open(self, open_: bool, *, refit: bool = True) -> None:
        """Show tile/GPU/paths + model list. Remember the choice."""
        self._advanced_open = bool(open_)
        if self._advanced_open:
            if not self._advanced_block.winfo_ismapped():
                self._advanced_block.pack(fill="x", pady=(6, 0))
            self.advanced_btn.configure(text="Advanced ▲")
        else:
            self._advanced_block.pack_forget()
            self.advanced_btn.configure(text="Advanced ▼")
        if self.controller is not None:
            try:
                setattr(self.controller, "spandrel_advanced_open", self._advanced_open)
            except Exception:
                pass
        if refit:
            self.after_idle(self._fit_and_center)

    def _refresh_job_summary(self) -> None:
        """Always-visible recipe line, including the folder hidden in Advanced."""
        var = getattr(self, "_job_summary_var", None)
        if var is None:
            return
        mode = self.factor_var.get() or "Model"
        if mode == "Custom":
            scale = self._target_scale()
            scale_txt = f"{scale:g}×" if scale else "custom"
        elif mode in ("2×", "4×"):
            scale_txt = mode
        else:
            scale_txt = "Model"
        fmt = self.format_var.get() or "JPEG"
        if fmt in ("JPEG", "WebP"):
            try:
                q = int(self.compress_var.get())
            except Exception:
                q = (
                    self._jpeg_quality
                    if fmt == "JPEG"
                    else self._webp_quality
                )
            fmt_txt = f"{fmt} q{q}"
        elif fmt == "PNG":
            try:
                c = int(self.compress_var.get())
            except Exception:
                c = self._png_compress
            fmt_txt = f"PNG c{c}"
        else:
            fmt_txt = fmt
        cap = (self.tile_var.get() or "Auto").strip()
        tile_txt = "auto tile" if cap == "Auto" else f"max tile {cap}"
        folder = _short_folder(self.out_dir_var.get())
        var.set(f"{scale_txt}  ·  {fmt_txt}  ·  {tile_txt}  ·  {folder}")

    def _rebuild_preset_menu(self, *, prefer_name: str | None = None) -> None:
        labels = [_PRESET_CURRENT]
        mapping: dict[str, str] = {}
        used = {_PRESET_CURRENT, _PRESET_SEP}
        built = [p for p in self._favorites if is_builtin_preset(p)]
        users = [p for p in self._favorites if not is_builtin_preset(p)]

        def _append(presets: list) -> None:
            for preset in presets:
                label = preset_menu_label(preset)
                name = str(preset.get("name") or "")
                if label in used:
                    label = f"{label}  ({name})"
                used.add(label)
                mapping[label] = name
                labels.append(label)

        _append(built)
        if built and users:
            labels.append(_PRESET_SEP)
        _append(users)
        self._preset_label_to_name = mapping
        try:
            self._preset_menu.configure(values=labels)
        except Exception:
            pass
        if prefer_name:
            for label, name in mapping.items():
                if name == prefer_name:
                    self.preset_var.set(label)
                    break
            else:
                self.preset_var.set(_PRESET_CURRENT)
        elif self.preset_var.get() not in labels or self.preset_var.get() == _PRESET_SEP:
            self.preset_var.set(_PRESET_CURRENT)
        try:
            self._preset_manage_btn.configure(
                state="normal" if self._favorites else "disabled"
            )
        except Exception:
            pass

    def _form_matches_preset(self, preset: dict) -> bool:
        """True when the visible recipe equals this preset (one model, no A/B)."""
        if (self._model_b or "").strip():
            return False
        current = (self._model_a or "").strip()
        if not current or self._find_model_key(str(preset.get("model") or "")) != current:
            return False
        mode = self.factor_var.get() or "Model"
        if mode != str(preset.get("scale_mode") or "Model"):
            return False
        if mode == "Custom":
            try:
                cur = float(self.custom_scale_var.get() or 0)
                want = float(preset.get("custom_scale") or 0)
            except (TypeError, ValueError):
                return False
            if abs(cur - want) > 1e-6:
                return False
        fmt = _FORMAT_TO_EXT.get(self.format_var.get(), "png")
        if fmt != str(preset.get("output_format") or "png"):
            return False
        return True

    def _sync_preset_choice(self) -> None:
        """Keep the preset rollout honest: a match stays selected, else (current)."""
        self._refresh_job_summary()
        if self._suspend_preset_sync or not hasattr(self, "preset_var"):
            return
        current_name = self._preset_label_to_name.get(self.preset_var.get() or "")
        match = None
        if current_name:
            chosen = find_preset(self._favorites, current_name)
            if chosen is not None and self._form_matches_preset(chosen):
                match = chosen
        if match is None:
            for preset in self._favorites:
                if self._form_matches_preset(preset):
                    match = preset
                    break
        label = preset_menu_label(match) if match is not None else _PRESET_CURRENT
        if label not in self._preset_label_to_name and label != _PRESET_CURRENT:
            for cand, name in self._preset_label_to_name.items():
                if match is not None and name == match.get("name"):
                    label = cand
                    break
        if self.preset_var.get() != label:
            self.preset_var.set(label)

    def _find_model_key(self, stored: str, models: list | None = None) -> str | None:
        stored_norm = (stored or "").replace("\\", "/").strip().lower()
        if not stored_norm:
            return None
        pool = self._all_models if models is None else models
        exact: list[str] = []
        by_name: list[str] = []
        base = os.path.basename(stored_norm)
        for item in pool:
            key = self._model_key(item)
            norms = []
            for cand in (
                key,
                str(item.get("rel") or ""),
                str(item.get("label") or ""),
                str(item.get("filename") or ""),
                str(item.get("path") or ""),
            ):
                text = cand.replace("\\", "/").strip().lower()
                if text:
                    norms.append(text)
            if stored_norm in norms:
                exact.append(key)
            elif base and base in {os.path.basename(n) for n in norms}:
                by_name.append(key)
        if exact:
            return exact[0]
        if len(by_name) == 1:
            return by_name[0]
        return None

    def _assign_preset_fields(self, preset: dict) -> None:
        mode = str(preset.get("scale_mode") or "Model").strip()
        if mode not in _FACTOR_LABELS:
            mode = "Model"
        self.factor_var.set(mode)
        try:
            custom = float(preset.get("custom_scale") or 3)
        except (TypeError, ValueError):
            custom = 3.0
        self.custom_scale_var.set(f"{custom:g}")
        fmt = str(preset.get("output_format") or "jpg").strip().lower()
        if fmt == "jpeg":
            fmt = "jpg"
        self.format_var.set(_EXT_TO_FORMAT.get(fmt, "JPEG"))
        self._on_factor_change(apply_filter=False)
        self._on_format_change(self.format_var.get())

    def _on_preset_picked(self, label: str) -> None:
        if label == _PRESET_CURRENT:
            self._sync_preset_choice()
            return
        if label == _PRESET_SEP:
            self._sync_preset_choice()
            return
        name = self._preset_label_to_name.get(label or "")
        preset = find_preset(self._favorites, name) if name else None
        if preset is None:
            self._sync_preset_choice()
            return
        self._apply_preset(preset)

    def _apply_preset(self, preset: dict) -> None:
        """Fill the form from a preset and solo-select its model."""
        name = str(preset.get("name") or "").strip()
        model = str(preset.get("model") or "").strip()
        models_dir = str(preset.get("models_dir") or "").strip()
        error = ""
        self._suspend_preset_sync = True
        try:
            if models_dir and not os.path.isdir(models_dir):
                error = (
                    f"Preset “{name}” points at a models folder that is not on this PC:\n"
                    f"{models_dir}"
                )
            else:
                if models_dir and os.path.isdir(models_dir):
                    current_dir = self._models_dir()
                    same = os.path.normcase(os.path.normpath(models_dir)) == os.path.normcase(
                        os.path.normpath(current_dir)
                    )
                    if not same:
                        probe = list_spandrel_models(models_dir)
                        if self._find_model_key(model, probe) is None:
                            error = (
                                f"Preset “{name}” needs model “{model}”, which is not in:\n"
                                f"{models_dir}"
                            )
                        else:
                            self.models_dir_var.set(str(Path(models_dir).resolve()))
                            if self.controller is not None:
                                try:
                                    setattr(
                                        self.controller,
                                        "spandrel_models_dir",
                                        self.models_dir_var.get(),
                                    )
                                except Exception:
                                    pass
                            self._refresh_status()
                if not error:
                    found = self._find_model_key(model)
                    if not found:
                        error = (
                            f"Preset “{name}” needs model “{model}”, "
                            "which is not in the models folder."
                        )
                    else:
                        self._assign_preset_fields(preset)
                        self._select_model_solo(found)
                        visible = {self._model_key(it) for it in self._filtered}
                        if found not in visible and (self.filter_var.get() or "").strip():
                            # Trace rebuilds the list while sync is suspended.
                            self.filter_var.set("")
                        else:
                            self._apply_filter()
        finally:
            self._suspend_preset_sync = False
        self._sync_preset_choice()
        if error:
            messagebox.showwarning("Favorite preset", error, parent=self)

    def _popup_manage_menu(self) -> None:
        existing = self._manage_win
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                self._manage_win = None
        if not self._favorites:
            messagebox.showinfo(
                "Favorite preset",
                "No presets yet.\n\nSave… → Save preset stores the current model, scale, and format.",
                parent=self,
            )
            return
        self._open_manage_window()

    def _open_manage_window(self) -> None:
        """List saved presets. A menu cannot show while this dialog holds the grab."""
        win = ctk.CTkToplevel(self)
        self._manage_win = win
        win.title("Presets")
        try:
            win.attributes("-alpha", 0.0)
        except Exception:
            pass
        win.withdraw()
        win.resizable(False, False)
        win.transient(self)
        win.geometry("480x300")

        ctk.CTkLabel(
            win,
            text="Saved presets",
            text_color="#00bfff",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(14, 2))
        ctk.CTkLabel(
            win,
            text="User presets can be removed. Built-ins stay. Model files stay on disk.",
            text_color="#888",
            font=ctk.CTkFont(size=11),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 8))

        # Pack actions first (bottom) so the list cannot crush them.
        btn = ctk.CTkFrame(win, fg_color="transparent")
        btn.pack(side="bottom", fill="x", padx=16, pady=(8, 14))
        ctk.CTkButton(
            btn,
            text="Remove",
            width=100,
            height=_UI_BTN_H,
            fg_color="#8a3a3a",
            hover_color="#6e2e2e",
            command=self._manage_remove_selected,
        ).pack(side="left")
        ctk.CTkButton(
            btn,
            text="Close",
            width=100,
            height=_UI_BTN_H,
            command=self._close_manage_window,
        ).pack(side="right")

        list_wrap = ctk.CTkFrame(
            win,
            fg_color=("gray90", "#1a1a1a"),
            corner_radius=6,
            border_width=1,
            border_color=("gray70", "#333"),
        )
        list_wrap.pack(fill="both", expand=True, padx=16, pady=(0, 0))
        lb = tk.Listbox(
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
        )
        scroll = tk.Scrollbar(list_wrap, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=scroll.set)
        lb.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        scroll.pack(side="right", fill="y", padx=(0, 4), pady=6)
        self._manage_list = lb
        self._manage_names: list[str] = []
        lb.bind("<Double-Button-1>", lambda _e: self._manage_remove_selected())
        win.bind("<Escape>", lambda _e: self._close_manage_window())
        win.protocol("WM_DELETE_WINDOW", self._close_manage_window)
        self._fill_manage_list()

        def _show() -> None:
            try:
                if not win.winfo_exists():
                    return
            except Exception:
                return
            try:
                self.grab_release()
            except Exception:
                pass
            try:
                win.update_idletasks()
                win.deiconify()
                win.lift()
                win.focus_force()
                win.grab_set()
                try:
                    win.attributes("-alpha", 1.0)
                except Exception:
                    pass
            except Exception:
                try:
                    win.deiconify()
                    win.attributes("-alpha", 1.0)
                except Exception:
                    pass

        win.after(40, _show)

    def _fill_manage_list(self) -> None:
        lb = getattr(self, "_manage_list", None)
        if lb is None:
            return
        try:
            if not lb.winfo_exists():
                return
        except Exception:
            return
        lb.delete(0, tk.END)
        names: list[str] = []
        built = [p for p in self._favorites if is_builtin_preset(p)]
        users = [p for p in self._favorites if not is_builtin_preset(p)]
        for preset in built:
            pname = str(preset.get("name") or "").strip()
            if not pname:
                continue
            lb.insert(tk.END, f"{preset_menu_label(preset)}  [built-in]")
            names.append(pname)
        if built and users:
            lb.insert(tk.END, _PRESET_SEP)
            names.append("")
        for preset in users:
            pname = str(preset.get("name") or "").strip()
            if not pname:
                continue
            lb.insert(tk.END, preset_menu_label(preset))
            names.append(pname)
        self._manage_names = names
        if any(names):
            for i, n in enumerate(names):
                if n:
                    lb.selection_set(i)
                    break
        else:
            self._close_manage_window()

    def _manage_remove_selected(self) -> None:
        lb = getattr(self, "_manage_list", None)
        win = self._manage_win
        if lb is None or win is None:
            return
        try:
            sel = lb.curselection()
        except Exception:
            return
        if not sel:
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._manage_names):
            return
        name = self._manage_names[idx]
        if not name:
            return
        if self._remove_favorite_preset(name, parent=win):
            self._fill_manage_list()

    def _close_manage_window(self) -> None:
        win = self._manage_win
        self._manage_win = None
        self._manage_list = None
        self._manage_names = []
        if win is not None:
            try:
                win.grab_release()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass
        try:
            if self.winfo_exists():
                self.grab_set()
                self.lift()
        except Exception:
            pass

    def _remove_favorite_preset(self, name: str, parent=None) -> bool:
        name = (name or "").strip()
        if not name:
            return False
        preset = find_preset(self._favorites, name)
        if is_builtin_preset(preset):
            messagebox.showinfo(
                "Favorite preset",
                "Built-in presets cannot be removed.",
                parent=parent or self,
            )
            return False
        label = preset_menu_label(preset) if preset else name
        if not messagebox.askyesno(
            "Favorite preset",
            f"Remove preset “{label}”?",
            parent=parent or self,
        ):
            return False
        self._favorites = remove_preset(self._favorites, name)
        self._persist_favorites()
        self._rebuild_preset_menu()
        self._sync_preset_choice()
        return True

    def _persist_favorites(self) -> None:
        if self.controller is None:
            return
        try:
            setattr(self.controller, "spandrel_favorite_presets", list(self._favorites))
        except Exception:
            pass

    def _sync_preview_from_pref(self) -> None:
        if self.show_preview_var.get():
            self._open_preview_window()

    def _start_model(self) -> str:
        """Model for Start / batch: always the newest (right), else the sole pick."""
        return (self._model_b or self._model_a or self.model_var.get() or "").strip()

    def _preview_job(self) -> dict | None:
        # Preview: left = previous (A), right = new (B). Solo pick → Original | A.
        left = (self._model_a or "").strip()
        right = (self._model_b or "").strip()
        if right and left and right != left:
            model, model_b = left, right
        else:
            model = right or left or (self.model_var.get() or "").strip()
            model_b = ""
        if not model or model.startswith("("):
            return None
        if not self.paths:
            return None
        gpu_label = self.gpu_var.get()
        return {
            "source_path": self.paths[0],
            "model": model,
            "model_b": model_b or None,
            "models_dir": self._models_dir(),
            "cuda_device": self._gpu_indices.get(gpu_label, "0"),
            "target_scale": self._target_scale(),
        }

    def _on_preview_toggle(self) -> None:
        if self.controller is not None:
            try:
                setattr(
                    self.controller,
                    "spandrel_show_preview",
                    bool(self.show_preview_var.get()),
                )
            except Exception:
                pass
        if self.show_preview_var.get():
            self._open_preview_window()
        else:
            self._close_preview_window()

    def _open_preview_window(self) -> None:
        win = self._preview_win
        if win is not None:
            try:
                if win.winfo_exists():
                    win.lift()
                    win.schedule_refresh(immediate=True)
                    try:
                        self.grab_release()
                    except Exception:
                        pass
                    return
            except Exception:
                self._preview_win = None

        from spandrel_preview_window import SpandrelPreviewWindow

        def _on_close():
            self._preview_win = None
            try:
                self.show_preview_var.set(False)
            except Exception:
                pass
            try:
                if self.winfo_exists():
                    self.grab_set()
            except Exception:
                pass

        try:
            self.grab_release()
        except Exception:
            pass
        self._preview_win = SpandrelPreviewWindow(
            self,
            get_job=self._preview_job,
            on_close=_on_close,
            on_preview_timing=self._on_preview_timing,
        )
        self._preview_win.lift()

    def _close_preview_window(self) -> None:
        win = self._preview_win
        self._preview_win = None
        if win is not None:
            try:
                if win.winfo_exists():
                    # Avoid re-entrant checkbox flip from on_close.
                    win.on_close = None
                    win.destroy()
            except Exception:
                pass
        try:
            if self.winfo_exists():
                self.grab_set()
        except Exception:
            pass

    def _notify_preview(self) -> None:
        win = self._preview_win
        if win is None:
            return
        try:
            if win.winfo_exists():
                win.schedule_refresh(immediate=False)
        except Exception:
            self._preview_win = None

    def destroy(self):
        self._close_preview_window()
        try:
            super().destroy()
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
        # Output scale does not filter models (use Filter text for 4x / SPAN…).
        _ = apply_filter
        self._notify_preview()
        self._sync_preset_choice()

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
            self._tile_menu,
            self._gpu_menu,
            self._preview_cb,
            self._sort_menu,
            self._out_entry,
            self._browse_btn,
            self._suffix_entry,
            self._format_menu,
            self._compress_slider,
            self._preset_menu,
            self._preset_save_btn,
            self._preset_manage_btn,
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
            try:
                self._preset_manage_btn.configure(
                    state="normal" if self._favorites else "disabled"
                )
            except Exception:
                pass

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

    def _model_key(self, item: dict) -> str:
        """Stable id for selection: prefer relative path inside the models folder."""
        return str(item.get("rel") or item.get("label") or item.get("filename") or "")

    def _index_at_event(self, event) -> int | None:
        try:
            idx = self._listbox.nearest(event.y)
        except Exception:
            return None
        if idx < 0 or idx >= len(self._filtered):
            return None
        return int(idx)

    def _select_model_solo(self, key: str) -> None:
        """Plain LMB: exactly one model (vs Original)."""
        key = (key or "").strip()
        if not key:
            return
        self._model_a = key
        self._model_b = ""
        self.model_var.set(key)

    def _ctrl_toggle_model(self, key: str) -> None:
        """
        Ctrl+LMB: standard multi-select (max 2 for A|B compare).

        Unselected → add as second (or first if empty). Already selected → remove.
        Third unselected while A|B full → replaces B.
        """
        key = (key or "").strip()
        if not key:
            return
        a = (self._model_a or "").strip()
        b = (self._model_b or "").strip()
        if key == a:
            self._model_a = b
            self._model_b = ""
        elif key == b:
            self._model_b = ""
        elif not a:
            self._model_a = key
        elif not b:
            self._model_b = key
        else:
            self._model_b = key
        self.model_var.set(self._start_model())

    def _on_list_lmb(self, event):
        # Ctrl+LMB also fires Button-1 on some platforms — route to toggle.
        if int(getattr(event, "state", 0) or 0) & 0x0004:
            return self._on_list_ctrl(event)
        idx = self._index_at_event(event)
        if idx is None:
            return "break"
        key = self._model_key(self._filtered[idx])
        self._select_model_solo(key)
        self._sync_list_selection()
        self._update_action_states()
        self._notify_preview()
        self._sync_preset_choice()
        return "break"

    def _on_list_ctrl(self, event):
        idx = self._index_at_event(event)
        if idx is None:
            return "break"
        key = self._model_key(self._filtered[idx])
        self._ctrl_toggle_model(key)
        self._sync_list_selection()
        self._update_action_states()
        self._notify_preview()
        self._sync_preset_choice()
        return "break"

    def _on_list_double(self, event):
        idx = self._index_at_event(event)
        if idx is not None:
            key = self._model_key(self._filtered[idx])
            # Double-click always solo → Start that model.
            self._select_model_solo(key)
            self._sync_list_selection()
            self._update_action_states()
        self._start()
        return "break"

    def _on_list_rmb(self, event):
        idx = self._index_at_event(event)
        if idx is None:
            return "break"
        key = self._model_key(self._filtered[idx])
        if not self._start_model():
            self._select_model_solo(key)
            self._sync_list_selection()
        self._popup_rate_menu(event, key)
        return "break"

    def _popup_rate_menu(self, event, model_key: str) -> None:
        try:
            if self._rate_menu is not None:
                self._rate_menu.destroy()
        except Exception:
            pass
        menu = tk.Menu(self, tearoff=0)
        self._rate_menu = menu
        cur = int((self._model_meta.get(model_key) or {}).get("rating") or 0)
        menu.add_command(
            label=f"Rate: {model_key}",
            state="disabled",
        )
        menu.add_separator()
        for r in range(0, 6):
            stars = _stars(r) or "(none)"
            mark = " ✓" if r == cur else ""
            menu.add_command(
                label=f"{r}  {stars}{mark}",
                command=lambda rating=r, mk=model_key: self._set_rating(mk, rating),
            )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _on_compress_slide(self, _value=None) -> None:
        fmt = self.format_var.get() or "JPEG"
        try:
            val = int(round(float(self.compress_var.get())))
        except Exception:
            return
        if fmt == "JPEG":
            self._jpeg_quality = max(1, min(100, val))
            self._compress_value.configure(text=str(self._jpeg_quality))
        elif fmt == "WebP":
            self._webp_quality = max(1, min(100, val))
            self._compress_value.configure(text=str(self._webp_quality))
        elif fmt == "PNG":
            self._png_compress = max(0, min(9, val))
            self._compress_value.configure(text=str(self._png_compress))
        self._refresh_job_summary()

    def _set_compress_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for w in (self._compress_label, self._compress_slider, self._compress_value):
            try:
                w.configure(state=state)
            except Exception:
                pass
        try:
            self._compress_label.configure(text_color="#888" if enabled else "#555")
            self._compress_value.configure(text_color="#888" if enabled else "#555")
        except Exception:
            pass

    def _on_format_change(self, value: str | None = None) -> None:
        fmt = (value or self.format_var.get() or "JPEG").strip()
        if fmt == "JPEG":
            self._compress_label.configure(text="Quality")
            self._compress_slider.configure(from_=1, to=100, number_of_steps=99)
            self.compress_var.set(self._jpeg_quality)
            self._compress_value.configure(text=str(self._jpeg_quality))
            self._set_compress_controls_enabled(True)
        elif fmt == "WebP":
            self._compress_label.configure(text="Quality")
            self._compress_slider.configure(from_=1, to=100, number_of_steps=99)
            self.compress_var.set(self._webp_quality)
            self._compress_value.configure(text=str(self._webp_quality))
            self._set_compress_controls_enabled(True)
        elif fmt == "PNG":
            self._compress_label.configure(text="Compress")
            self._compress_slider.configure(from_=0, to=9, number_of_steps=9)
            self.compress_var.set(self._png_compress)
            self._compress_value.configure(text=str(self._png_compress))
            self._set_compress_controls_enabled(True)
        else:
            self._compress_label.configure(text="Compress")
            self._compress_value.configure(text="—")
            self._set_compress_controls_enabled(False)
        self._sync_preset_choice()
        self._refresh_job_summary()

    def _popup_save_menu(self) -> None:
        """Save… → Save preset… / Save as default (keeps one affordance)."""
        try:
            if self._save_menu is not None:
                self._save_menu.destroy()
        except Exception:
            pass
        menu = tk.Menu(self, tearoff=0)
        self._save_menu = menu
        menu.add_command(label="Save preset…", command=self._save_favorite_preset)
        menu.add_command(label="Save as default", command=self._save_as_default)
        try:
            self.update_idletasks()
            x = self._preset_save_btn.winfo_rootx()
            y = self._preset_save_btn.winfo_rooty() + self._preset_save_btn.winfo_height()
            menu.tk_popup(x, y)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _flash_job_summary(self, text: str, ms: int = 1800) -> None:
        var = getattr(self, "_job_summary_var", None)
        if var is None:
            return
        var.set(text)
        self._summary_flash_token = int(getattr(self, "_summary_flash_token", 0) or 0) + 1
        token = self._summary_flash_token

        def _restore() -> None:
            if getattr(self, "_summary_flash_token", 0) == token:
                self._refresh_job_summary()

        try:
            self.after(ms, _restore)
        except Exception:
            self._refresh_job_summary()

    def _persist_form_defaults(self, *, model: str | None = None) -> None:
        """Write current form into controller attrs (next open + prefs dump)."""
        if self.controller is None:
            return
        mode = self.factor_var.get() or "Model"
        target = self._target_scale()
        tile_cap = _max_tile_cap(self.tile_var.get())
        fmt_label = self.format_var.get()
        out_fmt = _FORMAT_TO_EXT.get(fmt_label, "jpg")
        gpu_label = self.gpu_var.get()
        cuda = self._gpu_indices.get(gpu_label, "0")
        models_dir = self._models_dir()
        chosen = (model if model is not None else self._start_model() or "").strip()
        if chosen.startswith("("):
            chosen = ""
        # Sync slider → cached values before write.
        self._on_compress_slide()
        try:
            setattr(self.controller, "fast_upscale_backend", "spandrel")
            setattr(self.controller, "spandrel_models_dir", models_dir)
            if chosen:
                setattr(self.controller, "spandrel_model", chosen)
            setattr(self.controller, "spandrel_advanced_open", bool(self._advanced_open))
            setattr(self.controller, "spandrel_max_tile", tile_cap)
            setattr(self.controller, "spandrel_cuda_device", cuda)
            setattr(self.controller, "spandrel_scale_mode", mode)
            setattr(self.controller, "spandrel_model_filter", self.filter_var.get())
            setattr(self.controller, "spandrel_output_format", out_fmt)
            lossy_q = (
                self._webp_quality if out_fmt == "webp" else self._jpeg_quality
            )
            setattr(
                self.controller,
                "spandrel_output_quality",
                max(1, min(100, int(lossy_q))),
            )
            setattr(
                self.controller,
                "spandrel_png_compress",
                max(0, min(9, int(self._png_compress))),
            )
            setattr(self.controller, "spandrel_model_meta", dict(self._model_meta))
            setattr(self.controller, "spandrel_favorite_presets", list(self._favorites))
            setattr(
                self.controller,
                "spandrel_model_sort",
                self.sort_mode_var.get() or _SORT_DEFAULT,
            )
            setattr(
                self.controller,
                "spandrel_show_preview",
                bool(self.show_preview_var.get()),
            )
            if mode == "Custom" and target is not None:
                setattr(self.controller, "spandrel_custom_scale", target)
            else:
                try:
                    custom = float(self.custom_scale_var.get() or 3)
                except Exception:
                    custom = 3.0
                setattr(self.controller, "spandrel_custom_scale", custom)
            # Remember only an explicit user suffix; empty = always model name.
            setattr(
                self.controller,
                "fast_upscale_suffix",
                (self.suffix_var.get() or "").strip(),
            )
        except Exception:
            pass

    def _save_as_default(self) -> None:
        """Pin the current form as the dialog's opening defaults (no job)."""
        if self.controller is None:
            messagebox.showinfo(
                "Upscale",
                "Defaults cannot be saved without an app session.",
                parent=self,
            )
            return
        self._persist_form_defaults()
        try:
            if hasattr(self.controller, "save_preferences"):
                self.controller.save_preferences()
        except Exception:
            pass
        self._flash_job_summary("Saved as default")

    def _save_favorite_preset(self) -> None:
        from tkinter import simpledialog

        model_key = (self._model_a or "").strip()
        if not model_key or model_key.startswith("("):
            messagebox.showwarning(
                "Favorite preset",
                "Select a model first.\n\nA preset stores one model (the A selection), plus scale and format.",
                parent=self,
            )
            return
        default_name = os.path.splitext(os.path.basename(model_key.replace("\\", "/")))[0]
        prompt = "Preset name (model, scale, format):"
        if (self._model_b or "").strip():
            prompt = "Preset name (saves model A, plus scale and format):"
        name = simpledialog.askstring(
            "Favorite preset",
            prompt,
            initialvalue=default_name,
            parent=self,
        )
        if name is None:
            return
        name = name.strip()
        if not name:
            return
        if is_builtin_name(name):
            messagebox.showwarning(
                "Favorite preset",
                f"“{name}” is a built-in preset.\n\nChoose a different name.",
                parent=self,
            )
            return
        existing = {p.get("name") for p in self._favorites}
        if name in existing:
            if not messagebox.askyesno(
                "Favorite preset",
                f"Replace existing preset “{name}”?",
                parent=self,
            ):
                return
        mode = self.factor_var.get() or "Model"
        target = self._target_scale()
        try:
            custom = float(self.custom_scale_var.get() or 3)
        except Exception:
            custom = 3.0
        if mode == "Custom" and target is not None:
            custom = float(target)
        fmt = _FORMAT_TO_EXT.get(self.format_var.get(), "png")
        preset = {
            "name": name,
            "model": model_key,
            "models_dir": self._models_dir(),
            "scale_mode": mode,
            "custom_scale": custom,
            "output_format": fmt,
        }
        self._favorites = upsert_preset(self._favorites, preset)
        self._persist_favorites()
        self._rebuild_preset_menu(prefer_name=name)
        self._sync_preset_choice()
        try:
            if self.controller is not None and hasattr(self.controller, "save_preferences"):
                self.controller.save_preferences()
        except Exception:
            pass
        self._flash_job_summary(f"Preset “{name}” saved")

    def _set_rating(self, model_key: str, rating: int) -> None:
        entry = dict(self._model_meta.get(model_key) or {})
        entry["rating"] = max(0, min(5, int(rating)))
        self._model_meta[model_key] = entry
        self._persist_meta()
        self._rebuild_list_labels(keep_selection=True)

    def _on_preview_timing(self, model_key: str, seconds: float) -> None:
        if not model_key or seconds <= 0:
            return
        entry = dict(self._model_meta.get(model_key) or {})
        entry["preview_s"] = round(float(seconds), 2)
        self._model_meta[model_key] = entry
        self._persist_meta()
        # Refresh labels without resetting A/B.
        try:
            self.after(0, lambda: self._rebuild_list_labels(keep_selection=True))
        except Exception:
            pass

    def _persist_meta(self) -> None:
        if self.controller is None:
            return
        try:
            setattr(self.controller, "spandrel_model_meta", dict(self._model_meta))
        except Exception:
            pass

    def _on_sort_change(self) -> None:
        if self.controller is not None:
            try:
                setattr(
                    self.controller,
                    "spandrel_model_sort",
                    self.sort_mode_var.get() or _SORT_DEFAULT,
                )
            except Exception:
                pass
        self._apply_filter()

    def _meta_speed_s(self, model_key: str) -> float:
        """Best known timing for sort-by-speed (prefer full, else preview)."""
        meta = self._model_meta.get(model_key) or {}
        try:
            full = float(meta.get("full_s") or 0)
            if full > 0:
                return full
        except Exception:
            pass
        try:
            preview = float(meta.get("preview_s") or 0)
            if preview > 0:
                return preview
        except Exception:
            pass
        return float("inf")  # unknown → bottom

    def _format_row_label(self, item: dict) -> str:
        key = self._model_key(item)
        label = key
        scale = item.get("scale")
        if scale:
            label = f"{label}   [{scale:g}×]"
        meta = self._model_meta.get(key) or {}
        t = _time_suffix(meta)
        if t:
            label = f"{label}  · {t}"
        stars = _stars(int(meta.get("rating") or 0))
        if stars:
            label = f"{label}  {stars}"
        # A› = first pick (left), B› = second (right / Start).
        if self._model_b and key == self._model_b:
            label = f"B› {label}"
        elif key == self._model_a:
            label = f"A› {label}"
        return label

    def _sync_list_selection(self) -> None:
        """Refresh A›/B› prefixes and highlight both slots."""
        self._rebuild_list_labels(keep_selection=True)

    def _rebuild_list_labels(self, *, keep_selection: bool = True) -> None:
        try:
            yview = self._listbox.yview()
        except Exception:
            yview = (0.0, 1.0)
        selected = set()
        if keep_selection:
            selected = {self._model_a, self._model_b} - {""}
        self._listbox.delete(0, tk.END)
        for item in self._filtered:
            self._listbox.insert(tk.END, self._format_row_label(item))
        if keep_selection:
            for i, item in enumerate(self._filtered):
                if self._model_key(item) in selected:
                    try:
                        self._listbox.selection_set(i)
                    except Exception:
                        pass
        try:
            self._listbox.yview_moveto(yview[0])
        except Exception:
            pass

    def _update_action_states(self) -> None:
        model = self._start_model()
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

        filtered: list[dict] = []
        for item in self._all_models:
            name = item.get("filename") or ""
            rel = str(item.get("rel") or item.get("label") or name)
            hay = f"{name} {rel}".lower()
            if needle and needle not in hay:
                continue
            filtered.append(item)

        mode = (self.sort_mode_var.get() or _SORT_DEFAULT).strip()
        if mode == "Rating":
            def _rating_key(it: dict) -> tuple:
                k = self._model_key(it)
                meta = self._model_meta.get(k) or {}
                return (-int(meta.get("rating") or 0), k.lower())

            filtered.sort(key=_rating_key)
        elif mode == "Speed":
            def _speed_key(it: dict) -> tuple:
                k = self._model_key(it)
                return (self._meta_speed_s(k), k.lower())

            filtered.sort(key=_speed_key)
        else:
            filtered.sort(key=lambda it: self._model_key(it).lower())

        self._filtered = filtered
        keys = {self._model_key(it) for it in filtered}
        if self._model_a and self._model_a not in keys:
            self._model_a = ""
        if self._model_b and self._model_b not in keys:
            self._model_b = ""
        if not self._model_a and filtered:
            # Prefer previously saved model_var if still visible.
            current = (self.model_var.get() or "").strip()
            pick = None
            for it in filtered:
                k = self._model_key(it)
                if k == current or it.get("filename") == current or it.get("path") == current:
                    pick = k
                    break
            self._model_a = pick or self._model_key(filtered[0])

        if self._model_a:
            self.model_var.set(self._model_a)
        else:
            if not self._all_models:
                self.model_var.set("(no models in folder)")
            else:
                self.model_var.set("(no match)")

        total = len(self._all_models)
        shown = len(filtered)
        self._count_var.set(f"{shown}/{total}")

        self._listbox.delete(0, tk.END)
        select_idx = 0
        for i, item in enumerate(filtered):
            self._listbox.insert(tk.END, self._format_row_label(item))
            if self._model_key(item) == self._model_a:
                select_idx = i
        if filtered:
            self._listbox.selection_clear(0, tk.END)
            for i, item in enumerate(filtered):
                k = self._model_key(item)
                if k == self._model_a or k == self._model_b:
                    self._listbox.selection_set(i)
            self._listbox.see(select_idx)

        self._update_action_states()
        self._notify_preview()
        self._sync_preset_choice()

    def _refresh_status(self, *, refit: bool = True) -> None:
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
            self._alert_var.set(rt.get("message") or "Spandrel is not installed.")
        elif not self._all_models:
            parts.append(wt.get("message") or "No models found.")
            self._alert_var.set(wt.get("message") or "No models in this folder.")
        else:
            parts.append(f"{len(self._all_models)} model(s) in folder (incl. subfolders)")
            parts.append(f"Browse: {OPENMODELDB_URL}")
            self._alert_var.set("")
        self.status_var.set("\n".join(parts))
        self._status_checked = True
        self._apply_filter()
        self._sync_engine_chrome()
        if refit:
            self.after(30, self._fit_and_center)

    def _sync_engine_chrome(self) -> None:
        """Keep Install / folder tools on the main page only while setup is broken."""
        if not getattr(self, "_status_checked", False):
            return
        healthy = bool(self._runtime_ok) and bool(self._all_models)
        try:
            self._tools.pack_forget()
        except Exception:
            pass
        if healthy:
            try:
                self._alert_wrap.pack_forget()
            except Exception:
                pass
            self._tools.pack(in_=self._engine_tools_host, fill="x")
            return
        if not self._alert_wrap.winfo_ismapped():
            anchor = getattr(self, "_preset_row", None)
            if anchor is not None:
                self._alert_wrap.pack(fill="x", pady=(0, 6), before=anchor)
            else:
                self._alert_wrap.pack(fill="x", pady=(0, 6))
        self._tools.pack(in_=self._alert_wrap, fill="x", pady=(6, 0))

    def _start(self) -> None:
        if self._install_running or not self._ready:
            return
        # Drop preview before batch so VRAM stays free for full-frame passes.
        if self.show_preview_var.get():
            self.show_preview_var.set(False)
            self._close_preview_window()
        out_dir = (self.out_dir_var.get() or "").strip()
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showwarning(
                "Upscale",
                "Choose a valid output folder.",
                parent=self,
            )
            return

        rt = spandrel_runtime_status(deep=True)
        if not rt.get("ready") and not rt.get("cpu_fallback"):
            if rt.get("error") == "spandrel_missing":
                if messagebox.askyesno(
                    "Upscale",
                    (
                        "Python package 'spandrel' is not installed.\n\n"
                        "Install it now into the app environment?"
                    ),
                    parent=self,
                ):
                    self._install_spandrel()
                return
            messagebox.showwarning(
                "Upscale",
                rt.get("message") or "Spandrel / PyTorch runtime is not ready.",
                parent=self,
            )
            return

        model = self._start_model()
        if not model or model.startswith("("):
            messagebox.showwarning(
                "Upscale",
                "Select a model from the list (use Filter if the folder is large).",
                parent=self,
            )
            return

        mode = self.factor_var.get()
        target = self._target_scale()
        if mode == "Custom" and target is None:
            messagebox.showwarning(
                "Upscale",
                "Enter a valid custom scale (e.g. 3 or 1.5).",
                parent=self,
            )
            return

        suffix = (self.suffix_var.get() or "").strip() or model_stem_suffix(model)
        gpu_label = self.gpu_var.get()
        cuda = self._gpu_indices.get(gpu_label, "0")
        models_dir = self._models_dir()
        tile_cap = _max_tile_cap(self.tile_var.get())
        fmt_label = self.format_var.get()
        out_fmt = _FORMAT_TO_EXT.get(fmt_label, "jpg")
        self._on_compress_slide()
        options = {
            "backend_id": "spandrel",
            "output_dir": out_dir,
            "model": model,
            "models_dir": models_dir,
            "auto_tile": True,
            "tile_cap": tile_cap,
            "tile_size": 0,
            "overlap": 32,
            "cuda_device": cuda,
            "target_scale": target,
            "suffix": suffix,
            "output_format": out_fmt,
            "jpeg_quality": max(
                1,
                min(
                    100,
                    int(
                        self._webp_quality
                        if out_fmt == "webp"
                        else self._jpeg_quality
                    ),
                ),
            ),
            "png_compress": max(0, min(9, int(self._png_compress))),
            "unload_after": True,
        }

        # Last run becomes the next opening defaults (same path as Save as default).
        self._persist_form_defaults(model=options["model"])

        self.result = {"paths": list(self.paths), "options": options}
        if callable(self.on_confirm):
            self.on_confirm(list(self.paths), options)
        self.destroy()
