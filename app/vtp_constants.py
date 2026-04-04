"""
Shared media format tuples and folder-preview skip rules for Vibe Player.
Extracted from video_thumbnail_player so mixins can import without circular deps.
"""

from __future__ import annotations

# Supported video extensions (aligned with VLC usage in this app)
VIDEO_FORMATS = (
    ".avi",
    ".mp4",
    ".mkv",
    ".mov",
    ".wmv",
    ".flv",
    ".mpeg",
    ".mpg",
    ".webm",
    ".3gp",
    ".ogv",
    ".vob",
    ".asf",
    ".rm",
    ".qt",
)
IMAGE_FORMATS = (".png", ".jpg", ".jpeg", ".bmp", ".gif")

# Do not recurse into these when building folder preview grids (Windows junk / ACL).
_PREVIEW_SKIP_SUBDIRS = frozenset(
    {
        "$recycle.bin",
        "system volume information",
        "windowsapps",
        "recovery",
        "config.msi",
    }
)


def preview_skip_subdir(dirname: str) -> bool:
    if not dirname:
        return False
    n = dirname.strip().lower()
    if n in _PREVIEW_SKIP_SUBDIRS:
        return True
    if n.startswith("$recycle"):
        return True
    return False
