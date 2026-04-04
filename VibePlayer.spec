# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['app\\main.py'],
    pathex=[],
    binaries=[],
	datas=[
			# GUI prvky a ikony
			('app/assets', 'assets'),
			('app/icons', 'icons'),
			
			# AI modely a pluginy
			('app/models', 'models'),
			('app/plugins', 'plugins'),
			
			# Nástroje jako FFmpeg
			('tools/ffmpeg/bin', 'tools/ffmpeg/bin'),
			
			# Splash screen (pokud je uvnitř app)
			('app/splash_image.py', '.') 
	],
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