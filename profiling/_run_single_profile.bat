@echo off
setlocal

if "%PROFILE_TITLE%"=="" (
    echo [error] PROFILE_TITLE is not set.
    exit /b 2
)
if "%PROFILE_OUTPUT%"=="" (
    echo [error] PROFILE_OUTPUT is not set.
    exit /b 2
)
if "%PROFILE_ARGS%"=="" (
    echo [error] PROFILE_ARGS is not set.
    exit /b 2
)
if "%PROFILE_SORT%"=="" set "PROFILE_SORT=time"
if "%PROFILE_LIMIT%"=="" set "PROFILE_LIMIT=30"

cd /d "%~dp0.."
set "ROOT=%CD%"
set "APP=%ROOT%\app"
set "PY=%ROOT%\venv\Scripts\python.exe"
set "PROF_OUT=%ROOT%\profiling"
set "RUNNER=%APP%\run_profiling.py"
set "PROFILE_WRAPPER=%PROF_OUT%\_profile_wrapper.py"
set "PROFILE_PATH=%PROF_OUT%\%PROFILE_OUTPUT%"

if not exist "%PY%" (
    echo [error] Python venv not found: %PY%
    if not "%NOPAUSE%"=="1" pause
    exit /b 1
)
if not exist "%RUNNER%" (
    echo [error] Profiling runner not found: %RUNNER%
    echo [hint] Restore or create app\run_profiling.py before running profiling batches.
    if not "%NOPAUSE%"=="1" pause
    exit /b 1
)
if not exist "%PROFILE_WRAPPER%" (
    echo [error] Profiling wrapper not found: %PROFILE_WRAPPER%
    if not "%NOPAUSE%"=="1" pause
    exit /b 1
)
if not exist "%PROF_OUT%" mkdir "%PROF_OUT%"

if exist "%PROFILE_PATH%" del "%PROFILE_PATH%"

echo --- Running %PROFILE_TITLE% profile ---
pushd "%APP%" || exit /b 1
"%PY%" "%PROFILE_WRAPPER%" "%PROFILE_PATH%" "%RUNNER%" %PROFILE_ARGS%
set "RC=%ERRORLEVEL%"
popd

if not "%RC%"=="0" (
    echo [error] Profile command failed with exit code %RC%.
    if not "%NOPAUSE%"=="1" pause
    exit /b %RC%
)
if not exist "%PROFILE_PATH%" (
    echo [error] Profile output was not created: %PROFILE_PATH%
    if not "%NOPAUSE%"=="1" pause
    exit /b 1
)

echo.
echo --- Profile results: %PROFILE_OUTPUT% sorted by %PROFILE_SORT% ---
"%PY%" -c "import pstats; pstats.Stats(r'%PROFILE_PATH%').sort_stats('%PROFILE_SORT%').print_stats(%PROFILE_LIMIT%)"
set "RC=%ERRORLEVEL%"

if not "%NOPAUSE%"=="1" pause
exit /b %RC%
