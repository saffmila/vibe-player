"""
rife_config.py — Locate optional rife-ncnn-vulkan pack (tools/rife/).

The binary + models are NOT part of the base install. Users extract
``VibePlayer-rife-pack`` over the portable folder, or run
``python scripts/fetch_rife_ncnn.py`` during development.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

RIFE_PROJECT_URL = "https://github.com/nihui/rife-ncnn-vulkan"
RIFE_RELEASE_TAG = "20221029"
RIFE_WINDOWS_ZIP_URL = (
    f"{RIFE_PROJECT_URL}/releases/download/{RIFE_RELEASE_TAG}/"
    f"rife-ncnn-vulkan-{RIFE_RELEASE_TAG}-windows.zip"
)

# Prefer newer v4 models when several are shipped in the pack.
PREFERRED_MODELS = (
    "rife-v4.6",
    "rife-v4",
    "rife-v3.1",
    "rife-v2.3",
    "rife",
)

PACK_MISSING_MESSAGE = (
    "RIFE pack not found. Download the optional RIFE pack and extract it into "
    "your Vibe Player folder (so tools/rife/rife-ncnn-vulkan.exe exists), "
    "or run: python scripts/fetch_rife_ncnn.py"
)

MODEL_MISSING_MESSAGE = (
    "RIFE models not found next to rife-ncnn-vulkan.exe. Re-extract the full "
    f"optional pack from {RIFE_PROJECT_URL}/releases."
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _candidate_rife_dirs() -> list[Path]:
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        meipass = Path(getattr(sys, "_MEIPASS", exe_dir))
        dirs.append(exe_dir / "tools" / "rife")
        dirs.append(meipass / "tools" / "rife")
    root = _repo_root()
    dirs.append(root / "tools" / "rife")
    dirs.append(Path.cwd() / "tools" / "rife")
    # De-dupe while preserving order.
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        key = str(d.resolve()) if d.exists() else str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def default_rife_dir() -> Path:
    """Preferred install location for the optional pack (dev + portable)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "tools" / "rife"
    return _repo_root() / "tools" / "rife"


def find_rife_exe() -> str | None:
    """Return path to rife-ncnn-vulkan.exe if the optional pack is present."""
    names = ("rife-ncnn-vulkan.exe", "rife-ncnn-vulkan")
    for folder in _candidate_rife_dirs():
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                return str(candidate.resolve())
        # Release zip sometimes nests one extra folder.
        if folder.is_dir():
            for child in folder.iterdir():
                if not child.is_dir():
                    continue
                for name in names:
                    candidate = child / name
                    if candidate.is_file():
                        return str(candidate.resolve())
    return None


def find_rife_dir() -> Path | None:
    exe = find_rife_exe()
    if not exe:
        return None
    return Path(exe).resolve().parent


def list_rife_models(rife_dir: Path | None = None) -> list[str]:
    """Return model folder names that contain ncnn param/bin files."""
    base = rife_dir or find_rife_dir()
    if base is None or not base.is_dir():
        return []
    found: list[str] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        has_param = any(child.glob("*.param"))
        has_bin = any(child.glob("*.bin"))
        if has_param and has_bin:
            found.append(child.name)
    return found


def resolve_model_path(model_name: str | None = None, rife_dir: Path | None = None) -> Path | None:
    base = rife_dir or find_rife_dir()
    if base is None:
        return None
    available = list_rife_models(base)
    if not available:
        return None
    if model_name and model_name in available:
        return base / model_name
    for preferred in PREFERRED_MODELS:
        if preferred in available:
            return base / preferred
    return base / available[0]


def runtime_status() -> dict[str, Any]:
    """Check whether the optional RIFE pack is ready to run."""
    exe = find_rife_exe()
    if not exe:
        return {
            "ready": False,
            "error": "rife_pack_missing",
            "message": PACK_MISSING_MESSAGE,
            "exe": None,
            "dir": str(default_rife_dir()),
        }
    model = resolve_model_path(rife_dir=Path(exe).parent)
    if model is None:
        return {
            "ready": False,
            "error": "rife_model_missing",
            "message": MODEL_MISSING_MESSAGE,
            "exe": exe,
            "dir": str(Path(exe).parent),
        }
    return {
        "ready": True,
        "error": None,
        "message": None,
        "exe": exe,
        "dir": str(Path(exe).parent),
        "model": str(model),
        "models": list_rife_models(Path(exe).parent),
    }
