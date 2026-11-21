"""
topaz_favorites.py — Named Topaz Photo AI presets (model + scale + format).
"""

from __future__ import annotations

import re
from typing import Any


def model_stem_suffix(model: str | None) -> str:
    """Default output suffix from Topaz model label (no spaces), with leading ``_``."""
    raw = str(model or "").strip() or "topaz"
    # Drop a trailing extension if someone pasted a filename.
    if raw.lower().endswith((".pth", ".onnx", ".bin")):
        raw = raw.rsplit(".", 1)[0]
    stem = re.sub(r"[^\w\-]+", "_", raw).strip("_") or "topaz"
    return stem if stem.startswith("_") else f"_{stem}"


def normalize_presets(raw) -> list[dict[str, Any]]:
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
        try:
            scale = max(1, min(6, int(item.get("scale") or 4)))
        except Exception:
            scale = 4
        fmt = str(item.get("output_format") or "png").strip().lower()
        if fmt in ("jpeg",):
            fmt = "jpg"
        if fmt not in ("png", "jpg", "tiff", "webp"):
            fmt = "png"
        out.append(
            {
                "name": name,
                "model": model,
                "scale": scale,
                "output_format": fmt,
            }
        )
    return out


def upsert_preset(presets: list[dict[str, Any]], preset: dict[str, Any]) -> list[dict[str, Any]]:
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


def preset_to_options(preset: dict[str, Any], *, output_dir: str) -> dict[str, Any]:
    model = str(preset.get("model") or "").strip()
    try:
        scale = max(1, min(6, int(preset.get("scale") or 4)))
    except Exception:
        scale = 4
    return {
        "backend_id": "topaz",
        "output_dir": output_dir,
        "scale": scale,
        "model": model,
        "suffix": model_stem_suffix(model),
        "output_format": str(preset.get("output_format") or "png"),
    }
