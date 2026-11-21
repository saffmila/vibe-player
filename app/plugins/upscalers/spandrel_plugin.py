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
from spandrel_worker_host import get_spandrel_worker_host


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
            "tile_size": 0,  # used only when auto_tile is off
            "auto_tile": True,
            "tile_cap": 0,  # 0 = no ceiling; positive = max tile edge
            "overlap": 32,
            "cuda_device": "0",
            "target_scale": None,  # None = model native scale
            "suffix": "",  # empty → model filename stem (see model_stem_suffix)
            "output_format": "png",
            "unload_after": True,
        }

    def suggested_output_path(self, input_path: str, options: dict[str, Any] | None = None) -> str:
        opts = {**self.default_options(), **(options or {})}
        explicit = opts.get("output_path")
        if isinstance(explicit, str) and explicit.strip():
            return os.path.abspath(explicit.strip())
        from spandrel_favorites import model_stem_suffix

        raw_suffix = opts.get("suffix")
        if raw_suffix is None or str(raw_suffix).strip() == "":
            suffix = model_stem_suffix(opts.get("model"))
        else:
            suffix = str(raw_suffix)
        out_dir = opts.get("output_dir") or os.path.dirname(input_path)
        stem, _ext = os.path.splitext(os.path.basename(input_path))
        fmt = str(opts.get("output_format") or "png").lower().lstrip(".")
        if fmt in ("jpg", "jpeg"):
            ext = ".jpg"
        elif fmt in ("tif", "tiff"):
            ext = ".tiff"
        elif fmt == "webp":
            ext = ".webp"
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
        # Preserve explicit 0 (= tiling off). ``x or 512`` wrongly forced tiles.
        raw_tile = opts.get("tile_size", 512)
        try:
            tile_size = int(raw_tile) if raw_tile is not None else 512
        except (TypeError, ValueError):
            tile_size = 512
        raw_overlap = opts.get("overlap", 32)
        try:
            overlap = int(raw_overlap) if raw_overlap is not None else 32
        except (TypeError, ValueError):
            overlap = 32
        auto_tile = bool(opts.get("auto_tile", True))
        try:
            tile_cap = int(opts.get("tile_cap") or 0)
        except (TypeError, ValueError):
            tile_cap = 0
        # Child process so Cancel / app exit can hard-kill CUDA immediately.
        job = {
            "input_path": input_path,
            "output_path": output_path,
            "model": str(resolved),
            "tile_size": tile_size,
            "auto_tile": auto_tile,
            "tile_cap": tile_cap,
            "overlap": overlap,
            "cuda_device": opts.get("cuda_device"),
            "target_scale": opts.get("target_scale"),
            "unload_after": bool(opts.get("unload_after", True)),
        }
        return get_spandrel_worker_host().run_job(
            job,
            progress_cb=progress_cb,
            should_stop=should_stop,
        )


plugin_class = SpandrelUpscalePlugin
