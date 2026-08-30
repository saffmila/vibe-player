"""
birefnet_pipeline.py — PyTorch FP16 BiRefNet inference @ 1024 → PNG with alpha.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

from PIL import Image

from birefnet_config import (
    BIREFNET_INPUT_SIZE,
    default_birefnet_dir,
    find_model_dir,
    parse_bg_rgb,
    resolve_model_id,
    resolve_model_variant,
    runtime_status,
    weights_status,
)
from birefnet_mask import post_process_mask
from birefnet_weights_setup import download_recommended_weights

ProgressCb = Callable[[float, str], None]
StopCb = Callable[[], bool]

_model = None
_model_lock = threading.Lock()
_model_device: str | None = None
_model_cuda_index: str | None = None
_model_id_loaded: str | None = None


def _normalize_cuda_index(cuda_device: str | int | None) -> str:
    raw = str(cuda_device if cuda_device is not None else "0").strip()
    if not raw:
        return "0"
    if raw.lower().startswith("cuda:"):
        raw = raw.split(":", 1)[1].strip()
    if "," in raw:
        raw = raw.split(",", 1)[0].strip()
    return raw if raw.isdigit() else "0"


def _resolve_torch_device(cuda_device: str | int | None) -> str:
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for BiRefNet background removal.")
    idx = _normalize_cuda_index(cuda_device)
    count = torch.cuda.device_count()
    if count <= 0:
        raise RuntimeError("CUDA is required for BiRefNet background removal.")
    if int(idx) >= count:
        idx = "0"
    return f"cuda:{idx}"


def _emit(progress_cb: ProgressCb | None, frac: float, msg: str) -> None:
    if progress_cb:
        try:
            progress_cb(max(0.0, min(1.0, float(frac))), msg)
        except Exception:
            pass


def _stopped(should_stop: StopCb | None) -> bool:
    return bool(should_stop and should_stop())


def _load_model(
    *,
    cuda_device: str | int | None = None,
    model_variant: str | None = None,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
):
    global _model, _model_device, _model_cuda_index, _model_id_loaded

    want_dev = _resolve_torch_device(cuda_device)
    want_idx = _normalize_cuda_index(cuda_device)
    want_variant = resolve_model_variant(model_variant)
    want_model_id = resolve_model_id(want_variant)

    with _model_lock:
        if (
            _model is not None
            and _model_cuda_index == want_idx
            and _model_id_loaded == want_model_id
        ):
            return _model

        if _model is not None:
            _model = None
            _model_device = None
            _model_cuda_index = None
            _model_id_loaded = None
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

        rt = runtime_status(deep=True)
        if not rt.get("ready"):
            raise RuntimeError(rt.get("message") or "GPU runtime not ready.")

        wt = weights_status(model_variant=want_variant)
        if not wt.get("ready"):
            _emit(progress_cb, 0.05, "Downloading BiRefNet weights…")
            download_recommended_weights(
                model_variant=want_variant,
                progress_cb=_weights_progress(progress_cb),
                should_stop=should_stop,
            )
            wt = weights_status(model_variant=want_variant)
            if not wt.get("ready"):
                raise RuntimeError(wt.get("message") or "BiRefNet weights missing.")

        if _stopped(should_stop):
            raise InterruptedError("Cancelled.")

        import torch
        from transformers import AutoModelForImageSegmentation

        model_path = find_model_dir(model_id=want_model_id)
        cache_root = str(default_birefnet_dir())
        _emit(progress_cb, 0.15, "Loading BiRefNet model…")

        if model_path is not None:
            model = AutoModelForImageSegmentation.from_pretrained(
                str(model_path),
                trust_remote_code=True,
                local_files_only=True,
            )
        else:
            model = AutoModelForImageSegmentation.from_pretrained(
                want_model_id,
                trust_remote_code=True,
                cache_dir=cache_root,
            )

        try:
            torch.set_float32_matmul_precision("high")
        except Exception:
            pass

        model.to(want_dev)
        model.eval()
        model.half()
        _model = model
        _model_device = want_dev
        _model_cuda_index = want_idx
        _model_id_loaded = want_model_id
        _emit(progress_cb, 0.25, "BiRefNet ready.")
        return _model


def _weights_progress(outer: ProgressCb | None):
    def _inner(step: int, total: int, detail: str) -> None:
        if total <= 0:
            return
        frac = 0.05 + 0.10 * (step / total)
        _emit(outer, frac, detail)

    return _inner


def unload_model() -> None:
    """Release cached model (VRAM) after batch jobs."""
    global _model, _model_device, _model_cuda_index, _model_id_loaded
    with _model_lock:
        _model = None
        _model_device = None
        _model_cuda_index = None
        _model_id_loaded = None
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def remove_background_from_image(
    image: Image.Image,
    *,
    cuda_device: str | int | None = None,
    model_variant: str | None = None,
    mask_threshold: int = 0,
    mask_feather: int = 0,
    mask_morph: int = 0,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
) -> Image.Image:
    """Return RGBA PIL image with BiRefNet alpha matte."""
    if _stopped(should_stop):
        raise InterruptedError("Cancelled.")

    import torch
    from torchvision import transforms

    model = _load_model(
        cuda_device=cuda_device,
        model_variant=model_variant,
        progress_cb=progress_cb,
        should_stop=should_stop,
    )
    device = _model_device or _resolve_torch_device(cuda_device)

    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    rgb = image.convert("RGB")
    orig_w, orig_h = rgb.size

    _emit(progress_cb, 0.35, "Running BiRefNet…")

    transform = transforms.Compose(
        [
            transforms.Resize((BIREFNET_INPUT_SIZE, BIREFNET_INPUT_SIZE)),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )

    tensor = transform(rgb).unsqueeze(0).to(device).half()

    if _stopped(should_stop):
        raise InterruptedError("Cancelled.")

    with torch.no_grad():
        preds = model(tensor)
        if isinstance(preds, (list, tuple)):
            pred = preds[-1]
        else:
            pred = preds
        mask = pred.sigmoid().float().cpu()

    mask = mask[0].squeeze()
    mask_pil = transforms.ToPILImage()(mask)
    mask_pil = mask_pil.resize((orig_w, orig_h), Image.LANCZOS)
    mask_pil = post_process_mask(
        mask_pil,
        threshold_pct=int(mask_threshold or 0),
        feather_px=int(mask_feather or 0),
        morph=int(mask_morph or 0),
    )

    rgba = rgb.convert("RGBA")
    rgba.putalpha(mask_pil)
    _emit(progress_cb, 0.95, "Done.")
    return rgba


def apply_background(
    rgba: Image.Image,
    *,
    bg_mode: str = "transparent",
    bg_color: str | None = None,
) -> Image.Image:
    """
    ``transparent`` → RGBA with alpha.
    ``color`` → RGB image composited on a solid background.
    """
    mode = (bg_mode or "transparent").strip().lower()
    if mode != "color":
        return rgba
    rgb_bg = parse_bg_rgb(bg_color)
    base = Image.new("RGB", rgba.size, rgb_bg)
    if rgba.mode != "RGBA":
        rgba = rgba.convert("RGBA")
    base.paste(rgba, mask=rgba.split()[3])
    return base


def remove_background_from_file(
    input_path: str,
    output_path: str,
    *,
    bg_mode: str = "transparent",
    bg_color: str | None = None,
    cuda_device: str | int | None = None,
    model_variant: str | None = None,
    mask_threshold: int = 0,
    mask_feather: int = 0,
    mask_morph: int = 0,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
) -> dict[str, Any]:
    """Load image, remove background, save PNG (alpha or solid color)."""
    if _stopped(should_stop):
        return {
            "ok": False,
            "output_path": None,
            "error": "aborted",
            "message": "Cancelled.",
        }

    rt = runtime_status(deep=True)
    if not rt.get("ready"):
        return {
            "ok": False,
            "output_path": None,
            "error": rt.get("error") or "gpu_pack_missing",
            "message": rt.get("message"),
        }

    try:
        with Image.open(input_path) as im:
            rgba = remove_background_from_image(
                im,
                cuda_device=cuda_device,
                model_variant=model_variant,
                mask_threshold=mask_threshold,
                mask_feather=mask_feather,
                mask_morph=mask_morph,
                progress_cb=progress_cb,
                should_stop=should_stop,
            )
            result_im = apply_background(
                rgba,
                bg_mode=bg_mode,
                bg_color=bg_color,
            )
    except InterruptedError:
        return {
            "ok": False,
            "output_path": None,
            "error": "aborted",
            "message": "Cancelled.",
        }
    except Exception as exc:
        logging.exception("[BiRefNet] Failed for %s", input_path)
        return {
            "ok": False,
            "output_path": None,
            "error": "runtime_error",
            "message": str(exc),
        }

    out = output_path
    stem, _ext = os.path.splitext(output_path)
    if not stem.lower().endswith(".png"):
        out = f"{stem}.png"

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    save_kw: dict[str, Any] = {"format": "PNG", "compress_level": 6, "optimize": True}
    if result_im.mode == "RGBA":
        result_im.save(out, **save_kw)
    else:
        result_im.convert("RGB").save(out, **save_kw)
    _emit(progress_cb, 1.0, os.path.basename(out))
    return {"ok": True, "output_path": out, "error": None, "message": None}
