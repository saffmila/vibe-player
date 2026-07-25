"""
Leave-edit confirmation for image viewer (crop / resize).

Dark CTk modal matching ``VideoThumbnailPlayer.universal_dialog`` styling,
parented to the image viewer so it stays above fullscreen / topmost windows.

Button labels are action verbs (users often read buttons before body text):
  - Keep editing   → stay in the current edit (safe)
  - Discard & skip → abandon edit and continue next/prev
"""

from __future__ import annotations

import logging

import customtkinter as ctk


def confirm_leave_image_edit(parent, processes: list[str]) -> bool:
    """
    Ask whether to abandon an active edit and navigate away.

    Returns True if there is nothing to guard, or the user chooses Discard & skip.
    """
    labels = [p for p in (processes or []) if p]
    if not labels:
        return True

    if len(labels) == 1:
        title = f"{labels[0]} in progress"
        process_bit = labels[0].lower()
    else:
        title = "Edit in progress"
        process_bit = " / ".join(labels).lower()

    message = (
        f"Switch to another image and discard this {process_bit}?\n"
        "Unsaved changes will be lost."
    )

    return _show_leave_dialog(parent, title, message)


def _show_leave_dialog(parent, title: str, message: str) -> bool:
    result = {"value": False}

    if parent is None:
        return False

    try:
        dialog = ctk.CTkToplevel(parent)
    except Exception as e:
        logging.info("leave-edit dialog could not open: %s", e)
        return False

    dialog.title(title)
    dialog.resizable(False, False)
    try:
        dialog.transient(parent.winfo_toplevel())
    except Exception:
        pass
    try:
        dialog.attributes("-topmost", True)
    except Exception:
        pass

    ctk.CTkLabel(
        dialog,
        text=message,
        wraplength=360,
        anchor="w",
        justify="left",
    ).pack(padx=14, pady=14, fill="x")

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=10, pady=(0, 12))

    def _discard():
        result["value"] = True
        _close()

    def _keep():
        result["value"] = False
        _close()

    def _close():
        try:
            dialog.grab_release()
        except Exception:
            pass
        try:
            if dialog.winfo_exists():
                dialog.destroy()
        except Exception:
            pass

    # Same packing order as universal_dialog: confirm left, cancel right.
    ctk.CTkButton(
        btn_row, text="Discard & skip", width=130, command=_discard
    ).pack(side="left", padx=6)
    ctk.CTkButton(
        btn_row, text="Keep editing", width=120, command=_keep
    ).pack(side="right", padx=6)

    dialog.protocol("WM_DELETE_WINDOW", _keep)
    dialog.bind("<Escape>", lambda e: _keep())
    dialog.bind("<Return>", lambda e: _discard())

    try:
        dialog.update_idletasks()
        w, h = 420, 170
        px = parent.winfo_rootx() + max(0, (parent.winfo_width() - w) // 2)
        py = parent.winfo_rooty() + max(0, (parent.winfo_height() - h) // 2)
        dialog.geometry(f"{w}x{h}+{px}+{py}")
    except Exception:
        dialog.geometry("420x170")

    try:
        dialog.lift()
        dialog.focus_force()
        dialog.grab_set()
        dialog.wait_window()
    except Exception as e:
        logging.info("leave-edit dialog failed: %s", e)
        try:
            dialog.destroy()
        except Exception:
            pass
        return False

    return bool(result["value"])
