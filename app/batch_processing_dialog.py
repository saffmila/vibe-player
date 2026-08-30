"""
Batch Convert / Rename dialog for selected thumbnail images.

Inspired by FastStone batch convert, simplified: operates on the current
selection (no internal file browser). Advanced options use a Preferences-style
left-nav window (Rotate / Flip, Crop, Resize, Canvas).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Any, Callable, Optional

import customtkinter as ctk
import tkinter as tk
from PIL import Image as PILImage
from tkinter import colorchooser, filedialog, messagebox

from image_loader import load_pil_frames, get_pil_image_size
from image_resize_dialog import (
    RESAMPLE_OPTIONS,
    compute_resize_size,
    prepare_image_for_save,
)

# Keep in sync with image_resize_dialog unit labels.
_UNIT_PIXELS = "Pixels (px)"
_UNIT_PERCENT = "Percentage (%)"

# Display label -> file extension
OUTPUT_FORMATS: dict[str, str] = {
    "JPG": ".jpg",
    "PNG": ".png",
    "WebP": ".webp",
    "BMP": ".bmp",
}

ROTATE_OPTIONS: dict[str, Optional[str]] = {
    "None": None,
    "90° CW": "rotate_right",
    "90° CCW": "rotate_left",
    "180°": "rotate_180",
}

_HASH_RUN = re.compile(r"#+")

# Preferences-like chrome for the advanced options window
_ADV_PANEL_BG = "#343434"
_ADV_NAV_WIDTH = 178
_SECTION_ROTATE_FLIP = "Rotate / Flip"
_SECTION_CROP = "Crop"
_SECTION_CANVAS = "Canvas"

_CANVAS_BG_PRESETS = {
    "Black": "#000000",
    "White": "#FFFFFF",
    "Transparent": "transparent",
    "Custom…": "custom",
}

# Photoshop / Photopea-style 3×3 pin points for crop origin & canvas placement.
ANCHOR_KEYS = (
    "top-left",
    "top-center",
    "top-right",
    "center-left",
    "center",
    "center-right",
    "bottom-left",
    "bottom-center",
    "bottom-right",
)


def apply_rename_pattern(pattern: str, *, stem: str, index: int) -> str:
    """
    Build a file stem from a rename pattern.

    Tokens:
      ``{name}`` — original stem
      ``#`` runs — zero-padded counter (``###`` → ``001``); first run uses ``index``
    Plain text is kept as-is. Empty / whitespace-only patterns fall back to ``{name}``.
    """
    text = (pattern or "").strip() or "{name}"
    text = text.replace("{name}", stem)

    used_index = False

    def _repl(match: re.Match) -> str:
        nonlocal used_index
        width = len(match.group(0))
        if not used_index:
            used_index = True
            return str(int(index)).zfill(width)
        return str(int(index)).zfill(width)

    return _HASH_RUN.sub(_repl, text)


def build_output_path(
    src_path: str,
    *,
    index: int,
    out_ext: str,
    output_dir: Optional[str],
    rename_enabled: bool,
    rename_pattern: str,
) -> str:
    """Resolve destination path for one source file."""
    stem = os.path.splitext(os.path.basename(src_path))[0]
    if rename_enabled:
        stem = apply_rename_pattern(rename_pattern, stem=stem, index=index)
        stem = re.sub(r'[<>:"/\\|?*]', "_", stem).strip(" .") or "image"
    directory = output_dir if output_dir else os.path.dirname(src_path)
    return os.path.join(directory, f"{stem}{out_ext}")


def _rotate_frame(im: PILImage.Image, op: Optional[str]) -> PILImage.Image:
    if not op:
        return im
    if op == "rotate_left":
        return im.rotate(90, expand=True)
    if op == "rotate_right":
        return im.rotate(-90, expand=True)
    if op == "rotate_180":
        return im.rotate(180, expand=True)
    return im


def _flip_frame(im: PILImage.Image, *, flip_h: bool = False, flip_v: bool = False) -> PILImage.Image:
    if flip_h:
        im = im.transpose(PILImage.FLIP_LEFT_RIGHT)
    if flip_v:
        im = im.transpose(PILImage.FLIP_TOP_BOTTOM)
    return im


def _parse_bg_color(bg_color: str) -> Optional[tuple[int, int, int]]:
    """Return RGB tuple, or None for transparent."""
    s = (bg_color or "#000000").strip()
    if s.lower() in ("transparent", "none"):
        return None
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return (0, 0, 0)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def anchor_offset(
    container_w: int,
    container_h: int,
    child_w: int,
    child_h: int,
    anchor: str,
) -> tuple[int, int]:
    """Top-left of ``child`` inside ``container`` for a 3×3 anchor key."""
    a = (anchor or "center").lower()
    if "left" in a:
        x = 0
    elif "right" in a:
        x = container_w - child_w
    else:
        x = (container_w - child_w) // 2
    if "top" in a:
        y = 0
    elif "bottom" in a:
        y = container_h - child_h
    else:
        y = (container_h - child_h) // 2
    return int(x), int(y)


def apply_crop(
    image: PILImage.Image,
    crop_w: int,
    crop_h: int,
    *,
    anchor: str = "center",
    offset_x: Optional[int] = None,
    offset_y: Optional[int] = None,
) -> PILImage.Image:
    """Crop to ``crop_w``×``crop_h`` using anchor or explicit top-left offsets."""
    orig_w, orig_h = image.size
    crop_w = max(1, min(int(crop_w), orig_w))
    crop_h = max(1, min(int(crop_h), orig_h))
    if offset_x is None or offset_y is None:
        offset_x, offset_y = anchor_offset(orig_w, orig_h, crop_w, crop_h, anchor)
    offset_x = max(0, min(int(offset_x), orig_w - crop_w))
    offset_y = max(0, min(int(offset_y), orig_h - crop_h))
    return image.crop((offset_x, offset_y, offset_x + crop_w, offset_y + crop_h))


def apply_canvas_size(
    image: PILImage.Image,
    canvas_w: int,
    canvas_h: int,
    bg_color: str = "#000000",
    *,
    anchor: str = "center",
) -> PILImage.Image:
    """Place image on a new canvas; ``anchor`` pins where the image sits."""
    canvas_w = max(1, int(canvas_w))
    canvas_h = max(1, int(canvas_h))
    rgb = _parse_bg_color(bg_color)
    transparent = rgb is None
    ox, oy = anchor_offset(canvas_w, canvas_h, image.width, image.height, anchor)

    if transparent:
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        canvas = PILImage.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        canvas.paste(image, (ox, oy), mask=image)
        return canvas

    if image.mode == "RGBA":
        canvas = PILImage.new("RGBA", (canvas_w, canvas_h), (*rgb, 255))
        canvas.paste(image, (ox, oy), mask=image)
        return canvas

    if image.mode != "RGB":
        image = image.convert("RGB")
    canvas = PILImage.new("RGB", (canvas_w, canvas_h), rgb)
    canvas.paste(image, (ox, oy))
    return canvas


def process_one_image(
    src_path: str,
    dest_path: str,
    *,
    rotate_op: Optional[str] = None,
    flip_h: bool = False,
    flip_v: bool = False,
    crop_settings: Optional[dict[str, Any]] = None,
    resize_settings: Optional[dict[str, Any]] = None,
    canvas_settings: Optional[dict[str, Any]] = None,
    quality: int = 90,
    png_compress: int = 6,
) -> str:
    """
    Load ``src_path``, apply rotate → flip → crop → resize → canvas, then save.

    ``quality`` is used for JPG/WebP (1–100).
    ``png_compress`` is Pillow ``compress_level`` (0–9; higher = smaller/slower).
    """
    frames, durations = load_pil_frames(src_path)
    if not frames:
        raise ValueError("no frames")

    out_frames = []
    for im in frames:
        im = _rotate_frame(im, rotate_op)
        im = _flip_frame(im, flip_h=flip_h, flip_v=flip_v)
        if crop_settings:
            if crop_settings.get("custom_offset"):
                ox = crop_settings.get("offset_x")
                oy = crop_settings.get("offset_y")
                im = apply_crop(
                    im,
                    int(crop_settings["width"]),
                    int(crop_settings["height"]),
                    offset_x=None if ox is None else int(ox),
                    offset_y=None if oy is None else int(oy),
                )
            else:
                # Legacy: center=True / no anchor → center.
                if crop_settings.get("center") is False and "anchor" not in crop_settings:
                    anchor = "top-left"
                else:
                    anchor = str(crop_settings.get("anchor") or "center")
                im = apply_crop(
                    im,
                    int(crop_settings["width"]),
                    int(crop_settings["height"]),
                    anchor=anchor,
                )
        out_frames.append(im)

    if resize_settings:
        unit = resize_settings.get("unit") or "Percentage (%)"
        width_val = float(resize_settings["width_val"])
        height_val = float(resize_settings["height_val"])
        lock_aspect = bool(resize_settings.get("lock_aspect", True))
        filt = resize_settings.get("resample_filter", PILImage.LANCZOS)
        resized = []
        for im in out_frames:
            ow, oh = im.size
            nw, nh = compute_resize_size(
                ow,
                oh,
                unit=unit,
                width_val=width_val,
                height_val=height_val,
                lock_aspect=lock_aspect,
            )
            if nw > 50000 or nh > 50000:
                raise ValueError("target size too large")
            resized.append(im.resize((nw, nh), filt))
        out_frames = resized

    if canvas_settings:
        cw = int(canvas_settings["width"])
        ch = int(canvas_settings["height"])
        bg = str(canvas_settings.get("bg_color") or "#000000")
        anchor = str(canvas_settings.get("anchor") or "center")
        out_frames = [
            apply_canvas_size(im, cw, ch, bg_color=bg, anchor=anchor)
            for im in out_frames
        ]

    ext = os.path.splitext(dest_path)[1].lower()
    if ext in (".jpg", ".jpeg", ".bmp", ".png") and len(out_frames) > 1:
        out_frames = [out_frames[0]]
        durations = None

    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)

    dest_dir = os.path.dirname(dest_path) or "."
    fd, tmp_path = tempfile.mkstemp(suffix=ext or ".tmp", dir=dest_dir)
    os.close(fd)
    try:
        first = prepare_image_for_save(out_frames[0], dest_path)
        animated = len(out_frames) > 1 and ext in (".gif", ".webp")
        save_kw: dict = {}
        if animated:
            rest = [prepare_image_for_save(f, dest_path) for f in out_frames[1:]]
            save_kw.update(save_all=True, append_images=rest, loop=0)
            if durations:
                save_kw["duration"] = list(durations)[: len(out_frames)]

        q = max(1, min(100, int(quality)))
        if ext in (".jpg", ".jpeg"):
            save_kw.update(quality=q, subsampling=0, optimize=True)
        elif ext == ".webp":
            save_kw.update(quality=q, method=4)
        elif ext == ".png":
            # Lossless deflate level — not "quality". 0 = store, 9 = max compress.
            level = max(0, min(9, int(png_compress)))
            save_kw.update(compress_level=level, optimize=level > 0)

        first.save(tmp_path, **save_kw)
        os.replace(tmp_path, dest_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise

    return dest_path


def _center_on_parent(window, parent):
    try:
        window.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        w = window.winfo_reqwidth()
        h = window.winfo_reqheight()
        window.geometry(f"+{px + max(0, (pw - w) // 2)}+{py + max(0, (ph - h) // 2)}")
    except Exception:
        pass


class AnchorGridWidget(ctk.CTkFrame):
    """
    Clickable 3×3 anchor grid (Photoshop / Photopea style).

    Selected cell shows a filled circle; others are empty pads.
    """

    _CELL = 28
    _GAP = 3
    _SEL = ("#c8c8c8", "#d0d0d0")
    _IDLE = ("#4a4a4a", "#3a3a3a")
    _BORDER = ("#6a6a6a", "#555555")

    def __init__(
        self,
        master,
        *,
        value: str = "center",
        command: Optional[Callable[[str], None]] = None,
        **kwargs,
    ):
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)
        self._command = command
        self._enabled = True
        initial = value if value in ANCHOR_KEYS else "center"
        self._var = tk.StringVar(value=initial)
        self._cells: dict[str, ctk.CTkButton] = {}

        outer = ctk.CTkFrame(self, fg_color=("#2a2a2a", "#2a2a2a"), corner_radius=6)
        outer.pack()
        for i, key in enumerate(ANCHOR_KEYS):
            r, c = divmod(i, 3)
            btn = ctk.CTkButton(
                outer,
                text="●" if key == initial else "·",
                width=self._CELL,
                height=self._CELL,
                corner_radius=4,
                border_width=1,
                border_color=self._BORDER,
                fg_color=self._SEL if key == initial else self._IDLE,
                hover_color=("#5a5a5a", "#505050"),
                text_color=("white", "white"),
                font=ctk.CTkFont(size=14),
                command=lambda k=key: self._select(k),
            )
            btn.grid(row=r, column=c, padx=self._GAP, pady=self._GAP)
            self._cells[key] = btn

    def get(self) -> str:
        return self._var.get()

    def set(self, value: str):
        if value not in ANCHOR_KEYS:
            value = "center"
        self._var.set(value)
        self._refresh()

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        state = "normal" if self._enabled else "disabled"
        for btn in self._cells.values():
            try:
                btn.configure(state=state)
            except Exception:
                pass

    def _select(self, key: str):
        if not self._enabled:
            return
        self._var.set(key)
        self._refresh()
        if self._command:
            try:
                self._command(key)
            except Exception:
                pass

    def _refresh(self):
        cur = self._var.get()
        for key, btn in self._cells.items():
            selected = key == cur
            try:
                btn.configure(
                    text="●" if selected else "·",
                    fg_color=self._SEL if selected else self._IDLE,
                    text_color=("#1a1a1a", "#1a1a1a") if selected else ("#888888", "#888888"),
                )
            except Exception:
                pass


class BatchAdvancedOptionsDialog(ctk.CTkToplevel):
    """
    Preferences-style advanced options: left nav folders + right content.

    Each nav row has an enable checkbox + icon + name so active steps are
    visible at a glance. Sections: Rotate / Flip, Crop, Resize, Canvas.
    """

    def __init__(
        self,
        parent: "BatchProcessDialog",
        *,
        paths: list[str],
        initial: dict[str, Any],
        on_apply: Callable[[dict[str, Any]], None],
    ):
        super().__init__(parent)
        self.title("Advanced Options")
        self.geometry("640x480")
        self.resizable(True, True)
        self.minsize(560, 400)
        self._paths = list(paths)
        self._parent_dlg = parent
        self._on_apply = on_apply

        self._resize_enabled = tk.BooleanVar(value=bool(initial.get("resize_enabled")))
        self._rotate_enabled = tk.BooleanVar(value=bool(initial.get("rotate_enabled")))
        self._rotate_var = tk.StringVar(value=initial.get("rotate_label") or "None")
        self._flip_h_var = tk.BooleanVar(value=bool(initial.get("flip_h")))
        self._flip_v_var = tk.BooleanVar(value=bool(initial.get("flip_v")))
        self._crop_enabled = tk.BooleanVar(value=bool(initial.get("crop_enabled")))
        self._canvas_enabled = tk.BooleanVar(value=bool(initial.get("canvas_enabled")))

        # Inline resize state (mirrors BatchResizeImageDialog).
        self._lock_aspect = True
        self._syncing = False
        init_rs = initial.get("resize_settings") or {}
        init_crop = initial.get("crop_settings") or {}
        init_canvas = initial.get("canvas_settings") or {}
        self._unit = init_rs.get("unit") or _UNIT_PERCENT
        self._ref_w, self._ref_h = 1920, 1080
        if self._paths:
            try:
                self._ref_w, self._ref_h = get_pil_image_size(self._paths[0])
            except Exception:
                pass
        self._aspect = self._ref_w / max(1, self._ref_h)
        if init_rs.get("lock_aspect") is False:
            self._lock_aspect = False

        try:
            self.transient(parent.winfo_toplevel())
        except Exception:
            pass

        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        nav = ctk.CTkFrame(main, width=_ADV_NAV_WIDTH)
        nav.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        nav.grid_propagate(False)

        content_shell = ctk.CTkFrame(main, fg_color=_ADV_PANEL_BG)
        content_shell.grid(row=0, column=1, sticky="nsew")
        content_shell.grid_rowconfigure(0, weight=1)
        content_shell.grid_columnconfigure(0, weight=1)

        self._section_frames: dict[str, ctk.CTkFrame] = {}
        self._section_buttons: dict[str, ctk.CTkButton] = {}
        self._section_headings: dict[str, list[tuple[ctk.CTkLabel, str]]] = {}
        self._section_content_roots: dict[str, list] = {}
        self._section_enable_vars: dict[str, tk.BooleanVar] = {
            _SECTION_ROTATE_FLIP: self._rotate_enabled,
            _SECTION_CROP: self._crop_enabled,
            "Resize": self._resize_enabled,
            _SECTION_CANVAS: self._canvas_enabled,
        }
        self._current_section = _SECTION_ROTATE_FLIP

        def _show(name: str):
            self._current_section = name
            for section_name, frame in self._section_frames.items():
                if section_name == name:
                    frame.grid(row=0, column=0, sticky="nsew")
                else:
                    frame.grid_remove()
            for section_name, button in self._section_buttons.items():
                selected = section_name == name
                button.configure(
                    fg_color=("#5f6f7f", "#37424d") if selected else "transparent",
                    text_color=("white", "white") if selected else ("gray15", "gray85"),
                    hover_color=("#6a7a8a", "#45525e") if selected else ("gray70", "gray30"),
                )

        def _add_section(
            name: str, icon: str, enabled_var: tk.BooleanVar
        ) -> ctk.CTkFrame:
            outer = ctk.CTkFrame(content_shell, fg_color=_ADV_PANEL_BG)
            outer.grid_rowconfigure(0, weight=1)
            outer.grid_columnconfigure(0, weight=1)
            body = ctk.CTkFrame(outer, fg_color=_ADV_PANEL_BG)
            body.grid(row=0, column=0, sticky="nsew", padx=14, pady=12)
            self._section_frames[name] = outer

            row = ctk.CTkFrame(nav, fg_color="transparent")
            row.pack(
                fill="x",
                padx=4,
                pady=(6 if not self._section_buttons else 2, 2),
            )
            ctk.CTkCheckBox(
                row,
                text="",
                width=22,
                checkbox_width=18,
                checkbox_height=18,
                variable=enabled_var,
                command=lambda n=name: self._on_nav_enable(n),
            ).pack(side="left", padx=(2, 0))
            btn = ctk.CTkButton(
                row,
                text=f"{icon}  {name}",
                command=lambda n=name: _show(n),
                anchor="w",
                height=30,
                fg_color="transparent",
                text_color=("gray15", "gray85"),
                hover_color=("gray70", "gray30"),
            )
            btn.pack(side="left", fill="x", expand=True, padx=(2, 2))
            self._section_buttons[name] = btn
            outer.grid_remove()
            return body

        # --- Rotate / Flip ---
        rotate_body = _add_section(_SECTION_ROTATE_FLIP, "↻", self._rotate_enabled)
        self._add_section_heading(rotate_body, _SECTION_ROTATE_FLIP, "↻  Rotate")
        self._rotate_controls = ctk.CTkFrame(rotate_body, fg_color="transparent")
        self._rotate_controls.pack(fill="x")
        self._section_content_roots.setdefault(_SECTION_ROTATE_FLIP, []).append(
            self._rotate_controls
        )
        ctk.CTkLabel(self._rotate_controls, text="Angle:", anchor="w").pack(anchor="w")
        self._rotate_menu = ctk.CTkOptionMenu(
            self._rotate_controls,
            variable=self._rotate_var,
            values=list(ROTATE_OPTIONS.keys()),
            width=160,
            height=28,
            corner_radius=6,
        )
        self._rotate_menu.pack(anchor="w", pady=(6, 0))

        self._add_section_heading(rotate_body, _SECTION_ROTATE_FLIP, "↔  Flip", pady=(18, 8))
        self._flip_controls = ctk.CTkFrame(rotate_body, fg_color="transparent")
        self._flip_controls.pack(fill="x")
        self._section_content_roots.setdefault(_SECTION_ROTATE_FLIP, []).append(
            self._flip_controls
        )

        flip_h_row = ctk.CTkFrame(self._flip_controls, fg_color="transparent")
        flip_h_row.pack(fill="x", pady=2)
        ctk.CTkLabel(flip_h_row, text="↔", width=28, anchor="center").pack(side="left")
        self._flip_h_cb = ctk.CTkCheckBox(
            flip_h_row,
            text="Flip horizontally",
            variable=self._flip_h_var,
        )
        self._flip_h_cb.pack(side="left", padx=(4, 0))

        flip_v_row = ctk.CTkFrame(self._flip_controls, fg_color="transparent")
        flip_v_row.pack(fill="x", pady=2)
        ctk.CTkLabel(flip_v_row, text="↕", width=28, anchor="center").pack(side="left")
        self._flip_v_cb = ctk.CTkCheckBox(
            flip_v_row,
            text="Flip vertically",
            variable=self._flip_v_var,
        )
        self._flip_v_cb.pack(side="left", padx=(4, 0))

        # --- Crop ---
        crop_body = _add_section(_SECTION_CROP, "✂", self._crop_enabled)
        self._add_section_heading(crop_body, _SECTION_CROP, "✂  Crop")
        self._crop_controls = ctk.CTkFrame(crop_body, fg_color="transparent")
        self._crop_controls.pack(fill="x")
        self._section_content_roots.setdefault(_SECTION_CROP, []).append(self._crop_controls)

        crop_size = ctk.CTkFrame(self._crop_controls, fg_color="transparent")
        crop_size.pack(fill="x", pady=4)
        ctk.CTkLabel(crop_size, text="Width:", width=55, anchor="w").pack(side="left")
        self._crop_w_var = tk.StringVar(
            value=str(int(init_crop.get("width") or min(self._ref_w, 1000)))
        )
        self._crop_w_entry = ctk.CTkEntry(
            crop_size, textvariable=self._crop_w_var, width=90, height=28, justify="center"
        )
        self._crop_w_entry.pack(side="left", padx=(4, 12))
        ctk.CTkLabel(crop_size, text="Height:", width=55, anchor="w").pack(side="left")
        self._crop_h_var = tk.StringVar(
            value=str(int(init_crop.get("height") or min(self._ref_h, 1000)))
        )
        self._crop_h_entry = ctk.CTkEntry(
            crop_size, textvariable=self._crop_h_var, width=90, height=28, justify="center"
        )
        self._crop_h_entry.pack(side="left", padx=(4, 0))

        anchor_row = ctk.CTkFrame(self._crop_controls, fg_color="transparent")
        anchor_row.pack(fill="x", pady=(10, 4))
        ctk.CTkLabel(anchor_row, text="Anchor:", width=70, anchor="w").pack(
            side="left", anchor="n", pady=4
        )
        init_crop_anchor = str(init_crop.get("anchor") or "center")
        if init_crop_anchor not in ANCHOR_KEYS:
            init_crop_anchor = "center"
        self._crop_anchor = AnchorGridWidget(anchor_row, value=init_crop_anchor)
        self._crop_anchor.pack(side="left", padx=(4, 0))

        self._crop_custom_var = tk.BooleanVar(
            value=bool(init_crop.get("custom_offset"))
            or (
                init_crop.get("offset_x") is not None
                and init_crop.get("center") is False
                and "anchor" not in init_crop
            )
        )
        self._crop_custom_cb = ctk.CTkCheckBox(
            self._crop_controls,
            text="Use custom X/Y offset",
            variable=self._crop_custom_var,
            command=self._on_crop_custom_toggle,
        )
        self._crop_custom_cb.pack(anchor="w", pady=(10, 4))

        crop_off = ctk.CTkFrame(self._crop_controls, fg_color="transparent")
        crop_off.pack(fill="x", pady=4)
        ctk.CTkLabel(crop_off, text="Offset X:", width=70, anchor="w").pack(side="left")
        self._crop_ox_var = tk.StringVar(
            value="" if init_crop.get("offset_x") is None else str(int(init_crop["offset_x"]))
        )
        self._crop_ox_entry = ctk.CTkEntry(
            crop_off, textvariable=self._crop_ox_var, width=80, height=28, justify="center"
        )
        self._crop_ox_entry.pack(side="left", padx=(4, 12))
        ctk.CTkLabel(crop_off, text="Offset Y:", width=70, anchor="w").pack(side="left")
        self._crop_oy_var = tk.StringVar(
            value="" if init_crop.get("offset_y") is None else str(int(init_crop["offset_y"]))
        )
        self._crop_oy_entry = ctk.CTkEntry(
            crop_off, textvariable=self._crop_oy_var, width=80, height=28, justify="center"
        )
        self._crop_oy_entry.pack(side="left", padx=(4, 0))
        ctk.CTkLabel(
            self._crop_controls,
            text="Anchor picks which edge/corner stays; custom offset is top-left of the crop.",
            font=ctk.CTkFont(size=11),
            text_color=("gray70", "gray60"),
            anchor="w",
            wraplength=360,
            justify="left",
        ).pack(fill="x", pady=(8, 0))

        # --- Resize ---
        resize_body = _add_section("Resize", "📐", self._resize_enabled)
        self._add_section_heading(resize_body, "Resize", "📐  Resize")
        self._resize_controls = ctk.CTkFrame(resize_body, fg_color="transparent")
        self._resize_controls.pack(fill="x")
        self._section_content_roots.setdefault("Resize", []).append(self._resize_controls)

        unit_row = ctk.CTkFrame(self._resize_controls, fg_color="transparent")
        unit_row.pack(fill="x", pady=4)
        ctk.CTkLabel(unit_row, text="Units:", width=70, anchor="w").pack(side="left")
        self._unit_var = tk.StringVar(value=self._unit)
        ctk.CTkOptionMenu(
            unit_row,
            variable=self._unit_var,
            values=[_UNIT_PERCENT, _UNIT_PIXELS],
            command=self._on_unit_change,
            width=160,
            height=28,
            corner_radius=6,
        ).pack(side="left", padx=(8, 0))

        size_row = ctk.CTkFrame(self._resize_controls, fg_color="transparent")
        size_row.pack(fill="x", pady=4)
        ctk.CTkLabel(size_row, text="Width:", width=55, anchor="w").pack(side="left")
        if init_rs:
            self._w_var = tk.StringVar(value=f"{float(init_rs.get('width_val', 100)):g}")
            self._h_var = tk.StringVar(value=f"{float(init_rs.get('height_val', 100)):g}")
        else:
            self._w_var = tk.StringVar(value="100")
            self._h_var = tk.StringVar(value="100")
        self._w_entry = ctk.CTkEntry(
            size_row,
            textvariable=self._w_var,
            width=90,
            height=28,
            corner_radius=6,
            justify="center",
        )
        self._w_entry.pack(side="left", padx=(4, 8))
        self._w_entry.bind("<KeyRelease>", lambda _e: self._on_width_edit())
        self._w_entry.bind("<FocusOut>", lambda _e: self._on_width_edit())

        self._lock_btn = ctk.CTkButton(
            size_row,
            text="🔗" if self._lock_aspect else "🔓",
            width=36,
            height=28,
            corner_radius=6,
            fg_color="gray30",
            hover_color="gray25",
            command=self._toggle_lock,
        )
        self._lock_btn.pack(side="left", padx=4)

        ctk.CTkLabel(size_row, text="Height:", width=55, anchor="w").pack(
            side="left", padx=(8, 0)
        )
        self._h_entry = ctk.CTkEntry(
            size_row,
            textvariable=self._h_var,
            width=90,
            height=28,
            corner_radius=6,
            justify="center",
        )
        self._h_entry.pack(side="left", padx=(4, 0))
        self._h_entry.bind("<KeyRelease>", lambda _e: self._on_height_edit())
        self._h_entry.bind("<FocusOut>", lambda _e: self._on_height_edit())

        method_row = ctk.CTkFrame(self._resize_controls, fg_color="transparent")
        method_row.pack(fill="x", pady=4)
        ctk.CTkLabel(method_row, text="Resample:", width=70, anchor="w").pack(side="left")
        init_filt = init_rs.get("resample_filter", PILImage.LANCZOS)
        init_method = next(
            (k for k, v in RESAMPLE_OPTIONS.items() if v == init_filt),
            "Lanczos (High Quality)",
        )
        self._method_var = tk.StringVar(value=init_method)
        ctk.CTkOptionMenu(
            method_row,
            variable=self._method_var,
            values=list(RESAMPLE_OPTIONS.keys()),
            width=200,
            height=28,
            corner_radius=6,
        ).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(
            self._resize_controls,
            text=(
                "Percentage keeps each image’s aspect when linked. "
                "Pixels applies the same absolute size to every file."
            ),
            font=ctk.CTkFont(size=11),
            text_color=("gray70", "gray60"),
            anchor="w",
            wraplength=360,
            justify="left",
        ).pack(fill="x", pady=(8, 0))

        # --- Canvas ---
        canvas_body = _add_section(_SECTION_CANVAS, "🖼", self._canvas_enabled)
        self._add_section_heading(canvas_body, _SECTION_CANVAS, "🖼  Canvas Size")
        self._canvas_controls = ctk.CTkFrame(canvas_body, fg_color="transparent")
        self._canvas_controls.pack(fill="x")
        self._section_content_roots.setdefault(_SECTION_CANVAS, []).append(
            self._canvas_controls
        )

        canvas_size = ctk.CTkFrame(self._canvas_controls, fg_color="transparent")
        canvas_size.pack(fill="x", pady=4)
        ctk.CTkLabel(canvas_size, text="Width:", width=55, anchor="w").pack(side="left")
        self._canvas_w_var = tk.StringVar(
            value=str(int(init_canvas.get("width") or self._ref_w))
        )
        self._canvas_w_entry = ctk.CTkEntry(
            canvas_size,
            textvariable=self._canvas_w_var,
            width=90,
            height=28,
            justify="center",
        )
        self._canvas_w_entry.pack(side="left", padx=(4, 12))
        ctk.CTkLabel(canvas_size, text="Height:", width=55, anchor="w").pack(side="left")
        self._canvas_h_var = tk.StringVar(
            value=str(int(init_canvas.get("height") or self._ref_h))
        )
        self._canvas_h_entry = ctk.CTkEntry(
            canvas_size,
            textvariable=self._canvas_h_var,
            width=90,
            height=28,
            justify="center",
        )
        self._canvas_h_entry.pack(side="left", padx=(4, 0))

        canvas_anchor_row = ctk.CTkFrame(self._canvas_controls, fg_color="transparent")
        canvas_anchor_row.pack(fill="x", pady=(10, 4))
        ctk.CTkLabel(canvas_anchor_row, text="Anchor:", width=70, anchor="w").pack(
            side="left", anchor="n", pady=4
        )
        init_canvas_anchor = str(init_canvas.get("anchor") or "center")
        if init_canvas_anchor not in ANCHOR_KEYS:
            init_canvas_anchor = "center"
        self._canvas_anchor = AnchorGridWidget(
            canvas_anchor_row, value=init_canvas_anchor
        )
        self._canvas_anchor.pack(side="left", padx=(4, 0))
        ctk.CTkLabel(
            self._canvas_controls,
            text="Pin where the image sits when the canvas grows or shrinks.",
            font=ctk.CTkFont(size=11),
            text_color=("gray70", "gray60"),
            anchor="w",
            wraplength=360,
            justify="left",
        ).pack(fill="x", pady=(0, 4))

        bg_row = ctk.CTkFrame(self._canvas_controls, fg_color="transparent")
        bg_row.pack(fill="x", pady=(8, 4))
        ctk.CTkLabel(bg_row, text="Background:", width=90, anchor="w").pack(side="left")
        init_bg = str(init_canvas.get("bg_color") or "#000000")
        preset_label = "Black"
        for label, val in _CANVAS_BG_PRESETS.items():
            if val == "custom":
                continue
            if init_bg.lower() == str(val).lower() or (
                label == "Black" and init_bg.lower() in ("#000", "#000000")
            ):
                preset_label = label
                break
        else:
            if init_bg.lower() not in ("#000000", "#ffffff", "transparent", "none"):
                preset_label = "Custom…"
        self._canvas_bg_preset = tk.StringVar(value=preset_label)
        self._canvas_bg_menu = ctk.CTkOptionMenu(
            bg_row,
            variable=self._canvas_bg_preset,
            values=list(_CANVAS_BG_PRESETS.keys()),
            command=self._on_canvas_bg_preset,
            width=140,
            height=28,
        )
        self._canvas_bg_menu.pack(side="left", padx=(8, 8))
        init_custom_hex = init_bg if preset_label == "Custom…" else "#808080"
        if preset_label == "Black":
            init_custom_hex = "#000000"
        elif preset_label == "White":
            init_custom_hex = "#FFFFFF"
        self._canvas_bg_hex = tk.StringVar(
            value=init_bg if preset_label == "Custom…" else init_custom_hex
        )
        self._canvas_bg_swatch = ctk.CTkButton(
            bg_row,
            text="",
            width=28,
            height=28,
            corner_radius=6,
            border_width=1,
            border_color=("gray50", "gray40"),
            fg_color=self._normalize_hex_color(self._canvas_bg_hex.get()) or "#808080",
            hover_color=self._normalize_hex_color(self._canvas_bg_hex.get()) or "#808080",
            command=self._pick_canvas_bg_color,
        )
        self._canvas_bg_swatch.pack(side="left", padx=(0, 6))
        self._canvas_bg_entry = ctk.CTkEntry(
            bg_row,
            textvariable=self._canvas_bg_hex,
            width=90,
            height=28,
            placeholder_text="#RRGGBB",
        )
        self._canvas_bg_entry.pack(side="left")
        self._canvas_bg_entry.bind("<KeyRelease>", lambda _e: self._sync_canvas_bg_swatch())
        self._canvas_bg_entry.bind("<FocusOut>", lambda _e: self._sync_canvas_bg_swatch())
        self._canvas_bg_pick_btn = ctk.CTkButton(
            bg_row,
            text="Pick…",
            width=56,
            height=28,
            command=self._pick_canvas_bg_color,
        )
        self._canvas_bg_pick_btn.pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            self._canvas_controls,
            text="Image is centered. Smaller canvas clips; larger canvas pads.",
            font=ctk.CTkFont(size=11),
            text_color=("gray70", "gray60"),
            anchor="w",
            wraplength=360,
            justify="left",
        ).pack(fill="x", pady=(8, 0))

        self._on_nav_enable(_SECTION_ROTATE_FLIP)
        self._on_nav_enable(_SECTION_CROP)
        self._on_nav_enable("Resize")
        self._on_nav_enable(_SECTION_CANVAS)
        self._on_crop_custom_toggle()
        self._on_canvas_bg_preset(self._canvas_bg_preset.get())
        _show(_SECTION_ROTATE_FLIP)

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(6, 12))
        ctk.CTkButton(
            btn_row,
            text="Cancel",
            width=100,
            height=30,
            fg_color="gray30",
            hover_color="gray25",
            command=self._on_cancel,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btn_row,
            text="OK",
            width=100,
            height=30,
            command=self._on_ok,
        ).pack(side="right")

        self.bind("<Escape>", lambda _e: self._on_cancel())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        _center_on_parent(self, parent)
        self.lift()
        self.focus_force()
        self.after(10, lambda: self.grab_set())

    def _add_section_heading(
        self,
        parent,
        section: str,
        base_text: str,
        *,
        pady=(0, 10),
    ) -> ctk.CTkLabel:
        """Create a section heading that can show an inactive state."""
        label = ctk.CTkLabel(
            parent,
            text=base_text,
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        )
        label.pack(fill="x", pady=pady)
        self._section_headings.setdefault(section, []).append((label, base_text))
        return label

    def _set_section_heading_active(self, section: str, enabled: bool):
        """Grey title + '(not active)', and dim all labels inside the section."""
        for label, base in self._section_headings.get(section, []):
            try:
                if enabled:
                    label.configure(
                        text=base,
                        text_color=("gray90", "#f0f0f0"),
                    )
                else:
                    label.configure(
                        text=f"{base}  (not active)",
                        text_color=("gray50", "gray55"),
                    )
            except Exception:
                pass
        for root in self._section_content_roots.get(section, []):
            self._set_widget_tree_text_active(root, enabled)

    def _set_widget_tree_text_active(self, widget, enabled: bool):
        """Recursively grey / restore label & checkbox text under a section body."""
        inactive = ("gray50", "gray55")
        for child in widget.winfo_children():
            if isinstance(child, (ctk.CTkLabel, ctk.CTkCheckBox)):
                try:
                    if not hasattr(child, "_active_text_color"):
                        child._active_text_color = child.cget("text_color")
                    child.configure(
                        text_color=(
                            child._active_text_color if enabled else inactive
                        )
                    )
                except Exception:
                    pass
            self._set_widget_tree_text_active(child, enabled)

    def _on_nav_enable(self, name: str):
        if name == "Resize":
            self._on_resize_toggle()
        elif name == _SECTION_ROTATE_FLIP:
            self._on_rotate_toggle()
        elif name == _SECTION_CROP:
            self._on_crop_toggle()
        elif name == _SECTION_CANVAS:
            self._on_canvas_toggle()

    def _set_resize_controls_state(self, state: str):
        for w in (
            getattr(self, "_w_entry", None),
            getattr(self, "_h_entry", None),
            getattr(self, "_lock_btn", None),
        ):
            if w is None:
                continue
            try:
                w.configure(state=state)
            except Exception:
                pass
        for child in self._resize_controls.winfo_children():
            for sub in child.winfo_children():
                if isinstance(sub, ctk.CTkOptionMenu):
                    try:
                        sub.configure(state=state)
                    except Exception:
                        pass

    def _on_resize_toggle(self):
        enabled = self._resize_enabled.get()
        self._set_section_heading_active("Resize", enabled)
        self._set_resize_controls_state("normal" if enabled else "disabled")

    def _on_rotate_toggle(self):
        enabled = self._rotate_enabled.get()
        self._set_section_heading_active(_SECTION_ROTATE_FLIP, enabled)
        state = "normal" if enabled else "disabled"
        try:
            self._rotate_menu.configure(state=state)
        except Exception:
            pass
        for cb in (getattr(self, "_flip_h_cb", None), getattr(self, "_flip_v_cb", None)):
            if cb is None:
                continue
            try:
                cb.configure(state=state)
            except Exception:
                pass

    def _on_crop_toggle(self):
        enabled = self._crop_enabled.get()
        self._set_section_heading_active(_SECTION_CROP, enabled)
        state = "normal" if enabled else "disabled"
        for w in (
            self._crop_w_entry,
            self._crop_h_entry,
            self._crop_custom_cb,
            self._crop_ox_entry,
            self._crop_oy_entry,
        ):
            try:
                w.configure(state=state)
            except Exception:
                pass
        self._crop_anchor.set_enabled(enabled and not self._crop_custom_var.get())
        if enabled:
            self._on_crop_custom_toggle()

    def _on_crop_custom_toggle(self):
        if not self._crop_enabled.get():
            self._crop_anchor.set_enabled(False)
            return
        custom = bool(self._crop_custom_var.get())
        self._crop_anchor.set_enabled(not custom)
        off_state = "normal" if custom else "disabled"
        for w in (self._crop_ox_entry, self._crop_oy_entry):
            try:
                w.configure(state=off_state)
            except Exception:
                pass

    def _on_canvas_toggle(self):
        enabled = self._canvas_enabled.get()
        self._set_section_heading_active(_SECTION_CANVAS, enabled)
        state = "normal" if enabled else "disabled"
        for w in (
            self._canvas_w_entry,
            self._canvas_h_entry,
            self._canvas_bg_menu,
            self._canvas_bg_swatch,
            self._canvas_bg_pick_btn,
        ):
            try:
                w.configure(state=state)
            except Exception:
                pass
        self._canvas_anchor.set_enabled(enabled)
        if enabled:
            self._on_canvas_bg_preset(self._canvas_bg_preset.get())
        else:
            try:
                self._canvas_bg_entry.configure(state="disabled")
            except Exception:
                pass

    def _normalize_hex_color(self, value: str) -> Optional[str]:
        s = (value or "").strip()
        if not s or s.lower() in ("transparent", "none"):
            return None
        if not s.startswith("#"):
            s = "#" + s
        body = s[1:]
        if len(body) == 3:
            body = "".join(ch * 2 for ch in body)
        if len(body) != 6:
            return None
        try:
            int(body, 16)
        except ValueError:
            return None
        return "#" + body.upper()

    def _sync_canvas_bg_swatch(self):
        preset = self._canvas_bg_preset.get()
        if preset == "Transparent":
            # Checker-ish muted look — no solid color.
            try:
                self._canvas_bg_swatch.configure(
                    fg_color=("#555555", "#555555"),
                    hover_color=("#666666", "#666666"),
                    text="∅",
                    text_color=("gray80", "gray80"),
                )
            except Exception:
                pass
            return
        hex_color = self._normalize_hex_color(self._canvas_bg_hex.get()) or "#808080"
        try:
            self._canvas_bg_swatch.configure(
                fg_color=hex_color,
                hover_color=hex_color,
                text="",
            )
        except Exception:
            pass

    def _pick_canvas_bg_color(self):
        """Open system color picker; always switches Background to Custom…."""
        if not self._canvas_enabled.get():
            return
        if self._canvas_bg_preset.get() != "Custom…":
            self._canvas_bg_preset.set("Custom…")
            self._on_canvas_bg_preset("Custom…")
        initial = self._normalize_hex_color(self._canvas_bg_hex.get()) or "#808080"
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
        self._canvas_bg_hex.set(chosen)
        self._sync_canvas_bg_swatch()

    def _on_canvas_bg_preset(self, value: str):
        if not self._canvas_enabled.get():
            return
        # Swatch + Pick stay clickable for any preset (opens picker → Custom).
        try:
            self._canvas_bg_swatch.configure(state="normal")
            self._canvas_bg_pick_btn.configure(state="normal")
        except Exception:
            pass

        if value == "Custom…":
            try:
                self._canvas_bg_entry.configure(state="normal")
            except Exception:
                pass
            if not self._normalize_hex_color(self._canvas_bg_hex.get()):
                self._canvas_bg_hex.set("#808080")
            self._sync_canvas_bg_swatch()
            return

        if value == "Transparent":
            try:
                self._canvas_bg_entry.configure(state="disabled")
            except Exception:
                pass
            self._sync_canvas_bg_swatch()
            return

        preset_hex = _CANVAS_BG_PRESETS.get(value, "#000000")
        if preset_hex not in ("transparent", "custom"):
            self._canvas_bg_hex.set(preset_hex)
        try:
            self._canvas_bg_entry.configure(state="disabled")
        except Exception:
            pass
        self._sync_canvas_bg_swatch()

    def _toggle_lock(self):
        self._lock_aspect = not self._lock_aspect
        self._lock_btn.configure(text="🔗" if self._lock_aspect else "🔓")
        if self._lock_aspect:
            self._on_width_edit()

    def _parse_positive(self, text: str) -> Optional[float]:
        try:
            v = float(str(text).strip().replace(",", "."))
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        return v

    def _on_unit_change(self, value: str):
        if self._syncing:
            return
        self._syncing = True
        try:
            self._unit = value
            if value == _UNIT_PERCENT:
                self._w_var.set("100")
                self._h_var.set("100")
            else:
                self._w_var.set(str(self._ref_w))
                self._h_var.set(str(self._ref_h))
        finally:
            self._syncing = False

    def _on_width_edit(self):
        if self._syncing or not self._lock_aspect:
            return
        wv = self._parse_positive(self._w_var.get())
        if wv is None:
            return
        self._syncing = True
        try:
            if self._unit_var.get() == _UNIT_PERCENT:
                self._h_var.set(self._w_var.get().strip())
            else:
                self._h_var.set(str(max(1, int(round(wv / self._aspect)))))
        finally:
            self._syncing = False

    def _on_height_edit(self):
        if self._syncing or not self._lock_aspect:
            return
        hv = self._parse_positive(self._h_var.get())
        if hv is None:
            return
        self._syncing = True
        try:
            if self._unit_var.get() == _UNIT_PERCENT:
                self._w_var.set(self._h_var.get().strip())
            else:
                self._w_var.set(str(max(1, int(round(hv * self._aspect)))))
        finally:
            self._syncing = False

    def _collect_crop_settings(self) -> Optional[dict[str, Any]]:
        if not self._crop_enabled.get():
            return None
        wv = self._parse_positive(self._crop_w_var.get())
        hv = self._parse_positive(self._crop_h_var.get())
        if wv is None or hv is None:
            return None
        custom = bool(self._crop_custom_var.get())
        settings: dict[str, Any] = {
            "width": int(round(wv)),
            "height": int(round(hv)),
            "anchor": self._crop_anchor.get(),
            "custom_offset": custom,
            "offset_x": None,
            "offset_y": None,
        }
        if custom:
            ox = self._parse_nonneg_int(self._crop_ox_var.get())
            oy = self._parse_nonneg_int(self._crop_oy_var.get())
            if ox is None or oy is None:
                return None
            settings["offset_x"] = ox
            settings["offset_y"] = oy
        return settings

    def _collect_canvas_settings(self) -> Optional[dict[str, Any]]:
        if not self._canvas_enabled.get():
            return None
        wv = self._parse_positive(self._canvas_w_var.get())
        hv = self._parse_positive(self._canvas_h_var.get())
        if wv is None or hv is None:
            return None
        if wv > 50000 or hv > 50000:
            return None
        preset = self._canvas_bg_preset.get()
        if preset == "Custom…":
            bg = (self._canvas_bg_hex.get() or "").strip() or "#000000"
            if not bg.startswith("#"):
                bg = "#" + bg
        else:
            bg = _CANVAS_BG_PRESETS.get(preset, "#000000")
        return {
            "width": int(round(wv)),
            "height": int(round(hv)),
            "bg_color": bg,
            "anchor": self._canvas_anchor.get(),
        }

    def _parse_nonneg_int(self, text: str) -> Optional[int]:
        try:
            v = int(str(text).strip())
        except (TypeError, ValueError):
            return None
        if v < 0:
            return None
        return v

    def _collect_resize_settings(self) -> Optional[dict[str, Any]]:
        if not self._resize_enabled.get():
            return None
        wv = self._parse_positive(self._w_var.get())
        hv = self._parse_positive(self._h_var.get())
        if wv is None or hv is None:
            return None
        unit = self._unit_var.get()
        if unit == _UNIT_PIXELS and (wv > 50000 or hv > 50000):
            return None
        return {
            "unit": unit,
            "width_val": float(wv),
            "height_val": float(hv),
            "lock_aspect": bool(self._lock_aspect),
            "resample_filter": RESAMPLE_OPTIONS.get(
                self._method_var.get(), PILImage.LANCZOS
            ),
        }

    def _on_cancel(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _on_ok(self):
        resize_settings = None
        if self._resize_enabled.get():
            resize_settings = self._collect_resize_settings()
            if resize_settings is None:
                messagebox.showinfo(
                    "Advanced Options",
                    "Resize is enabled — enter valid Width and Height values.",
                    parent=self,
                )
                return
        crop_settings = None
        if self._crop_enabled.get():
            crop_settings = self._collect_crop_settings()
            if crop_settings is None:
                messagebox.showinfo(
                    "Advanced Options",
                    "Crop is enabled — enter valid size"
                    + (" and offsets." if self._crop_custom_var.get() else "."),
                    parent=self,
                )
                return
        canvas_settings = None
        if self._canvas_enabled.get():
            canvas_settings = self._collect_canvas_settings()
            if canvas_settings is None:
                messagebox.showinfo(
                    "Advanced Options",
                    "Canvas is enabled — enter valid Width and Height.",
                    parent=self,
                )
                return
        rotate_on = bool(self._rotate_enabled.get())
        rotate_label = self._rotate_var.get()
        rotate_op = ROTATE_OPTIONS.get(rotate_label) if rotate_on else None
        flip_h = bool(self._flip_h_var.get()) if rotate_on else False
        flip_v = bool(self._flip_v_var.get()) if rotate_on else False
        result = {
            "resize_enabled": bool(self._resize_enabled.get()),
            "resize_settings": resize_settings,
            "rotate_enabled": rotate_on,
            "rotate_label": rotate_label,
            "rotate_op": rotate_op,
            "flip_h": flip_h,
            "flip_v": flip_v,
            "crop_enabled": bool(self._crop_enabled.get()),
            "crop_settings": crop_settings,
            "canvas_enabled": bool(self._canvas_enabled.get()),
            "canvas_settings": canvas_settings,
        }
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        self._on_apply(result)


class BatchProcessDialog(ctk.CTkToplevel):
    """Modal batch convert / rename dialog for selected images."""

    def __init__(
        self,
        parent,
        *,
        paths: list[str],
        on_start: Callable[[dict], None],
        title: str = "Batch Convert / Rename",
    ):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self._paths = list(paths)
        self._start_callback = on_start
        self._parent = parent

        # Per-format compression memory so switching formats restores values.
        self._jpg_quality = 90
        self._webp_quality = 90
        self._png_compress = 6

        self._use_advanced = tk.BooleanVar(value=False)
        self._adv_resize_enabled = False
        self._adv_resize_settings: Optional[dict[str, Any]] = None
        self._adv_rotate_enabled = False
        self._adv_rotate_label = "None"
        self._adv_rotate_op: Optional[str] = None
        self._adv_flip_h = False
        self._adv_flip_v = False
        self._adv_crop_enabled = False
        self._adv_crop_settings: Optional[dict[str, Any]] = None
        self._adv_canvas_enabled = False
        self._adv_canvas_settings: Optional[dict[str, Any]] = None
        self._adv_window: Optional[BatchAdvancedOptionsDialog] = None

        try:
            self.transient(parent.winfo_toplevel())
        except Exception:
            pass

        n = len(self._paths)
        pad = {"padx": 16, "pady": 6}

        ctk.CTkLabel(
            self,
            text=f"{n} image{'s' if n != 1 else ''} selected",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(16, 4))

        # --- Output format ---
        fmt_row = ctk.CTkFrame(self, fg_color="transparent")
        fmt_row.pack(fill="x", **pad)
        ctk.CTkLabel(fmt_row, text="Format:", width=100, anchor="w").pack(side="left")
        self._format_var = tk.StringVar(value="JPG")
        ctk.CTkOptionMenu(
            fmt_row,
            variable=self._format_var,
            values=list(OUTPUT_FORMATS.keys()),
            command=self._on_format_change,
            width=140,
            height=28,
            corner_radius=6,
        ).pack(side="left", padx=(8, 0))

        # Quality (JPG/WebP) or PNG compress_level — label/range switch with format.
        q_row = ctk.CTkFrame(self, fg_color="transparent")
        q_row.pack(fill="x", **pad)
        self._compress_label = ctk.CTkLabel(
            q_row, text="Quality:", width=100, anchor="w"
        )
        self._compress_label.pack(side="left")
        self._compress_var = tk.IntVar(value=90)
        self._compress_slider = ctk.CTkSlider(
            q_row,
            from_=1,
            to=100,
            number_of_steps=99,
            variable=self._compress_var,
            width=180,
            command=self._on_compress_slide,
        )
        self._compress_slider.pack(side="left", padx=(8, 8))
        self._compress_value = ctk.CTkLabel(q_row, text="90", width=36)
        self._compress_value.pack(side="left")
        self._compress_hint = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
        )
        self._compress_hint.pack(fill="x", padx=16, pady=(0, 2))

        # --- Output directory ---
        out_frame = ctk.CTkFrame(self, fg_color="transparent")
        out_frame.pack(fill="x", **pad)
        ctk.CTkLabel(out_frame, text="Output:", width=100, anchor="w").pack(side="left")

        self._out_mode = tk.StringVar(value="source")
        mode_col = ctk.CTkFrame(out_frame, fg_color="transparent")
        mode_col.pack(side="left", fill="x", expand=True, padx=(8, 0))

        ctk.CTkRadioButton(
            mode_col,
            text="Same folder as source",
            variable=self._out_mode,
            value="source",
            command=self._on_out_mode_change,
        ).pack(anchor="w")
        ctk.CTkRadioButton(
            mode_col,
            text="Choose folder:",
            variable=self._out_mode,
            value="custom",
            command=self._on_out_mode_change,
        ).pack(anchor="w", pady=(4, 0))

        dir_row = ctk.CTkFrame(self, fg_color="transparent")
        dir_row.pack(fill="x", padx=16, pady=(0, 6))
        first_dir = os.path.dirname(self._paths[0]) if self._paths else ""
        self._outdir_var = tk.StringVar(value=first_dir)
        self._outdir_entry = ctk.CTkEntry(
            dir_row, textvariable=self._outdir_var, width=320, height=28, corner_radius=6
        )
        self._outdir_entry.pack(side="left", fill="x", expand=True, padx=(108, 6))
        self._browse_btn = ctk.CTkButton(
            dir_row, text="…", width=36, height=28, command=self._browse_outdir
        )
        self._browse_btn.pack(side="left")

        # --- Rename ---
        ren_row = ctk.CTkFrame(self, fg_color="transparent")
        ren_row.pack(fill="x", **pad)
        self._rename_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            ren_row,
            text="Rename files",
            variable=self._rename_var,
            command=self._on_rename_toggle,
        ).pack(side="left")

        pat_row = ctk.CTkFrame(self, fg_color="transparent")
        pat_row.pack(fill="x", padx=16, pady=(0, 2))
        ctk.CTkLabel(pat_row, text="Pattern:", width=100, anchor="w").pack(side="left")
        self._pattern_var = tk.StringVar(value="image_###")
        self._pattern_entry = ctk.CTkEntry(
            pat_row, textvariable=self._pattern_var, width=220, height=28, corner_radius=6
        )
        self._pattern_entry.pack(side="left", padx=(8, 0))
        self._pattern_entry.bind("<KeyRelease>", lambda _e: self._update_rename_preview())

        self._preview_label = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
            wraplength=420,
            justify="left",
        )
        self._preview_label.pack(fill="x", padx=16, pady=(0, 4))

        ctk.CTkLabel(
            self,
            text="Tokens: {name} = original stem, ### = padded counter (001, 002…)",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 4))

        # --- Options (FastStone-style) ---
        self._remove_bg_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self,
            text="Remove background (BiRefNet → PNG with alpha)",
            variable=self._remove_bg_var,
            command=self._on_remove_bg_toggle,
        ).pack(anchor="w", padx=16, pady=(4, 2))

        self._ask_overwrite_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            self,
            text="Ask before overwrite",
            variable=self._ask_overwrite_var,
        ).pack(anchor="w", padx=16, pady=(6, 2))

        adv_row = ctk.CTkFrame(self, fg_color="transparent")
        adv_row.pack(fill="x", padx=16, pady=(4, 2))
        ctk.CTkCheckBox(
            adv_row,
            text="Use Advanced Options…",
            variable=self._use_advanced,
            command=self._on_use_advanced_toggle,
        ).pack(side="left")
        self._adv_btn = ctk.CTkButton(
            adv_row,
            text="Advanced Options",
            width=140,
            height=28,
            command=self._open_advanced,
            state="disabled",
        )
        self._adv_btn.pack(side="left", padx=(12, 0))

        self._adv_summary = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
            wraplength=420,
            justify="left",
        )
        self._adv_summary.pack(fill="x", padx=16, pady=(0, 4))

        # --- Actions ---
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
            text="Start Conversion",
            width=140,
            height=30,
            corner_radius=6,
            command=self._on_start,
        ).pack(side="right")

        self.bind("<Escape>", lambda _e: self._on_cancel())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._on_out_mode_change()
        self._on_rename_toggle()
        self._on_format_change(self._format_var.get())
        self._update_rename_preview()
        self._update_adv_summary()

        self.update_idletasks()
        _center_on_parent(self, parent)
        self.lift()
        self.focus_force()
        self.after(10, lambda: self.grab_set())

    def _set_compression_controls_enabled(self, enabled: bool):
        """Visually enable/disable quality+compression controls for the format."""
        state = "normal" if enabled else "disabled"
        muted = ("gray55", "gray50")
        active = ("gray10", "gray90")
        try:
            self._compress_slider.configure(state=state)
        except Exception:
            pass
        try:
            self._compress_label.configure(text_color=active if enabled else muted)
            self._compress_value.configure(
                text="—" if not enabled else str(int(self._compress_var.get())),
                text_color=active if enabled else muted,
            )
            self._compress_hint.configure(text_color=("gray40", "gray65") if enabled else muted)
        except Exception:
            pass

    def _on_compress_slide(self, _value=None):
        fmt = self._format_var.get()
        if fmt == "BMP":
            return
        val = int(self._compress_var.get())
        self._compress_value.configure(text=str(val))
        if fmt == "JPG":
            self._jpg_quality = val
        elif fmt == "WebP":
            self._webp_quality = val
        elif fmt == "PNG":
            self._png_compress = val

    def _on_format_change(self, value: str):
        if value == "JPG":
            self._compress_label.configure(text="Quality:")
            self._compress_slider.configure(from_=1, to=100, number_of_steps=99)
            self._compress_var.set(self._jpg_quality)
            self._compress_hint.configure(text="JPEG quality 1–100 (higher = larger file).")
            self._set_compression_controls_enabled(True)
        elif value == "WebP":
            self._compress_label.configure(text="Quality:")
            self._compress_slider.configure(from_=1, to=100, number_of_steps=99)
            self._compress_var.set(self._webp_quality)
            self._compress_hint.configure(text="WebP quality 1–100 (higher = larger file).")
            self._set_compression_controls_enabled(True)
        elif value == "PNG":
            self._compress_label.configure(text="Compression:")
            self._compress_slider.configure(from_=0, to=9, number_of_steps=9)
            self._compress_var.set(self._png_compress)
            self._compress_hint.configure(
                text="PNG deflate level 0–9 (lossless; higher = smaller/slower)."
            )
            self._set_compression_controls_enabled(True)
        else:  # BMP — no quality / compression parameter
            self._compress_label.configure(text="Compression:")
            self._compress_hint.configure(text="BMP has no compression setting.")
            self._set_compression_controls_enabled(False)
        self._update_rename_preview()

    def _on_out_mode_change(self):
        custom = self._out_mode.get() == "custom"
        state = "normal" if custom else "disabled"
        self._outdir_entry.configure(state=state)
        self._browse_btn.configure(state=state)

    def _browse_outdir(self):
        initial = self._outdir_var.get() or (
            os.path.dirname(self._paths[0]) if self._paths else ""
        )
        chosen = filedialog.askdirectory(
            parent=self, title="Output folder", initialdir=initial or None
        )
        if chosen:
            self._outdir_var.set(chosen)

    def _on_rename_toggle(self):
        state = "normal" if self._rename_var.get() else "disabled"
        self._pattern_entry.configure(state=state)
        self._update_rename_preview()

    def _update_rename_preview(self):
        if not self._rename_var.get() or not self._paths:
            self._preview_label.configure(text="")
            return
        ext = OUTPUT_FORMATS.get(self._format_var.get(), ".jpg")
        samples = []
        for i, path in enumerate(self._paths[:3], start=1):
            stem = os.path.splitext(os.path.basename(path))[0]
            new_stem = apply_rename_pattern(
                self._pattern_var.get(), stem=stem, index=i
            )
            samples.append(f"{new_stem}{ext}")
        extra = len(self._paths) - len(samples)
        text = "Preview: " + ", ".join(samples)
        if extra > 0:
            text += f" +{extra} more"
        self._preview_label.configure(text=text)

    def _on_remove_bg_toggle(self):
        if self._remove_bg_var.get():
            self._format_var.set("PNG")
            self._on_format_change("PNG")
        self._update_rename_preview()

    def _on_use_advanced_toggle(self):
        enabled = self._use_advanced.get()
        self._adv_btn.configure(state="normal" if enabled else "disabled")
        self._update_adv_summary()

    def _update_adv_summary(self):
        if not self._use_advanced.get():
            self._adv_summary.configure(text="")
            return
        bits = []
        if self._adv_resize_enabled and self._adv_resize_settings:
            s = self._adv_resize_settings
            unit_short = "px" if "Pixel" in str(s.get("unit")) else "%"
            bits.append(f"📐 Resize {s['width_val']:g}×{s['height_val']:g}{unit_short}")
        elif self._adv_resize_enabled:
            bits.append("📐 Resize (incomplete)")
        if self._adv_rotate_enabled and self._adv_rotate_op:
            bits.append(f"↻ Rotate {self._adv_rotate_label}")
        flips = []
        if self._adv_rotate_enabled and self._adv_flip_h:
            flips.append("H")
        if self._adv_rotate_enabled and self._adv_flip_v:
            flips.append("V")
        if flips:
            bits.append("↔ Flip " + "+".join(flips))
        elif self._adv_rotate_enabled and not self._adv_rotate_op:
            bits.append("↻ Rotate / Flip")
        if self._adv_crop_enabled and self._adv_crop_settings:
            c = self._adv_crop_settings
            bits.append(f"✂ Crop {c['width']}×{c['height']}")
        elif self._adv_crop_enabled:
            bits.append("✂ Crop")
        if self._adv_canvas_enabled and self._adv_canvas_settings:
            c = self._adv_canvas_settings
            bits.append(f"🖼 Canvas {c['width']}×{c['height']}")
        elif self._adv_canvas_enabled:
            bits.append("🖼 Canvas")
        if bits:
            self._adv_summary.configure(text="Advanced: " + ", ".join(bits))
        else:
            # Keep quiet until the user configures something in Advanced Options.
            self._adv_summary.configure(text="")

    def _open_advanced(self):
        if self._adv_window is not None:
            try:
                if self._adv_window.winfo_exists():
                    self._adv_window.lift()
                    self._adv_window.focus_force()
                    return
            except Exception:
                pass

        initial = {
            "resize_enabled": self._adv_resize_enabled,
            "resize_settings": self._adv_resize_settings,
            "rotate_enabled": self._adv_rotate_enabled,
            "rotate_label": self._adv_rotate_label,
            "flip_h": self._adv_flip_h,
            "flip_v": self._adv_flip_v,
            "crop_enabled": self._adv_crop_enabled,
            "crop_settings": self._adv_crop_settings,
            "canvas_enabled": self._adv_canvas_enabled,
            "canvas_settings": self._adv_canvas_settings,
        }

        def _apply(result: dict):
            self._adv_window = None
            self._adv_resize_enabled = bool(result.get("resize_enabled"))
            self._adv_resize_settings = result.get("resize_settings")
            self._adv_rotate_enabled = bool(result.get("rotate_enabled"))
            self._adv_rotate_label = result.get("rotate_label") or "None"
            self._adv_rotate_op = result.get("rotate_op")
            self._adv_flip_h = bool(result.get("flip_h"))
            self._adv_flip_v = bool(result.get("flip_v"))
            self._adv_crop_enabled = bool(result.get("crop_enabled"))
            self._adv_crop_settings = result.get("crop_settings")
            self._adv_canvas_enabled = bool(result.get("canvas_enabled"))
            self._adv_canvas_settings = result.get("canvas_settings")
            self._use_advanced.set(True)
            self._on_use_advanced_toggle()
            self.after(20, self._restore_grab)

        try:
            self.grab_release()
        except Exception:
            pass

        self._adv_window = BatchAdvancedOptionsDialog(
            self,
            paths=self._paths,
            initial=initial,
            on_apply=_apply,
        )

        def _on_gone(event):
            if event.widget is self._adv_window:
                self._adv_window = None
                self.after(20, self._restore_grab)

        try:
            self._adv_window.bind("<Destroy>", _on_gone)
        except Exception:
            self.after(200, self._restore_grab)

    def _restore_grab(self):
        try:
            if self.winfo_exists():
                self.grab_set()
                self.lift()
        except Exception:
            pass

    def _on_cancel(self):
        try:
            if self._adv_window is not None and self._adv_window.winfo_exists():
                self._adv_window.destroy()
        except Exception:
            pass
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _on_start(self):
        fmt = self._format_var.get()
        out_ext = OUTPUT_FORMATS.get(fmt)
        if not out_ext:
            messagebox.showerror("Batch Convert", "Unknown output format.", parent=self)
            return

        output_dir = None
        if self._out_mode.get() == "custom":
            output_dir = (self._outdir_var.get() or "").strip()
            if not output_dir or not os.path.isdir(output_dir):
                messagebox.showerror(
                    "Batch Convert",
                    "Please choose a valid output folder.",
                    parent=self,
                )
                return

        rename_enabled = bool(self._rename_var.get())
        pattern = self._pattern_var.get()
        if rename_enabled and not (pattern or "").strip():
            messagebox.showerror(
                "Batch Convert", "Rename pattern cannot be empty.", parent=self
            )
            return

        use_adv = bool(self._use_advanced.get())
        rotate_op = (
            self._adv_rotate_op
            if use_adv and self._adv_rotate_enabled
            else None
        )
        flip_h = bool(self._adv_flip_h) if use_adv and self._adv_rotate_enabled else False
        flip_v = bool(self._adv_flip_v) if use_adv and self._adv_rotate_enabled else False
        crop_settings = None
        if use_adv and self._adv_crop_enabled:
            if not self._adv_crop_settings:
                messagebox.showinfo(
                    "Batch Convert",
                    "Crop is enabled in Advanced Options — set size first.",
                    parent=self,
                )
                return
            crop_settings = dict(self._adv_crop_settings)
        resize_settings = None
        if use_adv and self._adv_resize_enabled:
            if not self._adv_resize_settings:
                messagebox.showinfo(
                    "Batch Convert",
                    "Resize is enabled in Advanced Options — set Width/Height first.",
                    parent=self,
                )
                return
            resize_settings = dict(self._adv_resize_settings)
        canvas_settings = None
        if use_adv and self._adv_canvas_enabled:
            if not self._adv_canvas_settings:
                messagebox.showinfo(
                    "Batch Convert",
                    "Canvas is enabled in Advanced Options — set size first.",
                    parent=self,
                )
                return
            canvas_settings = dict(self._adv_canvas_settings)

        if self._remove_bg_var.get():
            fmt = "PNG"
            out_ext = ".png"

        # Persist current slider into the format-specific slot.
        self._on_compress_slide()

        quality = 90
        png_compress = 6
        if fmt == "JPG":
            quality = self._jpg_quality
        elif fmt == "WebP":
            quality = self._webp_quality
        elif fmt == "PNG":
            png_compress = self._png_compress

        job = {
            "paths": list(self._paths),
            "out_ext": out_ext,
            "output_dir": output_dir,
            "rename_enabled": rename_enabled,
            "rename_pattern": pattern,
            "rotate_op": rotate_op,
            "flip_h": flip_h,
            "flip_v": flip_v,
            "crop_settings": crop_settings,
            "resize_settings": resize_settings,
            "canvas_settings": canvas_settings,
            "quality": int(quality),
            "png_compress": int(png_compress),
            "ask_before_overwrite": bool(self._ask_overwrite_var.get()),
            "remove_background": bool(self._remove_bg_var.get()),
        }

        try:
            self.grab_release()
        except Exception:
            pass
        callback = self._start_callback
        self.destroy()
        try:
            if callback:
                callback(job)
        except Exception as e:
            logging.exception("Batch convert start failed: %s", e)
            messagebox.showerror("Batch Convert", str(e))


def open_batch_process_dialog(parent, paths: list[str], on_start: Callable[[dict], None]):
    """Create and show the batch convert dialog."""
    return BatchProcessDialog(parent, paths=paths, on_start=on_start)
