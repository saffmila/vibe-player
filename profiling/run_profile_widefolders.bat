@echo off
setlocal
set "PROFILE_TITLE=WIDE FOLDERS"
set "PROFILE_OUTPUT=profile_widefolders.prof"
set "PROFILE_ARGS=--widefolders"
set "PROFILE_SORT=time"
set "PROFILE_LIMIT=30"
call "%~dp0_run_single_profile.bat"
exit /b %ERRORLEVEL%
