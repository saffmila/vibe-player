"""
rife_plugin.py — Optional RIFE frame-interpolation backend (rife-ncnn-vulkan).

Ships as an optional pack under tools/rife/ — not part of the base install.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from plugins.processing_base import UpscaleBackend
from rife_config import (
    PACK_MISSING_MESSAGE,
    RIFE_PROJECT_URL,
    list_rife_models,
    runtime_status,
)
from rife_pipeline import interpolate_video
RIFE_VIDEO_FORMATS = frozenset(
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


class RifeInterpolatePlugin(UpscaleBackend):
    """Offline RIFE 2×/4× interpolation via the optional ncnn-vulkan pack."""

    id = "rife"
    name = "RIFE Interpolate"
    lazy_load = True

    def supports(self, file_path: str) -> bool:
        ext = os.path.splitext(file_path or "")[1].lower()
        return ext in RIFE_VIDEO_FORMATS and os.path.isfile(file_path or "")

    def runtime_status(self) -> dict[str, Any]:
        return runtime_status()

    def weights_status(self) -> dict[str, Any]:
        status = runtime_status()
        models = status.get("models") or list_rife_models()
        ready = bool(status.get("ready") and models)
        return {
            "ready": ready,
            "path": status.get("dir"),
            "download_url": RIFE_PROJECT_URL + "/releases",
            "message": None if ready else (status.get("message") or PACK_MISSING_MESSAGE),
            "models": models,
        }

    def default_options(self) -> dict[str, Any]:
        return {
            "multiplier": 2,
            "mode": "fps",  # fps | slowmo
            "suffix": "_rife",
            "include_audio": True,
            "uhd": None,
            "model": None,
        }

    def suggested_output_path(self, input_path: str, options: dict[str, Any] | None = None) -> str:
        opts = {**self.default_options(), **(options or {})}
        explicit = opts.get("output_path")
        if isinstance(explicit, str) and explicit.strip():
            return os.path.abspath(explicit.strip())
        out_dir = opts.get("output_dir") or os.path.dirname(input_path)
        stem, ext = os.path.splitext(os.path.basename(input_path))
        mult = int(opts.get("multiplier") or 2)
        if mult not in (2, 4):
            mult = 2
        mode = str(opts.get("mode") or "fps")
        suffix = str(opts.get("suffix") or "_rife")
        tag = f"{suffix}{mult}x" if mode == "fps" else f"{suffix}_slowmo{mult}x"
        return os.path.join(out_dir, f"{stem}{tag}{ext or '.mp4'}")

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
                "message": "RIFE supports common video formats only (mp4, mkv, mov, …).",
            }

        status = self.runtime_status()
        if not status.get("ready"):
            return {
                "ok": False,
                "output_path": None,
                "error": status.get("error") or "rife_pack_missing",
                "message": status.get("message") or PACK_MISSING_MESSAGE,
            }

        output_path = self.suggested_output_path(input_path, opts)

        encode_settings = opts.get("encode_settings") or {
            "ext": os.path.splitext(output_path)[1] or ".mp4",
            "video_quality": opts.get("video_quality") or "High",
            "audio_bitrate": opts.get("audio_bitrate") or "192k",
            "keep_size": True,
        }

        return interpolate_video(
            input_path,
            output_path,
            multiplier=int(opts.get("multiplier") or 2),
            mode=str(opts.get("mode") or "fps"),
            start=opts.get("start"),
            end=opts.get("end"),
            include_audio=bool(opts.get("include_audio", True)),
            model_name=opts.get("model"),
            uhd=opts.get("uhd"),
            encode_settings=encode_settings,
            progress_cb=progress_cb,
            should_stop=should_stop,
        )


plugin_class = RifeInterpolatePlugin
