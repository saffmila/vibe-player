"""
seedvr2_config.py — Persist SeedVR2 paths in settings.json.

Supports two runners:
  - ByteDance official SeedVR (projects/inference_seedvr2_*.py + torchrun)
  - ComfyUI-SeedVR2_VideoUpscaler (inference_cli.py)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path


SETTINGS_FILENAME = "settings.json"

KEY_WEIGHTS_DIR = "seedvr2_weights_dir"
KEY_RUNNER_DIR = "seedvr2_runner_dir"
KEY_PYTHON = "seedvr2_python"
KEY_CUDA_DEVICE = "seedvr2_cuda_device"
KEY_DIT_MODEL = "seedvr2_dit_model"
KEY_KEEP_VRAM = "seedvr2_keep_vram"
KEY_PRESCALE_MODE = "seedvr2_prescale_mode"
KEY_PRESCALE_CUSTOM = "seedvr2_prescale_custom"

# Long-edge presets (px). Downscale-only before SeedVR to clear soft/compressed detail.
PRESCALE_MODE_OFF = "off"
PRESCALE_MODE_OPTIMAL = "optimal"
PRESCALE_MODE_AGGRESSIVE = "aggressive"
PRESCALE_MODE_CUSTOM = "custom"
PRESCALE_OPTIMAL_LONG_EDGE = 1280
PRESCALE_AGGRESSIVE_LONG_EDGE = 960
PRESCALE_CUSTOM_DEFAULT = 1280
PRESCALE_MODE_LABELS = (
    ("Off (original)", PRESCALE_MODE_OFF),
    ("Optimal (~1280 long edge)", PRESCALE_MODE_OPTIMAL),
    ("Aggressive (~960 long edge)", PRESCALE_MODE_AGGRESSIVE),
    ("Custom…", PRESCALE_MODE_CUSTOM),
)

BYTEDANCE_REPO_URL = "https://github.com/ByteDance-Seed/SeedVR"
COMFY_REPO_URL = "https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler"
SEEDVR2_3B_CHECKPOINT = "seedvr2_ema_3b.pth"
DEFAULT_DIT_MODEL = "seedvr2_ema_3b_fp8_e4m3fn.safetensors"


def default_weights_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return str(Path(base) / "VibePlayer" / "models" / "seedvr2")
    return str(Path.home() / ".vibeplayer" / "models" / "seedvr2")


def default_runner_dir() -> str:
    """Prefer ``<repo>/seedvr2_runner`` when present next to the app."""
    here = Path(__file__).resolve().parent  # app/
    candidates = [
        here.parent / "seedvr2_runner",
        here / "seedvr2_runner",
        Path.cwd() / "seedvr2_runner",
        Path.cwd().parent / "seedvr2_runner",
    ]
    for cand in candidates:
        if detect_runner(str(cand)):
            return str(cand.resolve())
    return ""


def settings_path() -> Path:
    return Path(SETTINGS_FILENAME).resolve()


def load_seedvr2_settings() -> dict:
    """Read SeedVR2-related keys from settings.json (safe defaults)."""
    data = {
        KEY_WEIGHTS_DIR: default_weights_dir(),
        KEY_RUNNER_DIR: default_runner_dir(),
        KEY_PYTHON: "",
        KEY_CUDA_DEVICE: "0",
        KEY_DIT_MODEL: DEFAULT_DIT_MODEL,
        KEY_KEEP_VRAM: False,
        KEY_PRESCALE_MODE: PRESCALE_MODE_OFF,
        KEY_PRESCALE_CUSTOM: PRESCALE_CUSTOM_DEFAULT,
    }
    path = settings_path()
    if not path.is_file():
        return data
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return data
        for key in (KEY_WEIGHTS_DIR, KEY_RUNNER_DIR, KEY_PYTHON, KEY_CUDA_DEVICE, KEY_DIT_MODEL):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                data[key] = val.strip()
            elif key == KEY_CUDA_DEVICE and val is not None and str(val).strip() != "":
                data[key] = str(val).strip()
            elif key == KEY_DIT_MODEL and val is not None and str(val).strip() != "":
                data[key] = str(val).strip()
        if KEY_KEEP_VRAM in raw:
            data[KEY_KEEP_VRAM] = bool(raw.get(KEY_KEEP_VRAM))
        mode = raw.get(KEY_PRESCALE_MODE)
        if isinstance(mode, str) and mode.strip().lower() in {
            PRESCALE_MODE_OFF,
            PRESCALE_MODE_OPTIMAL,
            PRESCALE_MODE_AGGRESSIVE,
            PRESCALE_MODE_CUSTOM,
        }:
            data[KEY_PRESCALE_MODE] = mode.strip().lower()
        custom = raw.get(KEY_PRESCALE_CUSTOM)
        if custom is not None:
            try:
                data[KEY_PRESCALE_CUSTOM] = max(256, min(8192, int(custom)))
            except (TypeError, ValueError):
                pass
        if not data.get(KEY_RUNNER_DIR):
            data[KEY_RUNNER_DIR] = default_runner_dir()
    except Exception as exc:
        logging.warning("[SeedVR2] Could not load settings: %s", exc)
    return data


def resolve_prescale_long_edge(
    mode: str | None,
    custom: int | str | None = None,
) -> int | None:
    """
    Return max long-edge px for downscale-before-SeedVR, or None when disabled.
    """
    m = (mode or PRESCALE_MODE_OFF).strip().lower()
    if m in ("", PRESCALE_MODE_OFF, "disabled", "none", "original"):
        return None
    if m == PRESCALE_MODE_OPTIMAL:
        return PRESCALE_OPTIMAL_LONG_EDGE
    if m == PRESCALE_MODE_AGGRESSIVE:
        return PRESCALE_AGGRESSIVE_LONG_EDGE
    if m == PRESCALE_MODE_CUSTOM:
        try:
            return max(256, min(8192, int(custom if custom is not None else PRESCALE_CUSTOM_DEFAULT)))
        except (TypeError, ValueError):
            return PRESCALE_CUSTOM_DEFAULT
    # Allow raw integer mode for convenience.
    try:
        return max(256, min(8192, int(m)))
    except (TypeError, ValueError):
        return None


def save_seedvr2_settings(
    *,
    weights_dir: str | None = None,
    runner_dir: str | None = None,
    python_path: str | None = None,
    cuda_device: str | None = None,
    dit_model: str | None = None,
    keep_vram: bool | None = None,
    prescale_mode: str | None = None,
    prescale_custom: int | None = None,
) -> dict:
    """Merge SeedVR2 keys into settings.json and return the updated seedvr subset."""
    path = settings_path()
    settings: dict = {}
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                settings = loaded
        except Exception as exc:
            logging.warning("[SeedVR2] Could not read settings for update: %s", exc)

    current = load_seedvr2_settings()
    if weights_dir is not None:
        cleaned = weights_dir.strip()
        current[KEY_WEIGHTS_DIR] = cleaned or default_weights_dir()
    if runner_dir is not None:
        current[KEY_RUNNER_DIR] = runner_dir.strip()
    if python_path is not None:
        current[KEY_PYTHON] = python_path.strip()
    if cuda_device is not None:
        current[KEY_CUDA_DEVICE] = str(cuda_device).strip() or "0"
    if dit_model is not None:
        current[KEY_DIT_MODEL] = str(dit_model).strip() or DEFAULT_DIT_MODEL
    if keep_vram is not None:
        current[KEY_KEEP_VRAM] = bool(keep_vram)
    if prescale_mode is not None:
        m = str(prescale_mode).strip().lower()
        if m not in {
            PRESCALE_MODE_OFF,
            PRESCALE_MODE_OPTIMAL,
            PRESCALE_MODE_AGGRESSIVE,
            PRESCALE_MODE_CUSTOM,
        }:
            m = PRESCALE_MODE_OFF
        current[KEY_PRESCALE_MODE] = m
    if prescale_custom is not None:
        try:
            current[KEY_PRESCALE_CUSTOM] = max(256, min(8192, int(prescale_custom)))
        except (TypeError, ValueError):
            current[KEY_PRESCALE_CUSTOM] = PRESCALE_CUSTOM_DEFAULT

    settings.update(current)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except Exception as exc:
        logging.error("[SeedVR2] Failed to save settings: %s", exc)
    return current


def find_inference_cli(runner_dir: str) -> Path | None:
    """Return path to ComfyUI inference_cli.py inside runner_dir, if present."""
    info = detect_runner(runner_dir)
    if info and info.get("kind") == "comfy":
        return Path(info["script"])
    return None


def detect_runner(runner_dir: str) -> dict | None:
    """
    Detect SeedVR runner checkout.

    Preferred: ComfyUI-SeedVR2 CLI (inference_cli.py) — not the ComfyUI GUI.
    Fallback: official ByteDance SeedVR (torchrun scripts).

    Returns dict:
      kind: "comfy" | "bytedance"
      script: path to entry script
      root: runner root
      checkpoint_name: expected weight filename (bytedance only)
    """
    if not runner_dir:
        return None
    root = Path(runner_dir)
    if not root.is_dir():
        return None

    # Prefer community CLI wrapper (best fit for Vibe Player subprocess).
    for rel in (
        Path("inference_cli.py"),
        Path("seedvr2_videoupscaler") / "inference_cli.py",
        Path("custom_nodes") / "seedvr2_videoupscaler" / "inference_cli.py",
    ):
        cand = root / rel
        if cand.is_file():
            return {
                "kind": "comfy",
                "script": str(cand),
                "root": str(cand.parent.resolve()),
                "checkpoint_name": None,
                "download_url": COMFY_REPO_URL,
            }

    for rel in (
        Path("projects") / "inference_seedvr2_3b.py",
        Path("projects") / "inference_seedvr2_7b.py",
    ):
        cand = root / rel
        if cand.is_file():
            ckpt = (
                SEEDVR2_3B_CHECKPOINT
                if "3b" in cand.name
                else "seedvr2_ema_7b.pth"
            )
            return {
                "kind": "bytedance",
                "script": str(cand),
                "root": str(root.resolve()),
                "checkpoint_name": ckpt,
                "download_url": BYTEDANCE_REPO_URL,
            }
    return None


def list_cuda_gpus() -> list[dict]:
    """
    Return GPU list for the Upscale dialog.

    Each item: {index: int, label: str, free_mb: int|None, total_mb: int|None}
    Uses nvidia-smi when available (works even if torch is busy/unavailable).
    """
    import shutil
    import subprocess

    smi = shutil.which("nvidia-smi")
    if not smi:
        return [{"index": 0, "label": "cuda:0", "free_mb": None, "total_mb": None}]

    cmd = [
        smi,
        "--query-gpu=index,name,memory.free,memory.total",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except Exception as exc:
        logging.debug("[SeedVR2] nvidia-smi failed: %s", exc)
        return [{"index": 0, "label": "cuda:0", "free_mb": None, "total_mb": None}]

    gpus: list[dict] = []
    for line in (result.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            idx = int(parts[0])
            name = parts[1]
            free_mb = int(float(parts[2]))
            total_mb = int(float(parts[3]))
        except ValueError:
            continue
        free_gb = free_mb / 1024.0
        total_gb = total_mb / 1024.0
        label = f"cuda:{idx} — {name} ({free_gb:.1f}/{total_gb:.1f} GB free)"
        gpus.append(
            {
                "index": idx,
                "label": label,
                "free_mb": free_mb,
                "total_mb": total_mb,
            }
        )
    return gpus or [{"index": 0, "label": "cuda:0", "free_mb": None, "total_mb": None}]


def _dit_sort_key(filename: str) -> tuple:
    name = filename.lower()
    size_rank = 0 if "_3b" in name else 1 if "_7b" in name else 2
    if "fp8" in name:
        prec_rank = 0
    elif "fp16" in name:
        prec_rank = 1
    elif "gguf" in name or "q4" in name or "q6" in name or "q8" in name:
        prec_rank = 2
    elif "int8" in name:
        prec_rank = 3
    else:
        prec_rank = 4
    sharp_rank = 0 if "sharp" not in name else 1
    return (size_rank, sharp_rank, prec_rank, name)


def friendly_dit_label(filename: str) -> str:
    """Short human label; value passed to CLI remains the real filename."""
    name = filename
    lower = name.lower()
    bits: list[str] = []
    if "_3b" in lower:
        bits.append("3B")
    elif "_7b" in lower:
        bits.append("7B")
    if "sharp" in lower:
        bits.append("sharp")
    if "fp8" in lower:
        bits.append("FP8")
    elif "fp16" in lower:
        bits.append("FP16")
    elif "q4" in lower:
        bits.append("GGUF Q4")
    elif "q6" in lower:
        bits.append("GGUF Q6")
    elif "q8" in lower:
        bits.append("GGUF Q8")
    elif "int8" in lower:
        bits.append("INT8")
    elif lower.endswith(".gguf"):
        bits.append("GGUF")
    summary = " · ".join(bits) if bits else "DiT"
    return f"{summary}  —  {name}"


def list_dit_models(weights_dir: str | Path | None) -> list[dict]:
    """
    Scan weights folder for SeedVR DiT checkpoints (exclude VAE).

    Returns [{filename, label, path}, ...] sorted for the Upscale dialog.
    """
    root = Path(weights_dir) if weights_dir else Path(default_weights_dir())
    found: list[dict] = []
    if not root.is_dir():
        return found

    exts = {".safetensors", ".gguf", ".pth", ".pt", ".bin"}
    for child in root.iterdir():
        if not child.is_file():
            continue
        if child.suffix.lower() not in exts:
            continue
        name = child.name
        lower = name.lower()
        if lower.startswith("ema_vae") or "vae" == lower.split("_")[0]:
            continue
        if "vae" in lower and "seedvr" not in lower:
            continue
        # DiT / SeedVR model files
        if not any(tok in lower for tok in ("seedvr", "dit", "ema_3b", "ema_7b")):
            continue
        found.append(
            {
                "filename": name,
                "label": friendly_dit_label(name),
                "path": str(child),
            }
        )

    found.sort(key=lambda item: _dit_sort_key(item["filename"]))
    return found


def ensure_model_visible_to_runner(
    runner_dir: str | Path,
    weights_dir: str | Path,
    dit_model: str,
) -> Path | None:
    """
    ComfyUI CLI builds ``--dit_model`` choices from ``./models/SEEDVR2`` (+ registry).

    Symlink/hardlink the selected weight (and VAE if present) into that folder so
    custom/quantized files in the user weights dir are accepted by argparse.
    """
    runner_dir = Path(runner_dir)
    weights_dir = Path(weights_dir)
    dest_dir = runner_dir / "models" / "SEEDVR2"
    dest_dir.mkdir(parents=True, exist_ok=True)

    source = weights_dir / dit_model
    if not source.is_file():
        # already only a filename elsewhere?
        found = find_checkpoint_file(weights_dir, dit_model)
        if found is None:
            return None
        source = found

    target = dest_dir / source.name
    if target.is_file() and target.stat().st_size == source.stat().st_size:
        # Good enough — already present
        pass
    else:
        try:
            if target.exists() or target.is_symlink():
                target.unlink()
        except OSError:
            pass
        linked = False
        for linker in (
            lambda: os.link(str(source), str(target)),
            lambda: target.symlink_to(source),
        ):
            try:
                linker()
                linked = True
                break
            except OSError:
                continue
        if not linked:
            try:
                import shutil

                shutil.copy2(source, target)
            except OSError as exc:
                logging.error("[SeedVR2] Could not stage model into runner: %s", exc)
                return None

    # Stage default VAE next to it when available (CLI default).
    for vae_name in ("ema_vae_fp16.safetensors",):
        vae_src = weights_dir / vae_name
        if not vae_src.is_file():
            continue
        vae_dst = dest_dir / vae_name
        if vae_dst.exists():
            continue
        try:
            os.link(str(vae_src), str(vae_dst))
        except OSError:
            try:
                vae_dst.symlink_to(vae_src)
            except OSError:
                try:
                    import shutil

                    shutil.copy2(vae_src, vae_dst)
                except OSError:
                    pass

    return target


def resolve_runner_python(runner_dir: str, configured: str = "") -> str:
    """Prefer explicit python, then runner venv, then current interpreter."""
    if configured and Path(configured).is_file():
        return configured
    if runner_dir:
        root = Path(runner_dir)
        for rel in (
            Path(".venv") / "Scripts" / "python.exe",
            Path("venv") / "Scripts" / "python.exe",
            Path(".venv") / "bin" / "python",
            Path("venv") / "bin" / "python",
        ):
            cand = root / rel
            if cand.is_file():
                return str(cand)
            parent_cand = root.parent.parent / rel
            if parent_cand.is_file():
                return str(parent_cand)
    import sys

    return sys.executable


def find_checkpoint_file(weights_dir: str | Path, checkpoint_name: str) -> Path | None:
    """Locate a SeedVR checkpoint under weights_dir (or nested)."""
    root = Path(weights_dir)
    if not root.is_dir() or not checkpoint_name:
        return None
    direct = root / checkpoint_name
    if direct.is_file():
        return direct
    matches = list(root.rglob(checkpoint_name))
    for match in matches:
        if match.is_file():
            return match
    stem = Path(checkpoint_name).stem.lower()
    for child in root.rglob("*"):
        if not child.is_file():
            continue
        name = child.name.lower()
        if stem in name and name.endswith((".pth", ".pt", ".safetensors", ".bin")):
            try:
                if child.stat().st_size > 10 * 1024 * 1024:
                    return child
            except OSError:
                continue
    return None


def ensure_bytedance_ckpts_link(
    runner_root: str | Path,
    weights_dir: str | Path,
    checkpoint_name: str,
) -> Path | None:
    """
    Official scripts hardcode ``./ckpts/<checkpoint>``.
    Ensure that path exists by linking/copying from the user weights folder.
    """
    runner_root = Path(runner_root)
    ckpts = runner_root / "ckpts"
    ckpts.mkdir(parents=True, exist_ok=True)
    target = ckpts / checkpoint_name
    if target.is_file():
        return target

    source = find_checkpoint_file(weights_dir, checkpoint_name)
    if source is None:
        # Also accept weights already living under runner/ckpts with other names.
        alt = find_checkpoint_file(ckpts, checkpoint_name)
        if alt is not None and alt.resolve() != target.resolve():
            source = alt
        else:
            return None

    try:
        if target.exists() or target.is_symlink():
            target.unlink()
    except OSError:
        pass

    try:
        os.link(str(source), str(target))
        return target
    except OSError:
        pass
    try:
        target.symlink_to(source)
        return target
    except OSError:
        pass
    try:
        import shutil

        shutil.copy2(source, target)
        return target
    except OSError as exc:
        logging.error("[SeedVR2] Could not place checkpoint into ckpts/: %s", exc)
        return None
