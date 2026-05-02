"""
clip_yolo_plugin.py — CLIP/YOLO tagging plugin.

Runs the tagging pipeline (generate_tags_ilektra) with TaggingSettings from JSON.
"""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "tag_engine")))

from plugins.plugin_base import TaggingPlugin
from generate_tags_ilektra import run_tagging_pipeline
from app_settings import TaggingSettings


GPU_PACK_MISSING_MESSAGE = "Není nainstalovaný Autotag GPU Pack"


def _is_gpu_pack_missing_error(exc: Exception) -> bool:
    """Return True when exception indicates missing optional AI/GPU runtime."""
    text = str(exc).lower()
    markers = (
        "no module named 'torch",
        'no module named "torch',
        "no module named 'ultralytics",
        'no module named "ultralytics',
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


class ClipYoloPlugin(TaggingPlugin):
    def run(self, file_path, metadata=None):
        # 1) Instantiate a fresh TaggingSettings
        settings_obj = TaggingSettings()
        # 2) Load from app/settings_autotag.json (or legacy file in repo root)
        settings_obj.load_from_json()
        #    (load uses resolve_settings_load_path; save uses get_settings_path -> app/)

        print(f"[ClipYoloPlugin] TAGGING_ENGINE = {settings_obj.tagging_engine}")

        try:
            tags = run_tagging_pipeline(file_path, settings_obj)
        except Exception as e:
            if _is_gpu_pack_missing_error(e):
                print(f"[ClipYoloPlugin] {GPU_PACK_MISSING_MESSAGE} ({e})")
                return {
                    "tags": [],
                    "error": "gpu_pack_missing",
                    "message": GPU_PACK_MISSING_MESSAGE,
                }
            print(f"[ClipYoloPlugin] Tagging failed for {file_path}: {e}")
            return {"tags": [], "error": None}

        return {"tags": tags, "error": None}

plugin_class = ClipYoloPlugin
