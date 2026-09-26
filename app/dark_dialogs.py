"""
Dark-themed drop-in for ``tkinter.messagebox``.

Call ``install_dark_messagebox()`` once after CustomTkinter dark mode is set.
Existing ``messagebox.showinfo`` / ``askyesno`` call sites then use CTk dialogs.
"""

from __future__ import annotations

import logging
import sys
import tkinter as tk
import tkinter.messagebox as _tk_messagebox

import customtkinter as ctk

_installed = False
_ORIG: dict = {}


def _apply_windows_immersive_dark_titlebar(widget) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        widget.update_idletasks()
        wid = int(widget.winfo_id())
        hwnd = ctypes.windll.user32.GetAncestor(wid, 2) or wid
        use_dark = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(use_dark), ctypes.sizeof(use_dark)
        )
    except Exception:
        logging.debug("dark_dialogs: immersive title bar failed", exc_info=True)


def _resolve_parent(parent):
    if parent is not None:
        try:
            if parent.winfo_exists():
                return parent
        except tk.TclError:
            pass
    return getattr(tk, "_default_root", None)


def _message_dialog(
    parent,
    title: str,
    message: str,
    buttons: list[tuple[str, object]],
    *,
    default=None,
):
    """
    Modal CTk dialog. ``buttons`` is a list of ``(label, return_value)``.
    Closing via X / Escape returns ``default``.
    """
    host = _resolve_parent(parent)
    win = ctk.CTkToplevel(host) if host is not None else ctk.CTkToplevel()
    win.title(title or "")
    win.resizable(False, False)

    line_count = max(1, (message or "").count("\n") + 1)
    # Compact defaults (~15% narrower / ~40% shorter than the first dark_dialogs sizes).
    width = 290 if line_count <= 2 else 357
    height = min(168, 90 + max(0, line_count - 1) * 22)
    try:
        win.geometry(f"{width}x{height}")
    except Exception:
        pass

    if host is not None:
        try:
            win.transient(host.winfo_toplevel())
        except Exception:
            pass
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass
    try:
        win.after_idle(_apply_windows_immersive_dark_titlebar, win)
    except Exception:
        pass

    result = [default]

    def _finish(value):
        result[0] = value
        try:
            win.grab_release()
        except tk.TclError:
            pass
        if win.winfo_exists():
            win.destroy()

    btn_row = ctk.CTkFrame(win, fg_color="transparent")
    btn_row.pack(side="bottom", padx=15, pady=(6, 12))
    btn_inner = ctk.CTkFrame(btn_row, fg_color="transparent")
    btn_inner.pack(anchor="center")

    content = ctk.CTkFrame(win, fg_color="transparent")
    content.pack(side="top", fill="both", expand=True, padx=14, pady=(10, 4))

    wrap = max(220, width - 40)
    ctk.CTkLabel(
        content,
        text=message or "",
        wraplength=wrap,
        anchor="center",
        justify="center",
    ).pack(expand=True)

    btn_widgets: list[ctk.CTkButton] = []
    for label, value in buttons:
        btn = ctk.CTkButton(
            btn_inner,
            text=label,
            width=100,
            height=30,
            command=lambda v=value: _finish(v),
        )
        btn.pack(side="left", padx=6)
        btn_widgets.append(btn)

    def _on_close():
        _finish(default)

    win.protocol("WM_DELETE_WINDOW", _on_close)
    win.bind("<Escape>", lambda _e: _on_close())
    if btn_widgets:
        win.bind("<Return>", lambda _e: btn_widgets[0].invoke())
        win.bind("<KP_Enter>", lambda _e: btn_widgets[0].invoke())

    try:
        win.update_idletasks()
        req_h = int(win.winfo_reqheight())
        req_w = int(win.winfo_reqwidth())
        w = max(width, min(max(req_w, width), 400))
        h = max(height, min(req_h + 4, 180))
        if host is not None:
            try:
                px = host.winfo_rootx() + max(0, (host.winfo_width() - w) // 2)
                py = host.winfo_rooty() + max(0, (host.winfo_height() - h) // 2)
                win.geometry(f"{w}x{h}+{px}+{py}")
            except Exception:
                win.geometry(f"{w}x{h}")
        else:
            win.geometry(f"{w}x{h}")
    except Exception:
        pass

    try:
        win.grab_set()
    except tk.TclError:
        pass
    win.lift()
    try:
        win.focus_force()
    except tk.TclError:
        pass
    win.wait_window()
    return result[0]


def showinfo(title=None, message=None, **options):
    _message_dialog(
        options.get("parent"),
        title or "Info",
        message or "",
        [("OK", "ok")],
        default="ok",
    )
    return "ok"


def showwarning(title=None, message=None, **options):
    _message_dialog(
        options.get("parent"),
        title or "Warning",
        message or "",
        [("OK", "ok")],
        default="ok",
    )
    return "ok"


def showerror(title=None, message=None, **options):
    _message_dialog(
        options.get("parent"),
        title or "Error",
        message or "",
        [("OK", "ok")],
        default="ok",
    )
    return "ok"


def askyesno(title=None, message=None, **options):
    return bool(
        _message_dialog(
            options.get("parent"),
            title or "",
            message or "",
            [("Yes", True), ("No", False)],
            default=False,
        )
    )


def askokcancel(title=None, message=None, **options):
    return bool(
        _message_dialog(
            options.get("parent"),
            title or "",
            message or "",
            [("OK", True), ("Cancel", False)],
            default=False,
        )
    )


def askretrycancel(title=None, message=None, **options):
    return bool(
        _message_dialog(
            options.get("parent"),
            title or "",
            message or "",
            [("Retry", True), ("Cancel", False)],
            default=False,
        )
    )


def askquestion(title=None, message=None, **options):
    yes = _message_dialog(
        options.get("parent"),
        title or "",
        message or "",
        [("Yes", True), ("No", False)],
        default=False,
    )
    return "yes" if yes else "no"


def askyesnocancel(title=None, message=None, **options):
    return _message_dialog(
        options.get("parent"),
        title or "",
        message or "",
        [("Yes", True), ("No", False), ("Cancel", None)],
        default=None,
    )


def install_dark_messagebox() -> None:
    """Patch ``tkinter.messagebox`` so existing call sites use dark CTk dialogs."""
    global _installed
    if _installed:
        return
    for name, fn in (
        ("showinfo", showinfo),
        ("showwarning", showwarning),
        ("showerror", showerror),
        ("askyesno", askyesno),
        ("askokcancel", askokcancel),
        ("askretrycancel", askretrycancel),
        ("askquestion", askquestion),
        ("askyesnocancel", askyesnocancel),
    ):
        if name not in _ORIG:
            _ORIG[name] = getattr(_tk_messagebox, name, None)
        setattr(_tk_messagebox, name, fn)
    _installed = True
    logging.info("[UI] Dark messagebox installed")
