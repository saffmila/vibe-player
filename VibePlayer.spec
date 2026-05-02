# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

try:
    _ROOT = Path(__file__).resolve().parent
except NameError:
    # PyInstaller can execute spec without __file__ in some contexts.
    _ROOT = Path.cwd()
_MODELS_DIR = _ROOT / "app" / "models"
_INCLUDE_LOCAL_MODELS = os.environ.get("VP_INCLUDE_LOCAL_MODELS", "").strip() == "1"

_datas = [
    # GUI prvky a ikony
    ("app/assets", "assets"),
    ("app/icons", "icons"),
    # CLIP/YOLO tagging: UTF-8 tag lists and hints (not Python modules)
    ("app/tag_engine", "tag_engine"),
    # Pluginy
    ("app/plugins", "plugins"),
    # Nastroje jako FFmpeg
    ("tools/ffmpeg/bin", "tools/ffmpeg/bin"),
    # Splash screen (pokud je uvnitr app)
    ("app/splash_image.py", "."),
]

# Required for CLIP runtime: ships open_clip BPE vocabulary file
# (e.g. bpe_simple_vocab_16e6.txt.gz), which is not a Python module.
_datas += collect_data_files("open_clip")

# Volitelne: local modely pribal jen pokud fyzicky existuji
# a je explicitne zapnuty opt-in pres VP_INCLUDE_LOCAL_MODELS=1.
if _INCLUDE_LOCAL_MODELS and _MODELS_DIR.is_dir():
    _datas.append(("app/models", "models"))

a = Analysis(
    ['app\\main.py'],
    pathex=[],
    binaries=[],
    datas=_datas,
    # Sem se píšou moduly, které PyInstaller sám nenašel
    hiddenimports=['PIL._tkinter_finder'], 
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)


def _print_largest_binaries(binaries, top_n=10):
    """Print the largest packaged binary files for build diagnostics."""
    sized = []
    for entry in binaries:
        if not isinstance(entry, tuple) or len(entry) < 2:
            continue
        src_path = entry[1]
        try:
            size = Path(src_path).stat().st_size
        except OSError:
            continue
        sized.append((size, entry[0], src_path))

    sized.sort(reverse=True, key=lambda item: item[0])
    print("\n[build diagnostics] Top {} largest binaries:".format(top_n))
    if not sized:
        print("[build diagnostics] No binaries found.")
        return

    for idx, (size, name, src_path) in enumerate(sized[:top_n], start=1):
        size_mb = size / (1024 * 1024)
        print(
            "[build diagnostics] {:>2}. {:8.2f} MB  {} ({})".format(
                idx, size_mb, name, src_path
            )
        )


_print_largest_binaries(a.binaries, top_n=10)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VibePlayer',
    # PyInstaller 6 default: DLL a data v podslozce _internal — bez ni exe hlasi chybejici python311.dll.
    # Tecka = stare onedir rozlozeni (vse vedle VibePlayer.exe v dist\VibePlayer\).
    contents_directory='.',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False, # Zajišťuje, že na pozadí nepoběží černý terminál
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='assets/ikona.ico'  # Odkomentuj a uprav, pokud máš ikonu pro .exe!
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VibePlayer',
)