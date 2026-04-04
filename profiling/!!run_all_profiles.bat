@echo off
cd /d "%~dp0"
echo [.] Spoustim vsechny profilovaci skripty...
set NOPAUSE=1
call "%~dp0run_profile_startup.bat"
timeout /t 1 >nul

call "%~dp0run_profile_tree.bat"
timeout /t 1 >nul

call "%~dp0run_profile_grid.bat"
timeout /t 1 >nul

call "%~dp0run_profile_visiblethumbnails.bat"
timeout /t 1 >nul

if exist "%~dp0run_profile_mainloop.bat" call "%~dp0run_profile_mainloop.bat"
if exist "%~dp0run_profile_mainloop.bat" timeout /t 1 >nul

call "%~dp0run_profile_switching.bat"
timeout /t 1 >nul

call "%~dp0run_profile_selection.bat"
timeout /t 1 >nul

echo [ok] Vsechny dostupne profilovaci skripty probehly.

if exist "%~dp0show_profile.bat" (
    echo Spoustim show_profile...
    call "%~dp0show_profile.bat"
)
