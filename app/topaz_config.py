"""
topaz_config.py — Detect Topaz Photo AI install + tpai.exe CLI.

Uses the licensed local install only (no bundling). Older Photo AI builds
expose an experimental ``--upscale key=value`` CLI on tpai.exe.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:  # non-Windows
    winreg = None  # type: ignore[assignment]

from vtp_constants import IMAGE_FORMATS

TOPAZ_PROJECT_URL = "https://www.topazlabs.com/topaz-photo-ai"
NOT_INSTALLED_MESSAGE = (
    "Topaz Photo AI was not found.\n\n"
    "Install Topaz Photo AI (licensed), then reopen this dialog.\n"
    "Expected CLI: tpai.exe under Program Files\\Topaz Labs LLC\\Topaz Photo AI\\"
)
LOGIN_REQUIRED_MESSAGE = (
    "Topaz Photo AI CLI reported an invalid login token.\n\n"
    "Open Topaz Photo AI normally once, sign in, then retry."
)

# Common Enhance / Upscale model names across Photo AI 2.x–3.x.
TOPAZ_UPSCALE_MODELS: tuple[str, ...] = (
    "High Fidelity V2",
    "High Fidelity",
    "Standard V2",
    "Standard",
    "CGI",
    "Low Resolution V2",
    "Low Resolution",
    "Recovery V2",
    "Recovery",
)

DEFAULT_TOPAZ_MODEL = "High Fidelity V2"
DEFAULT_TOPAZ_SCALE = 4

_IMAGE_EXTS = frozenset(ext.lower() for ext in IMAGE_FORMATS)

_CANDIDATE_REL = Path("Topaz Labs LLC") / "Topaz Photo AI" / "tpai.exe"


def supports_image(file_path: str | None) -> bool:
    if not file_path:
        return False
    return os.path.isfile(file_path) and os.path.splitext(file_path)[1].lower() in _IMAGE_EXTS


def _program_files_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        raw = os.environ.get(key)
        if not raw:
            continue
        p = Path(raw)
        if key == "LOCALAPPDATA":
            roots.append(p / "Programs")
        else:
            roots.append(p)
    # De-dupe while preserving order
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _registry_install_dirs() -> list[Path]:
    dirs: list[Path] = []
    if os.name != "nt" or winreg is None:
        return dirs
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, sub in roots:
        try:
            with winreg.OpenKey(hive, sub) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as item:
                            name, _ = winreg.QueryValueEx(item, "DisplayName")
                            if "topaz photo ai" not in str(name).lower():
                                continue
                            try:
                                loc, _ = winreg.QueryValueEx(item, "InstallLocation")
                            except OSError:
                                loc = ""
                            if loc and Path(loc).is_dir():
                                dirs.append(Path(loc))
                    except OSError:
                        continue
        except OSError:
            continue
    return dirs


def find_tpai_exe(explicit: str | None = None) -> Path | None:
    """Locate tpai.exe (CLI), not the GUI Topaz Photo AI.exe."""
    if explicit:
        p = Path(explicit.strip().strip('"'))
        if p.is_file() and p.name.lower() == "tpai.exe":
            return p.resolve()
        if p.is_dir():
            cand = p / "tpai.exe"
            if cand.is_file():
                return cand.resolve()

    for root in _program_files_roots():
        cand = root / _CANDIDATE_REL
        if cand.is_file():
            return cand.resolve()

    for install in _registry_install_dirs():
        cand = install / "tpai.exe"
        if cand.is_file():
            return cand.resolve()
        # Sometimes InstallLocation is the parent "Topaz Labs LLC"
        nested = install / "Topaz Photo AI" / "tpai.exe"
        if nested.is_file():
            return nested.resolve()

    return None


def _registry_display_version() -> str | None:
    if os.name != "nt" or winreg is None:
        return None
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, sub in roots:
        try:
            with winreg.OpenKey(hive, sub) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as item:
                            name, _ = winreg.QueryValueEx(item, "DisplayName")
                            if "topaz photo ai" not in str(name).lower():
                                continue
                            try:
                                ver, _ = winreg.QueryValueEx(item, "DisplayVersion")
                            except OSError:
                                return None
                            return str(ver).strip() or None
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def topaz_version(exe: Path | None = None) -> str | None:
    path = exe or find_tpai_exe()
    if path is None or not path.is_file():
        return None
    if os.name != "nt":
        return None
    try:
        import win32api

        info = win32api.GetFileVersionInfo(str(path), "\\")
        ms = info["FileVersionMS"]
        ls = info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        pass
    return _registry_display_version()


def runtime_status(*, exe_override: str | None = None) -> dict[str, Any]:
    exe = find_tpai_exe(exe_override)
    if exe is None:
        return {
            "ready": False,
            "error": "topaz_missing",
            "message": NOT_INSTALLED_MESSAGE,
            "exe": None,
            "version": None,
        }
    ver = topaz_version(exe)
    return {
        "ready": True,
        "error": None,
        "message": None,
        "exe": str(exe),
        "version": ver,
        "dir": str(exe.parent),
    }


def weights_status(*, exe_override: str | None = None) -> dict[str, Any]:
    """Topaz ships models with the app — mirror runtime_status for UpscaleBackend."""
    status = runtime_status(exe_override=exe_override)
    return {
        "ready": bool(status.get("ready")),
        "path": status.get("dir"),
        "download_url": TOPAZ_PROJECT_URL,
        "message": status.get("message"),
        "exe": status.get("exe"),
        "version": status.get("version"),
    }
