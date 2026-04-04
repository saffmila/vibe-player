"""
Windows CF_HDROP clipboard helpers (file/folder paths), compatible with Explorer.

Falls back to an in-memory path list when the OS API is unavailable.
"""

from __future__ import annotations

import logging
import os
import struct
from typing import List, Optional

# In-process fallback when CF_HDROP cannot be read/written (non-Windows or errors).
_internal_paths: List[str] = []

# DROPEFFECT_* (used with "Preferred DropEffect" clipboard format)
DROPEFFECT_COPY = 1
DROPEFFECT_MOVE = 2


def _set_internal_paths(paths: List[str]) -> None:
    global _internal_paths
    _internal_paths = [os.path.normpath(p) for p in paths if p]


def _build_hdrop_bytes(paths: List[str]) -> bytes:
    abs_paths = [os.path.normpath(os.path.abspath(p)) for p in paths if p]
    if not abs_paths:
        raise ValueError("no paths")
    # UTF-16LE, double-null terminated list, final extra null
    wide = "\0".join(abs_paths) + "\0\0"
    body = wide.encode("utf-16le")
    # DROPFILES: pFiles=20, pt=(0,0), fNC=0, fWide=1
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    return header + body


def set_clipboard_file_paths(paths: List[str], *, cut: bool = False) -> bool:
    """
    Put file/folder paths on the clipboard. On Windows uses CF_HDROP (+ optional cut effect).
    Always mirrors paths to the in-process fallback for same-app paste reliability.
    """
    clean = [os.path.normpath(p) for p in paths if p and os.path.exists(p)]
    if not clean:
        logging.info("[clipboard] set_clipboard_file_paths: nothing to copy")
        return False
    _set_internal_paths(clean)

    if os.name != "nt":
        logging.info("[clipboard] non-Windows: using in-process path list only")
        return True

    try:
        import win32clipboard
        import win32con
        import win32api

        data = _build_hdrop_bytes(clean)
        gmem_moveable = 0x0002
        h_global = win32api.GlobalAlloc(gmem_moveable, len(data))
        ptr = win32api.GlobalLock(h_global)
        try:
            win32api.MoveMemory(ptr, data, len(data))
        finally:
            win32api.GlobalUnlock(h_global)

        win32clipboard.OpenClipboard(0)
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_HDROP, h_global)
            if cut:
                fmt = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
                eff = struct.pack("<I", DROPEFFECT_MOVE)
                h_eff = win32api.GlobalAlloc(gmem_moveable, len(eff))
                p_eff = win32api.GlobalLock(h_eff)
                try:
                    win32api.MoveMemory(p_eff, eff, len(eff))
                finally:
                    win32api.GlobalUnlock(h_eff)
                win32clipboard.SetClipboardData(fmt, h_eff)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception as e:
        logging.warning("[clipboard] CF_HDROP set failed: %s (in-process list still set)", e)
        return True


def get_clipboard_file_paths() -> List[str]:
    """Return absolute paths from CF_HDROP, or the in-process fallback."""
    if os.name == "nt":
        try:
            import win32clipboard
            import win32con

            win32clipboard.OpenClipboard(0)
            try:
                raw = win32clipboard.GetClipboardData(win32con.CF_HDROP)
            finally:
                win32clipboard.CloseClipboard()

            if raw is None:
                out = list(_internal_paths)
            elif isinstance(raw, (list, tuple)):
                out = [os.path.normpath(p) for p in raw if p]
            elif isinstance(raw, str):
                out = [os.path.normpath(raw)] if raw else []
            else:
                logging.info("[clipboard] unexpected CF_HDROP type: %s", type(raw))
                out = list(_internal_paths)
        except Exception as e:
            logging.info("[clipboard] CF_HDROP read failed: %s — using fallback", e)
            out = list(_internal_paths)
    else:
        out = list(_internal_paths)

    seen = set()
    unique = []
    for p in out:
        k = os.path.normcase(os.path.normpath(p))
        if k in seen:
            continue
        seen.add(k)
        unique.append(os.path.normpath(p))
    return [p for p in unique if os.path.exists(p)]


def clipboard_has_pastable_paths() -> bool:
    return bool(get_clipboard_file_paths())
