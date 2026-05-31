@echo off
setlocal
set "PROFILE_TITLE=VISIBLE THUMBNAILS"
set "PROFILE_OUTPUT=profile_visiblethumbnails.prof"
set "PROFILE_ARGS=--visiblethumbnails"
set "PROFILE_SORT=time"
set "PROFILE_LIMIT=30"
call "%~dp0_run_single_profile.bat"
exit /b %ERRORLEVEL%
