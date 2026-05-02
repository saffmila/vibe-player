"""
Menus, toolbar, preferences, search, and hotkey UI for Vibe Player.

Implements ``setup_menu``, ``setup_gui``, preference/search dialogs, and AutoTag settings.
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable
from PIL import Image, ImageTk
from tkinter import Menu, messagebox, filedialog
import shutil
import json
import webbrowser
import customtkinter as ctk

from app_settings import TaggingSettings
from utils import create_menu
import logging
from hotkeys import DEFAULT_HOTKEYS, format_accelerator_menu, iter_help_sections, menu_accel

# .app calls video_thumbnail_player, so no need to import it


class TogglePanelFrame(ctk.CTkFrame):
    def __init__(self, parent, title="Panel", default_height=150, app=None):
        super().__init__(parent, height=default_height) #fg_color="darkblue"
        self.expanded = True
        self.parent_paned = None
        self.default_height = default_height
        self.title = title
        self.app = app
        self.pack_propagate(False)
        self.preferences_window = None

        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(side="top", fill="x", pady=(0, 0), padx=3)

        self.title_label = ctk.CTkLabel(
            header_frame,
            text=self.title,
            font=ctk.CTkFont(size=10, weight="bold"),
            height=16  
        )
        self.title_label.pack(side="left", padx=(5, 0), pady=1)

        self.toggle_button = ctk.CTkButton(
            header_frame,
            text="▼",
            width=14,
            height=10,
            font=ctk.CTkFont(size=9),
            command=self.toggle_panel
        )
        self.toggle_button.pack(side="right", padx=2, pady=1)

        self.content_widget = None

    def set_content(self, widget):
        """Optional helper to insert widget into content_frame."""
        self.content_widget = widget
        self.content_widget.pack(in_=self, fill="both", expand=True)



    def toggle_panel(self):
        if self.parent_paned is None:
            parent = self.winfo_parent()
            widget = self.nametowidget(parent)
            if isinstance(widget, tk.PanedWindow):
                self.parent_paned = widget
            else:
                logging.info("[WARNING] TogglePanelFrame: not inside PanedWindow")
                return

        if self.expanded:
            logging.info(f"[TOGGLE] Hiding panel '{self.title}'")
            self.parent_paned.forget(self)
            self.expanded = False
        else:
            logging.info(f"[TOGGLE] Showing panel '{self.title}'")
            self.parent_paned.add(self)
            self.configure(height=self.default_height)
            self.parent_paned.paneconfig(self, minsize=80, height=self.default_height)
            self.expanded = True

        if hasattr(self, "app"):
            try:
                self.app.update_panel_flags(self.title, self.expanded)
            except Exception as e:
                logging.info("Error calling update_panel_flags via self.app: %s", e)


        self.toggle_button.configure(text="▼" if self.expanded else "▲")


class ConflictDialog(ctk.CTkToplevel):
    """Modal copy/move conflict dialog with optional 'apply to all'."""

    def __init__(self, parent, file_name: str):
        super().__init__(parent)
        self.title("File already exists")
        self.geometry("420x200")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.result: tuple[str, bool] = ("cancel", False)

        self.apply_all_var = ctk.BooleanVar(value=False)

        ctk.CTkLabel(
            self,
            text="An item with the same name already exists:",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(14, 6))
        ctk.CTkLabel(
            self,
            text=file_name,
            anchor="w",
            font=ctk.CTkFont(weight="bold"),
        ).pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkCheckBox(
            self,
            text="Apply to all files",
            variable=self.apply_all_var,
        ).pack(anchor="w", padx=14, pady=(0, 10))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkButton(btn_row, text="Replace", command=self._on_replace).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="Skip", command=self._on_skip).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="Cancel", command=self._on_cancel).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _close_with(self, action: str):
        self.result = (action, bool(self.apply_all_var.get()))
        self.destroy()

    def _on_replace(self):
        self._close_with("replace")

    def _on_skip(self):
        self._close_with("skip")

    def _on_cancel(self):
        self._close_with("cancel")

    def show_modal(self) -> tuple[str, bool]:
        """Show conflict dialog and block until user picks an action."""
        self.grab_set()
        self.focus_force()
        self.wait_window()
        return self.result


def open_conflict_dialog(parent, file_name: str) -> tuple[str, bool]:
    """Show conflict dialog and return (action, apply_to_all)."""
    dialog = ConflictDialog(parent, file_name)
    return dialog.show_modal()




def add_hover_effect(widget):
    widget.bind("<Enter>", lambda e: widget.configure(font=("Helvetica", 14, "underline")))
    widget.bind("<Leave>", lambda e: widget.configure(font=("Helvetica", 14)))
    



def setup_menu(app):
    app.menu_bar = ctk.CTkFrame(app, fg_color=app.BackroundColor, height=28)
    app.menu_bar.pack(side="top", fill="x")

    # --- File Menu ---
    app._file_menu = build_file_menu(app, app.menu_bar)
    
    app.file_button = ctk.CTkLabel(
        app.menu_bar,
        text="File",
        text_color=app.thumb_TextColor,
        font=("Helvetica", 14),
        cursor="hand2"
    )
    app.file_button.pack(side="left", padx=10, pady=2)
    app.file_button.bind("<Button-1>", lambda e: app._file_menu.tk_popup(
        app.file_button.winfo_rootx(), 
        app.file_button.winfo_rooty() + app.file_button.winfo_height()
    ))

    # --- View Menu ---
    app._view_menu = build_view_menu(app)
    
    app.view_button = ctk.CTkLabel(
        app.menu_bar,
        text="View",
        text_color=app.thumb_TextColor,
        font=("Helvetica", 14),
        cursor="hand2"
    )
    app.view_button.pack(side="left", padx=10, pady=2)
    app.view_button.bind("<Button-1>", lambda e: app._view_menu.tk_popup(
        app.view_button.winfo_rootx(), 
        app.view_button.winfo_rooty() + app.view_button.winfo_height()
    ))

    # --- Edit Menu (formerly Options) ---
    app._edit_menu = build_edit_menu(app)

    app.edit_button = ctk.CTkLabel(
        app.menu_bar,
        text="Edit",
        text_color=app.thumb_TextColor,
        font=("Helvetica", 14),
        cursor="hand2"
    )
    app.edit_button.pack(side="left", padx=10, pady=2)
    app.edit_button.bind("<Button-1>", lambda e: app._edit_menu.tk_popup(
        app.edit_button.winfo_rootx(), 
        app.edit_button.winfo_rooty() + app.edit_button.winfo_height()
    ))

    # --- Rating Menu ---
    app._rating_menu = build_rating_menu(app)

    app.rating_button = ctk.CTkLabel(
        app.menu_bar,
        text="Rating",
        text_color=app.thumb_TextColor,
        font=("Helvetica", 14),
        cursor="hand2"
    )
    app.rating_button.pack(side="left", padx=10, pady=2)
    app.rating_button.bind("<Button-1>", lambda e: app._rating_menu.tk_popup(
        app.rating_button.winfo_rootx(), 
        app.rating_button.winfo_rooty() + app.rating_button.winfo_height()
    ))

    # --- Help Menu ---
    app._help_menu = build_help_menu(app)
    app.help_button = ctk.CTkLabel(
        app.menu_bar,
        text="Help",
        text_color=app.thumb_TextColor,
        font=("Helvetica", 14),
        cursor="hand2"
    )
    app.help_button.pack(side="left", padx=10, pady=2)
    app.help_button.bind("<Button-1>", lambda e: app._help_menu.tk_popup(
        app.help_button.winfo_rootx(),
        app.help_button.winfo_rooty() + app.help_button.winfo_height()
    ))



def show_hotkeys_window(app):
    """
    Displays a read-only table of current hotkeys (grouped by category).
    Uses ``app.hotkeys_map`` when set, otherwise defaults from ``hotkeys.DEFAULT_HOTKEYS``.
    """
    hm = getattr(app, "hotkeys_map", None) or DEFAULT_HOTKEYS
    if not hm:
        logging.warning("Hotkeys map empty.")
        return

    if hasattr(app, 'hotkeys_window') and app.hotkeys_window is not None and app.hotkeys_window.winfo_exists():
        app.hotkeys_window.focus()
        return

    hk_window = ctk.CTkToplevel(app)
    hk_window.title("Keyboard Shortcuts")
    hk_window.geometry("580x640")
    hk_window.attributes('-topmost', True) 
    
    app.hotkeys_window = hk_window

    ctk.CTkLabel(hk_window, text="Keyboard Shortcuts", font=("Helvetica", 18, "bold")).pack(pady=(15, 6))
    ctk.CTkLabel(
        hk_window,
        text="Context menus show the same keys where applicable.",
        font=("Helvetica", 11),
        text_color="gray70",
    ).pack(pady=(0, 8))

    scroll_frame = ctk.CTkScrollableFrame(hk_window)
    scroll_frame.pack(fill="both", expand=True, padx=10, pady=10)

    row = 0
    for section_title, items in iter_help_sections(hm):
        ctk.CTkLabel(
            scroll_frame,
            text=section_title,
            font=("Helvetica", 13, "bold"),
            anchor="w",
        ).grid(row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=(14, 6))
        row += 1
        for action, seq in items:
            readable_action = action.replace("_", " ").title()
            readable_key = format_accelerator_menu(seq) if isinstance(seq, str) else str(seq)
            ctk.CTkLabel(scroll_frame, text=readable_action, anchor="w").grid(
                row=row, column=0, sticky="w", padx=14, pady=4
            )
            key_label = ctk.CTkLabel(
                scroll_frame,
                text=f" {readable_key} ",
                font=("Consolas", 12, "bold"),
                fg_color="#3a3a3a",
                corner_radius=4,
            )
            key_label.grid(row=row, column=1, sticky="e", padx=10, pady=4)
            row += 1

    scroll_frame.grid_columnconfigure(0, weight=1)
    scroll_frame.grid_columnconfigure(1, weight=0)

    def close_win():
        app.hotkeys_window = None
        hk_window.destroy()

    hk_window.protocol("WM_DELETE_WINDOW", close_win)
    ctk.CTkButton(hk_window, text="Close", command=close_win).pack(pady=15)


# Helper: Show menu below button
def show_menu_popup(app, menu, widget):
    x = widget.winfo_rootx()
    y = widget.winfo_rooty() + widget.winfo_height()
    menu.tk_popup(x, y)


def build_file_menu(app, *_):
    file_menu = create_menu(app, app)
    file_menu.add_command(label="Exit program", command=app.exit_program)
    return file_menu





# VIEW MENU

def build_view_menu(app):
    view_menu = create_menu(app, app)

    view_menu.add_command(label="Show Playlist", command=app.Open_playlist)
    
    app.wide_folders_check_var = tk.BooleanVar(value=(app.folder_view_mode.get() == "Wide"))
    view_menu.add_checkbutton(label="Show Wide Folders",
                              variable=app.wide_folders_check_var)

    _acc_ip = menu_accel(DEFAULT_HOTKEYS, "toggle_info_panel")
    _ip_opts = {
        "label": "Show Info Panel",
        "variable": tk.BooleanVar(value=True),
        "command": lambda: app.toggle_infopanel_menu(from_view_menu=True),
    }
    if _acc_ip:
        _ip_opts["accelerator"] = _acc_ip
    app.show_infopanel_var = _ip_opts["variable"]
    view_menu.add_checkbutton(**_ip_opts)

    _acc_tl = menu_accel(DEFAULT_HOTKEYS, "toggle_timeline")
    _tl_opts = {
        "label": "Show Timeline Widget",
        "variable": tk.BooleanVar(value=True),
        "command": lambda: app.toggle_timeline_menu(from_view_menu=True),
    }
    if _acc_tl:
        _tl_opts["accelerator"] = _acc_tl
    app.show_timeline_var = _tl_opts["variable"]
    view_menu.add_checkbutton(**_tl_opts)

    thumbnail_size_menu = create_menu(app, view_menu)
    for size in ["160x120", "240x180", "320x240", "400x300", "480x360"]:
        thumbnail_size_menu.add_radiobutton(label=size, variable=app.thumbnail_size_option, value=size,
                                            command=lambda s=size: app.change_thumbnail_size(s))
    view_menu.add_cascade(label="Thumbnail Size", menu=thumbnail_size_menu)

    wide_folder_size_menu = create_menu(app, view_menu)
    for size in [ "280x120", "320x160", "380x240", "400x320", "480x360"]:
        wide_folder_size_menu.add_radiobutton(label=size, variable=app.widefolder_size, value=size,
                                              command=lambda s=size: app.change_wide_folder_size(s))
    view_menu.add_cascade(label="Wide Folder Size", menu=wide_folder_size_menu)

    wide_folder_columns_menu = create_menu(app, view_menu)
    for num in range(1, 5):
        wide_folder_columns_menu.add_radiobutton(label=f"{num} Columns",
                                                 command=lambda n=num: app.set_wide_folder_columns(n))
    view_menu.add_cascade(label="Wide Folder Columns", menu=wide_folder_columns_menu)

    # --- "Show for files" Submenu ---
    show_for_files_menu = create_menu(app, view_menu)
    view_menu.add_cascade(label="Show for files by", menu=show_for_files_menu)
    show_for_files_menu.configure(selectcolor='white')

    # --- Checkbutton Options for File Info ---
    app.file_info_options = [
        ("All Fields", "all_fields"),
        ("Name", "name"),
        ("Path", "path"),
        ("File Size", "file_size"),
        ("Date/Time", "date_time"),
        ("Dimensions", "dimensions"),
        ("Keywords", "keywords"),
    ]
    app.file_info_vars = {}

    default_vals = {
        "name": ctk.BooleanVar(value=True),
        "file_size": ctk.BooleanVar(value=False),
        "date_time": ctk.BooleanVar(value=False),
        "dimensions": ctk.BooleanVar(value=False),
        "keywords": ctk.BooleanVar(value=True)
    }

    for label, option in app.file_info_options:
        var = default_vals.get(option, ctk.BooleanVar(value=False))
        command = app.toggle_all_fields if option == "all_fields" else app.sync_all_fields_checkbox
        show_for_files_menu.add_checkbutton(label=label, variable=var, command=command)
        app.file_info_vars[option] = var

    return view_menu



def build_edit_menu(app):
    """Build Edit menu with Search, Keyboard Shortcuts, Optimize, Preferences, Plugins."""
    edit_menu = create_menu(app, app)

    _search_opts = {"label": "Search...", "command": app.open_search_window}
    _sacc = menu_accel(DEFAULT_HOTKEYS, "search")
    if _sacc:
        _search_opts["accelerator"] = _sacc
    edit_menu.add_command(**_search_opts)
    edit_menu.add_separator()
    edit_menu.add_command(label="Keyboard Shortcuts", command=app.open_hotkeys_window)
    edit_menu.add_command(label="Optimize database", command=app.optimize_database)
    _pref_opts = {"label": "Preferences", "command": app.open_preferences_window}
    _pacc = menu_accel(DEFAULT_HOTKEYS, "open_preferences")
    if _pacc:
        _pref_opts["accelerator"] = _pacc
    edit_menu.add_command(**_pref_opts)

    # --- PLUGINS MENU ---
    plugins_menu = create_menu(app, edit_menu)
    plugins_menu.add_command(
        label="AutoTag Settings...",
        command=app.open_autotag_settings_window
    )
    edit_menu.add_cascade(label="Plugins", menu=plugins_menu)
    app.plugins_menu = plugins_menu

    return edit_menu


def build_help_menu(app):
    help_menu = create_menu(app, app)
    help_menu.add_command(label="Help", command=lambda: open_help_page(app))
    _log_acc = menu_accel(DEFAULT_HOTKEYS, "toggle_log") or "F12"
    help_menu.add_command(
        label="Show Debug Console",
        command=app.toggle_log_window,
        accelerator=_log_acc,
    )
    help_menu.add_command(label="About", command=lambda: show_about_window(app))
    return help_menu


def open_help_page(app):
    url = getattr(app, "help_url", "https://github.com/")
    try:
        webbrowser.open(url, new=2)
    except Exception as e:
        logging.info(f"[HELP] Failed to open URL: {e}")


def show_about_window(app):
    if hasattr(app, 'about_window') and app.about_window is not None and app.about_window.winfo_exists():
        app.about_window.focus()
        return

    about_window = ctk.CTkToplevel(app)
    app.about_window = about_window
    about_window.title("About Vibe Player")
    about_window.geometry("700x560")
    about_window.attributes('-topmost', True)

    def _close_about():
        app.about_window = None
        about_window.destroy()

    about_window.protocol("WM_DELETE_WINDOW", _close_about)

    text = (
        "Vibe Video Player\n"
        "Version 1.0\n\n"
        "Author: Milan Saffek\n\n"
        "Technical Background:\n"
        "This player is powered by the VLC Media Player engine (via python-vlc) "
        "and features a modern interface built with CustomTkinter.\n\n"
        "License & Open Source:\n"
        "Vibe Player is Open Source software. You can find the source code, report bugs, "
        "or download the latest updates on our GitHub repository.\n\n"
        "Support the Project:\n"
        "If you enjoy using Vibe Player, consider supporting its further development. "
        "Your contributions help keep the project alive!\n\n"
        "Special Thanks:\n"
        "A huge thanks to the open-source community and the developers of VLC and "
        "CustomTkinter. Created with passion for the community."
    )

    frame = ctk.CTkFrame(about_window)
    frame.pack(fill="both", expand=True, padx=12, pady=12)

    ctk.CTkLabel(
        frame,
        text=text,
        justify="left",
        anchor="w",
        wraplength=650
    ).pack(fill="x", padx=14, pady=(14, 10))

    btns = ctk.CTkFrame(frame)
    btns.pack(fill="x", padx=14, pady=(0, 8))

    coffee_url = getattr(app, "coffee_url", "https://buymeacoffee.com/")
    paypal_url = getattr(app, "paypal_url", "https://www.paypal.com/donate")
    github_url = getattr(app, "help_url", "https://github.com/")

    ctk.CTkButton(btns, text="Buy Me a Coffee", command=lambda: webbrowser.open(coffee_url, new=2)).pack(side="left", padx=(0, 8))
    ctk.CTkButton(btns, text="Donate via PayPal", command=lambda: webbrowser.open(paypal_url, new=2)).pack(side="left", padx=(0, 8))
    ctk.CTkButton(btns, text="Open GitHub", command=lambda: webbrowser.open(github_url, new=2)).pack(side="left")

    ctk.CTkButton(frame, text="Close", command=_close_about).pack(pady=(8, 12))


def build_edit_menuOld(app):
    """
    Legacy version of build_edit_menu without Search and Keyboard Shortcuts.
    Kept for backward compatibility.
    """
    # Create the main options menu using your custom helper function.
    options_menu = create_menu(app, app)

    # --- Main Items ---
    # Add a command to optimize the database.
    options_menu.add_command(label="Optimize database", command=app.optimize_database)
    # Add a command to open the preferences window.
    options_menu.add_command(label="Preferences", command=app.open_preferences_window)

    # --- "Show for files" Submenu ---
    # Create a submenu that will be nested within the main options menu.
    show_for_files_menu = create_menu(app, options_menu)
    # Attach the submenu to the main menu under the "Show for files" label.
    options_menu.add_cascade(label="Show for files", menu=show_for_files_menu)
    
    show_for_files_menu.configure(selectcolor='white')

    # --- Checkbutton Options for File Info ---
    # Define the labels and internal keys for the display options.
    app.file_info_options = [
        ("All Fields", "all_fields"),
        ("Name", "name"),
        ("Path", "path"),
        ("File Size", "file_size"),
        ("Date/Time", "date_time"),
        ("Dimensions", "dimensions"),
        ("Keywords", "keywords"),
    ]
    app.file_info_vars = {}

    default_vals = {
        "name": ctk.BooleanVar(value=True),
        "file_size": ctk.BooleanVar(value=False),
        "date_time": ctk.BooleanVar(value=False),
        "dimensions": ctk.BooleanVar(value=False),
        "keywords": ctk.BooleanVar(value=True)
    }

    # Loop through each option to create a checkbutton menu item.
    for label, option in app.file_info_options:
        var = default_vals.get(option, ctk.BooleanVar(value=False))
        
        if option == "all_fields":
            command = app.toggle_all_fields
        else:
            command = app.sync_all_fields_checkbox

        show_for_files_menu.add_checkbutton(label=label, variable=var, command=command)
        app.file_info_vars[option] = var

    plugins_menu = create_menu(app, options_menu)
    plugins_menu.add_command(
        label="AutoTag Settings...",
        command=app.open_autotag_settings_window
    )
    options_menu.add_cascade(label="Plugins", menu=plugins_menu)
    app.plugins_menu = plugins_menu

    return options_menu
    





# RATING MENU

def show_rating_panel_from_menu(app):
    """Same compact window as thumbnail context menu → Edit Rating."""
    thumbs = getattr(app, "selected_thumbnails", None) or []
    if not thumbs:
        messagebox.showinfo(
            "Rating",
            "Select one or more thumbnails first, then open Show rating panel again.",
            parent=app,
        )
        return
    app.edit_rating("")


def build_rating_menu(app):
    rating_menu = create_menu(app, app)

    rating_filter_menu = create_menu(app, rating_menu)
    for i in range(1, 6):
        _ro = {"label": f"Rating {i}", "command": lambda i=i: set_rating_filter(app, i)}
        _ra = menu_accel(DEFAULT_HOTKEYS, f"rate_{i}")
        if _ra:
            _ro["accelerator"] = _ra
        rating_filter_menu.add_command(**_ro)
    _r0f = {"label": "No rating", "command": lambda: set_rating_filter(app, 0)}
    _r0a = menu_accel(DEFAULT_HOTKEYS, "rate_0")
    if _r0a:
        _r0f["accelerator"] = _r0a
    rating_filter_menu.add_command(**_r0f)
    rating_menu.add_cascade(label="Rating Filter", menu=rating_filter_menu)

    edit_rating_menu = create_menu(app, rating_menu)
    for i in range(1, 6):
        _eo = {"label": f"Set Rating {i}", "command": lambda i=i: app.set_rating(i)}
        _ea = menu_accel(DEFAULT_HOTKEYS, f"rate_{i}")
        if _ea:
            _eo["accelerator"] = _ea
        edit_rating_menu.add_command(**_eo)
    rating_menu.add_cascade(label="Edit Rating", menu=edit_rating_menu)

    rating_menu.add_separator()
    rating_menu.add_command(
        label="Show rating panel…",
        command=lambda: show_rating_panel_from_menu(app),
    )

    return rating_menu


def set_rating_filter( app, rating):
    app.selected_rating = rating
    # self.refresh_thumbnails()  # Call a method to refresh the thumbnails based on the new filter
    app.update_thumbnail_info()

def update_vlc_settings(app):
        app.save_preferences()
        messagebox.showinfo("Settings Updated", "Please restart the video player for changes to take effect.")


def setup_gui(app):
    """
    Initializes and sets up the main graphical user interface components.
    This version refines the toolbar with smaller icons, increased padding for a cleaner look,
    and smaller fonts in dropdown menus for better visual hierarchy.
    """
    # FIX: changed self to app (because function argument is 'app')
    icons_dir = os.path.join(app.default_directory, "icons") 
    
    logging.debug("setup_gui: building toolbar and main layout")

    # --- Event Bindings ---
    app.bind('<Configure>', app.on_window_resize) 

    # --- UI Styling Constants ---
    DROPDOWN_FONT = ctk.CTkFont(family="Segoe UI", size=10)
    dropdown_frame_color = "#181a1d"
    dropdown_button_color = "#181a1d"
    ctkbuttons_color = "gray30"
    ctkbuttons_hover_color = "gray47"
    segmented_button_selected_color = "#3a7ebf"
    
    # --- Toolbar Frame ---
    app.toolbar_frame = ctk.CTkFrame(app)
    app.toolbar_frame.pack(side=ctk.TOP, fill=ctk.X, padx=5, pady=5)
    logging.info("[DEBUG-GUI] 'toolbar_frame' created and packed.")

    # =====================================================================
    # === LEFT-ALIGNED WIDGETS (Navigation and Filtering) ===
    # =====================================================================

    # --- Quick Access ComboBox ---
    app.quick_access_combo = ctk.CTkComboBox(
        app.toolbar_frame,
        values=app.recent_directories,
        width=300,
        command=app.quick_access_selected,
        dropdown_font=DROPDOWN_FONT,
        fg_color=dropdown_frame_color,
        border_color=dropdown_frame_color,
        button_color=dropdown_button_color
    )
    app.quick_access_combo.pack(side=ctk.LEFT, padx=(5, 10), pady=8)
    app.quick_access_combo.bind("<Return>", app.quick_access_selected)

    # --- Parent Directory Button ---
    try:
        folder_image = Image.open(os.path.join(icons_dir, "folder.png")) 
        app.folder_icon = ctk.CTkImage(light_image=folder_image, size=(18, 18))
        app.parent_dir_button = ctk.CTkButton(
            app.toolbar_frame,
            image=app.folder_icon,
            command=app.go_to_parent_directory,
            width=26, height=26, text="",
            fg_color=ctkbuttons_color,
            hover_color=ctkbuttons_hover_color
        )
        app.parent_dir_button.pack(side=ctk.LEFT, padx=4, pady=5)
    except Exception as e:
        logging.error(f"[GUI-ERROR] Failed to load 'folder.png': {e}")
        app.parent_dir_button = ctk.CTkButton(app.toolbar_frame, text="Up", command=app.go_to_parent_directory, width=35)
        app.parent_dir_button.pack(side=ctk.LEFT, padx=4, pady=5)

    # --- Sort Dropdown ---
    app.sort_option = ctk.StringVar(value="Filename")
    sort_dropdown = ctk.CTkComboBox(
        app.toolbar_frame,
        variable=app.sort_option,
        values=["Filename", "Size", "Date", "Dimensions", "File Type"],
        command=lambda choice: (
            app._toolbar_combo_begin(),
            app.display_thumbnails(app.current_directory, preserve_scroll=True),
        ),
        width=110,
        dropdown_font=DROPDOWN_FONT,
        fg_color=dropdown_frame_color,
        border_color=dropdown_frame_color,
        button_color=dropdown_button_color
    )
    sort_dropdown.pack(side=ctk.LEFT, padx=(45, 6), pady=5)

    # --- Thumbnail Size Dropdown ---
    thumbnail_size_choices = ["160x120", "240x180", "320x240", "400x300", "480x360"]
    default_thumb_size_str = f"{app.thumbnail_size[0]}x{app.thumbnail_size[1]}"
    if default_thumb_size_str not in thumbnail_size_choices:
        default_thumb_size_str = "320x240"
    app.thumbnail_size_option = ctk.StringVar(value=default_thumb_size_str)

    app.thumbnail_size_menu = ctk.CTkComboBox(
        app.toolbar_frame,
        values=thumbnail_size_choices,
        command=app.change_both_thumbnail_sizes,
        variable=app.thumbnail_size_option,
        width=100,
        dropdown_font=DROPDOWN_FONT,
        fg_color=dropdown_frame_color,
        border_color=dropdown_frame_color,
        button_color=dropdown_button_color
    )
    app.thumbnail_size_menu.set(app.thumbnail_size_option.get())
    app.thumbnail_size_menu.pack(side=ctk.LEFT, padx=6, pady=5)

    # --- Filter Dropdown ---
    app.filter_option = ctk.StringVar(value="Both")
    filter_dropdown = ctk.CTkComboBox(
        app.toolbar_frame,
        variable=app.filter_option,
        values=["Images", "Videos", "Both"],
        command=lambda choice: (
            app._toolbar_combo_begin(),
            app.display_thumbnails(app.current_directory, preserve_scroll=True),
        ),
        width=90,
        dropdown_font=DROPDOWN_FONT,
        fg_color=dropdown_frame_color,
        border_color=dropdown_frame_color,
        button_color=dropdown_button_color
    )
    filter_dropdown.pack(side=ctk.LEFT, padx=6, pady=5)
    
    # --- Folders View Mode ---
    app.folder_view_segmented_button = ctk.CTkSegmentedButton(
        app.toolbar_frame,
        values=["Standard", "Wide"],
        variable=app.folder_view_mode,
        height=24,
        font=DROPDOWN_FONT,
        fg_color=ctkbuttons_color,
        selected_color=segmented_button_selected_color,
        selected_hover_color="#4a9de8"
    )
    app.folder_view_segmented_button.pack(side=ctk.LEFT, padx=6, pady=4)
    
    # =====================================================================
    # === RATING WIDGET ===
    # =====================================================================
    rating_section_frame = ctk.CTkFrame(app.toolbar_frame, fg_color="transparent")
    rating_section_frame.pack(side=ctk.LEFT, padx=(50, 0), pady=5)

    rating_label = ctk.CTkLabel(
        rating_section_frame,
        text="Rating:",
        font=ctk.CTkFont(family="Segoe UI", size=11),
        text_color="#8A8A8A"
    )
    rating_label.pack(side=ctk.LEFT, padx=(0, 10))

    rating_colors = ["#5a7e8c", "#4a754a", "#b3a369", "#754a75", "#a65f5f"]
    app.rating_buttons = []

    for i in range(1, 6):
        button_color = rating_colors[i-1]
        rating_button = ctk.CTkButton(
            rating_section_frame,
            width=24, height=24, corner_radius=6, text=str(i),
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            fg_color="transparent", border_width=1, border_color=button_color,
            hover_color=button_color,
            command=lambda rating_value=i: app.set_rating(rating_value)
        )
        rating_button.pack(side=ctk.LEFT, padx=4)
        app.rating_buttons.append(rating_button)

    remove_rating_button = ctk.CTkButton(
        rating_section_frame,
        text="🗑️",
        font=ctk.CTkFont(family="Segoe UI", size=12),
        text_color="#8A8A8A",
        width=24, height=24,
        fg_color="transparent",
        hover_color="#404040",
        command=lambda: app.set_rating(0)
    )
    remove_rating_button.pack(side=ctk.LEFT, padx=(10, 0))

    # =====================================================================
    # === RIGHT-ALIGNED WIDGETS (Actions) ===
    # =====================================================================

    # --- Settings Button ---
    try:
        settings_image = Image.open(os.path.join(icons_dir, "settings.png"))
        app.settings_icon = ctk.CTkImage(light_image=settings_image, size=(18, 18))
        app.settings_button = ctk.CTkButton(
            app.toolbar_frame,
            image=app.settings_icon,
            command=app.open_preferences_window,
            width=26, height=26, text="",
            fg_color=ctkbuttons_color,
            hover_color=ctkbuttons_hover_color
        )
        app.settings_button.pack(side=ctk.RIGHT, padx=(4, 5), pady=5)
    except Exception as e:
        logging.error(f"[GUI-ERROR] Failed to load 'settings.png': {e}")
        app.settings_button = ctk.CTkButton(app.toolbar_frame, text="Settings", command=app.open_preferences_window, width=70)
        app.settings_button.pack(side=ctk.RIGHT, padx=(4, 5), pady=5)

    # --- Playlist Button ---
    try:
        playlist_image = Image.open(os.path.join(icons_dir, "playlist_ico.png")) 
        app.playlist_icon = ctk.CTkImage(light_image=playlist_image, size=(18, 18))
        app.playlist_button = ctk.CTkButton(
            app.toolbar_frame,
            image=app.playlist_icon,
            command=app.Open_playlist,
            width=26, height=26, text="",
            fg_color=ctkbuttons_color,
            hover_color=ctkbuttons_hover_color
        )
        app.playlist_button.pack(side=ctk.RIGHT, padx=4, pady=5)
    except Exception as e:
        logging.error(f"[GUI-ERROR] Failed to load 'playlist_ico.png': {e}")
        app.playlist_button = ctk.CTkButton(app.toolbar_frame, text="Playlist", command=app.Open_playlist, width=70)
        app.playlist_button.pack(side=ctk.RIGHT, padx=4, pady=5)

    # --- Search Button ---
    try:
        search_image = Image.open(os.path.join(icons_dir, "zoom.png"))
        app.search_icon = ctk.CTkImage(light_image=search_image, size=(18, 18))
        app.search_button = ctk.CTkButton(
            app.toolbar_frame,
            image=app.search_icon,
            command=app.open_search_window,
            width=26, height=26, text="",
            fg_color=ctkbuttons_color,
            hover_color=ctkbuttons_hover_color
        )
        app.search_button.pack(side=ctk.RIGHT, padx=(15, 4), pady=5)
    except Exception as e:
        logging.error(f"[GUI-ERROR] Failed to load 'zoom.png': {e}")
        app.search_button = ctk.CTkButton(app.toolbar_frame, text="Search", command=app.open_search_window, width=70)
        app.search_button.pack(side=ctk.RIGHT, padx=(15, 4), pady=5)

    logging.info("--- [DEBUG-GUI] setup_gui function completed successfully. ---")





def create_preferences_window(app):
    
    # --- FIX 1: Singleton Window Check ---
    # Check if the attribute exists, AND it's not None, AND the window widget still exists
    if hasattr(app, 'preferences_window') and app.preferences_window is not None and app.preferences_window.winfo_exists():
        app.preferences_window.focus()  # If it exists, bring it to the front
        return  # And exit the function, don't create a new one
    
    preferences_window = ctk.CTkToplevel(app)
    app.preferences_window = preferences_window  # Store the reference in the main app object
    preferences_window.title("Preferences")
    preferences_window.geometry("500x600")
    preferences_window.attributes('-topmost', True) 

    # --- FIX 1: Close Handler (for 'X' button and Save button) ---
    def on_close():
        # Function to be called when the window is closed
        # This clears the reference in the main app, allowing the window to be reopened
        app.preferences_window = None  # Clear the reference
        preferences_window.destroy()   # Destroy the Tkinter window
    # Bind the custom close function to the window's 'X' button (close protocol)
    preferences_window.protocol("WM_DELETE_WINDOW", on_close)

    canvas = ctk.CTkCanvas(preferences_window)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    scrollbar = ctk.CTkScrollbar(preferences_window, orientation=tk.VERTICAL, command=canvas.yview)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.configure(yscrollcommand=scrollbar.set)

    content_frame = ctk.CTkFrame(canvas)
    content_frame_id = canvas.create_window((0, 0), window=content_frame, anchor="nw")

    def on_configure(event):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfig(content_frame_id, width=canvas.winfo_width())

    content_frame.bind("<Configure>", on_configure)
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(content_frame_id, width=e.width))

    if hasattr(app, "ensure_audio_devices_loaded"):
        app.ensure_audio_devices_loaded()
    audio_devices = getattr(app, "audio_devices", [])
    audio_device_options = [f"{d['name']} ({d['index']})" for d in audio_devices] if audio_devices else ["No devices found"]

    # === VIDEO OPTIONS ===
    video_options_frame = ctk.CTkFrame(content_frame)
    video_options_frame.pack(fill="both", expand=True, padx=10, pady=10)
    ctk.CTkLabel(video_options_frame, text="Video Options", font=("Helvetica", 16)).pack(anchor="w", padx=5, pady=5)

    hud_enabled_var = ctk.BooleanVar(value=getattr(app, "video_show_hud", True))
    ctk.CTkCheckBox(
        video_options_frame, 
        text="Enable Video HUD Info (Overlay text)", 
        variable=hud_enabled_var
    ).pack(anchor="w", padx=10, pady=5)

    gpu_upscale_var = ctk.BooleanVar(value=getattr(app, "gpu_upscale", False))
    ctk.CTkCheckBox(
        video_options_frame,
        text="Enable GPU Upscaling (RTX / FSR)",
        variable=gpu_upscale_var,
        command=lambda: setattr(app, "gpu_upscale", gpu_upscale_var.get())
    ).pack(anchor="w", padx=10, pady=5)

    video_output_frame = ctk.CTkFrame(video_options_frame)
    video_output_frame.pack(fill="both", expand=True, padx=10, pady=10)
    video_output_var = ctk.StringVar(value=app.video_output_var.get())
    for option in ['direct3d11', 'direct3d9', 'glwin32', 'vmem']:
        ctk.CTkRadioButton(
            video_output_frame,
            text=option,
            variable=video_output_var,
            value=option,
            command=lambda: app.video_output_var.set(video_output_var.get())
        ).pack(anchor="w", padx=5)

    capture_method_frame = ctk.CTkFrame(video_options_frame)
    capture_method_frame.pack(fill="both", expand=True, padx=10, pady=10)
    capture_method_var = ctk.StringVar(value=app.capture_method_var.get())
    for option in ['Imageio', 'OpenCV', 'FFmpeg']:
        ctk.CTkRadioButton(
            capture_method_frame,
            text=option,
            variable=capture_method_var,
            value=option,
            command=lambda: app.capture_method_var.set(capture_method_var.get())
        ).pack(anchor="w", padx=5)

    # === VLC VIDEO FILTERS ===
    vlc_filters_frame = ctk.CTkFrame(content_frame)
    vlc_filters_frame.pack(fill="both", expand=True, padx=10, pady=10)
    ctk.CTkLabel(vlc_filters_frame, text="VLC Video Filters", font=("Helvetica", 16)).pack(anchor="w", padx=5, pady=5)

    vlc_postproc_var = ctk.BooleanVar(value=getattr(app, "vlc_enable_postproc", False))
    ctk.CTkCheckBox(
        vlc_filters_frame,
        text="Post-processing",
        variable=vlc_postproc_var
    ).pack(anchor="w", padx=10, pady=5)

    postproc_quality_frame = ctk.CTkFrame(vlc_filters_frame)
    postproc_quality_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    ctk.CTkLabel(postproc_quality_frame, text="Post-processing quality (0-6)").pack(anchor="w", padx=5)
    vlc_postproc_quality_var = ctk.IntVar(value=int(getattr(app, "vlc_postproc_quality", 6)))
    ctk.CTkSlider(
        postproc_quality_frame,
        from_=0,
        to=6,
        number_of_steps=6,
        variable=vlc_postproc_quality_var
    ).pack(fill="both", expand=True, padx=5, pady=5)

    vlc_gradfun_var = ctk.BooleanVar(value=getattr(app, "vlc_enable_gradfun", False))
    ctk.CTkCheckBox(
        vlc_filters_frame,
        text="Debanding (smooth color gradients)",
        variable=vlc_gradfun_var
    ).pack(anchor="w", padx=10, pady=5)

    vlc_deinterlace_var = ctk.BooleanVar(value=getattr(app, "vlc_enable_deinterlace", False))
    ctk.CTkCheckBox(
        vlc_filters_frame,
        text="Deinterlace (for interlaced/TV sources)",
        variable=vlc_deinterlace_var
    ).pack(anchor="w", padx=10, pady=5)

    vlc_skiploopfilter_var = ctk.BooleanVar(value=getattr(app, "vlc_skiploopfilter_disable", False))
    ctk.CTkCheckBox(
        vlc_filters_frame,
        text="Improve decode quality (disable loop-filter skipping)",
        variable=vlc_skiploopfilter_var
    ).pack(anchor="w", padx=10, pady=5)

    # === AUDIO OPTIONS ===
    audio_options_frame = ctk.CTkFrame(content_frame)
    audio_options_frame.pack(fill="both", expand=True, padx=10, pady=10)
    ctk.CTkLabel(audio_options_frame, text="Audio Options", font=("Helvetica", 16)).pack(anchor="w", padx=5, pady=5)

    audio_output_frame = ctk.CTkFrame(audio_options_frame)
    audio_output_frame.pack(fill="both", expand=True, padx=10, pady=10)
    audio_output_var = ctk.StringVar(value=app.audio_output_var.get())
    for option in ['directsound', 'waveout', 'alsa']:
        ctk.CTkRadioButton(
            audio_output_frame,
            text=option,
            variable=audio_output_var,
            value=option,
            command=lambda: app.audio_output_var.set(audio_output_var.get())
        ).pack(anchor="w", padx=5)

    hardware_decoding_frame = ctk.CTkFrame(audio_options_frame)
    hardware_decoding_frame.pack(fill="both", expand=True, padx=10, pady=10)
    hardware_decoding_var = ctk.StringVar(value=app.hardware_decoding_var.get())
    for option in ['none', 'dxva2', 'd3d11va', 'cuda']:
        ctk.CTkRadioButton(
            hardware_decoding_frame,
            text=option,
            variable=hardware_decoding_var,
            value=option,
            command=lambda: app.hardware_decoding_var.set(hardware_decoding_var.get())
        ).pack(anchor="w", padx=5)

    audio_device_frame = ctk.CTkFrame(audio_options_frame)
    audio_device_frame.pack(fill="both", expand=True, padx=10, pady=10)
    ctk.CTkLabel(audio_device_frame, text="Audio Device").pack(anchor="w", padx=5)
    audio_device_var = ctk.StringVar(value=app.audio_device_var.get())
    audio_device_dropdown = ctk.CTkOptionMenu(
        audio_device_frame,
        variable=audio_device_var,
        values=audio_device_options
    )
    audio_device_dropdown.pack(fill="both", expand=True, padx=5, pady=5)

    # === THUMBNAIL OPTIONS ===
    thumbnail_options_frame = ctk.CTkFrame(content_frame)
    thumbnail_options_frame.pack(fill="both", expand=True, padx=10, pady=10)
    ctk.CTkLabel(thumbnail_options_frame, text="Thumbnail Options", font=("Helvetica", 16)).pack(anchor="w", padx=5, pady=5)

    thumbnail_format_frame = ctk.CTkFrame(thumbnail_options_frame)
    thumbnail_format_frame.pack(fill="both", expand=True, padx=10, pady=10)
    thumbnail_format_var = ctk.StringVar(value=app.thumbnail_format)
    for option in ['PNG', 'JPG']:
        ctk.CTkRadioButton(thumbnail_format_frame, text=option, variable=thumbnail_format_var, value=option.lower()).pack(anchor="w", padx=5)

    thumbnail_size_frame = ctk.CTkFrame(thumbnail_options_frame)
    thumbnail_size_frame.pack(fill="both", expand=True, padx=10, pady=10)
    thumbnail_size_var = ctk.StringVar(value=f"{app.thumbnail_size[0]}x{app.thumbnail_size[1]}")
    thumbnail_size_dropdown = ctk.CTkOptionMenu(thumbnail_size_frame, variable=thumbnail_size_var, values=["160x120", "240x180", "320x240", "400x300", "460x320"])
    thumbnail_size_dropdown.pack(fill="both", expand=True, padx=5, pady=5)

    thumbnail_time_frame = ctk.CTkFrame(thumbnail_options_frame)
    thumbnail_time_frame.pack(fill="both", expand=True, padx=10, pady=10)
    ctk.CTkLabel(thumbnail_time_frame, text="Thumbnail Time (in % of video duration)").pack(anchor="w", padx=5)
    # thumbnail_time_var = tk.IntVar(value=int(app.thumbnail_time * 100))
    # thumbnail_time_slider = ctk.CTkSlider(thumbnail_time_frame, from_=0, to=100, variable=thumbnail_time_var)
    # thumbnail_time_slider.pack(fill="both", expand=True, padx=5, pady=5)
    # Prefer app.thumbnail_time_var over a new local
    thumbnail_time_slider = ctk.CTkSlider(
        thumbnail_time_frame,
        from_=0,
        to=100,
        variable=app.thumbnail_time_var
    )
    thumbnail_time_slider.pack(fill="both", expand=True, padx=5, pady=5)

    # === CACHE PATH ===
    cache_path_frame = ctk.CTkFrame(thumbnail_options_frame)
    cache_path_frame.pack(fill="both", expand=True, padx=10, pady=10)
    cache_path_var = ctk.StringVar(value=app.thumbnail_cache_path)
    cache_path_entry = ctk.CTkEntry(cache_path_frame, textvariable=cache_path_var)
    cache_path_entry.pack(side="left", fill="both", expand=True, padx=5)
    ctk.CTkButton(cache_path_frame, text="Browse", command=lambda: browse_cache_path(app, cache_path_var)).pack(side="left", padx=5)

    # === IMAGE VIEWER ===
    image_viewer_frame = ctk.CTkFrame(content_frame)
    image_viewer_frame.pack(fill="x", padx=10, pady=10)
    ctk.CTkLabel(
        image_viewer_frame,
        text="Image viewer",
        font=("Helvetica", 16),
    ).pack(anchor="w", padx=5, pady=(4, 6))

    image_viewer_pyglet_var = ctk.BooleanVar(
        value=getattr(app, "image_viewer_use_pyglet", False)
    )
    switch_row = ctk.CTkFrame(image_viewer_frame, fg_color="transparent")
    switch_row.pack(anchor="w", fill="x", padx=10, pady=(0, 4))

    ctk.CTkLabel(
        switch_row,
        text="GPU Accelerated Viewer (Experimental)",
        font=("Helvetica", 13),
        text_color=("gray20", "gray80"),
    ).pack(side="left", padx=(0, 16))
    ctk.CTkSwitch(
        switch_row,
        text="",
        variable=image_viewer_pyglet_var,
        onvalue=True,
        offvalue=False,
    ).pack(side="left")

    ctk.CTkLabel(
        image_viewer_frame,
        text="Warning: May cause freezes on laptops with hybrid graphics.",
        font=("Helvetica", 11),
        text_color=("gray35", "gray65"),
        wraplength=520,
        justify="left",
    ).pack(anchor="w", padx=10, pady=(2, 6))

    # === GENERAL OPTIONS ===
    general_options_frame = ctk.CTkFrame(content_frame)
    general_options_frame.pack(fill="both", expand=True, padx=10, pady=10)
    ctk.CTkLabel(general_options_frame, text="General Options", font=("Helvetica", 16)).pack(anchor="w", padx=5, pady=5)

    memory_cache_var = ctk.BooleanVar(value=app.memory_cache)
    memory_cache_checkbox = ctk.CTkCheckBox(general_options_frame, text="Enable Memory Cache", variable=memory_cache_var)
    memory_cache_checkbox.pack(anchor="w", padx=10, pady=5)

    auto_play_var = tk.BooleanVar(value=app.auto_play)
    ctk.CTkCheckBox(general_options_frame, text="Start Play Video Automatically", variable=auto_play_var).pack(anchor="w", padx=5, pady=5)
    play_broken_videos_var = ctk.BooleanVar(
        value=bool(getattr(app, "play_broken_videos", True))
    )
    ctk.CTkCheckBox(
        general_options_frame,
        text="Play broken videos (if possible)",
        variable=play_broken_videos_var,
    ).pack(anchor="w", padx=5, pady=5)
    
    
    # Player Interface Settings

    interface_frame = ctk.CTkFrame(content_frame)
    interface_frame.pack(fill="x", pady=10)

    ctk.CTkLabel(interface_frame, text="Player Interface Settings", font=("Helvetica", 16)).pack(anchor="w", padx=10)

    # Tree font label + slider
    ctk.CTkLabel(interface_frame, text="Left panel font size").pack(anchor="w", padx=10, pady=(10, 0))
    tree_slider = ctk.CTkSlider(interface_frame, from_=10, to=30, number_of_steps=20)
    tree_slider.set(app.base_font_size)
    tree_slider.configure(command=lambda val: app.set_tree_font_size(int(float(val))))
    tree_slider.pack(fill="x", padx=10)

    # Thumb font label + slider
    ctk.CTkLabel(interface_frame, text="Right panel font size").pack(anchor="w", padx=10, pady=(10, 0))
    thumb_slider = ctk.CTkSlider(interface_frame, from_=8, to=20, number_of_steps=12)
    thumb_slider.set(app.thumbFontSize)
    thumb_slider.configure(command=lambda val: app.set_thumb_font_size(int(float(val))))
    thumb_slider.pack(fill="x", padx=10)

    # === FILE OPERATIONS (collapsible) ===
    advanced_outer = ctk.CTkFrame(content_frame)
    advanced_outer.pack(fill="x", padx=10, pady=(5, 5))
    adv_open = {"v": False}
    adv_body = ctk.CTkFrame(advanced_outer)

    dnd_confirm_var = ctk.BooleanVar(value=getattr(app, "dnd_confirm_dialogs", False))
    delete_to_trash_var = ctk.BooleanVar(
        value=bool(getattr(app, "delete_to_trash", True))
    )

    def _toggle_advanced():
        adv_open["v"] = not adv_open["v"]
        if adv_open["v"]:
            adv_body.pack(fill="x", padx=0, pady=(4, 0))
            adv_toggle_btn.configure(text="File operations  ▼")
        else:
            adv_body.pack_forget()
            adv_toggle_btn.configure(text="File operations  ▶")

    adv_toggle_btn = ctk.CTkButton(
        advanced_outer,
        text="File operations  ▶",
        command=_toggle_advanced,
        fg_color=("gray75", "gray25"),
        anchor="w",
        height=32,
    )
    adv_toggle_btn.pack(fill="x")

    ctk.CTkLabel(
        adv_body,
        text="Delete behavior",
        font=("Helvetica", 14),
    ).pack(anchor="w", padx=8, pady=(4, 2))
    ctk.CTkCheckBox(
        adv_body,
        text="Move deleted items to Recycle Bin (recommended)",
        variable=delete_to_trash_var,
    ).pack(anchor="w", padx=12, pady=4)

    ctk.CTkLabel(
        adv_body,
        text="Drag and drop",
        font=("Helvetica", 14),
    ).pack(anchor="w", padx=8, pady=(4, 2))
    ctk.CTkCheckBox(
        adv_body,
        text="Confirm before each drag-and-drop copy/move",
        variable=dnd_confirm_var,
    ).pack(anchor="w", padx=12, pady=4)
    ctk.CTkLabel(
        adv_body,
        text=(
            "Internal (thumbnails / folder tree): no modifier = Move, Ctrl = Copy.\n"
            "From Windows Explorer: no modifier = Copy, Shift = Move."
        ),
        font=("Helvetica", 12),
        text_color=("gray30", "gray70"),
        justify="left",
        anchor="w",
    ).pack(anchor="w", padx=12, pady=(0, 8))

    def save_and_close_action():
        app.dnd_confirm_dialogs = dnd_confirm_var.get()
        app.delete_to_trash = bool(delete_to_trash_var.get())
        app.play_broken_videos = bool(play_broken_videos_var.get())
        if hasattr(app, "play_broken_videos_var"):
            app.play_broken_videos_var.set(app.play_broken_videos)
        app.image_viewer_use_pyglet = image_viewer_pyglet_var.get()
        app.video_show_hud = hud_enabled_var.get()
        app.gpu_upscale = gpu_upscale_var.get()
        app.vlc_enable_postproc = vlc_postproc_var.get()
        app.vlc_postproc_quality = int(vlc_postproc_quality_var.get())
        app.vlc_postproc_quality = max(0, min(6, app.vlc_postproc_quality))
        app.vlc_enable_gradfun = vlc_gradfun_var.get()
        app.vlc_enable_deinterlace = vlc_deinterlace_var.get()
        app.vlc_skiploopfilter_disable = vlc_skiploopfilter_var.get()
        # 1. Call the original save_preferences function (defined elsewhere)
        save_preferences(
            app,
            thumbnail_format_var.get(),
            cache_path_var.get(),
            auto_play_var.get(),
            memory_cache_var.get(),
            capture_method_var.get(),
            video_output_var.get(),
            audio_output_var.get(),
            hardware_decoding_var.get(),
            audio_device_var.get(),
            thumbnail_size_var.get(),
            app.thumbnail_time_var.get() / 100.0  # Pass the slider value correctly
        )
        
        on_close()        

    # === SAVE BUTTONS ===
    button_frame = ctk.CTkFrame(content_frame)
    button_frame.pack(fill="both", expand=True, padx=10, pady=10)
    ctk.CTkButton(button_frame, text="Clear Cache", command=lambda: clear_cache(app)).pack(fill="both", expand=True, padx=5, pady=5)
    ctk.CTkButton(button_frame, text="Open Cache Folder", command=lambda: open_cache_folder(app)).pack(fill="both", expand=True, padx=5, pady=5)
    ctk.CTkButton(
        button_frame,
        text="Save and Close",
        command=save_and_close_action
    ).pack(fill="both", expand=True, padx=5, pady=5)






def list_audio_devices():
    """Query and return list of audio device names. Requires sounddevice (sd)."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        device_list = [device['name'] for device in devices]
        return device_list
    except ImportError:
        return []



def save_preferences(app,thumbnail_format,cache_path,auto_play,memory_cache,capture_method,video_output,audio_output,hardware_decoding,audio_device,thumbnail_size,thumbnail_time):
    selected_audio_device = app.audio_device_var.get()
    audio_device_id = selected_audio_device.split('(')[-1].strip(')')

    preferences = {
        "capture_method": app.capture_method_var.get(),
        "thumbnail_size": f"{app.thumbnail_size[0]}x{app.thumbnail_size[1]}",
        "thumbnail_format": thumbnail_format,
        "thumbnail_cache_path": cache_path,
        "memory_cache": memory_cache,
        "auto_play": auto_play,
        "video_output": app.video_output_var.get(),
        "audio_output": app.audio_output_var.get(),
        "hardware_decoding": app.hardware_decoding_var.get(),
        "audio_device": audio_device_id,  # Save device ID
        "thumbnail_time": app.thumbnail_time_var.get() / 100,  # Store as a percentage (0.0 to 1.0)
        "numwidefolders_in_col": app.numwidefolders_in_col,
        "wide_folders_check_var": app.wide_folders_check_var.get(),
        "widefolder_size": f"{app.widefolder_size[0]}x{app.widefolder_size[1]}",
        "thumb_font_size": app.thumbFontSize,
        "tree_font_size": app.base_font_size,
        "info_panel_expanded": app.info_panel_container.expanded if app.info_panel_container else True,
        "timeline_widget_expanded": app.timeline_container.expanded if app.timeline_container else True,
        "video_show_hud": getattr(app, "video_show_hud", True),
        "gpu_upscale": getattr(app, "gpu_upscale", False),
        "vlc_enable_postproc": getattr(app, "vlc_enable_postproc", False),
        "vlc_postproc_quality": int(getattr(app, "vlc_postproc_quality", 6)),
        "vlc_enable_gradfun": getattr(app, "vlc_enable_gradfun", False),
        "vlc_enable_deinterlace": getattr(app, "vlc_enable_deinterlace", False),
        "vlc_skiploopfilter_disable": getattr(app, "vlc_skiploopfilter_disable", False),
        "preview_auto_play": (
            app.info_panel.preview_auto_play_var.get()
            if getattr(app, "info_panel", None) and hasattr(app.info_panel, "preview_auto_play_var")
            else True
        ),
        "play_broken_videos": bool(getattr(app, "play_broken_videos", True)),
        "timeline_strip_count": getattr(app, "timeline_strip_count", 20),
        "multiTimeline_limit": (
            app.info_panel.multiTimeline_limit_var.get()
            if getattr(app, "info_panel", None) and hasattr(app.info_panel, "multiTimeline_limit_var")
            else True
        ),
        "dnd_confirm_dialogs": getattr(app, "dnd_confirm_dialogs", False),
        "delete_to_trash": bool(getattr(app, "delete_to_trash", True)),
        "image_viewer_use_pyglet": bool(getattr(app, "image_viewer_use_pyglet", False)),
    }
    # Save splitter positions (fractions 0-1) when panes are visible
    try:
        if hasattr(app, "paned_window") and app.paned_window.winfo_exists():
            pw = app.paned_window.winfo_width()
            if pw > 10:
                coord = app.paned_window.sash_coord(0)
                if coord:
                    preferences["splitter_main_fraction"] = coord[0] / pw
                    logging.info(f"[SPLITTER SAVE] main: coord={coord}, width={pw} -> fraction={preferences['splitter_main_fraction']:.4f}")
        if hasattr(app, "left_split") and app.left_split.winfo_exists() and len(app.left_split.panes()) > 1:
            lh = app.left_split.winfo_height()
            if lh > 10:
                coord = app.left_split.sash_coord(0)
                if coord:
                    preferences["splitter_left_fraction"] = coord[1] / lh
                    logging.info(f"[SPLITTER SAVE] left: coord={coord}, height={lh} -> fraction={preferences['splitter_left_fraction']:.4f}")
        if hasattr(app, "right_split") and app.right_split.winfo_exists() and len(app.right_split.panes()) > 1:
            rh = app.right_split.winfo_height()
            if rh > 10:
                coord = app.right_split.sash_coord(0)
                if coord:
                    preferences["splitter_right_fraction"] = coord[1] / rh
                    logging.info(f"[SPLITTER SAVE] right: coord={coord}, height={rh} -> fraction={preferences['splitter_right_fraction']:.4f}")
    except Exception as e:
        logging.info(f"[SPLITTER SAVE] Exception: {e}")

    # Ensure settings are handled as a dictionary
    if os.path.exists("settings.json"):
        with open("settings.json", "r") as pref_file:
            settings = json.load(pref_file)
            if not isinstance(settings, dict):
                settings = {}
    else:
        settings = {}

    # Preserve splitter values if we couldn't read them (e.g. during load, layout not ready)
    for key in ("splitter_main_fraction", "splitter_left_fraction", "splitter_right_fraction"):
        if key not in preferences and key in settings:
            preferences[key] = settings[key]

    logging.info(f"Saving preferences: {preferences}")  # Debug statement

    # Update the preferences in the settings dictionary
    settings.update(preferences)
    settings.pop("force_gpu_on_windows", None)  # legacy key no longer used

    # Save the updated settings
    with open("settings.json", "w") as pref_file:
        json.dump(settings, pref_file)

    # Update the app attributes with the saved preferences
    app.thumbnail_format = preferences["thumbnail_format"]
    # app.thumbnail_size = preferences["thumbnail_size"]
    app.capture_method = preferences["capture_method"]
    app.thumbnail_size = app.parse_thumbnail_size(preferences["thumbnail_size"])  # Parse back to tuple
    app.thumbnail_cache_path = preferences["thumbnail_cache_path"]
    app.auto_play = preferences["auto_play"]
    app.video_output = preferences["video_output"]
    app.audio_output = preferences["audio_output"]
    app.hardware_decoding = preferences["hardware_decoding"]
    app.audio_device = preferences["audio_device"]  # Update audio_device in app
    app.numwidefolders_in_col = preferences["numwidefolders_in_col"]
    app.wide_folders_check_var.set(preferences["wide_folders_check_var"])
    app.widefolder_size = app.parse_thumbnail_size(preferences["widefolder_size"])  # Parse new tuple
    app.thumbnail_time = preferences["thumbnail_time"]
    app.play_broken_videos = bool(preferences.get("play_broken_videos", True))
    if hasattr(app, "play_broken_videos_var"):
        app.play_broken_videos_var.set(app.play_broken_videos)

    app.dnd_confirm_dialogs = bool(preferences.get("dnd_confirm_dialogs", False))
    app.delete_to_trash = bool(preferences.get("delete_to_trash", True))
    app.image_viewer_use_pyglet = bool(
        preferences.get("image_viewer_use_pyglet", False)
    )
    app.apply_preferences()
    logging.info(f"Preferences saved: {preferences}")




def browse_cache_path(app, cache_path_var):
    selected_path = filedialog.askdirectory()
    if selected_path:
        cache_path_var.set(selected_path)
        app.thumbnail_cache_path = selected_path
        app.save_preferences()
        logging.info(f"Cache path set: {cache_path_var.get()}")

def clear_cache(app):
    cache_dir = app.thumbnail_cache_path
    if os.path.exists(cache_dir):
        shutil.rmtree(cache_dir)
        os.makedirs(cache_dir)
        messagebox.showinfo("Cache Cleared", "Thumbnail cache cleared successfully.")
    else:
        messagebox.showinfo("Cache Folder Not Found", "The thumbnail cache folder does not exist.")

def open_cache_folder(app):
    cache_dir = app.thumbnail_cache_path
    if os.path.exists(cache_dir):
        os.startfile(cache_dir)
    else:
        messagebox.showinfo("Cache Folder Not Found", "The thumbnail cache folder does not exist.")


def perform_search(app, search_param, keyword, operator):
    if not keyword.strip():  # Check if the keyword is empty or just spaces
        logging.info("Empty search keyword. Please provide a valid keyword.")
        return

    # Validate operator to ensure it's one of the allowed ones
    valid_operators = ['<', '<=', '>', '>=', 'AND', 'OR']
    if operator not in valid_operators:
        logging.info(f"Invalid operator: {operator}. Expected one of {valid_operators}")
        return

    # If operator is AND/OR, assume a non-comparison search
    if operator in ['AND', 'OR']:
        logging.info(f"Performing non-comparison search with AND/OR operator: {operator}")
        app.search_database(search_param, keyword, operator)
        
    else:
        logging.info(f"Performing comparison search with operator: {operator}")
        app.search_database(search_param, keyword, "AND", operator)  # Default AND for comparisons

    
    

def create_search_window(app):
    search_window = ctk.CTkToplevel(app)
    search_window.title("Search")
    
    _sw, _sh = 760, 500
    if hasattr(app, "_center_toplevel_window"):
        app._center_toplevel_window(search_window, _sw, _sh)
    else:
        search_window.geometry(f"{_sw}x{_sh}")
    search_window.attributes('-topmost', True) 
    # Add a frame for the search parameter selection
    search_frame = ctk.CTkFrame(search_window)
    search_frame.pack(side=ctk.TOP, fill=ctk.X, padx=10, pady=5)

    # Add a label for the search parameter
    search_label = ctk.CTkLabel(search_frame, text="Search by:")
    search_label.pack(side=ctk.LEFT, padx=(0, 10))

    # Extract the search values from file_info_options
    search_values = [option[1] for option in app.file_info_options] + ['rating']  # Add 'rating' explicitly
    search_param_combobox = ctk.CTkComboBox(search_frame, values=search_values, width=140)
    search_param_combobox.set('all_fields')  # Default to "All Fields"
    search_param_combobox.pack(side=ctk.LEFT, padx=(0, 10))

    # Add the search entry
    search_entry = ctk.CTkEntry(search_frame, placeholder_text="Enter keyword or use <=, >= for comparisons")
    search_entry.pack(side=ctk.LEFT, padx=(0, 10), fill=ctk.X, expand=True)

    # Add AND/OR/<=/>=/=/!= combobox
    and_or_combobox = ctk.CTkComboBox(
        search_frame,
        values=['AND', 'OR', '<=', '>=', '=', '!='],  # Add comparison operators
        width=88,
    )
    and_or_combobox.set('AND')  # Default to AND
    and_or_combobox.pack(side=ctk.LEFT, padx=(10, 10))

    # Add the search button
    search_button = ctk.CTkButton(
        search_frame,
        text="Search",
        width=90,
        command=lambda: perform_search(app, search_param_combobox.get(), search_entry.get(), and_or_combobox.get()),
    )
    search_button.pack(side=ctk.LEFT, padx=(0, 0))

    # Add a CTkTextbox for the keyword list
    keyword_listbox = ctk.CTkTextbox(search_window, height=150, width=50)
    keyword_listbox.pack(fill=ctk.X, padx=10, pady=10)

    # Populate the textbox with existing keywords (strip CSV leftovers; sort for stable left edge)
    keywords = app.database.get_all_keywords()
    cleaned = sorted({(k or "").strip() for k in keywords if (k or "").strip()})
    for kw in cleaned:
        keyword_listbox.insert(ctk.END, f"{kw}\n")

    # Bind double-click event to add selected keyword to search entry
    keyword_listbox.bind("<Double-Button-1>", lambda e: select_keyword_from_textbox(e, keyword_listbox, search_entry))

    # Add instructions for using comparison operators
    instructions_label = ctk.CTkLabel(
        search_window,
        text="Instructions:\n"
             "1. Use 'AND' or 'OR' for combining multiple terms.\n"
             "2. Use '=', '!=', '<=', '>=', '<', or '>' to compare numerical fields (e.g., rating).\n"
             "3. Double-click a keyword to add it to the search field.",
        wraplength=720, justify="left"
    )
    instructions_label.pack(fill=ctk.X, padx=10, pady=5)
    
    # Create the checkbox and link it to our variable
    clear_results_checkbox = ctk.CTkCheckBox(
        search_window, # The parent widget (the search window itself)
        text="Clear previous results",
        variable=app.clear_search_var,
        onvalue=True,
        offvalue=False
    )
    clear_results_checkbox.pack(pady=10, padx=10,anchor="w") # Or .grid() depending on your layout

    def _focus_entry():
        try:
            if search_entry.winfo_exists():
                search_entry.focus_set()
        except tk.TclError:
            pass

    search_window.after(80, _focus_entry)

    return search_window


def select_keyword_from_textbox(event, textbox, entry):
    # Get the index of the line clicked
    index = textbox.index(f"@{event.x},{event.y}")
    line_number = int(index.split('.')[0])
    line_text = textbox.get(f"{line_number}.0", f"{line_number}.end")

    # Add the keyword to the search entry
    current_text = entry.get()
    if current_text:
        entry.insert(ctk.END, f", {line_text.strip()}")
    else:
        entry.insert(ctk.END, line_text.strip())



def open_autotag_settings_window(app, settings):
    window = ctk.CTkToplevel()
    window.title("AutoTag Settings")
    window.geometry("420x560")
    window.grab_set()  # modal window
    window.attributes('-topmost', True) 
    # === TAGGING ENGINE SELECTION ===
    model_frame = ctk.CTkFrame(window)
    model_frame.pack(padx=20, pady=(15, 5), fill="x")
    ctk.CTkLabel(model_frame, text="Tagging Model:").pack(anchor="w")

    model_var = tk.StringVar(value=settings.tagging_engine)
    for option in ["CLIP", "YOLO", "VIT"]:
        ctk.CTkRadioButton(model_frame, text=option, variable=model_var, value=option).pack(anchor="w")

    # === DETECTION PRESET SELECTION ===
    preset_frame = ctk.CTkFrame(window)
    preset_frame.pack(padx=20, pady=(10, 5), fill="x")
    ctk.CTkLabel(preset_frame, text="Detection Strength Preset:").pack(anchor="w")

    preset_var = tk.StringVar(value=settings.tagging_preset)
    for option in ["F_SUPER_AGGRESSIVE", "G_HUMAN_FOCUSED", "H_ULTRA_SAFE"]:
        ctk.CTkRadioButton(preset_frame, text=option, variable=preset_var, value=option).pack(anchor="w")

    # === NUMBER OF PASSES ===
    passes_frame = ctk.CTkFrame(window)
    passes_frame.pack(padx=20, pady=(10, 5), fill="x")
    ctk.CTkLabel(passes_frame, text="Number of Passes:").pack(anchor="w")

    passes_var = tk.StringVar(value=str(settings.number_of_passes))
    ctk.CTkOptionMenu(passes_frame, variable=passes_var, values=["1", "2", "3"]).pack(anchor="w", pady=5)
    
    # === CONFIDENCE THRESHOLD ===
    thresh_frame = ctk.CTkFrame(window)
    thresh_frame.pack(padx=20, pady=(10, 5), fill="x")
    ctk.CTkLabel(thresh_frame, text="Confidence Threshold:").pack(anchor="w")

    threshold_var = tk.StringVar(value=str(settings.confidence_threshold))
    ctk.CTkEntry(thresh_frame, textvariable=threshold_var, width=120).pack(anchor="w", pady=5)


    # === SAVE + CLOSE BUTTON ===
    button_frame = ctk.CTkFrame(window)
    button_frame.pack(pady=15)

    def apply_and_close():
        settings.tagging_engine = model_var.get()
        settings.tagging_preset = preset_var.get()
        settings.number_of_passes = int(passes_var.get())
    # Threshold
        try:
            settings.confidence_threshold = float(threshold_var.get())
        except Exception as e:
            logging.info(f"Invalid threshold, keeping previous: {e}")
        # ✅ Save settings
        try:
            settings.save_to_json()
            logging.info(f"Saved AutoTag settings to JSON: {settings.tagging_engine}, {settings.tagging_preset}, passes={settings.number_of_passes}")
        except Exception as e:
            logging.info(f"Failed to save settings: {e}")

        window.destroy()

    ctk.CTkButton(button_frame, text="Save & Close", command=apply_and_close).pack()


class CTkFlatContextMenu(ctk.CTkToplevel):
    """
    Simple flat context menu (commands + separators only) using CustomTkinter.
    Replacement for basic ``tk.Menu`` where native Win32 styling is unwanted.

    * Tkinter/CTk parent only — never use a non-Tk master (e.g. Pyglet window).
    * Dismiss: debounced FocusOut, Escape, global LMB outside menu, or Pyglet LMB.
    * Outer window stays rectangular on this CTk — only the inner ``CTkFrame`` is rounded.
    """

    _current: CTkFlatContextMenu | None = None
    _global_lmb_registered: bool = False

    def __init__(self, parent: tk.Misc, app: Any | None = None, **kwargs: Any):
        fg = (
            getattr(app, "BackroundColor", None)
            or kwargs.pop("fg_color", None)
            or "#2b2b2b"
        )
        # CTkToplevel on some CustomTkinter versions only accepts a subset of kwargs
        # (no corner_radius / border_width). Rounding stays on ``items_frame``.
        super().__init__(parent, fg_color=fg, **kwargs)
        self._app = app
        self._menu_fg = fg
        # Between previous 8px (~too small) and default 11–12 (~large)
        self._menu_font = ctk.CTkFont(family="Segoe UI", size=11)
        self._dismissed = False
        self._focus_out_token: str | None = None  # Tk ``after`` id
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.transient(parent.winfo_toplevel())
        except tk.TclError:
            pass
        # Kill default Tk highlight line (often reads as a harsh black frame on Win32)
        try:
            self.configure(highlightthickness=0)
        except tk.TclError:
            pass

        # Rounded body only on this frame (Toplevel has no corner_radius here.)
        self.items_frame = ctk.CTkFrame(
            self,
            fg_color=fg,
            corner_radius=10,
            border_width=0,
        )
        self.items_frame.pack(fill="both", expand=True, padx=0, pady=0)

        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Escape>", lambda e: self._dismiss())
        self.withdraw()

    def _wrap_command(self, command: Callable[[], Any] | None) -> Callable[[], Any] | None:
        if command is None:
            return None
        app = self._app

        def wrapped() -> Any:
            if app and hasattr(app, "_mark_menu_interaction"):
                app._mark_menu_interaction()
            return command()

        return wrapped

    @classmethod
    def _ensure_global_lmb(cls, root: tk.Misc) -> None:
        if cls._global_lmb_registered:
            return
        try:
            root.bind_all("<Button-1>", cls._on_global_button1, add="+")
        except tk.TclError:
            return
        cls._global_lmb_registered = True

    @classmethod
    def _on_global_button1(cls, event: tk.Event) -> None:
        m = cls._current
        if m is None or m._dismissed:
            return
        try:
            if not m.winfo_exists():
                cls._current = None
                return
        except tk.TclError:
            cls._current = None
            return
        if m._hit_test_screen(event.x_root, event.y_root):
            return
        m._dismiss()

    def _hit_test_screen(self, rx: int, ry: int) -> bool:
        try:
            x, y = self.winfo_rootx(), self.winfo_rooty()
            w, h = self.winfo_width(), self.winfo_height()
            return x <= rx < x + w and y <= ry < y + h
        except tk.TclError:
            return False

    @classmethod
    def dismiss_current(cls) -> None:
        if cls._current is not None and cls._current.winfo_exists():
            cls._current._dismiss()
        cls._current = None

    def _dismiss(self) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        if CTkFlatContextMenu._current is self:
            CTkFlatContextMenu._current = None
        if self._focus_out_token:
            try:
                self.after_cancel(self._focus_out_token)
            except tk.TclError:
                pass
            self._focus_out_token = None
        try:
            if self.winfo_exists():
                self.destroy()
        except tk.TclError:
            pass

    def _on_focus_out(self, event: tk.Event | None = None) -> None:
        if self._dismissed:
            return

        def maybe_close() -> None:
            self._focus_out_token = None
            if self._dismissed or not self.winfo_exists():
                return
            try:
                fg = self.focus_get()
            except tk.TclError:
                fg = None
            if fg is not None:
                w = str(fg)
                if w == str(self) or w.startswith(str(self) + "."):
                    return
            self._dismiss()

        if self._focus_out_token:
            try:
                self.after_cancel(self._focus_out_token)
            except tk.TclError:
                pass
        self._focus_out_token = self.after(12, maybe_close)

    def add_command(
        self,
        label: str,
        command: Callable[[], Any] | None = None,
        accelerator: str | None = None,
    ) -> None:
        run = self._wrap_command(command)
        hover = getattr(self._app, "hover_menu_row", None) or "#4a4a4a"

        item_frame = ctk.CTkFrame(self.items_frame, fg_color="transparent", corner_radius=2)
        item_frame.pack(fill="x", padx=0, pady=0)

        lbl_text = ctk.CTkLabel(
            item_frame,
            text=label,
            anchor="w",
            font=self._menu_font,
            height=20,
        )
        lbl_text.pack(side="left", padx=(5, 2), pady=0)

        widgets: tuple[tk.Misc, ...] = (item_frame, lbl_text)
        if accelerator:
            lbl_accel = ctk.CTkLabel(
                item_frame,
                text=accelerator,
                anchor="e",
                text_color="gray",
                font=self._menu_font,
                height=20,
            )
            lbl_accel.pack(side="right", padx=(2, 5), pady=0)
            widgets = (item_frame, lbl_text, lbl_accel)

        def on_enter(_e: tk.Event) -> None:
            item_frame.configure(fg_color=hover)

        def on_leave(_e: tk.Event) -> None:
            item_frame.configure(fg_color="transparent")

        def on_click(_e: tk.Event) -> None:
            cmd = run
            defer_master = self.master
            self._dismiss()
            if cmd and defer_master is not None and defer_master.winfo_exists():
                defer_master.after(0, cmd)

        for w in widgets:
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
            w.bind("<Button-1>", on_click)
            try:
                w.configure(cursor="hand2")
            except tk.TclError:
                pass

    def add_separator(self) -> None:
        sep_color = getattr(self._app, "separator_color", None) if self._app else None
        sep_color = sep_color or "#555555"
        sep = ctk.CTkFrame(self.items_frame, height=1, fg_color=sep_color)
        sep.pack(fill="x", padx=4, pady=0)

    def tk_popup(self, x: int, y: int) -> None:
        prev = CTkFlatContextMenu._current
        if prev is not None and prev is not self and prev.winfo_exists():
            prev._dismiss()
        CTkFlatContextMenu._current = self
        root = self.winfo_toplevel()
        CTkFlatContextMenu._ensure_global_lmb(root)
        self.update_idletasks()
        self.geometry(f"+{int(x)}+{int(y)}")
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.focus_force()
