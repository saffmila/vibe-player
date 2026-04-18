"""Preferences, settings load, and panel-state mixin for VideoThumbnailPlayer."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import customtkinter as ctk
from tkinter import ttk

from app_settings import TaggingSettings
from database import Database
from gui_elements import (
    TogglePanelFrame,
    create_preferences_window,
    open_autotag_settings_window,
    setup_gui,
)
from info_panel import InfoPanelFrame

_VTP_INSTALL_ROOT = Path(__file__).resolve().parent.parent


class VtpPreferencesMixin:
  
    def open_preferences_window(self):
        create_preferences_window(self)
        

    def open_autotag_settings_window(self):
        settings = TaggingSettings()
        settings.load_from_json()
        open_autotag_settings_window(self, settings)

        
    def optimize_database(self):
        logging.debug("remove_duplicate_thumbnails: clearing duplicates")
        Database.remove_duplicates_from_db("catalog.db")

    def set_thumbnail_format(self, format):
        self.thumbnail_format = format
        # self.save_preferences()    #{"thumbnail_format": self.thumbnail_format}
        
    def setup_gui(self):
        setup_gui(self)     
    
    def apply_treeview_theme(self):
        mode = ctk.get_appearance_mode()
        if mode == "Dark":
            style = ttk.Style()
            style.configure("Treeview", 
                            font=("Verdana", 80),
                            background="#2d2d2d",
                            foreground="white",
                            fieldbackground="#2d2d2d",
                            bordercolor="#2d2d2d",
                            lightcolor="#2d2d2d",
                            darkcolor="#2d2d2d",
                            borderwidth=0)
            style.map("Treeview", background=[("selected", "#3a3a3a")])
        else:
            style = ttk.Style()
            style.configure("Treeview",
                            font=("Helvetica", 20),
                            background="white",
                            foreground="black",
                            fieldbackground="white",
                            bordercolor="white",
                            lightcolor="white",
                            darkcolor="white",
                            borderwidth=0)
            style.map("Treeview", background=[("selected", "#e5e5e5")])
            # self.tree_frame.configure(fg_color="green")  # Set the background color of the tree_frame
            self.left_frame .configure(fg_color="green")  # Set the background color of the tree_frame
        

    def ensure_info_panel_container(self):
        """
        Lazily create the info panel container and ``InfoPanelFrame`` if missing.
        """
        if self.info_panel_container is not None and self.info_panel_container.winfo_exists():
            return

        logging.debug("Lazy init: creating Info panel")

        self.info_panel_container = TogglePanelFrame(self.left_split, title="Info Panel", app=self)

        # 2. Actual content with this parent
        self.info_panel = InfoPanelFrame(self.info_panel_container)
        self.info_panel.pack(fill="both", expand=True)

        # Bind tab-change callback: when user switches to the Video tab,
        # trigger video info extraction for the currently selected file
        self.info_panel.tabs.configure(command=self._on_info_panel_tab_changed)

        # 3. Add the whole panel to the PanedWindow
        self.left_split.add(self.info_panel_container)
        self.left_split.paneconfig(self.info_panel_container, minsize=100, height=150)

        if hasattr(self.info_panel, "preview_player") and self.info_panel.preview_player:
            self.info_panel.preview_player.timeline_widget = self.timeline_widget
            logging.debug("Lazy init: timeline widget linked to preview player")

        _settings_path = _VTP_INSTALL_ROOT / "settings.json"
        if _settings_path.is_file():
            with open(_settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
                self.apply_panel_states_from_settings(settings)


    def apply_preferences(self):
        if self.current_video_window:
            self.current_video_window.apply_preferences()
            logging.info("[DEBUG] current_video_window created: %s", self.current_video_window)
            logging.info("Preferences applied to current video window.")  # Debug
        else:
            logging.info("No current video window to apply preferences to.")  # Debug


    def parse_thumbnail_size(self, size_string):
        logging.debug("parse_thumbnail_size: %s", size_string)

        width, height = map(int, size_string.split("x"))
        return (width, height)


    def apply_panel_states_from_settings(self, settings):
        """
        Applies panel visibility states from loaded settings.
        Ensures internal flags (like ShowTWidget) are always synchronized
        with the loaded state, even if no visual toggle happens.
        """
        # --- Info Panel ---
        # Get the desired state from settings, default to True if not found
        desired_info_state = settings.get("info_panel_expanded", True)

        # Check if the container exists before accessing it
        if hasattr(self, "info_panel_container") and self.info_panel_container:

            self.preview_on = desired_info_state
            logging.info(f"[ApplySettings][InfoPanel] Set preview_on = {self.preview_on} based on settings.")


            # Only toggle visually if the current state doesn't match the desired state
            if self.info_panel_container.expanded != desired_info_state:
                # toggle_infopanel_menu already calls update_panel_flags internally
                self.toggle_infopanel_menu(save_prefs=False)  # Don't save during load - avoids overwriting other panel states prematurely

        # --- Timeline Widget ---
        # Get the desired state from settings, default to True if not found
        desired_timeline_state = settings.get("timeline_widget_expanded", True)

        # Check if the container exists before accessing it
        if hasattr(self, "timeline_container") and self.timeline_container:
            self.ShowTWidget = desired_timeline_state
            logging.info(f"[ApplySettings][Timeline] Set ShowTWidget = {self.ShowTWidget} based on settings.")


            # Only toggle visually if the current state doesn't match the desired state
            if self.timeline_container.expanded != desired_timeline_state:
                # Pass save_prefs=False because we don't need to save settings *during* loading 
                self.toggle_timeline_menu(save_prefs=False) # Let the toggle function handle the visual change





    def load_preferences(self):
        settings = {}  # Initialize as an empty dictionary in case the file doesn't exist
        if os.path.exists("settings.json"):
            logging.info(f"Loading settings from: {os.path.abspath('settings.json')}")
            with open("settings.json", "r") as pref_file:
                settings = json.load(pref_file)
                
                # Directly access the settings dictionary
                self.capture_method_var.set(settings.get("capture_method", "OpenCV"))  # Default to FFmpeg if not set
                self.thumbnail_size = self.parse_thumbnail_size(settings.get("thumbnail_size", "320x240"))  # Convert to tuple
                self.thumbnail_format = settings.get("thumbnail_format", self.thumbnail_format)
                # self.thumbnail_cache_path = settings.get("thumbnail_cache_path", self.thumbnail_cache_path)
                # Check if the settings contain more than just the default relative path.
                    # If it's just "thumbnail_cache", we keep the absolute path calculated during init.
                loaded_path = settings.get("thumbnail_cache_path")
                if loaded_path and loaded_path != "thumbnail_cache":
                    self.thumbnail_cache_path = loaded_path
                self.auto_play = settings.get("auto_play", self.auto_play)
                if getattr(self, "info_panel", None) and hasattr(self.info_panel, "preview_auto_play_var"):
                    self.info_panel.preview_auto_play_var.set(
                        settings.get("preview_auto_play", True)
                    )
                self.video_output_var.set(settings.get("video_output", 'direct3d11'))
                # self.audio_output_var.set(settings.get("audio_output", 'directsound'))
                self.audio_output_var.set(settings.get("audio_output", 'default'))
                self.hardware_decoding_var.set(settings.get("hardware_decoding", 'dxva2'))
                # self.audio_device_var.set(settings.get("audio_device", self.audio_device_var.get()))
                audio_device_from_settings = settings.get("audio_device")
                if audio_device_from_settings:
                    self.audio_device_var.set(audio_device_from_settings)
                else:
                    devices = self.audio_devices
                    if devices:
                        self.audio_device_var.set(devices[0]['name'])
                # Load thumbnail time
                self.thumbnail_time = settings.get("thumbnail_time", 0.1)  # Default to 10% if not set
                self.thumbnail_time_var.set(int(self.thumbnail_time * 100))  # Set slider value
                # New additions
                self.numwidefolders_in_col = settings.get("numwidefolders_in_col", 2)  # Default to 2
                # 1. Load value under the new settings key
                is_wide = settings.get("wide_folders_check_var", True)  # Default True (Wide)
                
                # 2. Set the BooleanVar directly
                self.wide_folders_check_var.set(is_wide)
                
                self.folder_view_mode.set("Wide" if is_wide else "Standard")
                
                self.widefolder_size = self.parse_thumbnail_size(settings.get("widefolder_size", "560x400"))  # Default to 560x400
                self.memory_cache = settings.get("memory_cache", True)  # Default to True if not set
                # Font size preferences
                self.thumbFontSize = settings.get("thumb_font_size", self.thumbFontSize)
                self.base_font_size = settings.get("tree_font_size", self.base_font_size)
                self.video_show_hud = settings.get("video_show_hud", True)
                self.gpu_upscale = settings.get("gpu_upscale", False)
                self.vlc_enable_postproc = settings.get("vlc_enable_postproc", False)
                self.vlc_postproc_quality = int(settings.get("vlc_postproc_quality", 6))
                self.vlc_postproc_quality = max(0, min(6, self.vlc_postproc_quality))
                self.vlc_enable_gradfun = settings.get("vlc_enable_gradfun", False)
                self.vlc_enable_deinterlace = settings.get("vlc_enable_deinterlace", False)
                self.vlc_skiploopfilter_disable = settings.get("vlc_skiploopfilter_disable", False)
                self.timeline_strip_count = settings.get("timeline_strip_count", 20)
                if getattr(self, "info_panel", None) and \
                        hasattr(self.info_panel, "multiTimeline_limit_var"):
                    self.info_panel.multiTimeline_limit_var.set(
                        settings.get("multiTimeline_limit", True)
                    )
                self.dnd_confirm_dialogs = bool(settings.get("dnd_confirm_dialogs", False))
                self.delete_to_trash = bool(settings.get("delete_to_trash", True))
                self.image_viewer_use_pyglet = bool(
                    settings.get("image_viewer_use_pyglet", False)
                )
                # Splitter positions (fractions 0-1)
                self._saved_main_sash_fraction = settings.get("splitter_main_fraction")
                self._saved_left_sash_fraction = settings.get("splitter_left_fraction")
                self._saved_right_sash_fraction = settings.get("splitter_right_fraction")
                logging.info(f"[SPLITTER LOAD] main={self._saved_main_sash_fraction}, left={self._saved_left_sash_fraction}, right={self._saved_right_sash_fraction}")
                # Apply panel visibility states LAST - after all variables are set,
                # so that any save_preferences triggered from here has correct values.
                self.apply_panel_states_from_settings(settings)
                # Debugging statements to verify loaded preferences
                logging.info(f"Loaded capture_method: {self.capture_method_var.get()}")
                logging.info(f"Loaded thumbnail_format: {self.thumbnail_format}")
                logging.info(f"Loaded thumbnail_cache_path: {self.thumbnail_cache_path}")
                logging.info(f"Loaded auto_play: {self.auto_play}")
                logging.info(f"Loaded video_output: {self.video_output_var.get()}")
                logging.info(f"Loaded audio_output: {self.audio_output_var.get()}")
                logging.info(f"Loaded hardware_decoding: {self.hardware_decoding_var.get()}")
                logging.info(f"Loaded audio_device: {self.audio_device_var.get()}")
                logging.info(f"Loaded numwidefolders_in_col: {self.numwidefolders_in_col}")
                logging.info(f"Loaded self.folder_view_mode: {self.folder_view_mode.get()}")
                logging.info(f"Loaded widefolder_size: {self.widefolder_size}")
                
                logging.info(f"Loaded thumbFontSize: {self.thumbFontSize}")
                logging.info(f"Loaded base_font_size: {self.base_font_size }")
                # logging.info(f"Loaded infopanel expanded or not: { self.info_panel_container.expanded  }")
                
        else:
            # If the settings file does not exist, initialize with defaults
            self._saved_main_sash_fraction = None
            self._saved_left_sash_fraction = None
            self._saved_right_sash_fraction = None
            logging.info("[SPLITTER LOAD] First run - no settings.json, all fractions=None")
            self.video_show_hud = True  # Default on
            self.gpu_upscale = False
            self.vlc_enable_postproc = False
            self.vlc_postproc_quality = 6
            self.vlc_enable_gradfun = False
            self.vlc_enable_deinterlace = False
            self.vlc_skiploopfilter_disable = False
            self.capture_method_var.set('OpenCV')
            self.thumbnail_size_option.set("320x240")
            self.thumbnail_format = "jpg"
            #self.thumbnail_cache_path =  "e:/python/vlc_player/thumbnail_cache"
            self.auto_play = True
            self.video_output_var.set('direct3d11')
            self.audio_output_var.set('wasapi')
            self.hardware_decoding_var.set('dxva2')
            # self.audio_device_var.set(sd.query_devices()[0]['name'])  # Default to first audio device
            self.thumbnail_time = 0.1  # Default to 10% for thumbnail creation time
            self.thumbnail_time_var.set(10)
            self.numwidefolders_in_col = 2  # Default
            # self.wide_folders_var.set(settings.get("wide_folders_var", True))  # Default
            # self.folder_view_mode.set("Wide" if settings.get("wide_folders_var") else "Standard")
            self.wide_folders_check_var.set(True)  # Default True (Wide)
            self.widefolder_size = (560, 400)  # Default size
            self.memory_cache = True  # Default if no settings file exists
            self.dnd_confirm_dialogs = False
            self.delete_to_trash = True
            self.image_viewer_use_pyglet = False
            #load audio device only it wasnt loaded before
            if "audio_device" in settings:
                    self.audio_device_var.set(settings["audio_device"])
            else:
                devices = self.audio_devices  # use already-loaded cache
                if devices:
                    self.audio_device_var.set(devices[0]['name'])
                   
                        
        # Additional debugging statements
        logging.info(f"PREFERENCES LOAD thumbnail_format: {self.thumbnail_format},  thumbnail_size  :  { self.thumbnail_size  }    cache_path: {self.thumbnail_cache_path}, auto_play: {self.auto_play}, audio_device: {self.audio_device_var.get()}     ")
         # Debug statement to confirm loading
        logging.info(f"PREFERENCES LOAD: numwidefolders_in_col={self.numwidefolders_in_col}, self.folder_view_mode={self.folder_view_mode}, widefolder_size={self.widefolder_size}")


