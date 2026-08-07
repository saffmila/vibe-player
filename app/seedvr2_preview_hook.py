"""
seedvr2_preview_hook.py — Preview helpers for SeedVR2 upscale UI.

- Source preview: 1:1 center crop of the current input (image / first video frame)
- Chunk preview: optional live update from each finished video chunk (upscaled RGB)
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Callable, Tuple

from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS

# Match FileOpProgressDialog preview label size (pixel-perfect crop).
PREVIEW_CROP_W = 480
PREVIEW_CROP_H = 270


def center_crop_box(
    width: int,
    height: int,
    crop_w: int = PREVIEW_CROP_W,
    crop_h: int = PREVIEW_CROP_H,
) -> Tuple[int, int, int, int]:
    """Return PIL-style (left, top, right, bottom) for a 1:1 center crop."""
    cw = max(1, int(crop_w))
    ch = max(1, int(crop_h))
    if width <= cw and height <= ch:
        return 0, 0, width, height
    x0 = max(0, (width - cw) // 2)
    y0 = max(0, (height - ch) // 2)
    return x0, y0, min(width, x0 + cw), min(height, y0 + ch)


def _save_rgb_crop(rgb, preview_path: str, crop_w: int, crop_h: int) -> bool:
    box = center_crop_box(rgb.width, rgb.height, crop_w, crop_h)
    crop = rgb.crop(box)
    os.makedirs(os.path.dirname(preview_path) or ".", exist_ok=True)
    tmp = f"{preview_path}.tmp.jpg"
    crop.save(tmp, format="JPEG", quality=92, optimize=True)
    try:
        os.replace(tmp, preview_path)
    except OSError:
        if os.path.isfile(preview_path):
            os.remove(preview_path)
        os.rename(tmp, preview_path)
    return True


def write_source_preview(
    input_path: str,
    preview_path: str,
    crop_w: int = PREVIEW_CROP_W,
    crop_h: int = PREVIEW_CROP_H,
) -> bool:
    """
    Write a 1:1 center-crop JPEG of ``input_path`` to ``preview_path``.

    Images via PIL; videos via a single FFmpeg frame grab. Returns True on success.
    """
    if not input_path or not preview_path or not os.path.isfile(input_path):
        return False
    ext = os.path.splitext(input_path)[1].lower()
    try:
        from PIL import Image

        if ext in IMAGE_FORMATS:
            with Image.open(input_path) as im:
                return _save_rgb_crop(im.convert("RGB"), preview_path, crop_w, crop_h)

        if ext in VIDEO_FORMATS:
            from file_operations import get_ffmpeg_path

            ffmpeg = get_ffmpeg_path()
            if not ffmpeg or not os.path.isfile(ffmpeg):
                return False
            tmp_frame = f"{preview_path}.frame.jpg"
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                "0",
                "-i",
                input_path,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                tmp_frame,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0 or not os.path.isfile(tmp_frame):
                return False
            try:
                with Image.open(tmp_frame) as im:
                    return _save_rgb_crop(im.convert("RGB"), preview_path, crop_w, crop_h)
            finally:
                try:
                    os.remove(tmp_frame)
                except OSError:
                    pass

        return False
    except Exception as exc:
        logging.debug("[SeedVR2 Preview] source preview failed: %s", exc)
        try:
            tmp = f"{preview_path}.tmp.jpg"
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def write_tensor_chunk_preview(
    frames_tensor: Any,
    preview_path: str,
    *,
    crop_w: int = PREVIEW_CROP_W,
    crop_h: int = PREVIEW_CROP_H,
    frame_index: int = -1,
) -> bool:
    """
    Write a 1:1 center-crop JPEG from an upscaled chunk tensor ``[T,H,W,C]``.

    Accepts float tensors in ~[0,1] or ~[-1,1], or uint8. Uses the last frame
    by default (``frame_index=-1``) so the preview tracks the newest output.
    """
    if frames_tensor is None or not preview_path:
        return False
    try:
        import numpy as np
        from PIL import Image

        arr = frames_tensor
        if hasattr(arr, "detach"):
            arr = arr.detach()
        if hasattr(arr, "float"):
            try:
                arr = arr.float()
            except Exception:
                pass
        if hasattr(arr, "cpu"):
            arr = arr.cpu()
        if hasattr(arr, "numpy"):
            arr = arr.numpy()
        arr = np.asarray(arr)
        # Allow single-frame [H,W,C] as well as [T,H,W,C].
        if arr.ndim == 3:
            arr = arr[None, ...]
        if arr.ndim != 4 or arr.shape[0] < 1:
            return False
        # [T,C,H,W] → [T,H,W,C]
        if arr.shape[1] in (1, 3, 4) and arr.shape[-1] not in (1, 3, 4):
            arr = np.transpose(arr, (0, 2, 3, 1))
        idx = frame_index if frame_index >= 0 else arr.shape[0] - 1
        idx = max(0, min(arr.shape[0] - 1, int(idx)))
        frame = arr[idx]
        if frame.shape[-1] > 3:
            frame = frame[..., :3]
        if frame.dtype != np.uint8:
            fmin = float(np.nanmin(frame))
            if fmin < -0.01:
                frame = (frame + 1.0) * 0.5
            frame = np.clip(frame * 255.0, 0, 255).astype(np.uint8)
        im = Image.fromarray(frame, mode="RGB")
        return _save_rgb_crop(im, preview_path, crop_w, crop_h)
    except Exception as exc:
        logging.debug("[SeedVR2 Preview] chunk preview failed: %s", exc)
        return False


def install_chunk_preview_hooks(
    cli_module: Any,
    preview_path: str,
    *,
    on_preview: Callable[[int], None] | None = None,
) -> Callable[[], None]:
    """
    Monkeypatch SeedVR save + decode paths so previews refresh during video work.

    - After each finished stream chunk (``save_frames_to_*``)
    - After each VAE decode batch (``optimized_sample_to_image_format``) — mid-chunk

    Returns a restore callable.
    """
    path = (preview_path or "").strip()
    if not path or cli_module is None:
        return lambda: None

    orig_video = getattr(cli_module, "save_frames_to_video", None)
    orig_image = getattr(cli_module, "save_frames_to_image", None)
    state = {"n": 0, "decode_n": 0}

    def _emit_preview(label: str, n: int, frames_tensor: Any) -> None:
        ok = write_tensor_chunk_preview(frames_tensor, path)
        if not ok:
            return
        logging.info("[SeedVR2 Preview] %s preview updated (#%s)", label, n)
        try:
            print(f"[seedvr2] Chunk preview updated ({label} {n})", flush=True)
        except Exception:
            pass
        if on_preview is not None:
            try:
                on_preview(n)
            except Exception:
                pass

    def _after_chunk(frames_tensor: Any) -> None:
        state["n"] += 1
        _emit_preview("chunk", state["n"], frames_tensor)

    def wrap_video(frames_tensor, *args, **kwargs):
        out = orig_video(frames_tensor, *args, **kwargs)
        try:
            _after_chunk(frames_tensor)
        except Exception as exc:
            logging.debug("[SeedVR2 Preview] video hook: %s", exc)
        return out

    def wrap_image(frames_tensor, *args, **kwargs):
        out = orig_image(frames_tensor, *args, **kwargs)
        try:
            _after_chunk(frames_tensor)
        except Exception as exc:
            logging.debug("[SeedVR2 Preview] image hook: %s", exc)
        return out

    restore_bits: list[Callable[[], None]] = []

    if callable(orig_video):
        cli_module.save_frames_to_video = wrap_video  # type: ignore[method-assign]
        restore_bits.append(
            lambda: setattr(cli_module, "save_frames_to_video", orig_video)
        )
    if callable(orig_image):
        cli_module.save_frames_to_image = wrap_image  # type: ignore[method-assign]
        restore_bits.append(
            lambda: setattr(cli_module, "save_frames_to_image", orig_image)
        )

    # Mid-chunk: after each Phase-3 decode batch converts to image layout.
    try:
        import src.optimization.performance as perf_mod
        import src.core.generation_phases as phases_mod

        orig_fmt = getattr(perf_mod, "optimized_sample_to_image_format", None)
        if callable(orig_fmt):

            def wrap_fmt(sample, *args, **kwargs):
                out = orig_fmt(sample, *args, **kwargs)
                try:
                    state["decode_n"] += 1
                    # Throttle UI spam slightly: always first, then every batch
                    # (decode batches are already coarse — typically seconds apart).
                    _emit_preview("decode", state["decode_n"], out)
                except Exception as exc:
                    logging.debug("[SeedVR2 Preview] decode hook: %s", exc)
                return out

            perf_mod.optimized_sample_to_image_format = wrap_fmt  # type: ignore[method-assign]
            restore_bits.append(
                lambda o=orig_fmt: setattr(
                    perf_mod, "optimized_sample_to_image_format", o
                )
            )
            if getattr(phases_mod, "optimized_sample_to_image_format", None) is orig_fmt:
                phases_mod.optimized_sample_to_image_format = wrap_fmt  # type: ignore[method-assign]
                restore_bits.append(
                    lambda o=orig_fmt: setattr(
                        phases_mod, "optimized_sample_to_image_format", o
                    )
                )
            # generation_utils may re-export the same symbol
            try:
                import src.core.generation_utils as utils_mod

                if getattr(utils_mod, "optimized_sample_to_image_format", None) is orig_fmt:
                    utils_mod.optimized_sample_to_image_format = wrap_fmt  # type: ignore[attr-defined]
                    restore_bits.append(
                        lambda o=orig_fmt: setattr(
                            utils_mod, "optimized_sample_to_image_format", o
                        )
                    )
            except Exception:
                pass
    except Exception as exc:
        logging.debug("[SeedVR2 Preview] decode-path hook unavailable: %s", exc)

    def restore() -> None:
        for fn in reversed(restore_bits):
            try:
                fn()
            except Exception:
                pass

    return restore
