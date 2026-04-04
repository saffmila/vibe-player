@echo off
setlocal
REM ===== locate repo root and app dir =====
cd /d "%~dp0"
set "ROOT=%CD%"
set "APP=%ROOT%\app"

if not exist "%APP%\main.py" (
  echo [ERROR] main.py not found in "%APP%"
  echo Expected structure: ^<root^>\app\main.py
  echo Current ROOT: "%ROOT%"
  echo.
  pause
  exit /b 1
)

REM ===== pick PythonW from venv if present (W for Windowed) =====
set "PYW=%ROOT%\venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"

REM ===== Spustit GUI aplikaci bez konzole =====
pushd "%APP%"
start "" "%PYW%" main.py %*
popd

exit /b 0