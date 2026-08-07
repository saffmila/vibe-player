"""
seedvr2_runner_setup.py — One-click SeedVR2 runner install.

Downloads the pinned ComfyUI-SeedVR2 CLI checkout (GitHub zipball — no git
required), creates an isolated ``.venv``, and installs PyTorch CUDA + deps.

Works for source / frozen build / Pinokio as long as a real CPython 3.10–3.12
interpreter is available to create the venv.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from seedvr2_config import COMFY_REPO_URL, default_setup_runner_dir, detect_runner


# Pin to a released tag (not floating main). Bump intentionally when upgrading.
SEEDVR2_RUNNER_REF = "v2.5.23"
SEEDVR2_RUNNER_REPO = "numz/ComfyUI-SeedVR2_VideoUpscaler"
SEEDVR2_TORCH_INDEX = "https://download.pytorch.org/whl/cu130"

# Rough disk budget shown in the confirm UI (sources + CUDA torch + venv).
SEEDVR2_SETUP_DISK_ESTIMATE = "~6–8 GB"

ProgressCb = Callable[[int, int, str], None]
StopCb = Callable[[], bool]

_SETUP_STEPS = 5

__all__ = [
    "SEEDVR2_RUNNER_REF",
    "SEEDVR2_RUNNER_REPO",
    "SEEDVR2_SETUP_DISK_ESTIMATE",
    "default_setup_runner_dir",
    "find_venv_base_python",
    "runner_archive_url",
    "runner_venv_ready",
    "setup_seedvr2_runner",
]


def runner_archive_url(ref: str = SEEDVR2_RUNNER_REF) -> str:
    ref = (ref or SEEDVR2_RUNNER_REF).strip()
    if ref.startswith("v") or "/" in ref or ref.startswith("refs/"):
        return f"https://github.com/{SEEDVR2_RUNNER_REPO}/archive/refs/tags/{ref}.zip"
    # Bare commit SHA
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
        return f"https://github.com/{SEEDVR2_RUNNER_REPO}/archive/{ref}.zip"
    return f"https://github.com/{SEEDVR2_RUNNER_REPO}/archive/refs/heads/{ref}.zip"


def _emit(progress_cb: ProgressCb | None, step: int, detail: str) -> None:
    if progress_cb:
        progress_cb(step, _SETUP_STEPS, detail)


def _stopped(should_stop: StopCb | None) -> bool:
    return bool(should_stop and should_stop())


def find_venv_base_python() -> str | None:
    """
    Resolve a real CPython suitable for ``python -m venv``.

    Frozen builds cannot use ``sys.executable`` (the app EXE). Prefer an
    existing interpreter on PATH / Pinokio env / nearby project env.
    Prefer 3.10–3.12 (best PyTorch wheel support), then 3.13.
    """
    candidates: list[str] = []

    if not getattr(sys, "frozen", False) and sys.executable:
        candidates.append(sys.executable)

    # Pinokio / project-local envs next to the app.
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
                candidates.append(str(cand))

    # Windows py launcher — prefer 3.12 / 3.11 / 3.10 (torch wheels).
    if os.name == "nt":
        for ver in ("3.12", "3.11", "3.10", "3.13"):
            try:
                out = subprocess.check_output(
                    ["py", f"-{ver}", "-c", "import sys; print(sys.executable)"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=15,
                ).strip()
                if out:
                    candidates.append(out)
            except (OSError, subprocess.SubprocessError):
                pass

    # PATH lookup
    for name in ("python3.12", "python3.11", "python3.10", "python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            key = os.path.normcase(os.path.abspath(path))
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        if not _python_can_make_venv(path):
            continue
        ver = _python_version_tuple(path)
        if ver is None:
            continue
        major, minor = ver
        if major != 3 or minor < 10 or minor > 13:
            continue
        # Lower score = better. Prefer 3.12, 3.11, 3.10, then 3.13.
        pref = {12: 0, 11: 1, 10: 2, 13: 3}.get(minor, 9)
        scored.append((pref, path))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0])
    return scored[0][1]


def _python_version_tuple(python_exe: str) -> tuple[int, int] | None:
    try:
        out = subprocess.check_output(
            [python_exe, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        ).strip()
        major_s, minor_s = out.split(".", 1)
        return int(major_s), int(minor_s)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _python_version_ok(python_exe: str) -> bool:
    ver = _python_version_tuple(python_exe)
    return bool(ver and ver[0] == 3 and 10 <= ver[1] <= 13)


def _python_can_make_venv(python_exe: str) -> bool:
    if not python_exe or not Path(python_exe).is_file():
        return False
    # Reject our own frozen EXE.
    if getattr(sys, "frozen", False):
        try:
            if Path(python_exe).resolve() == Path(sys.executable).resolve():
                return False
        except OSError:
            pass
    try:
        subprocess.check_output(
            [python_exe, "-c", "import venv, ensurepip; print('ok')"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _venv_python(runner_dir: Path) -> Path:
    if os.name == "nt":
        return runner_dir / ".venv" / "Scripts" / "python.exe"
    return runner_dir / ".venv" / "bin" / "python"


def _download_file(url: str, dest: Path, should_stop: StopCb | None = None) -> None:
    req = Request(url, headers={"User-Agent": "VibePlayer-SeedVR2-Setup"})
    with urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        while True:
            if _stopped(should_stop):
                raise InterruptedError("Setup cancelled.")
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)


def _extract_github_zip(zip_path: Path, target_dir: Path) -> None:
    """Extract GitHub archive so ``inference_cli.py`` lands in ``target_dir``."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if not names:
            raise RuntimeError("Downloaded archive is empty.")
        top = names[0].split("/")[0]
        target_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="seedvr2_extract_") as tmp:
            zf.extractall(tmp)
            src_root = Path(tmp) / top
            if not src_root.is_dir():
                # Flat archive fallback
                src_root = Path(tmp)
            # Copy tree into target (keep existing .venv if present).
            for item in src_root.iterdir():
                dest = target_dir / item.name
                if item.name in {".venv", "venv", "models"} and dest.exists():
                    continue
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)


def _filter_requirements(req_path: Path) -> list[str]:
    """Drop torch/torchvision lines — installed separately from the CUDA index."""
    skip = {"torch", "torchvision", "torchaudio"}
    lines: list[str] = []
    for raw in req_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~\[]", line, maxsplit=1)[0].strip().lower()
        if name in skip:
            continue
        lines.append(line)
    return lines


def _run_pip(python_exe: Path, args: list[str], should_stop: StopCb | None = None) -> None:
    if _stopped(should_stop):
        raise InterruptedError("Setup cancelled.")
    cmd = [str(python_exe), "-m", "pip", *args]
    logging.info("[SeedVR2 Setup] %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60 * 45,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise RuntimeError(f"pip failed ({proc.returncode}):\n{tail}")


def ensure_runner_checkout(
    target_dir: str | Path,
    *,
    ref: str = SEEDVR2_RUNNER_REF,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
) -> Path:
    """Download/extract runner sources into ``target_dir`` if needed."""
    root = Path(target_dir)
    root.mkdir(parents=True, exist_ok=True)

    info = detect_runner(str(root))
    if info and info.get("kind") == "comfy":
        _emit(progress_cb, 2, "Runner sources already present.")
        return Path(info["root"])

    if _stopped(should_stop):
        raise InterruptedError("Setup cancelled.")

    _emit(progress_cb, 1, f"Downloading SeedVR2 runner ({ref})…")
    url = runner_archive_url(ref)
    with tempfile.TemporaryDirectory(prefix="seedvr2_dl_") as tmp:
        zip_path = Path(tmp) / "runner.zip"
        try:
            _download_file(url, zip_path, should_stop=should_stop)
        except Exception as exc:
            # Tag might 404 on older mirrors — fall back to main branch zip.
            if ref != "main":
                logging.warning("[SeedVR2 Setup] Download of %s failed (%s); trying main.", ref, exc)
                _emit(progress_cb, 1, "Tagged release unavailable — downloading main…")
                _download_file(runner_archive_url("main"), zip_path, should_stop=should_stop)
            else:
                raise

        if _stopped(should_stop):
            raise InterruptedError("Setup cancelled.")

        _emit(progress_cb, 2, "Extracting runner…")
        _extract_github_zip(zip_path, root)

    info = detect_runner(str(root))
    if not info or info.get("kind") != "comfy":
        raise RuntimeError(
            f"Runner extract failed — inference_cli.py not found in:\n{root}\n"
            f"Expected checkout from {COMFY_REPO_URL}"
        )
    return Path(info["root"])


def runner_venv_ready(runner_root: str | Path) -> bool:
    """True when runner checkout has a .venv that can ``import torch``."""
    return _runner_venv_ready(Path(runner_root))


def _runner_venv_ready(runner_root: Path) -> bool:
    vpy = _venv_python(runner_root)
    if not vpy.is_file():
        return False
    if not (runner_root / "inference_cli.py").is_file():
        return False
    try:
        subprocess.check_output(
            [str(vpy), "-c", "import torch"],
            cwd=str(runner_root),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=60,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_runner_venv(
    runner_root: str | Path,
    *,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
    base_python: str | None = None,
    force_reinstall: bool = False,
) -> Path:
    """Create ``.venv`` under runner and install CUDA torch + requirements."""
    root = Path(runner_root)
    vpy = _venv_python(root)
    req = root / "requirements.txt"

    if not force_reinstall and _runner_venv_ready(root):
        _emit(progress_cb, 5, "Runner environment already ready.")
        return vpy

    if not vpy.is_file():
        _emit(progress_cb, 3, "Creating Python virtual environment…")
        base = base_python or find_venv_base_python()
        if not base:
            raise RuntimeError(
                "No suitable Python 3.10–3.12 found to create the runner venv.\n\n"
                "Install Python from https://www.python.org/downloads/ "
                "(enable “Add python.exe to PATH”), then try Setup runner again.\n\n"
                "Pinokio users: finish the app Install step first so the managed "
                "env exists."
            )
        if _stopped(should_stop):
            raise InterruptedError("Setup cancelled.")
        proc = subprocess.run(
            [base, "-m", "venv", str(root / ".venv")],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0 or not vpy.is_file():
            tail = (proc.stderr or proc.stdout or "").strip()[-800:]
            raise RuntimeError(f"Failed to create .venv:\n{tail}")
    else:
        _emit(progress_cb, 3, "Virtual environment already exists.")

    if _stopped(should_stop):
        raise InterruptedError("Setup cancelled.")

    _emit(progress_cb, 4, "Installing PyTorch (CUDA) + SeedVR2 dependencies…")
    _run_pip(vpy, ["install", "--upgrade", "pip", "setuptools", "wheel"], should_stop)
    _run_pip(
        vpy,
        [
            "install",
            "torch==2.9.1",
            "torchvision==0.24.1",
            "torchaudio==2.9.1",
            "--index-url",
            SEEDVR2_TORCH_INDEX,
        ],
        should_stop,
    )
    # Windows Triton build used by ComfyUI / RTX 50-series stacks.
    try:
        _run_pip(vpy, ["install", "triton-windows"], should_stop)
    except Exception as exc:
        logging.warning("[SeedVR2 Setup] triton-windows install skipped: %s", exc)
    if req.is_file():
        pkgs = _filter_requirements(req)
        if pkgs:
            _run_pip(vpy, ["install", *pkgs], should_stop)
    else:
        logging.warning("[SeedVR2 Setup] No requirements.txt in %s", root)

    _emit(progress_cb, 5, "Verifying runner…")
    try:
        subprocess.check_output(
            [str(vpy), "-c", "import torch; print(torch.__version__)"],
            cwd=str(root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "Runner venv was created but PyTorch import failed.\n"
            f"{(exc.output or '')[-800:]}"
        ) from exc

    return vpy


def setup_seedvr2_runner(
    target_dir: str | Path,
    *,
    ref: str = SEEDVR2_RUNNER_REF,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
    force_reinstall: bool = False,
) -> dict:
    """
    Full setup: download sources + venv + deps.

    Returns ``{ok, path, python, message, error}``.
    """
    try:
        _emit(progress_cb, 0, "Preparing…")
        root = ensure_runner_checkout(
            target_dir,
            ref=ref,
            progress_cb=progress_cb,
            should_stop=should_stop,
        )
        vpy = ensure_runner_venv(
            root,
            progress_cb=progress_cb,
            should_stop=should_stop,
            force_reinstall=force_reinstall,
        )
        _emit(progress_cb, 5, "SeedVR2 runner is ready.")
        return {
            "ok": True,
            "path": str(root),
            "python": str(vpy),
            "message": f"SeedVR2 runner ready:\n{root}",
            "error": None,
        }
    except InterruptedError:
        return {
            "ok": False,
            "path": str(target_dir),
            "python": None,
            "message": "Setup cancelled.",
            "error": "aborted",
        }
    except Exception as exc:
        logging.exception("[SeedVR2 Setup] Failed")
        return {
            "ok": False,
            "path": str(target_dir),
            "python": None,
            "message": f"SeedVR2 runner setup failed:\n{exc}",
            "error": "setup_failed",
        }
