"""Split a full PyInstaller onedir release into base + optional GPU autotag pack.

Build strategy:
- Build once (full app with all dependencies) into dist/VibePlayer
- Split files into:
  1) VibePlayer-base.zip (all non-heavy files)
  2) VibePlayer-autotag-gpu-pack.zip.001/.002/... (7-Zip multi-volume archive)

All archived files are stored under a common VibePlayer/ root directory inside ZIPs
so base + GPU packs can be extracted into the same target folder safely.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ZIP_ROOT_PREFIX = "VibePlayer/"
GPU_7Z_VOLUME_SIZE = "1500m"


HEAVY_PREFIXES = (
    "torch/",
    "torchvision/",
    "torchaudio/",
    "transformers/",
    "ultralytics/",
    "open_clip/",
    "open_clip_torch/",
    "tokenizers/",
    "safetensors/",
    "nvidia/",
    "triton/",
)

HEAVY_NAME_PARTS = (
    "torch",
    "cuda",
    "cudnn",
    "cublas",
    "cusparse",
    "cufft",
    "curand",
    "cusolver",
    "nvrtc",
)

JUNK_PREFIXES = (
    "_polars_runtime_32/",
)

JUNK_FILENAMES = (
    "_polars_runtime.pyd",
)


def is_junk(rel_posix: str) -> bool:
    lower = rel_posix.lower()
    if any(lower.startswith(prefix) for prefix in JUNK_PREFIXES):
        return True
    return any(lower.endswith(name) for name in JUNK_FILENAMES)


def is_heavy(rel_posix: str) -> bool:
    lower = rel_posix.lower()
    if any(lower.startswith(prefix) for prefix in HEAVY_PREFIXES):
        return True
    filename = Path(lower).name
    return any(part in filename for part in HEAVY_NAME_PARTS)


def iter_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def zip_files(root: Path, files: list[Path], zip_path: Path) -> int:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    total_size = 0
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED, compresslevel=6) as zf:
        for file_path in files:
            rel = file_path.relative_to(root)
            zf.write(file_path, arcname=f"{ZIP_ROOT_PREFIX}{rel.as_posix()}")
            total_size += file_path.stat().st_size
    return total_size


def stage_gpu_pack_files(root: Path, files: list[Path], staging_root: Path) -> int:
    """Copy GPU files into a temporary staging tree under VibePlayer/."""
    if staging_root.exists():
        shutil.rmtree(staging_root)
    target_root = staging_root / "VibePlayer"
    target_root.mkdir(parents=True, exist_ok=True)

    total_size = 0
    for file_path in files:
        rel = file_path.relative_to(root)
        dst = target_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, dst)
        total_size += file_path.stat().st_size
    return total_size


def find_7z_binary() -> str | None:
    """Return 7z executable path from PATH or common Windows install location."""
    in_path = shutil.which("7z")
    if in_path:
        return in_path
    common = Path("C:/Program Files/7-Zip/7z.exe")
    if common.is_file():
        return str(common)
    return None


def confirm_continue_without_7z(assume_yes: bool) -> bool:
    """Ask user whether to continue without GPU archive when 7-Zip is missing."""
    if assume_yes:
        return True
    while True:
        choice = input(
            "[split] 7-Zip was not found. Continue without GPU archive and keep unpacked folder? [Y/N]: "
        ).strip().lower()
        if choice in {"y", "yes"}:
            return True
        if choice in {"n", "no"}:
            return False
        print("[split] Please answer Y or N.")


def create_gpu_pack_with_7z(staging_root: Path, out_dir: Path, volume_size: str) -> list[Path]:
    """Create native multi-volume ZIP using external 7-Zip command."""
    seven_zip = find_7z_binary()
    if not seven_zip:
        return []

    out_base = out_dir / "VibePlayer-autotag-gpu-pack.zip"
    for existing in out_dir.glob("VibePlayer-autotag-gpu-pack.zip*"):
        existing.unlink()

    command = [
        seven_zip,
        "a",
        f"-v{volume_size}",
        str(out_base),
        "VibePlayer",
    ]
    # Run 7-Zip from staging root so archive contains VibePlayer/ at root level.
    result = subprocess.run(command, cwd=staging_root, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "7-Zip failed while creating GPU pack.\n"
            f"Command: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    part_paths = sorted(out_dir.glob("VibePlayer-autotag-gpu-pack.zip*"))
    if not part_paths:
        raise RuntimeError("7-Zip reported success but no GPU archive files were produced.")
    return part_paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Split full build into base and GPU pack ZIPs.")
    parser.add_argument("--dist-root", default="dist/VibePlayer", help="Path to full onedir build.")
    parser.add_argument("--out-dir", default="dist/releases", help="Output directory for generated ZIP files.")
    parser.add_argument(
        "--yes-without-7z",
        action="store_true",
        help="Automatically continue without GPU archive when 7-Zip is missing.",
    )
    args = parser.parse_args()

    dist_root = Path(args.dist_root).resolve()
    out_dir = Path(args.out_dir).resolve()

    if not dist_root.is_dir():
        raise SystemExit(f"[split] Dist root not found: {dist_root}")

    all_files = iter_files(dist_root)
    if not all_files:
        raise SystemExit(f"[split] No files found under: {dist_root}")

    base_files: list[Path] = []
    gpu_pack_files: list[Path] = []

    for file_path in all_files:
        rel_posix = file_path.relative_to(dist_root).as_posix()
        if is_junk(rel_posix):
            continue
        if is_heavy(rel_posix):
            gpu_pack_files.append(file_path)
        else:
            base_files.append(file_path)

    base_zip = out_dir / "VibePlayer-base.zip"
    base_uncompressed = zip_files(dist_root, base_files, base_zip)
    staging_root = out_dir / "_gpu_pack_staging"
    gpu_uncompressed = stage_gpu_pack_files(dist_root, gpu_pack_files, staging_root)
    gpu_part_paths: list[Path] = []
    fallback_unpacked_dir = out_dir / "VibePlayer-autotag-gpu-pack-unpacked"
    if fallback_unpacked_dir.exists():
        shutil.rmtree(fallback_unpacked_dir)
    seven_zip = find_7z_binary()

    try:
        if seven_zip:
            gpu_part_paths = create_gpu_pack_with_7z(staging_root, out_dir, GPU_7Z_VOLUME_SIZE)
        else:
            if not confirm_continue_without_7z(args.yes_without_7z):
                raise SystemExit("[split] Build cancelled by user (7-Zip missing).")
            shutil.copytree(staging_root / "VibePlayer", fallback_unpacked_dir / "VibePlayer")
    except RuntimeError as exc:
        raise SystemExit(f"[split] ERROR: {exc}") from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    print(f"[split] Dist root: {dist_root}")
    print(f"[split] Total files scanned: {len(all_files)}")
    print(f"[split] Base files: {len(base_files)}")
    print(f"[split] GPU pack files: {len(gpu_pack_files)}")
    print(f"[split] ZIP root prefix: {ZIP_ROOT_PREFIX}")
    print(f"[split] Base ZIP: {base_zip}")
    if gpu_part_paths:
        print("[split] GPU 7-Zip multi-volume parts:")
        for path in gpu_part_paths:
            print(f"[split]   - {path}")
    else:
        print("[split] GPU archive not created via 7-Zip.")
        print(f"[split] Prepared unpacked fallback folder: {fallback_unpacked_dir / 'VibePlayer'}")
        print("[split] Please compress this folder manually if needed.")
    print(f"[split] Base uncompressed size: {base_uncompressed / (1024 * 1024):.2f} MB")
    print(f"[split] GPU uncompressed size: {gpu_uncompressed / (1024 * 1024):.2f} MB")
    print(f"[split] GPU 7-Zip volume size target: {GPU_7Z_VOLUME_SIZE}")
    for path in gpu_part_paths:
        print(f"[split] GPU part size: {path.name} = {path.stat().st_size / (1024 * 1024):.2f} MB")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
