"""
Persist per-folder thumbnail grid scroll positions (Tk yview fractions).

Stored in ``folder_scroll_state.json`` so favorites navigation and app restart
can restore where the user left off in each folder.
"""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from typing import Mapping, MutableMapping, Optional

SCROLL_STATE_FILE = "folder_scroll_state.json"
MAX_SCROLL_ENTRIES = 300


def normalize_scroll_path(path: str) -> Optional[str]:
    """Return a stable cache key, or None for virtual / empty paths."""
    if not path or not isinstance(path, str):
        return None
    if path.startswith("virtual_library://"):
        return None
    try:
        return os.path.normcase(os.path.normpath(path))
    except (OSError, TypeError, ValueError):
        return None


def clamp_yview(frac) -> Optional[float]:
    try:
        value = float(frac)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return max(0.0, min(1.0, value))


def load_folder_scroll_state(
    path: str = SCROLL_STATE_FILE,
) -> OrderedDict[str, float]:
    """Load path → yview fraction map (insertion order = least→most recent)."""
    if not os.path.exists(path):
        return OrderedDict()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("scroll", data) if isinstance(data, dict) else {}
        if not isinstance(raw, dict):
            return OrderedDict()
        out: OrderedDict[str, float] = OrderedDict()
        for key, value in raw.items():
            norm = normalize_scroll_path(str(key))
            frac = clamp_yview(value)
            if norm is None or frac is None:
                continue
            out.pop(norm, None)
            out[norm] = frac
        return out
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        logging.info("[ScrollState] Failed to load %s: %s", path, exc)
        return OrderedDict()


def save_folder_scroll_state(
    scroll: Mapping[str, float],
    path: str = SCROLL_STATE_FILE,
) -> None:
    """Persist scroll map, trimming to ``MAX_SCROLL_ENTRIES`` (keep newest)."""
    payload: OrderedDict[str, float] = OrderedDict()
    items = list(scroll.items()) if scroll else []
    if len(items) > MAX_SCROLL_ENTRIES:
        items = items[-MAX_SCROLL_ENTRIES:]
    for key, value in items:
        norm = normalize_scroll_path(str(key))
        frac = clamp_yview(value)
        if norm is None or frac is None:
            continue
        payload.pop(norm, None)
        payload[norm] = frac
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"scroll": payload}, f, indent=2)
    except OSError as exc:
        logging.info("[ScrollState] Failed to save %s: %s", path, exc)


def remember_folder_scroll(
    scroll: MutableMapping[str, float],
    path: str,
    frac: float,
) -> None:
    """Update in-memory map (moves path to most-recent)."""
    key = normalize_scroll_path(path)
    value = clamp_yview(frac)
    if key is None or value is None:
        return
    scroll.pop(key, None)
    scroll[key] = value
    while len(scroll) > MAX_SCROLL_ENTRIES:
        try:
            next(iter(scroll))
            scroll.popitem(last=False)  # type: ignore[call-arg]
        except Exception:
            # Plain dict fallback: drop an arbitrary oldest-ish key
            oldest = next(iter(scroll), None)
            if oldest is None:
                break
            scroll.pop(oldest, None)
