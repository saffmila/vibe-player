"""
Bookmark manager window for Vibe Video Player.

This module provides a small dedicated UI for browsing and jumping to
time-based bookmarks of the currently loaded video.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog
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
        self.button_panel = None
        self.filter_var = tk.StringVar()
        self.filter_entry = None
        self.visible_indices: List[int] = []
        self.active_bookmark_index = None
        self._poll_after_id = None
        self.is_open = False
        self.bookmarks: List[Dict[str, object]] = []
        self._polling_suspended = False

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

        self.filter_entry = ctk.CTkEntry(
            main_frame,
            textvariable=self.filter_var,
            placeholder_text="Filter bookmarks...",
            height=30,
        )
        self.filter_entry.pack(fill=tk.X, padx=6, pady=(6, 2))
        self.filter_entry.bind("<KeyRelease>", self._on_filter_changed)

        self.bookmark_listbox = tk.Listbox(
            main_frame,
            bg="#2B2B2B",
            fg="white",
            selectbackground="#1F6AA5",
            selectforeground="white",
            highlightthickness=0,
            borderwidth=0,
            activestyle="none",
            exportselection=False,
            font=("Segoe UI", 12),
        )
        self.bookmark_listbox.pack(fill=tk.BOTH, expand=True, padx=6, pady=(2, 6))

        # Support both single-click and double-click bookmark jumps.
        self.bookmark_listbox.bind("<<ListboxSelect>>", self._on_bookmark_select)
        self.bookmark_listbox.bind("<Double-1>", self._on_bookmark_select)

        self._create_button_panel(main_frame)
        self._populate_bookmark_list()
        self._start_playback_polling()

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

        self._sort_bookmarks()
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

        filter_text = self.filter_var.get().strip().lower()
        self.visible_indices = []
        self.bookmark_listbox.delete(0, tk.END)
        for idx, bookmark in enumerate(self.bookmarks):
            label = str(bookmark.get("label", "")).strip()
            if filter_text and filter_text not in label.lower():
                continue
            self.visible_indices.append(idx)
            formatted_time = self._format_time(bookmark["time"])
            display_text = f"[{formatted_time}] {label}" if label else f"[{formatted_time}]"
            self.bookmark_listbox.insert(tk.END, display_text)

        self._apply_active_highlight()

    def _create_button_panel(self, parent_frame):
        """
        Create the bottom control panel in a compact playlist-like style.
        """
        self.button_panel = ctk.CTkFrame(parent_frame, fg_color="transparent")
        self.button_panel.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=(0, 6))
        self.button_panel.grid_columnconfigure(0, weight=1)
        self.button_panel.grid_columnconfigure((1, 2, 3, 4), weight=0)

        btn_style = {
            "font": ("Segoe UI", 11),
            "fg_color": "#333333",
            "hover_color": "#444444",
            "text_color": "#dddddd",
            "corner_radius": 4,
            "height": 26,
            "width": 58,
        }

        ctk.CTkButton(
            self.button_panel,
            text="✚ Add",
            command=self.add_bookmark,
            **btn_style,
        ).grid(row=0, column=0, padx=2, pady=2, sticky="w")

        ctk.CTkButton(
            self.button_panel,
            text="◀ Prev",
            command=self.skip_to_previous,
            **btn_style,
        ).grid(row=0, column=1, padx=2, pady=2, sticky="e")

        ctk.CTkButton(
            self.button_panel,
            text="Next ▶",
            command=self.skip_to_next,
            **btn_style,
        ).grid(row=0, column=2, padx=2, pady=2, sticky="e")

        ctk.CTkButton(
            self.button_panel,
            text="✖ Del",
            command=self.delete_selected_bookmark,
            **btn_style,
        ).grid(row=0, column=3, padx=2, pady=2, sticky="e")

        ctk.CTkButton(
            self.button_panel,
            text="🗑 Clear",
            command=self.clear_all_bookmarks,
            **btn_style,
        ).grid(row=0, column=4, padx=2, pady=2, sticky="e")

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

        list_index = selection[0]
        if not (0 <= list_index < len(self.visible_indices)):
            return

        bookmark_index = self.visible_indices[list_index]
        target_time = self.bookmarks[bookmark_index].get("time")
        if target_time is None:
            return

        self.video_player.set_time(float(target_time))

    def _on_filter_changed(self, event=None):
        """Refresh visible rows when the filter text changes."""
        self._populate_bookmark_list()

    def set_playback_polling_suspended(self, suspended: bool) -> None:
        """Pause VLC polling while the main player is being torn down or recreated."""
        self._polling_suspended = bool(suspended)

    def _start_playback_polling(self):
        """Start periodic playback polling while the manager window is open."""
        if not (self.window and self.window.winfo_exists()):
            return
        self._poll_playback_time()

    def _poll_playback_time(self):
        """
        Track playback position and update the active bookmark highlight.

        The active bookmark is the nearest bookmark whose timestamp is
        less than or equal to the current playback time.
        """
        if not (self.is_open and self.window and self.window.winfo_exists()):
            self._poll_after_id = None
            return

        if self._polling_suspended:
            self._poll_after_id = self.window.after(500, self._poll_playback_time)
            return

        ctrl = getattr(self.video_player, "controller", None)
        if ctrl and (
            getattr(ctrl, "_video_player_switching", False)
            or getattr(ctrl, "_open_video_job", None)
        ):
            self._poll_after_id = self.window.after(500, self._poll_playback_time)
            return

        current_time = self._get_current_playback_time_seconds()
        self._update_active_bookmark(current_time)
        self._poll_after_id = self.window.after(500, self._poll_playback_time)

    def _get_current_playback_time_seconds(self) -> float:
        """
        Return playback time in seconds.

        Handles both second-based and millisecond-based sources defensively.
        """
        if not hasattr(self.video_player, "get_current_time"):
            return 0.0
        try:
            value = float(self.video_player.get_current_time())
        except Exception:
            return 0.0

        # Defensive normalization when a backend unexpectedly returns ms.
        if value > 1_000_000:
            return value / 1000.0
        return max(0.0, value)

    def _update_active_bookmark(self, current_time: float):
        """Update active bookmark index and refresh row highlighting if changed."""
        active_idx = None
        for idx, item in enumerate(self.bookmarks):
            try:
                timestamp = float(item.get("time", 0.0))
            except (TypeError, ValueError):
                continue
            if timestamp <= current_time:
                active_idx = idx
            else:
                break

        if active_idx != self.active_bookmark_index:
            self.active_bookmark_index = active_idx
            self._apply_active_highlight()

    def _apply_active_highlight(self):
        """
        Paint non-selected rows to indicate active playback section.

        This custom background highlight is independent from list selection.
        """
        if not self.bookmark_listbox:
            return

        default_bg = "#2B2B2B"
        active_bg = "#404040"
        selected_set = set(self.bookmark_listbox.curselection())

        for list_idx, bookmark_idx in enumerate(self.visible_indices):
            if list_idx in selected_set:
                # Never override selected row colors; keep user's blue selection visible.
                continue
            row_bg = active_bg if bookmark_idx == self.active_bookmark_index else default_bg
            self.bookmark_listbox.itemconfig(list_idx, bg=row_bg, fg="white")

    def add_bookmark(self):
        """
        Add a bookmark at the current playback time with an optional label.
        """
        current_time = 0.0
        if hasattr(self.video_player, "get_current_time"):
            try:
                current_time = float(self.video_player.get_current_time())
            except Exception:
                current_time = 0.0

        default_label = f"Bookmark {len(self.bookmarks) + 1}"
        user_label = simpledialog.askstring(
            "Add Bookmark",
            f"Label for bookmark at {self._format_time(current_time)}:",
            parent=self.window,
            initialvalue=default_label,
        )

        if user_label is None:
            return

        # Warn when a bookmark already exists at (almost) the same timestamp.
        duplicate_exists = any(
            abs(float(item.get("time", -9999.0)) - current_time) < 1e-3
            for item in self.bookmarks
        )
        if duplicate_exists:
            should_continue = messagebox.askyesno(
                "Bookmark Exists",
                f"A bookmark already exists at {self._format_time(current_time)}.\n"
                "Do you want to add another one?",
                parent=self.window,
            )
            if not should_continue:
                return

        self.bookmarks.append(
            {
                "time": max(0.0, float(current_time)),
                "label": user_label.strip() or default_label,
            }
        )
        self._sort_bookmarks()
        self._populate_bookmark_list()
        self._sync_bookmarks_to_player()

    def delete_selected_bookmark(self):
        """
        Delete the currently selected bookmark from the list.
        """
        if not self.bookmark_listbox:
            return

        selection = self.bookmark_listbox.curselection()
        if not selection:
            return

        list_index = selection[0]
        if not (0 <= list_index < len(self.visible_indices)):
            return

        bookmark_index = self.visible_indices[list_index]
        if not (0 <= bookmark_index < len(self.bookmarks)):
            return

        del self.bookmarks[bookmark_index]
        self._sort_bookmarks()
        self._populate_bookmark_list()
        self._sync_bookmarks_to_player()

    def clear_all_bookmarks(self):
        """Delete all bookmarks for the current video after confirmation."""
        if not self.bookmarks:
            return
        if not messagebox.askyesno("Clear All Bookmarks", "Delete all bookmarks for this video?", parent=self.window):
            return
        self.bookmarks = []
        self._populate_bookmark_list()
        self._sync_bookmarks_to_player()

    def skip_to_next(self):
        """
        Jump to the nearest bookmark after the current playback position.
        """
        if not self.bookmarks:
            return

        current_time = 0.0
        if hasattr(self.video_player, "get_current_time"):
            try:
                current_time = float(self.video_player.get_current_time())
            except Exception:
                current_time = 0.0

        next_candidates = [b for b in self.bookmarks if float(b.get("time", 0.0)) > current_time + 1e-6]
        if not next_candidates:
            return

        target = min(next_candidates, key=lambda b: float(b["time"]))
        self.video_player.set_time(float(target["time"]))
        self._select_bookmark_by_time(float(target["time"]))

    def skip_to_previous(self):
        """
        Jump to the nearest bookmark before the current playback position.
        """
        if not self.bookmarks:
            return

        current_time = 0.0
        if hasattr(self.video_player, "get_current_time"):
            try:
                current_time = float(self.video_player.get_current_time())
            except Exception:
                current_time = 0.0

        prev_candidates = [b for b in self.bookmarks if float(b.get("time", 0.0)) < current_time - 1e-6]
        if not prev_candidates:
            return

        target = max(prev_candidates, key=lambda b: float(b["time"]))
        self.video_player.set_time(float(target["time"]))
        self._select_bookmark_by_time(float(target["time"]))

    def _sort_bookmarks(self):
        """Sort bookmarks chronologically by time."""
        self.bookmarks.sort(key=lambda b: float(b.get("time", 0.0)))

    def _select_bookmark_by_time(self, target_time: float):
        """Select and reveal bookmark row matching a target timestamp."""
        if not self.bookmark_listbox:
            return

        for list_idx, bookmark_idx in enumerate(self.visible_indices):
            item = self.bookmarks[bookmark_idx]
            if abs(float(item.get("time", -1.0)) - target_time) < 1e-6:
                self.bookmark_listbox.selection_clear(0, tk.END)
                self.bookmark_listbox.selection_set(list_idx)
                self.bookmark_listbox.activate(list_idx)
                self.bookmark_listbox.see(list_idx)
                return

    def _sync_bookmarks_to_player(self):
        """
        Push the current bookmark list to the underlying player, if supported.
        """
        if hasattr(self.video_player, "set_bookmarks"):
            self.video_player.set_bookmarks(self.bookmarks)

    def on_close(self):
        """
        Close and release the bookmark manager window.

        The manager keeps its bookmark data in memory and can be reopened
        later through ``show_manager()``.
        """
        self.is_open = False
        if self.window and self._poll_after_id is not None:
            try:
                self.window.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        if self.window:
            self.window.destroy()
            self.window = None
        self.bookmark_listbox = None
        self.filter_entry = None
        self.active_bookmark_index = None
