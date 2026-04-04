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
            print(f"[ClipYoloPlugin] Tagging failed for {file_path}: {e}")
            tags = []

        return {"tags": tags}

plugin_class = ClipYoloPlugin
