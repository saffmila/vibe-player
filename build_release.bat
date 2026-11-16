@echo off
setlocal
cd /d "%~dp0"

title VibePlayer - Release Build (Build Once, Split After)
echo ==========================================
echo   VIBE PLAYER - RELEASE BUILD (1x BUILD)
echo ==========================================
echo.

if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] Virtualni prostredi neexistuje. Nejdriv spust install.bat.
    pause
    exit /b 1
)

if not exist "scripts\split_release.py" (
    echo [ERROR] Chybi scripts\split_release.py
    pause
    exit /b 1
)

echo [1/4] Aktivuji virtualni prostredi (venv)...
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Nepodarilo se aktivovat virtualni prostredi.
    goto :fail
)

echo.
echo [2/5] Buildim FULL GPU app jednim PyInstaller během...
python -m PyInstaller VibePlayer.spec --clean
if errorlevel 1 (
    echo [ERROR] PyInstaller build selhal.
    goto :fail
)

echo.
echo [3/5] Overim, ze base build obsahuje AI runtime balicky (einops/kornia/...)...
python scripts\verify_release_bundle.py --dist-root "dist\VibePlayer"
if errorlevel 1 (
    echo [ERROR] Bundle verify selhal — nepokracuji na ZIP.
    goto :fail
)

echo.
echo [4/5] Delim full build na BASE + AUTOTAG GPU PACK ZIP (bez 7-Zip multi-volume)...
python scripts\split_release.py --dist-root "dist\VibePlayer" --out-dir "dist\releases"
if errorlevel 1 (
    echo [ERROR] Split release selhal.
    goto :fail
)

echo.
echo [5/5] Deaktivuji virtualni prostredi...
call venv\Scripts\deactivate.bat

echo.
echo ==========================================
echo HOTOVO!
echo Full build: dist\VibePlayer
echo Release ZIPy:
echo   - dist\releases\VibePlayer-base.zip
echo   - dist\releases\VibePlayer-autotag-gpu-pack.zip
echo     (nebo .part01.zip / .part02.zip ... — kazdy dil je normalni ZIP)
echo   - dist\releases\VibePlayer-rife-pack.zip  (pokud je v buildu)
echo GPU pack: rozbal vsechny ZIP dilky pres stejnou slozku VibePlayer\
echo ==========================================
pause
exit /b 0

:fail
echo.
echo [FAIL] Build release nedokoncen.
call venv\Scripts\deactivate.bat >nul 2>&1
pause
exit /b 1
