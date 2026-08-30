"""
birefnet_config.py — BiRefNet background removal (optional GPU pack + user weights).

PyTorch FP16 @ 1024; weights live under models/birefnet/ (downloaded on demand).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from vtp_constants import IMAGE_FORMATS

BIREFNET_MODEL_ID = "ZhengPeng7/BiRefNet"
BIREFNET_HF_URL = f"https://huggingface.co/{BIREFNET_MODEL_ID}"
BIREFNET_INPUT_SIZE = 1024
BIREFNET_DISK_ESTIMATE = "~900 MB"

BIREFNET_MODEL_VARIANTS: dict[str, dict[str, str]] = {
    "general": {
        "label": "General",
        "model_id": "ZhengPeng7/BiRefNet",
    },
    "matting": {
        "label": "Matting (hair, fur)",
        "model_id": "ZhengPeng7/BiRefNet-matting",
    },
    "portrait": {
        "label": "Portrait",
        "model_id": "ZhengPeng7/BiRefNet-portrait",
    },
}

DEFAULT_MODEL_VARIANT = "general"

GPU_PACK_MISSING_MESSAGE = "Autotag GPU Pack is not installed (PyTorch CUDA required)."
WEIGHTS_MISSING_MESSAGE = (
    "BiRefNet weights are not installed.\n\n"
    f"Download from {BIREFNET_HF_URL}\n"
    "or use Install weights in the Remove Background dialog."
)

_IMAGE_EXTS = frozenset(ext.lower() for ext in IMAGE_FORMATS)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_birefnet_dir() -> Path:
    """User-writable model cache (portable + dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "models" / "birefnet"
    return _repo_root() / "models" / "birefnet"


def _snapshot_dirs(root: Path) -> list[Path]:
    """Known on-disk layouts: flat folder or Hugging Face cache tree."""
    found: list[Path] = []
    if not root.is_dir():
        return found
    if (root / "config.json").is_file():
        found.append(root)
    for snap in root.glob("models--*/snapshots/*"):
        if (snap / "config.json").is_file():
            found.append(snap)
    for snap in root.glob("snapshots/*"):
        if (snap / "config.json").is_file():
            found.append(snap)
    # De-dupe
    seen: set[str] = set()
    out: list[Path] = []
    for p in found:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def resolve_model_variant(variant: str | None) -> str:
    key = (variant or DEFAULT_MODEL_VARIANT).strip().lower()
    return key if key in BIREFNET_MODEL_VARIANTS else DEFAULT_MODEL_VARIANT


def resolve_model_id(variant: str | None = None) -> str:
    key = resolve_model_variant(variant)
    return BIREFNET_MODEL_VARIANTS[key]["model_id"]


def hf_cache_slug(model_id: str) -> str:
    org, name = model_id.split("/", 1)
    safe = name.replace("/", "--")
    return f"models--{org}--{safe}"


def find_model_dir(*, model_variant: str | None = None, model_id: str | None = None) -> Path | None:
    mid = model_id if model_id else resolve_model_id(model_variant)
    root = default_birefnet_dir()
    slug = hf_cache_slug(mid)
    for snap in root.glob(f"{slug}/snapshots/*"):
        if (snap / "config.json").is_file() and (
            any(snap.glob("*.safetensors")) or any(snap.glob("pytorch_model.bin"))
        ):
            return snap
    if mid == BIREFNET_MODEL_ID:
        for snap in _snapshot_dirs(root):
            if any(snap.glob("*.safetensors")) or any(snap.glob("pytorch_model.bin")):
                return snap
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


def python_deps_status() -> dict[str, Any]:
    """BiRefNet modeling code needs einops + kornia (transformers trust_remote_code)."""
    missing: list[str] = []
    for name in ("einops", "kornia"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        joined = ", ".join(missing)
        return {
            "ready": False,
            "error": "deps_missing",
            "message": (
                f"Missing Python packages: {joined}.\n"
                f"Run: pip install {' '.join(missing)}"
            ),
            "missing": missing,
        }
    return {"ready": True, "error": None, "message": None, "missing": []}


def runtime_status(*, deep: bool = False) -> dict[str, Any]:
    """
    Check PyTorch CUDA availability (GPU pack).

    ``deep=True`` also imports torch and probes ``cuda.is_available()``.
    """
    deps = python_deps_status()
    if not deps.get("ready"):
        return deps

    try:
        import torch  # noqa: F401
    except Exception as exc:
        if _is_gpu_pack_missing_error(exc):
            return {
                "ready": False,
                "error": "gpu_pack_missing",
                "message": GPU_PACK_MISSING_MESSAGE,
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
            if "+cpu" in ver.lower() or not getattr(torch.version, "cuda", None):
                return {
                    "ready": False,
                    "error": "cuda_unavailable",
                    "message": (
                        "PyTorch is installed without CUDA support "
                        f"({ver or 'CPU build'}).\n\n"
                        "Your GPU is fine — reinstall PyTorch with CUDA wheels:\n"
                        "  pip install -r requirements.txt\n"
                        "(or run install.bat in a fresh venv).\n\n"
                        "Portable users: extract the Autotag GPU pack over VibePlayer/."
                    ),
                }
            return {
                "ready": False,
                "error": "cuda_unavailable",
                "message": (
                    "PyTorch is installed but CUDA is not available.\n"
                    f"torch {ver}\n\n"
                    "Update NVIDIA drivers or reinstall the CUDA PyTorch build "
                    "(pip install -r requirements.txt)."
                ),
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
                "message": GPU_PACK_MISSING_MESSAGE,
            }
        return {
            "ready": False,
            "error": "runtime_error",
            "message": str(exc),
        }


def weights_status(*, model_variant: str | None = None) -> dict[str, Any]:
    variant = resolve_model_variant(model_variant)
    model_id = resolve_model_id(variant)
    model_dir = find_model_dir(model_id=model_id)
    ready = model_dir is not None
    label = BIREFNET_MODEL_VARIANTS[variant]["label"]
    return {
        "ready": ready,
        "path": str(model_dir) if model_dir else str(default_birefnet_dir()),
        "download_url": f"https://huggingface.co/{model_id}",
        "message": None
        if ready
        else (
            f"{label} weights are not installed.\n\n"
            f"Download from https://huggingface.co/{model_id}\n"
            "or use Install weights in the Remove Background dialog."
        ),
        "model_id": model_id,
        "model_variant": variant,
    }


def birefnet_options_from_controller(controller: Any | None) -> dict[str, Any]:
    """Snapshot of BiRefNet prefs stored on the main window (or defaults)."""

    def _get(name: str, default: Any) -> Any:
        if controller is None:
            return default
        return getattr(controller, name, default)

    try:
        feather = max(0, min(5, int(_get("birefnet_mask_feather", 0) or 0)))
    except (TypeError, ValueError):
        feather = 0
    try:
        threshold = max(0, min(100, int(_get("birefnet_mask_threshold", 0) or 0)))
    except (TypeError, ValueError):
        threshold = 0
    try:
        morph = max(-1, min(1, int(_get("birefnet_mask_morph", 0) or 0)))
    except (TypeError, ValueError):
        morph = 0
    mode = str(_get("birefnet_bg_mode", "transparent") or "transparent").strip().lower()
    return {
        "cuda_device": str(_get("birefnet_cuda_device", "0") or "0").strip() or "0",
        "model_variant": resolve_model_variant(_get("birefnet_model_variant", DEFAULT_MODEL_VARIANT)),
        "mask_feather": feather,
        "mask_threshold": threshold,
        "mask_morph": morph,
        "suffix": str(_get("birefnet_suffix", "_nobg") or "_nobg").strip() or "_nobg",
        "bg_mode": "color" if mode == "color" else "transparent",
        "bg_color": normalize_hex_color(_get("birefnet_bg_color", None)) or "#FFFFFF",
    }


def format_birefnet_summary(options: dict[str, Any] | None) -> str:
    """One-line summary for Batch Convert (model · background · GPU)."""
    opts = options or {}
    variant = resolve_model_variant(opts.get("model_variant"))
    label = BIREFNET_MODEL_VARIANTS[variant]["label"]
    mode = str(opts.get("bg_mode") or "transparent").strip().lower()
    if mode == "color":
        bg = normalize_hex_color(opts.get("bg_color")) or "#FFFFFF"
    else:
        bg = "transparent"
    gpu = str(opts.get("cuda_device") or "0").strip() or "0"
    return f"{label} · {bg} · GPU {gpu}"


def supports_image(file_path: str) -> bool:
    if not file_path or not Path(file_path).is_file():
        return False
    ext = Path(file_path).suffix.lower()
    return ext in _IMAGE_EXTS


def normalize_hex_color(value: str | None) -> str | None:
    """Return ``#RRGGBB`` or None."""
    s = (value or "").strip()
    if not s:
        return None
    if s.lower() in ("transparent", "none"):
        return None
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return None
    try:
        int(s, 16)
    except ValueError:
        return None
    return f"#{s.upper()}"


def parse_bg_rgb(value: str | None, *, default: str = "#FFFFFF") -> tuple[int, int, int]:
    """Parse hex color to RGB tuple."""
    hex_color = normalize_hex_color(value) or normalize_hex_color(default) or "#FFFFFF"
    h = hex_color[1:]
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
