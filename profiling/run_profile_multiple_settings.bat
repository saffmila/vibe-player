@echo off
setlocal

set "WAS_NOPAUSE=%NOPAUSE%"
set "NOPAUSE=1"

call :run "GRID Conservative (Workers: 4, Batch: 12)" "prof_w4_b12.prof" "--grid --workers 4 --batch 12" || goto fail
call :run "GRID Balanced (Workers: 8, Batch: 24)" "prof_w8_b24.prof" "--grid --workers 8 --batch 24" || goto fail
call :run "GRID Aggressive (Workers: 16, Batch: 48)" "prof_w16_b48.prof" "--grid --workers 16 --batch 48" || goto fail

echo --- DONE! Check prof_*.prof in profiling\ ---
if not "%WAS_NOPAUSE%"=="1" pause
exit /b 0

:run
set "PROFILE_TITLE=%~1"
set "PROFILE_OUTPUT=%~2"
set "PROFILE_ARGS=%~3"
set "PROFILE_SORT=time"
set "PROFILE_LIMIT=30"
call "%~dp0_run_single_profile.bat"
exit /b %ERRORLEVEL%

:fail
set "RC=%ERRORLEVEL%"
echo [error] Multiple-settings profiling failed with exit code %RC%.
if not "%WAS_NOPAUSE%"=="1" pause
exit /b %RC%
