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
echo [2/4] Buildim FULL GPU app jednim PyInstaller během...
python -m PyInstaller VibePlayer.spec --clean
if errorlevel 1 (
    echo [ERROR] PyInstaller build selhal.
    goto :fail
)

echo.
echo [3/4] Delim full build na BASE + AUTOTAG GPU PACK ZIP...
python scripts\split_release.py --dist-root "dist\VibePlayer" --out-dir "dist\releases"
if errorlevel 1 (
    echo [ERROR] Split release selhal.
    goto :fail
)

echo.
echo [4/4] Deaktivuji virtualni prostredi...
call venv\Scripts\deactivate.bat

echo.
echo ==========================================
echo HOTOVO!
echo Full build: dist\VibePlayer
echo Release ZIPy:
echo   - dist\releases\VibePlayer-base.zip
echo   - dist\releases\VibePlayer-autotag-gpu-pack.zip
echo ==========================================
pause
exit /b 0

:fail
echo.
echo [FAIL] Build release nedokoncen.
call venv\Scripts\deactivate.bat >nul 2>&1
pause
exit /b 1
