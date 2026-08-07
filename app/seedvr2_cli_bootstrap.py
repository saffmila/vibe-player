"""
seedvr2_cli_bootstrap.py — Run SeedVR inference_cli with optional chunk preview.

Env:
  SEEDVR2_RUNNER_DIR          — ComfyUI-SeedVR2 checkout (required)
  VIBE_SEEDVR2_PREVIEW_PATH  — JPEG path for live chunk previews
  VIBE_SEEDVR2_CHUNK_PREVIEW — "1" to enable hooks (default off unless set)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    runner = (os.environ.get("SEEDVR2_RUNNER_DIR") or "").strip()
    if not runner:
        print("SEEDVR2_RUNNER_DIR is not set", file=sys.stderr)
        return 2
    runner_path = Path(runner).resolve()
    cli = runner_path / "inference_cli.py"
    if not cli.is_file():
        print(f"inference_cli.py not found in {runner_path}", file=sys.stderr)
        return 2

    app_dir = str(Path(__file__).resolve().parent)
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    runner_s = str(runner_path)
    if runner_s not in sys.path:
        sys.path.insert(0, runner_s)
    os.chdir(runner_s)

    import inference_cli

    preview = (os.environ.get("VIBE_SEEDVR2_PREVIEW_PATH") or "").strip()
    enabled = (os.environ.get("VIBE_SEEDVR2_CHUNK_PREVIEW") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    restore = None
    if preview and enabled:
        try:
            from seedvr2_preview_hook import install_chunk_preview_hooks

            restore = install_chunk_preview_hooks(inference_cli, preview)
            print(f"[seedvr2] Chunk preview enabled → {preview}", flush=True)
        except Exception as exc:
            print(f"[seedvr2] Chunk preview hook skipped: {exc}", flush=True)

    sys.argv = [str(cli)] + sys.argv[1:]
    try:
        code = inference_cli.main()
        return int(code or 0)
    finally:
        if restore is not None:
            try:
                restore()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
