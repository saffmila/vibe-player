@echo off
setlocal
cd /d "%~dp0"

title VibePlayer - PyInstaller Build
echo ==========================================
echo        VIBE PLAYER - BUILD EXECUTABLE
echo ==========================================
echo.

if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] Virtualni prostredi neexistuje. Nejdriv spust install.bat.
    pause
    exit /b 1
)

echo [1/3] Aktivuji virtualni prostredi (venv)...
call venv\Scripts\activate.bat

echo.
echo [2/3] Spoustim PyInstaller (cisteni mezipameti zapnuto)...
python -m PyInstaller VibePlayer.spec --clean

echo.
echo [3/3] Deaktivuji virtualni prostredi...
call venv\Scripts\deactivate.bat

echo.
echo ==========================================
echo  HOTOVO! Pokud nevidis nahore zadny ERROR,
echo  tva aplikace ceka ve slozce: dist\VibePlayer
echo  Pro prenos/spusteni jinde zkopiruj CELOU tuto slozku, nejen VibePlayer.exe.
echo ==========================================
pause