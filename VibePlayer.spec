# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_MODELS_DIR = _ROOT / "app" / "models"

_datas = [
    # GUI prvky a ikony
    ("app/assets", "assets"),
    ("app/icons", "icons"),
    # Pluginy
    ("app/plugins", "plugins"),
    # Nastroje jako FFmpeg
    ("tools/ffmpeg/bin", "tools/ffmpeg/bin"),
    # Splash screen (pokud je uvnitr app)
    ("app/splash_image.py", "."),
]

# Volitelne: local modely pribal jen pokud fyzicky existuji.
if _MODELS_DIR.is_dir():
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