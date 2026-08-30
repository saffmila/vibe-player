"""
birefnet_plugin.py — BiRefNet background removal (images, PyTorch GPU pack).
"""

from __future__ import annotations

import os
from typing import Any, Callable

from birefnet_config import (
    GPU_PACK_MISSING_MESSAGE,
    supports_image,
    runtime_status,
    weights_status,
)
from birefnet_pipeline import remove_background_from_file, unload_model
from plugins.processing_base import UpscaleBackend


class BirefnetRemoveBgPlugin(UpscaleBackend):
    """Offline background removal for still images via BiRefNet."""

    id = "birefnet"
    name = "Remove Background"
    lazy_load = True

    def supports(self, file_path: str) -> bool:
        return supports_image(file_path)

    def runtime_status(self, *, deep: bool = False) -> dict[str, Any]:
        return runtime_status(deep=deep)

    def weights_status(self, *, model_variant: str | None = None) -> dict[str, Any]:
        return weights_status(model_variant=model_variant)

    def default_options(self) -> dict[str, Any]:
        return {
            "suffix": "_nobg",
            "input_size": 1024,
            "bg_mode": "transparent",
            "bg_color": "#FFFFFF",
            "cuda_device": "0",
            "model_variant": "general",
            "mask_threshold": 0,
            "mask_feather": 0,
            "mask_morph": 0,
        }

    def suggested_output_path(self, input_path: str, options: dict[str, Any] | None = None) -> str:
        opts = {**self.default_options(), **(options or {})}
        explicit = opts.get("output_path")
        if isinstance(explicit, str) and explicit.strip():
            return os.path.abspath(explicit.strip())
        suffix = str(opts.get("suffix") or "_nobg")
        out_dir = opts.get("output_dir") or os.path.dirname(input_path)
        stem, _ext = os.path.splitext(os.path.basename(input_path))
        return os.path.join(out_dir, f"{stem}{suffix}.png")

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
                "message": "BiRefNet supports common image formats only.",
            }

        status = self.runtime_status(deep=True)
        if not status.get("ready"):
            return {
                "ok": False,
                "output_path": None,
                "error": status.get("error") or "gpu_pack_missing",
                "message": status.get("message") or GPU_PACK_MISSING_MESSAGE,
            }

        output_path = self.suggested_output_path(input_path, opts)
        result = remove_background_from_file(
            input_path,
            output_path,
            bg_mode=str(opts.get("bg_mode") or "transparent"),
            bg_color=opts.get("bg_color"),
            cuda_device=opts.get("cuda_device"),
            model_variant=str(opts.get("model_variant") or "general"),
            mask_threshold=int(opts.get("mask_threshold") or 0),
            mask_feather=int(opts.get("mask_feather") or 0),
            mask_morph=int(opts.get("mask_morph") or 0),
            progress_cb=progress_cb,
            should_stop=should_stop,
        )
        if opts.get("unload_after"):
            unload_model()
        return result


plugin_class = BirefnetRemoveBgPlugin
