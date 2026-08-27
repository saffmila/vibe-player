"""
processing_base.py — Base class for offline media processing plugins.

UpscaleBackend is the contract for SeedVR2, DAT, and future upscalers.
Heavy runtimes (torch, model code) stay optional; weights are user-downloaded.
"""

from __future__ import annotations

from typing import Any, Callable


class UpscaleBackend:
    """Offline upscale / restore backend discovered by PluginManager."""

    # Stable id used in menus and settings (e.g. "seedvr2").
    id: str = "upscale"
    # Human-readable label for context menus.
    name: str = "Upscaler"
    # When True, PluginManager keeps the class until first use.
    lazy_load: bool = True

    def __init__(self, settings: dict | None = None) -> None:
        self.settings = settings or {}

    def supports(self, file_path: str) -> bool:
        """Return True if this backend can process the given file path."""
        raise NotImplementedError

    def runtime_status(self) -> dict[str, Any]:
        """
        Check optional GPU/runtime pack availability.

        Returns dict with at least:
          ready: bool
          error: optional str code (e.g. "gpu_pack_missing")
          message: optional user-facing str
        """
        return {"ready": True, "error": None, "message": None}

    def weights_status(self) -> dict[str, Any]:
        """
        Check whether required model weights are present on disk.

        Returns dict with at least:
          ready: bool
          path: expected weights directory
          download_url: where the user can fetch weights
          message: optional user-facing str
        """
        return {
            "ready": False,
            "path": None,
            "download_url": None,
            "message": "Weights not configured.",
        }

    def default_options(self) -> dict[str, Any]:
        """Default process options (scale, output naming, etc.)."""
        return {"scale": 2, "suffix": f"_{self.id}"}

    def suggested_output_path(self, input_path: str, options: dict[str, Any] | None = None) -> str:
        """Default destination path for conflict checks / batch jobs."""
        import os

        opts = {**(self.default_options() or {}), **(options or {})}
        explicit = opts.get("output_path")
        if isinstance(explicit, str) and explicit.strip():
            return os.path.abspath(explicit.strip())
        suffix = str(opts.get("suffix") or f"_{self.id}")
        out_dir = opts.get("output_dir") or os.path.dirname(input_path)
        stem, ext = os.path.splitext(os.path.basename(input_path))
        return os.path.join(out_dir, f"{stem}{suffix}{ext}")

    def process(
        self,
        input_path: str,
        options: dict[str, Any] | None = None,
        progress_cb: Callable[[float, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """
        Run offline upscale on one file.

        Returns dict with:
          ok: bool
          output_path: str | None
          error: optional code (gpu_pack_missing, weights_missing, aborted, …)
          message: optional user-facing str
        """
        raise NotImplementedError("Upscale backend must implement process().")
