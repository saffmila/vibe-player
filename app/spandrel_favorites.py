"""
spandrel_favorites.py — Named Fast Upscale presets (model + scale + tiling + format).

GPU, output folder and suffix stay session settings — they are not part of a preset.
"""

from __future__ import annotations

import os
from typing import Any

_FORMAT_MENU = {
    "png": "PNG",
    "jpg": "JPEG",
    "tiff": "TIFF",
    "webp": "WebP",
}


def preset_scale_label(preset: dict[str, Any]) -> str:
    """Short scale text for menus: ``Model``, ``2×``, ``4×``, or ``3×``."""
    mode = str(preset.get("scale_mode") or "Model").strip()
    if mode in ("2×", "4×"):
        return mode
    if mode == "Custom":
        try:
            value = float(preset.get("custom_scale") or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return f"{value:g}×"
        return "Custom"
    return "Model"


def preset_model_stem(preset: dict[str, Any]) -> str:
    model = str(preset.get("model") or "").replace("\\", "/").strip()
    base = os.path.basename(model)
    stem, _ext = os.path.splitext(base)
    return (stem or base).strip()


def preset_menu_label(preset: dict[str, Any]) -> str:
    """One-line preset label: name, model if renamed, scale, format.

    Example: ``PurePhoto  ·  4×  ·  PNG``
    """
    name = str(preset.get("name") or "").strip() or "Preset"
    parts = [name]
    stem = preset_model_stem(preset)
    if stem and stem.casefold() != name.casefold():
        parts.append(stem)
    parts.append(preset_scale_label(preset))
    fmt = str(preset.get("output_format") or "png").strip().lower()
    if fmt == "jpeg":
        fmt = "jpg"
    parts.append(_FORMAT_MENU.get(fmt, "PNG"))
    return "  ·  ".join(parts)


def model_stem_suffix(model: str | None) -> str:
    """Default output suffix: model filename without extension, with leading ``_``."""
    base = os.path.basename(str(model or "").replace("\\", "/").strip())
    stem, _ext = os.path.splitext(base)
    stem = (stem or "spandrel").strip() or "spandrel"
    return stem if stem.startswith("_") else f"_{stem}"


def normalize_presets(raw) -> list[dict[str, Any]]:
    """Sanitize prefs list → [{name, model, scale_mode, ...}, ...]."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        model = str(item.get("model") or "").strip()
        if not name or not model or name in seen:
            continue
        seen.add(name)
        mode = str(item.get("scale_mode") or "Model").strip()
        if mode not in ("Model", "2×", "4×", "Custom"):
            mode = "Model"
        try:
            custom = float(item.get("custom_scale") or 3)
        except Exception:
            custom = 3.0
        try:
            tile = max(256, min(2048, int(item.get("tile_size") or 512)))
        except Exception:
            tile = 512
        fmt = str(item.get("output_format") or "png").strip().lower()
        if fmt in ("jpeg",):
            fmt = "jpg"
        if fmt not in ("png", "jpg", "tiff", "webp"):
            fmt = "png"
        out.append(
            {
                "name": name,
                "model": model,
                "models_dir": str(item.get("models_dir") or "").strip(),
                "scale_mode": mode,
                "custom_scale": custom,
                "tiling": bool(item.get("tiling", False)),
                "tile_size": tile,
                "output_format": fmt,
            }
        )
    return out


def upsert_preset(presets: list[dict[str, Any]], preset: dict[str, Any]) -> list[dict[str, Any]]:
    """Insert or replace by name; returns new list."""
    cleaned = normalize_presets(presets)
    name = str(preset.get("name") or "").strip()
    if not name:
        return cleaned
    body = normalize_presets([preset])
    if not body:
        return cleaned
    entry = body[0]
    out = [p for p in cleaned if p.get("name") != name]
    out.append(entry)
    out.sort(key=lambda p: str(p.get("name") or "").lower())
    return out


def remove_preset(presets: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    name = str(name or "").strip()
    return [p for p in normalize_presets(presets) if p.get("name") != name]


def find_preset(presets: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    name = str(name or "").strip()
    for p in normalize_presets(presets):
        if p.get("name") == name:
            return p
    return None


def target_scale_from_preset(preset: dict[str, Any]) -> float | None:
    mode = str(preset.get("scale_mode") or "Model")
    if mode == "Model":
        return None
    if mode == "2×":
        return 2.0
    if mode == "4×":
        return 4.0
    try:
        value = float(preset.get("custom_scale") or 0)
    except Exception:
        return None
    return value if value > 0 else None


def preset_to_options(
    preset: dict[str, Any],
    *,
    output_dir: str,
    cuda_device: str = "0",
    tile_cap: int = 0,
) -> dict[str, Any]:
    """Build ``start_fast_upscale_batch`` options from a favorite preset.

    Tiling is always chosen from free VRAM. ``tile_cap`` is an optional ceiling
    (session setting, not part of the preset).
    """
    model = str(preset.get("model") or "").strip()
    models_dir = str(preset.get("models_dir") or "").strip() or None
    try:
        cap = int(tile_cap or 0)
    except (TypeError, ValueError):
        cap = 0
    return {
        "backend_id": "spandrel",
        "output_dir": output_dir,
        "model": model,
        "models_dir": models_dir,
        "auto_tile": True,
        "tile_cap": cap if cap > 0 else 0,
        "tile_size": 0,
        "overlap": 32,
        "cuda_device": str(cuda_device or "0"),
        "target_scale": target_scale_from_preset(preset),
        "suffix": model_stem_suffix(model),
        "output_format": str(preset.get("output_format") or "png"),
        "unload_after": True,
    }
