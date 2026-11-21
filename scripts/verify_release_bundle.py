"""Fail the release build if critical portable packages are missing.

Pure-Python packages (einops, dill, …) often land in PYZ, not as folders next
to the exe — only datas like ``py.typed`` may appear on disk. Binary helpers
(kornia_rs ``*.pyd``, torch CUDA DLLs) must exist as real files.

Checks:
1. ``VibePlayer.exe`` present
2. Required modules listed in ``PYZ-00.toc`` (or as on-disk package dirs)
3. ``kornia_rs`` native extension on disk
4. Full build: ``torch/lib/torch_cuda.dll`` + heavy package folders
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# Pure / mostly-pure — OK if in PYZ or as a folder with real modules.
REQUIRED_MODULES = (
    "einops",
    "kornia",
    "ftfy",
    "dill",
    "huggingface_hub",
    "open_clip",
    "timm",
    "safetensors",
    "tokenizers",
    "regex",
)

# Must exist on disk (native / large binary trees).
REQUIRED_ON_DISK_DIRS = (
    "kornia_rs",
)

# TorchScript/kornia needs real .py sources next to the exe (not PYZ-only).
REQUIRED_ON_DISK_PY_PACKAGES = (
    "kornia",
    "einops",
)

REQUIRED_FULL_DIRS = (
    "torch",
    "transformers",
    "ultralytics",
)


def _load_pyz_module_names(toc_path: Path) -> set[str]:
    """Parse top-level module names from PyInstaller PYZ-00.toc."""
    text = toc_path.read_text(encoding="utf-8", errors="ignore")
    # Entries look like:  ('dill',  or  ('einops.layers',
    names: set[str] = set()
    for match in re.finditer(r"\(\s*'([A-Za-z0-9_]+)", text):
        names.add(match.group(1))
    return names


def _on_disk_package(root: Path, name: str) -> bool:
    pkg = root / name
    if not pkg.is_dir():
        return False
    # Ignore datas-only stubs (just py.typed / empty).
    if any(pkg.rglob("*.py")) or any(pkg.rglob("*.pyd")) or any(pkg.rglob("*.dll")):
        return True
    return False


def _module_present(root: Path, name: str, pyz_names: set[str] | None) -> bool:
    if _on_disk_package(root, name):
        return True
    if pyz_names is not None and name in pyz_names:
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify portable VibePlayer bundle.")
    parser.add_argument(
        "--dist-root",
        default="dist/VibePlayer",
        help="Path to onedir build root (folder with VibePlayer.exe).",
    )
    parser.add_argument(
        "--pyz-toc",
        default="build/VibePlayer/PYZ-00.toc",
        help="PyInstaller PYZ TOC used to confirm pure-Python modules.",
    )
    parser.add_argument(
        "--base-only",
        action="store_true",
        help="Only check base AI runtime (skip torch CUDA / GPU pack dirs).",
    )
    args = parser.parse_args()
    check_full = not bool(args.base_only)
    root = Path(args.dist_root).resolve()
    toc_path = Path(args.pyz_toc).resolve()

    if not root.is_dir():
        print(f"[verify] FAIL: dist root not found: {root}")
        return 1
    if not (root / "VibePlayer.exe").is_file():
        print(f"[verify] FAIL: VibePlayer.exe missing under {root}")
        return 1

    pyz_names: set[str] | None = None
    if toc_path.is_file():
        pyz_names = _load_pyz_module_names(toc_path)
        print(f"[verify] PYZ TOC: {toc_path} ({len(pyz_names)} top-level names)")
    else:
        print(
            f"[verify] WARN: PYZ TOC not found ({toc_path}); "
            "will only accept on-disk packages with .py/.pyd."
        )

    missing: list[str] = []
    for name in REQUIRED_MODULES:
        if _module_present(root, name, pyz_names):
            where = "disk" if _on_disk_package(root, name) else "PYZ"
            print(f"[verify]   OK {name} ({where})")
        else:
            missing.append(name)
            print(f"[verify]   MISSING {name}")

    for name in REQUIRED_ON_DISK_DIRS:
        pkg = root / name
        has_pyd = pkg.is_dir() and any(pkg.glob("*.pyd"))
        if has_pyd:
            print(f"[verify]   OK {name}/ (native .pyd on disk)")
        else:
            missing.append(name)
            print(f"[verify]   MISSING {name}/ native .pyd on disk")

    for name in REQUIRED_ON_DISK_PY_PACKAGES:
        pkg = root / name
        py_count = len(list(pkg.rglob("*.py"))) if pkg.is_dir() else 0
        if py_count > 0:
            print(f"[verify]   OK {name}/ ({py_count} .py files on disk)")
        else:
            missing.append(f"{name} (.py sources)")
            print(
                f"[verify]   MISSING {name}/ .py sources on disk "
                "(TorchScript/kornia needs collect_all, not PYZ-only)"
            )

    if missing:
        print("[verify] FAIL: required packages missing:")
        for name in missing:
            print(f"[verify]   - {name}")
        print("[verify] Fix VibePlayer.spec (_AI_RUNTIME_PACKAGES) and rebuild.")
        return 1

    if check_full:
        missing_full = [name for name in REQUIRED_FULL_DIRS if not (root / name).is_dir()]
        if missing_full:
            print("[verify] FAIL: required FULL-build packages missing:")
            for name in missing_full:
                print(f"[verify]   - {name}/")
            return 1
        for name in REQUIRED_FULL_DIRS:
            print(f"[verify]   OK {name}/")

        cuda_dll = root / "torch" / "lib" / "torch_cuda.dll"
        if not cuda_dll.is_file():
            print(f"[verify] FAIL: missing {cuda_dll}")
            return 1
        print(f"[verify]   OK torch/lib/{cuda_dll.name}")

    print("[verify] Bundle check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
