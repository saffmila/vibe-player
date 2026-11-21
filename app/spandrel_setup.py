"""
spandrel_setup.py — On-demand ``pip install spandrel`` into the app environment.

Same interpreter resolution as BiRefNet (dev venv / portable env).
"""

from __future__ import annotations

from typing import Any, Callable

from birefnet_weights_setup import (
    _purge_import_modules,
    _run_pip_cancelable,
    resolve_birefnet_pip_python,
)
from spandrel_config import SPANDREL_MISSING_MESSAGE, spandrel_import_status

ProgressCb = Callable[[int, int, str], None]
StopCb = Callable[[], bool]

SPANDREL_PIP_PACKAGE = "spandrel>=0.4.1"


def install_spandrel(
    *,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
) -> dict[str, Any]:
    """Install Spandrel into the running app's Python (dev / portable env)."""
    status = spandrel_import_status()
    if status.get("ready"):
        return {"ok": True, "message": "Spandrel is already installed."}

    python_exe = resolve_birefnet_pip_python()
    if not python_exe:
        return {
            "ok": False,
            "message": (
                "Cannot install packages automatically in this portable build.\n\n"
                f"{SPANDREL_MISSING_MESSAGE}"
            ),
        }

    result = _run_pip_cancelable(
        python_exe,
        ["install", "--disable-pip-version-check", SPANDREL_PIP_PACKAGE],
        progress_cb=progress_cb,
        should_stop=should_stop,
        step=0,
        total=1,
        detail="Installing spandrel…",
        timeout_s=300,
    )
    if not result.get("ok"):
        return result

    _purge_import_modules("spandrel")
    check = spandrel_import_status()
    if not check.get("ready"):
        return {
            "ok": False,
            "message": (
                check.get("message")
                or "spandrel installed but still not importable. Restart the app."
            ),
        }

    if progress_cb:
        progress_cb(1, 1, "Spandrel ready.")
    return {"ok": True, "message": "Installed spandrel."}
