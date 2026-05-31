"""Run a profiling target, dump cProfile stats, then force-exit.

The app intentionally owns background worker pools. In normal use those threads are
fine, but batch profiling must continue after the Tk window closes. This wrapper
dumps stats from the main thread and then exits the process without waiting for
idle non-daemon worker threads.
"""

from __future__ import annotations

import cProfile
import os
import runpy
import sys
import traceback


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: _profile_wrapper.py <profile-output.prof> <runner.py> [runner args...]")
        os._exit(2)

    profile_output = sys.argv[1]
    runner = sys.argv[2]
    runner_args = sys.argv[3:]
    exit_code = 0

    profiler = cProfile.Profile()
    try:
        runner_dir = os.path.dirname(os.path.abspath(runner))
        if runner_dir and runner_dir not in sys.path:
            sys.path.insert(0, runner_dir)
        sys.argv = [runner, *runner_args]
        profiler.enable()
        runpy.run_path(runner, run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        exit_code = code if isinstance(code, int) else 1
    except BaseException:
        traceback.print_exc()
        exit_code = 1
    finally:
        profiler.disable()
        profiler.dump_stats(profile_output)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)


if __name__ == "__main__":
    main()
