@echo off
setlocal

cd /d "%~dp0.."
set "ROOT=%CD%"
set "APP=%ROOT%\app"
set "PY=%ROOT%\venv\Scripts\python.exe"
set "PROF_OUT=%ROOT%\profiling"

pushd "%APP%"

echo --- Test 1: Conservative (Workers: 4, Batch: 12) ---
"%PY%" -m cProfile -o "%PROF_OUT%\prof_w4_b12.prof" run_profiling.py --grid --workers 4 --batch 12

echo --- Test 2: Balanced (Workers: 8, Batch: 24) ---
"%PY%" -m cProfile -o "%PROF_OUT%\prof_w8_b24.prof" run_profiling.py --grid --workers 8 --batch 24

echo --- Test 3: Aggressive (Workers: 16, Batch: 48) ---
"%PY%" -m cProfile -o "%PROF_OUT%\prof_w16_b48.prof" run_profiling.py --grid --workers 16 --batch 48

popd
echo --- DONE! Check prof_*.prof in profiling\ ---
pause
