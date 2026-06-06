@echo off
setlocal
cd /d "%~dp0"

set "ROOT=%CD%"
set "EXE=%ROOT%\VibePlayer.exe"

if exist "%EXE%" goto unregister_build

set "EXE=%ROOT%\dist\VibePlayer\VibePlayer.exe"
if exist "%EXE%" goto unregister_build

if exist "%ROOT%\app\main.py" goto unregister_source

echo [ERROR] Could not find VibePlayer.exe or app\main.py.
echo Run this file from the extracted VibePlayer folder, or from the project root.
pause
exit /b 1

:unregister_build
echo Removing Vibe Player file associations for:
echo   %EXE%
echo.
start "" /wait "%EXE%" --unregister-file-associations
if errorlevel 1 goto failed
goto removed

:unregister_source
set "PY=%ROOT%\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python.exe"
echo Removing development file associations...
echo.
pushd "%ROOT%\app"
"%PY%" main.py --unregister-file-associations
set "EXIT_CODE=%ERRORLEVEL%"
popd
if not "%EXIT_CODE%"=="0" goto failed
goto removed

:removed
echo.
echo Vibe Player file association registry entries were removed.
pause
exit /b 0

:failed
echo.
echo [ERROR] Unregistration failed. Check app\app.log for details.
pause
exit /b 1
