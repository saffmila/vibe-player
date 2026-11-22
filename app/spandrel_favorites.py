"""
spandrel_favorites.py — Named Upscale presets (model + scale + format).

GPU, output folder and suffix stay session settings — they are not part of a preset.

Built-in recipes ship with the app and are merged into the user's preset list on
load. Download of missing ``.pth`` files is handled later; presets just name the
local files.
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

# Curated recipes — order is menu order. ``model`` is a filename matched in the
# current models folder (basename). Download URLs come later.
BUILTIN_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "builtin_id": "photo_quality",
        "name": "Photo Quality",
        "blurb": "slow · people & detail",
        "model": "4xNomos8kHAT-L_otf.pth",
        "scale_mode": "Model",
        "custom_scale": 4.0,
        "output_format": "png",
    },
    {
        "builtin_id": "web_photo_fast",
        "name": "Web Photo Fast",
        "blurb": "restore compressed JPEGs",
        "model": "4xNomosWebPhoto_RealPLKSR.pth",
        "scale_mode": "Model",
        "custom_scale": 4.0,
        "output_format": "png",
    },
    {
        "builtin_id": "clean_fast",
        "name": "Clean Fast",
        "blurb": "near-instant · light sharpen",
        "model": "4x-ClearRealityV1.pth",
        "scale_mode": "Model",
        "custom_scale": 4.0,
        "output_format": "png",
    },
    {
        "builtin_id": "illustration_anime",
        "name": "Illustration & Anime",
        "blurb": "lineart & 2D",
        "model": "4x-UltraSharp.pth",
        "scale_mode": "Model",
        "custom_scale": 4.0,
        "output_format": "png",
    },
)


def _builtin_ids() -> set[str]:
    return {
        str(item.get("builtin_id") or "").strip()
        for item in BUILTIN_PRESETS
        if str(item.get("builtin_id") or "").strip()
    }


def _builtin_names() -> set[str]:
    return {
        str(item.get("name") or "").strip()
        for item in BUILTIN_PRESETS
        if str(item.get("name") or "").strip()
    }


def is_builtin_preset(preset: dict[str, Any] | None) -> bool:
    if not isinstance(preset, dict):
        return False
    bid = str(preset.get("builtin_id") or "").strip()
    if bid and bid in _builtin_ids():
        return True
    if bool(preset.get("builtin")):
        return True
    name = str(preset.get("name") or "").strip()
    return bool(name) and name in _builtin_names()


def is_builtin_name(name: str | None) -> bool:
    return bool(str(name or "").strip()) and str(name).strip() in _builtin_names()


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
    """One-line preset label for menus.

    Built-ins: ``Photo Quality  ·  slow · people & detail``
    User presets: ``My Shot  ·  PurePhoto…  ·  4×  ·  PNG``
    """
    name = str(preset.get("name") or "").strip() or "Preset"
    if is_builtin_preset(preset):
        blurb = str(preset.get("blurb") or "").strip()
        return f"{name}  ·  {blurb}" if blurb else name
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


def _normalize_one(item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("name") or "").strip()
    model = str(item.get("model") or "").strip()
    if not name or not model:
        return None
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
    out: dict[str, Any] = {
        "name": name,
        "model": model,
        "models_dir": str(item.get("models_dir") or "").strip(),
        "scale_mode": mode,
        "custom_scale": custom,
        "tiling": bool(item.get("tiling", False)),
        "tile_size": tile,
        "output_format": fmt,
    }
    bid = str(item.get("builtin_id") or "").strip()
    if bid:
        out["builtin_id"] = bid
    blurb = str(item.get("blurb") or "").strip()
    if blurb:
        out["blurb"] = blurb
    if bool(item.get("builtin")) or bid in _builtin_ids() or name in _builtin_names():
        out["builtin"] = True
        if not bid:
            for catalog in BUILTIN_PRESETS:
                if str(catalog.get("name") or "").strip() == name:
                    out["builtin_id"] = str(catalog.get("builtin_id") or "").strip()
                    if not blurb:
                        cat_blurb = str(catalog.get("blurb") or "").strip()
                        if cat_blurb:
                            out["blurb"] = cat_blurb
                    break
    return out


def normalize_presets(raw) -> list[dict[str, Any]]:
    """Sanitize prefs list → [{name, model, scale_mode, ...}, ...]."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry = _normalize_one(item)
        if entry is None or entry["name"] in seen:
            continue
        seen.add(entry["name"])
        out.append(entry)
    return out


def _catalog_presets() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in BUILTIN_PRESETS:
        body = dict(item)
        body["builtin"] = True
        body["models_dir"] = ""
        entry = _normalize_one(body)
        if entry is not None:
            out.append(entry)
    return out


def order_presets(presets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Built-ins in catalog order, then user presets A–Z."""
    cleaned = normalize_presets(presets)
    rank = {
        str(item.get("builtin_id") or "").strip(): idx
        for idx, item in enumerate(BUILTIN_PRESETS)
    }
    built: list[dict[str, Any]] = []
    users: list[dict[str, Any]] = []
    for preset in cleaned:
        if is_builtin_preset(preset):
            built.append(preset)
        else:
            users.append(preset)
    built.sort(
        key=lambda p: rank.get(str(p.get("builtin_id") or "").strip(), 10_000)
    )
    users.sort(key=lambda p: str(p.get("name") or "").lower())
    return built + users


def ensure_builtin_presets(raw) -> list[dict[str, Any]]:
    """Merge curated recipes into a prefs list (refresh built-ins from catalog)."""
    cleaned = normalize_presets(raw)
    known_ids = _builtin_ids()
    known_names = _builtin_names()
    users = [
        p
        for p in cleaned
        if str(p.get("builtin_id") or "").strip() not in known_ids
        and str(p.get("name") or "").strip() not in known_names
        and not bool(p.get("builtin"))
    ]
    return order_presets(_catalog_presets() + users)


def upsert_preset(presets: list[dict[str, Any]], preset: dict[str, Any]) -> list[dict[str, Any]]:
    """Insert or replace by name; returns new list. Refuses built-in names."""
    cleaned = ensure_builtin_presets(presets)
    name = str(preset.get("name") or "").strip()
    if not name or is_builtin_name(name) or bool(preset.get("builtin")):
        return cleaned
    body = normalize_presets([preset])
    if not body:
        return cleaned
    entry = body[0]
    entry.pop("builtin", None)
    entry.pop("builtin_id", None)
    out = [p for p in cleaned if p.get("name") != name]
    out.append(entry)
    return order_presets(out)


def remove_preset(presets: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Remove a user preset by name. Built-ins are kept."""
    name = str(name or "").strip()
    cleaned = ensure_builtin_presets(presets)
    if not name or is_builtin_name(name):
        return cleaned
    return order_presets([p for p in cleaned if p.get("name") != name])


def find_preset(presets: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    name = str(name or "").strip()
    for p in ensure_builtin_presets(presets):
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
    (session setting, not part of a preset).
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
