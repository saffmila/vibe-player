"""Split a full PyInstaller onedir release into base + optional packs.

Build strategy:
- Build once (full app with all dependencies) into dist/VibePlayer
- Split files into:
  1) VibePlayer-base.zip (all non-heavy files)
  2) VibePlayer-autotag-gpu-pack.zip (or .part01.zip, .part02.zip, …)
     — each part is a normal ZIP Total Commander / Explorer can open
  3) VibePlayer-rife-pack.zip (optional tools/rife — only if present in dist)

All archived files are stored under a common VibePlayer/ root directory inside ZIPs
so base + optional packs can be extracted into the same target folder safely.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ZIP_ROOT_PREFIX = "VibePlayer/"
# Soft limit per GPU ZIP part (uncompressed payload). Keeps each file under
# typical hosting caps (e.g. GitHub ~2 GB) while staying TC-openable.
GPU_ZIP_MAX_UNCOMPRESSED = 1500 * 1024 * 1024


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


def is_rife_pack(rel_posix: str) -> bool:
    """Optional rife-ncnn-vulkan tools — never ship in the base ZIP."""
    lower = rel_posix.lower().replace("\\", "/")
    return lower.startswith("tools/rife/") or "/tools/rife/" in f"/{lower}"


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


def clear_gpu_pack_outputs(out_dir: Path) -> None:
    """Remove previous GPU pack ZIPs (including old 7-Zip .zip.001 style)."""
    patterns = (
        "VibePlayer-autotag-gpu-pack.zip",
        "VibePlayer-autotag-gpu-pack.zip.*",
        "VibePlayer-autotag-gpu-pack.part*.zip",
    )
    for pattern in patterns:
        for path in out_dir.glob(pattern):
            if path.is_file():
                path.unlink()


def split_files_into_size_buckets(
    files: list[Path],
    max_uncompressed: int,
) -> list[list[Path]]:
    """Group files into buckets whose uncompressed sizes stay near max_uncompressed.

    A single file larger than the limit still gets its own bucket (cannot split
    one file across ZIPs without multi-volume archives).
    """
    if not files:
        return []

    ordered = sorted(files, key=lambda p: p.as_posix().lower())
    buckets: list[list[Path]] = []
    current: list[Path] = []
    current_size = 0

    for file_path in ordered:
        size = file_path.stat().st_size
        if current and current_size + size > max_uncompressed:
            buckets.append(current)
            current = []
            current_size = 0
        current.append(file_path)
        current_size += size

    if current:
        buckets.append(current)
    return buckets


def create_gpu_pack_zips(
    root: Path,
    files: list[Path],
    out_dir: Path,
    max_uncompressed: int = GPU_ZIP_MAX_UNCOMPRESSED,
) -> tuple[list[Path], int]:
    """Write one or more standalone ZIP parts for the GPU pack.

    Returns (part_paths, total_uncompressed_bytes).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    clear_gpu_pack_outputs(out_dir)

    if not files:
        return [], 0

    buckets = split_files_into_size_buckets(files, max_uncompressed)
    part_paths: list[Path] = []
    total_uncompressed = 0

    multi = len(buckets) > 1
    for index, bucket in enumerate(buckets, start=1):
        if multi:
            zip_path = out_dir / f"VibePlayer-autotag-gpu-pack.part{index:02d}.zip"
        else:
            zip_path = out_dir / "VibePlayer-autotag-gpu-pack.zip"
        total_uncompressed += zip_files(root, bucket, zip_path)
        part_paths.append(zip_path)

    return part_paths, total_uncompressed


def main() -> int:
    parser = argparse.ArgumentParser(description="Split full build into base and GPU pack ZIPs.")
    parser.add_argument("--dist-root", default="dist/VibePlayer", help="Path to full onedir build.")
    parser.add_argument("--out-dir", default="dist/releases", help="Output directory for generated ZIP files.")
    parser.add_argument(
        "--gpu-max-mb",
        type=int,
        default=1500,
        help="Max uncompressed MB per GPU ZIP part (default: 1500).",
    )
    args = parser.parse_args()

    dist_root = Path(args.dist_root).resolve()
    out_dir = Path(args.out_dir).resolve()
    max_uncompressed = max(1, int(args.gpu_max_mb)) * 1024 * 1024

    if not dist_root.is_dir():
        raise SystemExit(f"[split] Dist root not found: {dist_root}")

    all_files = iter_files(dist_root)
    if not all_files:
        raise SystemExit(f"[split] No files found under: {dist_root}")

    base_files: list[Path] = []
    gpu_pack_files: list[Path] = []
    rife_pack_files: list[Path] = []

    for file_path in all_files:
        rel_posix = file_path.relative_to(dist_root).as_posix()
        if is_junk(rel_posix):
            continue
        if is_rife_pack(rel_posix):
            rife_pack_files.append(file_path)
        elif is_heavy(rel_posix):
            gpu_pack_files.append(file_path)
        else:
            base_files.append(file_path)

    base_zip = out_dir / "VibePlayer-base.zip"
    base_uncompressed = zip_files(dist_root, base_files, base_zip)

    rife_zip = out_dir / "VibePlayer-rife-pack.zip"
    rife_uncompressed = 0
    if rife_pack_files:
        if rife_zip.exists():
            rife_zip.unlink()
        rife_uncompressed = zip_files(dist_root, rife_pack_files, rife_zip)
    elif rife_zip.exists():
        rife_zip.unlink()

    gpu_part_paths, gpu_uncompressed = create_gpu_pack_zips(
        dist_root,
        gpu_pack_files,
        out_dir,
        max_uncompressed=max_uncompressed,
    )

    print(f"[split] Dist root: {dist_root}")
    print(f"[split] Total files scanned: {len(all_files)}")
    print(f"[split] Base files: {len(base_files)}")
    print(f"[split] GPU pack files: {len(gpu_pack_files)}")
    print(f"[split] RIFE pack files: {len(rife_pack_files)}")
    print(f"[split] ZIP root prefix: {ZIP_ROOT_PREFIX}")
    print(f"[split] Base ZIP: {base_zip}")
    if rife_pack_files:
        print(f"[split] RIFE ZIP: {rife_zip}")
        print(f"[split] RIFE uncompressed size: {rife_uncompressed / (1024 * 1024):.2f} MB")
    else:
        print("[split] RIFE pack skipped (tools/rife not in dist — optional).")
    if gpu_part_paths:
        label = "GPU ZIP" if len(gpu_part_paths) == 1 else "GPU ZIP parts (extract all over the same VibePlayer/ folder)"
        print(f"[split] {label}:")
        for path in gpu_part_paths:
            print(f"[split]   - {path} ({path.stat().st_size / (1024 * 1024):.2f} MB)")
    else:
        print("[split] GPU pack skipped (no heavy files).")
    print(f"[split] Base uncompressed size: {base_uncompressed / (1024 * 1024):.2f} MB")
    print(f"[split] GPU uncompressed size: {gpu_uncompressed / (1024 * 1024):.2f} MB")
    print(f"[split] GPU ZIP max uncompressed per part: {max_uncompressed / (1024 * 1024):.0f} MB")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
