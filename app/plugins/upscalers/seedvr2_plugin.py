"""
seedvr2_plugin.py — SeedVR 2 offline video/image upscale backend.

Inference runs in an isolated ComfyUI-SeedVR2 runner ``.venv`` (Install runner…).
Model weights live in a configurable folder (Install weights… downloads the
recommended 3B FP8 + VAE from Hugging Face).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from plugins.processing_base import UpscaleBackend
from seedvr2_config import (
    DEFAULT_DIT_MODEL,
    KEY_CUDA_DEVICE,
    KEY_DIT_MODEL,
    KEY_KEEP_VRAM,
    KEY_PYTHON,
    KEY_RUNNER_DIR,
    KEY_WEIGHTS_DIR,
    COMFY_REPO_URL,
    default_weights_dir,
    detect_runner,
    ensure_model_visible_to_runner,
    load_seedvr2_settings,
    resolve_prescale_long_edge,
    resolve_runner_python,
)
from seedvr2_progress import SeedVR2ProgressState
from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS

# Extensions SeedVR2 CLI accepts as video (subset of VIDEO_FORMATS + .m4v).
SEEDVR2_VIDEO_FORMATS = frozenset(
    {
        ".mp4",
        ".avi",
        ".mov",
        ".mkv",
        ".webm",
        ".flv",
        ".wmv",
        ".m4v",
    }
)


def _preferred_attention_mode(
    python_exe: str | None = None,
    *,
    keep_vram: bool = False,
) -> str:
    """
    Pick a stable attention backend for the SeedVR runner venv.

    SageAttention 2 uses Triton kernels that break when DiT/VAE caching
    offloads tensors to CPU (``Keep model in VRAM`` path) — typical error:
    ``Pointer argument cannot be accessed from Triton (cpu tensor?)``.

    Prefer Flash Attention 2 (CUDA ext) then SDPA. Sage only when explicitly
    requested via options.
    """
    py = (python_exe or "").strip()
    if py and os.path.isfile(py):
        # keep_vram → avoid sageattn_2 (Triton + CPU offload).
        code = (
            "import importlib.util as u\n"
            "keep = " + ("True" if keep_vram else "False") + "\n"
            "if (not keep) and u.find_spec('sageattn3'):\n"
            "    print('sageattn_3')\n"
            "elif u.find_spec('flash_attn'):\n"
            "    print('flash_attn_2')\n"
            "else:\n"
            "    print('sdpa')\n"
        )
        try:
            out = subprocess.check_output(
                [py, "-c", code],
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                stderr=subprocess.DEVNULL,
            ).strip()
            if out in {"sageattn_3", "flash_attn_2", "sdpa"}:
                return out
        except Exception:
            pass
    return "sdpa"


SEEDVR2_PROJECT_URL = "https://github.com/ByteDance-Seed/SeedVR"
SEEDVR2_WEIGHTS_URL = "https://huggingface.co/numz/SeedVR2_comfyUI"
SEEDVR2_RUNNER_URL = COMFY_REPO_URL

RUNTIME_MISSING_MESSAGE = (
    "SeedVR 2 runner environment is not ready. Click “Install runner…” under "
    "Advanced → Paths (downloads ComfyUI-SeedVR2 + a CUDA PyTorch .venv)."
)
WEIGHTS_MISSING_MESSAGE = (
    "SeedVR 2 weights not found. Click “Install weights…” under Advanced → Paths "
    f"to download the recommended 3B FP8 model + VAE (~4 GB) from {SEEDVR2_WEIGHTS_URL}."
)
RUNNER_MISSING_MESSAGE = (
    "SeedVR 2 runner not configured. Click “Install runner…” to download the "
    "ComfyUI-SeedVR2 CLI checkout automatically, or point Runner folder at a "
    f"directory that contains inference_cli.py ({SEEDVR2_RUNNER_URL})."
)
OOM_HELP_MESSAGE = (
    "GPU out of memory (VRAM) during SeedVR 2.\n\n"
    "Try:\n"
    "• Prefer FP8 3B model (not FP16 / 7B) on 24 GB cards\n"
    "• Lower Scale (2×), or Prescale → Optimal / Aggressive\n"
    "• Enable “Low VRAM (tiled VAE)”\n"
    "• Use the RTX 4090 for heavy FP16 jobs if 5090 VRAM is tight\n"
    "• Close other GPU apps; turn off Keep-in-VRAM and retry"
)


def _is_oom_text(text: str) -> bool:
    low = (text or "").lower()
    return any(
        tok in low
        for tok in (
            "out of memory",
            "outofmemory",
            "cuda out of memory",
            "allocation on device",
            "cudnn_status_alloc_failed",
            "hip out of memory",
            "aborthandler",
            "unhandled exception caught in c10",
        )
    )


def _model_looks_heavy(dit_model: str) -> bool:
    """FP16 / 7B / sharp need more VRAM than FP8 3B."""
    name = (dit_model or "").lower()
    if "fp8" in name and "_3b" in name:
        return False
    if "_7b" in name or "sharp" in name:
        return True
    if "fp16" in name or name.endswith(".pth"):
        return True
    return False


def _auto_video_batch(resolution: int, dit_model: str) -> int:
    """
    Pick a 4n+1 video batch that fits common 24 GB cards.

    ComfyUI HD uses 33 at ~1080 on big VRAM; portrait 2× (1440 short edge)
    + FP16 easily OOMs a 4090 at batch 33.
    """
    heavy = _model_looks_heavy(dit_model)
    if resolution >= 1440:
        return 17 if heavy else 21
    if resolution >= 1080 and heavy:
        return 21
    return VIDEO_BATCH_SIZE_DEFAULT


def _format_runner_failure(tail: str) -> tuple[str, str]:
    """Return (error_code, user_message) for a failed runner log tail."""
    if _is_oom_text(tail):
        return "oom", OOM_HELP_MESSAGE
    low = (tail or "").lower()
    if "cannot be accessed from triton" in low or "cpu tensor?" in low:
        return (
            "attention_backend",
            "SeedVR attention backend failed (SageAttention/Triton + CPU offload).\n\n"
            "Retry after restart — the app now prefers Flash Attention 2.\n"
            "Or turn off “Keep model in VRAM” and try again.",
        )
    short = (tail or "").strip()
    if len(short) > 900:
        short = short[-900:]
    return "runner_failed", f"SeedVR 2 failed:\n{short}"


def _is_gpu_pack_missing_error(exc: BaseException) -> bool:
    """Legacy helper — kept for error-text heuristics only."""
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


def _seedvr_batch_4n1(value: int) -> int:
    """SeedVR requires batch_size in {1, 5, 9, 13, ...} (4n+1)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 5
    n = max(1, n)
    return max(1, ((n - 1) // 4) * 4 + 1)


# ComfyUI HD video upscale default (matches SeedVR temporal window).
VIDEO_BATCH_SIZE_DEFAULT = 33
IMAGE_BATCH_SIZE_DEFAULT = 5


def _has_weight_files(path: Path) -> bool:
    if not path.is_dir():
        return False
    for child in path.rglob("*"):
        if not child.is_file():
            continue
        name = child.name.lower()
        if name.endswith((".pt", ".pth", ".safetensors", ".bin", ".ckpt", ".gguf")):
            try:
                if child.stat().st_size > 1024 * 1024:
                    return True
            except OSError:
                continue
    return False


def _probe_short_side(file_path: str) -> int | None:
    """Best-effort short-side pixels for scale→resolution mapping."""
    size = _probe_size(file_path)
    if not size:
        return None
    return min(size)


def _probe_long_side(file_path: str) -> int | None:
    size = _probe_size(file_path)
    if not size:
        return None
    return max(size)


def _probe_size(file_path: str) -> tuple[int, int] | None:
    """Return (width, height) when possible."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in IMAGE_FORMATS:
        try:
            from PIL import Image

            with Image.open(file_path) as im:
                w, h = im.size
            if w > 0 and h > 0:
                return int(w), int(h)
        except Exception:
            pass
    try:
        from file_operations import get_ffprobe_path

        ffprobe = get_ffprobe_path()
        cmd = [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            file_path,
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
        if "x" in out:
            w_s, h_s = out.split("x", 1)
            w, h = int(w_s), int(h_s)
            if w > 0 and h > 0:
                return w, h
    except Exception:
        pass
    return None


def prepare_prescaled_input(
    input_path: str,
    max_long_edge: int | None,
    progress_cb: Callable[[float, str], None] | None = None,
) -> tuple[str, str | None]:
    """
    Downscale so the longest side is at most ``max_long_edge`` (Lanczos / FFmpeg).

    Returns ``(path_for_inference, temp_path_or_None)``. Temp must be deleted by caller.
    Skips when disabled, already small enough, or on failure (falls back to original).
    """
    if not max_long_edge or max_long_edge <= 0:
        return input_path, None

    long_side = _probe_long_side(input_path)
    if long_side is not None and long_side <= max_long_edge:
        logging.info(
            "[SeedVR2 Prescale] Skip — long edge %spx ≤ %spx",
            long_side,
            max_long_edge,
        )
        return input_path, None

    import tempfile

    ext = os.path.splitext(input_path)[1].lower()
    stem = Path(input_path).stem
    if progress_cb:
        progress_cb(0.02, f"Prescale → max {max_long_edge}px long edge…", "load")

    try:
        if ext in IMAGE_FORMATS:
            from PIL import Image

            with Image.open(input_path) as im:
                if im.mode != "RGB":
                    im = im.convert("RGB")
                w, h = im.size
                if max(w, h) <= max_long_edge:
                    return input_path, None
                if w >= h:
                    new_w = max_long_edge
                    new_h = max(1, int(round(h * (max_long_edge / float(w)))))
                else:
                    new_h = max_long_edge
                    new_w = max(1, int(round(w * (max_long_edge / float(h)))))
                # Keep even dims for video-ish pipelines; harmless for stills.
                new_w -= new_w % 2
                new_h -= new_h % 2
                new_w = max(2, new_w)
                new_h = max(2, new_h)
                try:
                    resample = Image.Resampling.LANCZOS
                except AttributeError:
                    resample = Image.LANCZOS
                resized = im.resize((new_w, new_h), resample)
                out_ext = ".png" if ext in (".png", ".webp") else ".jpg"
                fd, temp_path = tempfile.mkstemp(
                    prefix=f"{stem}_prescale_", suffix=out_ext
                )
                os.close(fd)
                save_kw: dict[str, Any] = {}
                if out_ext == ".jpg":
                    save_kw.update({"quality": 95, "optimize": True})
                resized.save(temp_path, **save_kw)
            logging.info(
                "[SeedVR2 Prescale] Image %s → %s (%dx%d)",
                input_path,
                temp_path,
                new_w,
                new_h,
            )
            return temp_path, temp_path

        # Video (and unknown): FFmpeg long-edge downscale.
        from file_operations import get_ffmpeg_path

        ffmpeg = get_ffmpeg_path()
        fd, temp_path = tempfile.mkstemp(prefix=f"{stem}_prescale_", suffix=".mp4")
        os.close(fd)
        try:
            os.remove(temp_path)
        except OSError:
            pass

        # Downscale only when longer side exceeds max; keep aspect; even dims.
        m = int(max_long_edge)
        scale_filter = (
            f"scale="
            f"'if(gt(iw,ih),min(iw,{m}),-2)':"
            f"'if(gt(ih,iw),min(ih,{m}),-2)':"
            f"flags=lanczos"
        )
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            input_path,
            "-vf",
            scale_filter,
            "-c:v",
            "libx264",
            "-crf",
            "17",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            temp_path,
        ]
        logging.info("[SeedVR2 Prescale] %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.isfile(temp_path):
            logging.error(
                "[SeedVR2 Prescale] FFmpeg failed: %s",
                (result.stderr or result.stdout or "")[:500],
            )
            try:
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            return input_path, None
        return temp_path, temp_path
    except Exception as exc:
        logging.error("[SeedVR2 Prescale] Failed: %s", exc)
        return input_path, None


def _cleanup_temp(path: str | None) -> None:
    """Best-effort delete of a temporary prescale file."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _ffmpeg_hide_window_kwargs() -> dict:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def _source_has_audio(path: str) -> bool:
    """True when ffprobe finds at least one audio stream."""
    try:
        from file_operations import get_ffprobe_path

        ffprobe = get_ffprobe_path()
    except Exception:
        return False
    if not path or not os.path.isfile(path):
        return False
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=45,
            **_ffmpeg_hide_window_kwargs(),
        )
        return bool((result.stdout or "").strip())
    except Exception:
        return False


def _mux_source_audio_into_output(
    source_path: str,
    output_path: str,
    *,
    progress_cb: Callable[..., None] | None = None,
) -> str:
    """
    Remux source audio into SeedVR's silent video output when present.

    Always-on (no UI toggle): video-only sources are left unchanged. Prefer
    ``-c:a copy``, fall back to AAC if the container rejects the codec.
    Returns the final output path (same path on success or soft failure).
    """
    if not source_path or not output_path:
        return output_path
    if not os.path.isfile(source_path) or not os.path.isfile(output_path):
        return output_path
    if not _source_has_audio(source_path):
        logging.info("[SeedVR2] Source has no audio stream — leaving output as-is")
        return output_path
    try:
        from file_operations import get_ffmpeg_path

        ffmpeg = get_ffmpeg_path()
    except Exception as exc:
        logging.warning("[SeedVR2] Skipping audio mux (ffmpeg missing): %s", exc)
        return output_path

    if progress_cb:
        try:
            progress_cb(0.97, "Muxing source audio…", "upscale")
        except TypeError:
            progress_cb(0.97, "Muxing source audio…")

    parent = os.path.dirname(output_path) or "."
    tmp = os.path.join(parent, f".{Path(output_path).stem}_audiomux.tmp.mp4")
    try:
        if os.path.isfile(tmp):
            os.remove(tmp)
    except OSError:
        pass

    def _run(audio_args: list[str]) -> subprocess.CompletedProcess:
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            output_path,
            "-i",
            source_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            *audio_args,
            "-shortest",
            "-movflags",
            "+faststart",
            tmp,
        ]
        logging.info("[SeedVR2] Audio mux: %s", " ".join(cmd))
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900,
            **_ffmpeg_hide_window_kwargs(),
        )

    result = _run(["-c:a", "copy"])
    if result.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) < 64:
        logging.info(
            "[SeedVR2] Audio stream copy failed — retrying with AAC (%s)",
            (result.stderr or result.stdout or "")[:240],
        )
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        result = _run(["-c:a", "aac", "-b:a", "192k"])

    if result.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) < 64:
        logging.warning(
            "[SeedVR2] Audio mux failed — keeping silent video: %s",
            (result.stderr or result.stdout or "")[:500],
        )
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return output_path

    try:
        os.replace(tmp, output_path)
        logging.info("[SeedVR2] Muxed source audio into %s", output_path)
    except OSError as exc:
        logging.warning("[SeedVR2] Could not replace output with muxed file: %s", exc)
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
    return output_path


# Back-compat alias matching the Gemini/spec helper name.
def prepare_prescaled_media(file_path: str, max_dim: int) -> str:
    """
    Downscale media when the long edge exceeds ``max_dim``.

    Returns the path to use for inference (original or a temp file).
    Caller must delete the returned path when it differs from ``file_path``.
    """
    work_path, _temp = prepare_prescaled_input(
        file_path, max_dim if max_dim and max_dim > 0 else None
    )
    return work_path


class SeedVR2UpscalePlugin(UpscaleBackend):
    """Offline SeedVR 2 upscaler (lazy-loaded; optional GPU pack + external CLI)."""

    id = "seedvr2"
    name = "SeedVR 2"
    lazy_load = True

    def __init__(self, settings: dict | None = None) -> None:
        super().__init__(settings)
        self.reload_config()

    def reload_config(self) -> None:
        cfg = load_seedvr2_settings()
        override = (self.settings or {}).get("weights_dir")
        self.weights_dir = Path(override) if override else Path(cfg[KEY_WEIGHTS_DIR] or default_weights_dir())
        self.runner_dir = str(cfg.get(KEY_RUNNER_DIR) or "")
        self.python_path = str(cfg.get(KEY_PYTHON) or "")
        self.cuda_device = str(cfg.get(KEY_CUDA_DEVICE) or "0")
        self.dit_model = str(cfg.get(KEY_DIT_MODEL) or DEFAULT_DIT_MODEL)
        self.keep_vram = bool(cfg.get(KEY_KEEP_VRAM))

    def supports(self, file_path: str) -> bool:
        ext = os.path.splitext(file_path)[1].lower()
        return ext in IMAGE_FORMATS or ext in SEEDVR2_VIDEO_FORMATS

    def _runner_python_exe(self) -> str | None:
        """Resolve runner venv python path without importing torch."""
        runner = (self.runner_dir or "").strip()
        if not runner or not Path(runner).is_dir():
            return None
        configured = ""
        try:
            configured = str(
                (self.settings or {}).get(KEY_PYTHON)
                or load_seedvr2_settings().get(KEY_PYTHON)
                or ""
            ).strip()
        except Exception:
            configured = ""
        py = resolve_runner_python(runner, configured)
        for rel in (
            Path(".venv") / "Scripts" / "python.exe",
            Path(".venv") / "bin" / "python",
            Path("venv") / "Scripts" / "python.exe",
            Path("venv") / "bin" / "python",
        ):
            cand = Path(runner) / rel
            if cand.is_file():
                return str(cand)
        return py if py and Path(py).is_file() else None

    def runtime_status(self, *, deep: bool = True) -> dict[str, Any]:
        """
        Check the SeedVR2 runner environment.

        ``deep=False`` only verifies the runner ``.venv`` python exists (fast,
        safe for dialog open). ``deep=True`` also probes ``import torch`` + CUDA
        (slow cold start — use on Start / after install).
        """
        runner = (self.runner_dir or "").strip()
        if not runner or not Path(runner).is_dir():
            return {
                "ready": False,
                "error": "runner_missing",
                "message": RUNTIME_MISSING_MESSAGE,
            }
        py = self._runner_python_exe()
        if not py:
            return {
                "ready": False,
                "error": "runner_venv_missing",
                "message": RUNTIME_MISSING_MESSAGE,
            }
        if not deep:
            return {
                "ready": True,
                "error": None,
                "message": None,
                "python": py,
                "deep": False,
            }
        try:
            out = subprocess.check_output(
                [
                    py,
                    "-c",
                    "import torch; "
                    "print('cuda' if torch.cuda.is_available() else 'cpu'); "
                    "print(getattr(torch, '__version__', ''))",
                ],
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                stderr=subprocess.STDOUT,
                cwd=runner,
            ).strip().splitlines()
            device = (out[0] if out else "").strip().lower()
            ver = (out[1] if len(out) > 1 else "").strip()
            if device != "cuda":
                return {
                    "ready": False,
                    "error": "cuda_unavailable",
                    "message": (
                        "SeedVR 2 runner PyTorch has no CUDA device.\n"
                        f"Python: {py}\n"
                        f"torch: {ver or '?'}\n\n"
                        "Install a current NVIDIA driver, then re-run "
                        "“Install runner…” if needed."
                    ),
                    "python": py,
                }
            return {
                "ready": True,
                "error": None,
                "message": None,
                "python": py,
                "torch": ver,
                "deep": True,
            }
        except Exception as exc:
            return {
                "ready": False,
                "error": "runtime_error",
                "message": (
                    f"SeedVR 2 runner environment error:\n{exc}\n\n"
                    "Click “Install runner…” under Advanced → Paths."
                ),
                "python": py,
            }

    def weights_status(self) -> dict[str, Any]:
        path = self.weights_dir
        ready = _has_weight_files(path)
        return {
            "ready": ready,
            "path": str(path),
            "download_url": SEEDVR2_WEIGHTS_URL,
            "project_url": SEEDVR2_PROJECT_URL,
            "message": None if ready else WEIGHTS_MISSING_MESSAGE,
        }

    def runner_status(self, *, deep: bool = True) -> dict[str, Any]:
        """
        Check ComfyUI CLI checkout.

        ``deep=False`` only checks ``inference_cli.py`` + ``.venv`` python file
        (fast). ``deep=True`` also runs ``import torch`` in that venv.
        """
        info = detect_runner(self.runner_dir)
        # Prefer ComfyUI CLI wrapper; ByteDance research checkout is not wired for Start.
        if info and info.get("kind") == "bytedance":
            return {
                "ready": False,
                "path": self.runner_dir,
                "cli": None,
                "download_url": SEEDVR2_RUNNER_URL,
                "message": (
                    "This folder is the ByteDance research repo. "
                    "Please use the ComfyUI-SeedVR2 CLI checkout instead "
                    f"({SEEDVR2_RUNNER_URL}) — folder must contain inference_cli.py."
                ),
            }
        if not info or info.get("kind") != "comfy":
            return {
                "ready": False,
                "path": self.runner_dir,
                "cli": None,
                "download_url": SEEDVR2_RUNNER_URL,
                "message": RUNNER_MISSING_MESSAGE,
            }
        root = Path(info.get("root") or self.runner_dir)
        venv_py = root / ".venv" / "Scripts" / "python.exe"
        if not venv_py.is_file():
            venv_py = root / ".venv" / "bin" / "python"
        if not venv_py.is_file():
            return {
                "ready": False,
                "path": self.runner_dir,
                "cli": info.get("script"),
                "download_url": SEEDVR2_RUNNER_URL,
                "message": (
                    "Runner sources found, but the CUDA .venv is missing or broken. "
                    "Click “Install runner…” to finish setup."
                ),
            }
        if deep:
            try:
                from seedvr2_runner_setup import runner_venv_ready

                if not runner_venv_ready(root):
                    return {
                        "ready": False,
                        "path": self.runner_dir,
                        "cli": info.get("script"),
                        "download_url": SEEDVR2_RUNNER_URL,
                        "message": (
                            "Runner sources found, but the CUDA .venv is missing or broken. "
                            "Click “Install runner…” to finish setup."
                        ),
                    }
            except Exception:
                pass
        return {
            "ready": True,
            "path": self.runner_dir,
            "cli": info.get("script"),
            "download_url": SEEDVR2_RUNNER_URL,
            "message": None,
        }

    def default_options(self) -> dict[str, Any]:
        return {
            "scale": 2,
            "suffix": "_seedvr2",
            "output_dir": None,
            "batch_size": None,  # None/0 = auto (video by model/res; images → 5)
            "resolution": None,  # auto from scale × short side
            "cuda_device": None,  # None = use saved setting
            "dit_model": None,  # None = use saved setting
            "keep_vram": None,  # None = use saved setting
            "vae_tiled": True,  # tiled VAE encode/decode — much lower VRAM
            "uniform_batch_size": None,  # None = auto (on for video)
            "temporal_overlap": None,
            "chunk_size": None,
            "vae_encode_tile_size": 1024,
            "vae_decode_tile_size": 768,
            "chunk_preview": True,
            "preview_path": None,
            "output_format": "png",  # images: png|jpg (never keep webp/source)
            "prescale_mode": "off",
            "prescale_long_edge": None,  # int when custom / resolved
        }

    def suggested_output_path(self, input_path: str, options: dict[str, Any] | None = None) -> str:
        opts = {**self.default_options(), **(options or {})}
        explicit = opts.get("output_path")
        if isinstance(explicit, str) and explicit.strip():
            return os.path.abspath(explicit.strip())
        suffix = str(opts.get("suffix") or "_seedvr2")
        out_dir = opts.get("output_dir") or os.path.dirname(input_path)
        stem, ext = os.path.splitext(os.path.basename(input_path))
        ext_l = ext.lower()
        # Images: never keep source container (webp/jfif/…). Default PNG.
        if ext_l in IMAGE_FORMATS:
            fmt = str(opts.get("output_format") or "png").strip().lower()
            if fmt in ("jpg", "jpeg"):
                ext = ".jpg"
            else:
                ext = ".png"
        elif ext_l in SEEDVR2_VIDEO_FORMATS or ext_l in VIDEO_FORMATS:
            # SeedVR video output is always mp4.
            ext = ".mp4"
        return os.path.join(out_dir, f"{stem}{suffix}{ext}")

    def _target_resolution(self, input_path: str, options: dict[str, Any]) -> int:
        explicit = options.get("resolution")
        if explicit:
            try:
                return max(256, int(explicit))
            except (TypeError, ValueError):
                pass
        try:
            scale = max(1, int(options.get("scale") or 2))
        except (TypeError, ValueError):
            scale = 2
        short = _probe_short_side(input_path)
        if short:
            return max(256, int(short * scale))
        # Fallback when probe fails: common SeedVR defaults.
        return 1080 if scale <= 2 else 2160

    def process(
        self,
        input_path: str,
        options: dict[str, Any] | None = None,
        progress_cb: Callable[[float, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self.reload_config()
        opts = {**self.default_options(), **(options or {})}

        if not os.path.isfile(input_path):
            return {
                "ok": False,
                "output_path": None,
                "error": "missing_input",
                "message": f"File not found: {input_path}",
            }
        if not self.supports(input_path):
            return {
                "ok": False,
                "output_path": None,
                "error": "unsupported",
                "message": f"Unsupported file type: {input_path}",
            }

        runtime = self.runtime_status()
        if not runtime.get("ready"):
            return {
                "ok": False,
                "output_path": None,
                "error": runtime.get("error") or "runtime_error",
                "message": runtime.get("message"),
            }

        weights = self.weights_status()
        if not weights.get("ready"):
            return {
                "ok": False,
                "output_path": None,
                "error": "weights_missing",
                "message": weights.get("message") or WEIGHTS_MISSING_MESSAGE,
            }

        runner = self.runner_status()
        if not runner.get("ready"):
            return {
                "ok": False,
                "output_path": None,
                "error": "runner_missing",
                "message": runner.get("message") or RUNNER_MISSING_MESSAGE,
            }

        if should_stop and should_stop():
            return {
                "ok": False,
                "output_path": None,
                "error": "aborted",
                "message": "Upscale aborted.",
            }

        cli_path = Path(runner["cli"])
        python_exe = resolve_runner_python(self.runner_dir, self.python_path)
        output_path = self.suggested_output_path(input_path, opts)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        max_long = opts.get("prescale_long_edge")
        if max_long is None:
            max_long = resolve_prescale_long_edge(
                opts.get("prescale_mode"),
                opts.get("prescale_custom"),
            )
        else:
            try:
                max_long = int(max_long)
            except (TypeError, ValueError):
                max_long = resolve_prescale_long_edge(
                    opts.get("prescale_mode"),
                    opts.get("prescale_custom"),
                )

        work_path, temp_prescale = prepare_prescaled_input(
            input_path, max_long, progress_cb=progress_cb
        )
        try:
            return self._process_after_prescale(
                input_path=input_path,
                work_path=work_path,
                output_path=output_path,
                opts=opts,
                cli_path=cli_path,
                python_exe=python_exe,
                progress_cb=progress_cb,
                should_stop=should_stop,
            )
        finally:
            _cleanup_temp(temp_prescale)

    def _process_after_prescale(
        self,
        *,
        input_path: str,
        work_path: str,
        output_path: str,
        opts: dict[str, Any],
        cli_path: Path,
        python_exe: str,
        progress_cb: Callable[[float, str], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        # Resolution is based on the (possibly prescaled) work input.
        resolution = self._target_resolution(work_path, opts)
        is_video = os.path.splitext(work_path)[1].lower() in SEEDVR2_VIDEO_FORMATS

        cuda_device = str(opts.get("cuda_device") or self.cuda_device or "0").strip() or "0"
        dit_model = str(opts.get("dit_model") or self.dit_model or DEFAULT_DIT_MODEL).strip() or DEFAULT_DIT_MODEL

        # ComfyUI HD video uses batch_size=33 (+ uniform). Images stay on small batches.
        # Auto when unset/0; explicit dialog values always win.
        raw_batch = opts.get("batch_size")
        try:
            raw_batch_i = int(raw_batch) if raw_batch is not None else 0
        except (TypeError, ValueError):
            raw_batch_i = 0
        if is_video:
            if raw_batch_i <= 0:
                batch_size = _auto_video_batch(resolution, dit_model)
            else:
                batch_size = raw_batch_i
        else:
            batch_size = raw_batch_i if raw_batch_i > 0 else IMAGE_BATCH_SIZE_DEFAULT
        batch_size = _seedvr_batch_4n1(batch_size)
        if is_video:
            logging.info(
                "[SeedVR2] Video batch_size=%s (res=%s, model=%s)",
                batch_size,
                resolution,
                dit_model,
            )

        uniform_batch = opts.get("uniform_batch_size")
        if uniform_batch is None:
            uniform_batch = bool(is_video)
        else:
            uniform_batch = bool(uniform_batch)

        keep_vram = opts.get("keep_vram")
        if keep_vram is None:
            keep_vram = bool(getattr(self, "keep_vram", False))
        else:
            keep_vram = bool(keep_vram)

        vae_tiled = opts.get("vae_tiled")
        if vae_tiled is None:
            vae_tiled = True
        else:
            vae_tiled = bool(vae_tiled)

        try:
            encode_tile = int(opts.get("vae_encode_tile_size") or 1024)
        except (TypeError, ValueError):
            encode_tile = 1024
        try:
            decode_tile = int(opts.get("vae_decode_tile_size") or 768)
        except (TypeError, ValueError):
            decode_tile = 768
        attention_mode = str(opts.get("attention_mode") or "").strip()
        if not attention_mode:
            attention_mode = _preferred_attention_mode(
                python_exe, keep_vram=bool(keep_vram)
            )
        # SageAttention 2 + Triton crashes when weights are CPU-offloaded.
        if keep_vram and attention_mode.startswith("sageattn"):
            logging.warning(
                "[SeedVR2] Forcing flash_attn_2/sdpa — sageattn incompatible with Keep-VRAM CPU offload"
            )
            attention_mode = _preferred_attention_mode(python_exe, keep_vram=True)
        logging.info("[SeedVR2] attention_mode=%s (keep_vram=%s, vae_tiled=%s)", attention_mode, keep_vram, vae_tiled)

        staged = ensure_model_visible_to_runner(self.runner_dir, self.weights_dir, dit_model)
        if staged is None and not (Path(self.weights_dir) / dit_model).is_file():
            return {
                "ok": False,
                "output_path": None,
                "error": "weights_missing",
                "message": f"Selected model not found: {dit_model}",
            }

        video_backend = "opencv"
        try:
            from file_operations import get_ffmpeg_path

            ffmpeg = get_ffmpeg_path()
            if ffmpeg and os.path.isfile(ffmpeg):
                video_backend = "ffmpeg"
                os.environ.setdefault("FFMPEG_BINARY", ffmpeg)
                ff_dir = os.path.dirname(ffmpeg)
                path_env = os.environ.get("PATH", "")
                if ff_dir and ff_dir not in path_env.split(os.pathsep):
                    os.environ["PATH"] = ff_dir + os.pathsep + path_env
        except Exception:
            pass

        # Stream long videos in chunks so RAM/VRAM stay bounded.
        chunk_size = 0
        temporal_overlap = 0
        if is_video:
            try:
                chunk_size = max(0, int(opts.get("chunk_size") or 0))
            except (TypeError, ValueError):
                chunk_size = 0
            if chunk_size <= 0:
                # Prefer ~10× batch window (Comfy examples use 330 with batch 33).
                chunk_size = max(batch_size * 10, 100)
                # Keep chunk length compatible with temporal batches.
                chunk_size = _seedvr_batch_4n1(chunk_size)
            try:
                if opts.get("temporal_overlap") is None:
                    temporal_overlap = 3
                else:
                    temporal_overlap = max(0, int(opts.get("temporal_overlap")))
            except (TypeError, ValueError):
                temporal_overlap = 3

        if keep_vram:
            return self._process_persistent(
                input_path=work_path,
                audio_source_path=input_path,
                output_path=output_path,
                python_exe=python_exe,
                cli_path=cli_path,
                cuda_device=cuda_device,
                dit_model=dit_model,
                resolution=resolution,
                batch_size=batch_size,
                video_backend=video_backend,
                vae_tiled=vae_tiled,
                chunk_size=chunk_size,
                temporal_overlap=temporal_overlap,
                uniform_batch_size=uniform_batch,
                encode_tile=encode_tile,
                decode_tile=decode_tile,
                attention_mode=attention_mode,
                is_video=is_video,
                preview_path=str(opts.get("preview_path") or "").strip() or None,
                chunk_preview=bool(opts.get("chunk_preview", True)),
                progress_cb=progress_cb,
                should_stop=should_stop,
            )

        runner_root = str(cli_path.parent)
        preview_path = str(opts.get("preview_path") or "").strip()
        chunk_preview = bool(opts.get("chunk_preview", True)) and bool(preview_path) and is_video
        bootstrap = Path(__file__).resolve().parents[2] / "seedvr2_cli_bootstrap.py"
        cli_args = [
            os.path.abspath(work_path),
            "--model_dir",
            str(self.weights_dir),
            "--output",
            os.path.abspath(output_path),
            "--resolution",
            str(resolution),
            "--batch_size",
            str(batch_size),
            "--cuda_device",
            cuda_device,
            "--dit_model",
            dit_model,
            "--attention_mode",
            attention_mode,
        ]
        if uniform_batch:
            cli_args.append("--uniform_batch_size")
        if is_video:
            cli_args.extend(["--output_format", "mp4"])
            if video_backend == "ffmpeg":
                cli_args.extend(["--video_backend", "ffmpeg"])
            if chunk_size > 0:
                cli_args.extend(["--chunk_size", str(chunk_size)])
            if temporal_overlap > 0:
                cli_args.extend(["--temporal_overlap", str(temporal_overlap)])
        elif video_backend == "ffmpeg":
            # Harmless for stills; kept for runner compatibility.
            cli_args.extend(["--video_backend", "ffmpeg"])
        if vae_tiled:
            cli_args.extend(["--vae_encode_tiled", "--vae_decode_tiled"])
            cli_args.extend(
                [
                    "--vae_encode_tile_size",
                    str(encode_tile),
                    "--vae_decode_tile_size",
                    str(decode_tile),
                ]
            )

        # Prefer ffmpeg backend when available (bundled with Vibe Player).
        env = os.environ.copy()
        # Runner prints emoji status lines; Windows cp1250 consoles crash without UTF-8.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if chunk_preview and bootstrap.is_file():
            env["SEEDVR2_RUNNER_DIR"] = runner_root
            env["VIBE_SEEDVR2_PREVIEW_PATH"] = preview_path
            env["VIBE_SEEDVR2_CHUNK_PREVIEW"] = "1"
            cmd = [python_exe, str(bootstrap), *cli_args]
        else:
            cmd = [python_exe, str(cli_path), *cli_args]

        if progress_cb:
            progress_cb(
                0.05,
                f"Starting SeedVR 2 (gpu={cuda_device}, model={dit_model}, res={resolution})…",
                "load",
            )

        logging.info("[SeedVR2] Running: %s", " ".join(cmd))
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cli_path.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except Exception as exc:
            return {
                "ok": False,
                "output_path": None,
                "error": "runner_error",
                "message": f"Failed to start SeedVR 2 runner: {exc}",
            }

        log_lines: list[str] = []
        progress_state = SeedVR2ProgressState()
        assert proc.stdout is not None
        for line in proc.stdout:
            if should_stop and should_stop():
                try:
                    proc.terminate()
                except Exception:
                    pass
                return {
                    "ok": False,
                    "output_path": None,
                    "error": "aborted",
                    "message": "Upscale aborted.",
                }
            text = line.rstrip()
            if text:
                log_lines.append(text)
                logging.info("[SeedVR2] %s", text)
                if progress_cb:
                    frac, msg, phase = progress_state.update(text)
                    progress_cb(frac, msg, phase)

        code = proc.wait()
        if code != 0:
            tail = "\n".join(log_lines[-20:]) if log_lines else f"exit code {code}"
            err, msg = _format_runner_failure(tail)
            return {
                "ok": False,
                "output_path": None,
                "error": err,
                "message": msg,
            }

        # CLI may write a slightly different name; accept nearby matches.
        final_path = output_path
        if not os.path.isfile(final_path):
            stem = Path(output_path).stem
            parent = Path(output_path).parent
            candidates = sorted(parent.glob(f"{stem}*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for cand in candidates:
                if cand.is_file() and cand.suffix.lower() in {Path(output_path).suffix.lower(), ".mp4", ".png", ".jpg"}:
                    final_path = str(cand)
                    break

        if not os.path.isfile(final_path):
            return {
                "ok": False,
                "output_path": None,
                "error": "missing_output",
                "message": "SeedVR 2 finished but output file was not found.",
            }

        # If CLI wrote without our suffix, rename into place.
        if os.path.abspath(final_path) != os.path.abspath(output_path):
            try:
                if not os.path.exists(output_path):
                    shutil.move(final_path, output_path)
                    final_path = output_path
            except Exception as exc:
                logging.warning("[SeedVR2] Could not rename output: %s", exc)

        if is_video:
            final_path = _mux_source_audio_into_output(
                input_path,
                final_path,
                progress_cb=progress_cb,
            )

        if progress_cb:
            progress_cb(1.0, "SeedVR 2: done", "upscale")
        return {
            "ok": True,
            "output_path": final_path,
            "error": None,
            "message": None,
        }

    def _process_persistent(
        self,
        *,
        input_path: str,
        output_path: str,
        python_exe: str,
        cli_path: Path,
        cuda_device: str,
        dit_model: str,
        resolution: int,
        batch_size: int,
        video_backend: str,
        vae_tiled: bool = True,
        chunk_size: int = 0,
        temporal_overlap: int = 0,
        uniform_batch_size: bool = False,
        encode_tile: int = 1024,
        decode_tile: int = 768,
        attention_mode: str = "sdpa",
        is_video: bool = False,
        preview_path: str | None = None,
        chunk_preview: bool = True,
        audio_source_path: str | None = None,
        progress_cb: Callable[[float, str], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        """Reuse a long-lived worker so the model stays in VRAM until app exit."""
        from seedvr2_worker_host import get_seedvr2_worker_host

        worker_script = str(
            Path(__file__).resolve().parents[2] / "seedvr2_persistent_worker.py"
        )
        if not os.path.isfile(worker_script):
            return {
                "ok": False,
                "output_path": None,
                "error": "worker_missing",
                "message": f"Persistent worker script not found: {worker_script}",
            }

        host = get_seedvr2_worker_host()
        try:
            host.ensure_started(python_exe, str(cli_path.parent), worker_script)
        except Exception as exc:
            return {
                "ok": False,
                "output_path": None,
                "error": "worker_start_failed",
                "message": str(exc),
            }

        if progress_cb:
            progress_cb(
                0.05,
                f"Loading model {dit_model} (gpu={cuda_device})…",
                "load",
            )

        job_opts = {
            "resolution": resolution,
            "batch_size": batch_size,
            "video_backend": video_backend,
            "vae_tiled": bool(vae_tiled),
            "uniform_batch_size": bool(uniform_batch_size),
            "attention_mode": attention_mode or "sdpa",
            "chunk_preview": bool(chunk_preview) and bool(is_video),
        }
        if preview_path and job_opts["chunk_preview"]:
            job_opts["preview_path"] = str(preview_path)
        if is_video:
            job_opts["output_format"] = "mp4"
            if chunk_size > 0:
                job_opts["chunk_size"] = int(chunk_size)
            if temporal_overlap > 0:
                job_opts["temporal_overlap"] = int(temporal_overlap)
        if vae_tiled:
            job_opts["vae_encode_tile_size"] = int(encode_tile)
            job_opts["vae_decode_tile_size"] = int(decode_tile)

        result = host.upscale(
            input_path=os.path.abspath(input_path),
            output_path=os.path.abspath(output_path),
            model_dir=str(self.weights_dir),
            dit_model=dit_model,
            cuda_device=cuda_device,
            options=job_opts,
            progress_cb=progress_cb,
            should_stop=should_stop,
        )
        if result.get("ok") and result.get("output_path"):
            out = str(result["output_path"])
            if is_video:
                out = _mux_source_audio_into_output(
                    audio_source_path or input_path,
                    out,
                    progress_cb=progress_cb,
                )
            if progress_cb:
                progress_cb(1.0, "SeedVR 2: done (model kept in VRAM)", "upscale")
            return {
                "ok": True,
                "output_path": out,
                "error": None,
                "message": None,
            }
        err = result.get("error") or "runner_failed"
        msg = result.get("message") or "Persistent worker failed"
        if _is_oom_text(msg) or "cannot be accessed from triton" in msg.lower():
            err, msg = _format_runner_failure(msg)
        return {
            "ok": False,
            "output_path": None,
            "error": err,
            "message": msg,
        }


plugin_class = SeedVR2UpscalePlugin
