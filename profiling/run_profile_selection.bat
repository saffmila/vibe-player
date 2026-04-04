@echo off
setlocal

cd /d "%~dp0.."
set "ROOT=%CD%"
set "APP=%ROOT%\app"
set "PY=%ROOT%\venv\Scripts\python.exe"
set "PROF_OUT=%ROOT%\profiling"

echo --- Running SELECTION STRESS profile ---

pushd "%APP%"
"%PY%" -m cProfile -o "%PROF_OUT%\profile_selection.prof" run_profiling.py --selection
popd

echo.
echo --- Profile results (Top 50 calls by Cumulative Time) ---
"%PY%" -c "import pstats; pstats.Stats(r'%PROF_OUT%\profile_selection.prof').sort_stats('cumtime').print_stats(50)"

if not "%NOPAUSE%"=="1" pause
