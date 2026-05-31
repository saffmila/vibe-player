@echo off
setlocal
set "PROFILE_TITLE=SELECTION STRESS"
set "PROFILE_OUTPUT=profile_selection.prof"
set "PROFILE_ARGS=--selection"
set "PROFILE_SORT=cumtime"
set "PROFILE_LIMIT=50"
call "%~dp0_run_single_profile.bat"
exit /b %ERRORLEVEL%
