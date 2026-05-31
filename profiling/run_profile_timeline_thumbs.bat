@echo off
setlocal
set "PROFILE_TITLE=TIMELINE THUMB PIPELINE"
set "PROFILE_OUTPUT=profile_timeline_thumbs.prof"
set "PROFILE_ARGS=--thumbpipeline"
set "PROFILE_SORT=cumtime"
set "PROFILE_LIMIT=40"
call "%~dp0_run_single_profile.bat"
exit /b %ERRORLEVEL%
