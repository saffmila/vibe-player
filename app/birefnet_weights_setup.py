"""
birefnet_weights_setup.py — Download BiRefNet weights into models/birefnet/.

Uses ``transformers`` (already in the app deps) — no huggingface_hub required.
Also installs missing Python / CUDA PyTorch packages on demand from the UI.
"""

from __future__ import annotations

import importlib
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

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

# Packages required by BiRefNet trust_remote_code modeling.
BIREFNET_PIP_DEPS: tuple[str, ...] = ("einops", "kornia")

# Same CUDA stack as requirements.txt / SeedVR runner setup.
BIREFNET_TORCH_INDEX = "https://download.pytorch.org/whl/cu130"
BIREFNET_TORCH_PACKAGES: tuple[str, ...] = (
    "torch==2.9.1",
    "torchvision==0.24.1",
    "torchaudio==2.9.1",
)
BIREFNET_TORCH_DISK_ESTIMATE = "~2–3 GB"
BIREFNET_TORCH_TIME_HINT = "several minutes"


def _ensure_birefnet_deps() -> None:
    deps = python_deps_status()
    if not deps.get("ready"):
        raise RuntimeError(deps.get("message") or "Missing BiRefNet Python dependencies.")


def resolve_birefnet_pip_python() -> str | None:
    """Interpreter that owns the running app packages (for ``python -m pip``)."""
    if not getattr(sys, "frozen", False) and sys.executable:
        return sys.executable

    here = Path(__file__).resolve().parent
    roots = [
        here.parent,
        Path(sys.executable).resolve().parent,
        Path(sys.executable).resolve().parent.parent,
        Path.cwd(),
    ]
    for root in roots:
        for rel in (
            Path("env") / "Scripts" / "python.exe",
            Path("env") / "bin" / "python",
            Path(".venv") / "Scripts" / "python.exe",
            Path(".venv") / "bin" / "python",
        ):
            cand = root / rel
            if cand.is_file():
                return str(cand)
    return None


def _purge_import_modules(*roots: str) -> None:
    """Drop packages from ``sys.modules`` so a fresh import can pick up new wheels."""
    importlib.invalidate_caches()
    for root in roots:
        sys.modules.pop(root, None)
        prefix = root + "."
        for key in list(sys.modules):
            if key == root or key.startswith(prefix):
                sys.modules.pop(key, None)


def _terminate_pip(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_pip_cancelable(
    python_exe: str,
    pip_args: list[str],
    *,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
    step: int = 0,
    total: int = 1,
    detail: str = "Installing…",
    timeout_s: int = 60 * 45,
) -> dict[str, Any]:
    """Run ``python -m pip …`` with Cancel support (terminate process)."""
    if should_stop and should_stop():
        return {"ok": False, "message": "Install cancelled.", "cancelled": True}

    cmd = [python_exe, "-m", "pip", *pip_args]
    logging.info("[BiRefNet] %s", " ".join(cmd))
    if progress_cb:
        progress_cb(step, total, detail)

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(Path(python_exe).resolve().parent),
            env={**os.environ},
            creationflags=creationflags,
        )
    except OSError as exc:
        return {"ok": False, "message": f"Could not run pip:\n{exc}", "cancelled": False}

    output: list[str] = []
    deadline = time.monotonic() + max(60, int(timeout_s))
    try:
        assert proc.stdout is not None
        while True:
            if should_stop and should_stop():
                _terminate_pip(proc)
                return {
                    "ok": False,
                    "message": "Install cancelled.",
                    "cancelled": True,
                }
            if time.monotonic() > deadline:
                _terminate_pip(proc)
                return {
                    "ok": False,
                    "message": "pip install timed out.",
                    "cancelled": False,
                }
            line = proc.stdout.readline()
            if line:
                output.append(line)
                text = line.strip()
                if text and progress_cb:
                    progress_cb(step, total, text[:120])
            elif proc.poll() is not None:
                break
            else:
                time.sleep(0.15)
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass

    code = proc.wait(timeout=5) if proc.poll() is None else proc.returncode
    if code != 0:
        tail = "".join(output).strip()
        if len(tail) > 1200:
            tail = tail[-1200:]
        return {
            "ok": False,
            "message": f"pip failed ({code}):\n{tail}",
            "cancelled": False,
        }
    return {"ok": True, "message": None, "cancelled": False}


def install_birefnet_python_deps(
    packages: list[str] | None = None,
    *,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
) -> dict[str, Any]:
    """
    ``pip install`` missing BiRefNet Python packages into the app environment.

    Returns ``{"ok": True}`` or ``{"ok": False, "message": ...}``.
    """
    deps = python_deps_status()
    missing = list(packages or deps.get("missing") or [])
    if not missing:
        if deps.get("ready"):
            return {"ok": True, "message": "Dependencies already installed.", "missing": []}
        missing = list(BIREFNET_PIP_DEPS)

    python_exe = resolve_birefnet_pip_python()
    if not python_exe:
        return {
            "ok": False,
            "message": (
                "Cannot install packages automatically in this build.\n"
                f"Install manually: pip install {' '.join(missing)}"
            ),
            "missing": missing,
        }

    result = _run_pip_cancelable(
        python_exe,
        ["install", "--disable-pip-version-check", *missing],
        progress_cb=progress_cb,
        should_stop=should_stop,
        step=0,
        total=1,
        detail=f"Installing {' '.join(missing)}…",
        timeout_s=300,
    )
    if not result.get("ok"):
        result["missing"] = missing
        return result

    _purge_import_modules(*missing)
    check = python_deps_status()
    if not check.get("ready"):
        return {
            "ok": False,
            "message": check.get("message")
            or "Packages installed but still not importable. Restart the app.",
            "missing": check.get("missing") or missing,
        }

    if progress_cb:
        progress_cb(1, 1, "Dependencies ready.")
    return {
        "ok": True,
        "message": f"Installed: {', '.join(missing)}",
        "missing": [],
    }


def install_birefnet_torch_cuda(
    *,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
) -> dict[str, Any]:
    """
    Install CUDA PyTorch wheels (cu130) into the app environment.

    Also ensures einops/kornia. May require an app restart before CUDA is usable
    in the current process (Windows DLL lock).
    """
    python_exe = resolve_birefnet_pip_python()
    if not python_exe:
        return {
            "ok": False,
            "message": (
                "Cannot install PyTorch automatically in this build.\n"
                "Portable users: extract the Autotag GPU pack over VibePlayer/.\n"
                "Dev: pip install -r requirements.txt"
            ),
            "restart_required": False,
        }

    deps = python_deps_status()
    if not deps.get("ready"):
        dep_result = install_birefnet_python_deps(
            list(deps.get("missing") or []),
            progress_cb=progress_cb,
            should_stop=should_stop,
        )
        if not dep_result.get("ok"):
            return {
                "ok": False,
                "message": dep_result.get("message"),
                "restart_required": False,
                "cancelled": bool(dep_result.get("cancelled")),
            }

    if should_stop and should_stop():
        return {
            "ok": False,
            "message": "Install cancelled.",
            "restart_required": False,
            "cancelled": True,
        }

    torch_result = _run_pip_cancelable(
        python_exe,
        [
            "install",
            "--disable-pip-version-check",
            *BIREFNET_TORCH_PACKAGES,
            "--index-url",
            BIREFNET_TORCH_INDEX,
        ],
        progress_cb=progress_cb,
        should_stop=should_stop,
        step=0,
        total=1,
        detail=f"Downloading PyTorch CUDA ({BIREFNET_TORCH_DISK_ESTIMATE})…",
        timeout_s=60 * 45,
    )
    if not torch_result.get("ok"):
        return {
            "ok": False,
            "message": torch_result.get("message"),
            "restart_required": False,
            "cancelled": bool(torch_result.get("cancelled")),
        }

    if progress_cb:
        progress_cb(1, 1, "Packages installed. Verifying in a fresh process…")

    # Never import the new Torch into THIS process — Windows often keeps the
    # old c10_cuda.dll mapped and raises WinError 127. Probe via subprocess.
    probe = _probe_torch_cuda_subprocess(python_exe)
    if probe.get("ready"):
        if progress_cb:
            progress_cb(1, 1, f"PyTorch CUDA ready ({probe.get('torch') or 'ok'}).")
        return {
            "ok": True,
            "message": "PyTorch CUDA installed.",
            # Still restart: this app process may have loaded the old Torch.
            "restart_required": True,
        }

    # Pip said success but probe failed — still ask for restart / show detail.
    detail = probe.get("message") or "Could not verify CUDA Torch in a fresh process."
    return {
        "ok": True,
        "message": (
            "PyTorch packages were installed, but verification failed:\n"
            f"{detail}\n\n"
            "Restart Vibe Player and try again. If it still fails, update NVIDIA drivers."
        ),
        "restart_required": True,
    }


def _probe_torch_cuda_subprocess(python_exe: str) -> dict[str, Any]:
    """Import torch in a child process (safe after in-place wheel swap)."""
    code = (
        "import torch\n"
        "print(torch.__version__)\n"
        "print('1' if torch.cuda.is_available() else '0')\n"
        "print(getattr(torch.version, 'cuda', '') or '')\n"
    )
    try:
        proc = subprocess.run(
            [python_exe, "-c", code],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(Path(python_exe).resolve().parent),
            env={**os.environ},
        )
    except Exception as exc:
        return {"ready": False, "message": str(exc)}

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()
        if len(tail) > 600:
            tail = tail[-600:]
        return {"ready": False, "message": tail or f"exit {proc.returncode}"}

    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    ver = lines[0] if lines else ""
    cuda_ok = len(lines) > 1 and lines[1] == "1"
    if not cuda_ok:
        return {
            "ready": False,
            "message": (
                f"torch {ver or '?'} imported but CUDA is not available "
                f"(cuda={lines[2] if len(lines) > 2 else '?'})."
            ),
            "torch": ver,
        }
    return {"ready": True, "torch": ver, "message": None}


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
