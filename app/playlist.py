"""
Playlist window and ``PlaylistManager`` for Vibe Player.

Add, remove, reorder, and play videos from a persisted list tied to the main player.
"""

import tkinter as tk
from tkinter import filedialog
import customtkinter as ctk
import os
import json
import random
import logging

# Import the create_menu utility
from utils import create_menu

import tkinterdnd2 as dnd
from vtp_constants import VIDEO_FORMATS
from vtp_mixin_dnd import VtpDndMixin

class PlaylistManager:
    def __init__(self, parent, controller):
        """
        Initializes the PlaylistManager window and its components.
        """
        self.parent = parent
        self.controller = controller
        self.playlist_window = None
        self.playlist = []
        self.current_playing_index = -1
        self.is_playlist_open = False




    def show_playlist(self):
        """
        Creates and displays the playlist window if it doesn't already exist.
        """
        logging.info("show_playlist() called.")
        
        # Check if the playlist window already exists
        if self.is_playlist_open and self.playlist_window and self.playlist_window.winfo_exists():
            logging.info("Playlist window already exists. Bringing it to the front.")
            
            # self.playlist_window.lift()
            self.playlist_window.attributes('-topmost', True)
            self.playlist_window.focus_force()
            
            logging.info("Attributes '-topmost' set to True and focus forced.")
            return

        # Create a new playlist window
        logging.info("Creating a new playlist window.")
        self.playlist_window = ctk.CTkToplevel(self.parent)
        self.playlist_window.title("Playlist")
        self.playlist_window.geometry("400x600")
        self.is_playlist_open = True
        self.playlist_window.protocol("WM_DELETE_WINDOW", self.on_close)

        # Set the new window to be always on top
        self.playlist_window.attributes('-topmost', True)
        logging.info("New playlist window created with '-topmost' set to True.")

        # --- Main Frame for Layout ---
        self.playlist_main_frame = ctk.CTkFrame(self.playlist_window, fg_color="transparent")
        self.playlist_main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame = self.playlist_main_frame
        logging.info("Main frame created.")

        # --- Listbox for playlist items ---
        self.playlist_box = tk.Listbox(main_frame, bg="#2B2B2B", fg="white",
                                     selectbackground="#1F6AA5", highlightthickness=0, borderwidth=0)
        self.playlist_box.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Bind events
        self.playlist_box.bind("<Double-1>", self._on_double_click)
        self.playlist_box.bind("<Delete>", self.remove_selected)
        self.playlist_box.bind("<Control-a>", self.select_all)
        logging.info("Listbox created and events bound.")

        # --- NEW: Button Panel ---
        self.create_button_panel(main_frame)
        logging.info("Button panel created.")

        self.populate_playlist_box()
        self.update_playlist_selection()
        logging.info("Playlist populated and selection updated.")

        self._setup_playlist_drop_target()

    def _setup_playlist_drop_target(self):
        """Append dropped video files (internal or Explorer); COPY only — no file move."""
        targets = []
        for w in (self.playlist_box, self.playlist_main_frame, self.playlist_window):
            if w is None:
                continue
            surf = w if hasattr(w, "drop_target_register") else getattr(w, "_canvas", None)
            if surf is not None and hasattr(surf, "drop_target_register") and surf not in targets:
                targets.append(surf)
        for surf in targets:
            try:
                surf.drop_target_register(dnd.DND_FILES)
                surf.dnd_bind("<<Drop>>", self._on_playlist_files_drop)
                surf.dnd_bind("<<DropPosition>>", self._on_playlist_drop_position)
            except Exception as e:
                logging.warning("[DnD] playlist drop target failed: %s", e)

    def _on_playlist_drop_position(self, event):
        return dnd.COPY

    def _on_playlist_files_drop(self, event):
        paths = VtpDndMixin._dnd_parse_paths(event.data)
        videos = [
            p
            for p in paths
            if isinstance(p, str) and os.path.isfile(p) and p.lower().endswith(VIDEO_FORMATS)
        ]
        if videos:
            self.add_to_playlist(videos)

    def update_ui_selection(self, index):
        """
        Updates the visual selection in the Listbox based on the index (called from the player).
        Ensures the row is highlighted in blue and visible (scrolls if necessary).
        """
        # Check if the window and listbox exist
        # In your file, the listbox is named 'self.playlist_box'
        if not hasattr(self, "playlist_box") or not self.playlist_box.winfo_exists():
            return

        try:
            # 1. Clear the previous selection
            self.playlist_box.selection_clear(0, "end")
            
            # 2. Activate and select the new row
            self.playlist_box.activate(index)
            self.playlist_box.selection_set(index)
            
            # 3. IMPORTANT: Scroll to make the row visible (if the list is long)
            self.playlist_box.see(index)
            
        except Exception as e:
            logging.info(f"[Playlist Error] Failed to update selection: {e}")

    def create_button_panel(self, parent_frame):
        """
        Creates a frame at the bottom with smaller, styled control buttons,
        matching the aesthetic of the timeline widget.
        """
        # Frame to hold the buttons
        button_frame = ctk.CTkFrame(parent_frame, fg_color="transparent")
        button_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=(0, 5))
        button_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        # Style dictionary for the buttons, inspired by TimelineBarWidget
        btn_style = {
            "font": ("Segoe UI", 10),
            "fg": "#dddddd",
            "bg": "#333333",
            "activebackground": "#444444",
            "activeforeground": "white",
            "relief": "flat",
            "padx": 8,
            "pady": 4,
            "borderwidth": 0,
            "highlightthickness": 0
        }

        # --- Button Definitions using tk.Button and the shared style ---
        btn_add = tk.Button(button_frame, text="✚ Add", command=self.add_files_dialog, **btn_style)
        btn_rem = tk.Button(button_frame, text="✖ Remove", command=self.remove_selected, **btn_style)
        btn_sort = tk.Button(button_frame, text="⇅ Sort", command=self.show_sort_menu, **btn_style)
        btn_clear = tk.Button(button_frame, text="🗑 Clear", command=self.clear_playlist, **btn_style)

        # --- Place buttons in the grid ---
        btn_add.grid(row=0, column=0, padx=1, pady=1, sticky="ew")
        btn_rem.grid(row=0, column=1, padx=1, pady=1, sticky="ew")
        btn_sort.grid(row=0, column=2, padx=1, pady=1, sticky="ew")
        btn_clear.grid(row=0, column=3, padx=1, pady=1, sticky="ew")
        
        # Store the sort button to anchor the menu to it
        self.sort_button = btn_sort

   

    def add_files_dialog(self):
        """
        Opens a file dialog to select and add video files to the playlist.
        """
        file_paths = filedialog.askopenfilenames(
            title="Select Video Files",
            filetypes=(("Video files", "*.mp4 *.avi *.mkv *.mov"), ("All files", "*.*"))
        )
        if file_paths:
            self.add_to_playlist(list(file_paths))

    def show_sort_menu(self):
        """
        Displays a dropdown menu with sorting options.
        """
        menu = create_menu(self.controller, self.playlist_window)
        menu.add_command(label="Sort A-Z", command=self.sort_playlist_az)
        menu.add_command(label="Sort Z-A", command=self.sort_playlist_za)
        menu.add_separator()
        menu.add_command(label="Shuffle", command=self.shuffle_playlist)

        # Position and display the menu under the sort button
        x = self.sort_button.winfo_rootx()
        y = self.sort_button.winfo_rooty() + self.sort_button.winfo_height()
        menu.tk_popup(x, y)

    def sort_playlist_az(self):
        """
        Sorts the current playlist alphabetically (A-Z).
        """
        self.playlist.sort(key=lambda x: os.path.basename(x).lower())
        self.populate_playlist_box()
        logging.info("Playlist sorted A-Z.")

    def sort_playlist_za(self):
        """
        Sorts the current playlist in reverse alphabetical order (Z-A).
        """
        self.playlist.sort(key=lambda x: os.path.basename(x).lower(), reverse=True)
        self.populate_playlist_box()
        logging.info("Playlist sorted Z-A.")

    def add_to_playlist(self, file_paths):
        """
        Adds a list of file paths to the playlist.

        Returns the number of paths actually appended (skips duplicates already in the list).
        """
        added_count = 0
        for path in file_paths:
            if path not in self.playlist:
                self.playlist.append(path)
                added_count += 1
        if added_count > 0:
            self.populate_playlist_box()
        logging.info(f"Added {added_count} items to the playlist.")
        return added_count

    def remove_selected(self, event=None):
        """
        Removes the currently selected items from the playlist.
        """
        selected_indices = self.playlist_box.curselection()
        # Iterate backwards to avoid index shifting issues
        for i in sorted(selected_indices, reverse=True):
            del self.playlist[i]
        self.populate_playlist_box()

    def clear_playlist(self, event=None):
        """
        Removes all items from the playlist.
        """
        self.playlist.clear()
        self.current_playing_index = -1
        self.populate_playlist_box()
        logging.info("Playlist cleared.")

    def shuffle_playlist(self):
        """
        Randomly shuffles the items in the playlist.
        """
        random.shuffle(self.playlist)
        self.populate_playlist_box()
        logging.info("Playlist shuffled.")

    def populate_playlist_box(self):
        """
        Clears and repopulates the listbox with current playlist items.
        """
        if self.is_playlist_open and self.playlist_box:
            self.playlist_box.delete(0, tk.END)
            for item in self.playlist:
                self.playlist_box.insert(tk.END, os.path.basename(item))
            self.update_playlist_selection()

    def update_playlist_selection(self):
        """
        Highlights the currently playing item in the listbox.
        """
        if not (self.is_playlist_open and self.playlist_box):
            return
        self.playlist_box.selection_clear(0, tk.END)
        if 0 <= self.current_playing_index < len(self.playlist):
            self.playlist_box.selection_set(self.current_playing_index)
            self.playlist_box.activate(self.current_playing_index)
            self.playlist_box.see(self.current_playing_index)

    def _on_double_click(self, event):
        """
        Handles double-click event on a playlist item to play it.
        """
        selection = self.playlist_box.curselection()
        if selection:
            index = selection[0]
            self.current_playing_index = index
            video_path = self.playlist[index]
            self.controller.open_video_player(video_path, os.path.basename(video_path))
            self.update_playlist_selection()

    def on_close(self):
        """
        Handles the closing of the playlist window.
        """
        self.is_playlist_open = False
        if self.playlist_window:
            self.playlist_window.destroy()
            self.playlist_window = None
        logging.info("Playlist window closed.")

    def select_all(self, event=None):
        """
        Selects all items in the listbox.
        """
        self.playlist_box.select_set(0, tk.END)
        return "break"  # Prevents default event handling