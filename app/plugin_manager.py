"""
Plugin discovery and lazy loading for Vibe Player.

Scans ``app/plugins/``, loads lightweight plugins immediately, and defers heavy
CLIP/YOLO-style plugins until first use.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import sys
import traceback

# Import base class so issubclass() checks work
try:
    from plugins.plugin_base import TaggingPlugin
except ImportError:
    from plugin_base import TaggingPlugin


class PluginManager:
    def __init__(self, plugin_dir: str | None = None) -> None:
        if plugin_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            plugin_dir = os.path.join(base_dir, "plugins")

        self.plugin_dir = plugin_dir
        self.plugins: dict[str, object] = {}

    def load_plugins(self) -> None:
        """
        Scan for plugins: light plugins start immediately; heavy AI plugins stay as classes.
        """
        logging.info("[PluginManager] Scanning directory: %s", self.plugin_dir)

        if not os.path.isdir(self.plugin_dir):
            logging.error("[PluginManager] Directory not found: %s", self.plugin_dir)
            return

        for filename in os.listdir(self.plugin_dir):
            if filename.endswith(".py") and not filename.startswith("__"):
                module_name = f"plugins.{filename[:-3]}"
                spec = importlib.util.spec_from_file_location(
                    module_name, os.path.join(self.plugin_dir, filename)
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module

                try:
                    assert spec.loader is not None
                    spec.loader.exec_module(module)
                except Exception as e:
                    logging.error(
                        "[PluginManager] Failed to load module %s: %s",
                        module_name,
                        e,
                    )
                    logging.debug(traceback.format_exc())
                    continue

                plugin_cls = getattr(module, "plugin_class", None)
                if not plugin_cls:
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        if issubclass(obj, TaggingPlugin) and obj is not TaggingPlugin:
                            plugin_cls = obj
                            break

                if plugin_cls:
                    try:
                        if any(x in module_name.lower() for x in ("clip", "yolo")):
                            self.plugins[module_name] = plugin_cls
                            logging.info(
                                "[PluginManager] %s registered (lazy load)",
                                module_name,
                            )
                        else:
                            self.plugins[module_name] = plugin_cls()
                            logging.info(
                                "[PluginManager] %s loaded and started",
                                module_name,
                            )
                    except Exception as e:
                        logging.error(
                            "[PluginManager] Error processing %s: %s",
                            module_name,
                            e,
                        )

    def get_plugin(self, name: str):
        """
        Return a plugin instance; instantiate lazy-loaded classes on first access.
        """
        full_name = next((k for k in self.plugins if name in k), name)
        plugin_entry = self.plugins.get(full_name)

        if inspect.isclass(plugin_entry):
            logging.info(
                "[PluginManager] First use of %s; initializing…",
                full_name,
            )
            try:
                instance = plugin_entry()
                self.plugins[full_name] = instance
                return instance
            except Exception as e:
                logging.error(
                    "[PluginManager] Failed to initialize %s: %s",
                    full_name,
                    e,
                )
                return None

        return plugin_entry
