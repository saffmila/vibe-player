"""
Bookmark manager window for Vibe Video Player.

This module provides a small dedicated UI for browsing and jumping to
time-based bookmarks of the currently loaded video.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import colorchooser, messagebox
from typing import Dict, List, Optional

import customtkinter as ctk

from utils import Tooltip

DEFAULT_BOOKMARK_COLOR = "#FFFFFF"
LEGACY_AUTO_BOOKMARK_COLORS = {"#FFA500", "#FFD700", "#FFFFB3"}
BOOKMARK_MANAGER_MIN_WIDTH = 320
BOOKMARK_MANAGER_MIN_HEIGHT = 360


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

    @staticmethod
    def _normalize_hex_color(value) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if not text.startswith("#"):
            return None
        hex_part = text[1:]
        if len(hex_part) == 3:
            hex_part = "".join(ch * 2 for ch in hex_part)
        if len(hex_part) != 6:
            return None
        try:
            int(hex_part, 16)
        except ValueError:
            return None
        return f"#{hex_part.upper()}"

    @classmethod
    def is_custom_bookmark_color(cls, value) -> bool:
        """True only for user-selected colors, not legacy/auto display colors."""
        color = cls._normalize_hex_color(value)
        if not color:
            return False
        if color == DEFAULT_BOOKMARK_COLOR:
            return False
        return color not in LEGACY_AUTO_BOOKMARK_COLORS

    @staticmethod
    def _blend_hex(bg: str, fg: str, fg_weight: float) -> str:
        try:
            br, bg_c, bb = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
            fr, fg_c, fb = int(fg[1:3], 16), int(fg[3:5], 16), int(fg[5:7], 16)
            w = max(0.0, min(1.0, float(fg_weight)))
            r = int(br * (1.0 - w) + fr * w)
            g = int(bg_c * (1.0 - w) + fg_c * w)
            b = int(bb * (1.0 - w) + fb * w)
            return f"#{r:02X}{g:02X}{b:02X}"
        except (TypeError, ValueError):
            return bg

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
        self.window.geometry(f"{BOOKMARK_MANAGER_MIN_WIDTH}x{BOOKMARK_MANAGER_MIN_HEIGHT}")
        self.window.minsize(BOOKMARK_MANAGER_MIN_WIDTH, BOOKMARK_MANAGER_MIN_HEIGHT)
        self.window.protocol("WM_DELETE_WINDOW", self.on_close)
        self.window.attributes("-topmost", True)
        self.is_open = True

        main_frame = ctk.CTkFrame(self.window, fg_color="transparent")
        main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame.grid_columnconfigure(0, weight=1)
        main_frame.grid_rowconfigure(1, weight=1)

        self.filter_entry = ctk.CTkEntry(
            main_frame,
            textvariable=self.filter_var,
            placeholder_text="Filter bookmarks...",
            height=30,
        )
        self.filter_entry.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        self.filter_entry.bind("<KeyRelease>", self._on_filter_changed)

        self.bookmark_listbox = tk.Listbox(
            main_frame,
            bg="#2B2B2B",
            fg="white",
            selectbackground="#2A6BB0",
            selectforeground="white",
            highlightthickness=0,
            borderwidth=0,
            activestyle="none",
            exportselection=False,
            font=("Segoe UI", 12),
        )
        self.bookmark_listbox.grid(row=1, column=0, sticky="nsew", padx=6, pady=(2, 6))

        self.bookmark_listbox.bind("<<ListboxSelect>>", self._on_bookmark_select)
        self.bookmark_listbox.bind("<Double-1>", self._on_bookmark_double_click)

        self._create_button_panel(main_frame)
        self._populate_bookmark_list()
        self._start_playback_polling()

    def load_bookmarks_for_video(self, bookmarks_data):
        """
        Load bookmark entries for the active video.

        Args:
            bookmarks_data: List of dictionaries in the form:
                ``{"time": float, "label": str, "color": str (optional)}``
        """
        self.bookmarks = []
        seen_keys = {}
        dropped_duplicates = 0

        for item in bookmarks_data or []:
            if not isinstance(item, dict):
                continue

            bookmark_time = item.get("time")
            label = item.get("label", item.get("name", ""))
            if bookmark_time is None:
                continue

            try:
                normalized_time = float(bookmark_time)
            except (TypeError, ValueError):
                continue

            entry = {
                "time": max(0.0, normalized_time),
                "label": str(label) if label is not None else "",
            }
            color = self._normalize_hex_color(item.get("color"))
            if self.is_custom_bookmark_color(color):
                entry["color"] = color

            key = self._bookmark_duplicate_key(entry)
            existing_index = seen_keys.get(key)
            if existing_index is not None:
                dropped_duplicates += 1
                existing = self.bookmarks[existing_index]
                if "color" not in existing and "color" in entry:
                    existing["color"] = entry["color"]
                continue

            seen_keys[key] = len(self.bookmarks)
            self.bookmarks.append(entry)

        self._sort_bookmarks()
        self._populate_bookmark_list()
        if dropped_duplicates:
            self._sync_bookmarks_to_player()

    @staticmethod
    def _bookmark_duplicate_key(bookmark):
        """Return a stable key for exact duplicate bookmark rows."""
        try:
            timestamp = round(float(bookmark.get("time", 0.0)), 3)
        except (TypeError, ValueError):
            timestamp = 0.0
        label = str(bookmark.get("label", bookmark.get("name", ""))).strip().casefold()
        return (timestamp, label)

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

        self._ensure_list_selection()
        self._apply_list_row_styles()

    def _ensure_list_selection(self):
        """Select the first visible row when nothing is selected in the listbox."""
        if not self.bookmark_listbox or not self.visible_indices:
            return
        selection = self.bookmark_listbox.curselection()
        if selection:
            list_idx = selection[0]
            if 0 <= list_idx < len(self.visible_indices):
                return
        self.bookmark_listbox.selection_set(0)
        self.bookmark_listbox.activate(0)

    def _create_button_panel(self, parent_frame):
        """
        Create the bottom control panel in a compact playlist-like style.
        """
        self.button_panel = ctk.CTkFrame(parent_frame, fg_color="transparent")
        self.button_panel.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 6))
        self.button_panel.grid_columnconfigure(0, weight=1)
        self.button_panel.grid_columnconfigure((1, 2, 3, 4, 5), weight=0)

        btn_style = {
            "font": ("Segoe UI", 12),
            "fg_color": "#333333",
            "hover_color": "#444444",
            "text_color": "#dddddd",
            "corner_radius": 3,
            "height": 22,
            "width": 34,
        }

        btn_add = ctk.CTkButton(
            self.button_panel,
            text="+ Add",
            command=self.add_bookmark,
            **btn_style,
        )

        btn_color = ctk.CTkButton(
            self.button_panel,
            text="🎨",
            command=self.set_selected_bookmark_color,
            **btn_style,
        )

        btn_prev = ctk.CTkButton(
            self.button_panel,
            text="◀",
            command=self.skip_to_previous,
            **btn_style,
        )

        btn_next = ctk.CTkButton(
            self.button_panel,
            text="▶",
            command=self.skip_to_next,
            **btn_style,
        )

        btn_delete = ctk.CTkButton(
            self.button_panel,
            text="×",
            command=self.delete_selected_bookmark,
            **btn_style,
        )

        btn_clear = ctk.CTkButton(
            self.button_panel,
            text="🗑",
            command=self.clear_all_bookmarks,
            **btn_style,
        )
        btn_add.configure(width=58)

        btn_add.grid(row=0, column=0, padx=1, pady=1, sticky="w")
        btn_color.grid(row=0, column=1, padx=1, pady=1, sticky="e")
        btn_prev.grid(row=0, column=2, padx=1, pady=1, sticky="e")
        btn_next.grid(row=0, column=3, padx=1, pady=1, sticky="e")
        btn_delete.grid(row=0, column=4, padx=1, pady=1, sticky="e")
        btn_clear.grid(row=0, column=5, padx=1, pady=1, sticky="e")

        self._button_tooltips = [
            Tooltip(btn_add, "Add Bookmark"),
            Tooltip(btn_color, "Set Bookmark Color"),
            Tooltip(btn_prev, "Previous Bookmark"),
            Tooltip(btn_next, "Next Bookmark"),
            Tooltip(btn_delete, "Delete Selected Bookmark"),
            Tooltip(btn_clear, "Clear All Bookmarks"),
        ]

    def _selected_bookmark_time(self) -> Optional[float]:
        """Return the source timestamp (seconds) for the listbox selection, if any."""
        if not self.bookmark_listbox:
            return None
        selection = self.bookmark_listbox.curselection()
        if not selection:
            return None
        list_index = selection[0]
        if not (0 <= list_index < len(self.visible_indices)):
            return None
        bookmark_index = self.visible_indices[list_index]
        target_time = self.bookmarks[bookmark_index].get("time")
        if target_time is None:
            return None
        try:
            return max(0.0, float(target_time))
        except (TypeError, ValueError):
            return None

    def _configure_listbox_row(self, list_idx: int, **options) -> None:
        """Apply row colors, tolerating Tk builds with fewer item options."""
        if not self.bookmark_listbox:
            return
        try:
            self.bookmark_listbox.itemconfig(list_idx, **options)
        except tk.TclError:
            fallback = {k: v for k, v in options.items() if k in {"bg", "fg", "background", "foreground"}}
            if fallback:
                self.bookmark_listbox.itemconfig(list_idx, **fallback)

    def _on_bookmark_select(self, event=None):
        """Single-click: seek to the bookmark without changing play/pause."""
        self._apply_list_row_styles()
        target_time = self._selected_bookmark_time()
        if target_time is None:
            return
        self.video_player.set_time(target_time)

    def _on_bookmark_double_click(self, event=None):
        """Double-click: seek and start playback from the bookmark."""
        target_time = self._selected_bookmark_time()
        if target_time is None:
            return
        if hasattr(self.video_player, "play_from_time"):
            self.video_player.play_from_time(target_time)
            return
        self.video_player.set_time(target_time)
        for method_name in ("play", "play_video"):
            play_fn = getattr(self.video_player, method_name, None)
            if callable(play_fn):
                play_fn()
                break

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
            self._apply_list_row_styles()

    def _apply_list_row_styles(self):
        """Apply per-bookmark colors and active-playback row highlight."""
        if not self.bookmark_listbox:
            return

        default_bg = "#2B2B2B"
        active_bg = "#404040"
        selected_bg = "#2A6BB0"
        selected_active_bg = "#347BD0"
        selected_fg = "#FFFFFF"
        selected_set = set(self.bookmark_listbox.curselection())

        for list_idx, bookmark_idx in enumerate(self.visible_indices):
            bookmark = self.bookmarks[bookmark_idx]
            is_active = bookmark_idx == self.active_bookmark_index
            is_selected = list_idx in selected_set
            color = self._normalize_hex_color(bookmark.get("color"))
            if is_selected:
                row_bg = selected_active_bg if is_active else selected_bg
                row_fg = color if self.is_custom_bookmark_color(color) else selected_fg
                self._configure_listbox_row(
                    list_idx,
                    bg=row_bg,
                    fg=row_fg,
                    selectbackground=row_bg,
                    selectforeground=row_fg,
                )
                continue

            if self.is_custom_bookmark_color(color):
                mix = 0.42 if is_active else 0.28
                row_bg = self._blend_hex(default_bg if not is_active else active_bg, color, mix)
                row_fg = color
            else:
                row_bg = active_bg if is_active else default_bg
                row_fg = "white"
            self._configure_listbox_row(
                list_idx,
                bg=row_bg,
                fg=row_fg,
                selectbackground=selected_bg,
                selectforeground=selected_fg,
            )

    def _selected_bookmark_index(self) -> Optional[int]:
        if not self.bookmark_listbox:
            return None
        selection = self.bookmark_listbox.curselection()
        if not selection:
            return None
        list_index = selection[0]
        if not (0 <= list_index < len(self.visible_indices)):
            return None
        bookmark_index = self.visible_indices[list_index]
        if not (0 <= bookmark_index < len(self.bookmarks)):
            return None
        return bookmark_index

    def set_selected_bookmark_color(self):
        """Pick a color for the selected bookmark and sync to timeline + JSON."""
        self._ensure_list_selection()
        bookmark_index = self._selected_bookmark_index()
        if bookmark_index is None:
            messagebox.showinfo("Bookmark Color", "Select a bookmark first.", parent=self.window)
            return

        bookmark = self.bookmarks[bookmark_index]
        initial = self._normalize_hex_color(bookmark.get("color")) or DEFAULT_BOOKMARK_COLOR
        result = colorchooser.askcolor(
            color=initial,
            title="Bookmark color",
            parent=self.window,
        )
        if not result or result[1] is None:
            return

        chosen = self._normalize_hex_color(result[1])
        if not chosen:
            return

        if self.is_custom_bookmark_color(chosen):
            bookmark["color"] = chosen
        else:
            bookmark.pop("color", None)
        self._populate_bookmark_list()
        self._sync_bookmarks_to_player()

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

        def finish_add_bookmark(user_label):
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
                    "label": str(user_label).strip() or default_label,
                }
            )
            self._sort_bookmarks()
            self._populate_bookmark_list()
            self._sync_bookmarks_to_player()

        dialog_owner = self.parent if hasattr(self.parent, "universal_dialog") else None
        if dialog_owner is None:
            dialog_owner = getattr(self.video_player, "controller", None)
        if not dialog_owner or not hasattr(dialog_owner, "universal_dialog"):
            messagebox.showerror("Add Bookmark", "Bookmark dialog is not available.", parent=self.window)
            return

        dialog_owner.universal_dialog(
            title="Add Bookmark",
            message=f"Label for bookmark at {self._format_time(current_time)}:",
            confirm_callback=finish_add_bookmark,
            input_field=True,
            default_input=default_label,
            modal=False,
        )

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
                self._apply_list_row_styles()
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
