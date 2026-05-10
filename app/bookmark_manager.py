"""
Bookmark manager window for Vibe Video Player.

This module provides a small dedicated UI for browsing and jumping to
time-based bookmarks of the currently loaded video.
"""

from __future__ import annotations

import tkinter as tk
from typing import Dict, List

import customtkinter as ctk


class BookmarkManager:
    """Manage a bookmark list window and seek interactions for one video player."""

    def __init__(self, parent, video_player):
        """
        Initialize the bookmark manager.

        Args:
            parent: Parent tkinter widget used to create the toplevel window.
            video_player: Player-like object exposing ``set_time(seconds)``.
        """
        self.parent = parent
        self.video_player = video_player

        self.window = None
        self.bookmark_listbox = None
        self.is_open = False
        self.bookmarks: List[Dict[str, object]] = []

    def show_manager(self):
        """
        Show the bookmark manager window.

        If the window already exists, it is brought to the foreground.
        Otherwise, a new window is created and populated.
        """
        if self.is_open and self.window and self.window.winfo_exists():
            self.window.attributes("-topmost", True)
            self.window.focus_force()
            return

        self.window = ctk.CTkToplevel(self.parent)
        self.window.title("Bookmarks")
        self.window.geometry("300x400")
        self.window.protocol("WM_DELETE_WINDOW", self.on_close)
        self.window.attributes("-topmost", True)
        self.is_open = True

        main_frame = ctk.CTkFrame(self.window, fg_color="transparent")
        main_frame.pack(fill=tk.BOTH, expand=True)

        self.bookmark_listbox = tk.Listbox(
            main_frame,
            bg="#2B2B2B",
            fg="white",
            selectbackground="#1F6AA5",
            highlightthickness=0,
            borderwidth=0,
            activestyle="none",
        )
        self.bookmark_listbox.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # Support both single-click and double-click bookmark jumps.
        self.bookmark_listbox.bind("<<ListboxSelect>>", self._on_bookmark_select)
        self.bookmark_listbox.bind("<Double-1>", self._on_bookmark_select)

        self._populate_bookmark_list()

    def load_bookmarks_for_video(self, bookmarks_data):
        """
        Load bookmark entries for the active video.

        Args:
            bookmarks_data: List of dictionaries in the form:
                ``{"time": float, "label": str}``
        """
        self.bookmarks = []
        for item in bookmarks_data or []:
            if not isinstance(item, dict):
                continue

            bookmark_time = item.get("time")
            label = item.get("label", "")
            if bookmark_time is None:
                continue

            try:
                normalized_time = float(bookmark_time)
            except (TypeError, ValueError):
                continue

            self.bookmarks.append(
                {
                    "time": max(0.0, normalized_time),
                    "label": str(label) if label is not None else "",
                }
            )

        self.bookmarks.sort(key=lambda b: b["time"])
        self._populate_bookmark_list()

    def _format_time(self, seconds):
        """
        Convert seconds into ``MM:SS`` display format.

        Args:
            seconds: Timestamp in seconds.

        Returns:
            Formatted ``MM:SS`` string.
        """
        total_seconds = max(0, int(float(seconds)))
        minutes = total_seconds // 60
        secs = total_seconds % 60
        return f"{minutes:02d}:{secs:02d}"

    def _populate_bookmark_list(self):
        """Refresh the listbox display from the currently loaded bookmarks."""
        if not (self.is_open and self.bookmark_listbox):
            return

        self.bookmark_listbox.delete(0, tk.END)
        for bookmark in self.bookmarks:
            formatted_time = self._format_time(bookmark["time"])
            label = str(bookmark.get("label", "")).strip()
            display_text = f"[{formatted_time}] {label}" if label else f"[{formatted_time}]"
            self.bookmark_listbox.insert(tk.END, display_text)

    def _on_bookmark_select(self, event=None):
        """
        Seek to the selected bookmark's timestamp.

        The selected entry is resolved to the source bookmark index and then
        sent to the connected video player.
        """
        if not self.bookmark_listbox:
            return

        selection = self.bookmark_listbox.curselection()
        if not selection:
            return

        index = selection[0]
        if not (0 <= index < len(self.bookmarks)):
            return

        target_time = self.bookmarks[index].get("time")
        if target_time is None:
            return

        self.video_player.set_time(float(target_time))

    def on_close(self):
        """
        Close and release the bookmark manager window.

        The manager keeps its bookmark data in memory and can be reopened
        later through ``show_manager()``.
        """
        self.is_open = False
        if self.window:
            self.window.destroy()
            self.window = None
        self.bookmark_listbox = None
