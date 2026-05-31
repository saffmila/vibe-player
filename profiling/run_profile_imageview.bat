@echo off
setlocal
set "PROFILE_TITLE=IMAGE VIEWER"
set "PROFILE_OUTPUT=profile_imageview.prof"
set "PROFILE_ARGS=--imageviewer"
set "PROFILE_SORT=cumtime"
set "PROFILE_LIMIT=50"
call "%~dp0_run_single_profile.bat"
exit /b %ERRORLEVEL%
