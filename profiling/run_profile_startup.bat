@echo off
setlocal
set "PROFILE_TITLE=STARTUP"
set "PROFILE_OUTPUT=profile_startup.prof"
set "PROFILE_ARGS=--startup"
set "PROFILE_SORT=time"
set "PROFILE_LIMIT=30"
call "%~dp0_run_single_profile.bat"
exit /b %ERRORLEVEL%
