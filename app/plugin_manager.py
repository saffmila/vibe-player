"""
Plugin discovery and lazy loading for Vibe Player.

Scans ``app/plugins/`` (and ``app/plugins/upscalers/``), loads lightweight
plugins immediately, and defers heavy CLIP/YOLO/SeedVR-style plugins until
first use.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import sys
import traceback

# Import base classes so issubclass() checks work
try:
    from plugins.plugin_base import TaggingPlugin
except ImportError:
    from plugin_base import TaggingPlugin

try:
    from plugins.processing_base import UpscaleBackend
except ImportError:
    try:
        from processing_base import UpscaleBackend
    except ImportError:
        UpscaleBackend = None  # type: ignore[misc, assignment]


_LAZY_NAME_MARKERS = ("clip", "yolo", "seedvr", "upscale", "dat", "rife")


class PluginManager:
    def __init__(self, plugin_dir: str | None = None) -> None:
        if plugin_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            plugin_dir = os.path.join(base_dir, "plugins")

        self.plugin_dir = plugin_dir
        self.plugins: dict[str, object] = {}
        self.upscale_plugins: dict[str, object] = {}

    def _iter_plugin_files(self) -> list[tuple[str, str]]:
        """Return (module_name, file_path) pairs for discoverable plugin modules."""
        found: list[tuple[str, str]] = []
        if not os.path.isdir(self.plugin_dir):
            return found

        def _is_plugin_module(filename: str) -> bool:
            if not filename.endswith(".py") or filename.startswith("__"):
                return False
            # Skip shared base modules (not concrete plugins).
            stem = filename[:-3].lower()
            return not stem.endswith("_base")

        for filename in os.listdir(self.plugin_dir):
            if _is_plugin_module(filename):
                module_name = f"plugins.{filename[:-3]}"
                found.append((module_name, os.path.join(self.plugin_dir, filename)))

        upscalers_dir = os.path.join(self.plugin_dir, "upscalers")
        if os.path.isdir(upscalers_dir):
            for filename in os.listdir(upscalers_dir):
                if _is_plugin_module(filename):
                    module_name = f"plugins.upscalers.{filename[:-3]}"
                    found.append((module_name, os.path.join(upscalers_dir, filename)))

        return found

    def _resolve_plugin_class(self, module) -> type | None:
        plugin_cls = getattr(module, "plugin_class", None)
        if plugin_cls:
            return plugin_cls

        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if UpscaleBackend is not None and issubclass(obj, UpscaleBackend) and obj is not UpscaleBackend:
                return obj
            if issubclass(obj, TaggingPlugin) and obj is not TaggingPlugin:
                return obj
        return None

    def _should_lazy_load(self, module_name: str, plugin_cls: type) -> bool:
        if any(x in module_name.lower() for x in _LAZY_NAME_MARKERS):
            return True
        if UpscaleBackend is not None and issubclass(plugin_cls, UpscaleBackend):
            return bool(getattr(plugin_cls, "lazy_load", True))
        return False

    def _register_plugin(self, module_name: str, plugin_cls: type) -> None:
        is_upscale = UpscaleBackend is not None and issubclass(plugin_cls, UpscaleBackend)
        store = self.upscale_plugins if is_upscale else self.plugins

        try:
            if self._should_lazy_load(module_name, plugin_cls):
                store[module_name] = plugin_cls
                logging.info("[PluginManager] %s registered (lazy load)", module_name)
            else:
                store[module_name] = plugin_cls()
                logging.info("[PluginManager] %s loaded and started", module_name)
        except Exception as e:
            logging.error("[PluginManager] Error processing %s: %s", module_name, e)

    def load_plugins(self) -> None:
        """
        Scan for plugins: light plugins start immediately; heavy AI plugins stay as classes.
        """
        logging.info("[PluginManager] Scanning directory: %s", self.plugin_dir)

        if not os.path.isdir(self.plugin_dir):
            logging.error("[PluginManager] Directory not found: %s", self.plugin_dir)
            return

        for module_name, file_path in self._iter_plugin_files():
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                logging.error("[PluginManager] Cannot load spec for %s", module_name)
                continue

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module

            try:
                spec.loader.exec_module(module)
            except Exception as e:
                logging.error(
                    "[PluginManager] Failed to load module %s: %s",
                    module_name,
                    e,
                )
                logging.debug(traceback.format_exc())
                continue

            plugin_cls = self._resolve_plugin_class(module)
            if plugin_cls:
                self._register_plugin(module_name, plugin_cls)

    def _instantiate_if_needed(self, store: dict[str, object], full_name: str):
        plugin_entry = store.get(full_name)
        if inspect.isclass(plugin_entry):
            logging.info("[PluginManager] First use of %s; initializing…", full_name)
            try:
                instance = plugin_entry()
                store[full_name] = instance
                return instance
            except Exception as e:
                logging.error(
                    "[PluginManager] Failed to initialize %s: %s",
                    full_name,
                    e,
                )
                return None
        return plugin_entry

    def get_plugin(self, name: str):
        """
        Return a tagging/UI plugin instance; instantiate lazy-loaded classes on first access.
        """
        full_name = next((k for k in self.plugins if name in k), name)
        return self._instantiate_if_needed(self.plugins, full_name)

    def get_upscale_plugin(self, name: str):
        """Return an upscale backend by id or module name fragment."""
        # Prefer exact backend id match after instantiation candidates
        for key, entry in list(self.upscale_plugins.items()):
            instance = self._instantiate_if_needed(self.upscale_plugins, key)
            if instance is None:
                continue
            if getattr(instance, "id", None) == name or name in key:
                return instance
        full_name = next((k for k in self.upscale_plugins if name in k), name)
        return self._instantiate_if_needed(self.upscale_plugins, full_name)

    def list_upscale_plugins(self) -> list:
        """Return instantiated upscale backends (lazy classes are created)."""
        backends = []
        for key in list(self.upscale_plugins.keys()):
            instance = self._instantiate_if_needed(self.upscale_plugins, key)
            if instance is not None:
                backends.append(instance)
        return backends
