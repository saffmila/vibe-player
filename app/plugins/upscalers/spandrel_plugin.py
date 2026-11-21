"""
spandrel_plugin.py — Single-pass Spandrel upscalers (RealPLKSR, SPAN, DAT, …).
"""

from __future__ import annotations

import os
from typing import Any, Callable

from plugins.processing_base import UpscaleBackend
from spandrel_config import (
    GPU_PACK_MISSING_MESSAGE,
    SPANDREL_MISSING_MESSAGE,
    WEIGHTS_MISSING_MESSAGE,
    default_upscale_dir,
    list_spandrel_models,
    resolve_model_path,
    runtime_status,
    supports_image,
    weights_status,
)
from spandrel_pipeline import unload_model, upscale_image_file


class SpandrelUpscalePlugin(UpscaleBackend):
    """Offline single-pass image upscale via Spandrel auto-detect."""

    id = "spandrel"
    name = "Spandrel (Fast)"
    lazy_load = True

    def supports(self, file_path: str) -> bool:
        return supports_image(file_path)

    def runtime_status(self, *, deep: bool = False) -> dict[str, Any]:
        return runtime_status(deep=deep)

    def weights_status(self, *, models_dir: str | None = None) -> dict[str, Any]:
        return weights_status(models_dir=models_dir)

    def default_options(self) -> dict[str, Any]:
        models = list_spandrel_models()
        first = models[0]["filename"] if models else ""
        return {
            "model": first,
            "models_dir": str(default_upscale_dir()),
            "tile_size": 0,  # 0 = off (full-frame pass)
            "overlap": 32,
            "cuda_device": "0",
            "target_scale": None,  # None = model native scale
            "suffix": "_spandrel",
            "output_format": "png",
            "unload_after": True,
        }

    def suggested_output_path(self, input_path: str, options: dict[str, Any] | None = None) -> str:
        opts = {**self.default_options(), **(options or {})}
        explicit = opts.get("output_path")
        if isinstance(explicit, str) and explicit.strip():
            return os.path.abspath(explicit.strip())
        suffix = str(opts.get("suffix") or "_spandrel")
        out_dir = opts.get("output_dir") or os.path.dirname(input_path)
        stem, _ext = os.path.splitext(os.path.basename(input_path))
        fmt = str(opts.get("output_format") or "png").lower().lstrip(".")
        if fmt in ("jpg", "jpeg"):
            ext = ".jpg"
        elif fmt in ("tif", "tiff"):
            ext = ".tiff"
        else:
            ext = ".png"
        return os.path.join(out_dir, f"{stem}{suffix}{ext}")

    def process(
        self,
        input_path: str,
        options: dict[str, Any] | None = None,
        progress_cb: Callable[[float, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        opts = {**self.default_options(), **(options or {})}
        if not self.supports(input_path):
            return {
                "ok": False,
                "output_path": None,
                "error": "unsupported",
                "message": "Spandrel supports common image formats only.",
            }

        status = self.runtime_status(deep=True)
        # Allow CPU fallback if torch is present but CUDA is not.
        if not status.get("ready") and not status.get("cpu_fallback"):
            err = status.get("error") or "runtime_error"
            msg = status.get("message")
            if err == "spandrel_missing":
                msg = msg or SPANDREL_MISSING_MESSAGE
            elif err == "gpu_pack_missing":
                msg = msg or GPU_PACK_MISSING_MESSAGE
            return {
                "ok": False,
                "output_path": None,
                "error": err,
                "message": msg or "Spandrel runtime is not ready.",
            }

        model = str(opts.get("model") or "").strip()
        models_dir = opts.get("models_dir")
        resolved = resolve_model_path(model, models_dir=models_dir)
        if resolved is None:
            return {
                "ok": False,
                "output_path": None,
                "error": "weights_missing",
                "message": WEIGHTS_MISSING_MESSAGE.format(
                    path=str(models_dir or default_upscale_dir())
                ),
            }

        output_path = self.suggested_output_path(input_path, opts)
        result = upscale_image_file(
            input_path,
            output_path,
            model=str(resolved),
            tile_size=int(opts.get("tile_size") or 512),
            overlap=int(opts.get("overlap") or 32),
            cuda_device=opts.get("cuda_device"),
            target_scale=opts.get("target_scale"),
            unload_after=bool(opts.get("unload_after", True)),
            progress_cb=progress_cb,
            should_stop=should_stop,
        )
        if opts.get("unload_after"):
            unload_model()
        return result


plugin_class = SpandrelUpscalePlugin
