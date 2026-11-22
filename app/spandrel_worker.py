"""
spandrel_worker.py — Child process for one Spandrel upscale job.

Runs in a separate OS process so Cancel / app exit can ``taskkill /T`` the
tree and free CUDA immediately (in-process torch cannot interrupt a forward).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _progress(pct: float, msg: str) -> None:
    _emit({"t": "progress", "pct": float(pct), "msg": str(msg or "")})


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    from spandrel_pipeline import upscale_image_file

    input_path = str(job.get("input_path") or "")
    output_path = str(job.get("output_path") or "")
    model = str(job.get("model") or "")
    try:
        tile_size = int(job.get("tile_size") if job.get("tile_size") is not None else 0)
    except (TypeError, ValueError):
        tile_size = 0
    try:
        overlap = int(job.get("overlap") if job.get("overlap") is not None else 32)
    except (TypeError, ValueError):
        overlap = 32
    try:
        tile_cap = int(job.get("tile_cap") or 0)
    except (TypeError, ValueError):
        tile_cap = 0
    try:
        jpeg_quality = int(
            job.get("jpeg_quality") if job.get("jpeg_quality") is not None else 92
        )
    except (TypeError, ValueError):
        jpeg_quality = 92
    try:
        png_compress = int(
            job.get("png_compress") if job.get("png_compress") is not None else 3
        )
    except (TypeError, ValueError):
        png_compress = 3
    return upscale_image_file(
        input_path,
        output_path,
        model=model,
        tile_size=tile_size,
        overlap=overlap,
        auto_tile=bool(job.get("auto_tile", True)),
        tile_cap=tile_cap,
        cuda_device=job.get("cuda_device"),
        target_scale=job.get("target_scale"),
        unload_after=bool(job.get("unload_after", True)),
        jpeg_quality=jpeg_quality,
        png_compress=png_compress,
        progress_cb=_progress,
        should_stop=None,
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Support: ``VibePlayer.exe --vibe-spandrel-job <job.json>``
    if args and args[0] == "--vibe-spandrel-job":
        args = args[1:]
    if not args:
        _emit(
            {
                "t": "result",
                "ok": False,
                "output_path": None,
                "error": "runtime_error",
                "message": "Spandrel worker: missing job JSON path.",
            }
        )
        return 2

    job_path = args[0]
    try:
        with open(job_path, "r", encoding="utf-8") as fh:
            job = json.load(fh)
    except Exception as exc:
        _emit(
            {
                "t": "result",
                "ok": False,
                "output_path": None,
                "error": "runtime_error",
                "message": f"Spandrel worker: bad job file: {exc}",
            }
        )
        return 2

    # Keep stdout clean for JSONL; send logs to stderr / file.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    try:
        result = run_job(job if isinstance(job, dict) else {})
    except Exception as exc:
        logging.exception("[SpandrelWorker] job failed")
        result = {
            "ok": False,
            "output_path": None,
            "error": "runtime_error",
            "message": str(exc),
        }

    payload = {"t": "result", **(result if isinstance(result, dict) else {})}
    _emit(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    # Ensure ``app/`` is on sys.path when launched as a script.
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    raise SystemExit(main())
