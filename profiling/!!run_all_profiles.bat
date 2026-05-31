@echo off
setlocal
cd /d "%~dp0"
set "NOPAUSE=1"

echo [.] Running all profiling scripts...
call :run "run_profile_startup.bat" || goto fail
call :run "run_profile_tree.bat" || goto fail
call :run "run_profile_grid.bat" || goto fail
call :run "run_profile_visiblethumbnails.bat" || goto fail
call :run "run_profile_widefolders.bat" || goto fail
call :run "run_profile_switching.bat" || goto fail
call :run "run_profile_selection.bat" || goto fail
call :run "run_profile_imageview.bat" || goto fail
call :run "run_profile_treestress.bat" || goto fail
call :run "run_profile_timeline.bat" || goto fail
call :run "run_profile_timeline_thumbs.bat" || goto fail
call :run "run_profile_multiple_settings.bat" || goto fail

echo [ok] All profiling scripts completed.
exit /b 0

:run
echo.
echo [.] %~1
call "%~dp0%~1"
exit /b %ERRORLEVEL%

:fail
set "RC=%ERRORLEVEL%"
echo [error] Profiling stopped because one script failed. Exit code: %RC%
exit /b %RC%
