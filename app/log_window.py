"""
Log file viewer for Vibe Player.

Opens a ``tk.Toplevel`` (debug console) that tails ``app.log`` (or the configured path)
with periodic polling and appends new bytes to a read-only text widget.
"""

from __future__ import annotations

import os
import tkinter as tk


_LOG_BG = "#000000"
_LOG_FG = "#ffffff"


class LogWindow(tk.Toplevel):
    """Display the tail of the application log file with auto-scroll."""

    def __init__(self, parent: tk.Widget, log_path: str) -> None:
        super().__init__(parent)
        self.title("Debug Console")
        self.geometry("900x500")
        self.configure(bg=_LOG_BG)

        self.text = tk.Text(
            self,
            wrap="none",
            font=("Consolas", 10),
            bg=_LOG_BG,
            fg=_LOG_FG,
            insertbackground=_LOG_FG,
            selectbackground="#333355",
            selectforeground=_LOG_FG,
            highlightthickness=0,
            borderwidth=0,
        )
        self.text.pack(fill="both", expand=True)

        self.log_path = log_path
        self._last_size = 0

        self.after(250, self._poll)

    def _poll(self) -> None:
        """Append any new log content and reschedule polling."""
        try:
            size = os.path.getsize(self.log_path)
            if size > self._last_size:
                with open(self.log_path, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(self._last_size)
                    chunk = f.read()
                if chunk:
                    self.text.insert("end", chunk)
                    self.text.see("end")
                self._last_size = size
        except FileNotFoundError:
            pass

        self.after(500, self._poll)
