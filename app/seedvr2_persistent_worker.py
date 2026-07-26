"""
seedvr2_persistent_worker.py — Long-lived SeedVR2 worker (runner venv).

Speaks JSON-lines on stdin/stdout. Keeps runner_cache so DiT/VAE stay loaded
across jobs until shutdown (app exit).

Launch (from host):
  <runner>/.venv/Scripts/python.exe seedvr2_persistent_worker.py --runner-dir <runner>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _build_args(job: dict) -> SimpleNamespace:
    """Build a Namespace compatible with inference_cli.process_single_file."""
    opts = job.get("options") or {}
    model_dir = job.get("model_dir") or opts.get("model_dir") or ""
    dit_model = job.get("dit_model") or opts.get("dit_model") or "seedvr2_ema_3b_fp8_e4m3fn.safetensors"
    cuda_device = str(job.get("cuda_device") or opts.get("cuda_device") or "0")
    resolution = int(opts.get("resolution") or 1080)
    batch_size = int(opts.get("batch_size") or 5)
    video_backend = opts.get("video_backend") or "opencv"

    return SimpleNamespace(
        input=job.get("input"),
        output=job.get("output"),
        output_format=opts.get("output_format"),  # None = auto
        video_backend=video_backend,
        use_10bit=False,
        model_dir=model_dir,
        dit_model=dit_model,
        resolution=resolution,
        max_resolution=int(opts.get("max_resolution") or 0),
        batch_size=batch_size,
        seed=int(opts.get("seed") or 42),
        skip_first_frames=0,
        load_cap=0,
        chunk_size=0,
        prepend_frames=0,
        temporal_overlap=0,
        color_correction=opts.get("color_correction") or "lab",
        input_noise_scale=0.0,
        latent_noise_scale=0.0,
        dit_offload_device="none",
        vae_offload_device="none",
        tensor_offload_device="none",
        blocks_to_swap=0,
        swap_io_components=False,
        vae_encode_tiled=bool(opts.get("vae_tiled", True)),
        vae_encode_tile_size=int(opts.get("vae_encode_tile_size") or 1024),
        vae_encode_tile_overlap=128,
        vae_decode_tiled=bool(opts.get("vae_tiled", True)),
        vae_decode_tile_size=int(opts.get("vae_decode_tile_size") or 1024),
        vae_decode_tile_overlap=128,
        tile_debug="false",
        allow_vram_overflow=False,
        attention_mode="sdpa",
        compile_dit=False,
        compile_vae=False,
        compile_backend="inductor",
        compile_mode="default",
        compile_fullgraph=False,
        compile_dynamic=False,
        compile_dynamo_cache_size_limit=64,
        compile_dynamo_recompile_limit=128,
        cache_dit=True,
        cache_vae=True,
        cuda_device=cuda_device,
        debug=bool(opts.get("debug") or False),
        uniform_batch_size=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner-dir", required=True, help="ComfyUI-SeedVR2 checkout root")
    cli = parser.parse_args()
    runner_dir = Path(cli.runner_dir).resolve()
    if not (runner_dir / "inference_cli.py").is_file():
        _emit({"event": "fatal", "ok": False, "error": "bad_runner", "message": f"No inference_cli.py in {runner_dir}"})
        return 2

    os.chdir(runner_dir)
    if str(runner_dir) not in sys.path:
        sys.path.insert(0, str(runner_dir))

    try:
        import inference_cli as seedvr_cli
        from src.utils.downloads import download_weight
        from src.utils.model_registry import DEFAULT_VAE
    except Exception as exc:
        _emit(
            {
                "event": "fatal",
                "ok": False,
                "error": "import_failed",
                "message": f"Failed to import SeedVR CLI: {exc}",
            }
        )
        return 3

    runner_cache: dict = {}
    loaded_key: tuple | None = None
    models_ready = False

    _emit({"event": "ready", "ok": True})

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit({"event": "result", "ok": False, "error": "bad_json", "message": str(exc)})
            continue

        cmd = (req.get("cmd") or "").lower()
        if cmd in ("shutdown", "quit", "exit"):
            _emit({"event": "shutdown", "ok": True})
            break

        if cmd == "ping":
            _emit({"event": "pong", "ok": True, "loaded": loaded_key is not None})
            continue

        if cmd == "unload":
            runner_cache.clear()
            loaded_key = None
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            _emit({"event": "unloaded", "ok": True})
            continue

        if cmd != "upscale":
            _emit({"event": "result", "ok": False, "error": "unknown_cmd", "message": cmd})
            continue

        input_path = req.get("input")
        output_path = req.get("output")
        if not input_path or not output_path:
            _emit(
                {
                    "event": "result",
                    "ok": False,
                    "error": "bad_request",
                    "message": "input and output are required",
                }
            )
            continue

        args = _build_args(req)
        device_list = [d.strip() for d in str(args.cuda_device).split(",") if d.strip()] or ["0"]
        key = (str(args.model_dir), str(args.dit_model), str(args.cuda_device))

        try:
            _emit({"event": "progress", "phase": "upscale", "msg": f"Upscaling {Path(input_path).name}…"})

            if not models_ready or loaded_key != key:
                # Model / device change → drop cache and (re)download/stage weights check.
                runner_cache.clear()
                _emit({"event": "progress", "phase": "load", "msg": f"Loading model {args.dit_model}…"})
                ok = download_weight(
                    dit_model=args.dit_model,
                    vae_model=DEFAULT_VAE,
                    model_dir=args.model_dir,
                    debug=seedvr_cli.debug,
                )
                if not ok:
                    _emit(
                        {
                            "event": "result",
                            "ok": False,
                            "error": "download_failed",
                            "message": "Failed to resolve/download model weights",
                        }
                    )
                    continue
                models_ready = True
                loaded_key = key
                _emit({"event": "progress", "phase": "upscale", "msg": f"Upscaling {Path(input_path).name}…"})

            # Auto format when not set
            format_auto = args.output_format is None
            if format_auto:
                itype = seedvr_cli.get_input_type(input_path)
                args.output_format = "mp4" if itype == "video" else "png"

            _emit({"event": "progress", "phase": "upscale", "msg": f"Upscaling {Path(input_path).name}…"})

            frames = seedvr_cli.process_single_file(
                input_path,
                args,
                device_list,
                output_path,
                format_auto_detected=format_auto,
                runner_cache=runner_cache,
            )
            if frames <= 0 and not Path(output_path).is_file():
                _emit(
                    {
                        "event": "result",
                        "ok": False,
                        "error": "no_frames",
                        "message": "Upscale produced no frames / missing output",
                    }
                )
                continue

            _emit(
                {
                    "event": "result",
                    "ok": True,
                    "output_path": output_path,
                    "frames": int(frames),
                }
            )
        except Exception as exc:
            _emit(
                {
                    "event": "result",
                    "ok": False,
                    "error": "runner_failed",
                    "message": f"{exc}\n{traceback.format_exc()[-1200:]}",
                }
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
