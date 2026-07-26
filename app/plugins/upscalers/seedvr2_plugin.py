"""
seedvr2_plugin.py — SeedVR 2 offline video/image upscale backend.

Runtime (torch/CUDA) comes from the optional Autotag GPU Pack.
Model weights are user-downloaded into a configurable folder.
Inference runs via an external ComfyUI-SeedVR2 checkout
(``inference_cli.py`` from numz/ComfyUI-SeedVR2_VideoUpscaler).
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
    resolve_runner_python,
)
from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS

SEEDVR2_PROJECT_URL = "https://github.com/ByteDance-Seed/SeedVR"
SEEDVR2_WEIGHTS_URL = "https://huggingface.co/models?other=seedvr"
SEEDVR2_RUNNER_URL = COMFY_REPO_URL

GPU_PACK_MISSING_MESSAGE = "Není nainstalovaný Autotag GPU Pack (potřeba pro SeedVR 2)."
WEIGHTS_MISSING_MESSAGE = (
    "SeedVR 2 weights not found. Download them from Hugging Face "
    f"({SEEDVR2_WEIGHTS_URL}) and place them in the models folder."
)
RUNNER_MISSING_MESSAGE = (
    "SeedVR 2 runner not configured. Use the ComfyUI-SeedVR2 CLI checkout "
    f"({SEEDVR2_RUNNER_URL}) — not the ByteDance research repo and not ComfyUI GUI. "
    "Clone it, create .venv, then set Runner folder to the directory that contains "
    "inference_cli.py."
)


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
    ext = os.path.splitext(file_path)[1].lower()
    if ext in IMAGE_FORMATS:
        try:
            from PIL import Image

            with Image.open(file_path) as im:
                w, h = im.size
            if w > 0 and h > 0:
                return min(w, h)
        except Exception:
            pass
        return None
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
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            startupinfo=startupinfo,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split("x")
            if len(parts) >= 2:
                w, h = int(parts[0]), int(parts[1])
                if w > 0 and h > 0:
                    return min(w, h)
    except Exception as exc:
        logging.debug("[SeedVR2] Could not probe short side: %s", exc)
    return None


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
        return ext in VIDEO_FORMATS or ext in IMAGE_FORMATS

    def runtime_status(self) -> dict[str, Any]:
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
                "message": f"SeedVR 2 runtime error: {exc}",
            }
        return {"ready": True, "error": None, "message": None}

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

    def runner_status(self) -> dict[str, Any]:
        info = detect_runner(self.runner_dir)
        # Prefer ComfyUI CLI wrapper; ByteDance research checkout is not wired for Start.
        ready = bool(info and info.get("kind") == "comfy")
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
        return {
            "ready": ready,
            "path": self.runner_dir,
            "cli": info.get("script") if info else None,
            "download_url": SEEDVR2_RUNNER_URL,
            "message": None if ready else RUNNER_MISSING_MESSAGE,
        }

    def default_options(self) -> dict[str, Any]:
        return {
            "scale": 2,
            "suffix": "_seedvr2",
            "output_dir": None,
            "batch_size": 5,
            "resolution": None,  # auto from scale × short side
            "cuda_device": None,  # None = use saved setting
            "dit_model": None,  # None = use saved setting
            "keep_vram": None,  # None = use saved setting
        }

    def suggested_output_path(self, input_path: str, options: dict[str, Any] | None = None) -> str:
        opts = {**self.default_options(), **(options or {})}
        explicit = opts.get("output_path")
        if isinstance(explicit, str) and explicit.strip():
            return os.path.abspath(explicit.strip())
        suffix = str(opts.get("suffix") or "_seedvr2")
        out_dir = opts.get("output_dir") or os.path.dirname(input_path)
        stem, ext = os.path.splitext(os.path.basename(input_path))
        # CLI video output prefers .mp4; keep image extension for stills.
        ext_l = ext.lower()
        if ext_l in VIDEO_FORMATS and ext_l not in (".mp4", ".png"):
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
        resolution = self._target_resolution(input_path, opts)
        try:
            batch_size = int(opts.get("batch_size") or 5)
        except (TypeError, ValueError):
            batch_size = 5
        if batch_size < 1:
            batch_size = 1

        cuda_device = str(opts.get("cuda_device") or self.cuda_device or "0").strip() or "0"
        dit_model = str(opts.get("dit_model") or self.dit_model or DEFAULT_DIT_MODEL).strip() or DEFAULT_DIT_MODEL
        keep_vram = opts.get("keep_vram")
        if keep_vram is None:
            keep_vram = bool(getattr(self, "keep_vram", False))
        else:
            keep_vram = bool(keep_vram)

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

        if keep_vram:
            return self._process_persistent(
                input_path=input_path,
                output_path=output_path,
                python_exe=python_exe,
                cli_path=cli_path,
                cuda_device=cuda_device,
                dit_model=dit_model,
                resolution=resolution,
                batch_size=batch_size,
                video_backend=video_backend,
                progress_cb=progress_cb,
                should_stop=should_stop,
            )

        cmd = [
            python_exe,
            str(cli_path),
            os.path.abspath(input_path),
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
        ]
        if video_backend == "ffmpeg":
            cmd.extend(["--video_backend", "ffmpeg"])

        # Prefer ffmpeg backend when available (bundled with Vibe Player).
        env = os.environ.copy()
        # Runner prints emoji status lines; Windows cp1250 consoles crash without UTF-8.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

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
                    low = text.lower()
                    phase = "load" if any(
                        t in low for t in ("load", "download", "weight", "model")
                    ) and "upscal" not in low else "upscale"
                    # Once inference-ish lines appear, force upscale phase.
                    if any(t in low for t in ("processing", "frame", "encode", "decode", "generation", "fps")):
                        phase = "upscale"
                    progress_cb(0.5, text[:120], phase)

        code = proc.wait()
        if code != 0:
            tail = "\n".join(log_lines[-12:]) if log_lines else f"exit code {code}"
            return {
                "ok": False,
                "output_path": None,
                "error": "runner_failed",
                "message": f"SeedVR 2 failed:\n{tail}",
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

        result = host.upscale(
            input_path=os.path.abspath(input_path),
            output_path=os.path.abspath(output_path),
            model_dir=str(self.weights_dir),
            dit_model=dit_model,
            cuda_device=cuda_device,
            options={
                "resolution": resolution,
                "batch_size": batch_size,
                "video_backend": video_backend,
            },
            progress_cb=progress_cb,
            should_stop=should_stop,
        )
        if result.get("ok") and result.get("output_path"):
            if progress_cb:
                progress_cb(1.0, "SeedVR 2: done (model kept in VRAM)", "upscale")
            return {
                "ok": True,
                "output_path": result["output_path"],
                "error": None,
                "message": None,
            }
        return {
            "ok": False,
            "output_path": None,
            "error": result.get("error") or "runner_failed",
            "message": result.get("message") or "Persistent worker failed",
        }


plugin_class = SeedVR2UpscalePlugin
