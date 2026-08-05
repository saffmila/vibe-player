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
    resolve_prescale_long_edge,
    resolve_runner_python,
)
from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS

SEEDVR2_PROJECT_URL = "https://github.com/ByteDance-Seed/SeedVR"
SEEDVR2_WEIGHTS_URL = "https://huggingface.co/models?other=seedvr"
SEEDVR2_RUNNER_URL = COMFY_REPO_URL

GPU_PACK_MISSING_MESSAGE = "Autotag GPU Pack is not installed (required for SeedVR 2)."
WEIGHTS_MISSING_MESSAGE = (
    "SeedVR 2 weights not found. Download them from Hugging Face "
    f"({SEEDVR2_WEIGHTS_URL}) and place them in the models folder."
)
RUNNER_MISSING_MESSAGE = (
    "SeedVR 2 runner not configured. Click “Install runner…” to download the "
    "ComfyUI-SeedVR2 CLI checkout automatically, or point Runner folder at a "
    f"directory that contains inference_cli.py ({SEEDVR2_RUNNER_URL})."
)
OOM_HELP_MESSAGE = (
    "GPU out of memory (VRAM) during SeedVR 2.\n\n"
    "Try:\n"
    "• Enable “Low VRAM (tiled VAE)” in the Upscale dialog\n"
    "• Use Prescale → Optimal / Aggressive (smaller input)\n"
    "• Lower Scale (2× instead of 4×)\n"
    "• Pick a freer GPU, or turn off Keep-in-VRAM and retry\n"
    "• Close other GPU apps (training, games, browsers)"
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
        )
    )


def _format_runner_failure(tail: str) -> tuple[str, str]:
    """Return (error_code, user_message) for a failed runner log tail."""
    if _is_oom_text(tail):
        return "oom", OOM_HELP_MESSAGE
    short = (tail or "").strip()
    if len(short) > 900:
        short = short[-900:]
    return "runner_failed", f"SeedVR 2 failed:\n{short}"


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
        # Video upscale is temporarily disabled (long jobs / freeze risk).
        ext = os.path.splitext(file_path)[1].lower()
        return ext in IMAGE_FORMATS

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
            "vae_tiled": True,  # tiled VAE encode/decode — much lower VRAM
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
        elif ext_l in VIDEO_FORMATS and ext_l not in (".mp4", ".png"):
            # Video path kept for a future re-enable; force mp4.
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

        vae_tiled = opts.get("vae_tiled")
        if vae_tiled is None:
            vae_tiled = True
        else:
            vae_tiled = bool(vae_tiled)
        # High output short-side → force tiling even if user left it off.
        if resolution >= 1440:
            vae_tiled = True

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
                input_path=work_path,
                output_path=output_path,
                python_exe=python_exe,
                cli_path=cli_path,
                cuda_device=cuda_device,
                dit_model=dit_model,
                resolution=resolution,
                batch_size=batch_size,
                video_backend=video_backend,
                vae_tiled=vae_tiled,
                progress_cb=progress_cb,
                should_stop=should_stop,
            )

        cmd = [
            python_exe,
            str(cli_path),
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
        ]
        if video_backend == "ffmpeg":
            cmd.extend(["--video_backend", "ffmpeg"])
        if vae_tiled:
            cmd.extend(["--vae_encode_tiled", "--vae_decode_tiled"])
            # Slightly smaller tiles help stubborn OOMs on large stills.
            if resolution >= 1440:
                cmd.extend(
                    [
                        "--vae_encode_tile_size",
                        "768",
                        "--vae_decode_tile_size",
                        "768",
                    ]
                )

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
        }
        if vae_tiled and resolution >= 1440:
            job_opts["vae_encode_tile_size"] = 768
            job_opts["vae_decode_tile_size"] = 768

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
            if progress_cb:
                progress_cb(1.0, "SeedVR 2: done (model kept in VRAM)", "upscale")
            return {
                "ok": True,
                "output_path": result["output_path"],
                "error": None,
                "message": None,
            }
        err = result.get("error") or "runner_failed"
        msg = result.get("message") or "Persistent worker failed"
        if _is_oom_text(msg):
            err, msg = "oom", OOM_HELP_MESSAGE
        return {
            "ok": False,
            "output_path": None,
            "error": err,
            "message": msg,
        }


plugin_class = SeedVR2UpscalePlugin
