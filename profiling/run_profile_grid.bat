@echo off
setlocal
set "PROFILE_TITLE=GRID"
set "PROFILE_OUTPUT=profile_grid.prof"
set "PROFILE_ARGS=--cprofile-grid"
set "PROFILE_SORT=time"
set "PROFILE_LIMIT=30"
call "%~dp0_run_single_profile.bat"
exit /b %ERRORLEVEL%
