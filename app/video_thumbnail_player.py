"""
Main Vibe Player window: folder tree, thumbnail grid, playback, search, and plugins.

``VideoThumbnailPlayer`` hosts the tree, grid, timeline, playlist, preferences,
hotkeys, tagging plugins, and folder watchers.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import math
from pathlib import Path

from PIL import Image, ImageTk, ImageOps, ImageDraw

_APP_DIR = Path(__file__).resolve().parent
_INSTALL_ROOT = _APP_DIR.parent

splash_proc = None
_DETACHED_PROCESS = 0x00000008
_splash_script = _APP_DIR / "splash_image.py"
_splash_image_primary = _APP_DIR / "assets" / "vibe_player_final.png"
_splash_image_legacy = _INSTALL_ROOT / "assets" / "vibe_player_final.png"


def _splash_image_to_show() -> Path | None:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        bundled = base / "assets" / "vibe_player_final.png"
        if bundled.is_file():
            return bundled
        return None
    if _splash_image_primary.is_file():
        return _splash_image_primary
    if _splash_image_legacy.is_file():
        return _splash_image_legacy
    return None


_splash_img = _splash_image_to_show()
if _splash_img is not None:
    if getattr(sys, "frozen", False):
        splash_proc = subprocess.Popen(
            [sys.executable, "--vibe-splash", str(_splash_img)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=_DETACHED_PROCESS,
        )
    elif _splash_script.is_file():
        splash_proc = subprocess.Popen(
            [sys.executable, str(_splash_script), str(_splash_img)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=_DETACHED_PROCESS,
        )

from log_window import LogWindow

os.environ["TQDM_DISABLE"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"

import gc
import threading
import queue
from queue import Queue
import cv2
import tkinter as tk
from tkinter import filedialog, ttk, simpledialog, messagebox

from file_operations import *
from gui_elements import *
from playlist import PlaylistManager

from statusbar import StatusBar
from database import Database
from virtual_folders import load_virtual_folders, save_virtual_folders, add_to_virtual_folder, create_virtual_folder
from clipboard_file_list import clipboard_has_pastable_paths
import json
import sqlite3
import shutil
import customtkinter as ctk
from gui_elements import setup_menu
from gui_elements import save_preferences
from tkinter import Menu
from utils import ThumbnailCache
from utils import get_video_size
import tkinter.font as tkFont
from debug_overlay import DebugOverlay
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import signal
import traceback
import functools
import hashlib
import mimetypes
import faulthandler
import logging
import ctypes
from customtkinter import CTkImage

from plugin_manager import PluginManager
from info_panel import InfoPanelFrame
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from video_operations import get_audio_devices

from video_operations import VideoPlayer
from timeline_manager import TimelineManager
from timeline_bar_widget import TimelineBarWidget
from multi_timeline_viewer import MultiTimelineViewer
from utils import create_menu
import win32api
import string

import tkinterdnd2 as dnd

from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS
from vtp_mixin_dnd import VtpDndMixin
from vtp_mixin_grid import VtpGridMixin
from vtp_mixin_legacy_drag import VtpLegacyDragMixin
from vtp_mixin_preferences import VtpPreferencesMixin
from vtp_mixin_tagging import VtpTaggingMixin
from vtp_mixin_window_layout import VtpWindowLayoutMixin
from vtp_virtual_grid import VtpVirtualGridMixin

from screeninfo import get_monitors
from hotkeys import DEFAULT_HOTKEYS


# Set the logging level for PIL to WARNING to suppress debug logs
logging.getLogger("PIL").setLevel(logging.WARNING)

_crash_log_path = _INSTALL_ROOT / "crash.log"
with open(_crash_log_path, "w", encoding="utf-8") as log_file:
    faulthandler.enable(file=log_file, all_threads=True)

# Enable DPI awareness for accurate resolution detection
ctypes.windll.shcore.SetProcessDpiAwareness(2)


def _apply_windows_immersive_dark_titlebar(widget):
    """
    Use DWM immersive dark mode for the native caption (minimize / maximize / close bar)
    on Windows 10 1809+ and Windows 11. No-op on other platforms or if the API fails.
    """
    if sys.platform != "win32":
        return
    try:
        widget.update_idletasks()
        wid = int(widget.winfo_id())
        GA_ROOT = 2
        hwnd = ctypes.windll.user32.GetAncestor(wid, GA_ROOT) or wid
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        use_dark = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(use_dark),
            ctypes.sizeof(use_dark),
        )
    except Exception:
        logging.debug("Immersive dark title bar could not be applied.", exc_info=True)


def handle_signal(signum, frame):
    logging.info(f"Received signal: {signum}")

# Use SIGINT or skip signal handling entirely
signal.signal(signal.SIGINT, handle_signal)


def _resolve_paths():
    """
    Resolve install paths relative to this file.
    Returns:
        app_dir: .../vlc_player/app
        install_root: .../vlc_player
        cache_dir: .../vlc_player/thumbnail_cache
    """
    app_dir = Path(__file__).resolve().parent
    install_root = app_dir.parent
    cache_dir = install_root / "thumbnail_cache"
    return app_dir, install_root, cache_dir


# logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')


class DirectoryChangeHandler(FileSystemEventHandler):
    def __init__(self, on_change_callback):
        self.on_change_callback = on_change_callback

    def on_any_event(self, event):
        if event.is_directory:
            logging.debug("Watchdog directory event: %s", event.src_path)
        else:
            logging.debug("Watchdog file event: %s", event.src_path)

        self.on_change_callback(event.src_path)


class VideoThumbnailPlayer(
    VtpVirtualGridMixin,
    VtpGridMixin,
    VtpDndMixin,
    VtpWindowLayoutMixin,
    VtpLegacyDragMixin,
    VtpTaggingMixin,
    VtpPreferencesMixin,
    dnd.TkinterDnD.Tk,
):
    """
    Main application window: folder tree, thumbnail grid, playback, playlist,
    search, preferences, plugins, and DnD. Subclasses TkinterDnD root for drag-and-drop.
    """
    # Compatibility shim for CustomTkinter DPI tracker.
    # TkinterDnD root does not implement these CTk APIs.
    def block_update_dimensions_event(self):
        self._dimensions_update_blocked = True

    def unblock_update_dimensions_event(self):
        self._dimensions_update_blocked = False

    def __init__(self, log_path):
        super().__init__()  # Must be the first call in __init__ (Tk / DnD).
        
        start_time = time.time()  # ⬅ start timer

        # --- Performance tuning parameters for thumbnail loading ---
        self.max_immediate_items = 40  # Max thumbs loaded synchronously blocking the UI
        self.chunk_time_limit = 0.060  # Max time (in seconds) the background loop can freeze UI
        self.min_chunk_size = 20     # Minimum thumbnails to process per background chunk.
        
        _initial_layout_done = False
        self.plugin_manager = PluginManager()
        threading.Thread(target=self.plugin_manager.load_plugins, daemon=True).start()
        logging.info("[PERF] Plugin loading moved to background thread.")
        self._pending_dpi_scale = None
        self._dimensions_update_blocked = False
        self._preview_timer = None
        self.thumbSelColor = "#4f575f"       #3399ff#b0b0b0       # or "#3399ff" for blue selection
        self.thumbBorderColor = "#282828" #  "#282828"
        self.thumbBGColor = "#181a1d"
        self.BackroundColor = "#101215"
        # bottom thumbnail color - if you want unreal5 style change to darker
        self.labelBGColor  =  "#181a1d" #101215  
        #color of fonts in top menu
        self.thumb_TextColor = "#c4c4c4" 
        self.LTreeBGColor = "#181a1d" ##1f1f1f
        self.TreeCursorColor = "#365f8d"

        self.tree_TextColor = "#c4c4c4" 

        # TkinterDnD root is a plain Tk window (not CTk), so without an explicit bg it stays white.
        # That shows up as a light frame around the top toolbar (padding).
        self.configure(bg=self.BackroundColor, highlightthickness=0, bd=0)
        self._last_menu_interaction_time = 0
        # DnD debounce: drag starts only after intentional button hold
        self._dnd_hold_ms = 180
        # For multi-select (e.g. Ctrl+A) use a shorter hold so drag still works with fast gestures.
        self._dnd_hold_ms_multi = 35
        # Tree DragInit fires before thumbnail DragInit; a long hold would block tree DnD.
        self._dnd_hold_ms_tree = 30
        self._dnd_press_ts = 0.0
        self._dnd_press_kind = None   # "thumb" | "tree" | None
        self._dnd_press_path = None
        # Drop target dwell guard (anti-accidental drop)
        self._dnd_target_dwell_ms = 220
        self._dnd_tree_hover_since = 0.0
        # True = drag started inside our app (thumb/tree); Explorer / other app = False
        self._dnd_internal_drag = False
        # Thumbnail drag-out actually began (avoids collapsing multi-select on LMB release).
        self._dnd_drag_happened = False
        # Confirm dialogs before DnD copy/move (default off — Advanced in preferences)
        self.dnd_confirm_dialogs = False
        self.folder_title_font_base_size = 12
        self.folder_title_font = ctk.CTkFont(size=self.folder_title_font_base_size, weight="bold") # Or choose size/weight you prefer 
        self.thumb_BorderSize = 14
        self.thumb_Padding = 2
        self.outlinewidth=2 # deselected outline (+1px for better visibility)
        self.Select_outlinewidth=2 #selected outline (+1px for better visibility)
        self.load_time_history = []
        self.rows = 4
        self.columns = 4
        self.visible_range    = (0, 0)
        self.selected_thumbnail_path = None
        self.lastcolumns = 4
        self.thumbnail_chunk_size = 100
        self.thumbnail_size_option = ctk.StringVar(value="320x240")
        self.thumbnail_size = (320, 240)
        self.widefolder_size = (320, 160)  # New wide thumbnail size for folders  (320, 160)
        self.cache_enabled = True
        self.hud_enabled = True
        self.thumbFontSize = 7  # Default font size for RIGHT thumbs panel
        self.base_font_size = 11  # Default font size for LEFT  TREE panel
        
        self.row_height = 30
        
        self.current_dpi_scale = self.get_windows_scaling_factor()
        logging.info(f"Initial DPI scale detected: {self.current_dpi_scale}")

        self.update_all_scaling(self.current_dpi_scale)

        self.bind("<Configure>", self.on_window_resize)
        self.log_path = log_path
        self.log_window = None
        self.watchdog_observer = None
        self.watchdog_handler = None
        self.thumbnail_widgets = []  # Keep track of thumbnail widgets  
        
        
        # --- resolved, portable paths ---
        _app_dir, _install_root, _cache_dir = _resolve_paths()
        self.default_directory = str(_app_dir)
        
        
        self.thumbnail_cache_path = str(_cache_dir)
        os.makedirs(self.thumbnail_cache_path, exist_ok=True)

        self.thumbnail_cache = ThumbnailCache(cache_dir=self.thumbnail_cache_path)
        
        icon_png_path = os.path.join(self.default_directory, 'icons', 'vibe_player.png')
        icon_ico_path = os.path.join(self.default_directory, 'icons', 'vibe_player.ico')

        # Windows taskbar groups windows by AppUserModelID. Setting it helps preserve
        # the custom app icon instead of generic python icon in some environments.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("vibe.player.app")
        except Exception:
            pass

        # Keep a strong reference to icon image; without it, Tk can revert to default icon.
        self._app_icon_image = None
        try:
            if os.path.exists(icon_png_path):
                self._app_icon_image = ImageTk.PhotoImage(Image.open(icon_png_path))
                self.iconphoto(True, self._app_icon_image)
                logging.info(f"[ICON] Loaded app icon from PNG: {icon_png_path}")
            elif os.path.exists(icon_ico_path):
                # Fallback to .ico for environments where PNG is missing.
                self._app_icon_image = ImageTk.PhotoImage(Image.open(icon_ico_path))
                self.iconphoto(True, self._app_icon_image)
                logging.info(f"[ICON] Loaded app icon from ICO: {icon_ico_path}")
            else:
                logging.warning(f"[ICON] Icon file not found: {icon_png_path}")
        except Exception as e:
            logging.warning(f"[ICON] Failed to set iconphoto: {e}")

        # iconbitmap can help taskbar icon on some Windows/Tk builds, so try it as best effort.
        try:
            if os.path.exists(icon_ico_path):
                self.iconbitmap(icon_ico_path)
        except Exception:
            pass
            

        self.settings_file = "recent_directories.json"
        self.memory_cache = True
        self.active_player = None
        self.total_files_to_process = 0
        self.processed_files_count = 0
        self.capture_method_var = tk.StringVar(value="OpenCV")
        self.current_directory = self.default_directory
        self.selected_thumbnails = []
        self.wide_folders = []  # Track wide folder paths for selection handling
        self._wide_folder_stats_cache = {}  # normalize_path(folder) -> aggregate stats dict
        self._wide_folder_left_gutter_px = None  # one left-column width per wide-folder batch
        self._folder_media_presence_cache = {}  # normalize_path(folder) -> bool
        self.wide_folder_stats_font = ctk.CTkFont(size=10)
        self._tree_sync_after_id = None
        self.option_menu = None  # Initialize option_menu here
        self._thumb_click_after_id = None
        self.preview_on = True
        self.ShowTWidget = False
        self.setup_styles()
        self.status_queue = queue.Queue()
        self.status_bar = StatusBar(self)
        # Initialize thumbnail time as a float representing percentage (default: 10% of video duration)
        self.thumbnail_time = 0.1  # Default value: 10% of video duration

        self.folder_view_mode = ctk.StringVar(value="Standard")  # default "Standard" (Wide mode via checkbox)
        self.folder_view_mode.trace_add("write", self._on_folder_view_changed)
        
        self.wide_folders_check_var = tk.BooleanVar()
        self.wide_folders_check_var.trace_add("write", self._on_check_var_changed)
        # Wide folder styling variables
        self.wide_folder_cornerRadius = 12     # corner radius for wide-folder cards
        self.wide_folder_gap = 25              # spacing between previews in wide strip (was 10)
        self.wide_folder_borderWidth = 0       # permanent border thickness
        self.wide_folder_borderColor = "#383838"  # subtle gray border (dark theme)
        self.wide_folder_innerThumbRadius = 12  # corner radius for thumbs inside wide strip

        self.numwidefolders_in_col = 2
        # StringVar for controlling thumbnail time via slider
        self.thumbnail_time_var = tk.IntVar(value=int(self.thumbnail_time * 100))  # Convert to percentage for the slider
        self.showFolderImages = True
        self.thumb_queue = Queue()
        self.gui_render_queue = Queue()
        self.thumb_batch_size = 24
        self.thumb_queue_running = False
        self.autotag_settings = TaggingSettings()
        self.after_jobs = []
        self.autotag_settings.load_from_json()

        self._last_click_time = 0
        self._click_timer = None
        self._click_interval = 250  # ms
        

        # Initialize empty list to prevent blocking GUI thread
        self.audio_devices = [] 

        def _delayed_audio_init():
            """
            Fetches audio devices 1 second after app startup in the main thread.
            Prevents sounddevice C++ crashes and keeps the startup fast.
            """
            from video_operations import get_audio_devices
            self.audio_devices = get_audio_devices()

        # Tkinter calls this 1000ms after the GUI is already loaded and visible
        self.after(1000, _delayed_audio_init)
        
        self.status_bar.set_stop_callback(self.stop_scan)
        self.selected_rating = 0
        self.update_thread = threading.Thread(target=self._update_status)
        self.update_thread.daemon = True
        self.update_thread.start()
        
        # --- ADD THESE TWO LINES FOR FOLDER COLORS ---
        self.folder_color_media = "#242b33"  # A nice blue for folders with media
        self.folder_color_empty = "#8a795d"  # A subtle gold/brown for empty folders
        optimal_workers = min(32, (os.cpu_count() or 4) + 4)

        self.executor = ThreadPoolExecutor(max_workers=optimal_workers, thread_name_prefix='ThumbnailWorker')
        
        logging.info(f"[DEBUG INIT] Thread pool created: id={id(self.executor)}, shutdown={self.executor._shutdown}")

        def simple_test_task():
            import threading
            logging.info(f"[EXECUTOR TEST] >>> test task on thread {threading.current_thread().name} <<<")
        self.executor.submit(simple_test_task)

        self.img_cache = {}
        self.check_status_queue()
        self.recursive_tree_refresh = False
        self.get_vidsize = False
        self.get_imgsize = True
        self.autoplay_vid = True
        self.setup_icons()
        self.thumbnail_labels = {}
        self.current_volume = 100
        self.quick_acces_history = 15
        self.selected_file_path = None
        self.current_image_window = None
        # False = Tk legacy viewer (default); True = Pyglet/OpenGL (settings.json)
        self.image_viewer_use_pyglet = False
        self.search_window = None
        self.skip_generated = True
        self.search_param = ctk.StringVar(value="filename")  # Initialize with a default value
        self.thumbnail_format = "jpg"
        self.title("Vibe Player")
        self.previous_size = (self.winfo_width(), self.winfo_height())
        self.set_default_window_geometry()
        self.update_idletasks()  # needed for correct width/height
        _apply_windows_immersive_dark_titlebar(self)
        self.after_idle(_apply_windows_immersive_dark_titlebar, self)

        self.show_empty_strips_var = tk.BooleanVar(value=True)  # default on    
        self.thumbnail_rating_widgets = {}
        self.metadata_cache = {}
        self.db_name = "catalog.db"
        t0 = time.time()
        self.database = Database(self.db_name)
        _orig_db_update_keywords = self.database.update_keywords

        def _db_update_keywords_invalidate_wide_stats(file_path, keywords):
            _orig_db_update_keywords(file_path, keywords)
            self._wide_folder_stats_cache.clear()

        self.database.update_keywords = _db_update_keywords_invalidate_wide_stats
        logging.info(f"[TIMER] Database init: {time.time() - t0:.2f}s")
        self.file_ops = FileOperations(self, VIDEO_FORMATS, IMAGE_FORMATS)
        
        self.debug_overlay = DebugOverlay(self)

        self._previous_size = (self.winfo_width(), self.winfo_height())
        self.video_files      = []          # avoid AttributeError before first load
        # This variable acts as a lock to prevent duplicate loading processes.
        self._is_loading = False
        # Cancellation token: incremented on every new load; each async phase checks it.
        self._render_id = 0
        # Debounce job for TreeView folder selection
        self._debounce_job: str | None = None
        # CTkComboBox dropdown mouse-up can "fall through" to tree/thumbs below.
        self._ignore_pointer_navigation_until = 0.0
        # While syncing tree selection to current_directory, ignore folder loads from <<TreeviewSelect>>.
        self._suppress_tree_select_navigation = False

       
        # Variable to hold the state of the checkbox (True/False)
        # We'll set the default to True, so clearing results is the initial behavior.
        self.clear_search_var = ctk.BooleanVar(value=True) 
        # A list to store and accumulate search results
        self.current_search_results = []

        # Folder icons for grid (yellow placeholders if load fails)
        icons_dir = os.path.join(self.default_directory, "icons")
        
        try:
            self.folder_icon = ctk.CTkImage(
                light_image=Image.open(os.path.join(icons_dir, "folder.png")), 
                dark_image=Image.open(os.path.join(icons_dir, "folder.png")),
                size=(96, 96)
            )
            self.folder_icon_green = ctk.CTkImage(
                light_image=Image.open(os.path.join(icons_dir, "folder_g.png")), 
                dark_image=Image.open(os.path.join(icons_dir, "folder_g.png")),
                size=(96, 96)
            )
        except Exception as e:
            logging.error(f"[GUI-ERROR] Failed to load folder icons from {icons_dir}: {e}")
 
        self.multiple_vid_instances = False
        self.auto_play = True
        self.subtitles_enabled=True
        self.recent_directories = []
        self.recent_directories = load_recent_directories(self.settings_file)
        logging.info(f"Loading recent directories from: {os.path.abspath(self.settings_file)}")
        self.is_fullscreen = False
        self.processed_directories = set()

        self.video_output_var = ctk.StringVar()
        self.audio_output_var = ctk.StringVar()
        self.hardware_decoding_var = ctk.StringVar()
        self.audio_device_var = ctk.StringVar()

        setup_menu(self)
        self.setup_styles()
        self.thumbnail_images = []
        self.image_references = []
        t1 = time.time()
        self.setup_gui()
        logging.info(f"[TIMER] GUI setup: {time.time() - t1:.2f}s")

        self.paned_window = tk.PanedWindow(
            self,
            orient=tk.HORIZONTAL,
            bg="#2d2d2d",
            bd=0,
            sashwidth=2,
            showhandle=False,
            relief='flat'
        )

        
        self.paned_window.pack(fill=ctk.BOTH, expand=True, padx=0, pady=0)
        self.left_frame = ctk.CTkFrame(self, width=330, fg_color=self.LTreeBGColor, corner_radius=0)
        self.left_frame.pack(side=ctk.LEFT, fill=ctk.Y)
        self.left_frame.pack_propagate(False)
   
       
        # Tree + InfoPanel in PanedWindow for resizable split
        self.left_split = tk.PanedWindow(self.left_frame, orient=tk.VERTICAL, bg="#2d2d2d")
        self.left_split.pack(fill=ctk.BOTH, expand=True)

        self.tree_frame = ctk.CTkFrame(self.left_split, fg_color="#2d2d2d")
        self.left_split.add(self.tree_frame)

        self.info_panel_container = TogglePanelFrame(self.left_split, title="Info Panel", app=self)

        self.info_panel = InfoPanelFrame(self.info_panel_container)

        self.info_panel.pack(fill="both", expand=True)

        # Wire VIDEO/STRIPS switch (multi_viewer is lazy; callback must exist now)
        if hasattr(self.info_panel, "preview_mode_switch"):
            self.info_panel.preview_mode_switch.configure(command=self._on_preview_mode_change)

        # 3. Add the whole panel to the PanedWindow
        self.left_split.add(self.info_panel_container)
        self.left_split.paneconfig(self.info_panel_container, minsize=100, height=150)

        self.right_frame = ctk.CTkFrame(self)
        self.right_frame.pack(side=ctk.RIGHT, fill=ctk.BOTH, expand=True)

        video_path = None
        video_name = ""
        # Info panel height is adjusted after the window is shown
        self.timeline_manager = TimelineManager(
            thumbnail_size=(320, 240),
            thumbnail_format="jpg",
            cache_dir=self.thumbnail_cache_path  #  "thumbnail_cache"
        )
        self.multi_viewer = None          # lazy init – created on first multi-select
        self.timeline_strip_count = 20    # max strips (safety cap)
        # VLC quality/post-processing options (overridable from Preferences)
        self.vlc_enable_postproc = False
        self.vlc_postproc_quality = 6
        self.vlc_enable_gradfun = False
        self.vlc_enable_deinterlace = False
        self.vlc_skiploopfilter_disable = False
        
        
        # Right split: thumbnails + timeline
        self.right_split = tk.PanedWindow(self.right_frame, orient=tk.VERTICAL, bg="#2d2d2d", sashwidth=4)
        self.right_split.pack(fill=ctk.BOTH, expand=True)

        self.frame = ctk.CTkFrame(self.right_split, fg_color=self.BackroundColor, corner_radius=0)  # parent: right_split
        self.canvas = tk.Canvas(
            self.frame,
            bg=self.BackroundColor,
            bd=0,
            highlightthickness=0,
            relief="flat"
        )
        self.canvas.pack(side="left", fill=ctk.BOTH, expand=True)

        # Thumbnails panel in the split
        self.right_split.add(self.frame)

        self.timeline_container = TogglePanelFrame(self.right_split, title="Timeline", app=self)

        self.timeline_widget = TimelineBarWidget(
            parent=self.timeline_container,
            controller=self,
            video_path=video_path,
            timeline_manager=self.timeline_manager,
            on_seek=self.seek_video
        )
        
        self.timeline_widget.pack(fill="both", expand=True)

        self.timeline_container.content_widget = self.timeline_widget
        self.timeline_container.parent_paned = self.right_split
   
        self.right_split.add(self.timeline_container)
        self.right_split.paneconfig(self.timeline_container, minsize=100, height=150)

                    
        # Safe if no video is selected yet
        if self.selected_file_path:
            video_path = self.selected_file_path
            video_name = os.path.basename(self.selected_file_path)
        else:
            video_path = None
            video_name = ""
        
        # 4. Dismiss splash once GUI is up
        if splash_proc is not None:
            try:
                splash_proc.terminate()
            except Exception as e:
                logging.info(f"Could not terminate splash screen: {e}")
                

        # Add Left Frame (Folder Tree) to PanedWindow
        self.paned_window.add(self.left_frame)

        #folder tree
        self.tree_style = ttk.Style()
        self.tree_style.theme_use("clam")

        style = ttk.Style()
        style.theme_use("clam")

        # Remove tree border via custom ttk layout
        style.layout("NoBorder.Treeview", [('Treeview.treearea', {'sticky': 'nswe'})])
        style.map("Treeview", background=[("selected", self.TreeCursorColor)])  # soft dark gray
        style.map("Treeview", background=[("selected", "#2e2e2e")], foreground=[("selected", "#d0d0d0")])

        self.tree = ttk.Treeview(self.tree_frame, style="NoBorder.Treeview", show="tree")
        self.tree["show"] = "tree"  # This hides the column header row
        self._node_path_cache: dict[str, str] = {}   # normalized_path → tree item_id
        self._node_missing_cache: set[str] = set()   # paths confirmed NOT in tree (negative cache)
        
     
        # styling scrollbar for left tree
        # IMPORTANT: pack scrollbar BEFORE tree. If tree packs first with fill=BOTH+expand,
        # it steals full width and CTkScrollbar often ends up 0 px wide (wheel scroll works, bar "vanishes").
        self.tree_vsb = ctk.CTkScrollbar(
            self.tree_frame,
            orientation="vertical",
            command=self.tree.yview,
            fg_color="#1e1e1e",             # background of scrollbar track
            button_color="#242b33",         # color of the handle
            button_hover_color="#244459",   # handle on hover
            width=14                         # adjust as needed
        )
 
        self.tree_vsb.configure(command=self.on_tree_scrollbar)
        self.tree.configure(takefocus=False)
        self.tree_vsb.pack(side=ctk.RIGHT, fill=ctk.Y)
        self.tree.pack(side=ctk.LEFT, fill=ctk.BOTH, expand=True, padx=0, pady=0)
        self.tree.configure(yscrollcommand=self.tree_vsb.set)

        self.tree["columns"] = ("path")
        self.tree.column("path", width=0, stretch=ctk.NO)

        self.tree.bind('<<TreeviewOpen>>', self.open_node)
        self.tree.bind('<ButtonPress-1>', self.select_item)
        self.tree.bind('<ButtonPress-1>', self._dnd_mark_tree_press, add="+")

        self.tree.bind("<ButtonRelease-3>", lambda e: self.show_tree_context_menu(e, self.tree.identify_row(e.y)))

        self.tree.bind("<ButtonPress-2>", lambda e: self.start_drag(e, source_type="tree"))
        self.tree.bind("<ButtonRelease-2>", lambda e: [self.drop_item(e, copy_mode=False), self.end_drag(e)])
        self.tree.bind('<B2-Motion>', self.drag_motion_tree)
        self.tree.bind("<MouseWheel>", self.on_tree_scroll_event)
        self.tree.bind("<Shift-MouseWheel>", self.on_tree_scroll_event)
        self.tree.bind("<<TreeviewSelect>>", self.select_item)
    
        # Drag data placeholder
        self.drag_data = {"item_id": None, "path": None}
        self.bind("<MouseWheel>", self._on_mouse_wheel)
        self.bind("<Shift-MouseWheel>", self._on_shift_mouse_wheel)

        self.canvas.bind('<Button-1>', self.detect_canvas_click)
        self.canvas.bind("<Button-3>", self._on_thumbnail_canvas_empty_rmb, add="+")
        self.canvas.bind("<Enter>", lambda e: self.canvas.focus_set())
        self.canvas.bind("<Configure>", self._on_main_canvas_configure)

        self.canvas.bind("<Up>",   lambda e: self.move_selection("up", shift=(e.state & 0x0001), ctrl=(e.state & 0x0004)))
        self.canvas.bind("<Down>", lambda e: self.move_selection("down", shift=(e.state & 0x0001), ctrl=(e.state & 0x0004)))
        self.canvas.bind("<Left>", lambda e: self.move_selection("left", shift=(e.state & 0x0001), ctrl=(e.state & 0x0004)))
        self.canvas.bind("<Right>",lambda e: self.move_selection("right", shift=(e.state & 0x0001), ctrl=(e.state & 0x0004)))

        t2 = time.time()
        logging.info(f"[TIMER] Tree populate: {time.time() - t2:.2f}s")

        self.scrollbar = ctk.CTkScrollbar(
            self.frame,
            orientation="vertical",
            command=self.canvas.yview,
            fg_color="#1e1e1e",             # background of scrollbar track
            button_color="#242b33",         # color of the handle
            button_hover_color="#244459",   # handle on hover
            width=14                         # adjust as needed
        )

        self.scrollable_frame = ctk.CTkFrame(self.canvas, fg_color=self.BackroundColor) #self.BackroundColor
        # nove prehoz z displ visibl
        self.wide_folders_frame = ctk.CTkFrame(self.scrollable_frame, fg_color=self.thumbBGColor)  # self.thumbBGColor Container for wide folders
        self.regular_thumbnails_frame = ctk.CTkFrame(self.scrollable_frame, fg_color=self.thumbBGColor)  # self.thumbBGColorContainer for regular thumbnails

        # 3. Keep this block: updates vertical scrollregion (width is handled separately)
        #    Skip when virtual grid is active — it manages scrollregion itself.
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: (
                self.canvas.configure(scrollregion=self.canvas.bbox("all"))
                if not getattr(self, '_vg_active', False) else None
            )
        )
             
     
     
        self.scrollable_frame.bind("<Button-1>", self.print_widget_info)

        # Filler at top of scrollable_frame for top alignment
        self.filler = tk.Frame(self.scrollable_frame, height=1 ,   bg=self.BackroundColor   ) # bg=self.BackroundColor 

        self.filler.pack(side="top", fill="x")
        
        # Allow mouse wheel events to pass through to the canvas
        # self.filler.bindtags((self.canvas,))
        # Pass mouse wheel events directly to the canvas
        self.filler.bind("<MouseWheel>", self._on_mouse_wheel)
        self.filler.bind("<Shift-MouseWheel>", self._on_shift_mouse_wheel)
        self.filler.bind("<Button-3>", self._on_thumb_container_empty_rmb)

        self.scrollable_frame.bind("<Button-3>", self._on_thumb_container_empty_rmb)
        self.wide_folders_frame.bind("<Button-3>", self._on_thumb_container_empty_rmb)
        self.regular_thumbnails_frame.bind("<Button-3>", self._on_thumb_container_empty_rmb)

        # self.filler .bind('<Button-1>', self.detect_canvas_click)
        self.scrollable_frame_window_id = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        
        # self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.configure(yscrollcommand="")

        self.paned_window.add(self.right_frame)

        self.canvas.pack(side="left", fill=ctk.BOTH, expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.init_virtual_grid()

        self.current_video_window = None
        logging.info("[DEBUG] current_video_window closed/set to None")
        self.selected_thumbnail = None
        self.cap = None
        self.hover_popup = None
        self.hover_label = None
        
     
        self.after(500, self.toggle_fullscreen)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.playlist_manager = PlaylistManager(self, self)
        self.bind_global_keys()
        t3 = time.time()
        self.load_virtual_libraries()
        logging.info(f"[TIMER] Load virtual libraries: {time.time() - t3:.2f}s")
        
        t4 = time.time()
        self.load_preferences()
        logging.info(f"[TIMER] load preferences: {time.time() - t4:.2f}s")

        # Apply matching widget_scale to the tree
        if self.current_dpi_scale > 1.5:
            initial_widget_scale = 1.2
        else:
            initial_widget_scale = 0.9
        
        self.update_treeview_scaling(initial_widget_scale)
        end_time = time.time()  # ⬅ stop timer
        logging.info(f"[TIMER] Full __init__ took {end_time - start_time:.2f} seconds")

        self.filler.update_idletasks()
        logging.info("[DEBUG] Filler exists: %s", self.filler.winfo_exists())
        logging.info("[DEBUG] Filler size: %s x %s", self.filler.winfo_width(), self.filler.winfo_height())
        logging.info("[DEBUG] Filler position: %s, %s", self.filler.winfo_x(), self.filler.winfo_y())
        
        self.after(400, self.initialize_gui_content)  # defer heavy init        
        self.after(1000, self.set_initial_split_heights)

        self._setup_dnd()


    def auto_tag_selected_items(self, clicked_path=None, *args, **kwargs):
        """
        Processes all currently selected thumbnails (images and videos) sequentially.
        If no multiple selection exists, it falls back to the specifically right-clicked item.
        Runs in a single background thread to prevent resource clashes.
        Updates the UI strictly following status bar guidelines.
        """
        selected_paths = []

        # 1. Try to get globally selected items first (multiple selection)
        if hasattr(self, 'selected_thumbnails') and self.selected_thumbnails:
            selected_paths = [item[0] for item in self.selected_thumbnails if not os.path.isdir(item[0])]

        # 2. If nothing was explicitly selected, but we clicked on a specific file in the menu
        if not selected_paths and clicked_path and not os.path.isdir(clicked_path):
            selected_paths = [clicked_path]

        # 3. If STILL nothing, show the info box and abort
        if not selected_paths:
            messagebox.showinfo("Info", "No items selected to tag.")
            return

        plugin_name = self.get_plugin_name_for_engine()
        plugin = self.plugin_manager.get_plugin(plugin_name)
        if not plugin:
            messagebox.showerror("Error", f"Plugin '{plugin_name}' not found.")
            return

        def batch_worker():
            # --- UI UPDATE: Standard prefix ---
            self.after(0, lambda: self.status_bar.set_action_message("Processing selected files..."))
            
            for idx, file_path in enumerate(selected_paths, start=1):
                if getattr(self, "stop_requested", False):
                    logging.info("STOP requested — exiting batch autotag")
                    # --- UI UPDATE: Abort message ---
                    self.after(0, lambda: self.status_bar.set_action_message("Tagging aborted."))
                    break

                # --- UI UPDATE: Concise progress message ---
                self.after(0, lambda i=idx, t=len(selected_paths): self.status_bar.set_action_message(f"Processing selected: {i}/{t}"))
                
                ext = os.path.splitext(file_path)[1].lower()
                
                try:
                    if ext in VIDEO_FORMATS:
                        # CRITICAL: Call video function synchronously (run_in_thread=False)
                        self.auto_tag_video_with_plugin(file_path, run_in_thread=False)
                        
                    elif ext in IMAGE_FORMATS:
                        result = plugin.run(file_path)
                        tags = result.get("tags", [])
                        if tags:
                            final_tags = ", ".join(tags)
                            self.database.update_keywords(file_path, final_tags)
                            # Update UI immediately for this thumbnail
                            self.after(0, lambda p=file_path: self.refresh_single_thumbnail(p, True))
                            logging.info("[OK] %s: %s", os.path.basename(file_path), final_tags)
                    else:
                        logging.warning(f"File type {ext} not supported: {file_path}")
                        
                except Exception as e:
                    logging.error(f"[AutoTag] Error processing {file_path}: {e}")

            # Finalize only if not aborted
            if not getattr(self, "stop_requested", False):
                self.after(0, lambda: self.status_bar.set_action_message("Tagging completely finished!"))
                
            # Clean up UI
            self.after(3000, self.status_bar.clear_action_message)
            self.after(0, self.status_bar.disable_stop)

        # Start one master thread for the whole batch
        threading.Thread(target=batch_worker, daemon=True).start()

    def auto_tag_with_plugin(self, plugin_name):
        # Lazy import tagging engine (only when needed)
        if plugin_name == "generate_tags_ilektra":
            from generate_tags_ilektra import TaggingSettings

        folder = filedialog.askdirectory(title="Select Folder to Auto-Tag")
        if not folder:
            return

        plugin = self.plugin_manager.get_plugin(plugin_name)
        if not plugin:
            messagebox.showerror("Error", f"Plugin '{plugin_name}' not found.")
            return

        ignored_dirs = {"img_metadata", "__pycache__", ".git", "outputs", "metadata", "cache"}

        image_paths = []

        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in ignored_dirs]

            for file in files:
                if file.lower().endswith((".jpg", ".jpeg", ".png")):
                    image_paths.append(os.path.join(root, file))

        if not image_paths:
            messagebox.showinfo("Info", "No images found in folder.")
            return

        def tag_worker():
            logging.info(f"[{plugin_name}] Tagging {len(image_paths)} images...")
            for path in image_paths:
                result = plugin.run(path)
                tags = result.get("tags", [])
                if tags:
                    self.database.update_keywords(path, ", ".join(tags))
                    normalized_path = os.path.normcase(os.path.normpath(path))

                    self.after(0, lambda p=normalized_path: self.refresh_single_thumbnail(p))

                    logging.info("[OK] %s: %s", os.path.basename(path), tags)

            self.after(0, lambda: messagebox.showinfo("Done", f"{plugin_name} tagged {len(image_paths)} images."))

        threading.Thread(target=tag_worker, daemon=True).start()

    
    def update_tree_view(self, source_path, target_path):
        """
        Update the tree view after moving a folder.
        """
        # Find the source node
        source_node = self.find_node_by_path(source_path)
        logging.info(f"DEBUG: Source node for delete = {source_node}, Source path = {source_path}")

        if source_node:
            self.tree.delete(source_node)  # Remove the item from its original location
            logging.info(f"Removed source node: {source_path}")
        else:
            logging.info(f"WARNING: Source node not found for {source_path}")

        # Refresh the target node
        target_node = self.find_node_by_path(target_path)
        logging.info(f"DEBUG: Target node for refresh = {target_node}, Target path = {target_path}")

        if target_node:
            self.process_directory(target_node, target_path)  # Refresh the target directory
            logging.info(f"Refreshed target node: {target_path}")
        else:
            logging.info(f"WARNING: Target node not found for {target_path}")
        

    
    
    def create_new_folder(self, parent_path=None):
        if not parent_path:
            parent_path = getattr(self, "current_directory", None)
            if not parent_path:
                self.show_error_message(title="Error", message="No parent path available to create the folder.")
                return

        def confirm_create(folder_name):
            if not folder_name.strip():
                self.show_error_message(title="Error", message="Folder name cannot be empty!")
                return

            new_folder_path = os.path.join(parent_path, folder_name.strip())
            if os.path.exists(new_folder_path):
                self.show_error_message(title="Error", message="Folder already exists!")
                return

            try:
                os.mkdir(new_folder_path)
                logging.info(f"Created new folder: {new_folder_path}")
                self.update_tree_view(new_folder_path, parent_path)
                self.display_thumbnails(self.current_directory)
            except Exception as e:
                self.show_error_message(title="Error", message=f"Could not create folder: {e}")

        self.universal_dialog(
            title="Create New Folder",
            message="Enter folder name:",
            confirm_callback=confirm_create,
            input_field=True
        )


     
    def rename_item(self, old_path):
        def confirm_rename(new_name):
            new_name = new_name.strip()
            if not new_name:
                self.show_error_message(title="Error", message="Name cannot be empty.")
                return
            if not old_path or not os.path.exists(old_path):
                self.show_error_message(
                    title="Rename failed",
                    message=f"The original item no longer exists:\n{old_path}",
                )
                return
            if new_name == os.path.basename(old_path):
                return

            new_path = os.path.join(os.path.dirname(old_path), new_name)
            if os.path.exists(new_path):
                self.show_error_message(
                    title="Error",
                    message=f"An item named '{new_name}' already exists.",
                )
                return

            try:
                os.rename(old_path, new_path)
                logging.info("Renamed: %s -> %s", old_path, new_path)

                try:
                    if os.path.isdir(new_path):
                        self.database.update_folder_path(old_path, new_path)
                    else:
                        self.database.update_folder_path(old_path, new_path)
                except Exception as db_err:
                    logging.warning("Rename: database update failed: %s", db_err)

                self.update_tree_view(old_path, os.path.dirname(new_path))

                if os.path.normcase(self.current_directory) == os.path.normcase(old_path):
                    self.current_directory = new_path

                self.display_thumbnails(self.current_directory, force_refresh=True)

            except Exception as e:
                logging.error("Rename failed: %s -> %s: %s", old_path, new_path, e)
                self.show_error_message(title="Rename failed", message=str(e))

        self.universal_dialog(
            title="Rename",
            message=f"New name for:\n{os.path.basename(old_path)}",
            confirm_callback=confirm_rename,
            input_field=True,
            default_input=os.path.basename(old_path)
        )


    
    def universal_dialog(
        self,
        title,
        message,
        confirm_callback=None,
        cancel_callback=None,
        third_button=None,
        third_callback=None,
        input_field=False,
        default_input="",
        confirm_text="Confirm",
        cancel_text="Cancel",
        show_cancel=True
    ):
        """
        Create a universal dialog window for confirmation, error, or input.

        Args:
            title (str): The title of the dialog.
            message (str): The main message or question for the user.
            confirm_callback (function): The function to call when the user confirms (optional).
            cancel_callback (function): The function to call when the user cancels (optional).
            third_button (str): Label for the third button (optional).
            third_callback (function): The function to call when the third button is clicked (optional).
            input_field (bool): Whether the dialog requires an input field (default is False).
            default_input (str): The default value for the input field if enabled.
        """
        dialog_window = ctk.CTkToplevel(self)
        dialog_window.title(title)
        self._center_toplevel_window(dialog_window, 400, 200)
        dialog_window.resizable(False, False)

        # Bring the dialog to the front
        dialog_window.attributes('-topmost', True)
        dialog_window.grab_set()

        # Display the message
        label = ctk.CTkLabel(dialog_window, text=message, wraplength=350, anchor="w", justify="left")
        label.pack(padx=10, pady=10)

        # Add input field if required
        input_var = ctk.StringVar(value=default_input) if input_field else None
        if input_field:
            input_entry = ctk.CTkEntry(dialog_window, textvariable=input_var)
            input_entry.pack(padx=10, pady=5)

        # Confirm button
        def on_confirm():
            if confirm_callback:
                if input_field:
                    confirm_callback(input_var.get())  # Pass the input value
                else:
                    confirm_callback()
            if dialog_window.winfo_exists():
                dialog_window.destroy()

        btn_confirm = None
        if confirm_callback is not None:
            btn_confirm = ctk.CTkButton(dialog_window, text=confirm_text, command=on_confirm)
            btn_confirm.pack(side="left", padx=10, pady=10)

        # Third button
        if third_button and third_callback:
            def on_third():
                third_callback()
                dialog_window.destroy()

            btn_third = ctk.CTkButton(dialog_window, text=third_button, command=on_third)
            btn_third.pack(side="left", padx=10, pady=10)

        # Cancel button
        btn_cancel = None
        if show_cancel:
            if cancel_callback:
                def on_cancel():
                    cancel_callback()
                    if dialog_window.winfo_exists():
                        dialog_window.destroy()
                btn_cancel = ctk.CTkButton(dialog_window, text=cancel_text, command=on_cancel)
                btn_cancel.pack(side="right", padx=10, pady=10)
            else:
                btn_cancel = ctk.CTkButton(
                    dialog_window,
                    text=cancel_text,
                    command=lambda: dialog_window.winfo_exists() and dialog_window.destroy(),
                )
                btn_cancel.pack(side="right", padx=10, pady=10)

        # Keyboard shortcuts for all universal dialogs:
        # Enter = confirm, Escape = cancel
        dialog_window.bind("<Return>", lambda e: on_confirm() if btn_confirm is not None else None)
        if btn_cancel is not None:
            dialog_window.bind("<Escape>", lambda e: btn_cancel.invoke())

        # Better UX for input dialogs: focus + select all text
        if input_field:
            def _safe_focus_input():
                try:
                    if not dialog_window.winfo_exists():
                        return
                    if not input_entry.winfo_exists():
                        return
                    input_entry.focus_set()
                    input_entry.select_range(0, "end")
                    input_entry.icursor("end")
                except tk.TclError:
                    pass
            dialog_window.after(25, _safe_focus_input)

    def prepare_file_deletion_release_handles(self, file_path: str) -> None:
        """
        Before os.remove on Windows: stop preview/main VLC if needed, wait for timeline
        ffmpeg worker, drop duration + in-memory thumbnail cache entries for this path.
        """
        def _n(p):
            try:
                return os.path.normcase(os.path.normpath(os.path.abspath(p)))
            except Exception:
                return os.path.normcase(os.path.normpath(p))

        target = _n(file_path)
        is_video = isinstance(file_path, str) and file_path.lower().endswith(VIDEO_FORMATS)

        # Block deferred thumb/preview work from starting VLC while we tear down handles.
        for attr in ("_preview_timer", "_click_timer"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass
                setattr(self, attr, None)

        ip = getattr(self, "info_panel", None)
        if ip and is_video:
            try:
                # Always stop embedded preview VLC for video deletes — path checks miss races
                # (pending thread, stale video_path) and stop_video() does not release Media.
                ip.stop_video_preview()
            except Exception as e:
                logging.debug("[Delete] preview release: %s", e)
            if os.name == "nt":
                time.sleep(0.35)

        cv = getattr(self, "current_video_window", None)
        if cv and getattr(cv, "video_path", None) and _n(cv.video_path) == target:
            try:
                cv.cleanup()
            except Exception:
                pass
            self.current_video_window = None

        tw = getattr(self, "timeline_widget", None)
        tw_useful = (
            getattr(self, "ShowTWidget", False)
            and tw is not None
            and getattr(tw, "winfo_exists", lambda: False)()
        )
        try:
            tw_visible = tw_useful and tw.winfo_viewable()
        except tk.TclError:
            tw_visible = False
        if (
            tw_visible
            and getattr(tw, "video_path", None)
            and _n(getattr(tw, "video_path", "")) == target
        ):
            wt = getattr(tw, "worker_thread", None)
            if wt is not None and wt.is_alive():
                wt.join(timeout=4.0)

        # Virtual grid: async file/folder thumbnail workers may keep the file open (FFmpeg/OpenCV).
        def _vgrid_pending_blocks_delete() -> bool:
            if not getattr(self, "_vg_active", False):
                return False
            pending = getattr(self, "_vg_pending_gen", None)
            if not pending:
                return False
            try:
                abs_file = os.path.normcase(os.path.normpath(os.path.abspath(file_path)))
            except Exception:
                abs_file = target
            for p in list(pending):
                pn = _n(p)
                if pn == target:
                    return True
                try:
                    if os.path.isdir(p):
                        ap = os.path.normcase(os.path.normpath(os.path.abspath(p)))
                        common = os.path.normcase(
                            os.path.normpath(os.path.commonpath([ap, abs_file]))
                        )
                        if common == ap:
                            return True
                except (ValueError, OSError):
                    continue
            return False

        if _vgrid_pending_blocks_delete():
            deadline = time.time() + 22.0
            while _vgrid_pending_blocks_delete() and time.time() < deadline:
                time.sleep(0.05)

        try:
            discard_duration_cache_entry(file_path)
        except Exception:
            pass

        tc = getattr(self.thumbnail_cache, "cache", None)
        if tc:
            try:
                for key in list(tc.keys()):
                    base = str(key).split("\x00", 1)[0]
                    if _n(base) == target:
                        tc.pop(key, None)
            except Exception:
                pass

        gc.collect()

 
    def confirm_delete_item(self, item_ids=None, paths=None):
        """
        Confirm and delete files/folders; refresh grid, tree, DB, and folder watcher.
        """
        if not paths:
            logging.debug("[Delete] No paths passed.")
            return

        paths = [p for p in paths if p and os.path.exists(p)]
        if not paths:
            logging.info("[Delete] All paths invalid or already gone.")
            return

        logging.info("[Delete] Paths to delete: %s", paths)

        def _norm_sel_path(p):
            try:
                return os.path.normcase(os.path.normpath(os.path.abspath(p)))
            except Exception:
                return os.path.normcase(os.path.normpath(p))

        def _prune_selection_after_paths_removed(removed_paths):
            dead = {_norm_sel_path(p) for p in removed_paths}
            if not dead:
                return
            st = getattr(self, "selected_thumbnails", None) or []
            self.selected_thumbnails = [
                t
                for t in st
                if isinstance(t, (list, tuple))
                and len(t) > 0
                and _norm_sel_path(str(t[0])) not in dead
            ]
            sfp = getattr(self, "selected_file_path", None)
            if sfp and _norm_sel_path(sfp) in dead:
                self.selected_file_path = None

        def _prune_selection_after_dir_removed(dir_path):
            nd = _norm_sel_path(dir_path)
            pref = nd + os.sep
            st = getattr(self, "selected_thumbnails", None) or []
            self.selected_thumbnails = [
                t
                for t in st
                if isinstance(t, (list, tuple))
                and len(t) > 0
                and _norm_sel_path(str(t[0])) != nd
                and not _norm_sel_path(str(t[0])).startswith(pref)
            ]
            sfp = getattr(self, "selected_file_path", None)
            if sfp:
                ns = _norm_sel_path(sfp)
                if ns == nd or ns.startswith(pref):
                    self.selected_file_path = None

        n = len(paths)
        if n == 1:
            detail = os.path.basename(paths[0])
        elif n <= 5:
            detail = "\n".join(f"  {os.path.basename(p)}" for p in paths)
        else:
            detail = "\n".join(f"  {os.path.basename(p)}" for p in paths[:4])
            detail += f"\n  … and {n - 4} more"
        message = f"Delete {n} item(s)?\n\n{detail}"

        def purge_cache_for_deleted_path(path: str):
            """Best-effort: remove disk and memory cache for a deleted path."""
            try:
                cache_root = self.thumbnail_cache_path
                abs_path = os.path.abspath(path)
                rel = abs_path.replace(":", "")
                cache_path = os.path.join(cache_root, rel)

                if os.path.isdir(path):
                    if os.path.isdir(cache_path):
                        shutil.rmtree(cache_path, ignore_errors=True)
                else:
                    cache_dir = os.path.dirname(cache_path)
                    cache_base = os.path.basename(cache_path)
                    if os.path.isdir(cache_dir):
                        for fn in os.listdir(cache_dir):
                            if fn.startswith(cache_base):
                                try:
                                    os.remove(os.path.join(cache_dir, fn))
                                except Exception:
                                    pass

                try:
                    if hasattr(self, "thumbnail_cache") and hasattr(self.thumbnail_cache, "cache"):
                        self.thumbnail_cache.cache.pop(path, None)
                        self.thumbnail_cache.cache.pop(os.path.normcase(os.path.normpath(path)), None)
                except Exception:
                    pass

                # DB cache status flag
                try:
                    self.database.update_cache_status(path, False)
                except Exception:
                    pass
            except Exception as e:
                logging.warning(f"[Delete] Cache purge failed for {path}: {e}")

        def delete_items():
            if self.watchdog_observer and self.watchdog_observer.is_alive():
                try:
                    self.watchdog_observer.stop()
                    self.watchdog_observer.join(timeout=1)
                except RuntimeError as e:
                    logging.warning(f"Watcher join error: {e}")
            delay_ms = 100
            if any(
                isinstance(p, str) and p.lower().endswith(VIDEO_FORMATS)
                for p in paths
            ):
                delay_ms = 550
            self.after(delay_ms, perform_deletion)

        def _windows_schedule_delete_on_reboot(path: str) -> bool:
            """Last resort: file still locked by OS/AV — remove at next boot."""
            if os.name != "nt":
                return False
            try:
                import ctypes
                MOVEFILE_DELAY_UNTIL_REBOOT = 4
                p = os.path.normpath(path)
                return bool(ctypes.windll.kernel32.MoveFileExW(p, None, MOVEFILE_DELAY_UNTIL_REBOOT))
            except Exception:
                return False

        def _unlink_file_with_retry(path: str, *, is_video: bool = False):
            """Return None if removed now, 'reboot' if queued for next Windows start."""
            if os.name == "nt" and os.path.isfile(path):
                try:
                    import stat
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
            last_err = None
            attempts = 28 if is_video else 12
            delay = 0.2 if is_video else 0.12
            for attempt in range(attempts):
                try:
                    os.remove(path)
                    return None
                except PermissionError as e:
                    last_err = e
                except OSError as e:
                    last_err = e
                    w = getattr(e, "winerror", None)
                    if w not in (32, 5) and e.errno not in (13, 11):
                        raise
                gc.collect()
                if attempt + 1 < attempts:
                    time.sleep(delay)
            if last_err:
                if os.name == "nt" and _windows_schedule_delete_on_reboot(path):
                    logging.warning(
                        "[Delete] File locked; scheduled removal at next Windows restart: %s", path
                    )
                    return "reboot"
                raise last_err
            return None

        def perform_deletion():
            errors = []
            reboot_queued = []
            current_dir_deleted = False
            parent_of_deleted = None

            # Deleting a tree node moves Treeview selection (often to the next sibling folder).
            # <<TreeviewSelect>> then debounces display_thumbnails() and overrides our refresh
            # of the current folder — suppress navigation until deletes finish.
            if getattr(self, "_debounce_job", None) is not None:
                try:
                    self.after_cancel(self._debounce_job)
                except (tk.TclError, ValueError):
                    pass
                self._debounce_job = None

            self._suppress_tree_select_navigation = True
            try:
                for path in paths:
                    try:
                        if self.current_directory and \
                           Path(self.current_directory).resolve() == Path(path).resolve():
                            current_dir_deleted = True
                            parent_of_deleted = str(Path(path).parent.resolve())

                        if os.path.isdir(path):
                            shutil.rmtree(path)
                            logging.info("[Delete] Deleted: %s", path)
                            _prune_selection_after_dir_removed(path)
                        else:
                            self.prepare_file_deletion_release_handles(path)
                            gc.collect()
                            how = _unlink_file_with_retry(
                                path, is_video=path.lower().endswith(VIDEO_FORMATS)
                            )
                            if how == "reboot":
                                reboot_queued.append(path)
                                logging.info("[Delete] Removal deferred until restart: %s", path)
                            else:
                                logging.info("[Delete] Deleted: %s", path)

                        purge_cache_for_deleted_path(path)

                        node_id = self.find_node_by_path(path)
                        if node_id:
                            self.tree.delete(node_id)

                        try:
                            self.database.remove_entry(path)
                        except Exception:
                            pass

                        _prune_selection_after_paths_removed([path])

                    except Exception as e:
                        errors.append(path)
                        logging.error("[Delete] Failed to delete %s: %s", path, e)
            finally:
                self._suppress_tree_select_navigation = False

            if current_dir_deleted:
                dest = parent_of_deleted if parent_of_deleted and os.path.isdir(parent_of_deleted) \
                       else self.default_directory
                self.current_directory = dest
                self.display_thumbnails(dest, force_refresh=False)
                self.add_to_recent_directories(dest)
                parent_node = self.find_node_by_path(dest)
                if parent_node:
                    self.process_directory(parent_node, dest)
            else:
                if self.current_directory and os.path.isdir(self.current_directory):
                    self.display_thumbnails(self.current_directory, force_refresh=False)
                    self.refresh_folder_icons_subtree(self.current_directory)
                    self.after_idle(self.select_current_folder_in_tree)

            if self.current_directory and os.path.isdir(self.current_directory):
                self.start_directory_watcher(self.current_directory)

            if errors:
                names = "\n".join(os.path.basename(p) for p in errors[:5])
                self.show_error_message(
                    title="Delete failed",
                    message=f"Could not delete:\n{names}",
                )
            elif reboot_queued:
                names = "\n".join(os.path.basename(p) for p in reboot_queued[:5])
                messagebox.showinfo(
                    "Delete queued for restart",
                    "These files are still locked by Windows or another program. "
                    "They were scheduled for removal at the next PC restart:\n\n" + names,
                )

        self.universal_dialog(
            title="Confirm delete",
            message=message,
            confirm_callback=delete_items
        )



    def print_widget_info(self, event):
        widget = event.widget
        logging.info(f"Widget: {widget}, Type: {type(widget)}, ID: {widget.winfo_id()}")

        
    def _is_child_of(self, widget, parent):
        """Return True if widget is parent or child of given parent frame."""
        while widget:
            if widget == parent:
                return True
            widget = widget.master
        return False
        

        
    def _on_mouse_wheel(self, event):
        """
        Scrolls the thumbnail canvas only when the cursor is over
        the right panel (canvas or its child widgets).
        """
        if getattr(self, '_vg_active', False):
            widget_under_cursor = self.winfo_containing(event.x_root, event.y_root)
            if widget_under_cursor and (
                widget_under_cursor == self.canvas
                or self._is_child_of(widget_under_cursor, self.canvas)
            ):
                self._vg_on_mousewheel(event)
            return

        widget_under_cursor = self.winfo_containing(event.x_root, event.y_root)
        if widget_under_cursor and (
            widget_under_cursor == self.canvas or
            self._is_child_of(widget_under_cursor, self.scrollable_frame)
        ):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_shift_mouse_wheel(self, event):
        """
        Horizontal scroll (Shift + wheel) with the same panel detection.
        """
        widget_under_cursor = self.winfo_containing(event.x_root, event.y_root)
        if widget_under_cursor and (
            widget_under_cursor == self.canvas or
            self._is_child_of(widget_under_cursor, self.scrollable_frame)
        ):
            self.canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")

    def _update_status(self):
        """
        Continuously updates the status bar information in a separate thread.
        This function also cleans up the list of selected thumbnails by removing
        entries for files that no longer exist.
        """
        while True:
            try:
                # --- Defensive cleanup of selected_thumbnails ---
                cleaned_thumbnails = []
                for thumb in self.selected_thumbnails:
                    # We must validate the data structure BEFORE trying to access it.
                    # We expect 'thumb' to be a list or tuple with at least one element (the path).
                    
                    # Check 1: Is 'thumb' a list or tuple?
                    if isinstance(thumb, (list, tuple)):
                        
                        # Check 2: Is it non-empty?
                        if len(thumb) > 0:
                            
                            # Check 3: Does the file path (thumb[0]) actually exist?
                            # We cast to str() just in case it's not a string, os.path.exists is flexible.
                            if os.path.exists(str(thumb[0])):
                                cleaned_thumbnails.append(thumb)
                                
                        # else: thumb is an empty list/tuple, e.g., [], so we drop it.
                    # else: thumb is not a list/tuple (e.g., it's a dict or None), so we drop it.
                    # This check securely filters out the bad data that caused the KeyError.
                
                self.selected_thumbnails = cleaned_thumbnails
                # --- End of cleanup ---

                # calculate folder, file counts, and sizes
                folder_count, file_count, total_size = self.status_bar.count_folders_and_files(self.current_directory)
                # This count_selected_files_and_size might also fail if it expects 3-tuples
                # but the cleanup above doesn't enforce the length of 3.
                selected_count, selected_size = self.status_bar.count_selected_files_and_size(self.selected_thumbnails)

                # update the queue with refreshed status
                self.status_queue.put((folder_count, file_count, total_size, selected_count, selected_size))
            
            except Exception as e:
                # Log any other unexpected error in this thread to prevent a silent crash
                logging.exception("Error in _update_status thread: %s", e)

            time.sleep(1)  # adjust the sleep interval if needed



    def check_status_queue(self):
        try:
            while not self.status_queue.empty():
                folder_count, file_count, total_size, selected_count, selected_size = self.status_queue.get_nowait()
                self.status_bar.update_status(folder_count, file_count, total_size, selected_count, selected_size)
        except queue.Empty:
            pass
        self.after(100, self.check_status_queue)  # Check the queue every 100 ms

    def update_status_bar(self):
        # Ensure the queue checking loop is running
        self.check_status_queue()
                    
    def delete_thumbnail_cache(self, path):
            """
            Safely deletes the thumbnail cache directory for a given path.
            Handles Windows PermissionErrors by forcing garbage collection and 
            using a robust rmtree handler.
            """
            import gc
            import shutil

            def on_rm_error(func, path, exc_info):
                """Error handler for shutil.rmtree to handle read-only or locked files."""
                import os, stat
                try:
                    os.chmod(path, stat.S_IWRITE)
                    func(path)
                except Exception as e:
                    logging.error(f"[Cache] Could not delete {path}: {e}")

            try:
                # 1. Clear memory references
                if hasattr(self, 'thumbnail_cache'):
                    self.thumbnail_cache.clear()
                
                # 2. Force Python to release file handles held by PIL or other objects
                gc.collect()

                # 3. Get the exact cache directory path
                cache_dir_path, full_path = get_cache_dir_path(path, self.thumbnail_cache_path)

                if os.path.exists(full_path):
                    # Using onerror handler to deal with Windows file locks/permissions
                    shutil.rmtree(full_path, onerror=on_rm_error)
                    logging.info(f"Deleted cache directory: {full_path}")
                else:
                    logging.info(f"Cache directory does not exist: {full_path}")

                # 4. Cleanup database
                self.remove_database_entries_for_folder(path)
                self.refresh_folder_icons_subtree(path)
                
                logging.info("Thumbnail cache and database entries deleted successfully.")
            except Exception as e:
                logging.error(f"Error during delete_thumbnail_cache for {path}: {e}")


    def remove_database_entries_for_folder(self, folder_path):
        try:
            # Loop through all items in the folder and remove database entries
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    self.database.remove_entry(file_path)
                    # logging.info(f"Removed database entry for {file_path}")
                for dir in dirs:
                    dir_path = os.path.join(root, dir)
                    self.database.remove_entry(dir_path)
                    # logging.info(f"Removed database entry for {dir_path}")
                    
            # Finally, remove the entry for the folder itself
            self.database.remove_entry(folder_path)
            # logging.info(f"Removed database entry for {folder_path}")
        except Exception as e:
            logging.info(f"Error removing database entries for folder {folder_path}: {e}")


    def get_file_path_from_item_id(self, item_id):
        """
        Resolve the file path from a tree item ID, using the hash if available.
        
        Args:
            item_id (str): The ID of the tree item.

        Returns:
            str: The resolved file path, or None if not found.
        """
        if not item_id:
            logging.info("DEBUG: No item_id provided.")
            return None

        item_values = self.tree.item(item_id, 'values')
        if item_values and len(item_values) > 1:  # Assuming the second value is the hash
            path_hash = item_values[1]
            logging.info(f"DEBUG: Retrieved hash for item_id {item_id}: {path_hash}")
            return self.find_path_by_hash(path_hash)  # Resolve the hash back to the path
        elif item_values and len(item_values) > 0:  # Fallback to the first value if no hash
            logging.info(f"DEBUG: Using raw path for item_id {item_id}: {item_values[0]}")
            return item_values[0]
        else:
            logging.info(f"DEBUG: item_id {item_id} has invalid or missing 'values': {item_values}")
            return None


    # Update the context menu to use the unified function
    def show_tree_context_menu(self, event, item_id=None):
        """
        Show the context menu for tree items.
        Allows operations like rename, delete, and others on tree items.
        
        Args:
            event: The triggered event for the context menu.
            item_id: The tree item ID (optional; used for folder-specific operations).
        """
        # Determine the file/folder path from the tree
        # file_path = self.tree.item(item_id, 'values')[0] if item_id else None
        file_path = self.get_file_path_from_item_id(item_id)

     # Debug the resolved file path
        logging.info(f"DEBUG: Resolved file_path for context menu: {file_path}")

        if not file_path:
            logging.info("DEBUG: No valid file path resolved for the context menu.")
            return

        menu = tk.Menu(self, tearoff=0)

        # Add context menu options
        # menu.add_command(label="Add Keywords", command=lambda: self.open_keyword_window(file_path))
        # menu.add_command(label="Remove Keywords", command=lambda: self.open_remove_keyword_window(file_path))
        menu.add_command(label="Scan Thumbnails in tree", command=lambda: self.scan_subtree(file_path))
        
        menu.add_command(label="Refresh Thumbnails in tree", command=lambda: self.refresh_thumbnails_in_subtree(file_path))
        
        menu.add_command(label="Refresh Wide Folders preview", command=lambda: self.refresh_folder_wide_thumbnail(file_path))
        menu.add_separator()
        
        # menu.add_command(label="Add to Existing Playlist", command=lambda: self.add_selected_to_playlist())
        # menu.add_command(label="Add to New Playlist", command=lambda: self.add_selected_to_playlist(event, new_playlist=True))
        # menu.add_command(label="Edit Rating", command=lambda: self.edit_rating(file_path))
        menu.add_command(label="Create New Folder", command=lambda: self.create_new_folder(file_path))
        menu.add_command(label="Rename", command=lambda: self.rename_item(file_path))

        menu.add_separator()
        menu.add_command(
            label="Copy",
            command=lambda fp=file_path: self.copy_tree_folder_path_to_clipboard(fp),
        )
        self.add_clipboard_paste_cascade(menu, file_path)

        # Add delete option using the updated `confirm_delete_item` function
        menu.add_command(
            label="Delete", command=lambda: self.confirm_delete_item(paths=[file_path])
        )
        


        menu.add_command(label="Create New Virtual Library", command=self.create_virtual_library)
        # === Optional plugin tagging ===
        if hasattr(self, "plugin_manager") and self.plugin_manager.plugins:
            menu.add_separator()
            menu.add_command(
                label="Auto Tag Folder",
                command=lambda: self.auto_tag_with_plugin_from_folder(file_path)
            )

        

        # Show the context menu at the mouse pointer location
        menu.tk_popup(event.x_root, event.y_root)

    def show_empty_thumbnail_view_context_menu(self, event):
        """Right-click on empty thumbnail area (no item under cursor): minimal menu (Paste)."""
        menu = tk.Menu(self, tearoff=0)
        self.add_clipboard_paste_cascade(menu, getattr(self, "current_directory", None))
        menu.tk_popup(event.x_root, event.y_root)

    def _on_thumbnail_canvas_empty_rmb(self, event):
        """Virtual grid: clicks on bare canvas (not on a slot window) reach here."""
        if not getattr(self, "_vg_active", False):
            return
        self.show_empty_thumbnail_view_context_menu(event)

    def _on_thumb_container_empty_rmb(self, event):
        """Legacy grid: empty space in scrollable / wide / regular frames (not on a thumb widget)."""
        if getattr(self, "_vg_active", False):
            return
        self.show_empty_thumbnail_view_context_menu(event)

    def _skip_file_clipboard_hotkey(self) -> bool:
        """Skip file clipboard shortcuts while typing in any Entry/Text (incl. main window)."""
        if self._is_input_focused():
            return True
        focused = self.focus_get()
        if focused is None:
            return False
        cls = focused.winfo_class()
        return cls in (
            "Entry",
            "Text",
            "TEntry",
            "TText",
            "CTkEntry",
            "CTkTextbox",
        )

    def _primary_path_for_file_clipboard(self):
        primary = getattr(self, "selected_file_path", None)
        if primary and os.path.exists(primary):
            return primary
        raw = getattr(self, "selected_thumbnails", None) or []
        if raw:
            first = raw[0]
            p = first[0] if isinstance(first, tuple) and first else first
            if p and os.path.exists(p):
                return p
        try:
            sel = self.tree.selection()
            if sel:
                p = self.get_file_path_from_item_id(sel[0])
                if p and os.path.exists(p):
                    return p
        except Exception:
            pass
        return None

    def hotkey_files_clipboard_copy(self, event=None):
        if self._skip_file_clipboard_hotkey():
            return
        primary = self._primary_path_for_file_clipboard()
        if not primary:
            self._clipboard_status_flash(
                "Nothing to copy — select a file, folder, or tree item.", 3500
            )
            return
        self.copy_thumb_paths_to_clipboard(primary)

    def hotkey_files_clipboard_paste_copy(self, event=None):
        if self._skip_file_clipboard_hotkey():
            return
        cd = getattr(self, "current_directory", None)
        if not cd or not os.path.isdir(cd):
            self._clipboard_status_flash("Open a folder in the thumbnail view first.", 3500)
            return
        if not clipboard_has_pastable_paths():
            self._clipboard_status_flash(
                "No files or folders on the clipboard to paste.", 3500
            )
            return
        self.paste_clipboard_into_folder(cd, True)

    def hotkey_files_clipboard_paste_move(self, event=None):
        if self._skip_file_clipboard_hotkey():
            return
        cd = getattr(self, "current_directory", None)
        if not cd or not os.path.isdir(cd):
            self._clipboard_status_flash("Open a folder in the thumbnail view first.", 3500)
            return
        if not clipboard_has_pastable_paths():
            self._clipboard_status_flash(
                "No files or folders on the clipboard to paste.", 3500
            )
            return
        self.paste_clipboard_into_folder(cd, False)

    # V souboru video_thumbnail_player.py


    def refresh_folder_wide_thumbnail(self, folder_path):
        """
        Forces a refresh of a wide folder thumbnail. 
        Refreshes the parent directory to prevent jumping into the subfolder.
        """
        target_height = self.widefolder_size[1]
        cache_dir_path, _ = get_cache_dir_path(folder_path, self.thumbnail_cache_path)
        
        _bn = os.path.basename(folder_path)
        _prefix = f"!folder_wide_{_bn}_"
        if os.path.isdir(cache_dir_path):
            for fn in os.listdir(cache_dir_path):
                if fn.startswith(_prefix) and fn.lower().endswith(".png"):
                    try:
                        os.remove(os.path.join(cache_dir_path, fn))
                        logging.info(f"[Refresh] Deleted cached wide thumbnail: {fn}")
                    except OSError as e:
                        logging.error(f"[Refresh] Failed to delete cache {fn}: {e}")

        parent_dir = os.path.dirname(folder_path)
        if os.path.isdir(parent_dir):
            logging.info(f"[Refresh] Reloading parent directory: {parent_dir}")
            self.display_thumbnails(parent_dir)
        else:
            self.display_thumbnails(self.current_directory)


    def refresh_thumbnails_in_subtree(self, folder_path):
        """
        Coordinates a full refresh by clearing UI widgets and memory cache 
        BEFORE attempting to delete files from disk to avoid PermissionErrors.
        """
        logging.info("Full subtree refresh: clearing UI and caches")

        if hasattr(self, 'stop_preview'):
            self.stop_preview()

        self.clear_thumbnails()

        if hasattr(self, 'thumbnail_cache'):
            self.thumbnail_cache.clear()

        gc.collect()
        logging.debug("UI and memory cache cleared before disk delete")

        self.delete_thumbnail_cache(folder_path)

        if hasattr(self, 'database'):
            self.database.update_cache_status(folder_path, False)
            logging.info(
                "Folder cache flag reset in DB: %s",
                os.path.basename(folder_path),
            )

        self.scan_subtree(folder_path, force_refresh=True)
        self.display_thumbnails(self.current_directory, force_refresh=True, thumbnail_time=self.thumbnail_time)

    # Extract the set_rating function outside of edit_rating
    def set_rating(self, rating):
        logging.info (f"rating was set to: {rating}")
        if not self.selected_thumbnails:
            logging.info("No thumbnails selected.")
            return
        for file_path, _, _ in self.selected_thumbnails:
            self.save_rating(file_path, rating)

    # Edit rating window
    def edit_rating(self, path):
        if not self.selected_thumbnails:
            logging.info("No thumbnails selected.")
            return

        rating_window = ctk.CTkToplevel(self)
        rating_window.title("Edit Rating")
        self._center_toplevel_window(rating_window, 600, 100)

        button_frame = ctk.CTkFrame(rating_window)
        button_frame.pack(pady=20)
        
        col = ["lightblue", "lightgreen", "yellow", "purple", "red"]
        
        for i in range(1, 6):
            rating_button = ctk.CTkButton(button_frame, width=32, height=32, corner_radius=2, fg_color=col[i-1], text=str(i), command=lambda i=i: self.set_rating( i))
            rating_button.pack(side=ctk.LEFT, padx=10)

    def save_rating(self, file_path, rating):
        try:
            self.database.update_rating(file_path, rating)
            self._wide_folder_stats_cache.clear()

            # Refresh the rating bar after saving the rating
            for stored_file_path, info in self.thumbnail_labels.items():
                if stored_file_path == file_path:
                    canvas = info["canvas"]
                    thumbnail_frame = canvas.master
                    canvas_width = self.thumbnail_size[0] + 14 * 2  # Adjust width based on existing logic

                    # Update rating display
                    self.add_rating_circle(file_path, thumbnail_frame, rating, canvas_width)
                    break
        except Exception as e:
            logging.info(f"Failed to save rating for {file_path}: {e}")


                                
    def create_virtual_library(self):
        """
        Prompts the user to enter a name for a new virtual library
        and creates it.
        """
        # Ask the user for the name of the new virtual library
        folder_name = simpledialog.askstring("New Virtual Library", "Enter name for the virtual library:")
        
        # If a name was provided (user didn't cancel)
        if folder_name:
            # Call the function to create the underlying folder/structure
            create_virtual_folder(folder_name)
            # Refresh the list of virtual libraries in the UI
            self.refresh_virtual_libraries()

    def add_to_virtual_library(self, thumbnails, library_name):
        # Extract file paths from the selected thumbnails
        logging.info(f"Adding to virtual library: {library_name}")
        logging.info(f"Selected thumbnails: {thumbnails}")
        
        file_paths = [thumbnail[0] for thumbnail in thumbnails if isinstance(thumbnail, tuple) and isinstance(thumbnail[0], str)]
        
        logging.info(f"File paths to add: {file_paths}")

        # Add each file path to the specified virtual library
        for file_path in file_paths:
            # logging.info(f"Adding file path: {file_path} to library: {library_name}")
            add_to_virtual_folder(library_name, file_path)
        
        logging.info(f"Finished adding to virtual library: {library_name}")
        
        # Refresh the virtual libraries in the UI
        self.refresh_virtual_libraries()
        logging.info("Refreshed virtual libraries in the UI.")

    def remove_from_virtual_library(self, thumbnails, library_name):
        # Extract file paths from the selected thumbnails
        file_paths = [thumbnail[0] for thumbnail in thumbnails if isinstance(thumbnail, tuple) and isinstance(thumbnail[0], str)]

        # Load existing virtual folders data
        data = load_virtual_folders()

        # Remove the selected files from the specified virtual library
        if library_name in data["virtual_folders"]:
            data["virtual_folders"][library_name] = [fp for fp in data["virtual_folders"][library_name] if fp not in file_paths]

            # Save the updated virtual folders data
            save_virtual_folders(data)

            # Refresh the virtual libraries in the UI
            self.refresh_virtual_libraries()
            logging.info(f"Removed selected files from virtual library: {library_name}")
            
             # Refresh the displayed thumbnails
            self.display_thumbnails(f"virtual_library://{library_name}")
        else:
            logging.info(f"Library {library_name} not found.")


    def delete_virtual_library(self, library_name):
        # Load existing virtual folders data
        data = load_virtual_folders()

        # Remove the specified virtual library
        if library_name in data["virtual_folders"]:
            del data["virtual_folders"][library_name]

            # Save the updated virtual folders data
            save_virtual_folders(data)

            # Refresh the virtual libraries in the UI
            self.refresh_virtual_libraries()
            logging.info(f"Deleted virtual library: {library_name}")
        else:
            logging.info(f"Library {library_name} not found.")



    def load_virtual_libraries(self):
        data = load_virtual_folders()
        for folder_name in data["virtual_folders"].keys():
            virtual_library_path = f"virtual_library://{folder_name}"
            self._tree_insert('', 'end', text=folder_name, image=self.folder_virtual_icon, values=(virtual_library_path,))

          

    def refresh_virtual_libraries(self):
        # Remove existing virtual libraries
        for item in self.tree.get_children():
            values = self.tree.item(item, 'values')
            if values and values[0].startswith("virtual_library://"):
                self.tree.delete(item)
        
        # Load virtual libraries again
        self.load_virtual_libraries()  

    def update_thumbnail_info(self):
        self.display_thumbnails(self.current_directory)

    def scan_subtree(self, folder_path, force_refresh=False, thumbnail_time=None):
            """
            Initiates a background thread to scan the entire subtree for media files.
            Resets the progress bar and submits the worker task to the executor.
            """
            logging.debug("Scheduling subtree scan for: %s", folder_path)
            self.status_bar.reset_progress()
            self.total_files_to_process = 0
            self.processed_files_count = 0
            self.status_bar.stop_scan_flag = False
            self.status_bar.enable_stop()
            
            def task_done(future):
                """Callback triggered when the worker thread finishes or raises an exception."""
                try:
                    future.result() 
                except Exception as e:
                    logging.error("Subtree scan worker failed: %s", e, exc_info=True)
                    import traceback
                    traceback.print_exc()

            # Passes thumbnail_time properly to the worker
            future = self.executor.submit(self._scan_subtree_worker, folder_path, force_refresh, thumbnail_time)
            future.add_done_callback(task_done)


    def _scan_subtree_worker(self, folder_path, force_refresh=False, thumbnail_time=None):
        """
        Worker function running in the executor.
        Scans directories recursively, queues thumbnails generation, 
        and updates the progress bar safely via the main GUI thread.
        Includes a semaphore constraint to prevent 'Too many open files' error.
        """
        video_count, image_count, _ = self.count_files_and_size_in_subtree(folder_path)
        total_files = video_count + image_count
        
        self.total_files_to_process = total_files
        
        if total_files == 0:
            self.after(0, self.status_bar.disable_stop)
            self.after(0, lambda: self.status_bar.set_action_message(f"Scan finished: {folder_path} (No files)"))
            logging.info("Subtree scan finished (no files): %s", folder_path)
            return

        current_progress = 0
        progress_increment = 100 / total_files if total_files > 0 else 0

        for root, dirs, files in os.walk(folder_path):
            if not force_refresh and self.database.is_folder_cached(root):
                # Update progress even for skipped cached folders to prevent freezing
                valid_files_count = len([f for f in files if f.lower().endswith(VIDEO_FORMATS) or f.lower().endswith(IMAGE_FORMATS)])
                current_progress += progress_increment * valid_files_count
                self.after(0, lambda p=current_progress: self.status_bar.update_progress(p))
                
                self.after(0, lambda r=root: self.refresh_folder_icon(r))
                continue

            current_folder_name = os.path.basename(root)
            self.after(0, lambda r=current_folder_name: self.status_bar.set_action_message(f"Scanning folder: {r}"))
            logging.debug("Scanning folder: %s", root)

            for file_name in files:
                while self.thumb_queue.qsize() > 50:
                    time.sleep(0.1)
                
                if self.status_bar.stop_scan_flag:
                    logging.info("Scan stopped by user.")
                    self.after(0, self.status_bar.disable_stop)
                    self.after(0, lambda: self.status_bar.set_action_message("Scan aborted."))
                    return

                file_path = os.path.join(root, file_name)
                
                if file_name.lower().endswith(VIDEO_FORMATS) or file_name.lower().endswith(IMAGE_FORMATS):
                    
                    # Keep subtree scan aligned with Preferences -> Thumbnail Time.
                    # If caller passes a percentage (0.0-1.0), convert to absolute seconds.
                    # If caller passes seconds (>1.0), use them directly.
                    actual_time_for_video = None
                    if file_name.lower().endswith(VIDEO_FORMATS):
                        if thumbnail_time is None:
                            actual_time_for_video = self.calculate_thumbnail_time(file_path)
                        else:
                            try:
                                tval = float(thumbnail_time)
                                if 0.0 <= tval <= 1.0:
                                    total_duration = get_video_duration_mediainfo(file_path)
                                    if total_duration and total_duration > 0:
                                        actual_time_for_video = min(total_duration * tval, total_duration - 0.1)
                                    else:
                                        actual_time_for_video = None
                                else:
                                    actual_time_for_video = tval
                            except Exception:
                                actual_time_for_video = self.calculate_thumbnail_time(file_path)
                    
                    self.queue_thumbnail(
                        file_path,
                        file_name,
                        None,
                        None,
                        None, 
                        is_folder=False,
                        thumbnail_time=actual_time_for_video, 
                        overwrite=force_refresh,
                        target_frame=None
                    )

                current_progress += progress_increment
                self.after(0, lambda p=current_progress: self.status_bar.update_progress(p))       

            self.database.update_cache_status(root, True)
            self.after(0, lambda r=root: self.refresh_folder_icon(r))

        self.after(0, lambda: self.refresh_folder_icon(folder_path))
        self.after(0, self.status_bar.disable_stop)
        self.after(0, lambda: self.status_bar.set_action_message("Scan completely finished!"))
        logging.info("Subtree scan finished: %s", folder_path)


    def update_thumbnail_time(self, value):
        self.thumbnail_time = value / 100  # Convert percentage back to decimal
        logging.info(f"Thumbnail time set to {self.thumbnail_time * 100}% of video duration")


    def contains_thumbnails(self, folder_path):
      #  """Check if the folder is marked as cached in the database."""
        return self.database.is_folder_cached(folder_path)

    # Ensures the correct cache directory is passed to prevent creating duplicate cache folders
    def measure_time_for_thumbnail(self, file_path, is_video):
        start_time = time.time()
        if is_video:
            thumbnail = create_video_thumbnail(
                file_path, 
                self.thumbnail_size,
                self.thumbnail_format, 
                self.capture_method_var.get(),
                cache_dir=self.thumbnail_cache_path  # explicit cache_dir
            )
            width, height = get_video_size(file_path)
        else:
            thumbnail = create_image_thumbnail(
                file_path, 
                self.thumbnail_size,
                self.thumbnail_format,
                cache_dir=self.thumbnail_cache_path  # explicit cache_dir
            )
            width, height = self.get_image_size(file_path)
            
        self.database.add_entry(os.path.basename(file_path), file_path, width, height)
        end_time = time.time()
        return end_time - start_time
    
    def count_files_and_size_in_subtree(self, dir_path):
        """
        Counts video/image files and their total size in a given directory and all subdirectories.
        Optimized with recursive os.scandir and entry.stat() to minimize disk I/O.
        """
        video_count = 0
        image_count = 0
        total_size = 0

        def scan_recursively(current_path):
            nonlocal video_count, image_count, total_size
            try:
                with os.scandir(current_path) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            scan_recursively(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            name_lower = entry.name.lower()
                            if name_lower.endswith(VIDEO_FORMATS):
                                video_count += 1
                                total_size += entry.stat(follow_symlinks=False).st_size
                            elif name_lower.endswith(IMAGE_FORMATS):
                                image_count += 1
                                total_size += entry.stat(follow_symlinks=False).st_size
            except (OSError, PermissionError):
                pass  # Ignore folders we cannot access

        scan_recursively(dir_path)
        return video_count, image_count, total_size / (1024 * 1024)  # Return size in MB
    
    
    def estimate_thumbnail_creation_time(self, dir_path):
        video_count, image_count, total_size = self.count_files_and_size_in_subtree(dir_path)

        # Measure time for a single video and image thumbnail creation
        sample_video_path = None
        sample_image_path = None

        for root, _, files in os.walk(dir_path):
            for file in files:
                file_path = os.path.join(root, file)
                if sample_video_path is None and file.lower().endswith(VIDEO_FORMATS):
                    sample_video_path = file_path
                if sample_image_path is None and file.lower().endswith(IMAGE_FORMATS):
                    sample_image_path = file_path
                if sample_video_path and sample_image_path:
                    break
            if sample_video_path and sample_image_path:
                break

        video_thumbnail_time = 0
        if sample_video_path:
            video_thumbnail_time = self.measure_time_for_thumbnail(sample_video_path, is_video=True)

        image_thumbnail_time = 0
        if sample_image_path:
            image_thumbnail_time = self.measure_time_for_thumbnail(sample_image_path, is_video=False)

        estimated_video_time = video_thumbnail_time * video_count
        estimated_image_time = image_thumbnail_time * image_count

        logging.info(f"Estimated time to create thumbnails for all videos: {estimated_video_time:.2f} seconds")
        logging.info(f"Estimated time to create thumbnails for all images: {estimated_image_time:.2f} seconds")
        logging.info(f"Total size of files in subtree: {total_size / (1024 * 1024):.2f} MB")

        return estimated_video_time, estimated_image_time, total_size


    def stop_scan(self):
        self.status_bar.stop_scan_flag = True  # Set the flag to indicate the scan should stop
    

     
    def setup_styles(self):
            ctk.set_appearance_mode("dark")  # Set the appearance mode of the interface
            ctk.set_default_color_theme("blue")  # Set the default color theme     
            


    def get_image_size(self, image_path):
        try:
            with Image.open(image_path) as img:
                return img.width, img.height
        except FileNotFoundError:
            logging.info(f"Image file not found: {image_path}")
            return None, None
        except OSError as e:
            logging.info(f"OS error when opening image file: {image_path}, error: {e}")
            return None, None
        except Exception as e:
            logging.info(f"Error getting image size for {image_path}: {e}")
            return None, None

         
    #because in global shortcut we dont have file_path.. maybe change to more general handle_global_keys!!
    def handle_global_delete(self, event):
        """
        Priority:
          1. Selected thumbnails in the grid (canvas) — delete selected files/folders
          2. Selected tree item — delete folder from tree
        """
        # 1. Any thumbnails selected in the grid?
        if self.selected_thumbnails:
            paths = [thumb[0] for thumb in self.selected_thumbnails
                     if thumb[0] and os.path.exists(thumb[0])]
            if paths:
                logging.info(f"[Delete] Grid selection: {paths}")
                self.confirm_delete_item(paths=paths)
                return

        # 2. Fallback — selected tree item
        selected_item = self.tree.focus()
        if not selected_item:
            logging.debug("[Delete] Nothing selected.")
            return
        values = self.tree.item(selected_item, "values")
        if values and values[0]:
            file_path = values[0]
            logging.info(f"[Delete] Tree selection: {file_path}")
            self.confirm_delete_item(paths=[file_path])
        else:
            logging.debug("[Delete] No valid path in tree.")

    
     #ONLY FOR TESTIN  OR DEBUG - for quickly run plugin
    def run_plugin_by_name(self, plugin_name):
        plugin = self.plugin_manager.plugins.get(plugin_name)
        if not plugin:
            logging.info(f"Plugin {plugin_name} not found.")
            return

        # Selected file_path from current selection
        if not self.selected_thumbnails:
            logging.info("No file selected!")
            return

        file_path = self.selected_thumbnails[0]  # or copy/list slice for multi-select

        # Invoke plugin with path
        plugin.run(self, file_path=file_path)

    def open_hotkeys_window(self):
        """Wrapper to open the hotkeys window from gui_elements."""
        show_hotkeys_window(self)


    def _is_input_focused(self):
        """True only when a text field in a dialog (Toplevel) has focus, not the main window.
        Block hotkeys only while typing in popups (keywords, search, rename...),
        not when the search bar or other fields in the main app have focus."""
        focused = self.focus_get()
        if focused is None:
            return False
        if focused.winfo_class() not in ('Entry', 'Text', 'TEntry', 'TText'):
            return False
        # Only block when the focused field is in a window other than the main app
        return focused.winfo_toplevel() is not self

    def bind_global_keys(self):
        """
        Binds all global hotkeys.
        Now loads definition directly from external 'hotkeys.py' file.
        """
        logging.info("Binding all global hotkeys from external file...")

        # 1. Load from external defaults (hotkeys module)
        self.hotkeys_map = DEFAULT_HOTKEYS

        # Wrap callback: skip hotkey when a text field has focus
        def g(callback):
            def handler(event):
                if self._is_input_focused():
                    return  # let the event reach Entry/Text
                return callback(event)
            return handler

        # --- Playback and navigation ---
        self.bind_all(self.hotkeys_map['play_pause'], g(lambda e: self.skip_global_next()))
        self.bind_all(self.hotkeys_map['skip_next'], g(lambda e: self.skip_global_next()))
        self.bind_all(self.hotkeys_map['skip_back'], g(lambda e: self.skip_global_back()))
        self.bind(self.hotkeys_map['enter_action'], self.on_thumbnail_enter_key)

        # --- Playlists ---
        self.bind_all(self.hotkeys_map['add_to_playlist'], g(self.add_selected_to_playlist))
        self.bind_all(self.hotkeys_map['new_playlist'], g(lambda event: self.add_selected_to_playlist(event, new_playlist=True)))
        
        # --- Window and UI ---
        self.bind_all(self.hotkeys_map['toggle_fullscreen'], g(self.toggle_fullscreen))
        self.bind(self.hotkeys_map['zoom_thumb'], self.zoom_thumbnail_ctrl_wheel)
        self.bind_all(self.hotkeys_map['select_all'], g(self.select_all_thumbnails))

        # --- File operations (standard) ---
        self.bind(self.hotkeys_map['delete'], lambda e: self.handle_global_delete(e))
        self.bind_all(self.hotkeys_map['files_clipboard_copy'], g(self.hotkey_files_clipboard_copy))
        self.bind_all(self.hotkeys_map['files_clipboard_paste_copy'], g(self.hotkey_files_clipboard_paste_copy))
        # Paste-move: bind every spelling — Windows Tk often emits <Control-Shift-V> for Ctrl+Shift+v
        _seen_paste_move = set()
        for seq in (
            self.hotkeys_map["files_clipboard_paste_move"],
            "<Control-Shift-v>",
            "<Control-Shift-V>",
        ):
            if seq not in _seen_paste_move:
                _seen_paste_move.add(seq)
                self.bind_all(seq, g(self.hotkey_files_clipboard_paste_move))
        self.bind_all(self.hotkeys_map['search'], g(lambda event: self.open_search_window()))
        self.bind_all(self.hotkeys_map['metadata'], g(lambda event: self.show_metadata_popup(self.selected_file_path)))
        self.bind_all(self.hotkeys_map['keywords'], g(lambda e: self.open_keyword_window(self.selected_file_path)))

        # --- Essentials (Refresh, Rename, Up) ---
        self.bind_all(self.hotkeys_map['refresh'], g(lambda e: self.refresh_thumbnails_in_subtree(self.current_directory)))
        self.bind_all(self.hotkeys_map['parent_dir'], g(lambda e: self.go_to_parent_directory()))
        self.bind_all(self.hotkeys_map['rename'], g(lambda e: self.rename_item(self.selected_file_path) if self.selected_file_path else None))

        # --- Panels ---
        self.bind_all(self.hotkeys_map['toggle_info_panel'], g(lambda e: self.toggle_infopanel_menu()))
        self.bind_all(self.hotkeys_map['toggle_timeline'], g(lambda e: self.toggle_timeline_menu()))

        # --- Rating ---
        self.bind_all(self.hotkeys_map['rate_0'], g(lambda e: self.set_rating(0)))
        self.bind_all(self.hotkeys_map['rate_1'], g(lambda e: self.set_rating(1)))
        self.bind_all(self.hotkeys_map['rate_2'], g(lambda e: self.set_rating(2)))
        self.bind_all(self.hotkeys_map['rate_3'], g(lambda e: self.set_rating(3)))
        self.bind_all(self.hotkeys_map['rate_4'], g(lambda e: self.set_rating(4)))
        self.bind_all(self.hotkeys_map['rate_5'], g(lambda e: self.set_rating(5)))

        # --- Video Loop ---
        self.bind(self.hotkeys_map['loop_start'], self.set_loop_start_shortcut)
        self.bind(self.hotkeys_map['loop_end'], self.set_loop_end_shortcut)
        self.bind(self.hotkeys_map['loop_toggle'], self.toggle_loop_shortcut)

        # --- Video Speed ---
        self.bind(self.hotkeys_map['video_speed_up'], self.speed_up_shortcut)
        self.bind(self.hotkeys_map['video_speed_down'], self.speed_down_shortcut)
        
        # --- Debug and development ---
        self.bind_all(self.hotkeys_map['run_plugin'], g(lambda e: self.run_plugin_by_name("timelinebar_plugin")))
        self.bind_all(self.hotkeys_map['show_debug'], g(lambda e: self.debug_overlay.show_overlay()))
        self.bind_all(self.hotkeys_map['hide_debug'], g(lambda e: self.debug_overlay.hide_overlay()))
        self.bind_all(self.hotkeys_map['toggle_log'], g(self.toggle_log_window))
        self.bind_all(self.hotkeys_map['view_catalog'], g(lambda event: self.view_catalog()))
        
        self.bind_all(self.hotkeys_map['debug_thumb'], g(lambda event: self.debug_selected_thumbnail(self.selected_thumbnail_path)))



    def toggle_log_window(self, event=None):
        """Show or hide the log window."""
        if self.log_window is None or not self.log_window.winfo_exists():
            # Create window if missing
            self.log_window = LogWindow(self, self.log_path)
        else:
            # Destroy existing window
            self.log_window.destroy()
            self.log_window = None
    
    def show_metadata_popup(self, file_path):
        try:
            image = Image.open(file_path)
            exif_data = image.getexif()
            description = exif_data.get(270, "No ImageDescription found.")
        except Exception as e:
            description = f"Error: {e}"

        popup = ctk.CTkToplevel(self)
        popup.title("Image Metadata")
        self._center_toplevel_window(popup, 500, 200)
        label = ctk.CTkLabel(popup, text=f"File: {file_path}\n\nImageDescription:\n{description}", wraplength=480, anchor="w", justify="left")
        label.pack(padx=10, pady=10)
        ctk.CTkButton(popup, text="Close", command=popup.destroy).pack(pady=10)



    def select_all_thumbnails(self, event=None):
        """
        Selects all thumbnails currently displayed in the grid.
        """
        try:
            self.selected_thumbnails = []
            # Every item in the current grid (virtual or classic), not only thumbnail_labels keys,
            # so Ctrl+A + refresh/diff keep _prev_selected_indices consistent with update_thumbnail_selection.
            for i, vf in enumerate(self.video_files):
                path = vf.get("path")
                if not path:
                    continue
                info = self._thumbnail_label_info_for_path(path, i)
                if info is None:
                    info = {"index": i, "canvas": None, "label": None}
                self.selected_thumbnails.append((path, info, i))

            if self.selected_thumbnails:
                self.selected_thumbnail_index = self.selected_thumbnails[-1][2]
                self.selected_file_path = self.selected_thumbnails[-1][0]
            else:
                self.selected_thumbnail_index = None
                self.selected_file_path = None

            self._prev_selected_indices = set()
            self.update_thumbnail_selection()
            if getattr(self, "_vg_active", False):
                try:
                    self._vg_reapply_selection()
                except Exception:
                    pass

            self.update_status_bar()

            # Multi-timeline strips for selected videos (same as on click)
            if self.preview_on and self.info_panel:
                selected_video_paths = [
                    p for p, _, _ in self.selected_thumbnails
                    if p.lower().endswith(VIDEO_FORMATS)
                ]
                if len(selected_video_paths) > 1:
                    # _show_multi_timeline applies limit via _apply_strip_limit
                    self._show_multi_timeline(selected_video_paths)

        except Exception as e:
            logging.info(f"select_all_thumbnails error: {e}")

    def save_preferences(self):
        save_preferences(
            self,
            self.thumbnail_format,
            self.thumbnail_cache_path,
            self.auto_play,
            self.memory_cache,
            self.capture_method_var.get(),
            self.video_output_var.get(),
            self.audio_output_var.get(),
            self.hardware_decoding_var.get(),
            self.audio_device_var.get(),
            self.thumbnail_size,
            self.thumbnail_time,
        )

    def update_panel_flags(self, title, expanded):
        if title == "Info Panel":
            self.preview_on = expanded
            logging.info(f"[TOGGLE][InfoPanel] preview_on set to {expanded}")
        elif title == "Timeline":
            self.ShowTWidget = expanded
            logging.info(f"[TOGGLE][Timeline] ShowTWidget set to {expanded}")


    def toggle_infopanel_menu(self, save_prefs=True):
        # No-op if panel container is missing
        if hasattr(self, "info_panel_container") and self.info_panel_container:
            self.info_panel_container.toggle_panel()
            self.show_infopanel_var.set(self.info_panel_container.expanded)
            if save_prefs:
                self.save_preferences()


    def toggle_timeline_menu(self, save_prefs=True):
        """
        Toggles the timeline panel and updates the menu variable.
        Saves preferences only if 'save_prefs' is True.
        """
        if hasattr(self, "timeline_container"):
            self.timeline_container.toggle_panel()
            self.show_timeline_var.set(self.timeline_container.expanded)
            
            # Only save if explicitly told to (default is True)
            if save_prefs:
                self.save_preferences()  # unified save    
        


    def change_thumbnail_size(self,size_option):
           

            width, height = map(int, size_option.split('x'))
            self.thumbnail_size = (width, height)
            logging.debug("Thumbnail size -> %s", self.thumbnail_size)
            
            self.display_thumbnails(self.current_directory)
            
    def change_wide_folder_size(self,size_option):
       

        width, height = map(int, size_option.split('x'))
        self.widefolder_size = (width, height)
        logging.debug("Wide folder size -> %s", self.widefolder_size)
        
        self.display_thumbnails(self.current_directory)       
        

    def _guard_against_dropdown_clickthrough(self, ms: float = 650.0):
        """Call after toolbar combobox selection so stray mouse-up does not activate content below."""
        self._ignore_pointer_navigation_until = time.monotonic() + (ms / 1000.0)

    def _toolbar_combo_begin(self):
        """Cancel pending tree folder debounce + block stray clicks after dropdown closes."""
        self._guard_against_dropdown_clickthrough(650.0)
        if self._debounce_job is not None:
            try:
                self.after_cancel(self._debounce_job)
            except Exception:
                pass
            self._debounce_job = None

    def change_both_thumbnail_sizes(self, size_option):
        """
        Changes BOTH the regular thumbnail size AND the wide folder size
        simultaneously based on the selection from the main toolbar ComboBox,
        then refreshes the display.

        Args:
            size_option (str): The selected size string (e.g., "320x240").
        """
        self._toolbar_combo_begin()
        try:
            # Parse the selected size string into width and height
            width, height = map(int, size_option.split('x'))
            new_size_tuple = (width, height)

            # Flag to track if any size actually changed
            size_changed = False

            # Update regular thumbnail size if different
            if self.thumbnail_size != new_size_tuple:
                logging.info(f"Toolbar ComboBox: Changing REGULAR thumbnail size to: {new_size_tuple}")
                self.thumbnail_size = new_size_tuple
                size_changed = True

            # Update wide folder size if different
            if self.widefolder_size != new_size_tuple:
                logging.info(f"Toolbar ComboBox: Changing WIDE FOLDER size to: {new_size_tuple}")
                self.widefolder_size = new_size_tuple
                size_changed = True

            # Refresh the display only if at least one size was actually changed
            if size_changed:
                self.display_thumbnails(self.current_directory)
            else:
                logging.info(f"Toolbar ComboBox: Both sizes are already {new_size_tuple}. No refresh needed.")

            # Optionally save preferences if needed after size change
            # self.save_preferences() # Uncomment if you want to save immediately

        except ValueError:
            # Log an error if the size string is invalid
            logging.error(f"Invalid size format received: {size_option}. Expected 'widthxheight'.")
        except Exception as e:
            # Log any other unexpected errors
            logging.error(f"Error changing both thumbnail sizes: {e}", exc_info=True)


    def show_hover_info(self, event, path):
        """Show hover info for the specified file."""
        # Destroy any existing hover popups
        if self.hover_popup:
            self.hover_popup.destroy()

        self.hover_popup = tk.Toplevel(self)
        self.hover_popup.wm_overrideredirect(True)
        self.hover_popup.geometry(f"+{event.x_root + 10}+{event.y_root + 10}")
        self.hover_popup.configure(
            bg="#2b2b2b",                  # background behind the label
            highlightbackground="#777777", # border color
            highlightthickness=1           # border width
        )

        try:
            file_info = self.get_file_info(path)
            text_to_show = file_info
        except FileNotFoundError:
            text_to_show = f"File not found:\n{path}"

        self.hover_label = tk.Label(
            self.hover_popup,
            text=text_to_show,
            justify=tk.LEFT,
            anchor="w",
            bg="#2b2b2b",     # Dark background like in Unreal
            fg="#d0d0d0",     # Light gray text
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
            padx=10,
            pady=6
        )
        self.hover_label.pack()


    def hide_hover_info(self):
        if self.hover_popup:
            self.hover_popup.destroy()
        self.hover_popup = None
    
    def get_file_info(self, path):
        file_name = os.path.basename(path)
        file_size = os.path.getsize(path)
        file_mtime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(path)))
        keywords = self.database.get_keywords(path)  # Retrieve the keywords from the database
        rating = self.database.get_rating(path)
        file_info = f"{file_name}\nSize: {file_size} bytes\nModified: {file_mtime}\nKeywords: {keywords} \nRating: {rating}"
        return file_info

    def quick_access_selected(self, event):
        if time.monotonic() < getattr(self, "_ignore_pointer_navigation_until", 0.0):
            return
        selected_path = self.quick_access_combo.get()
        logging.info(f"Selected path from quick access: {selected_path}")  # Debug info
        if os.path.isdir(selected_path):
            self.display_thumbnails(selected_path)
            self.current_directory = selected_path
            self.select_current_folder_in_tree()
            
  
        
    def add_to_recent_directories(self, path):
            logging.info(f"add_to_recent_directories PATH: {path}")  # Debug info

        # Add path if it's not already in recent_directories
        # if path not in self.recent_directories:
            self.recent_directories.append(path)
           
            # Ensure recent_directories does not exceed quick_acces_history limit
            if len(self.recent_directories) > self.quick_acces_history:
                removed_path = self.recent_directories.pop(0)  # Remove the oldest entry
                logging.info(f"Removed oldest entry from recent directories: {removed_path}")  # Debug info
                
            self.update_quick_access_combo(path)

    def update_quick_access_combo(self, path):
         # Display recent_directories in reverse order, so newest entries appear at the top
        self.quick_access_combo.configure(values=self.recent_directories[::-1])

        # Set the selected path directly (current_directory may not be updated yet at this point)
        self.quick_access_combo.set(path)
        logging.info(f"Quick access updated with path: {path}")  # Debug info


    def exit_program(self):
        """
        Closes the menu window and invokes the on_closing function.
        """
        self.quit()  # Gracefully exit the application loop
        self.on_closing()  # Perform any cleanup tasks before exiting    

    def on_closing(self):
        save_recent_directories(self.settings_file,self.recent_directories)
        self.save_preferences()  # unified save

        self.destroy()

    def catalog_file_info(self, file_path):
        filename = os.path.basename(file_path)
        try:
            with Image.open(file_path) as img:
                resolution = f"{img.width}x{img.height}"
        except:
            resolution = "N/A"
        self.db.insert_file(filename, resolution)
        logging.info(f"Cataloged {filename} with resolution {resolution}")

    def pad_label(self, s: str, n: int = 1) -> str:
        """Return label padded on the left to increase gap between icon and text."""
        # non-breaking spaces keep consistent visual gap
        return ("\u00A0" * n) + s



    def populate_tree(self):
        # Ensure tree is populated first
        first_item = self.tree.get_children()[0] if self.tree.get_children() else None
        if first_item:
            self.tree.focus(first_item)
            self.tree.selection_set(first_item)

        user_profile = os.path.expanduser("~")
        special_folders = {
            "Desktop": os.path.join(user_profile, "Desktop"),
            "Documents": os.path.join(user_profile, "Documents"),
            "Pictures": os.path.join(user_profile, "Pictures"),
            "Videos": os.path.join(user_profile, "Videos")
        }

        # Try to locate Google Drive and add it to the list
        gdrive_path = self.find_google_drive_path()
        if gdrive_path:
            special_folders["Google Drive"] = gdrive_path

        # Insert special folders; each process_directory call is deferred via
        # after(0, ...) so the Tk event loop can paint each node before
        # continuing — keeps the UI responsive while the tree is built.
        for name, path in special_folders.items():
            if os.path.exists(path):
                icon = self.folder_treeicon
                if name == "Google Drive":
                    icon = self.google_icon
                elif name == "Desktop":
                    icon = self.desktop_icon
                elif name == "Documents":
                    icon = self.documents_icon
                elif name == "Pictures":
                    icon = self.pictures_icon
                elif name == "Videos":
                    icon = self.videos_icon
                else:
                    icon = self.folder_treeicon  # fallback

                path_hash = self.get_path_hash(path)
                root_node = self._tree_insert('', 'end', text=self.pad_label(name), image=icon, open=False, values=(path, path_hash))
                # Defer sub-directory scan so the node appears in the UI immediately
                self.after(0, lambda rn=root_node, p=path: self.process_directory(rn, p))

        # get_available_drives now uses win32api (instant) instead of os.popen
        drives = self.get_available_drives()
        for drive in drives:
            # Skip any drive that is actually Google Drive (already added above)
            try:
                volume_name = win32api.GetVolumeInformation(drive)[0]
                if "google drive" in volume_name.lower():
                    continue
            except Exception:
                pass

            if os.path.exists(drive):
                path_hash = self.get_path_hash(drive)
                root_node = self._tree_insert('', 'end', text=self.pad_label(drive), image=self.hdd_icon, open=False, values=(drive, path_hash))
                # Defer sub-directory scan so each drive node appears immediately
                self.after(0, lambda rn=root_node, d=drive: self.process_directory(rn, d))
        


    def find_google_drive_path(self):
        # Method 1: virtual drive letter (most common)
        drives = win32api.GetLogicalDriveStrings()
        drives = drives.split('\000')[:-1]
        for drive in drives:
            try:
                volume_name = win32api.GetVolumeInformation(drive)[0]
                if "google drive" in volume_name.lower():
                    # Found drive — return its root folder
                    return os.path.join(drive, "My Drive")
            except Exception:
                # Some drives (e.g. empty DVD) can raise
                continue

        # Method 2: standard folder under user profile (older client)
        user_profile = os.path.expanduser("~")
        potential_path = os.path.join(user_profile, "Google Drive")
        if os.path.exists(potential_path):
            return potential_path
            
        return None  # Google Drive not found


    def process_directory(self, parent, path):
        """
        Populates the given parent node with its subdirectories.
        Fetches directory contents and sorts them case-insensitively 
        to ensure consistency with the grid view sorting.
        """
        # Normalize path and ensure it's a directory
        path = os.path.normcase(os.path.normpath(path))
        # logging.info(f"[PROCESS] Attempting to list: {path}")

        if not os.path.isdir(path):
            logging.info(f"[SKIP] {path} is not a directory.")
            return

        if path.startswith("virtual_library://"):
            logging.info(f"[SKIP] {path} is virtual, ignoring.")
            return

        try:
            entries = os.listdir(path)
            # Case-insensitive sort (match grid view)
            entries.sort(key=lambda x: x.lower())
        except Exception as e:
            logging.info(f"[ERROR] Cannot list directory: {path} → {e}")
            return

        try:
            # Remove stale dummy placeholder rows under the currently opened parent.
            for child in self.tree.get_children(parent):
                vals = self.tree.item(child, 'values')
                if vals and vals[0] == 'dummy':
                    self.tree.delete(child)

            existing_paths = {
                os.path.normcase(self.tree.item(child, 'values')[0])
                for child in self.tree.get_children(parent)
                if self.tree.item(child, 'values')
            }

            for p in entries:
                full_path = os.path.normcase(os.path.normpath(os.path.join(path, p)))
                if os.path.isdir(full_path):
                    if full_path not in existing_paths:
                        is_cached = self.database.is_folder_cached(full_path)
                        icon = self.folder_treeicon_green if is_cached else self.folder_treeicon
                        path_hash = self.get_path_hash(full_path)

                        node = self._tree_insert(parent, 'end', text=self.pad_label(p), image=icon, open=False, values=(full_path, path_hash))

                        # Add dummy child only when REAL subfolders exist.
                        try:
                            child_entries = os.listdir(full_path)
                            has_subdirs = any(
                                os.path.isdir(os.path.join(full_path, child_name))
                                for child_name in child_entries
                            )
                            if has_subdirs and not self.tree.get_children(node):
                                self.tree.insert(node, 'end', text='', values=('dummy',))
                        except Exception as e:
                            logging.warning(f"Could not check children for: {full_path} → {e}")

        except Exception as e:
            logging.error(f"Unexpected error in process_directory for {path}: {e}")


    def get_path_hash(self, path):
        """Generate a consistent hash for a file or folder path."""
        normalized_path = os.path.normcase(os.path.abspath(path))  # Normalize path
        return hashlib.md5(normalized_path.encode('utf-8')).hexdigest()


    # when we want to refresh tree from thumbnail window we need to get path.. 
    def get_selected_thumbnail_path(self, event):
        canvas_item = self.scrollable_frame.winfo_containing(event.x_root, event.y_root)
        if canvas_item:
            for video_file in self.video_files:
                if video_file['path'] == canvas_item.find_withtag("current")[0]:
                    logging.info(f"get_selected_thumbnail_path: {video_file['path']}")
                    return video_file['path']
        return None


    def _on_info_panel_tab_changed(self):
        """Called when the user switches tabs in the info panel.
        Triggers video info extraction if the Video tab becomes active."""
        if not getattr(self, "info_panel", None):
            return
        active_tab = self.info_panel.tabs.get()
        if active_tab == "Video":
            file_path = getattr(self, "selected_file_path", None)
            if file_path and file_path.lower().endswith(VIDEO_FORMATS):
                self.update_video_info_if_tab_active(file_path)

    def update_video_info_if_tab_active(self, file_path):
        """Extract video info only if 'Video' tab in info panel is active.
        MediaInfo.parse() runs in a background thread so it does not block the UI."""
        if not getattr(self, "info_panel", None):
            return
        active_tab = self.info_panel.tabs.get()
        if active_tab != "Video":
            return
        if not file_path.lower().endswith(VIDEO_FORMATS):
            return

        from utils import extract_video_info

        # Reset fields to '?' so user sees we are loading
        self.info_panel.reset_video_tab()

        def _extract_in_bg():
            video_info = extract_video_info(file_path)

            def _apply():
                # Update the cache
                if hasattr(self, "metadata_cache"):
                    if file_path in self.metadata_cache:
                        self.metadata_cache[file_path].update(video_info)
                    else:
                        self.metadata_cache[file_path] = video_info
                # Update the Video tab in the info panel
                if self.info_panel:
                    self.info_panel.update_video_tab(video_info)

            self.after(0, _apply)

        threading.Thread(target=_extract_in_bg, daemon=True).start()

    def on_thumbnail_enter_key(self, event):
        logging.info("[DEBUG] ENTER pressed on thumbnail")
        file_path = self.selected_file_path
        if not file_path or os.path.isdir(file_path):
            logging.info("[DEBUG] No file selected or is a folder, not opening player.")
            return

        # Stop preview in InfoPanel
        try:
            if hasattr(self, "info_panel") and self.info_panel is not None:
                if hasattr(self.info_panel, "stop_video_preview"):
                    self.info_panel.stop_video_preview()
                elif hasattr(self.info_panel, "preview_player") and self.info_panel.preview_player:
                    self.info_panel.preview_player.stop_video()
                logging.info("[DEBUG] Preview player stopped before opening main player.")
        except Exception as e:
            logging.info("[DEBUG] Failed to stop preview player before player open: %s", e)

        # Branch by file type
        ext = os.path.splitext(file_path)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"):
            self.open_image_viewer(file_path, os.path.basename(file_path))
        elif ext in VIDEO_FORMATS:
            self.open_video_player(file_path, os.path.basename(file_path))
        else:
            logging.info("[DEBUG] Unsupported file type for ENTER.")


    def detect_canvas_click(self, event):
        widget1 = event.widget
        logging.info(f"Widget: {widget1}, Type: {type(widget1)}, ID: {widget1.winfo_id()}")
        # Widget under cursor at click position
        widget = event.widget.winfo_containing(event.x_root, event.y_root)
        # Not a thumbnail or its child:
        if not hasattr(widget, "file_path"):
            logging.info("[DEBUG] Clicked empty canvas, stopping preview.")
            self.stop_preview()
            # (Optional: clear all thumbnail selection)
            # self.clear_selection()

    def stop_preview(self):
        """
        Note: Safely stops any running preview (video or image).
        It first checks if the necessary objects exist and are not None before
        attempting to perform any actions on them.
        """
        # First, a safety check to ensure the info_panel itself exists.
        if not hasattr(self, "info_panel") or self.info_panel is None:
            return  # Exit the function if there is no info_panel to work with.

        # --- Safely handle the VIDEO preview ---
        # We check if the 'preview_player' attribute exists AND its value is not None.
        if hasattr(self.info_panel, "preview_player") and self.info_panel.preview_player is not None:
            try:
                if hasattr(self.info_panel, "stop_video_preview"):
                    self.info_panel.stop_video_preview()
                else:
                    self.info_panel.preview_player.stop_video()
                logging.info("Video preview stopped successfully.")
            except Exception as e:
                # Correctly log any unexpected errors using an f-string.
                logging.warning(f"An error occurred while stopping the video preview: {e}")

        # --- Safely handle the IMAGE preview ---
        # We do the same check for the image canvas.
        if hasattr(self.info_panel, "preview_canvas") and self.info_panel.preview_canvas is not None:
            try:
                # In Tkinter, it's possible for a widget to be destroyed but the attribute
                # still exists. We add an extra check for that.
                if self.info_panel.preview_canvas.winfo_exists():
                    self.info_panel.preview_canvas.destroy()
                    logging.info("Image preview canvas destroyed.")
                # Set the attribute to None to keep the state clean.
                self.info_panel.preview_canvas = None
            except Exception as e:
                # Correctly log any unexpected errors using an f-string.
                logging.warning(f"An error occurred while destroying the image preview: {e}")


    def _mark_menu_interaction(self):
        """
        Records the timestamp of the last menu interaction to prevent accidental clicks on background elements.
        """
 
        self._last_menu_interaction_time = time.time()

    def _focus_back_after_dialog(self):
        """Return focus to main window after a dialog (keyword, etc.) closes."""
        try:
            self.lift()  # bring to front
            self.focus_force()
            self._last_menu_interaction_time = 0  # reset guard so first click isn't blocked
            self._restore_selection_visual()  # re-apply selection border (lost after refresh)
            logging.info("[Focus] Main window focus restored after dialog close.")
        except Exception as e:
            logging.info(f"[Focus] _focus_back_after_dialog failed: {e}")

    def _focus_video_window_after_dialog(self):
        """Return focus to currently opened video window after keyword dialog."""
        try:
            vw = getattr(getattr(self, "current_video_window", None), "video_window", None)
            if vw is not None and vw.winfo_exists():
                # Bring player window back above main app after dialog closes.
                try:
                    vw.attributes("-topmost", True)
                    vw.after(30, lambda: vw.attributes("-topmost", False))
                except Exception:
                    pass
                vw.lift()
                vw.focus_force()
                self._last_menu_interaction_time = 0
                logging.info("[Focus] Video window focus restored after dialog close.")
                return
        except Exception as e:
            logging.info(f"[Focus] _focus_video_window_after_dialog failed: {e}")
        self._focus_back_after_dialog()

    def _restore_selection_visual(self):
        """Re-apply selection border to all selected thumbnails. Call after refresh replaces a thumbnail."""
        if not self.selected_thumbnails:
            return
        self._prev_selected_indices = set()  # force full re-apply
        self.update_thumbnail_selection()

    def _close_remove_keyword_window(self):
        """Close remove-keyword dialog and return focus to main window."""
        if hasattr(self, "remove_keyword_window") and self.remove_keyword_window.winfo_exists():
            self.remove_keyword_window.destroy()
        self.after(150, self._focus_back_after_dialog)


    def on_thumbnail_click(self, event, file_path):
        """
        Main thumbnail click handler: single/double click or folder vs file.
        """

        # Guard check: skip if menu was recently interacted with (within 200ms)
        if time.time() - self._last_menu_interaction_time < 0.2:
            return

        # After Ctrl+A (etc.), ButtonPress skipped select on an already-selected cell so DnD can keep
        # multi-selection; on release without a real drag, collapse to the clicked thumb.
        if not getattr(self, "_dnd_drag_happened", False):
            if len(self.selected_thumbnails) > 1:
                shift_rel = (event.state & 0x0001) != 0
                ctrl_rel = (event.state & 0x0004) != 0
                if not shift_rel and not ctrl_rel:
                    try:
                        nfp = os.path.normcase(os.path.normpath(file_path))
                        idx = next(
                            i
                            for i, vf in enumerate(self.video_files)
                            if os.path.normcase(os.path.normpath(vf.get("path", ""))) == nfp
                        )
                    except StopIteration:
                        idx = None
                    if idx is not None:
                        sel_idx = {
                            t[2]
                            for t in self.selected_thumbnails
                            if isinstance(t, (list, tuple)) and len(t) > 2
                        }
                        if idx in sel_idx:
                            self.select_thumbnail(
                                idx,
                                shift=False,
                                ctrl=False,
                                trigger_preview=False,
                            )
        
        if os.path.isdir(file_path):
            self.current_directory = file_path
            self._schedule_tree_sync_for_current_dir()
            return

        now = int(time.time() * 1000)

        # Double-click detection
        if self._last_click_time and (now - self._last_click_time < self._click_interval):
            if self._click_timer:
                self.after_cancel(self._click_timer)
                self._click_timer = None
            self._last_click_time = 0
            self._handle_thumbnail_double_click(file_path)
            return

        # --- MULTI-TIMELINE SWITCH ---
        # selected_thumbnails is already updated from on_thumb_click (Button press)
        if self.preview_on and self.info_panel:
            strips_mode = getattr(self.info_panel, "preview_mode_var", None)
            is_strips = strips_mode and strips_mode.get() == "Strips"
            selected_video_paths = [
                p for p, _, _ in self.selected_thumbnails
                if p.lower().endswith(VIDEO_FORMATS)
            ]
            logging.info(
                "[MultiTimeline] is_strips=%s  sel_count=%d  ShowTWidget=%s  "
                "has_tw=%s  tw_viewable=%s  file=%s",
                is_strips, len(selected_video_paths),
                getattr(self, "ShowTWidget", "?"),
                hasattr(self, "timeline_widget"),
                self.timeline_widget.winfo_viewable() if hasattr(self, "timeline_widget") else "N/A",
                os.path.basename(file_path),
            )
            if len(selected_video_paths) > 1 or (is_strips and selected_video_paths):
                # Multi-select always → strips; single in Strips mode → strips too
                if self._click_timer:
                    self.after_cancel(self._click_timer)
                    self._click_timer = None
                self._last_click_time = now
                self._show_multi_timeline(selected_video_paths)
                # Update bottom timeline widget for multi/strips — shows last-clicked video
                if hasattr(self, "timeline_widget") and file_path.lower().endswith(VIDEO_FORMATS):
                    tw = self.timeline_widget
                    logging.info(
                        "[MultiTimeline] TW update attempt: ShowTWidget=%s  viewable=%s  exists=%s",
                        self.ShowTWidget, tw.winfo_viewable(), tw.winfo_exists(),
                    )
                    if self.ShowTWidget and tw.winfo_exists():
                        tw.load_thumbnails(video_path=file_path)
                        tw.redraw_timeline()
                        logging.info("[MultiTimeline] TW updated OK for %s", os.path.basename(file_path))
                return
            else:
                # Back to single Video mode — restore normal panel
                if not is_strips:
                    self._show_single_preview()

        # Single-click logic (deferred)
        self._last_click_time = now
        if self._click_timer:
            self.after_cancel(self._click_timer)
        
        # Schedule single-click handler after a short delay
        self._click_timer = self.after(
            self._click_interval, 
            lambda: self._handle_thumbnail_single_click(file_path)
        )

    def _handle_thumbnail_single_click(self, file_path):
            """
            Single-click: preview and GUI updates.
            Debounced so rapid browsing does not run heavy VLC/timeline work per item.
            """
            self._click_timer = None
            
            # Cancel any pending preview if user clicked elsewhere quickly
            if hasattr(self, '_preview_timer') and self._preview_timer:
                self.after_cancel(self._preview_timer)
                self._preview_timer = None

            def load_heavy_preview():
                self._preview_timer = None  # timer fired, clear handle
                
                is_video = file_path.lower().endswith(VIDEO_FORMATS)
                is_image = file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.gif'))

                if is_video:
                    logging.info(f"[DEBUG] Checking timeline conditions: ShowTWidget={self.ShowTWidget}, Has timeline_widget={hasattr(self, 'timeline_widget')}")

                    # B) Timeline widget first — no MediaInfo/VLC wait
                    if self.ShowTWidget and hasattr(self, "timeline_widget") and self.timeline_widget.winfo_viewable():
                        self.timeline_widget.load_thumbnails(video_path=file_path)
                        self.timeline_widget.redraw_timeline()

                    # A) InfoPanel Preview
                    if self.preview_on and self.info_panel:
                        self.update_video_info_if_tab_active(file_path)

                        auto_play_preview = (
                            self.info_panel.preview_auto_play_var.get()
                            if hasattr(self.info_panel, "preview_auto_play_var")
                            else True
                        )

                        if auto_play_preview:
                            # Standard path: VLC player
                            self.ensure_preview_player()
                            self.info_panel.preview_player.video_path = file_path
                            self.info_panel.start_video_preview(file_path)
                            self.active_player = self.info_panel.preview_player
                        else:
                            # Fast path: show cached thumbnail frame, skip VLC entirely
                            if hasattr(self.info_panel, "preview_player") and self.info_panel.preview_player:
                                try:
                                    self.info_panel.preview_player.stop_video()
                                except Exception:
                                    pass
                            thumb_path = self._get_cached_thumb_path(file_path)
                            if thumb_path and os.path.exists(thumb_path):
                                self.info_panel.show_image_preview(thumb_path)
                            else:
                                # Thumbnail not in cache yet — extract in background thread
                                import threading
                                from file_operations import create_video_thumbnail
                                def _extract_and_show():
                                    t = create_video_thumbnail(
                                        file_path,
                                        self.thumbnail_size,
                                        self.thumbnail_format,
                                        self.capture_method_var.get(),
                                        thumbnail_time=self.calculate_thumbnail_time(file_path),
                                        cache_enabled=self.cache_enabled,
                                        overwrite=False,
                                        cache_dir=self.thumbnail_cache_path,
                                        database=self.database,
                                    )
                                    p = self._get_cached_thumb_path(file_path)
                                    if p and os.path.exists(p):
                                        self.after(0, lambda: self.info_panel.show_image_preview(p))
                                threading.Thread(target=_extract_and_show, daemon=True).start()

                elif is_image:
                    self.info_panel.show_image_preview(file_path)

            # Run after 200 ms debounce (wait until rapid browsing stops)
            self._preview_timer = self.after(200, load_heavy_preview)


    def _handle_thumbnail_double_click(self, file_path):
        """
        Note: Handles the logic for a double-click event: opening the file.
        """
        is_video = file_path.lower().endswith(VIDEO_FORMATS)
        is_image = file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.gif'))

        # Safely stop any running preview before opening the main player.
        if hasattr(self, "info_panel") and self.info_panel is not None:
            try:
                if hasattr(self.info_panel, "stop_video_preview"):
                    self.info_panel.stop_video_preview()
                elif getattr(self.info_panel, "preview_player", None):
                    self.info_panel.preview_player.stop_video()
            except Exception as e:
                logging.warning(f"Error stopping preview on double-click: {e}")

        if is_video:
            self.open_video_player(file_path, os.path.basename(file_path))
        elif is_image:
            self.open_image_viewer(file_path, os.path.basename(file_path))


    def _get_cached_thumb_path(self, file_path):
        """Returns the disk path to the cached thumbnail JPEG for a video file, or None."""
        try:
            from file_operations import get_cache_dir_path
            cache_dir_path, _ = get_cache_dir_path(
                file_path, os.path.abspath(self.thumbnail_cache_path)
            )
            w, h = self.thumbnail_size
            cache_key = f"{os.path.basename(file_path)}_{w}x{h}.jpg"
            return os.path.normpath(os.path.join(cache_dir_path, cache_key))
        except Exception as e:
            logging.warning("[preview] _get_cached_thumb_path failed: %s", e)
            return None

    def ensure_preview_player(self):
        """Ensure the info_panel.preview_player exists and is fully initialized, including VLC instance."""
        if not hasattr(self.info_panel, "preview_player") or self.info_panel.preview_player is None:
            from video_operations import VideoPlayer
            logging.info("info_panel.preview_player was not initialized, initializing...")
            self.info_panel.preview_player = VideoPlayer(
                parent=self.info_panel.tab_preview,
                controller=self,
                video_path="",
                video_name="PreviewPlayer",
                initial_volume=0,
                 # VLC options from main app
                vlc_video_output=self.video_output_var.get(),
                vlc_audio_output=self.audio_output_var.get(),
                vlc_hw_decoding=self.hardware_decoding_var.get(),
                vlc_audio_device=self.audio_device_var.get(),
                auto_play=False,
                embed=True,
                show_video_button_bar=False,
                use_gpu_upscale=getattr(self, "gpu_upscale", False)
            )
            
            self.info_panel.preview_player.video_window.pack(fill="both", expand=True)
            
            #self.info_panel.preview_player.video_window.withdraw()

        # 🔧 NEW: Lazy init VLC instance if needed
        if not getattr(self.info_panel.preview_player, "instance", None):
            logging.info("[INFO] Initializing VLC instance for preview_player...")
            self.info_panel.preview_player.apply_preferences()
            self.info_panel.preview_player.instance = self.info_panel.preview_player.create_vlc_instance()
            if not self.info_panel.preview_player.instance:
                logging.info("[ERROR] Failed to create VLC instance for preview_player.")
                return
            self.info_panel.preview_player.player = self.info_panel.preview_player.instance.media_player_new()


    def set_active_player(self):
        """Update active player after preview is ready."""
        self.active_player = self.info_panel.preview_player
        # Add any follow-up logic after preview is ready here.

    def _ensure_multi_viewer(self):
        """Lazy-init MultiTimelineViewer inside info_panel.tab_preview."""
        if self.multi_viewer is not None and self.multi_viewer.winfo_exists():
            return
        if not getattr(self, "info_panel", None):
            return
        self.multi_viewer = MultiTimelineViewer(
            master=self.info_panel.tab_preview,
            timeline_manager=self.timeline_manager,
            controller=self,
            fg_color="transparent"
        )
        # Wire VIDEO/STRIPS mode switch callback
        if hasattr(self.info_panel, "preview_mode_switch"):
            self.info_panel.preview_mode_switch.configure(command=self._on_preview_mode_change)

    def _set_limit_checkbox_enabled(self, enabled: bool):
        """Enable/disable Limit checkbox visually (CustomTkinter needs explicit colors)."""
        if not getattr(self, "info_panel", None):
            logging.info("[LimitCB] info_panel missing")
            return
        cb = getattr(self.info_panel, "multiTimeline_limit_checkbox", None)
        if not cb:
            logging.info("[LimitCB] checkbox missing")
            return
        logging.info("[LimitCB] setting enabled=%s", enabled)
        if enabled:
            cb.configure(
                state="normal",
                text_color="gray",
                fg_color=("#2CC985", "#2FA572"),       # default CTK green/blue
                border_color=("gray50", "gray50"),
                checkmark_color=("gray10", "gray90"),
            )
        else:
            DARK = ("#3a3a3a", "#3a3a3a")
            cb.configure(
                state="disabled",
                text_color=DARK,
                fg_color=DARK,
                border_color=DARK,
                checkmark_color=DARK,
            )

    def _on_preview_mode_change(self, mode):
        """VIDEO / Strips mode switch callback from info panel."""
        if not getattr(self, "info_panel", None):
            return

        # Limit checkbox only meaningful in Strips mode
        self._set_limit_checkbox_enabled(mode == "Strips")

        if mode == "Strips":
            paths = [p for p, _, _ in self.selected_thumbnails
                     if p.lower().endswith(VIDEO_FORMATS)]
            if not paths and self.selected_file_path and \
               self.selected_file_path.lower().endswith(VIDEO_FORMATS):
                paths = [self.selected_file_path]
            if paths:
                self._show_multi_timeline(paths)
        else:
            self._show_single_preview()
            if self.selected_file_path and \
               self.selected_file_path.lower().endswith(VIDEO_FORMATS):
                self._handle_thumbnail_single_click(self.selected_file_path)

    def _apply_strip_limit(self, video_paths):
        """Cap strip count when limit is enabled in info panel."""
        limit_on = (
            getattr(self, "info_panel", None) and
            hasattr(self.info_panel, "multiTimeline_limit_var") and
            self.info_panel.multiTimeline_limit_var.get()
        )
        if limit_on and len(video_paths) > self.timeline_strip_count:
            logging.info(
                "[MultiTimeline] Limit on: showing %d of %d videos.",
                self.timeline_strip_count, len(video_paths)
            )
            return video_paths[:self.timeline_strip_count]
        return video_paths

    def _show_multi_timeline(self, video_paths):
        """
        Hide VLC preview and show MultiTimelineViewer with film strips.
        """
        if not getattr(self, "info_panel", None):
            return

        video_paths = self._apply_strip_limit(video_paths)

        # Cancel pending debounced preview timer
        if hasattr(self, '_preview_timer') and self._preview_timer:
            self.after_cancel(self._preview_timer)
            self._preview_timer = None

        # Stop VLC preview
        self.stop_preview()

        # Hide VLC video_window first, then fix layout
        if getattr(self.info_panel, "preview_player", None):
            try:
                vw = self.info_panel.preview_player.video_window
                vw.pack_forget()
                # Detach VLC from hwnd so it stops drawing
                if getattr(self.info_panel.preview_player, "player", None):
                    self.info_panel.preview_player.player.set_hwnd(0)
            except Exception:
                pass

        # Hide image preview canvas if present
        if getattr(self.info_panel, "preview_canvas", None):
            try:
                self.info_panel.preview_canvas.pack_forget()
            except Exception:
                pass

        # Force layout before showing multi_viewer (avoids VLC ghost frames)
        self.info_panel.tab_preview.update_idletasks()

        # Lazy init + show multi viewer
        self._ensure_multi_viewer()
        if self.multi_viewer:
            self.multi_viewer.pack(fill="both", expand=True, padx=2, pady=(2, 0))
            self.multi_viewer.load_videos(video_paths)

        # Sync mode switch and Limit checkbox
        if hasattr(self.info_panel, "preview_mode_var"):
            self.info_panel.preview_mode_var.set("Strips")
        self._set_limit_checkbox_enabled(True)

    def _show_single_preview(self):
        """
        Hide MultiTimelineViewer and restore normal VLC preview panel.
        """
        if self.multi_viewer and self.multi_viewer.winfo_exists():
            self.multi_viewer.pack_forget()
            # Reset zoom — next show auto-fits to panel width
            self.multi_viewer._user_zoomed = False

        # Switch back to Video and dim Limit checkbox
        if getattr(self, "info_panel", None) and hasattr(self.info_panel, "preview_mode_var"):
            self.info_panel.preview_mode_var.set("Video")
        self._set_limit_checkbox_enabled(False)

    
    def update_cache_status(self, folder_path, is_cached=True):
        logging.info(f"Updating cache status for {folder_path} to {is_cached}")  # Debugging statement
        self.database.update_cache_status(folder_path, is_cached)
        self.refresh_folder_icon(folder_path)


    # Helper function to normalize paths
    @functools.lru_cache(maxsize=4096)
    def normalize_path(self, path):
        return os.path.normpath(path).lower()


    def _tree_insert(self, parent, index, **kwargs) -> str:
        """Wrapper around tree.insert that keeps path caches up to date."""
        item_id = self.tree.insert(parent, index, **kwargs)
        values = kwargs.get('values')
        if values and values[0] not in ('dummy',):
            key = self.normalize_path(values[0])
            self._node_path_cache[key] = item_id
            # A newly inserted path is definitely in the tree now
            self._node_missing_cache.discard(key)
        return item_id

    def find_node_by_path(self, folder_path):
        """O(1) lookup via positive cache; O(1) return for known-missing paths;
        falls back to tree traversal only once per unknown path."""
        key = self.normalize_path(folder_path)

        # 1. Positive cache hit
        item_id = self._node_path_cache.get(key)
        if item_id:
            if self.tree.exists(item_id):
                return item_id
            # Item was deleted — evict from both caches
            del self._node_path_cache[key]

        # 2. Negative cache hit — path is known NOT to be in the tree
        if key in self._node_missing_cache:
            return None

        # 3. Full traversal (runs only once per unknown path; warms caches)
        for item in self.tree.get_children(''):
            node_values = self.tree.item(item, 'values')
            if node_values and node_values[0] not in ('dummy',):
                node_key = self.normalize_path(node_values[0])
                self._node_path_cache[node_key] = item
                if node_key == key:
                    return item
                if self.tree.item(item, 'open'):
                    child_node = self.find_node_by_path_recursive(item, key)
                    if child_node:
                        return child_node

        # Path not found anywhere — remember this so next call is O(1)
        self._node_missing_cache.add(key)
        return None

    def find_node_by_path_recursive(self, parent_item, folder_path):
        key = self.normalize_path(folder_path)
        for item in self.tree.get_children(parent_item):
            node_values = self.tree.item(item, 'values')
            if node_values and node_values[0] not in ('dummy',):
                node_key = self.normalize_path(node_values[0])
                self._node_path_cache[node_key] = item
                self._node_missing_cache.discard(node_key)
                if node_key == key:
                    return item
                if self.tree.item(item, 'open'):
                    child_node = self.find_node_by_path_recursive(item, key)
                    if child_node:
                        return child_node
        return None


    def refresh_folder_icon(self, folder_path):
        item_id = self.find_node_by_path(folder_path)
        
        if item_id and self.tree.exists(item_id):
            # Do not change drive root icons — parent '' means top-level drive
            parent_id = self.tree.parent(item_id)
            if parent_id == '':
                return

            # Folder nodes only below drive roots
            is_cached = self.database.is_folder_cached(folder_path)
            new_icon = self.folder_treeicon_green if is_cached else self.folder_treeicon
            self.tree.item(item_id, image=new_icon)

     #This function will recursively update the icons for all nodes under the specified folder.
    def refresh_folder_icons_subtree(self, folder_path):
        item_id = self.find_node_by_path(folder_path)
        if item_id:
            self.refresh_folder_icon(folder_path)
            for child in self.tree.get_children(item_id):
                child_path = self.tree.item(child, 'values')[0]
                self.refresh_folder_icons_subtree(child_path)


    def get_available_drives(self):
        if os.name == 'nt':  # Windows
            # win32api.GetLogicalDriveStrings() is instant (no subprocess) vs
            # the old os.popen('wmic ...') which cost ~0.11s on every startup.
            drive_string = win32api.GetLogicalDriveStrings()
            drives = [d for d in drive_string.split('\000') if d]
            return drives
        else:  # Unix-like
            return ['/mnt', '/media']


    def refresh_tree_view(self, target_directory=None):
        """
        Refresh the tree view, focusing on the given directory or the current_directory.
        
        Args:
            target_directory (str): The directory to refresh and focus on. Defaults to current_directory.
        """
        # Determine the target directory
        target_directory = target_directory or self.current_directory

        if os.path.isdir(target_directory):
            logging.info(f"Refreshing tree view for: {target_directory}")  # Debug

            # Locate the corresponding tree node
            item = self.find_node_by_path(target_directory)
            if item:
                logging.info(f"Found tree node for {target_directory}: {item}")  # Debug

                # Open parent nodes to make the target visible
                parent_item = self.tree.parent(item)
                while parent_item:
                    self.tree.item(parent_item, open=True)
                    parent_item = self.tree.parent(parent_item)

                # Select and focus on the target node
                self.tree.selection_set(item)
                self.tree.see(item)
                self.tree.item(item, open=True)

                # Repopulate the child nodes if needed
                self.process_directory(item, target_directory)
                self._heal_open_tree_dummy_rows()
            else:
                logging.info(f"No tree node found for {target_directory}. Attempting to repopulate tree.")
                self.populate_tree()  # Fall back to repopulating the entire tree
                self.refresh_tree_view(target_directory)  # Retry after repopulation
        else:
            logging.info(f"Skipping refresh: {target_directory} is not a directory.")  # Debug

        
    def open_node(self, event):
        # TreeviewOpen can occasionally fire with stale focus; that leaves a visible
        # "dummy" row (empty gap) under the actually opened branch.
        # Resolve multiple candidates, then run a quick self-heal pass.
        candidates = []

        def _item_path(_item_id):
            if not _item_id:
                return None
            try:
                vals = self.tree.item(_item_id, "values")
                if vals and vals[0] and vals[0] != "dummy":
                    return vals[0]
            except Exception:
                return None
            return None

        # 1) Preferred: focus + selected nodes
        for item_id in [self.tree.focus(), *self.tree.selection()]:
            if item_id and item_id not in candidates:
                candidates.append(item_id)

        # 2) Mouse-trigger fallback: row under cursor
        try:
            ey = getattr(event, "y", None)
            if ey is not None:
                row_id = self.tree.identify_row(ey)
                if row_id and row_id not in candidates:
                    candidates.append(row_id)
        except Exception:
            pass

        handled_any = False
        for item_id in candidates:
            path = _item_path(item_id)
            if not path:
                continue
            handled_any = True
            if path.startswith("virtual_library://"):
                self.display_thumbnails(path)
            else:
                self.process_directory(item_id, path)

        if not handled_any:
            logging.debug("[TreeOpen] No valid node candidate to expand.")

        self._heal_open_tree_dummy_rows()

    def _heal_open_tree_dummy_rows(self):
        """
        Repair occasional stale 'dummy' placeholders under already-open nodes.
        These placeholders render as one empty row in the left tree.
        """
        max_scan = 1000
        scanned = 0
        queue = list(self.tree.get_children(""))

        while queue and scanned < max_scan:
            item_id = queue.pop(0)
            scanned += 1

            try:
                queue.extend(self.tree.get_children(item_id))
            except Exception:
                continue

            try:
                if not self.tree.item(item_id, "open"):
                    continue
                vals = self.tree.item(item_id, "values")
                if not vals or not vals[0] or vals[0] in ("dummy",) or vals[0].startswith("virtual_library://"):
                    continue
                children = self.tree.get_children(item_id)
                has_dummy = any(
                    (self.tree.item(ch, "values") and self.tree.item(ch, "values")[0] == "dummy")
                    for ch in children
                )
                if has_dummy:
                    self.process_directory(item_id, vals[0])
            except Exception:
                continue

    
    def get_full_path(self, node):
        if not node:
            return None
        values = self.tree.item(node, "values")
        if values:
            return values[0]  # Full path is stored here
        return None
    
    def get_full_pathOld(self, node):
        path = []
        while node:
            text = self.tree.item(node)['text']
            if text:  # Ensure the text is not empty
                path.append(text)
            node = self.tree.parent(node)  # Move to the parent node
        path.reverse()  # Build the path from root to leaf
        if path:  # Ensure there is a valid path to join
            return os.path.join(*path)
        return None  # Return None if no path could be constructed

    
    def go_to_parent_directory(self):
        parent_dir = os.path.dirname(self.current_directory)
        if parent_dir != self.current_directory:  # Check if not already at the root
            self.current_directory = parent_dir
            self.display_thumbnails(self.current_directory)
            
    
    def update_panel_info(self, path):
        """Update info panel asynchronously (non-blocking)."""
        if not self.preview_on:
              return
        
        if not os.path.exists(path):
            logging.info(f"[ERROR] update_panel_info: path does not exist: {path}")
            return

        # Reset Video tab immediately so stale data from previous file isn't visible
        if self.info_panel and path.lower().endswith(VIDEO_FORMATS):
            self.info_panel.reset_video_tab()

        def worker():
            try:
                metadata = get_file_metadata(path)
                if not isinstance(metadata, dict):
                    logging.info(f"[ERROR] Metadata for {path} is invalid: {metadata}")
                    return

                metadata["file_path"] = path
                metadata["filename"] = os.path.basename(path)

                rating = self.database.get_rating(path)
                keywords = self.database.get_keywords(path)

                metadata["rating"] = rating
                metadata["keywords"] = keywords

                self.after(0, lambda: self.info_panel.update_info(metadata, rating, keywords) if self.info_panel else None)
            except Exception as e:
                logging.info(f"[ERROR] Failed to update panel info for {path}: {e}")

        # Run off main thread so UI stays responsive
        threading.Thread(target=worker, daemon=True).start()
            
    
    def select_item(self, event):
        """
        Handle selection of a tree item.
        Distinguishes between user tree navigation (loads folder) 
        and selection sync from thumbnails (updates info only).
        """
        # Guard check: skip if menu was recently interacted with
        if time.time() - self._last_menu_interaction_time < 0.2:
            return
        if time.monotonic() < getattr(self, "_ignore_pointer_navigation_until", 0.0):
            return

        selection = self.tree.selection()
        if not selection:
            return
            
        item_id = selection[0]
        path = self.get_full_path(item_id)
        if not path:
            return

        # Always update the info panel (metadata, tags, etc.)
        self.update_panel_info(path)

        # Programmatic tree sync (thumb → tree) must not start a folder load / debounce.
        if getattr(self, "_suppress_tree_select_navigation", False):
            return

        # 🔥 SYNC PROTECTION: Only "jump" into folder if the Treeview actually has focus.
        # This prevents the right pane from reloading when you just click a folder thumbnail.
        if self.focus_get() != self.tree:
            # We still want to update info, but we stop here to avoid jumping in
            return

        # Prevents redundant loading of the same path
        if hasattr(self, '_last_processed_path') and self._last_processed_path == path:
            return
        self._last_processed_path = path

        self.stop_preview()

        if os.path.isdir(path) or path.startswith("virtual_library://"):
            # Debounce: cancel any pending load scheduled within the last 150ms.
            # This prevents redundant loads when the user rapidly clicks through folders.
            if self._debounce_job is not None:
                self.after_cancel(self._debounce_job)
            self._debounce_job = self.after(150, lambda p=path: self._start_loading_folder(p))
            self.add_to_recent_directories(path)
            self.save_tree_state()
    
    
    def _start_loading_folder(self, path):
        """
        Calls the main display function to refresh thumbnails.
        """
        self._debounce_job = None
        if time.monotonic() < getattr(self, "_ignore_pointer_navigation_until", 0.0):
            return
        if getattr(self, "_suppress_tree_select_navigation", False):
            return
        self.display_thumbnails(path)
            
   
    def sort_thumbnails(self, files_list, sort_option=None, filter_option=None):
        """sort_option, filter_option: pass when called from worker thread (avoids Tkinter from non-main thread)."""
        if sort_option is None:
            sort_option = self.sort_option.get()
        if filter_option is None:
            filter_option = self.filter_option.get()

        def sort_key(f):
            if f['is_folder']:
                return (0, f['name'].lower())  # Folders come first
            else:
                try:
                    path = f['path']
                    if sort_option == "Filename":
                        return (1, f['name'].lower())
                    elif sort_option == "Size":
                        return (1, os.path.getsize(path))
                    elif sort_option == "Date":
                        return (1, os.path.getmtime(path))
                    elif sort_option == "Dimensions":
                        dimensions = self.get_video_dimensions(path)
                        if isinstance(dimensions, tuple):
                            return (1, dimensions[0] * dimensions[1])  # Example: sort by area
                        return (1, dimensions)
                    elif sort_option == "File Type":
                        return (1, os.path.splitext(path)[-1].lower())  # Sort by file extension
                    else:
                        return (1, f['name'].lower())  # Default to filename if no valid option is found
                except Exception as e:
                    logging.info(f"Error sorting {f['name']} by {sort_option}: {e}")
                    return (1, f['name'].lower())  # Fallback to sorting by filename in case of an error

        # Filter files based on the filter option
        if filter_option == "Images":
            files_list = [f for f in files_list if f['path'].lower().endswith(IMAGE_FORMATS)]
        elif filter_option == "Videos":
            files_list = [f for f in files_list if f['path'].lower().endswith(VIDEO_FORMATS)]

     # Filter files based on the selected rating
        if hasattr(self, 'selected_rating') and self.selected_rating > 0:
            files_list = [
                f for f in files_list 
                if (record := self.database.get_entry(f['path'])) and record.get('rating', 0) == self.selected_rating
            ]

        return sorted(files_list, key=sort_key)

 
    def get_video_dimensions(self, file_path):
        cap = None
        try:
            cap = cv2.VideoCapture(file_path)
            if cap.isOpened():
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                return width, height
            return 0, 0
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass


    #function to synchronize, if i slect folder in thumb view so will be synced in left tree
    def select_current_folder_in_tree(self):
        self._tree_sync_after_id = None
        # Ensure the current_directory is a directory
        if os.path.isdir(self.current_directory):
            # Block <<TreeviewSelect>> from treating programmatic sync as user navigation.
            self._suppress_tree_select_navigation = True
            try:
                item = self.find_node_by_path(self.current_directory)
                logging.info(f"SELECT TREE ITEM:  {self.current_directory}: {item}")  # Debug
                if item:
                    logging.info(f"Found node for {self.current_directory}: {item}")  # Debug
                    parent_item = self.tree.parent(item)
                    while parent_item:
                        self.tree.item(parent_item, open=True)
                        parent_item = self.tree.parent(parent_item)

                    self.tree.selection_set(item)
                    self.tree.focus(item)
                    self.tree.see(item)
                    self.tree.item(item, open=True)
                else:
                    logging.info(f"Node for {self.current_directory} not found in tree.")  # Debug
                    self.expand_tree_to_path(self.current_directory, select_final_node=True)
                # Programmatic open=True often does not fire <<TreeviewOpen>>, so dummy
                # placeholder rows (expand chevrons) never get replaced — visible as a gap.
                self._heal_open_tree_dummy_rows()
            finally:
                self.after_idle(lambda: setattr(self, "_suppress_tree_select_navigation", False))
        else:
            # If it's a file, avoid processing it as a directory
            logging.info(f"{self.current_directory} is a file, not a directory. Skipping folder synchronization.")  # Debug
            self.tree.selection_remove(self.tree.selection())  # Clear any tree selection


def launch_only():
    root = ctk.CTk()
    app = VideoThumbnailPlayer(root)
    # root.mainloop() intentionally skipped


def profile_visible_thumbnails():
    logging.info("Benchmark: two-folder load...")
    
    root = ctk.CTk()
    app = VideoThumbnailPlayer(root)
    root.update_idletasks() 

    logging.info("--- Load folder 1: c:\\Images\\videos ---")
    app.display_thumbnails("c:\\Images\\videos")

    # Then load second folder (clears previous thumbnails)
    logging.info("--- Load folder 2: c:\\Images\\Foto_Srilanka_2020 ---")
    app.display_thumbnails("c:\\Images\\Foto_Srilanka_2020")

    logging.info("Waiting for thread pool to finish...")
    app.executor.shutdown(wait=True)
    logging.info("Profiling run finished.")




def profile_grid_render():
    root = ctk.CTk()
    app = VideoThumbnailPlayer(root)

    def delayed_display():
        app.display_thumbnails("c:\\Images\\Foto_Srilanka_2020")
        logging.info("Grid rendering done.")
        root.after(2000, root.destroy)

    root.after(100, delayed_display)
    root.mainloop()


