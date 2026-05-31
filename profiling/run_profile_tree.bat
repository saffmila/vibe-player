@echo off
setlocal
set "PROFILE_TITLE=TREE"
set "PROFILE_OUTPUT=profile_tree.prof"
set "PROFILE_ARGS=--tree"
set "PROFILE_SORT=time"
set "PROFILE_LIMIT=30"
call "%~dp0_run_single_profile.bat"
exit /b %ERRORLEVEL%
