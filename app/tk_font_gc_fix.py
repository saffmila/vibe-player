"""
Prevent permanent UI freezes from ``tkinter.font.Font.__del__`` on worker threads.

Python's Font destructor calls into Tcl (``font delete``). When GC runs that
during ``Thread.start()`` bootstrap on a worker, the worker blocks waiting for
the Tk main thread while the main thread blocks on ``Thread.start``'s
``_started`` event — classic deadlock (see ``[UI-HANG]`` dumps with stacks in
``font.py`` ``__del__`` + ``threading.Thread.start``).

Fix: never call Tk from Font.__del__ off the main thread; queue deletes for a
periodic drain on the UI thread. Prefer leaking a named font over freezing.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter.font as tkfont
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

_pending: queue.Queue[tuple[Any, str]] = queue.Queue()
_installed = False
_pump_root = None
_spawn_pool: Optional[ThreadPoolExecutor] = None
_ORIG_DEL = tkfont.Font.__del__


def _safe_font_del(self) -> None:
    if not getattr(self, "delete_font", False):
        return
    name = getattr(self, "name", None)
    tk = getattr(self, "_tk", None)
    # Clear flag first so a second GC pass cannot re-enter Tk.
    self.delete_font = False
    if not name or tk is None:
        return
    if threading.current_thread() is threading.main_thread():
        try:
            tk.call("font", "delete", name)
        except Exception:
            pass
        return
    try:
        _pending.put_nowait((tk, name))
    except Exception:
        pass


def drain_pending_font_deletes() -> None:
    """Delete fonts queued by off-thread ``Font.__del__`` (UI thread only)."""
    while True:
        try:
            tk, name = _pending.get_nowait()
        except queue.Empty:
            break
        try:
            tk.call("font", "delete", name)
        except Exception:
            pass


def _pump() -> None:
    drain_pending_font_deletes()
    root = _pump_root
    if root is None:
        return
    try:
        root.after(500, _pump)
    except Exception:
        pass


def spawn(fn: Callable[..., Any], *args, **kwargs) -> None:
    """Run ``fn`` on a pre-warmed pool so the UI thread never calls ``Thread.start``."""
    global _spawn_pool
    if _spawn_pool is None:
        # First use on UI before install(): still creates threads — prefer install().
        _spawn_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="UiSpawn")
    _spawn_pool.submit(fn, *args, **kwargs)


def install(root=None) -> None:
    """Patch Font.__del__ and optionally start the UI drain pump + spawn pool."""
    global _installed, _pump_root, _spawn_pool
    if not _installed:
        tkfont.Font.__del__ = _safe_font_del  # type: ignore[method-assign]
        _installed = True
        logging.info("[TkFontGC] Font.__del__ patched (off-thread deletes queued)")
    if _spawn_pool is None:
        # Warm workers now (startup), not on first video click.
        _spawn_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="UiSpawn")
        logging.info("[TkFontGC] UiSpawn pool ready")
    if root is not None:
        _pump_root = root
        try:
            root.after(500, _pump)
        except Exception:
            pass
