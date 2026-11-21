"""
topaz_pipeline.py — Drive Topaz Photo AI via tpai.exe CLI (licensed local install).

Example (Photo AI 3.x)::

    tpai.exe --overwrite --override --format png --output OUTDIR \\
        --upscale model="High Fidelity V2" INPUT.png

Note: tpai.exe currently stringifies numeric CLI overrides (``scale=4`` →
``"scale": "4"``), which crashes the engine with json.exception.type_error.302.
Scale is therefore applied via Autopilot registry keys for the duration of the run.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from topaz_config import (
    DEFAULT_TOPAZ_MODEL,
    DEFAULT_TOPAZ_SCALE,
    LOGIN_REQUIRED_MESSAGE,
    find_tpai_exe,
    supports_image,
)

try:
    import winreg
except ImportError:  # non-Windows
    winreg = None  # type: ignore[assignment]

_TOPAZ_REG_PATH = r"Software\Topaz Labs LLC\Topaz Photo AI"
_REG_FACTOR = "autopilotUpscalingFactor"
_REG_TYPE = "autopilotUpscalingType"


def _creationflags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _reg_get_sz(key, name: str) -> str | None:
    try:
        value, typ = winreg.QueryValueEx(key, name)
        if typ in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            return str(value)
        return str(value)
    except OSError:
        return None


@contextmanager
def _temporary_autopilot_scale(scale: int) -> Iterator[bool]:
    """
    Point Topaz Autopilot at a fixed scale factor for one CLI run.

    Returns whether the registry was actually patched (False on non-Windows /
    missing key — caller still runs, scale then follows Autopilot defaults).
    """
    if winreg is None or os.name != "nt":
        yield False
        return

    scale_i = max(1, min(6, int(scale or DEFAULT_TOPAZ_SCALE)))
    key = None
    old_factor = None
    old_type = None
    patched = False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _TOPAZ_REG_PATH, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        )
        old_factor = _reg_get_sz(key, _REG_FACTOR)
        old_type = _reg_get_sz(key, _REG_TYPE)
        winreg.SetValueEx(key, _REG_FACTOR, 0, winreg.REG_SZ, str(scale_i))
        winreg.SetValueEx(key, _REG_TYPE, 0, winreg.REG_SZ, "scale")
        patched = True
        logging.debug(
            "[Topaz] Autopilot scale override via registry: factor=%s type=scale "
            "(was factor=%s type=%s)",
            scale_i,
            old_factor,
            old_type,
        )
        yield True
    except OSError as exc:
        logging.warning("[Topaz] Could not set Autopilot scale in registry: %s", exc)
        yield False
    finally:
        if key is not None and patched:
            try:
                if old_factor is not None:
                    winreg.SetValueEx(key, _REG_FACTOR, 0, winreg.REG_SZ, str(old_factor))
                if old_type is not None:
                    winreg.SetValueEx(key, _REG_TYPE, 0, winreg.REG_SZ, str(old_type))
            except OSError as exc:
                logging.warning("[Topaz] Failed to restore Autopilot registry: %s", exc)
            try:
                winreg.CloseKey(key)
            except OSError:
                pass


def _guess_output_name(input_path: str, out_dir: Path, fmt: str) -> Path:
    stem = Path(input_path).stem
    if fmt == "preserve":
        ext = Path(input_path).suffix or ".png"
    else:
        ext = f".{fmt.lstrip('.')}"
        if ext.lower() == ".jpeg":
            ext = ".jpg"
    # Prefer exact stem match; Topaz usually keeps the basename.
    direct = out_dir / f"{stem}{ext}"
    if direct.is_file():
        return direct
    # Fallback: newest file in out_dir
    candidates = [p for p in out_dir.iterdir() if p.is_file()]
    if not candidates:
        return direct
    return max(candidates, key=lambda p: p.stat().st_mtime)


def build_tpai_command(
    *,
    exe: Path,
    input_path: str,
    output_dir: str,
    scale: int = DEFAULT_TOPAZ_SCALE,
    model: str = DEFAULT_TOPAZ_MODEL,
    fmt: str = "png",
    overwrite: bool = True,
    quality: int = 95,
    extra_upscale: dict[str, str] | None = None,
) -> list[str]:
    # ``scale`` is intentionally unused on the CLI — see module docstring.
    _ = scale
    cmd: list[str] = [str(exe)]
    if overwrite:
        cmd.append("--overwrite")
    # Replace Autopilot filters so Face Recovery / etc. don't ride along.
    cmd.append("--override")
    cmd.extend(["--format", fmt])
    if fmt.lower() in ("jpg", "jpeg"):
        cmd.extend(["--quality", str(max(1, min(100, int(quality))))])
    cmd.extend(["--output", output_dir])

    # Only string-safe overrides (model name). Never pass scale/width/height/
    # param* — tpai stores them as JSON strings and the engine aborts.
    upscale_bits: list[str] = []
    model_name = (model or "").strip()
    if model_name:
        upscale_bits.append(f"model={model_name}")
    if extra_upscale:
        for key, val in extra_upscale.items():
            if not key or val is None or str(val) == "":
                continue
            if str(key).lower() in {
                "scale",
                "width",
                "height",
                "param1",
                "param2",
                "param3",
                "resolution",
            }:
                continue
            upscale_bits.append(f"{key}={val}")
    cmd.append("--upscale")
    cmd.extend(upscale_bits)
    cmd.append(os.path.abspath(input_path))
    return cmd


def upscale_image_file(
    input_path: str,
    output_path: str,
    *,
    scale: int = DEFAULT_TOPAZ_SCALE,
    model: str = DEFAULT_TOPAZ_MODEL,
    exe_override: str | None = None,
    fmt: str | None = None,
    quality: int = 95,
    progress_cb: Callable[[float, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """
    Run Topaz Photo AI CLI on one image and place the result at ``output_path``.

    Uses a temp output folder then moves/renames to the suggested path so we
    can apply Vibe's ``_topaz`` suffix without relying on Topaz naming.
    """
    if not supports_image(input_path):
        return {
            "ok": False,
            "output_path": None,
            "error": "unsupported",
            "message": "Topaz Photo AI supports common image formats only.",
        }

    exe = find_tpai_exe(exe_override)
    if exe is None:
        return {
            "ok": False,
            "output_path": None,
            "error": "topaz_missing",
            "message": "Topaz Photo AI (tpai.exe) was not found.",
        }

    if should_stop and should_stop():
        return {
            "ok": False,
            "output_path": None,
            "error": "aborted",
            "message": "Canceled.",
        }

    out_ext = os.path.splitext(output_path)[1].lower()
    if fmt:
        out_fmt = fmt.lower().lstrip(".")
    elif out_ext in (".jpg", ".jpeg"):
        out_fmt = "jpg"
    elif out_ext in (".tif", ".tiff"):
        out_fmt = "tiff"
    else:
        out_fmt = "png"

    scale_i = int(scale or DEFAULT_TOPAZ_SCALE)
    if scale_i < 1:
        scale_i = 1
    if scale_i > 6:
        scale_i = 6

    tmp_dir = tempfile.mkdtemp(prefix="vibe_topaz_")
    try:
        with _temporary_autopilot_scale(scale_i):
            if progress_cb:
                progress_cb(0.1, "Starting Topaz Photo AI…")

            cmd = build_tpai_command(
                exe=exe,
                input_path=input_path,
                output_dir=tmp_dir,
                scale=scale_i,
                model=model or DEFAULT_TOPAZ_MODEL,
                fmt=out_fmt,
                quality=quality,
            )
            logging.info("[Topaz] Running: %s", " ".join(cmd))

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_creationflags(),
            )

            log_lines: list[str] = []
            assert proc.stdout is not None
            for line in proc.stdout:
                text = (line or "").rstrip()
                if text:
                    log_lines.append(text)
                    logging.debug("[Topaz] %s", text)
                if should_stop and should_stop():
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return {
                        "ok": False,
                        "output_path": None,
                        "error": "aborted",
                        "message": "Canceled.",
                    }
                if progress_cb and len(log_lines) % 8 == 0:
                    progress_cb(0.4, "Topaz processing…")

            rc = proc.wait(timeout=3600)
            # Normalize unsigned 32-bit Windows NTSTATUS (e.g. 0xC0000409).
            rc_signed = rc if rc < 0x80000000 else rc - 0x100000000
            joined = "\n".join(log_lines[-40:])

            # Detect the known CLI JSON-type bug even when rc is odd.
            if "type must be number, but is string" in joined:
                return {
                    "ok": False,
                    "output_path": None,
                    "error": "runtime_error",
                    "message": (
                        "Topaz Photo AI rejected numeric CLI overrides "
                        "(known tpai.exe bug).\n"
                        "Retry — scale is applied via Autopilot settings.\n\n"
                        + joined
                    ),
                }

            # Return codes from tpai --help
            if rc in (254, -2) or rc_signed == -2:
                return {
                    "ok": False,
                    "output_path": None,
                    "error": "topaz_login",
                    "message": LOGIN_REQUIRED_MESSAGE,
                }
            if rc in (253, -3) or rc_signed == -3:
                return {
                    "ok": False,
                    "output_path": None,
                    "error": "invalid_args",
                    "message": f"Topaz CLI rejected arguments.\n\n{joined}",
                }

            produced = _guess_output_name(input_path, Path(tmp_dir), out_fmt)
            produced_ok = bool(
                produced.is_file() and produced.stat().st_size > 0
            )

            # tpai 3.x often finishes the image then crashes on shutdown
            # (STATUS_STACK_BUFFER_OVERRUN / 0xC0000409). Prefer the output
            # file over the exit code when one was written.
            if rc not in (0, 1) and not produced_ok:
                return {
                    "ok": False,
                    "output_path": None,
                    "error": "runtime_error",
                    "message": (
                        f"Topaz exited with code {rc} (signed {rc_signed}).\n\n"
                        + joined
                    ),
                }
            if not produced_ok:
                return {
                    "ok": False,
                    "output_path": None,
                    "error": "runtime_error",
                    "message": (
                        "Topaz finished but no output file was found.\n\n" + joined
                    ),
                }
            if rc not in (0, 1):
                logging.warning(
                    "[Topaz] tpai exited %s (signed %s) but output exists (%s, %s bytes) — treating as success",
                    rc,
                    rc_signed,
                    produced.name,
                    produced.stat().st_size,
                )

            if progress_cb:
                progress_cb(0.9, "Saving…")
            os.makedirs(
                os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True
            )
            if os.path.abspath(produced) != os.path.abspath(output_path):
                if os.path.isfile(output_path):
                    os.remove(output_path)
                shutil.move(str(produced), output_path)

            if progress_cb:
                progress_cb(1.0, "Done")
            return {
                "ok": True,
                "output_path": output_path,
                "error": None,
                "message": None,
                "scale": scale_i,
            }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "output_path": None,
            "error": "timeout",
            "message": "Topaz Photo AI timed out.",
        }
    except Exception as exc:
        logging.error("[Topaz] Upscale failed: %s", exc, exc_info=True)
        return {
            "ok": False,
            "output_path": None,
            "error": "runtime_error",
            "message": str(exc),
        }
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
