@echo off
setlocal
set "PROFILE_TITLE=FOLDER SWITCHING"
set "PROFILE_OUTPUT=profile_switching.prof"
set "PROFILE_ARGS=--cprofile-switching"
set "PROFILE_SORT=cumtime"
set "PROFILE_LIMIT=40"
call "%~dp0_run_single_profile.bat"
exit /b %ERRORLEVEL%
