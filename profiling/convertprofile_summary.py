"""Summarize all .prof files in this directory (profiling/)."""
import glob
import os

import pstats

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

prof_files = glob.glob(os.path.join(_SCRIPT_DIR, "*.prof"))

for file in prof_files:
    print(f"Zpracovávám {file}...")
    base = os.path.splitext(os.path.basename(file))[0]

    with open(os.path.join(_SCRIPT_DIR, f"{base}_tottime.txt"), "w", encoding="utf-8") as f:
        p = pstats.Stats(file, stream=f)
        p.sort_stats("tottime").print_stats(40)

    with open(os.path.join(_SCRIPT_DIR, f"{base}_cumtime.txt"), "w", encoding="utf-8") as f:
        p = pstats.Stats(file, stream=f)
        p.sort_stats("cumtime").print_stats(40)

print("Hotovo! Výstupní textáky jsou ve složce profiling/.")
