"""
birefnet_weights_setup.py — Download BiRefNet weights into models/birefnet/.

Uses ``transformers`` (already in the app deps) — no huggingface_hub required.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from birefnet_config import (
    BIREFNET_DISK_ESTIMATE,
    BIREFNET_MODEL_VARIANTS,
    default_birefnet_dir,
    find_model_dir,
    python_deps_status,
    resolve_model_id,
    resolve_model_variant,
    runtime_status,
)

ProgressCb = Callable[[int, int, str], None]
StopCb = Callable[[], bool]


def _ensure_birefnet_deps() -> None:
    deps = python_deps_status()
    if not deps.get("ready"):
        raise RuntimeError(deps.get("message") or "Missing BiRefNet Python dependencies.")


def _emit(progress_cb: ProgressCb | None, step: int, total: int, detail: str) -> None:
    if progress_cb:
        progress_cb(step, total, detail)


def _stopped(should_stop: StopCb | None) -> bool:
    return bool(should_stop and should_stop())


def download_recommended_weights(
    *,
    model_variant: str | None = None,
    target_dir: str | Path | None = None,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
) -> Path:
    """
    Pull a BiRefNet variant into ``models/birefnet/``.

    Returns the resolved snapshot directory containing ``config.json``.
    """
    rt = runtime_status(deep=True)
    if not rt.get("ready"):
        raise RuntimeError(rt.get("message") or "GPU runtime not ready.")

    variant = resolve_model_variant(model_variant)
    model_id = resolve_model_id(variant)
    label = BIREFNET_MODEL_VARIANTS[variant]["label"]

    root = Path(target_dir or default_birefnet_dir())
    root.mkdir(parents=True, exist_ok=True)

    existing = find_model_dir(model_id=model_id)
    if existing is not None:
        _emit(progress_cb, 1, 1, f"{label} weights already present.")
        return existing

    if _stopped(should_stop):
        raise InterruptedError("Download cancelled.")

    _ensure_birefnet_deps()
    _emit(
        progress_cb,
        0,
        2,
        f"Downloading {label} ({BIREFNET_DISK_ESTIMATE})…",
    )

    from transformers import AutoModelForImageSegmentation

    if _stopped(should_stop):
        raise InterruptedError("Download cancelled.")

    try:
        AutoModelForImageSegmentation.from_pretrained(
            model_id,
            trust_remote_code=True,
            cache_dir=str(root),
        )
    except Exception as exc:
        logging.exception("[BiRefNet] Weight download failed for %s", model_id)
        raise RuntimeError(f"BiRefNet download failed:\n{exc}") from exc

    if _stopped(should_stop):
        raise InterruptedError("Download cancelled.")

    snap = find_model_dir(model_id=model_id)
    if snap is None:
        raise RuntimeError(
            "Download finished but BiRefNet weights were not found on disk.\n"
            f"Model: {model_id}\nExpected under: {root}"
        )

    _emit(progress_cb, 2, 2, f"{label} weights ready.")
    return snap
