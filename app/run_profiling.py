"""
Profiling and stress tests for Vibe Player (thumbnail grid, timeline, folder switching).

Folder lists are **not** hardcoded. Configure either:

- Environment variables (``os.pathsep``-separated paths), e.g.
  ``VIBE_PLAYER_PROFILE_GRID_FOLDERS``, ``VIBE_PLAYER_PROFILE_VIDEO_DIR``, or
- Text files next to this module (one absolute path per line, ``#`` comments allowed):
  ``profile_paths_grid.txt``, ``profile_video_dir.txt``, ``profile_paths_wide.txt``,
  ``profile_paths_switching.txt``, ``profile_paths_selection.txt``.

Run: ``python run_profiling.py --grid`` (see ``main()`` for all profile flags).

**CPU profile (main thread)** — ``python run_profiling.py --cprofile-grid``

Uses the first folder from the same sources as ``--grid``. Optional:

- ``VIBE_PLAYER_PROFILE_CPROFILE_MS`` — how long to keep the profiler on after starting
  ``display_thumbnails`` (default ``10000``). The window includes Tk event processing.
- Writes ``thumbnail_grid_cprofile.prof`` next to this script (load in SnakeViz, py-spy, etc.).

Note: standard ``cProfile`` attributes most time to the **main thread**. Work done inside
``ThreadPoolExecutor`` workers appears indirectly (e.g. ``after`` callbacks, queue drains),
not as full PIL/OpenCV stacks in worker threads.
"""

import cProfile
import io
import logging
import os
import pstats
import sys
import time
from pathlib import Path

from video_thumbnail_player import VideoThumbnailPlayer
from logging_setup import setup_logging

_APP_DIR = Path(__file__).resolve().parent


def _paths_from_lines_file(filename: str) -> list[str]:
    p = _APP_DIR / filename
    if not p.is_file():
        return []
    out: list[str] = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def _paths_from_env(key: str) -> list[str]:
    raw = os.environ.get(key, "")
    if not raw.strip():
        return []
    return [x.strip() for x in raw.split(os.pathsep) if x.strip()]


def _merge_paths(*lists: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for p in lst:
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def apply_overrides(app, workers, batch):
    """Apply thread-pool and batch-size overrides on the app instance."""
    if workers:
        app.executor._max_workers = workers
        logging.info("Performance override: workers=%s", workers)
    if batch:
        app.thumb_batch_size = batch
        logging.info("Performance override: batch_size=%s", batch)


def check_path(path):
    """Return True if ``path`` exists; log a warning otherwise."""
    if not os.path.exists(path):
        logging.warning("Path does not exist: %s", path)
        return False
    return True


def profile_grid_render(log_path, workers=None, batch=None):
    """
    Cycle through multiple folders to profile thumbnail grid throughput.

    Configure folders via env or ``profile_paths_grid.txt`` (see module docstring).
    """
    app = VideoThumbnailPlayer(log_path=log_path)
    apply_overrides(app, workers, batch)

    folders_to_profile = _merge_paths(
        _paths_from_env("VIBE_PLAYER_PROFILE_GRID_FOLDERS"),
        _paths_from_lines_file("profile_paths_grid.txt"),
    )
    if not folders_to_profile:
        logging.warning(
            "No grid profile folders: set VIBE_PLAYER_PROFILE_GRID_FOLDERS (%s-separated) "
            "or create profile_paths_grid.txt beside this script (one folder per line).",
            os.pathsep,
        )
        app.after(1000, app.destroy)
        return

    def run_sequence(index):
        if index >= len(folders_to_profile):
            logging.info("Grid stress test completed, closing.")
            app.after(5000, app.destroy)
            return

        path = folders_to_profile[index]
        if check_path(path):
            logging.info(
                "[GRID PROFILE] Loading (%s/%s): %s",
                index + 1,
                len(folders_to_profile),
                path,
            )
            app.display_thumbnails(path)
            # Wait for one folder to load before switching (raise if folders are huge).
            app.after(8000, lambda: run_sequence(index + 1))
        else:
            run_sequence(index + 1)

    app.after(1000, lambda: run_sequence(0))
    app.mainloop()

def profile_timeline_thumbnails(log_path, workers=None, batch=None):
    """
    Open several videos sequentially and measure timeline thumbnail load times.

    Uses ``VIBE_PLAYER_PROFILE_VIDEO_DIR`` or the first line of ``profile_video_dir.txt``.
    Run: ``python run_profiling.py --thumbpipeline``.
    """
    video_lines = _paths_from_lines_file("profile_video_dir.txt")
    VIDEO_DIR = (
        os.environ.get("VIBE_PLAYER_PROFILE_VIDEO_DIR", "").strip()
        or (video_lines[0] if video_lines else "")
    )
    NUM_THUMBS = 5
    TEST_COUNT = 5
    SETTLE_MS = 400

    app = VideoThumbnailPlayer(log_path=log_path)
    apply_overrides(app, workers, batch)
    timings = []

    def start_test():
        if not VIDEO_DIR:
            logging.error(
                "No video folder: set VIBE_PLAYER_PROFILE_VIDEO_DIR or profile_video_dir.txt"
            )
            app.destroy()
            return
        if not check_path(VIDEO_DIR):
            logging.error("Folder does not exist: %s", VIDEO_DIR)
            app.destroy()
            return
        logging.info("[PROFILE] Loading grid from folder: %s", VIDEO_DIR)
        app.display_thumbnails(VIDEO_DIR)
        app.after(3000, pick_videos)

    def pick_videos():
        VIDEO_EXTS = ('.mp4', '.avi', '.mov', '.mkv', '.wmv')
        videos = [v for v in getattr(app, 'video_files', [])
                  if v["path"].lower().endswith(VIDEO_EXTS)]
        if not videos:
            logging.info("[ERROR] No videos found in grid.")
            app.destroy()
            return
        test_videos = videos[:TEST_COUNT]
        logging.info(
            "[PROFILE] Testing %s videos (single-click + timeline thumb wait)",
            len(test_videos),
        )
        run_step(0, test_videos)

    def run_step(idx, test_videos):
        if idx >= len(test_videos):
            print_summary()
            app.after(1500, app.destroy)
            return

        video    = test_videos[idx]
        vpath    = video["path"]
        vname    = video["name"]
        entry    = {"name": vname, "t_click": None, "t_first": None, "t_all": None}
        timings.append(entry)

        logging.info("[%s/%s] Click: %s", idx + 1, len(test_videos), vname)

        entry["t_click"] = time.perf_counter()

        tw = getattr(app, 'timeline_widget', None)
        if tw is None:
            logging.warning("timeline_widget unavailable, skipping")
            app.after(500, lambda: run_step(idx + 1, test_videos))
            return

        # Resetujeme thumb_images aby poll věděl kdy přišly nové thumby
        tw.thumb_images = []

        # Simulujeme REÁLNÝ singleclick flow přes _handle_thumbnail_single_click
        # (ten čeká 400ms debounce, pak spustí load_heavy_preview = MediaInfo + VLC + timeline)
        if hasattr(app, '_handle_thumbnail_single_click'):
            app._handle_thumbnail_single_click(vpath)
            logging.info("  _handle_thumbnail_single_click (400ms debounce)")
        else:
            # Fallback: přímé volání load_thumbnails
            tw.load_thumbnails(video_path=vpath, num_thumbs=NUM_THUMBS)

        # Pollujeme dokud se neobjeví thumbnaile
        poll_for_thumbs(idx, test_videos, entry, attempt=0)

    def poll_for_thumbs(idx, test_videos, entry, attempt):
        tw = getattr(app, 'timeline_widget', None)
        if tw is None:
            app.after(500, lambda: run_step(idx + 1, test_videos))
            return

        valid = [t for t in tw.thumb_images if t[1] != -1]

        now = time.perf_counter()

        if valid and entry["t_first"] is None:
            entry["t_first"] = now
            first_ms = (now - entry["t_click"]) * 1000
            logging.info(f"  → 1. thumb za {first_ms:.0f} ms")

        if len(valid) >= NUM_THUMBS:
            entry["t_all"] = now
            all_ms = (now - entry["t_click"]) * 1000
            logging.info(f"  → všechny thumby za {all_ms:.0f} ms")
            app.after(300, lambda: run_step(idx + 1, test_videos))
            return

        if attempt < 100:  # max 10s
            app.after(100, lambda: poll_for_thumbs(idx, test_videos, entry, attempt + 1))
        else:
            logging.info(f"  → TIMEOUT (valid={len(valid)}/{NUM_THUMBS})")
            app.after(300, lambda: run_step(idx + 1, test_videos))

    def print_summary():
        logging.info(f"\n{'='*62}")
        logging.info(f"  {'VIDEO':<38} {'1st':>8}  {'all':>8}")
        logging.info(f"  {'-'*38} {'-'*8}  {'-'*8}")
        first_times = []
        all_times   = []
        for e in timings:
            t_first = (e["t_first"] - e["t_click"]) * 1000 if e["t_first"] else -1
            t_all   = (e["t_all"]   - e["t_click"]) * 1000 if e["t_all"]   else -1
            if t_first > 0: first_times.append(t_first)
            if t_all   > 0: all_times.append(t_all)
            logging.info(f"  {e['name'][:38]:<38} {t_first:>7.0f}ms  {t_all:>7.0f}ms")
        logging.info(f"  {'─'*58}")
        if first_times:
            logging.info(f"  {'AVG':<38} {sum(first_times)/len(first_times):>7.0f}ms  {sum(all_times)/len(all_times):>7.0f}ms")
        logging.info(f"{'='*62}\n")

    app.after(1000, start_test)
    app.mainloop()


def profile_timeline_performance(log_path, workers=None, batch=None):
    """
    Open several videos in sequence and measure timeline thumbnail load times.

    Uses ``VIBE_PLAYER_PROFILE_VIDEO_DIR`` or ``profile_video_dir.txt`` (same as thumb pipeline).
    """
    app = VideoThumbnailPlayer(log_path=log_path)
    apply_overrides(app, workers, batch)

    vlines = _paths_from_lines_file("profile_video_dir.txt")
    video_dir = (
        os.environ.get("VIBE_PLAYER_PROFILE_VIDEO_DIR", "").strip()
        or (vlines[0] if vlines else "")
    )

    def start_test():
        if not video_dir:
            logging.error(
                "No video folder: set VIBE_PLAYER_PROFILE_VIDEO_DIR or profile_video_dir.txt"
            )
            app.destroy()
            return
        if check_path(video_dir):
            logging.info("[TIMELINE PROFILE] Loading folder: %s", video_dir)
            app.display_thumbnails(video_dir)
            app.after(4000, run_timeline_sequence)
        else:
            logging.error("[TIMELINE PROFILE] Folder not found: %s", video_dir)
            app.destroy()

    def run_timeline_sequence():
        VIDEO_EXTS = ('.mp4', '.avi', '.mov', '.mkv', '.wmv')
        videos = [v for v in app.video_files if v["path"].lower().endswith(VIDEO_EXTS)]

        if not videos:
            logging.info("--- [TIMELINE PROFILE] No videos found. ---")
            app.destroy()
            return

        test_videos = videos[:5]
        logging.info(f"--- [TIMELINE PROFILE] Starting sequence for {len(test_videos)} videos ---")
        timings = {}

        def open_step(idx):
            if idx >= len(test_videos):
                logging.info(f"\n{'='*55}")
                logging.info("  TIMELINE LOAD TIMINGS SUMMARY")
                logging.info(f"{'='*55}")
                for name, (t_start, t_first, t_done) in timings.items():
                    first_ms = (t_first - t_start) * 1000 if t_first else -1
                    done_ms  = (t_done  - t_start) * 1000 if t_done  else -1
                    logging.info(f"  {name[:40]:<40} first={first_ms:6.0f}ms  all={done_ms:6.0f}ms")
                logging.info(f"{'='*55}\n")
                app.after(2000, app.destroy)
                return

            video = test_videos[idx]
            name  = video['name']
            logging.info(f"\n--- [TIMELINE PROFILE] ({idx+1}/{len(test_videos)}) Opening: {name} ---")

            t_start = time.perf_counter()
            timings[name] = [t_start, None, None]

            # Monkey-patch timeline widget pro zachycení prvního a posledního thumbnailu
            app.open_video_player(video['path'], name)

            def poll_thumbs(attempt=0):
                tw = getattr(app, 'timeline_widget', None)
                if tw is None:
                    if attempt < 20:
                        app.after(200, lambda: poll_thumbs(attempt + 1))
                    return

                valid = [t for t in tw.thumb_images if t[1] != -1]

                if valid and timings[name][1] is None:
                    timings[name][1] = time.perf_counter()
                    logging.info(f"    First thumb visible: {(timings[name][1]-t_start)*1000:.0f} ms")

                if len(valid) >= len(tw.thumb_images) and tw.thumb_images:
                    timings[name][2] = time.perf_counter()
                    logging.info(f"    All thumbs visible : {(timings[name][2]-t_start)*1000:.0f} ms")
                    app.after(800, lambda: close_and_next(idx))
                    return

                if attempt < 60:  # max 12s
                    app.after(200, lambda: poll_thumbs(attempt + 1))
                else:
                    logging.info(f"    TIMEOUT waiting for thumbs")
                    app.after(500, lambda: close_and_next(idx))

            app.after(300, poll_thumbs)

        def close_and_next(idx):
            if hasattr(app, 'close_video_player'):
                app.close_video_player()
            app.after(800, lambda: open_step(idx + 1))

        open_step(0)

    app.after(1000, start_test)
    app.mainloop()


def profile_wide_folders(log_path, workers=None, batch=None):
    """
    Profile thumbnail rendering in Wide folder view.

    Folder list: ``VIBE_PLAYER_PROFILE_WIDE_FOLDERS`` or ``profile_paths_wide.txt``.
    """
    app = VideoThumbnailPlayer(log_path=log_path)
    apply_overrides(app, workers, batch)

    if hasattr(app, "folder_view_mode"):
        app.folder_view_mode.set("Wide")
        logging.debug("Profiling: folder view mode set to Wide")

    folders_to_test = _merge_paths(
        _paths_from_env("VIBE_PLAYER_PROFILE_WIDE_FOLDERS"),
        _paths_from_lines_file("profile_paths_wide.txt"),
    )

    valid_folders = [f for f in folders_to_test if check_path(f)]

    if not valid_folders:
        logging.error(
            "No valid wide-mode profile folders. Set VIBE_PLAYER_PROFILE_WIDE_FOLDERS or "
            "create profile_paths_wide.txt beside this script."
        )
        app.destroy()
        return

    def run_test(idx=0):
        if idx < len(valid_folders):
            logging.info(
                "[PROFILE] Wide folders %s/%s: %s",
                idx + 1,
                len(valid_folders),
                valid_folders[idx],
            )
            app.display_thumbnails(valid_folders[idx])
            app.after(5000, lambda: run_test(idx + 1))
        else:
            logging.info("[PROFILE] Wide folder sequence finished, closing.")
            app.destroy()

    app.after(500, lambda: run_test(0))
    app.mainloop()


def profile_folder_switching(log_path, workers=None, batch=None):
    """Switch between folders sequentially, then exit (folder switching stress test)."""
    app = VideoThumbnailPlayer(log_path=log_path)
    apply_overrides(app, workers, batch)

    folders_to_test = _merge_paths(
        _paths_from_env("VIBE_PLAYER_PROFILE_SWITCH_FOLDERS"),
        _paths_from_lines_file("profile_paths_switching.txt"),
    )
    if not folders_to_test:
        logging.warning(
            "No folders for switching test. Set VIBE_PLAYER_PROFILE_SWITCH_FOLDERS or "
            "profile_paths_switching.txt."
        )
        app.after(1000, app.destroy)
        return
    
    def switch_step(index):
        if index >= len(folders_to_test):
            logging.info("--- Switching test done, closing. ---")
            app.after(1000, app.destroy)
            return
        path = folders_to_test[index]
        if check_path(path):
            logging.info(f"--- [SWITCHING]: {path} ---")
            app.display_thumbnails(path)
        app.after(5000, lambda: switch_step(index + 1))
        
    app.after(1000, lambda: switch_step(0))
    app.mainloop()

def profile_selection_stress(log_path, workers=None, batch=None):
    """
    Simulate grid clicks and measure latency until timeline thumbnails appear.

    Target folder: ``VIBE_PLAYER_PROFILE_SELECTION_DIR`` or first line of
    ``profile_paths_selection.txt``. Run: ``python run_profiling.py --selection``.
    """
    s_lines = _paths_from_lines_file("profile_paths_selection.txt")
    target_dir = (
        os.environ.get("VIBE_PLAYER_PROFILE_SELECTION_DIR", "").strip()
        or (s_lines[0] if s_lines else "")
    )
    TEST_COUNT = 8
    WAIT_MS = 2500

    app = VideoThumbnailPlayer(log_path=log_path)
    apply_overrides(app, workers, batch)
    timings = []

    def start_test():
        if not target_dir:
            logging.error(
                "No selection test folder: set VIBE_PLAYER_PROFILE_SELECTION_DIR or "
                "profile_paths_selection.txt"
            )
            app.destroy()
            return
        if not check_path(target_dir):
            logging.error("Folder does not exist: %s", target_dir)
            app.destroy()
            return
        logging.info("[PROFILE] Loading grid: %s", target_dir)
        app.display_thumbnails(target_dir)
        app.after(4000, start_selection_loop)

    def start_selection_loop():
        VIDEO_EXTS = ('.mp4', '.avi', '.mov', '.mkv', '.wmv')
        videos = [v for v in getattr(app, 'video_files', [])
                  if v["path"].lower().endswith(VIDEO_EXTS)]
        if not videos:
            logging.info("[ERROR] No videos.")
            app.destroy()
            return
        test_videos = videos[:TEST_COUNT]
        logging.info("[PROFILE] Testing %s videos (debounce=200ms)", len(test_videos))
        select_step(0, test_videos)

    def select_step(idx, test_videos):
        if idx >= len(test_videos):
            print_summary()
            app.after(1500, app.destroy)
            return

        video = test_videos[idx]
        vpath = video["path"]
        vname = video["name"]
        entry = {"name": vname, "t_click": None, "t_load_start": None,
                 "t_first_thumb": None, "t_all_thumbs": None}
        timings.append(entry)

        logging.info("[%s/%s] Click: %s", idx + 1, len(test_videos), vname)
        entry["t_click"] = time.perf_counter()

        # Monkey-patch load_thumbnails aby zachytil kdy přesně začne
        tw = getattr(app, 'timeline_widget', None)
        if tw:
            original_load = tw.load_thumbnails
            def patched_load(*args, **kwargs):
                entry["t_load_start"] = time.perf_counter()
                delay = (entry["t_load_start"] - entry["t_click"]) * 1000
                logging.info("  load_thumbnails started after %.0f ms from click", delay)
                tw.load_thumbnails = original_load  # obnovíme
                return original_load(*args, **kwargs)
            tw.load_thumbnails = patched_load
            tw.thumb_images = []

        # Simulujeme reálný klik přes _handle_thumbnail_single_click
        if hasattr(app, '_handle_thumbnail_single_click'):
            app._handle_thumbnail_single_click(vpath)
        elif hasattr(app, 'select_thumbnail'):
            app.select_thumbnail(idx)

        poll_thumbs(idx, test_videos, entry, attempt=0)

    def poll_thumbs(idx, test_videos, entry, attempt):
        tw = getattr(app, 'timeline_widget', None)
        if tw is None:
            app.after(WAIT_MS, lambda: select_step(idx + 1, test_videos))
            return

        valid = [t for t in tw.thumb_images if t[1] != -1]
        now   = time.perf_counter()

        if valid and entry["t_first_thumb"] is None:
            entry["t_first_thumb"] = now
            ms = (now - entry["t_click"]) * 1000
            logging.info(f"  → 1. thumb za {ms:.0f}ms od kliknutí")

        if len(valid) >= getattr(tw, 'num_thumbs', 5):
            entry["t_all_thumbs"] = now
            ms = (now - entry["t_click"]) * 1000
            logging.info(f"  → všechny thumby za {ms:.0f}ms od kliknutí")
            app.after(400, lambda: select_step(idx + 1, test_videos))
            return

        max_attempts = int(WAIT_MS / 100)
        if attempt < max_attempts:
            app.after(100, lambda: poll_thumbs(idx, test_videos, entry, attempt + 1))
        else:
            logging.info(f"  → TIMEOUT (valid={len(valid)}/{getattr(tw,'num_thumbs',5)})")
            app.after(300, lambda: select_step(idx + 1, test_videos))

    def print_summary():
        logging.info(f"\n{'='*65}")
        logging.info(f"  {'VIDEO':<35} {'debounce':>9} {'1.thumb':>8} {'všechny':>8}")
        logging.info(f"  {'-'*35} {'-'*9} {'-'*8} {'-'*8}")
        for e in timings:
            t_load  = (e["t_load_start"]  - e["t_click"]) * 1000 if e["t_load_start"]  else -1
            t_first = (e["t_first_thumb"] - e["t_click"]) * 1000 if e["t_first_thumb"] else -1
            t_all   = (e["t_all_thumbs"]  - e["t_click"]) * 1000 if e["t_all_thumbs"]  else -1
            logging.info(f"  {e['name'][:35]:<35} {t_load:>8.0f}ms {t_first:>7.0f}ms {t_all:>7.0f}ms")
        valid = [e for e in timings if e["t_first_thumb"]]
        if valid:
            avg_first = sum((e["t_first_thumb"]-e["t_click"])*1000 for e in valid) / len(valid)
            avg_all   = sum((e["t_all_thumbs"] -e["t_click"])*1000 for e in valid if e["t_all_thumbs"]) / len(valid)
            logging.info(f"  {'─'*63}")
            logging.info(f"  {'AVG':<35} {'':>9} {avg_first:>7.0f}ms {avg_all:>7.0f}ms")
        logging.info(f"{'='*65}\n")

    app.after(1000, start_test)
    app.mainloop()

def launch_only(log_path, workers=None, batch=None):
    """Measures only startup time."""
    app = VideoThumbnailPlayer(log_path=log_path)
    apply_overrides(app, workers, batch)
    app.after(2000, app.destroy)
    app.mainloop()


def profile_cprofile_grid(log_path, workers=None, batch=None):
    """
    CPU profile of the main thread during thumbnail grid load.

    Uses the first folder from ``profile_paths_grid.txt`` or the
    ``VIBE_PLAYER_PROFILE_GRID_FOLDERS`` env variable.

    Sampling window: VIBE_PLAYER_PROFILE_CPROFILE_MS ms (default 10 000).

    Output files written next to this script:
      - ``thumbnail_grid_cprofile.prof``   — binary pstats file (open in SnakeViz)
      - ``thumbnail_grid_cprofile.txt``    — top-60 by cumtime (plain text)
    """
    import cProfile
    import io
    import pstats

    folders = _merge_paths(
        _paths_from_env("VIBE_PLAYER_PROFILE_GRID_FOLDERS"),
        _paths_from_lines_file("profile_paths_grid.txt"),
    )
    if not folders:
        logging.error(
            "--cprofile-grid: no folder configured.\n"
            "  Create app/profile_paths_grid.txt with one absolute folder path per line,\n"
            "  or set VIBE_PLAYER_PROFILE_GRID_FOLDERS=<path>."
        )
        return

    target_folder = folders[0]
    if not check_path(target_folder):
        return

    measure_ms = int(os.environ.get("VIBE_PLAYER_PROFILE_CPROFILE_MS", "10000"))
    prof_path = str(_APP_DIR / "thumbnail_grid_cprofile.prof")
    txt_path  = str(_APP_DIR / "thumbnail_grid_cprofile.txt")

    logging.info("[cProfile] folder : %s", target_folder)
    logging.info("[cProfile] window : %s ms after display_thumbnails()", measure_ms)
    logging.info("[cProfile] output : %s", prof_path)

    pr = cProfile.Profile()

    app = VideoThumbnailPlayer(log_path=log_path)
    apply_overrides(app, workers, batch)

    def _start_profiling():
        logging.info("[cProfile] profiler START")
        pr.enable()
        app.display_thumbnails(target_folder)
        app.after(measure_ms, _stop_profiling)

    def _stop_profiling():
        pr.disable()
        logging.info("[cProfile] profiler STOP — writing results")

        pr.dump_stats(prof_path)

        stream = io.StringIO()
        ps = pstats.Stats(pr, stream=stream)
        ps.strip_dirs()
        ps.sort_stats("cumulative")
        ps.print_stats(60)
        report = stream.getvalue()

        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(report)

        logging.info("[cProfile] Top 60 by cumtime:\n%s", report)
        logging.info("[cProfile] Binary profile: %s", prof_path)
        logging.info("[cProfile] Text report   : %s", txt_path)
        logging.info("[cProfile] Visualise with: pip install snakeviz && snakeviz %s", prof_path)

        app.after(1000, app.destroy)

    app.after(1500, _start_profiling)
    app.mainloop()


def profile_cprofile_switching(log_path, workers=None, batch=None):
    """
    CPU profile of the main thread across multiple folder switches.

    Profiler runs continuously from the first ``display_thumbnails`` call until the last
    folder finishes loading, so the output captures the full cost of clearing, sorting,
    queueing and batch-processing thumbnails across *all* folders.

    Folder list: same sources as ``--switching``
      (``profile_paths_switching.txt`` or ``VIBE_PLAYER_PROFILE_SWITCH_FOLDERS``).

    Each folder gets ``VIBE_PLAYER_PROFILE_SWITCH_DWELL_MS`` ms before the next switch
    (default 6 000).  After the last folder the profiler stops and results are written.

    Output files (next to this script):
      - ``switching_cprofile.prof``  — binary pstats (SnakeViz)
      - ``switching_cprofile.txt``   — top-80 by cumtime (plain text)
    """
    import cProfile
    import io
    import pstats

    folders = _merge_paths(
        _paths_from_env("VIBE_PLAYER_PROFILE_SWITCH_FOLDERS"),
        _paths_from_lines_file("profile_paths_switching.txt"),
    )
    if not folders:
        logging.error(
            "--cprofile-switching: no folders configured.\n"
            "  Create app/profile_paths_switching.txt (one absolute path per line)\n"
            "  or set VIBE_PLAYER_PROFILE_SWITCH_FOLDERS=path1%spath2..." ,
            os.pathsep,
        )
        return

    dwell_ms = int(os.environ.get("VIBE_PLAYER_PROFILE_SWITCH_DWELL_MS", "6000"))
    prof_path = str(_APP_DIR / "switching_cprofile.prof")
    txt_path  = str(_APP_DIR / "switching_cprofile.txt")

    logging.info("[cProfile-switching] %s folders, dwell=%s ms each", len(folders), dwell_ms)
    logging.info("[cProfile-switching] output: %s", prof_path)

    pr = cProfile.Profile()
    app = VideoThumbnailPlayer(log_path=log_path)
    apply_overrides(app, workers, batch)

    def _switch_step(index):
        if index >= len(folders):
            _finish()
            return

        path = folders[index]
        if not check_path(path):
            logging.warning("[cProfile-switching] skipping missing folder: %s", path)
            app.after(0, lambda: _switch_step(index + 1))
            return

        logging.info(
            "[cProfile-switching] %s/%s → %s",
            index + 1, len(folders), path,
        )
        app.display_thumbnails(path)
        app.after(dwell_ms, lambda: _switch_step(index + 1))

    def _start():
        logging.info("[cProfile-switching] profiler START")
        pr.enable()
        _switch_step(0)

    def _finish():
        pr.disable()
        logging.info("[cProfile-switching] profiler STOP — writing results")

        pr.dump_stats(prof_path)

        stream = io.StringIO()
        ps = pstats.Stats(pr, stream=stream)
        ps.strip_dirs()
        ps.sort_stats("cumulative")
        ps.print_stats(80)
        report = stream.getvalue()

        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(report)

        logging.info("[cProfile-switching] Top 80 by cumtime:\n%s", report)
        logging.info("[cProfile-switching] Binary: %s", prof_path)
        logging.info("[cProfile-switching] Text  : %s", txt_path)
        logging.info(
            "[cProfile-switching] Visualise: pip install snakeviz && snakeviz %s", prof_path
        )
        app.after(1500, app.destroy)

    app.after(1500, _start)
    app.mainloop()


# --- MAIN EXECUTION BLOCK ---

def main():
    log_path = setup_logging(debug=True)

    # Parsing parametrů z .bat
    test_workers = None
    test_batch = None
    
    if "--workers" in sys.argv:
        idx = sys.argv.index("--workers")
        test_workers = int(sys.argv[idx + 1])
    if "--batch" in sys.argv:
        idx = sys.argv.index("--batch")
        test_batch = int(sys.argv[idx + 1])

    profiles = {
        '--startup':        launch_only,
        '--grid':           profile_grid_render,
        '--switching':      profile_folder_switching,
        '--selection':      profile_selection_stress,
        '--timeline':       profile_timeline_performance,
        "--thumbpipeline":  profile_timeline_thumbnails,
        '--widefolders':    profile_wide_folders,
        '--cprofile-grid':       profile_cprofile_grid,
        '--cprofile-switching':  profile_cprofile_switching,
    }

    # Najít profil k odpálení
    profile_to_run = None
    for arg in sys.argv:
        if arg in profiles:
            profile_to_run = profiles[arg]
            break

    if profile_to_run:
        logging.info(f"--- Spouštím profil: {profile_to_run.__name__} ---")
        profile_to_run(log_path, workers=test_workers, batch=test_batch)
    else:
        logging.error(
            "Specify a profile flag (e.g. --grid, --switching, --selection). See --help in source."
        )

if __name__ == "__main__":
    main()