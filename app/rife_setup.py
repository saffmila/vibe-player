"""
rife_setup.py — One-click install of the optional rife-ncnn-vulkan pack.

Downloads the official Windows ZIP from nihui/rife-ncnn-vulkan, extracts it
into tools/rife/, and by default keeps only the preferred model (slim pack).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from rife_config import (
    PREFERRED_MODELS,
    RIFE_PROJECT_URL,
    RIFE_WINDOWS_ZIP_URL,
    default_rife_dir,
    runtime_status,
)

# Official Windows ZIP ships every model (~400 MB). Slim install keeps one model.
RIFE_PACK_DOWNLOAD_ESTIMATE = "~400 MB"
RIFE_PACK_DISK_ESTIMATE = "~40–80 MB (slim) / ~400 MB (all models)"
RIFE_PACK_HF_NOTE = f"{RIFE_PROJECT_URL}/releases"

# Public GitHub download accelerator (often faster than raw github.com).
GHPROXY_PREFIX = "https://ghfast.top/"

ProgressCb = Callable[[int, int, str], None]
StopCb = Callable[[], bool]

_CHUNK = 1024 * 1024  # 1 MiB
_UA = "VibePlayer-RIFE/1.0"
_ZIP_NAME = "rife-windows.zip"
# Progress budget: download 0..80, extract 80..90, install 90..100
_PROGRESS_TOTAL = 100


def mirrored_url(url: str) -> str:
    if url.startswith(GHPROXY_PREFIX):
        return url
    return GHPROXY_PREFIX + url


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
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
    progress_base: int = 0,
    progress_span: int = 80,
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
        total = existing + chunk_len if chunk_len else 0

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
                    pct = min(progress_span, int(done * progress_span / total))
                    _emit(
                        progress_cb,
                        progress_base + pct,
                        _PROGRESS_TOTAL,
                        f"{dest.name}: {_fmt_bytes(done)} / {_fmt_bytes(total)}",
                    )
                else:
                    _emit(
                        progress_cb,
                        progress_base,
                        _PROGRESS_TOTAL,
                        f"{dest.name}: {_fmt_bytes(done)}",
                    )

    if not partial.is_file() or partial.stat().st_size < 1_000_000:
        raise RuntimeError(f"Download incomplete or too small: {dest.name}")
    os.replace(partial, dest)


def pick_model_dirs(pack_root: Path, keep_all: bool) -> list[Path]:
    model_dirs = [
        p
        for p in pack_root.iterdir()
        if p.is_dir() and any(p.glob("*.param")) and any(p.glob("*.bin"))
    ]
    if keep_all or not model_dirs:
        return model_dirs
    names = {p.name for p in model_dirs}
    for preferred in PREFERRED_MODELS:
        if preferred in names:
            return [pack_root / preferred]
    return [sorted(model_dirs, key=lambda p: p.name)[0]]


def install_from_extracted(
    extract_dir: Path,
    out_dir: Path,
    *,
    keep_all: bool = False,
) -> None:
    candidates = [p for p in extract_dir.iterdir() if p.is_dir()]
    source = candidates[0] if len(candidates) == 1 else extract_dir
    exe = next(source.rglob("rife-ncnn-vulkan.exe"), None)
    if exe is None:
        raise RuntimeError("rife-ncnn-vulkan.exe not found in the archive.")

    pack_root = exe.parent
    keep_models = {p.name for p in pick_model_dirs(pack_root, keep_all)}
    logging.info("[RIFE Setup] Installing from %s → %s", pack_root, out_dir)
    if not keep_all:
        logging.info(
            "[RIFE Setup] Slim mode — keeping model(s): %s",
            ", ".join(sorted(keep_models)) or "(none)",
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    for item in pack_root.iterdir():
        if item.is_dir() and any(item.glob("*.param")) and any(item.glob("*.bin")):
            if item.name not in keep_models:
                continue
        dest = out_dir / item.name
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)


def install_rife_pack(
    target_dir: str | Path | None = None,
    *,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
    use_mirror: bool = False,
    keep_all_models: bool = False,
    zip_path: str | Path | None = None,
    url: str | None = None,
) -> dict:
    """
    Download (unless ``zip_path``) and install rife-ncnn-vulkan into ``target_dir``.

    Returns ``{ok, path, message, error, exe}``.
    """
    root = Path(target_dir or default_rife_dir()).resolve()
    source_url = url or RIFE_WINDOWS_ZIP_URL
    if use_mirror:
        source_url = mirrored_url(source_url)

    try:
        root.mkdir(parents=True, exist_ok=True)
        _emit(progress_cb, 0, _PROGRESS_TOTAL, f"Saving RIFE pack to:\n{root}")

        with tempfile.TemporaryDirectory(prefix="vibe_rife_dl_") as tmp:
            tmp_path = Path(tmp)
            local_zip: Path | None = None
            if zip_path:
                local_zip = Path(zip_path).expanduser().resolve()
                if not local_zip.is_file():
                    raise FileNotFoundError(f"ZIP not found: {local_zip}")
                _emit(progress_cb, 80, _PROGRESS_TOTAL, f"Using local ZIP:\n{local_zip}")
            else:
                zip_dest = tmp_path / _ZIP_NAME
                urls = [source_url]
                if not use_mirror and not source_url.startswith(GHPROXY_PREFIX):
                    urls.append(mirrored_url(source_url))

                last_err: Exception | None = None
                for attempt, candidate in enumerate(urls):
                    if _stopped(should_stop):
                        raise InterruptedError("Download cancelled.")
                    if attempt:
                        _emit(
                            progress_cb,
                            0,
                            _PROGRESS_TOTAL,
                            f"Retry via mirror…\n{candidate}",
                        )
                    try:
                        logging.info("[RIFE Setup] Downloading %s", candidate)
                        _download_file(
                            candidate,
                            zip_dest,
                            progress_cb=progress_cb,
                            should_stop=should_stop,
                        )
                        local_zip = zip_dest
                        break
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        last_err = exc
                        logging.warning("[RIFE Setup] Download failed: %s", exc)
                        if zip_dest.exists():
                            try:
                                zip_dest.unlink()
                            except OSError:
                                pass
                if local_zip is None:
                    raise RuntimeError(f"Download failed. Last error: {last_err}")

            if _stopped(should_stop):
                raise InterruptedError("Download cancelled.")

            extract_dir = tmp_path / "extracted"
            extract_dir.mkdir()
            _emit(progress_cb, 82, _PROGRESS_TOTAL, "Extracting archive…")
            with zipfile.ZipFile(local_zip, "r") as zf:
                members = zf.namelist()
                for i, name in enumerate(members):
                    if _stopped(should_stop):
                        raise InterruptedError("Download cancelled.")
                    zf.extract(name, extract_dir)
                    if members and i % 20 == 0:
                        pct = 82 + int((i + 1) * 8 / len(members))
                        _emit(
                            progress_cb,
                            min(89, pct),
                            _PROGRESS_TOTAL,
                            f"Extracting… ({i + 1}/{len(members)})",
                        )

            if _stopped(should_stop):
                raise InterruptedError("Download cancelled.")

            _emit(progress_cb, 92, _PROGRESS_TOTAL, f"Installing into:\n{root}")
            install_from_extracted(
                extract_dir,
                root,
                keep_all=bool(keep_all_models),
            )

        final_exe = root / "rife-ncnn-vulkan.exe"
        if not final_exe.is_file():
            raise RuntimeError(f"Install incomplete — missing {final_exe}")

        status = runtime_status()
        if not status.get("ready"):
            raise RuntimeError(status.get("message") or "RIFE pack installed but not ready.")

        total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        _emit(progress_cb, _PROGRESS_TOTAL, _PROGRESS_TOTAL, "RIFE pack ready.")
        return {
            "ok": True,
            "path": str(root),
            "exe": str(final_exe),
            "message": (
                "RIFE pack ready:\n"
                f"{final_exe}\n\n"
                f"Installed size: {_fmt_bytes(total)}\n"
                f"Models: {', '.join(status.get('models') or []) or '(none)'}"
            ),
            "error": None,
        }
    except InterruptedError:
        return {
            "ok": False,
            "path": str(root),
            "exe": None,
            "message": "Download cancelled.",
            "error": "aborted",
        }
    except Exception as exc:
        logging.exception("[RIFE Setup] install failed")
        return {
            "ok": False,
            "path": str(root),
            "exe": None,
            "message": f"RIFE pack install failed:\n{exc}",
            "error": "failed",
        }
