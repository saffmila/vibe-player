"""
seedvr2_weights_setup.py — One-click download of recommended SeedVR2 models.

Pulls the default DiT (3B FP8) + VAE from Hugging Face into the user weights
folder. No huggingface_hub dependency — plain HTTPS with resume support.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from seedvr2_config import DEFAULT_DIT_MODEL, default_weights_dir


SEEDVR2_WEIGHTS_REPO = "numz/SeedVR2_comfyUI"
SEEDVR2_WEIGHTS_HF_URL = f"https://huggingface.co/{SEEDVR2_WEIGHTS_REPO}"
SEEDVR2_VAE_MODEL = "ema_vae_fp16.safetensors"

# (filename, approximate bytes) — sizes are hints for UI only.
RECOMMENDED_WEIGHT_FILES: tuple[tuple[str, int], ...] = (
    (DEFAULT_DIT_MODEL, 3_391_544_696),
    (SEEDVR2_VAE_MODEL, 501_324_814),
)

SEEDVR2_WEIGHTS_DISK_ESTIMATE = "~4 GB"

ProgressCb = Callable[[int, int, str], None]
StopCb = Callable[[], bool]

_CHUNK = 1024 * 1024  # 1 MiB
_UA = "VibePlayer-SeedVR2/1.0"


def recommended_weight_url(filename: str) -> str:
    name = (filename or "").strip().lstrip("/")
    return f"{SEEDVR2_WEIGHTS_HF_URL}/resolve/main/{name}"


def _emit(progress_cb: ProgressCb | None, step: int, total: int, detail: str) -> None:
    if progress_cb:
        progress_cb(step, total, detail)


def _stopped(should_stop: StopCb | None) -> bool:
    return bool(should_stop and should_stop())


def _fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / (1024 ** 3):.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / (1024 ** 2):.0f} MB"
    return f"{n} B"


def _download_file(
    url: str,
    dest: Path,
    *,
    expected_size: int | None = None,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
    file_index: int = 0,
    file_count: int = 1,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".partial")
    existing = partial.stat().st_size if partial.is_file() else 0

    headers = {"User-Agent": _UA}
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"

    req = Request(url, headers=headers)
    with urlopen(req, timeout=120) as resp:
        # If server ignored Range, restart.
        status = getattr(resp, "status", None) or resp.getcode()
        if existing > 0 and status == 200:
            existing = 0
            try:
                partial.unlink(missing_ok=True)
            except TypeError:
                if partial.is_file():
                    partial.unlink()
        length_hdr = resp.headers.get("Content-Length")
        try:
            chunk_len = int(length_hdr) if length_hdr else 0
        except ValueError:
            chunk_len = 0
        total = existing + chunk_len if chunk_len else (expected_size or 0)

        mode = "ab" if existing > 0 and status == 206 else "wb"
        if mode == "wb":
            existing = 0
        done = existing
        with open(partial, mode) as out:
            while True:
                if _stopped(should_stop):
                    raise InterruptedError("Download cancelled.")
                block = resp.read(_CHUNK)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if total > 0:
                    # Map byte progress into a per-file slice of 0..100 * file_count.
                    file_base = file_index * 100
                    pct = min(100, int(done * 100 / total))
                    step = file_base + pct
                    _emit(
                        progress_cb,
                        step,
                        file_count * 100,
                        f"{dest.name}: {_fmt_bytes(done)}"
                        + (f" / {_fmt_bytes(total)}" if total else ""),
                    )
                else:
                    _emit(
                        progress_cb,
                        file_index * 100,
                        max(1, file_count * 100),
                        f"{dest.name}: {_fmt_bytes(done)}",
                    )

    if not partial.is_file() or partial.stat().st_size < 1024 * 1024:
        raise RuntimeError(f"Download incomplete or too small: {dest.name}")
    os.replace(partial, dest)


def download_recommended_weights(
    target_dir: str | Path | None = None,
    *,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
    force: bool = False,
) -> dict:
    """
    Download default 3B FP8 DiT + VAE into ``target_dir``.

    Returns ``{ok, path, message, error, files}``.
    """
    root = Path(target_dir or default_weights_dir())
    try:
        root.mkdir(parents=True, exist_ok=True)
        files_done: list[str] = []
        total_files = len(RECOMMENDED_WEIGHT_FILES)
        _emit(progress_cb, 0, total_files * 100, f"Saving models to:\n{root}")

        for i, (name, size_hint) in enumerate(RECOMMENDED_WEIGHT_FILES):
            if _stopped(should_stop):
                raise InterruptedError("Download cancelled.")
            dest = root / name
            if dest.is_file() and dest.stat().st_size > 1024 * 1024 and not force:
                _emit(
                    progress_cb,
                    (i + 1) * 100,
                    total_files * 100,
                    f"Already present: {name}",
                )
                files_done.append(name)
                continue
            url = recommended_weight_url(name)
            logging.info("[SeedVR2 Weights] Downloading %s", url)
            _download_file(
                url,
                dest,
                expected_size=size_hint,
                progress_cb=progress_cb,
                should_stop=should_stop,
                file_index=i,
                file_count=total_files,
            )
            files_done.append(name)

        _emit(progress_cb, total_files * 100, total_files * 100, "Weights ready.")
        return {
            "ok": True,
            "path": str(root),
            "files": files_done,
            "message": (
                "SeedVR2 weights ready:\n"
                f"{root}\n\n"
                + "\n".join(f"• {n}" for n in files_done)
            ),
            "error": None,
        }
    except InterruptedError:
        return {
            "ok": False,
            "path": str(root),
            "files": [],
            "message": "Download cancelled.",
            "error": "aborted",
        }
    except Exception as exc:
        logging.exception("[SeedVR2 Weights] download failed")
        return {
            "ok": False,
            "path": str(root),
            "files": [],
            "message": f"SeedVR2 weights download failed:\n{exc}",
            "error": "failed",
        }
