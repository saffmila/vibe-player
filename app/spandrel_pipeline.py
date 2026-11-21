"""
spandrel_pipeline.py — Lightweight single-pass SR via Spandrel ModelLoader.

Auto-detects architecture (RealPLKSR, SPAN, DRCT, DAT, HAT, ESRGAN, …)
from .pth / .safetensors without Basicsr / Real-ESRGAN packages.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from PIL import Image

from spandrel_config import resolve_model_path, supports_image

_engine: Optional["ImageUpscalerEngine"] = None


def get_engine() -> "ImageUpscalerEngine":
    global _engine
    if _engine is None:
        _engine = ImageUpscalerEngine()
    return _engine


def unload_model() -> None:
    global _engine
    if _engine is not None:
        _engine.unload_model()


class ImageUpscalerEngine:
    """Lazy-load Spandrel ImageModelDescriptor and run tiled inference."""

    def __init__(self, device: Optional[str] = None) -> None:
        self.device = device or self._default_device()
        self.descriptor = None
        self.loaded_model_path: Optional[str] = None
        self.scale: int = 1

    @staticmethod
    def _default_device() -> str:
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def _resolve_device(self, cuda_device: str | int | None) -> str:
        try:
            import torch

            if not torch.cuda.is_available():
                return "cpu"
        except Exception:
            return "cpu"
        if cuda_device is None or str(cuda_device).strip() == "":
            return "cuda:0"
        text = str(cuda_device).strip()
        if text.startswith("cuda"):
            return text
        try:
            return f"cuda:{int(text)}"
        except ValueError:
            return "cuda:0"

    def load_model(self, model_path: str, *, cuda_device: str | int | None = None) -> bool:
        try:
            from spandrel import ImageModelDescriptor, ModelLoader
        except ImportError:
            logging.error("[Spandrel] 'spandrel' is not installed.")
            return False

        path = resolve_model_path(model_path)
        if path is None or not path.is_file():
            logging.error("[Spandrel] Model not found: %s", model_path)
            return False
        abs_path = str(path.resolve())
        target = self._resolve_device(cuda_device)

        if (
            self.loaded_model_path == abs_path
            and self.descriptor is not None
            and self.device == target
        ):
            return True

        self.unload_model()
        self.device = target

        try:
            import torch

            loader = ModelLoader()
            descriptor = loader.load_from_file(abs_path)
            if not isinstance(descriptor, ImageModelDescriptor):
                logging.error("[Spandrel] Unsupported model type: %s", type(descriptor))
                return False

            descriptor = descriptor.to(self.device).eval()
            # FP16 on CUDA saves VRAM; fall back to FP32 if the arch rejects it.
            if self.device.startswith("cuda"):
                try:
                    descriptor = descriptor.half()
                except Exception:
                    logging.debug("[Spandrel] FP16 cast skipped", exc_info=True)

            self.descriptor = descriptor
            self.loaded_model_path = abs_path
            self.scale = int(getattr(descriptor, "scale", 1) or 1)
            logging.info(
                "[Spandrel] Loaded %s (%dx, arch=%s) on %s",
                os.path.basename(abs_path),
                self.scale,
                getattr(getattr(descriptor, "architecture", None), "name", "?"),
                self.device,
            )
            return True
        except Exception as exc:
            logging.error("[Spandrel] Failed to load %s: %s", abs_path, exc, exc_info=True)
            self.unload_model()
            return False

    def unload_model(self) -> None:
        self.descriptor = None
        self.loaded_model_path = None
        self.scale = 1
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def upscale(
        self,
        pil_image: Image.Image,
        *,
        tile_size: int = 0,
        overlap: int = 32,
    ) -> Image.Image:
        """
        Run SR on ``pil_image``.

        ``tile_size`` <= 0 disables tiling (single forward pass — preferred on
        GPUs with enough VRAM). Positive values enable overlap tiles for large
        inputs / low VRAM.
        """
        if self.descriptor is None:
            raise RuntimeError("No Spandrel model is currently loaded.")

        import torch
        import torchvision.transforms.functional as TF

        has_alpha = pil_image.mode in ("RGBA", "LA") or (
            pil_image.mode == "P" and "transparency" in pil_image.info
        )
        alpha = None
        if has_alpha:
            rgba = pil_image.convert("RGBA")
            alpha = rgba.getchannel("A")
            rgb_img = rgba.convert("RGB")
        else:
            rgb_img = pil_image.convert("RGB")

        w, h = rgb_img.size
        scale = int(getattr(self.descriptor, "scale", self.scale) or 1)
        use_tiles = int(tile_size or 0) > 0
        tile = max(64, int(tile_size)) if use_tiles else 0
        overlap = max(0, min(int(overlap or 0), (tile // 2) if tile else 0))

        use_half = False
        try:
            use_half = (
                self.device.startswith("cuda")
                and next(self.descriptor.model.parameters()).dtype == torch.float16
            )
        except Exception:
            use_half = False

        def _run_tensor(tensor: "torch.Tensor") -> "torch.Tensor":
            if use_half:
                tensor = tensor.half()
            with torch.inference_mode():
                out = self.descriptor(tensor)
            return out.float().clamp(0.0, 1.0)

        if (not use_tiles) or (w <= tile and h <= tile):
            tensor = TF.to_tensor(rgb_img).unsqueeze(0).to(self.device)
            out = _run_tensor(tensor)
            out_img = TF.to_pil_image(out.squeeze(0).cpu())
        else:
            output_img = Image.new("RGB", (w * scale, h * scale))
            step = max(1, tile - overlap)
            for y in range(0, h, step):
                for x in range(0, w, step):
                    x1 = min(x + tile, w)
                    y1 = min(y + tile, h)
                    x0 = max(0, x1 - tile)
                    y0 = max(0, y1 - tile)

                    crop = rgb_img.crop((x0, y0, x1, y1))
                    tensor = TF.to_tensor(crop).unsqueeze(0).to(self.device)
                    out_tile = _run_tensor(tensor)
                    pil_tile = TF.to_pil_image(out_tile.squeeze(0).cpu())

                    crop_l = (x - x0) * scale if x > 0 else 0
                    crop_t = (y - y0) * scale if y > 0 else 0
                    valid_tile = pil_tile.crop(
                        (crop_l, crop_t, pil_tile.width, pil_tile.height)
                    )
                    output_img.paste(valid_tile, (x * scale, y * scale))
            out_img = output_img

        if alpha is not None:
            alpha_up = alpha.resize(out_img.size, Image.Resampling.LANCZOS)
            out_img = out_img.convert("RGBA")
            out_img.putalpha(alpha_up)
        return out_img


def _parse_target_scale(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return None
    return scale if scale > 0 else None


def upscale_image_file(
    input_path: str,
    output_path: str,
    *,
    model: str,
    tile_size: int = 0,
    overlap: int = 32,
    cuda_device: str | int | None = "0",
    target_scale: float | int | None = None,
    unload_after: bool = False,
    progress_cb: Callable[[float, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """
    Upscale one image file with Spandrel; write PNG/JPEG by extension.

    ``target_scale`` (optional): desired overall factor vs input.
    Uses model native scale, optional multi-pass when target is an integer
    multiple of the model scale, then Lanczos to the exact pixel size.
    """
    if not supports_image(input_path):
        return {
            "ok": False,
            "output_path": None,
            "error": "unsupported",
            "message": "Spandrel supports common image formats only.",
        }
    if should_stop and should_stop():
        return {
            "ok": False,
            "output_path": None,
            "error": "aborted",
            "message": "Canceled.",
        }

    if progress_cb:
        progress_cb(0.05, "Loading model…")

    # Avoid fighting SeedVR keep_vram DiT for VRAM.
    try:
        from seedvr2_worker_host import get_seedvr2_worker_host, shutdown_seedvr2_worker_host

        if get_seedvr2_worker_host().alive:
            logging.info("[Spandrel] Shutting down SeedVR worker before upscale.")
            shutdown_seedvr2_worker_host()
    except Exception:
        logging.debug("[Spandrel] SeedVR worker check skipped", exc_info=True)

    engine = get_engine()
    if not engine.load_model(model, cuda_device=cuda_device):
        return {
            "ok": False,
            "output_path": None,
            "error": "weights_missing",
            "message": f"Could not load Spandrel model: {model}",
        }

    if should_stop and should_stop():
        return {
            "ok": False,
            "output_path": None,
            "error": "aborted",
            "message": "Canceled.",
        }

    try:
        if progress_cb:
            progress_cb(0.15, "Reading image…")
        with Image.open(input_path) as im:
            im.load()
            src = im.copy()

        model_scale = max(1, int(engine.scale or 1))
        want = _parse_target_scale(target_scale)
        out = src
        achieved = 1.0
        passes = 0
        max_passes = 4

        # Run the SR network at least once; keep chaining while still below target
        # and target is a multiple of the model factor (e.g. 2× model → 4×).
        while True:
            if should_stop and should_stop():
                return {
                    "ok": False,
                    "output_path": None,
                    "error": "aborted",
                    "message": "Canceled.",
                }
            passes += 1
            if progress_cb:
                progress_cb(
                    min(0.2 + 0.55 * (passes / max(1, max_passes)), 0.85),
                    f"Upscaling pass {passes} ({model_scale}×)…",
                )
            out = engine.upscale(out, tile_size=tile_size, overlap=overlap)
            achieved *= float(model_scale)

            if want is None:
                break
            if achieved + 1e-6 >= want:
                break
            # Only chain when another full model pass stays within the target.
            if achieved * model_scale > want + 1e-6:
                break
            if passes >= max_passes:
                break

        if want is not None:
            target_w = max(1, int(round(src.size[0] * want)))
            target_h = max(1, int(round(src.size[1] * want)))
            if out.size != (target_w, target_h):
                if progress_cb:
                    progress_cb(0.88, f"Fitting to {want:g}×…")
                out = out.resize((target_w, target_h), Image.Resampling.LANCZOS)
                achieved = float(want)

        if should_stop and should_stop():
            return {
                "ok": False,
                "output_path": None,
                "error": "aborted",
                "message": "Canceled.",
            }

        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
        ext = os.path.splitext(output_path)[1].lower()
        save_kwargs: dict = {}
        if ext in (".jpg", ".jpeg"):
            if out.mode == "RGBA":
                out = out.convert("RGB")
            save_kwargs.update({"quality": 95, "optimize": True})
        elif ext == ".png":
            save_kwargs["optimize"] = True

        if progress_cb:
            progress_cb(0.9, "Saving…")
        out.save(output_path, **save_kwargs)

        if progress_cb:
            progress_cb(1.0, "Done")
        return {
            "ok": True,
            "output_path": output_path,
            "error": None,
            "message": None,
            "scale": achieved,
            "model_scale": model_scale,
        }
    except Exception as exc:
        logging.error("[Spandrel] Upscale failed: %s", exc, exc_info=True)
        return {
            "ok": False,
            "output_path": None,
            "error": "runtime_error",
            "message": str(exc),
        }
    finally:
        if unload_after:
            unload_model()
