"""
Silent OpenCV video reader shared by Video Compare and Video Crop.

Decodes frames to numpy (BGR) without audio. Prefer sequential reads when
scrubbing forward so H.264 seeks stay cheap.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import cv2
import numpy as np


class OpenCvClip:
    """Silent OpenCV reader with reliable ratio seek (no audio)."""

    def __init__(self, path: str, *, log_tag: str = "OpenCvClip"):
        self.path = os.path.normpath(path)
        self.log_tag = log_tag
        self.cap: Optional[cv2.VideoCapture] = None
        self.fps = 25.0
        self.frame_count = 0
        self.duration = 0.0
        self.width = 0
        self.height = 0
        self.frame_idx = 0
        self.frame_bgr: Optional[np.ndarray] = None
        self._open(self.path)

    def _open(self, path: str) -> bool:
        self.close()
        self.path = os.path.normpath(path)

        params = [
            cv2.CAP_PROP_HW_ACCELERATION,
            cv2.VIDEO_ACCELERATION_ANY,
        ]
        cap = cv2.VideoCapture(self.path, cv2.CAP_FFMPEG, params)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self.path)

        if not cap.isOpened():
            self.cap = None
            logging.error("[%s] OpenCV failed to open %s", self.log_tag, self.path)
            return False

        self.cap = cap
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        self.fps = fps if fps > 1e-3 else 25.0
        self.frame_count = max(0, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

        if self.frame_count > 0:
            self.duration = self.frame_count / self.fps
        else:
            self.duration = 0.0

        self.frame_idx = 0
        self.frame_bgr = None
        self.read_at_index(0)
        return True

    def close(self) -> None:
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = None
        self.frame_bgr = None

    def reopen(self, path: str) -> bool:
        return self._open(path)

    @property
    def ratio(self) -> float:
        if self.frame_count <= 1:
            return 0.0
        return max(0.0, min(1.0, self.frame_idx / float(self.frame_count - 1)))

    @property
    def time_s(self) -> float:
        if self.fps > 1e-6:
            return self.frame_idx / self.fps
        if self.duration > 0 and self.frame_count > 1:
            return self.ratio * self.duration
        return 0.0

    def read_at_index(self, idx: int) -> bool:
        if self.cap is None:
            return False
        n = max(1, self.frame_count)
        idx = int(max(0, min(n - 1, idx)))
        try:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))
            ok, frame = self.cap.read()
        except Exception:
            logging.debug("[%s] read_at_index failed", self.log_tag, exc_info=True)
            return False
        if not ok or frame is None:
            return False
        self.frame_bgr = frame
        self.frame_idx = idx
        if self.width <= 0 or self.height <= 0:
            self.height, self.width = frame.shape[:2]
        return True

    def seek_ratio(self, ratio: float) -> bool:
        ratio = max(0.0, min(1.0, float(ratio)))
        if self.frame_count <= 1:
            return self.read_at_index(0)
        idx = int(round(ratio * (self.frame_count - 1)))
        return self.seek_index(idx)

    def seek_index(self, idx: int) -> bool:
        if self.cap is None:
            return False
        n = max(1, self.frame_count)
        idx = int(max(0, min(n - 1, idx)))
        if idx == self.frame_idx and self.frame_bgr is not None:
            return True
        if idx > self.frame_idx and (idx - self.frame_idx) <= 45:
            while self.frame_idx < idx:
                if not self.advance_one():
                    return self.read_at_index(idx)
            return True
        return self.read_at_index(idx)

    def seek_time(self, seconds: float) -> bool:
        if self.fps > 1e-6:
            return self.seek_index(int(round(float(seconds) * self.fps)))
        if self.duration > 0:
            return self.seek_ratio(float(seconds) / self.duration)
        return False

    def advance_one(self) -> bool:
        if self.cap is None:
            return False
        if self.frame_count > 0 and self.frame_idx >= self.frame_count - 1:
            return False
        try:
            ok, frame = self.cap.read()
        except Exception:
            return False
        if not ok or frame is None:
            return False
        self.frame_bgr = frame
        self.frame_idx = min(self.frame_idx + 1, max(0, self.frame_count - 1))
        return True
