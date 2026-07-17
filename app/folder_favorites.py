"""
Folder favorites (location bookmarks) for Vibe Player.

Pinned folder paths with a Favorites menu and a small organize window.
Each favorite stores ``{"path": "...", "name": "..."}``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox

from utils import Tooltip, create_menu

FAVORITES_FILE = "folder_favorites.json"
FavoriteEntry = Dict[str, str]

_BG = "#1A1C1E"
_LIST_BG = "#2B2B2B"
_TEXT = "#F2F4F8"
_WINDOW_WIDTH = 480
_WINDOW_HEIGHT = 420
_MIN_WIDTH = 360
_MIN_HEIGHT = 280


def default_favorite_name(path: str) -> str:
    """Default display name from the folder basename."""
    cleaned = str(path or "").rstrip("\\/")
    return os.path.basename(cleaned) or cleaned or path


def normalize_favorite_entry(item: Any) -> Optional[FavoriteEntry]:
    """Normalize a JSON item to ``{"path", "name"}`` (supports legacy plain strings)."""
    if isinstance(item, str):
        path = os.path.normpath(item.strip())
        if not path:
            return None
        return {"path": path, "name": default_favorite_name(path)}

    if isinstance(item, dict):
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        path = os.path.normpath(raw_path.strip())
        name = str(item.get("name") or "").strip() or default_favorite_name(path)
        return {"path": path, "name": name}

    return None


def favorite_entry_path(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("path") or "")
    return str(entry or "")


def favorite_menu_label(entry: Any, favorites: Optional[List[Any]] = None) -> str:
    """Label for menus/lists — custom name, with parent hint on duplicates."""
    if isinstance(entry, dict):
        path = favorite_entry_path(entry)
        name = str(entry.get("name") or "").strip() or default_favorite_name(path)
    else:
        path = str(entry or "")
        name = default_favorite_name(path)

    if favorites:
        same_name_count = 0
        for item in favorites:
            if isinstance(item, dict):
                other = str(item.get("name") or "").strip() or default_favorite_name(
                    favorite_entry_path(item)
                )
            else:
                other = default_favorite_name(str(item))
            if other.casefold() == name.casefold():
                same_name_count += 1
        if same_name_count > 1:
            parent = os.path.basename(os.path.dirname(path.rstrip("\\/")))
            if parent:
                return f"{name} ({parent})"
    return name


def load_folder_favorites(path: str = FAVORITES_FILE) -> List[FavoriteEntry]:
    """Load favorite folder entries from JSON."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("favorites", []) if isinstance(data, dict) else []
        favorites: List[FavoriteEntry] = []
        seen = set()
        for item in raw:
            entry = normalize_favorite_entry(item)
            if not entry:
                continue
            key = os.path.normcase(entry["path"])
            if key in seen:
                continue
            seen.add(key)
            favorites.append(entry)
        return favorites
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        logging.info("[Favorites] Failed to load %s: %s", path, exc)
        return []


def save_folder_favorites(favorites: List[FavoriteEntry], path: str = FAVORITES_FILE) -> None:
    """Persist favorite folder entries to JSON."""
    payload = []
    for item in favorites or []:
        entry = normalize_favorite_entry(item)
        if entry:
            payload.append(entry)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"favorites": payload}, f, indent=2)
    except OSError as exc:
        logging.info("[Favorites] Failed to save %s: %s", path, exc)


class FolderFavoritesManager:
    """Manage pinned folder favorites and the organize window."""

    def __init__(self, controller):
        self.controller = controller
        self.window = None
        self.listbox = None
        self.is_open = False
        self._button_tooltips = []

    @property
    def favorites(self) -> List[FavoriteEntry]:
        return getattr(self.controller, "folder_favorites", [])

    @favorites.setter
    def favorites(self, value: List[FavoriteEntry]) -> None:
        self.controller.folder_favorites = list(value)

    def _persist_and_refresh(self) -> None:
        save_folder_favorites(self.favorites)
        ctrl = self.controller
        # Rebuild after the Favorites menu has fully dismissed (same grab issue).
        ctrl.after_idle(lambda: rebuild_favorites_menu(ctrl))
        self._populate_listbox()

    def _path_already_favorited(self, path: str) -> bool:
        key = os.path.normcase(os.path.normpath(path))
        return any(os.path.normcase(favorite_entry_path(item)) == key for item in self.favorites)

    def add_current_folder(self) -> None:
        """Pin the currently open folder to favorites."""
        # Defer so Favorites menu can release its grab before any dialogs.
        self.controller.after(1, self._add_current_folder_impl)

    def _add_current_folder_impl(self) -> None:
        path = getattr(self.controller, "current_directory", None)
        if not path or not os.path.isdir(path):
            messagebox.showinfo("Favorites", "No valid folder is currently open.")
            return
        self.add_folder(path, quiet_if_duplicate=False)

    def add_folder(self, path: str, quiet_if_duplicate: bool = True) -> bool:
        """Prompt for a name and add ``path`` to favorites."""
        if not path or not os.path.isdir(path):
            messagebox.showwarning("Favorites", f"Folder not found:\n{path}")
            return False

        normalized = os.path.normpath(path)
        if self._path_already_favorited(normalized):
            if not quiet_if_duplicate:
                messagebox.showinfo("Favorites", "This folder is already in Favorites.")
            return False

        default_name = default_favorite_name(normalized)
        ctrl = self.controller

        def on_confirm(name: str) -> None:
            label = (name or "").strip() or default_name
            if self._path_already_favorited(normalized):
                messagebox.showinfo("Favorites", "This folder is already in Favorites.")
                return
            self.favorites = self.favorites + [{"path": normalized, "name": label}]
            self._persist_and_refresh()
            logging.info("[Favorites] Added: %s (%s)", label, normalized)

        if hasattr(ctrl, "universal_dialog"):
            ctrl.universal_dialog(
                title="Add to Favorites",
                message="Enter a name for this favorite:",
                confirm_callback=on_confirm,
                input_field=True,
                default_input=default_name,
                confirm_text="OK",
                modal=True,
            )
            return True

        on_confirm(default_name)
        return True

    def navigate_to(self, path: str) -> None:
        """Open a favorite folder in the main browser."""
        if not path or not os.path.isdir(path):
            messagebox.showwarning("Favorites", f"Folder not found:\n{path}")
            return

        normalized = os.path.normpath(path)
        ctrl = self.controller
        # Defer past menu unpost/grab release. Running display_thumbnails /
        # tree expand inside a Tk menu command freezes the UI on Windows
        # while background workers keep logging.
        ctrl.after(1, lambda p=normalized: self._navigate_to_impl(p))

    def _navigate_to_impl(self, path: str) -> None:
        if not path or not os.path.isdir(path):
            messagebox.showwarning("Favorites", f"Folder not found:\n{path}")
            return

        ctrl = self.controller
        ctrl.display_thumbnails(path)
        ctrl.current_directory = path
        if hasattr(ctrl, "select_current_folder_in_tree"):
            ctrl.select_current_folder_in_tree()
        if hasattr(ctrl, "add_to_recent_directories"):
            ctrl.add_to_recent_directories(path)

    def show_organize(self) -> None:
        """Open the favorites organize window."""
        # Defer so Favorites menu can release its grab first.
        self.controller.after(1, self._show_organize_impl)

    def _show_organize_impl(self) -> None:
        if self.is_open and self.window and self.window.winfo_exists():
            self.window.attributes("-topmost", True)
            self.window.focus_force()
            return

        self.window = ctk.CTkToplevel(self.controller)
        self.window.title("Organize Favorites")
        self.window.geometry(f"{_WINDOW_WIDTH}x{_WINDOW_HEIGHT}")
        self.window.minsize(_MIN_WIDTH, _MIN_HEIGHT)
        self.window.configure(fg_color=_BG)
        self.window.protocol("WM_DELETE_WINDOW", self.on_close)
        self.window.attributes("-topmost", True)
        self.is_open = True

        main_frame = ctk.CTkFrame(self.window, fg_color=_BG, corner_radius=0, border_width=0)
        main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame.grid_columnconfigure(0, weight=1)
        main_frame.grid_rowconfigure(0, weight=1)

        self.listbox = tk.Listbox(
            main_frame,
            bg=_LIST_BG,
            fg=_TEXT,
            selectbackground="#1F6AA5",
            selectforeground="white",
            highlightthickness=0,
            borderwidth=0,
            activestyle="none",
            exportselection=False,
            font=("Segoe UI", 11),
        )
        self.listbox.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.listbox.bind("<Double-1>", self._on_double_click)
        self.listbox.bind("<Delete>", lambda _e: self.remove_selected())
        self.listbox.bind("<Return>", self._on_double_click)

        self._create_button_panel(main_frame)
        self._populate_listbox()

    def _create_button_panel(self, parent_frame) -> None:
        button_frame = ctk.CTkFrame(parent_frame, fg_color=_BG, corner_radius=0, border_width=0)
        button_frame.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))
        button_frame.grid_columnconfigure(0, weight=1)
        button_frame.grid_columnconfigure((1, 2, 3, 4, 5), weight=0)

        btn_style = {
            "font": ("Segoe UI", 12),
            "fg_color": "#333333",
            "hover_color": "#444444",
            "text_color": "#dddddd",
            "corner_radius": 3,
            "height": 22,
            "width": 34,
        }

        btn_add = ctk.CTkButton(button_frame, text="+ Add", command=self._browse_add, **btn_style)
        btn_add.configure(width=58)
        btn_up = ctk.CTkButton(button_frame, text="▲", command=self.move_selected_up, **btn_style)
        btn_down = ctk.CTkButton(button_frame, text="▼", command=self.move_selected_down, **btn_style)
        btn_go = ctk.CTkButton(button_frame, text="▶", command=self.open_selected, **btn_style)
        btn_rem = ctk.CTkButton(button_frame, text="×", command=self.remove_selected, **btn_style)
        btn_clear = ctk.CTkButton(button_frame, text="🗑", command=self.clear_all, **btn_style)

        btn_add.grid(row=0, column=0, padx=1, pady=1, sticky="w")
        btn_up.grid(row=0, column=1, padx=1, pady=1, sticky="e")
        btn_down.grid(row=0, column=2, padx=1, pady=1, sticky="e")
        btn_go.grid(row=0, column=3, padx=1, pady=1, sticky="e")
        btn_rem.grid(row=0, column=4, padx=1, pady=1, sticky="e")
        btn_clear.grid(row=0, column=5, padx=1, pady=1, sticky="e")

        self._button_tooltips = [
            Tooltip(btn_add, "Add Folder"),
            Tooltip(btn_up, "Move Up"),
            Tooltip(btn_down, "Move Down"),
            Tooltip(btn_go, "Go to Folder"),
            Tooltip(btn_rem, "Remove Selected"),
            Tooltip(btn_clear, "Clear All Favorites"),
        ]

    def _populate_listbox(self) -> None:
        if not self.is_open or not self.listbox or not self.listbox.winfo_exists():
            return
        selection = self.listbox.curselection()
        selected_idx = selection[0] if selection else None

        self.listbox.delete(0, tk.END)
        for entry in self.favorites:
            label = favorite_menu_label(entry, self.favorites)
            path = favorite_entry_path(entry)
            self.listbox.insert(tk.END, f"{label}  —  {path}")

        if selected_idx is not None and 0 <= selected_idx < self.listbox.size():
            self.listbox.selection_set(selected_idx)
            self.listbox.activate(selected_idx)
            self.listbox.see(selected_idx)

    def _selected_index(self) -> Optional[int]:
        if not self.listbox:
            return None
        selection = self.listbox.curselection()
        if not selection:
            return None
        idx = int(selection[0])
        if 0 <= idx < len(self.favorites):
            return idx
        return None

    def _browse_add(self) -> None:
        initial = getattr(self.controller, "current_directory", None) or None
        path = filedialog.askdirectory(title="Add Folder to Favorites", initialdir=initial)
        if path:
            self.add_folder(path, quiet_if_duplicate=False)

    def remove_selected(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        items = list(self.favorites)
        removed = items.pop(idx)
        self.favorites = items
        self._persist_and_refresh()
        logging.info("[Favorites] Removed: %s", removed)
        if self.listbox and self.listbox.size() > 0:
            new_idx = min(idx, self.listbox.size() - 1)
            self.listbox.selection_set(new_idx)
            self.listbox.activate(new_idx)

    def move_selected_up(self) -> None:
        idx = self._selected_index()
        if idx is None or idx <= 0:
            return
        items = list(self.favorites)
        items[idx - 1], items[idx] = items[idx], items[idx - 1]
        self.favorites = items
        self._persist_and_refresh()
        if self.listbox:
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(idx - 1)
            self.listbox.activate(idx - 1)
            self.listbox.see(idx - 1)

    def move_selected_down(self) -> None:
        idx = self._selected_index()
        if idx is None or idx >= len(self.favorites) - 1:
            return
        items = list(self.favorites)
        items[idx + 1], items[idx] = items[idx], items[idx + 1]
        self.favorites = items
        self._persist_and_refresh()
        if self.listbox:
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(idx + 1)
            self.listbox.activate(idx + 1)
            self.listbox.see(idx + 1)

    def open_selected(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        self.navigate_to(favorite_entry_path(self.favorites[idx]))

    def clear_all(self) -> None:
        if not self.favorites:
            return
        if not messagebox.askyesno("Favorites", "Remove all favorite folders?"):
            return
        self.favorites = []
        self._persist_and_refresh()

    def _on_double_click(self, _event=None):
        self.open_selected()
        return "break"

    def on_close(self) -> None:
        self.is_open = False
        self.listbox = None
        if self.window and self.window.winfo_exists():
            self.window.destroy()
        self.window = None


def build_favorites_menu(app):
    """Build the Favorites menu (add / organize / pinned folders)."""
    menu = create_menu(app, app)
    mgr = getattr(app, "folder_favorites_manager", None)

    menu.add_command(
        label="Add to Favorites",
        command=(mgr.add_current_folder if mgr else (lambda: None)),
    )
    menu.add_command(
        label="Organize Favorites...",
        command=(mgr.show_organize if mgr else (lambda: None)),
    )
    menu.add_separator()

    favorites = getattr(app, "folder_favorites", []) or []
    if not favorites:
        menu.add_command(label="(empty)", state="disabled")
    else:
        for entry in favorites:
            path = favorite_entry_path(entry)
            menu.add_command(
                label=favorite_menu_label(entry, favorites),
                command=(lambda p=path: mgr.navigate_to(p) if mgr else None),
            )
    return menu


def rebuild_favorites_menu(app) -> None:
    """Recreate the Favorites popup after the list changes."""
    if not hasattr(app, "favorites_button"):
        return
    app._favorites_menu = build_favorites_menu(app)
