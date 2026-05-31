@echo off
setlocal
set "PROFILE_TITLE=TREE STRESS"
set "PROFILE_OUTPUT=profile_tree_stress.prof"
set "PROFILE_ARGS=--treestress"
set "PROFILE_SORT=cumtime"
set "PROFILE_LIMIT=40"
call "%~dp0_run_single_profile.bat"
exit /b %ERRORLEVEL%
