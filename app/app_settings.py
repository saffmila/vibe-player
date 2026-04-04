"""
Application and tagging configuration for Vibe Player.

Defines ``TaggingSettings`` (CLIP/YOLO engine, presets, thresholds, model paths)
and ``AppSettings`` (overlay, thumbnail size, extra model path, plugin config).
Both load from and save to JSON alongside the application.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

_APP_DIR = Path(__file__).resolve().parent
_SETTINGS_AUTOTAG = "settings_autotag.json"


@dataclass
class TaggingSettings:
    tagging_engine: str = "CLIP"
    tagging_preset: str = "F_SUPER_AGGRESSIVE"
    number_of_passes: int = 2

    enable_fallback: bool = True
    yolo_model_path: str = "models/yolov8/yolov8n.pt"
    confidence_threshold: float = 0.06

    # Extended at runtime by presets / JSON (kept as fields for from_dict / to_dict)
    min_votes: int = 1
    pass_confidence_thresholds: dict[int, float] = field(default_factory=dict)
    pass_priority: dict[int, int] = field(default_factory=dict)
    human_vote_multiplier: int = 5
    yolo_confidence_threshold: float = 0.25
    yolo_image_size: int = 640
    openclip_model_dir: str = ""
    openclip_model_path: str = ""
    class_hint_sets: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaggingSettings:
        valid_keys = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def get_settings_path(self) -> str:
        """Canonical path for new saves (alongside other app/*.json)."""
        return str(_APP_DIR / _SETTINGS_AUTOTAG)

    def resolve_settings_load_path(self) -> str:
        """Prefer app/settings_autotag.json; fall back to legacy repo-root copy."""
        app_path = _APP_DIR / _SETTINGS_AUTOTAG
        legacy_path = _APP_DIR.parent / _SETTINGS_AUTOTAG
        if app_path.is_file():
            return str(app_path)
        if legacy_path.is_file():
            return str(legacy_path)
        return str(app_path)

    def load_from_json(self, path: str | None = None) -> TaggingSettings:
        path = path or self.resolve_settings_load_path()
        logging.debug("Loading TaggingSettings from: %s", path)
        if not os.path.isfile(path):
            logging.debug("Tagging settings file not found, using defaults.")
            return self
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logging.debug("Loaded tagging JSON keys: %s", list(data.keys()))
        for k, v in data.items():
            setattr(self, k, v)
        logging.debug("TaggingSettings after load: %s", vars(self))
        return self

    def save_to_json(self, path: str | None = None) -> None:
        path = path or self.get_settings_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.__dict__, f, indent=2)


@dataclass
class AppSettings:
    tagging: TaggingSettings = field(default_factory=TaggingSettings)
    overlay_enabled: bool = True
    thumbnail_size: tuple[int, int] = (320, 240)
    extra_model_path: str = "models/yolov8/extra/"
    future_plugin_config: dict[str, Any] = field(default_factory=dict)

    def save(self, path: str = "settings.json") -> None:
        data = {
            "tagging": self.tagging.to_dict(),
            "overlay_enabled": self.overlay_enabled,
            "thumbnail_size": self.thumbnail_size,
            "extra_model_path": self.extra_model_path,
            "future_plugin_config": self.future_plugin_config,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)

    @classmethod
    def load(cls, path: str | None = None) -> AppSettings:
        path = path or TaggingSettings().resolve_settings_load_path()
        logging.info("AppSettings.load() reading: %s", path)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        instance = cls()
        instance.tagging = TaggingSettings.from_dict(data.get("tagging", {}))
        instance.overlay_enabled = data.get("overlay_enabled", True)
        instance.thumbnail_size = tuple(data.get("thumbnail_size", (320, 240)))
        instance.extra_model_path = data.get("extra_model_path", "models/yolov8/extra/")
        instance.future_plugin_config = data.get("future_plugin_config", {})
        return instance
