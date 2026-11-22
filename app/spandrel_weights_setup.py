"""
spandrel_weights_setup.py — One-click download of built-in Upscale models.

Pulls curated Spandrel weights from author GitHub Releases / Hugging Face into
``models/upscale/``. Same pattern as SeedVR2: plain HTTPS, resume, progress UI.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from spandrel_config import default_upscale_dir

# filename, bytes, sha256, url, source label
RECOMMENDED_MODELS: tuple[dict[str, Any], ...] = (
    {
        "filename": "4xNomos8kHAT-L_otf.pth",
        "size": 165_817_474,
        "sha256": "16c10572caf29b2381e75821364ef499a582039c5baeb6a5f9fc12d6366163ca",
        "url": (
            "https://github.com/Phhofm/models/releases/download/"
            "4xNomos8kHAT-L_otf/4xNomos8kHAT-L_otf.pth"
        ),
        "source": "GitHub · Phhofm/models",
    },
    {
        "filename": "4xNomosWebPhoto_RealPLKSR.pth",
        "size": 29_683_482,
        "sha256": "a9db66c9b674c6a5025b6ef3bee71a57c33b8605d8a2de0980470f89002efbbe",
        "url": (
            "https://github.com/Phhofm/models/releases/download/"
            "4xNomosWebPhoto_RealPLKSR/4xNomosWebPhoto_RealPLKSR.pth"
        ),
        "source": "GitHub · Phhofm/models",
    },
    {
        "filename": "4x-ClearRealityV1.pth",
        "size": 9_016_074,
        "sha256": "a4cd3a25b00e0be949d4302fc774eb4d7f2ed5f47cdb51551e2d75fa6562e51e",
        "url": (
            "https://huggingface.co/Kim2091/ClearRealityV1/resolve/main/"
            "4x-ClearRealityV1.pth"
        ),
        "source": "Hugging Face · Kim2091/ClearRealityV1",
    },
    {
        "filename": "4x-UltraSharp.pth",
        "size": 66_961_958,
        "sha256": "a5812231fc936b42af08a5edba784195495d303d5b3248c24489ef0c4021fe01",
        "url": (
            "https://huggingface.co/Kim2091/UltraSharp/resolve/main/"
            "4x-UltraSharp.pth"
        ),
        "source": "Hugging Face · Kim2091/UltraSharp",
    },
)

SPANDREL_WEIGHTS_DISK_ESTIMATE = "~260 MB"

ProgressCb = Callable[[int, int, str], None]
StopCb = Callable[[], bool]

_CHUNK = 1024 * 1024  # 1 MiB
_UA = "VibePlayer-Spandrel/1.0"
_MIN_BYTES = 512 * 1024


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


def recommended_by_filename(filename: str | None) -> dict[str, Any] | None:
    want = os.path.basename(str(filename or "").replace("\\", "/")).strip().lower()
    if not want:
        return None
    for item in RECOMMENDED_MODELS:
        if str(item.get("filename") or "").strip().lower() == want:
            return dict(item)
    return None


def find_local_model(models_dir: str | Path | None, filename: str | None) -> Path | None:
    """Return an existing weight path (root or nested) matching ``filename``."""
    name = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    if not name:
        return None
    root = Path(models_dir or default_upscale_dir())
    direct = root / name
    if direct.is_file() and direct.stat().st_size >= _MIN_BYTES:
        return direct.resolve()
    if not root.is_dir():
        return None
    want = name.casefold()
    try:
        for path in root.rglob("*"):
            if path.is_file() and path.name.casefold() == want:
                if path.stat().st_size >= _MIN_BYTES:
                    return path.resolve()
    except OSError:
        return None
    return None


def missing_recommended(
    models_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    root = Path(models_dir or default_upscale_dir())
    return [
        dict(item)
        for item in RECOMMENDED_MODELS
        if find_local_model(root, str(item.get("filename") or "")) is None
    ]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _download_file(
    url: str,
    dest: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
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

    min_ok = min(_MIN_BYTES, int(expected_size or _MIN_BYTES))
    if not partial.is_file() or partial.stat().st_size < min_ok:
        raise RuntimeError(f"Download incomplete or too small: {dest.name}")

    want_sha = str(expected_sha256 or "").strip().lower()
    if want_sha:
        _emit(
            progress_cb,
            file_index * 100 + 99,
            file_count * 100,
            f"{dest.name}: verifying…",
        )
        got = _sha256_file(partial).lower()
        if got != want_sha:
            try:
                partial.unlink(missing_ok=True)
            except TypeError:
                if partial.is_file():
                    partial.unlink()
            raise RuntimeError(
                f"Checksum mismatch for {dest.name}.\n"
                f"Expected {want_sha[:12]}…, got {got[:12]}…"
            )

    os.replace(partial, dest)


def download_model_file(
    filename: str,
    target_dir: str | Path | None = None,
    *,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
    force: bool = False,
) -> dict:
    """Download one curated model by filename into ``target_dir``."""
    meta = recommended_by_filename(filename)
    root = Path(target_dir or default_upscale_dir())
    if meta is None:
        return {
            "ok": False,
            "path": str(root),
            "files": [],
            "message": f"No download URL for “{filename}”.",
            "error": "unknown",
        }
    return download_recommended_weights(
        root,
        progress_cb=progress_cb,
        should_stop=should_stop,
        force=force,
        only_filenames=[str(meta["filename"])],
    )


def download_recommended_weights(
    target_dir: str | Path | None = None,
    *,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
    force: bool = False,
    only_filenames: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """
    Download curated Upscale models into ``target_dir``.

    Returns ``{ok, path, message, error, files}``.
    """
    root = Path(target_dir or default_upscale_dir())
    try:
        root.mkdir(parents=True, exist_ok=True)
        if only_filenames:
            wanted = {
                os.path.basename(str(n or "").replace("\\", "/")).strip().lower()
                for n in only_filenames
                if str(n or "").strip()
            }
            catalog = [
                item
                for item in RECOMMENDED_MODELS
                if str(item.get("filename") or "").strip().lower() in wanted
            ]
        else:
            catalog = list(RECOMMENDED_MODELS)
        if not catalog:
            return {
                "ok": False,
                "path": str(root),
                "files": [],
                "message": "No matching models to download.",
                "error": "unknown",
            }

        files_done: list[str] = []
        total_files = len(catalog)
        _emit(progress_cb, 0, total_files * 100, f"Saving models to:\n{root}")

        for i, item in enumerate(catalog):
            if _stopped(should_stop):
                raise InterruptedError("Download cancelled.")
            name = str(item.get("filename") or "").strip()
            url = str(item.get("url") or "").strip()
            size_hint = int(item.get("size") or 0)
            sha = str(item.get("sha256") or "").strip()
            dest = root / name
            existing = find_local_model(root, name)
            if existing is not None and not force:
                _emit(
                    progress_cb,
                    (i + 1) * 100,
                    total_files * 100,
                    f"Already present: {name}",
                )
                files_done.append(name)
                continue
            if not url:
                raise RuntimeError(f"Missing URL for {name}")
            logging.info("[Spandrel Weights] Downloading %s", url)
            _download_file(
                url,
                dest,
                expected_size=size_hint or None,
                expected_sha256=sha or None,
                progress_cb=progress_cb,
                should_stop=should_stop,
                file_index=i,
                file_count=total_files,
            )
            files_done.append(name)

        _emit(progress_cb, total_files * 100, total_files * 100, "Models ready.")
        return {
            "ok": True,
            "path": str(root),
            "files": files_done,
            "message": (
                "Upscale models ready:\n"
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
        logging.exception("[Spandrel Weights] download failed")
        return {
            "ok": False,
            "path": str(root),
            "files": [],
            "message": f"Upscale model download failed:\n{exc}",
            "error": "failed",
        }
