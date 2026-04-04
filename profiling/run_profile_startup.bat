@echo off
setlocal

REM Repo root = nadřazená složka k profiling/
cd /d "%~dp0.."
set "ROOT=%CD%"
set "APP=%ROOT%\app"
set "PY=%ROOT%\venv\Scripts\python.exe"
set "PROF_OUT=%ROOT%\profiling"

echo --- Running STARTUP profile ---

pushd "%APP%"
"%PY%" -m cProfile -o "%PROF_OUT%\profile_startup.prof" run_profiling.py --startup
popd

echo.
echo --- Profile results ---
"%PY%" -c "import pstats; pstats.Stats(r'%PROF_OUT%\profile_startup.prof').sort_stats('time').print_stats(30)"

if not "%NOPAUSE%"=="1" pause
