"""
Shared encode settings UI (Preset / Video / Audio cards) for Convert + Timeline Export.

Export/convert *logic* stays in callers; this module only builds settings widgets
and collects a settings dict for custom re-encode.
"""

from __future__ import annotations

import customtkinter as ctk

PRESET_CUSTOM = "Manual…"
VIDEO_QUALITY_LEVELS = ("Low", "Medium", "High")
AUDIO_BITRATE_LEVELS = ("96k", "128k", "192k", "256k")
DEFAULT_VIDEO_QUALITY = "High"
DEFAULT_AUDIO_BITRATE = "192k"
SUPPORTED_FORMATS = [".mp4", ".avi", ".mkv", ".mov", ".webm"]
CUSTOM_SCROLL_HEIGHT = 500
EXPORT_CUSTOM_SCROLL_HEIGHT = 400

# Rotate labels → ops (same keys as image batch / convert transforms).
VIDEO_ROTATE_OPTIONS: dict[str, str | None] = {
    "None": None,
    "90° CW": "rotate_right",
    "90° CCW": "rotate_left",
    "180°": "rotate_180",
}

# SeedVR-like section cards
_UI_SECTION_BG = ("gray88", "#2a2a2a")
_UI_SECTION_BORDER = ("gray70", "#3d3d3d")
_UI_SECTION_TITLE = "#8ab4c8"
_UI_INFO_BG = ("gray85", "#0c0c0c")
_UI_INFO_TEXT = ("#555555", "#9aa3ad")

_ENTRY_TEXT_LOCKED = ("#7a7a7a", "#7a7a7a")
_ENTRY_FG_LOCKED = ("#d0d0d0", "#252525")
_ENTRY_TEXT_EDIT = ("gray14", "#DCE4EE")
_ENTRY_FG_EDIT = ("#F9F9FA", "#343638")

ENCODE_PRESETS = {
    "MP4 · original size": {"ext": ".mp4", "keep_size": True},
    "WebM · original size": {"ext": ".webm", "keep_size": True},
    "MP4 1920x1080": {"ext": ".mp4", "width": 1920, "height": 1080, "fps": 30},
    "MP4 1600x1200 HQ": {"ext": ".mp4", "width": 1600, "height": 1200, "fps": 30},
    "MP4 1280x720": {"ext": ".mp4", "width": 1280, "height": 720, "fps": 30},
    "MP4 854x480": {"ext": ".mp4", "width": 854, "height": 480, "fps": 30},
    "AVI 640x480": {"ext": ".avi", "width": 640, "height": 480, "fps": 25},
}

PRESET_INFO = {
    "MP4 · original size": (
        "MP4 · H.264 · keeps source size & FPS\n"
        "High quality re-encode · General use / archive"
    ),
    "WebM · original size": (
        "WebM · VP9 · keeps source size & FPS\n"
        "Smaller files · Web delivery"
    ),
    "MP4 1920x1080": (
        "MP4 · 1920×1080 @ 30 fps · H.264\n"
        "Full HD · TV / desktop / YouTube"
    ),
    "MP4 1600x1200 HQ": (
        "MP4 · 1600×1200 @ 30 fps · H.264\n"
        "4:3 HQ · Legacy displays / kiosk"
    ),
    "MP4 1280x720": (
        "MP4 · 1280×720 @ 30 fps · H.264\n"
        "HD 720p · Laptop / tablet / web"
    ),
    "MP4 854x480": (
        "MP4 · 854×480 @ 30 fps · H.264\n"
        "SD · Small screens / low bandwidth"
    ),
    "AVI 640x480": (
        "AVI · 640×480 @ 25 fps · MPEG-4\n"
        "Legacy / compatibility"
    ),
    PRESET_CUSTOM: (
        "Manual settings — edit video & audio below\n"
        "Choose format, size, quality and audio bitrate"
    ),
}

AUDIO_INFO = {
    "96k": "96 kbps — compact, speech / background",
    "128k": "128 kbps — light stereo / web",
    "192k": "192 kbps — solid stereo for most uses",
    "256k": "256 kbps — higher fidelity music / archive",
}


def make_section(parent, title: str):
    """SeedVR-style rounded card with title; returns (card, body)."""
    card = ctk.CTkFrame(
        parent,
        fg_color=_UI_SECTION_BG,
        corner_radius=8,
        border_width=1,
        border_color=_UI_SECTION_BORDER,
    )
    ctk.CTkLabel(
        card,
        text=title,
        text_color=_UI_SECTION_TITLE,
        font=ctk.CTkFont(size=11, weight="bold"),
        anchor="w",
    ).pack(fill="x", padx=10, pady=(8, 0))
    body = ctk.CTkFrame(card, fg_color="transparent")
    body.pack(fill="x", padx=10, pady=(4, 10))
    return card, body


def make_info_box(parent, wraplength: int = 300, icon: str = ""):
    """Dark info panel with optional leading emoji icon and text indented right."""
    box = ctk.CTkFrame(parent, fg_color=_UI_INFO_BG, corner_radius=8, border_width=0)
    row = ctk.CTkFrame(box, fg_color="transparent")
    row.pack(fill="x", padx=10, pady=8)
    if icon:
        ctk.CTkLabel(
            row,
            text=icon,
            font=("", 16),
            width=28,
            anchor="center",
            text_color=_UI_INFO_TEXT,
        ).pack(side="left", padx=(2, 10))
    label = ctk.CTkLabel(
        row,
        text="",
        text_color=_UI_INFO_TEXT,
        font=("", 10),
        justify="left",
        anchor="w",
        wraplength=wraplength,
    )
    label.pack(side="left", fill="x", expand=True)
    box._info_label = label  # type: ignore[attr-defined]
    return box


def set_info_text(box, text: str):
    label = getattr(box, "_info_label", None)
    if label is not None:
        label.configure(text=text)


RIFE_MULT_LABELS = ("2×", "4×")
RIFE_MODE_LABELS = ("Higher FPS (keep duration)", "Slow motion (keep FPS)")


class VideoEncodeSettingsPanel(ctk.CTkFrame):
    """
    Scrollable Preset / Video / Video operations / Audio cards shared by Convert + Export.
    """

    def __init__(
        self,
        parent,
        *,
        source_width: int | None = None,
        source_height: int | None = None,
        source_fps: float | None = None,
        scroll_height: int = CUSTOM_SCROLL_HEIGHT,
        default_preset: str = "MP4 · original size",
        enable_rife: bool = False,
    ):
        super().__init__(parent, fg_color="transparent")
        self._source_width = source_width
        self._source_height = source_height
        self._source_fps = source_fps
        self._enable_rife = bool(enable_rife)

        self.presets = dict(ENCODE_PRESETS)
        self._preset_values = list(self.presets.keys()) + [PRESET_CUSTOM]
        if default_preset not in self._preset_values:
            default_preset = self._preset_values[0]

        self.preset_var = ctk.StringVar(value=default_preset)
        self.ext_var = ctk.StringVar(value=".mp4")
        self.width_var = ctk.StringVar(value="")
        self.height_var = ctk.StringVar(value="")
        self.fps_var = ctk.StringVar(value="")
        self.sound_var = ctk.BooleanVar(value=True)
        self.video_quality_var = ctk.StringVar(value=DEFAULT_VIDEO_QUALITY)
        self.audio_bitrate_var = ctk.StringVar(value=DEFAULT_AUDIO_BITRATE)
        self.rotate_var = ctk.StringVar(value="None")
        self.mirror_var = ctk.BooleanVar(value=False)
        self.flip_v_var = ctk.BooleanVar(value=False)
        self.rife_enabled_var = ctk.BooleanVar(value=False)
        self.rife_mult_var = ctk.StringVar(value="2×")
        self.rife_mode_var = ctk.StringVar(value=RIFE_MODE_LABELS[0])
        self._dim_entries: list[ctk.CTkEntry] = []

        self._scroll = ctk.CTkScrollableFrame(
            self, height=scroll_height, fg_color="transparent"
        )
        self._scroll.pack(fill="both", expand=True, padx=(0, 2))

        preset_card, preset_body = make_section(self._scroll, "Preset")
        preset_card.pack(fill="x", pady=(0, 8))
        self._preset_menu = ctk.CTkOptionMenu(
            preset_body,
            variable=self.preset_var,
            values=self._preset_values,
            command=self.apply_preset,
            height=28,
        )
        self._preset_menu.pack(fill="x", pady=(0, 6))
        self._preset_info = make_info_box(preset_body, icon="🎬")
        self._preset_info.pack(fill="x", pady=(0, 2))

        video_card, video_body = make_section(self._scroll, "Video")
        video_card.pack(fill="x", pady=(0, 8))
        self._size_form = ctk.CTkFrame(video_body, fg_color="transparent")
        self._add_entry(self._size_form, "Width:", self.width_var)
        self._add_entry(self._size_form, "Height:", self.height_var)
        self._add_entry(self._size_form, "FPS:", self.fps_var)
        self._size_form.pack(fill="x", pady=(0, 4))

        self._format_row = ctk.CTkFrame(video_body, fg_color="transparent")
        ctk.CTkLabel(self._format_row, text="Format:", width=100, anchor="w").pack(
            side="left"
        )
        self._format_menu = ctk.CTkOptionMenu(
            self._format_row,
            variable=self.ext_var,
            values=SUPPORTED_FORMATS,
            height=28,
        )
        self._format_menu.pack(side="left", fill="x", expand=True)
        self._format_row.pack(fill="x", pady=(2, 4))

        q_row = ctk.CTkFrame(video_body, fg_color="transparent")
        ctk.CTkLabel(q_row, text="Video quality:", width=100, anchor="w").pack(
            side="left"
        )
        self._quality_menu = ctk.CTkOptionMenu(
            q_row,
            variable=self.video_quality_var,
            values=list(VIDEO_QUALITY_LEVELS),
            height=28,
            command=lambda _v: self.refresh_preset_info(),
        )
        self._quality_menu.pack(side="left", fill="x", expand=True)
        q_row.pack(fill="x", pady=(2, 2))

        ops_card, ops_body = make_section(self._scroll, "Video operations")
        ops_card.pack(fill="x", pady=(0, 8))
        rot_row = ctk.CTkFrame(ops_body, fg_color="transparent")
        rot_row.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(rot_row, text="Rotate/Transform:", width=120, anchor="w").pack(
            side="left"
        )
        self._rotate_menu = ctk.CTkOptionMenu(
            rot_row,
            variable=self.rotate_var,
            values=list(VIDEO_ROTATE_OPTIONS.keys()),
            height=28,
        )
        self._rotate_menu.pack(side="left", fill="x", expand=True)
        flip_row = ctk.CTkFrame(ops_body, fg_color="transparent")
        flip_row.pack(fill="x", pady=(0, 2))
        self._mirror_check = ctk.CTkCheckBox(
            flip_row, text="Mirror horizontal", variable=self.mirror_var
        )
        self._mirror_check.pack(side="left", padx=(0, 16))
        self._flip_v_check = ctk.CTkCheckBox(
            flip_row, text="Flip vertical", variable=self.flip_v_var
        )
        self._flip_v_check.pack(side="left")

        if self._enable_rife:
            rife_card, rife_body = make_section(self._scroll, "RIFE interpolate (optional)")
            rife_card.pack(fill="x", pady=(0, 8))
            self._rife_check = ctk.CTkCheckBox(
                rife_body,
                text="Interpolate frames (rife-ncnn-vulkan)",
                variable=self.rife_enabled_var,
                command=self._refresh_rife_controls,
            )
            self._rife_check.pack(anchor="w", pady=(0, 6))
            mult_row = ctk.CTkFrame(rife_body, fg_color="transparent")
            mult_row.pack(fill="x", pady=(0, 4))
            ctk.CTkLabel(mult_row, text="Multiplier:", width=100, anchor="w").pack(
                side="left"
            )
            self._rife_mult_menu = ctk.CTkOptionMenu(
                mult_row,
                variable=self.rife_mult_var,
                values=list(RIFE_MULT_LABELS),
                height=28,
                command=lambda _v: self._refresh_rife_info(),
            )
            self._rife_mult_menu.pack(side="left", fill="x", expand=True)
            mode_row = ctk.CTkFrame(rife_body, fg_color="transparent")
            mode_row.pack(fill="x", pady=(0, 4))
            ctk.CTkLabel(mode_row, text="Mode:", width=100, anchor="w").pack(side="left")
            self._rife_mode_menu = ctk.CTkOptionMenu(
                mode_row,
                variable=self.rife_mode_var,
                values=list(RIFE_MODE_LABELS),
                height=28,
                command=lambda _v: self._refresh_rife_info(),
            )
            self._rife_mode_menu.pack(side="left", fill="x", expand=True)
            self._rife_info = make_info_box(rife_body, icon="✨")
            self._rife_info.pack(fill="x", pady=(2, 2))
            self._refresh_rife_controls()

        audio_card, audio_body = make_section(self._scroll, "Audio")
        audio_card.pack(fill="x", pady=(0, 4))
        a_row = ctk.CTkFrame(audio_body, fg_color="transparent")
        ctk.CTkLabel(a_row, text="Audio bitrate:", width=100, anchor="w").pack(
            side="left"
        )
        self._audio_bitrate_menu = ctk.CTkOptionMenu(
            a_row,
            variable=self.audio_bitrate_var,
            values=list(AUDIO_BITRATE_LEVELS),
            height=28,
            command=lambda _v: self.refresh_audio_info(),
        )
        self._audio_bitrate_menu.pack(side="left", fill="x", expand=True)
        a_row.pack(fill="x", pady=(0, 4))
        self._audio_check = ctk.CTkCheckBox(
            audio_body, text="Include audio", variable=self.sound_var
        )
        self._audio_check.pack(anchor="w", pady=(2, 6))
        self._audio_info = make_info_box(audio_body, icon="🎧")
        self._audio_info.pack(fill="x", pady=(0, 2))

        self.apply_preset(self.preset_var.get())
        self.refresh_audio_info()

    def set_source_props(
        self,
        width: int | None,
        height: int | None,
        fps: float | None,
    ):
        self._source_width = width
        self._source_height = height
        self._source_fps = fps
        # Refresh displayed dims if current preset keeps source size.
        name = self.preset_var.get()
        preset = self.presets.get(name)
        if preset and preset.get("keep_size"):
            self.apply_preset(name)

    def _add_entry(self, frame, label, var):
        row = ctk.CTkFrame(frame, fg_color="transparent")
        row.pack(fill="x", pady=1)
        ctk.CTkLabel(row, text=label, width=100, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(row, textvariable=var, height=28)
        entry.pack(side="left", fill="x", expand=True)
        self._dim_entries.append(entry)
        return entry

    def _set_dim_fields_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        text_color = _ENTRY_TEXT_EDIT if enabled else _ENTRY_TEXT_LOCKED
        fg_color = _ENTRY_FG_EDIT if enabled else _ENTRY_FG_LOCKED
        for entry in self._dim_entries:
            try:
                entry.configure(state=state, text_color=text_color, fg_color=fg_color)
            except Exception:
                entry.configure(state=state)

    def _set_format_enabled(self, enabled: bool):
        self._format_menu.configure(state="normal" if enabled else "disabled")

    def _set_quality_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self._quality_menu.configure(state=state)
        self._audio_bitrate_menu.configure(state=state)

    def _fill_source_dims(self):
        w, h, fps = self._source_width, self._source_height, self._source_fps
        self.width_var.set(str(w) if w else "")
        self.height_var.set(str(h) if h else "")
        if fps:
            self.fps_var.set(f"{fps:g}")
        else:
            self.fps_var.set("")

    def refresh_preset_info(self):
        name = self.preset_var.get()
        text = PRESET_INFO.get(name) or PRESET_INFO[PRESET_CUSTOM]
        if name == PRESET_CUSTOM:
            q = self.video_quality_var.get() or DEFAULT_VIDEO_QUALITY
            ext = self.ext_var.get() or ".mp4"
            text = (
                f"Manual · {ext} · video quality {q}\n"
                "Edit size, format, quality and audio below"
            )
        set_info_text(self._preset_info, text)

    def refresh_audio_info(self):
        br = self.audio_bitrate_var.get() or DEFAULT_AUDIO_BITRATE
        set_info_text(
            self._audio_info, AUDIO_INFO.get(br, AUDIO_INFO[DEFAULT_AUDIO_BITRATE])
        )

    def _rife_multiplier_value(self) -> int:
        raw = (self.rife_mult_var.get() or "2×").strip()
        return 4 if raw.startswith("4") else 2

    def _rife_mode_value(self) -> str:
        label = self.rife_mode_var.get() or RIFE_MODE_LABELS[0]
        return "slowmo" if "slow" in label.lower() else "fps"

    def _refresh_rife_info(self):
        if not self._enable_rife or not hasattr(self, "_rife_info"):
            return
        mult = self._rife_multiplier_value()
        mode = self._rife_mode_value()
        src = self._source_fps
        if mode == "slowmo":
            text = (
                f"RIFE {mult}× slow motion — keeps ~{src:g} fps, "
                f"duration ×{mult}"
                if src
                else f"RIFE {mult}× slow motion — keeps source FPS, longer clip"
            )
        else:
            if src:
                text = (
                    f"RIFE {mult}× — raises FPS {src:g} → {src * mult:g}, "
                    "same duration"
                )
            else:
                text = f"RIFE {mult}× — higher FPS, same duration"
        text += "\nOptional pack: tools/rife/ (not in base install)"
        set_info_text(self._rife_info, text)

    def _refresh_rife_controls(self):
        if not self._enable_rife:
            return
        on = bool(self.rife_enabled_var.get())
        state = "normal" if on else "disabled"
        for w in (getattr(self, "_rife_mult_menu", None), getattr(self, "_rife_mode_menu", None)):
            if w is not None:
                try:
                    w.configure(state=state)
                except Exception:
                    pass
        self._refresh_rife_info()

    def apply_preset(self, preset_name: str | None = None):
        """Named presets lock size/format/quality; only Manual… edits them."""
        name = preset_name or self.preset_var.get()
        is_custom = name == PRESET_CUSTOM or name not in self.presets
        if is_custom:
            self._set_dim_fields_enabled(True)
            self._set_format_enabled(True)
            self._set_quality_enabled(True)
            self.refresh_preset_info()
            self.refresh_audio_info()
            return

        preset = self.presets[name]
        self._set_dim_fields_enabled(True)
        self.ext_var.set(preset["ext"])
        if preset.get("keep_size"):
            self._fill_source_dims()
        else:
            self.width_var.set(str(preset["width"]))
            self.height_var.set(str(preset["height"]))
            self.fps_var.set(str(preset["fps"]))
        self.video_quality_var.set(DEFAULT_VIDEO_QUALITY)
        self.audio_bitrate_var.set(DEFAULT_AUDIO_BITRATE)
        self._set_dim_fields_enabled(False)
        self._set_format_enabled(False)
        self._set_quality_enabled(False)
        self.refresh_preset_info()
        self.refresh_audio_info()

    def get_custom_settings(self) -> dict:
        """
        Build custom re-encode settings from the panel.

        Raises ValueError on invalid input.
        """
        name = self.preset_var.get()
        preset = self.presets.get(name) if name != PRESET_CUSTOM else None
        keep_size = bool(preset and preset.get("keep_size"))
        from_custom_ui = name == PRESET_CUSTOM or preset is None
        quality = {
            "video_quality": (
                self.video_quality_var.get()
                if from_custom_ui
                else DEFAULT_VIDEO_QUALITY
            )
            or DEFAULT_VIDEO_QUALITY,
            "audio_bitrate": (
                self.audio_bitrate_var.get()
                if from_custom_ui
                else DEFAULT_AUDIO_BITRATE
            )
            or DEFAULT_AUDIO_BITRATE,
        }
        rotate_op = VIDEO_ROTATE_OPTIONS.get(self.rotate_var.get())
        transform = {
            "rotate_op": rotate_op,
            "flip_h": bool(self.mirror_var.get()),
            "flip_v": bool(self.flip_v_var.get()),
        }
        rife_opts = {}
        if self._enable_rife and bool(self.rife_enabled_var.get()):
            rife_opts = {
                "rife_enabled": True,
                "rife_multiplier": self._rife_multiplier_value(),
                "rife_mode": self._rife_mode_value(),
            }

        if keep_size:
            if not self._source_width or not self._source_height:
                raise ValueError(
                    "Could not read source resolution. Choose Manual… and enter size manually."
                )
            return {
                "mode": "custom",
                "ext": self.ext_var.get(),
                "keep_size": True,
                "include_audio": bool(self.sound_var.get()),
                **quality,
                **transform,
                **rife_opts,
            }

        settings = {
            "mode": "custom",
            "ext": self.ext_var.get(),
            "width": int(self.width_var.get()),
            "height": int(self.height_var.get()),
            "fps": float(self.fps_var.get()),
            "include_audio": bool(self.sound_var.get()),
            **quality,
            **transform,
            **rife_opts,
        }
        if settings["width"] <= 0 or settings["height"] <= 0 or settings["fps"] <= 0:
            raise ValueError("Width, height, and FPS must be positive.")
        return settings
