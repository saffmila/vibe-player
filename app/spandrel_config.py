"""
spandrel_config.py — Single-pass Spandrel upscalers (RealPLKSR, SPAN, DAT, …).

Weights live under models/upscale/ (user-dropped .pth / .safetensors).
Uses the app-process CUDA PyTorch pack (same stack as BiRefNet / CLIP).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from vtp_constants import IMAGE_FORMATS

# OpenModelDB / Real-ESRGAN names: 4xPurePhoto-…, 2x_…, …_x4plus
_SCALE_IN_NAME = re.compile(
    r"(?i)(?:^|[_\-\s./])(\d+(?:\.\d+)?)x|(?:^|[_\-\s./])x(\d+(?:\.\d+)?)"
)

WEIGHT_EXTENSIONS = (".pth", ".pt", ".safetensors", ".ckpt")
OPENMODELDB_URL = "https://openmodeldb.info/"

GPU_PACK_MISSING_MESSAGE = (
    "PyTorch CUDA (GPU pack) is not available.\n\n"
    "Portable: extract VibePlayer-autotag-gpu-pack*.zip over your VibePlayer/ "
    "folder (all parts if split), then restart Vibe Player.\n"
    "Dev: install CUDA PyTorch into the app venv (pip install -r requirements.txt)."
)
SPANDREL_MISSING_MESSAGE = (
    "Python package 'spandrel' is not installed.\n\n"
    "Dev: pip install spandrel\n"
    "Portable: include spandrel in the GPU/autotag pack, then restart."
)
WEIGHTS_MISSING_MESSAGE = (
    "No Spandrel upscale models found.\n\n"
    f"Drop .pth / .safetensors files into:\n  {{path}}\n\n"
    f"Browse models at {OPENMODELDB_URL}\n"
    "(RealPLKSR, SPAN, DRCT, DAT, ESRGAN, …)."
)

_IMAGE_EXTS = frozenset(ext.lower() for ext in IMAGE_FORMATS)


def _repo_root() -> Path:
    from app_paths import install_root

    return install_root()


def default_upscale_dir() -> Path:
    """User-writable single-pass SR weights under the install root."""
    return _repo_root() / "models" / "upscale"


def resolve_models_dir(explicit: str | Path | None = None) -> Path:
    """Prefer a user-picked folder; fall back to models/upscale."""
    raw = str(explicit or "").strip().strip('"')
    if raw:
        path = Path(raw)
        if path.is_dir():
            return path.resolve()
    return default_upscale_dir()


def guess_scale_from_name(name: str | None) -> float | None:
    """Best-effort scale from filename (e.g. ``4xFoo.pth`` / ``x4plus`` → 4.0)."""
    if not name:
        return None
    match = _SCALE_IN_NAME.search(str(name))
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def supports_image(file_path: str | None) -> bool:
    if not file_path:
        return False
    from os.path import isfile, splitext

    return isfile(file_path) and splitext(file_path)[1].lower() in _IMAGE_EXTS


def list_spandrel_models(models_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """
    Scan for Spandrel-loadable weight files.

    Returns [{filename, label, path}, ...] sorted by name.
    """
    root = resolve_models_dir(models_dir)
    if not root.is_dir():
        return []

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in WEIGHT_EXTENSIONS:
            continue
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        found.append(
            {
                "filename": path.name,
                "label": rel,
                "rel": rel,
                "path": str(path.resolve()),
                "scale": guess_scale_from_name(path.name),
            }
        )
    return found


def resolve_model_path(
    model: str | None,
    *,
    models_dir: str | Path | None = None,
) -> Path | None:
    """Resolve a model filename, relative path, or absolute path under the models dir."""
    raw = (model or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_file():
        return candidate.resolve()
    root = resolve_models_dir(models_dir)
    nested = (root / raw).resolve()
    if nested.is_file():
        return nested
    # Normalize slashes for rel matches (UI may show !!Best/foo.pth).
    raw_norm = raw.replace("\\", "/").lower()
    for item in list_spandrel_models(root):
        rel = str(item.get("rel") or item.get("label") or "").replace("\\", "/").lower()
        if rel == raw_norm or item["filename"].lower() == raw_norm:
            return Path(item["path"])
        if item["path"].lower() == raw_norm:
            return Path(item["path"])
    return None


def _is_gpu_pack_missing_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "no module named 'torch",
        'no module named "torch',
        "torch not available",
        "cudnn",
        "cublas",
        "cufft",
        "cusparse",
        "torch_cuda.dll",
        "torch_python.dll",
        "winerror 126",
        "dll load failed",
    )
    return any(marker in text for marker in markers)


def spandrel_import_status() -> dict[str, Any]:
    try:
        import spandrel  # noqa: F401
    except ImportError:
        return {
            "ready": False,
            "error": "spandrel_missing",
            "message": SPANDREL_MISSING_MESSAGE,
        }
    except Exception as exc:
        return {
            "ready": False,
            "error": "runtime_error",
            "message": f"Failed to import spandrel: {exc}",
        }
    return {"ready": True, "error": None, "message": None}


def runtime_status(*, deep: bool = False) -> dict[str, Any]:
    """Check Spandrel + PyTorch CUDA availability."""
    sp = spandrel_import_status()
    if not sp.get("ready"):
        return sp

    try:
        import torch  # noqa: F401
    except Exception as exc:
        if _is_gpu_pack_missing_error(exc):
            return {
                "ready": False,
                "error": "gpu_pack_missing",
                "message": f"{GPU_PACK_MISSING_MESSAGE}\n\nDetails: {exc}",
            }
        return {
            "ready": False,
            "error": "runtime_error",
            "message": str(exc),
        }

    if not deep:
        return {"ready": True, "error": None, "message": None}

    try:
        import torch

        if not torch.cuda.is_available():
            ver = getattr(torch, "__version__", "")
            return {
                "ready": False,
                "error": "cuda_unavailable",
                "message": (
                    "PyTorch is installed but CUDA is not available.\n"
                    f"torch {ver}\n\n"
                    "Spandrel can fall back to CPU (very slow). "
                    "Prefer a CUDA GPU pack / CUDA torch build."
                ),
                "cpu_fallback": True,
                "torch": ver,
            }
        ver = getattr(torch, "__version__", "")
        return {
            "ready": True,
            "error": None,
            "message": None,
            "torch": ver,
            "device": torch.cuda.get_device_name(0) if torch.cuda.device_count() else "",
        }
    except Exception as exc:
        if _is_gpu_pack_missing_error(exc):
            return {
                "ready": False,
                "error": "gpu_pack_missing",
                "message": f"{GPU_PACK_MISSING_MESSAGE}\n\nDetails: {exc}",
            }
        return {
            "ready": False,
            "error": "runtime_error",
            "message": str(exc),
        }


def weights_status(*, models_dir: str | Path | None = None) -> dict[str, Any]:
    root = resolve_models_dir(models_dir)
    models = list_spandrel_models(root)
    ready = bool(models)
    msg = None
    if not ready:
        msg = WEIGHTS_MISSING_MESSAGE.format(path=str(root))
    return {
        "ready": ready,
        "path": str(root),
        "download_url": OPENMODELDB_URL,
        "message": msg,
        "models": models,
        "count": len(models),
    }
