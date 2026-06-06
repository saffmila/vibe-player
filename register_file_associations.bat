@echo off
setlocal
cd /d "%~dp0"

set "ROOT=%CD%"
set "EXE=%ROOT%\VibePlayer.exe"

if exist "%EXE%" goto register_build

set "EXE=%ROOT%\dist\VibePlayer\VibePlayer.exe"
if exist "%EXE%" goto register_build

if exist "%ROOT%\app\main.py" goto register_source

echo [ERROR] Could not find VibePlayer.exe or app\main.py.
echo Run this file from the extracted VibePlayer folder, or from the project root.
pause
exit /b 1

:register_build
echo Registering file associations for:
echo   %EXE%
echo.
start "" /wait "%EXE%" --register-file-associations
if errorlevel 1 goto failed
goto registered

:register_source
set "PY=%ROOT%\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python.exe"
echo Registering development file associations through run.bat...
echo.
pushd "%ROOT%\app"
"%PY%" main.py --register-file-associations
set "EXIT_CODE=%ERRORLEVEL%"
popd
if not "%EXIT_CODE%"=="0" goto failed
goto registered

:registered
echo.
echo Vibe Player is now registered in Windows.
echo Windows may still require one manual selection:
echo   Settings ^> Apps ^> Default apps ^> choose by file type
echo or right-click a media file ^> Open with ^> Choose another app.
echo.
echo Opening Default apps settings...
start "" "ms-settings:defaultapps"
pause
exit /b 0

:failed
echo.
echo [ERROR] Registration failed. Check app\app.log for details.
pause
exit /b 1
