"""
plugin_base.py — Base class for tagging plugins.

TaggingPlugin defines the interface: run(file_path, metadata) returning {'tags': [...]}.
"""


class TaggingPlugin:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def run(self, file_path, metadata=None):
        """
        Main method to tag one file. Returns dict with at least {'tags': [...]}.
        """
        raise NotImplementedError("Plugin must implement the run() method.")
