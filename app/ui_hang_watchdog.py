"""
Detect UI-thread freezes and dump all Python stacks to ``app.fault.log``.

How it works
------------
- The Tk main thread schedules a heartbeat every ``BEAT_MS``.
- A daemon watcher checks that heartbeat. If it goes stale longer than
  ``THRESHOLD_S`` (and LMB is not held — OLE drag parks the UI intentionally),
  we log ``[UI-HANG]`` and call ``faulthandler.dump_traceback``.

Also exposes ``fault_log_file()`` so ``dump_traceback_later`` can use a real
file with a valid ``fileno()`` (``StreamToLogger`` returns -1 and breaks it).
"""

from __future__ import annotations

import faulthandler
import logging
import os
import threading
import time
from typing import Any, Optional

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
FAULT_LOG_PATH = os.path.join(_APP_DIR, "app.fault.log")

BEAT_MS = 500
THRESHOLD_S = 8.0
DUMP_COOLDOWN_S = 30.0

_fault_file = None
_fault_file_lock = threading.Lock()
_heartbeat = time.monotonic()
_hb_lock = threading.Lock()
_stop = threading.Event()
_watcher: Optional[threading.Thread] = None
_last_dump_mono = 0.0
_started = False


def fault_log_file():
    """Process-lifetime append handle for faulthandler (valid fileno on Windows)."""
    global _fault_file
    with _fault_file_lock:
        if _fault_file is None or getattr(_fault_file, "closed", True):
            _fault_file = open(FAULT_LOG_PATH, "a", encoding="utf-8", buffering=1)
        return _fault_file


def note_ui_alive() -> None:
    global _heartbeat
    with _hb_lock:
        _heartbeat = time.monotonic()


def _lmb_held() -> bool:
    """OLE drag / press keeps LMB down and parks Tk — not a real hang."""
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetKeyState(0x01) & 0x8000)
    except Exception:
        return False


def dump_all_threads(reason: str = "manual") -> None:
    """Write a full thread dump to app.fault.log and a short marker to app.log."""
    global _last_dump_mono
    now = time.monotonic()
    if now - _last_dump_mono < DUMP_COOLDOWN_S and reason == "hang":
        return
    _last_dump_mono = now
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    fh = fault_log_file()
    try:
        fh.write(f"\n===== UI DUMP ({reason}) {stamp} =====\n")
        fh.flush()
        faulthandler.dump_traceback(file=fh, all_threads=True)
        fh.write(f"===== END UI DUMP ({reason}) =====\n")
        fh.flush()
    except Exception:
        logging.exception("[UI-HANG] faulthandler dump failed")
    logging.error(
        "[UI-HANG] dumped all threads (%s) → %s",
        reason,
        FAULT_LOG_PATH,
    )
    # Force rotating handler to disk so the marker survives a hard kill.
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass


def _watch_loop() -> None:
    while not _stop.wait(1.0):
        with _hb_lock:
            age = time.monotonic() - _heartbeat
        if age < THRESHOLD_S:
            continue
        if _lmb_held():
            # Drag-drop / press: UI is blocked on purpose.
            note_ui_alive()
            continue
        logging.error(
            "[UI-HANG] no UI heartbeat for %.1fs (threshold=%.0fs)",
            age,
            THRESHOLD_S,
        )
        dump_all_threads("hang")
        # Avoid continuous dumps while still frozen.
        note_ui_alive()


def _schedule_beat(widget: Any) -> None:
    if _stop.is_set():
        return
    note_ui_alive()
    try:
        widget.after(BEAT_MS, lambda w=widget: _schedule_beat(w))
    except Exception:
        pass


def start(widget: Any) -> None:
    """Start heartbeat on ``widget`` (Tk root) and the background watcher."""
    global _watcher, _started
    if _started:
        note_ui_alive()
        return
    _started = True
    _stop.clear()
    note_ui_alive()
    # Ensure faulthandler has a dump target with a real fileno.
    try:
        faulthandler.enable(file=fault_log_file(), all_threads=True)
    except Exception:
        logging.debug("[UI-HANG] faulthandler.enable failed", exc_info=True)
    _watcher = threading.Thread(
        target=_watch_loop, name="UIHangWatchdog", daemon=True
    )
    _watcher.start()
    _schedule_beat(widget)
    try:
        from tk_font_gc_fix import install as _install_tk_font_gc

        _install_tk_font_gc(widget)
    except Exception:
        logging.debug("[UI-HANG] tk_font_gc_fix install failed", exc_info=True)
    logging.info(
        "[UI-HANG] watchdog on (beat=%dms, hang≥%.0fs) → %s",
        BEAT_MS,
        THRESHOLD_S,
        FAULT_LOG_PATH,
    )


def stop() -> None:
    global _started
    _stop.set()
    _started = False


def arm_dump_later(timeout_s: float = 8.0) -> bool:
    """Arm faulthandler.dump_traceback_later on the fault log file. Returns ok."""
    try:
        faulthandler.dump_traceback_later(
            timeout_s, repeat=False, file=fault_log_file()
        )
        return True
    except Exception as exc:
        logging.debug("[UI-HANG] dump_traceback_later failed: %s", exc)
        return False


def cancel_dump_later() -> None:
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
