"""
pack_rife_optional.py — Zip tools/rife into VibePlayer-rife-pack.zip for releases.

Expected layout inside the ZIP (same as base / GPU packs):
  VibePlayer/tools/rife/...

Does not modify the base install — ship this ZIP separately.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
ZIP_ROOT_PREFIX = "VibePlayer/"


def main() -> int:
    parser = argparse.ArgumentParser(description="Pack optional RIFE tools into a release ZIP.")
    parser.add_argument(
        "--rife-dir",
        default=str(ROOT / "tools" / "rife"),
        help="Source tools/rife directory.",
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "dist" / "releases" / "VibePlayer-rife-pack.zip"),
        help="Output ZIP path.",
    )
    args = parser.parse_args()

    rife_dir = Path(args.rife_dir).resolve()
    out_path = Path(args.out).resolve()
    exe = rife_dir / "rife-ncnn-vulkan.exe"
    if not exe.is_file():
        print(
            f"[rife-pack] Missing {exe}\n"
            "Run: python scripts/fetch_rife_ncnn.py",
            file=sys.stderr,
        )
        return 1

    files = [p for p in rife_dir.rglob("*") if p.is_file()]
    if not files:
        print("[rife-pack] No files to pack.", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    total = 0
    with ZipFile(out_path, "w", compression=ZIP_DEFLATED, compresslevel=6) as zf:
        for file_path in files:
            rel = file_path.relative_to(rife_dir)
            arc = f"{ZIP_ROOT_PREFIX}tools/rife/{rel.as_posix()}"
            zf.write(file_path, arcname=arc)
            total += file_path.stat().st_size

    print(f"[rife-pack] Files: {len(files)}")
    print(f"[rife-pack] Uncompressed: {total / (1024 * 1024):.1f} MB")
    print(f"[rife-pack] ZIP: {out_path} ({out_path.stat().st_size / (1024 * 1024):.1f} MB)")
    print("[rife-pack] Extract over the same folder as VibePlayer-base.zip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
