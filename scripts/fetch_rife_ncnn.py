"""
fetch_rife_ncnn.py — Download nihui/rife-ncnn-vulkan into tools/rife/.

The official Windows ZIP is ~400 MB (all models). urllib is single-connection and
often painfully slow — this script prefers aria2c / curl, supports mirrors, and
by default keeps only one model (slim pack ~tens of MB on disk).

Usage:
  python scripts/fetch_rife_ncnn.py
  python scripts/fetch_rife_ncnn.py --mirror
  python scripts/fetch_rife_ncnn.py --zip path\\to\\already-downloaded.zip
  python scripts/fetch_rife_ncnn.py --keep-all-models
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from rife_config import (  # noqa: E402
    PREFERRED_MODELS,
    RIFE_WINDOWS_ZIP_URL,
    default_rife_dir,
)

# Public GitHub download accelerator (often much faster than raw github.com).
GHPROXY_PREFIX = "https://ghfast.top/"


def _mirrored_url(url: str) -> str:
    if url.startswith(GHPROXY_PREFIX):
        return url
    return GHPROXY_PREFIX + url


def _which(name: str) -> str | None:
    return shutil.which(name)


def _download_aria2(url: str, dest: Path) -> bool:
    aria = _which("aria2c")
    if not aria:
        return False
    print(f"[rife] Using aria2c (multi-connection) ← {url}")
    cmd = [
        aria,
        "-x",
        "16",
        "-s",
        "16",
        "-k",
        "1M",
        "--file-allocation=none",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "-c",
        "-o",
        dest.name,
        "-d",
        str(dest.parent),
        url,
    ]
    result = subprocess.run(cmd)
    return result.returncode == 0 and dest.is_file() and dest.stat().st_size > 1_000_000


def _download_curl(url: str, dest: Path) -> bool:
    curl = _which("curl")
    if not curl:
        return False
    print(f"[rife] Using curl ← {url}")
    cmd = [
        curl,
        "-L",
        "--retry",
        "5",
        "--retry-all-errors",
        "--connect-timeout",
        "30",
        "-o",
        str(dest),
        url,
    ]
    # Prefer progress bar when stderr is a TTY.
    if sys.stderr.isatty():
        cmd[1:1] = ["--progress-bar"]
    else:
        cmd[1:1] = ["-#"]
    result = subprocess.run(cmd)
    return result.returncode == 0 and dest.is_file() and dest.stat().st_size > 1_000_000


def _download_urllib(url: str, dest: Path) -> None:
    print(f"[rife] Using urllib (slow single stream) ← {url}")
    print("[rife] Tip: install aria2 (`winget install aria2`) or pass --mirror / --zip")

    def _hook(block_num: int, block_size: int, total: int) -> None:
        if total <= 0:
            return
        done = min(total, block_num * block_size)
        pct = done * 100.0 / total
        mb = done / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        print(f"\r[rife] {pct:5.1f}%  {mb:.0f}/{total_mb:.0f} MB", end="", flush=True)

    urlretrieve(url, dest, reporthook=_hook)
    print()


def _download(url: str, dest: Path, *, try_mirror_fallback: bool) -> None:
    urls = [url]
    if try_mirror_fallback and not url.startswith(GHPROXY_PREFIX):
        urls.append(_mirrored_url(url))

    last_err: Exception | None = None
    for attempt, candidate in enumerate(urls):
        if attempt:
            print(f"[rife] Retry via mirror: {candidate}")
        try:
            if _download_aria2(candidate, dest):
                return
            if _download_curl(candidate, dest):
                return
            _download_urllib(candidate, dest)
            if dest.is_file() and dest.stat().st_size > 1_000_000:
                return
        except Exception as exc:
            last_err = exc
            print(f"[rife] Download failed: {exc}")
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
    raise SystemExit(f"[rife] Download failed. Last error: {last_err}")


def _pick_model_dirs(pack_root: Path, keep_all: bool) -> list[Path]:
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


def _install_from_extracted(extract_dir: Path, out_dir: Path, *, keep_all: bool) -> None:
    candidates = [p for p in extract_dir.iterdir() if p.is_dir()]
    source = candidates[0] if len(candidates) == 1 else extract_dir
    exe = next(source.rglob("rife-ncnn-vulkan.exe"), None)
    if exe is None:
        raise SystemExit("[rife] rife-ncnn-vulkan.exe not found in the archive.")

    pack_root = exe.parent
    keep_models = {p.name for p in _pick_model_dirs(pack_root, keep_all)}
    print(f"[rife] Installing from {pack_root} → {out_dir}")
    if not keep_all:
        print(f"[rife] Slim mode — keeping model(s): {', '.join(sorted(keep_models)) or '(none)'}")

    out_dir.mkdir(parents=True, exist_ok=True)
    for item in pack_root.iterdir():
        # Skip extra model folders in slim mode.
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch optional rife-ncnn-vulkan pack.")
    parser.add_argument(
        "--out",
        default=str(default_rife_dir()),
        help="Install directory (default: tools/rife).",
    )
    parser.add_argument(
        "--url",
        default=RIFE_WINDOWS_ZIP_URL,
        help="Windows release ZIP URL.",
    )
    parser.add_argument(
        "--mirror",
        action="store_true",
        help=f"Download via {GHPROXY_PREFIX} mirror (often faster).",
    )
    parser.add_argument(
        "--zip",
        dest="zip_path",
        default="",
        help="Use an already-downloaded ZIP instead of downloading.",
    )
    parser.add_argument(
        "--keep-all-models",
        action="store_true",
        help="Keep every model from the ZIP (default: only preferred rife-v4.6).",
    )
    args = parser.parse_args()

    out_dir = Path(args.out).resolve()
    url = _mirrored_url(args.url) if args.mirror else args.url

    with tempfile.TemporaryDirectory(prefix="vibe_rife_dl_") as tmp:
        tmp_path = Path(tmp)
        if args.zip_path:
            zip_path = Path(args.zip_path).expanduser().resolve()
            if not zip_path.is_file():
                raise SystemExit(f"[rife] ZIP not found: {zip_path}")
            print(f"[rife] Using local ZIP: {zip_path}")
        else:
            zip_path = tmp_path / "rife-windows.zip"
            print(
                "[rife] Official Windows pack is ~400 MB (all models).\n"
                "[rife] Faster options:\n"
                "  • winget install aria2   then re-run this script\n"
                "  • python scripts/fetch_rife_ncnn.py --mirror\n"
                "  • download ZIP in browser, then:\n"
                "      python scripts/fetch_rife_ncnn.py --zip PATH\\to\\zip\n"
            )
            _download(url, zip_path, try_mirror_fallback=not args.mirror)

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        print("[rife] Extracting…")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        _install_from_extracted(
            extract_dir,
            out_dir,
            keep_all=bool(args.keep_all_models),
        )

    final_exe = out_dir / "rife-ncnn-vulkan.exe"
    if not final_exe.is_file():
        raise SystemExit(f"[rife] Install incomplete — missing {final_exe}")

    # Rough installed size.
    total = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
    print(f"[rife] Ready: {final_exe}")
    print(f"[rife] Installed size: {total / (1024 * 1024):.1f} MB under {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
