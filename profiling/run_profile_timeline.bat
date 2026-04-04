@echo off
setlocal

cd /d "%~dp0.."
set "ROOT=%CD%"
set "APP=%ROOT%\app"
set "PY=%ROOT%\venv\Scripts\python.exe"
set "PROF_OUT=%ROOT%\profiling"

echo --- Running TIMELINE WIDGET profile ---

pushd "%APP%"
"%PY%" -m cProfile -o "%PROF_OUT%\profile_timeline.prof" run_profiling.py --timeline
popd

echo.
echo --- Profile results (Top 40 calls by Cumulative Time) ---
"%PY%" -c "import pstats; pstats.Stats(r'%PROF_OUT%\profile_timeline.prof').sort_stats('cumtime').print_stats(40)"

if not "%NOPAUSE%"=="1" pause
