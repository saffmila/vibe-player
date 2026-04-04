"""
Backward compatibility: the Canvas viewer used to live here as ``ImageViewer``.

Import from ``image_operations`` for new code (``create_image_viewer``, ``ImageViewerGPU``, …).
"""

from image_operations import ImageViewerLegacy, create_image_viewer  # noqa: F401

# Historical name: Canvas-only class was ``ImageViewer`` in this module.
ImageViewer = ImageViewerLegacy

__all__ = ["ImageViewer", "ImageViewerLegacy", "create_image_viewer"]
