@echo off
setlocal
set "PROFILE_TITLE=TIMELINE WIDGET"
set "PROFILE_OUTPUT=profile_timeline.prof"
set "PROFILE_ARGS=--timeline"
set "PROFILE_SORT=cumtime"
set "PROFILE_LIMIT=40"
call "%~dp0_run_single_profile.bat"
exit /b %ERRORLEVEL%
