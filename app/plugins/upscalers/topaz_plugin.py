"""
topaz_plugin.py — Optional Topaz Photo AI upscale via licensed tpai.exe CLI.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from plugins.processing_base import UpscaleBackend
from topaz_config import (
    DEFAULT_TOPAZ_MODEL,
    DEFAULT_TOPAZ_SCALE,
    NOT_INSTALLED_MESSAGE,
    runtime_status,
    supports_image,
    weights_status,
)
from topaz_pipeline import upscale_image_file


class TopazPhotoAiPlugin(UpscaleBackend):
    """Offline image upscale through the user's Topaz Photo AI install."""

    id = "topaz"
    name = "Topaz Photo AI"
    lazy_load = True

    def supports(self, file_path: str) -> bool:
        return supports_image(file_path)

    def runtime_status(self, *, exe_override: str | None = None) -> dict[str, Any]:
        return runtime_status(exe_override=exe_override)

    def weights_status(self, *, exe_override: str | None = None) -> dict[str, Any]:
        return weights_status(exe_override=exe_override)

    def default_options(self) -> dict[str, Any]:
        return {
            "scale": DEFAULT_TOPAZ_SCALE,
            "model": DEFAULT_TOPAZ_MODEL,
            "suffix": "_topaz",
            "output_format": "png",
            "quality": 95,
            "exe": "",
        }

    def suggested_output_path(self, input_path: str, options: dict[str, Any] | None = None) -> str:
        opts = {**self.default_options(), **(options or {})}
        explicit = opts.get("output_path")
        if isinstance(explicit, str) and explicit.strip():
            return os.path.abspath(explicit.strip())
        suffix = str(opts.get("suffix") or "_topaz")
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
                "message": "Topaz Photo AI supports common image formats only.",
            }

        exe = str(opts.get("exe") or "").strip() or None
        status = self.runtime_status(exe_override=exe)
        if not status.get("ready"):
            return {
                "ok": False,
                "output_path": None,
                "error": status.get("error") or "topaz_missing",
                "message": status.get("message") or NOT_INSTALLED_MESSAGE,
            }

        output_path = self.suggested_output_path(input_path, opts)
        return upscale_image_file(
            input_path,
            output_path,
            scale=int(opts.get("scale") or DEFAULT_TOPAZ_SCALE),
            model=str(opts.get("model") or DEFAULT_TOPAZ_MODEL),
            exe_override=exe,
            fmt=str(opts.get("output_format") or "png"),
            quality=int(opts.get("quality") or 95),
            progress_cb=progress_cb,
            should_stop=should_stop,
        )


plugin_class = TopazPhotoAiPlugin
