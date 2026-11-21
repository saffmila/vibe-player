"""Writable install-root helpers for portable builds and source checkouts."""

from __future__ import annotations

import sys
from pathlib import Path


def install_root() -> Path:
    """
    Directory that owns user-writable data for this install.

    Frozen/portable: folder with ``VibePlayer.exe``.
    Source: repository root (parent of ``app/``).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_thumbnail_cache_dir() -> Path:
    return install_root() / "thumbnail_cache"
