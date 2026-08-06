"""
seedvr2_progress.py — Approximate progress from SeedVR2 CLI / debug log lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


_RE_VIDEO_INFO = re.compile(
    r"Video info:\s*(\d+)\s*frames",
    re.IGNORECASE,
)
_RE_STREAMING = re.compile(
    r"Streaming mode:\s*chunks of\s*(\d+)\s*frames",
    re.IGNORECASE,
)
_RE_CHUNK = re.compile(
    r"Chunk\s+(\d+)\s*/\s*(\d+)\s*:",
    re.IGNORECASE,
)
_RE_STREAMING_DONE = re.compile(
    r"Streaming complete:\s*(\d+)\s*frames",
    re.IGNORECASE,
)
_RE_OUTPUT_SAVED = re.compile(r"Output saved to:", re.IGNORECASE)
_RE_PROCESSING_TIME = re.compile(r"Processing time:", re.IGNORECASE)


@dataclass
class SeedVR2ProgressState:
    """Mutable state while consuming runner log lines."""

    total_frames: int = 0
    chunk_size: int = 0
    total_chunks: int = 0
    current_chunk: int = 0
    frac: float = 0.05
    message: str = ""
    phase: str = "load"
    _seen_upscale: bool = field(default=False, repr=False)

    def update(self, line: str) -> tuple[float, str, str]:
        """
        Ingest one log line. Returns ``(frac, message, phase)`` for the UI.
        ``frac`` is approximate (chunk-based for video).
        """
        text = (line or "").strip()
        if not text:
            return self.frac, self.message or "Working…", self.phase

        low = text.lower()

        m = _RE_VIDEO_INFO.search(text)
        if m:
            self.total_frames = int(m.group(1))
            self.phase = "upscale"
            self._seen_upscale = True
            self.frac = max(self.frac, 0.08)
            self.message = f"Video: {self.total_frames} frames"
            return self.frac, self.message, self.phase

        m = _RE_STREAMING.search(text)
        if m:
            self.chunk_size = int(m.group(1))
            if self.total_frames > 0 and self.chunk_size > 0:
                self.total_chunks = max(
                    1, (self.total_frames + self.chunk_size - 1) // self.chunk_size
                )
            self.phase = "upscale"
            self._seen_upscale = True
            self.frac = max(self.frac, 0.1)
            bits = [f"chunks of {self.chunk_size}"]
            if self.total_chunks:
                bits.append(f"~{self.total_chunks} total")
            self.message = "Streaming: " + ", ".join(bits)
            return self.frac, self.message, self.phase

        m = _RE_CHUNK.search(text)
        if m:
            cur = int(m.group(1))
            total = int(m.group(2))
            self.current_chunk = cur
            self.total_chunks = max(self.total_chunks, total)
            self.phase = "upscale"
            self._seen_upscale = True
            # Chunk line is emitted at the *start* of that chunk.
            done_before = max(0, cur - 1)
            if total > 0:
                self.frac = 0.12 + 0.82 * (done_before / float(total))
            self.message = f"Chunk {cur}/{total}"
            # Append short context from the log (frame counts) if present.
            rest = text[m.end() :].strip()
            if rest:
                self.message = f"{self.message}: {rest[:80]}"
            return self.frac, self.message, self.phase

        m = _RE_STREAMING_DONE.search(text)
        if m:
            written = int(m.group(1))
            self.phase = "upscale"
            self.frac = 0.95
            self.message = f"Streaming complete: {written} frames"
            return self.frac, self.message, self.phase

        if _RE_OUTPUT_SAVED.search(text) or _RE_PROCESSING_TIME.search(text):
            self.frac = 0.98
            self.phase = "upscale"
            self.message = text[:120]
            return self.frac, self.message, self.phase

        # Generic phase heuristic for remaining lines.
        if (
            any(t in low for t in ("load", "download", "weight", "model", "resolving"))
            and "upscal" not in low
            and not self._seen_upscale
        ):
            self.phase = "load"
            self.frac = max(self.frac, 0.05)
        elif any(
            t in low
            for t in (
                "processing",
                "frame",
                "encode",
                "decode",
                "generation",
                "fps",
                "chunk",
                "upscal",
                "streaming",
            )
        ):
            self.phase = "upscale"
            self._seen_upscale = True
            if self.frac < 0.15:
                self.frac = 0.15

        # Prefer short human-readable status over ASCII banners.
        if len(text) > 4 and not text.startswith("█") and "━" not in text[:3]:
            self.message = text[:120]
        return self.frac, self.message or text[:120], self.phase
