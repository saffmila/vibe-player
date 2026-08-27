"""Thumbnail grid pipeline mixin for VideoThumbnailPlayer."""
from __future__ import annotations

import ctypes
import faulthandler
import json
import logging
import math
import mimetypes
import os
import queue
import threading
import time

import customtkinter as ctk
import tkinter as tk
import tkinter.font as tkfont
import tkinter.ttk as ttk
from tkinter import messagebox
import tkinterdnd2 as dnd

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageTk

from file_operations import *
from gui_elements import (
    append_rating_submenu,
    create_search_window,
    get_conflict_rename_path,
    open_conflict_dialog,
    open_file_op_progress_dialog,
)
from image_operations import create_image_viewer, image_file_exists, notify_missing_image
from image_loader import load_pil_image, get_pil_image_size
from image_compare_dialog import open_image_compare_dialog
from video_compare_dialog import open_video_compare_dialog
from image_resize_dialog import (
    IMAGE_TRANSFORM_LABELS,
    image_reencode_is_lossy,
    open_batch_resize_dialog,
    resize_image_file,
    transform_image_file,
)
from batch_processing_dialog import (
    build_output_path,
    open_batch_process_dialog,
    process_one_image,
)
from video_operations import VideoPlayer
from video_convert import open_convert_video_dialog
from video_merge import open_merge_videos_dialog
from utils import get_video_size
from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS, preview_skip_subdir
from virtual_folders import load_virtual_folders
from hotkeys import DEFAULT_HOTKEYS, menu_accel, rename_accelerators_label
from bookmark_manager import BookmarkManager
from external_apps import append_external_apps_cascade
from folder_scroll_state import (
    clamp_yview,
    normalize_scroll_path,
    remember_folder_scroll,
)


def _norm_video_path(path) -> str:
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))
    except (OSError, ValueError, TypeError):
        return os.path.normcase(os.path.normpath(str(path)))


def _video_player_is_live(player) -> bool:
    if player is None:
        return False
    if getattr(player, "_cleaning_up", False) or getattr(player, "_cleanup_done", False):
        return False
    window = getattr(player, "video_window", None)
    if window is None:
        return False
    try:
        return bool(window.winfo_exists())
    except Exception:
        return False


class _BookmarkSeekProxy:
    """Provide a stable bookmark-manager API over ``VideoPlayer`` objects."""

    def __init__(self, video_player, video_path=None, controller=None):
        self.video_player = video_player
        self.video_path = video_path or (
            getattr(video_player, "video_path", None) if video_player else None
        )
        self.controller = controller

    def _resolve_player(self):
        player_obj = self.video_player
        want = _norm_video_path(self.video_path)
        if player_obj and _video_player_is_live(player_obj) and (
            not want or _norm_video_path(getattr(player_obj, "video_path", None)) == want
        ):
            return player_obj
        ctrl = self.controller
        if not ctrl or not want:
            return None
        for candidate in (
            getattr(ctrl, "current_video_window", None),
            getattr(ctrl, "active_player", None),
        ):
            if (
                candidate
                and _video_player_is_live(candidate)
                and _norm_video_path(getattr(candidate, "video_path", None)) == want
            ):
                return candidate
        return None

    def set_time(self, target_time_seconds: float) -> None:
        """Seek the wrapped player to a target time in seconds."""
        ctrl = self.controller
        if ctrl and getattr(ctrl, "_video_player_switching", False):
            return
        player_obj = self._resolve_player()
        if player_obj is None:
            return

        t = max(0.0, float(target_time_seconds))
        ms = int(t * 1000)

        # Preferred API (already seconds-based).
        if hasattr(player_obj, "set_time") and callable(getattr(player_obj, "set_time")):
            player_obj.set_time(t)
            return

        if hasattr(player_obj, "last_position"):
            player_obj.last_position = ms

        # Fallback for ``VideoPlayer`` which exposes ``player.set_time(ms)``.
        inner_player = getattr(player_obj, "player", None)
        if inner_player is not None and hasattr(inner_player, "set_time"):
            inner_player.set_time(ms)

        if hasattr(player_obj, "seek_to_time") and callable(getattr(player_obj, "seek_to_time")):
            try:
                player_obj.seek_to_time(t)
                return
            except Exception:
                pass

        timeline = getattr(player_obj, "timeline_widget", None)
        if timeline is not None and hasattr(timeline, "set_current_time"):
            try:
                timeline.set_current_time(t)
            except Exception:
                pass

    def play(self) -> None:
        """Resume or start playback on the resolved player."""
        player_obj = self._resolve_player()
        if player_obj is None:
            return
        if hasattr(player_obj, "play_video") and callable(getattr(player_obj, "play_video")):
            player_obj.play_video()
            return
        inner_player = getattr(player_obj, "player", None)
        if inner_player is not None and hasattr(inner_player, "play"):
            try:
                inner_player.play()
            except Exception:
                return
            try:
                player_obj.playing = True
            except Exception:
                pass

    def play_from_time(self, target_time_seconds: float) -> None:
        """Seek and play — used by bookmark manager double-click."""
        ctrl = self.controller
        if ctrl and getattr(ctrl, "_video_player_switching", False):
            return
        t = max(0.0, float(target_time_seconds))
        player_obj = self._resolve_player()
        if player_obj is None:
            ctrl = self.controller
            path = self.video_path
            if ctrl and path and os.path.isfile(path):
                self._pending_play_from_time = t
                ctrl.open_video_player(path, os.path.basename(path))
                ctrl.after(450, self._complete_pending_play_from_time)
            return
        self.set_time(t)
        self.play()

    def _complete_pending_play_from_time(self) -> None:
        t = getattr(self, "_pending_play_from_time", None)
        if t is None:
            return
        self._pending_play_from_time = None
        if self._resolve_player() is None:
            return
        self.set_time(t)
        self.play()

    def get_current_time(self) -> float:
        """Return current playback time in seconds."""
        player_obj = self._resolve_player()
        if player_obj is None:
            return 0.0

        if hasattr(player_obj, "get_current_time") and callable(getattr(player_obj, "get_current_time")):
            try:
                return float(player_obj.get_current_time())
            except Exception:
                return 0.0

        inner_player = getattr(player_obj, "player", None)
        if inner_player is not None and hasattr(inner_player, "get_time"):
            try:
                return max(0.0, float(inner_player.get_time()) / 1000.0)
            except Exception:
                return 0.0
        return 0.0

    def set_bookmarks(self, bookmarks):
        """
        Replace bookmarks in the wrapped player and persist them.

        Args:
            bookmarks: List of ``{"time": float, "label": str}`` dictionaries.
        """
        if self.video_player is None and not (self.controller and self.video_path):
            return

        normalized = []
        for item in bookmarks or []:
            if not isinstance(item, dict) or "time" not in item:
                continue
            try:
                ts = float(item.get("time", 0.0))
            except (TypeError, ValueError):
                continue
            label = str(item.get("label", item.get("name", ""))).strip()
            entry = {"name": label, "time": max(0.0, ts)}
            color = BookmarkManager._normalize_hex_color(item.get("color"))
            if BookmarkManager.is_custom_bookmark_color(color):
                entry["color"] = color
            normalized.append(entry)

        player_obj = self._resolve_player()
        if player_obj is not None:
            try:
                player_obj.bookmarks = normalized
            except Exception:
                return
            if hasattr(player_obj, "save_bookmarks") and callable(getattr(player_obj, "save_bookmarks")):
                try:
                    player_obj.save_bookmarks()
                except Exception:
                    pass
        elif self.controller and self.video_path:
            timeline = getattr(self.controller, "timeline_widget", None)
            if timeline and hasattr(timeline, "save_bookmarks_standalone"):
                try:
                    timeline.save_bookmarks_standalone(self.video_path, normalized)
                except Exception:
                    return

        timeline = getattr(self.controller, "timeline_widget", None) if self.controller else None
        if timeline and _norm_video_path(getattr(timeline, "video_path", None)) == _norm_video_path(self.video_path):
            try:
                timeline.update_bookmarks()
                timeline.redraw_timeline()
            except Exception:
                pass


class VtpGridMixin:
    def _initialize_thumbnail_display(self, dir_path):
        """
        Handles the initial setup for displaying thumbnails:
        - Prevents concurrent loads.
        - Cancels pending background jobs from previous loads.
        - Clears the UI and resets state variables.
        - Sets the current directory.
        Returns True if initialization is successful, False if loading is already in progress or path is invalid.
        """
        prev_cd = getattr(self, "current_directory", None)

        # --- Phase 0: Cancel any in-progress load and cancel old after-jobs ---
        # Instead of blocking (return False), we preempt the old load by incrementing
        # _render_id. Every async phase (worker thread, chunk loop) holds a snapshot
        # of render_id and aborts itself when it detects a newer render has started.
        self._render_id += 1
        self._is_loading = True
        logging.info(f"LOCK ACQUIRED [render_id={self._render_id}] for: {os.path.basename(str(dir_path))}")

        # Cancel any pending 'after' jobs from previous loads
        for job_id in self.after_jobs:
            try:
                self.after_cancel(job_id)
            except ValueError:
                # Job might have already been cancelled or finished
                pass # Ignore errors if the job ID is no longer valid
        self.after_jobs.clear()
        logging.info("🧹 Cleared pending background load jobs.")

        # --- Initial Setup ---
        self.load_start_time = time.time() # Start timing the load process
        logging.info(f"⏱️ [TIMER] begin to measure time for folder: {dir_path}")
        self.clear_thumbnails() # Clear UI elements and reset related lists

        # Normalize and set the current directory path
        # Check type before attempting normalization or os operations
        if not isinstance(dir_path, str):
             logging.error(f"Invalid directory path type: {type(dir_path)}. Path: {dir_path}. Cannot proceed.")
             self._is_loading = False # Release lock on error
             logging.info(f"🔑 LOCK RELEASED (Invalid Path Type) for: {os.path.basename(str(dir_path))}")
             # Attempt to show an error or return gracefully
             messagebox.showerror("Error", f"Invalid path type provided: {type(dir_path)}")
             return False # Indicate failure

        # Proceed only if dir_path is a string
        if not dir_path.startswith("virtual_library://"):
            try:
                # Ensure path exists before normalizing
                if not os.path.exists(dir_path):
                     logging.error(f"Directory path does not exist: {dir_path}")
                     # Optionally inform the user
                     messagebox.showerror("Error", f"Directory not found:\n{dir_path}")
                     self._is_loading = False # Release lock
                     logging.info(f"🔑 LOCK RELEASED (Path Not Found) for: {os.path.basename(dir_path)}")
                     return False # Indicate failure
                dir_path = os.path.normpath(dir_path) # Normalize only if it exists
            except TypeError as e:
                 # This might catch issues if dir_path somehow becomes non-string after the initial check
                 logging.error(f"Invalid directory path type during normalization: {type(dir_path)}. Error: {e}")
                 self._is_loading = False # Release lock
                 logging.info(f"🔑 LOCK RELEASED (Normalization Error) for: {os.path.basename(str(dir_path))}")
                 messagebox.showerror("Error", f"Invalid path type: {type(dir_path)}")
                 return False # Indicate failure
            except Exception as e: # Catch other potential os.path errors
                 logging.error(f"Error normalizing path '{dir_path}': {e}", exc_info=True)
                 self._is_loading = False # Release lock
                 logging.info(f"🔑 LOCK RELEASED (Normalization Error) for: {os.path.basename(str(dir_path))}")
                 messagebox.showerror("Error", f"An error occurred processing path:\n{dir_path}")
                 return False # Indicate failure

        # Path is now either a valid normalized path or a virtual library path
        self.current_directory = dir_path
        logging.info(f"--- [START Load] Displaying: {self.current_directory} ---")

        # Sync left tree after an actual folder open (thumb double-click / navigate),
        # not on mere folder-thumb selection.
        try:
            prev_key = (
                os.path.normcase(os.path.normpath(str(prev_cd)))
                if prev_cd
                else None
            )
            new_key = os.path.normcase(os.path.normpath(str(dir_path)))
            if prev_key != new_key and hasattr(self, "_schedule_tree_sync_for_current_dir"):
                self._schedule_tree_sync_for_current_dir(delay_ms=30)
        except Exception:
            pass

        _nav_clear = getattr(
            self, "_maybe_clear_folder_cache_mark_blocks_after_display_nav", None
        )
        if callable(_nav_clear):
            _nav_clear(prev_cd, dir_path)

        # Immediately stop the previous folder watcher + cancel debounce so
        # ComfyUI/Explorer events from the old path cannot flood Tk during load.
        stop_watch = getattr(self, "stop_directory_watcher", None)
        if callable(stop_watch):
            try:
                stop_watch()
            except Exception:
                logging.debug("stop_directory_watcher during nav failed", exc_info=True)

        # Drop deferred preview/click work from the previous folder — otherwise a
        # pending after() may still open a missing/stale file and freeze the UI
        # while decoding a huge image on the main thread.
        for attr in ("_preview_timer", "_click_timer"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass
                setattr(self, attr, None)
        ip = getattr(self, "info_panel", None)
        if ip is not None and hasattr(ip, "cancel_pending_preview"):
            try:
                ip.cancel_pending_preview()
            except Exception:
                pass

        # Optional auto-watch of the new folder (off by default).
        if getattr(self, "auto_refresh_folder", False):
            schedule_watch = getattr(self, "_schedule_directory_watcher", None)
            if callable(schedule_watch):
                try:
                    schedule_watch(dir_path)
                except Exception:
                    logging.debug("schedule directory watcher failed", exc_info=True)

        return True # Indicate successful initialization

    def _process_single_entry_for_list(self, entry_or_path):
        """
        Processes a single file or folder path (or os.DirEntry) and returns a dictionary 
        suitable for the self.video_files list.
        Optimized to use DirEntry attributes directly to avoid redundant disk I/O (nt.stat calls).
        """
        try:
            # DirEntry from os.scandir (fast path)
            if hasattr(entry_or_path, 'path'):
                path = entry_or_path.path
                name = entry_or_path.name
                is_dir = entry_or_path.is_dir(follow_symlinks=False)
                is_file = entry_or_path.is_file(follow_symlinks=False)
            # Plain path string (slower fallback)
            else:
                path = entry_or_path
                if not os.path.exists(path):
                    return None
                name = os.path.basename(path)
                is_dir = os.path.isdir(path)
                is_file = os.path.isfile(path)

            if is_dir:
                return {'path': path, 'name': name, 'is_folder': True}
            elif is_file:
                # Supported extensions only
                if name.lower().endswith(VIDEO_FORMATS + IMAGE_FORMATS):
                    return {'path': path, 'name': name, 'is_folder': False}
            
            return None
        except OSError as e:
            logging.error(f"OS Error checking entry '{entry_or_path}': {e}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error processing entry '{entry_or_path}': {e}")
            return None

    def _prepare_thumbnail_data(self, dir_path, sort_option=None, filter_option=None, render_id=None, sort_reverse=None):
        """
        Loads, processes, and sorts the list of files and folders for the given directory path.
        Handles virtual libraries and filesystem errors.
        sort_option, filter_option, sort_reverse: pass when called from worker thread (Tkinter vars not thread-safe).
        render_id: if provided, abort early when a newer display_thumbnails() has started
        (avoids blocking the 2-thread DirLoader pool with obsolete folder loads).
        Returns the sorted list of item dictionaries, or None if an error occurs, empty, or aborted.
        Does not assign ``self.video_files`` — caller must do that after confirming render_id.
        """
        def _aborted():
            return render_id is not None and self._render_id != render_id

        if sort_reverse is None:
            sort_reverse = bool(getattr(self, "sort_reverse", False))

        video_files_list = []  # Use a local list to gather items
        t_scan0 = time.perf_counter()
        try:
            # Load file list (virtual or real)
            if dir_path.startswith("virtual_library://"):
                library_name = dir_path.split("://")[1]
                virtual_data = load_virtual_folders()
                entries = virtual_data.get("virtual_folders", {}).get(library_name, [])
                logging.info(
                    "Processing virtual library '%s' with %d entries.",
                    library_name,
                    len(entries),
                )
                for i, file_path in enumerate(entries):
                    if i & 31 == 0 and _aborted():
                        logging.info(
                            "[Prepare] abort virtual scan rid=%s (superseded)",
                            render_id,
                        )
                        return None
                    entry_data = self._process_single_entry_for_list(file_path)
                    if entry_data:
                        video_files_list.append(entry_data)
                    else:
                        logging.warning(
                            "Skipping invalid or unsupported entry from virtual library '%s': %s",
                            library_name,
                            file_path,
                        )

            else:
                if not os.path.isdir(dir_path):
                    logging.error(f"Path is not a directory: {dir_path}")
                    raise FileNotFoundError(
                        f"Directory not found or is not a directory: {dir_path}"
                    )

                logging.info(f"Processing directory contents for: {dir_path}")
                try:
                    with os.scandir(dir_path) as it:
                        for i, entry in enumerate(it):
                            if i & 31 == 0 and _aborted():
                                logging.info(
                                    "[Prepare] abort scan rid=%s after %d entries (superseded)",
                                    render_id,
                                    i,
                                )
                                return None
                            # Pass DirEntry (not entry.path) to avoid per-file exists/isdir/isfile.
                            entry_data = self._process_single_entry_for_list(entry)
                            if entry_data:
                                # Cheap stats for Size/Date sort — one DirEntry.stat(), not later getsize/mtime.
                                if not entry_data.get("is_folder"):
                                    try:
                                        st = entry.stat(follow_symlinks=False)
                                        entry_data["_size"] = int(st.st_size)
                                        entry_data["_mtime"] = float(st.st_mtime)
                                    except OSError:
                                        pass
                                video_files_list.append(entry_data)
                except PermissionError:
                    raise
                except OSError as e:
                    logging.error(
                        f"OS Error scanning directory '{dir_path}': {e}", exc_info=True
                    )
                    raise Exception(f"Failed to scan directory: {e}") from e

        except FileNotFoundError:
            logging.error(f"Directory not found during data preparation: {dir_path}")
            return None
        except PermissionError:
            logging.error(f"Permission denied during data preparation for: {dir_path}")
            return None
        except Exception as e:
            logging.error(
                f"Unexpected error preparing thumbnail data for {dir_path}: {e}",
                exc_info=True,
            )
            msg = f"Failed to read directory contents:\n{dir_path}"
            self.after(0, lambda m=msg: messagebox.showerror("Error", m))
            return None

        scan_s = time.perf_counter() - t_scan0

        if _aborted():
            logging.info("[Prepare] abort before sort rid=%s (superseded)", render_id)
            return None

        if not video_files_list:
            logging.info("No media files found or processed in directory.")
            return []

        logging.info(
            "Sorting %d collected items... (option=%s, reverse=%s, scan=%.3fs, rid=%s)",
            len(video_files_list),
            sort_option,
            sort_reverse,
            scan_s,
            render_id,
        )
        t_sort0 = time.perf_counter()
        try:
            sorted_items = self.sort_thumbnails(
                video_files_list, sort_option, filter_option, sort_reverse=sort_reverse
            )
        except Exception as e:
            logging.error(
                f"Error during sorting thumbnails for {dir_path}: {e}", exc_info=True
            )
            self.after(
                0, lambda: messagebox.showerror("Error", "Failed to sort directory items.")
            )
            return None

        if _aborted():
            logging.info(
                "[Prepare] abort after sort rid=%s (superseded, sort=%.3fs)",
                render_id,
                time.perf_counter() - t_sort0,
            )
            return None

        if not sorted_items:
            def _empty_ui_update():
                try:
                    self.wide_folders_frame.pack_forget()
                    self.regular_thumbnails_frame.pack_forget()
                    self.regular_thumbnails_frame.pack(
                        side="top", fill="both", expand=True, padx=5, pady=5
                    )
                    self.adjust_scroll_region_and_filler()
                except Exception as e:
                    logging.error(
                        f"Error adjusting UI for empty directory {dir_path}: {e}"
                    )

            self.after(0, _empty_ui_update)
            self.after(0, self.update_status_bar)
            return []

        logging.info(
            "Prepared and sorted %d items. (sort=%.3fs, option=%s, rid=%s)",
            len(sorted_items),
            time.perf_counter() - t_sort0,
            sort_option,
            render_id,
        )
        return sorted_items




        # Insert this function into the VideoThumbnailPlayer class
    def _queue_visible_thumbnails(self, force_refresh, thumbnail_time):
        """
        Calculates the visible grid range, prepares the necessary display frames
        (wide folder and regular grid), and queues the generation/rendering
        of only the immediately visible thumbnails or all wide folders.

        Returns:
            tuple: (items_for_lazy_load, lazy_start_index, show_wide)
                   Information needed for scheduling the background load.
            Returns None if self.video_files is empty or grid/frame setup fails.
        """
        # Check if video_files list is populated (should be by _prepare_thumbnail_data)
        if not self.video_files:
            logging.warning("_queue_visible_thumbnails called but self.video_files is empty.")
            return None # Cannot proceed without items

        # --- Calculate Grid and Visible Range ---
        try:
            self.calculate_grid() # Determines self.columns
            # Check if calculate_grid failed (e.g., canvas not ready, division by zero)
            if not hasattr(self, 'columns') or self.columns <= 0:
                logging.error("Grid calculation failed or resulted in invalid columns. Aborting queue.")
                return None

            self.calculate_visible_grid() # Determines self.visible_range
            # Check if calculate_visible_grid failed
            if not hasattr(self, 'visible_range') or self.visible_range[0] is None or self.visible_range[1] is None:
                 logging.error("Visible grid calculation failed. Aborting queue.")
                 return None
            start_idx, end_idx = self.visible_range
            logging.info(f"Calculated visible range: {start_idx} - {end_idx}")
        except Exception as e:
            logging.error(f"Error during grid calculation: {e}", exc_info=True)
            return None # Abort if grid calculation fails unexpectedly


        # --- Prepare Frames and Determine Item Lists ---
        # Separate into folders and files (self.video_files is already sorted)
        folders_list = [item for item in self.video_files if item.get('is_folder')]
        files_list = [item for item in self.video_files if not item.get('is_folder')]

        # Reset and pack necessary frames based on mode and content
        # Use try-except blocks for robustness in case frames are destroyed unexpectedly
        try:
            # Ensure frames exist before trying to pack/forget
            if hasattr(self, 'wide_folders_frame') and self.wide_folders_frame:
                self.wide_folders_frame.pack_forget()
            if hasattr(self, 'regular_thumbnails_frame') and self.regular_thumbnails_frame:
                self.regular_thumbnails_frame.pack_forget()
        except Exception as e:
            # Log error but might be able to continue if frames are recreated/repacked later
            logging.warning(f"Error forgetting frames (might be harmless if frames are repacked): {e}")

        num_folders = len(folders_list)
        show_wide = self.folder_view_mode.get() == "Wide" and num_folders > 0

        # Pack frames conditionally, ensuring they exist
        try:
            if show_wide:
                if hasattr(self, 'wide_folders_frame') and self.wide_folders_frame:
                     # Check if widget exists before packing
                     if self.wide_folders_frame.winfo_exists():
                        self.wide_folders_frame.pack(side="top", fill="x", expand=False, padx=5, pady=5)
                     else:
                        logging.error("wide_folders_frame does not exist, cannot pack.")
                        # Handle error: maybe recreate the frame or abort
                        return None
                else:
                    logging.error("Attempting to show wide folders, but wide_folders_frame is not initialized.")
                    return None # Cannot proceed without the frame

            # Always pack regular frame if it exists, it's needed for layout/filler
            if hasattr(self, 'regular_thumbnails_frame') and self.regular_thumbnails_frame:
                 if self.regular_thumbnails_frame.winfo_exists():
                    self.regular_thumbnails_frame.pack(side="top", fill="both", expand=True, padx=5, pady=5)
                 else:
                     logging.error("regular_thumbnails_frame does not exist, cannot pack.")
                     # Handle error: maybe recreate the frame or abort
                     return None
            else:
                logging.error("regular_thumbnails_frame is not initialized.")
                return None # Cannot proceed without the frame

        except Exception as e:
             logging.error(f"Error packing frames: {e}", exc_info=True)
             return None # Cannot proceed if frames fail to pack


        # Determine which items to render immediately vs lazy load
        items_to_render_immediately = []
        items_for_lazy_load = []
        lazy_start_index = 0

        if show_wide:
            # Wide mode: Render all folders now, lazy load only files
            items_to_render_immediately.extend(folders_list)
            items_for_lazy_load = files_list
            lazy_start_index = 0 # Lazy file loading starts from index 0 of files_list
        else:
            """
            Standard mode: Render a small visible slice of items, lazy load the rest.
            FIX: Implemented a hard limit (MAX_IMMEDIATE_ITEMS) to prevent GUI freezing.
            Any items beyond this limit are seamlessly pushed to the background lazy loader.
            """
            safe_start_idx = max(0, start_idx)
            safe_end_idx = min(len(self.video_files), end_idx)
 
          # --- LIMIT INITIAL LOAD ---
            # Uses the dynamic parameter from __init__ instead of a hardcoded value
            if (safe_end_idx - safe_start_idx) > self.max_immediate_items:
                safe_end_idx = safe_start_idx + self.max_immediate_items

            if safe_start_idx >= safe_end_idx:
                 # Canvas often reports 0x0 until the first geometry pass — visible_range becomes
                 # (0, 0) and we would lazy-load the entire folder with nothing on screen first.
                 if len(self.video_files) > 0:
                     logging.info(
                         "Visible range empty (e.g. canvas not sized yet); "
                         "loading first %s items immediately.",
                         self.max_immediate_items,
                     )
                     safe_start_idx = 0
                     safe_end_idx = min(len(self.video_files), self.max_immediate_items)
                     items_to_render_immediately = self.video_files[safe_start_idx:safe_end_idx]
                     items_for_lazy_load = self.video_files
                     lazy_start_index = safe_end_idx
                 else:
                     logging.info("No items in the calculated visible range to render immediately.")
                     items_to_render_immediately = []
                     items_for_lazy_load = self.video_files
                     lazy_start_index = 0
            else:
                items_to_render_immediately = self.video_files[safe_start_idx:safe_end_idx]
                items_for_lazy_load = self.video_files # Lazy load applies to the full list
                lazy_start_index = safe_end_idx # Lazy loading starts exactly where immediate load stopped

        logging.info(f"Queueing {len(items_to_render_immediately)} items immediately...")

        # --- Queue ONLY the immediately visible items / all folders in wide mode ---
        queued_count = 0
        for item_info in items_to_render_immediately:
            # Find the correct global index in the original sorted list (self.video_files)
            try:
                # Use path for lookup, assuming it's unique within the current view
                # Add safety check in case item_info['path'] is not available
                item_path = item_info.get('path')
                if item_path is None:
                    logging.warning(f"Skipping immediate item due to missing path: {item_info}")
                    continue
                idx = next(i for i, vf in enumerate(self.video_files) if vf.get('path') == item_path)
            except StopIteration:
                logging.warning(f"Could not find global index for immediate item: {item_path}")
                continue # Skip if index cannot be found
            except Exception as e:
                logging.error(f"Error finding index for immediate item {item_info}: {e}", exc_info=True)
                continue # Skip on unexpected error

            # Defensive check for columns value
            if not hasattr(self, 'columns') or self.columns <= 0:
                 logging.error("Cannot calculate row/col, self.columns is invalid or not set.")
                 continue # Skip item if grid columns are not valid

            row, col = divmod(idx, self.columns)
            
            is_folder = item_info.get('is_folder', False)
            
         
            row, col = self.get_grid_position(idx, is_folder)
                  
            # Determine the correct target frame based on mode and type
            target_frame = self.wide_folders_frame if (show_wide and is_folder) else self.regular_thumbnails_frame

            actual_time_for_video = None
            item_path_str = item_info.get('path', '') # Ensure path is a string
            if thumbnail_time is not None and not is_folder and item_path_str.lower().endswith(VIDEO_FORMATS):
                # Unify all refresh paths to the same absolute timestamp computed
                # from Preferences -> Thumbnail Time.
                actual_time_for_video = self.calculate_thumbnail_time(item_path_str)

            # Ensure target frame exists and is valid before queueing
            # Add extra check .winfo_exists() for robustness
            if target_frame and hasattr(target_frame, 'winfo_exists') and target_frame.winfo_exists():
                try:
                    # Ensure all arguments passed to queue_thumbnail are valid
                    item_name = item_info.get('name', 'Unknown') # Provide default name
                    self.queue_thumbnail(
                        item_path_str, item_name, row, col, idx,
                        is_folder=is_folder,
                        target_frame=target_frame,
                        force_refresh=force_refresh,
                        thumbnail_time=actual_time_for_video,
                        render_id=self._render_id,
                    )
                    queued_count += 1
                except Exception as e:
                     logging.error(f"Error during queue_thumbnail for {item_path_str}: {e}", exc_info=True)
                     # Decide whether to continue or abort based on the severity of the error
                     # continue
            else:
                 logging.warning(f"Skipping immediate queue for {item_path_str} - target frame invalid or destroyed.")

        logging.info(f"Successfully queued {queued_count} items immediately.")
        # Return the necessary info for the next phase (lazy loading)
        return items_for_lazy_load, lazy_start_index, show_wide






    def capture_current_folder_scroll(self) -> None:
        """Save the current virtual-grid yview fraction for ``current_directory``."""
        path = getattr(self, "current_directory", None)
        if not normalize_scroll_path(str(path) if path else ""):
            return
        try:
            if not getattr(self, "_vg_active", False):
                return
            canvas = getattr(self, "canvas", None)
            if canvas is None:
                return
            frac = clamp_yview(canvas.yview()[0])
            if frac is None:
                return
            positions = getattr(self, "folder_scroll_positions", None)
            if positions is None:
                self.folder_scroll_positions = {}
                positions = self.folder_scroll_positions
            remember_folder_scroll(positions, path, frac)
        except Exception:
            logging.debug("[ScrollState] capture failed", exc_info=True)

    def peek_folder_scroll(self, path: str):
        """Return saved yview fraction for ``path``, or None."""
        key = normalize_scroll_path(str(path) if path else "")
        if not key:
            return None
        positions = getattr(self, "folder_scroll_positions", None) or {}
        return clamp_yview(positions.get(key))

    def display_thumbnails(
        self, dir_path, force_refresh=False, thumbnail_time=None, preserve_scroll=False
    ):
        """
        Async flow:
        1. Clear the grid immediately (user sees feedback).
        2. Worker loads and sorts the file list.
        3. Main thread renders the GUI.

        preserve_scroll: if True, restore vertical canvas scroll fraction after reload (virtual grid only). Used e.g. after in-place DnD refresh of the same folder.
        When navigating to another folder, the previous folder's scroll is saved and the
        destination's last scroll (if any) is restored — including after app restart.
        """
        if self._should_refresh_search_results_instead(dir_path):
            self.display_last_search_results()
            return

        was_search_view = getattr(self, "search_results_active", False)
        self._leave_search_results_view(
            clear_results=False,
            clear_action=False,
        )
        if was_search_view and hasattr(self, "status_bar") and self.status_bar:
            self._show_return_to_search_status()

        leaving = getattr(self, "current_directory", None)
        same_folder = False
        try:
            left_key = normalize_scroll_path(str(leaving)) if leaving else None
            dest_key = normalize_scroll_path(str(dir_path)) if dir_path else None
            same_folder = bool(left_key and dest_key and left_key == dest_key)
        except Exception:
            same_folder = False

        # Remember scroll for the folder we are leaving (favorites, tree, etc.).
        if leaving and not same_folder:
            self.capture_current_folder_scroll()

        # Capture before any clear — clear_thumbnails resets yview.
        if preserve_scroll:
            try:
                if getattr(self, "_vg_active", False):
                    frac = max(0.0, min(1.0, float(self.canvas.yview()[0])))
                    self._thumb_reload_preserve_yview = frac
                    if same_folder and leaving:
                        remember_folder_scroll(
                            getattr(self, "folder_scroll_positions", {}),
                            leaving,
                            frac,
                        )
                else:
                    self._thumb_reload_preserve_yview = None
            except Exception:
                self._thumb_reload_preserve_yview = None
        elif not same_folder or not getattr(self, "_vg_active", False):
            # New folder, or first paint (e.g. startup already set current_directory).
            self._thumb_reload_preserve_yview = self.peek_folder_scroll(dir_path)
        else:
            # Same folder already on screen and not preserve_scroll → reset to top.
            self._thumb_reload_preserve_yview = None

        # Force the UI to calculate its actual dimensions before we start
        # self.update_idletasks()

        # 1. Init, cancel stale load, capture render_id (includes clear_thumbnails)
        if self._initialize_thumbnail_display(dir_path) is False:
            return
        my_render_id = self._render_id  # Snapshot — older async phases will abort when they see this changed

        # Remember any folder open (tree, thumb double-click, favorites, …) so
        # restart restores the folder actually on screen — not only last tree click.
        if not same_folder:
            try:
                if hasattr(self, "add_to_recent_directories"):
                    self.add_to_recent_directories(self.current_directory)
            except Exception:
                logging.debug("add_to_recent_directories from display_thumbnails failed", exc_info=True)

        # 2. DB cache only — grid already cleared inside _initialize_thumbnail_display
        self.database.clear_entry_cache()

        # On directory change hide multi-timeline strips and switch to Video mode
        if getattr(self, "multi_viewer", None) and self.multi_viewer and \
                self.multi_viewer.winfo_exists():
            self._show_single_preview()
            self.stop_preview()

        logging.info(f"--- [ASYNC START rid={my_render_id}] worker for: {dir_path} ---")

        # 3. Read Tk variables on main thread (not safe from worker)
        sort_option = self.sort_option.get()
        filter_option = self.filter_option.get()
        sort_reverse = bool(getattr(self, "sort_reverse", False))

        # 4. Heavy work (listdir, sort) in dedicated I/O pool.
        # Prevent starvation when thumbnail workers are busy generating images.
        self.io_executor.submit(
            self._worker_prepare_and_display,
            dir_path,
            force_refresh,
            thumbnail_time,
            my_render_id,
            sort_option,
            filter_option,
            sort_reverse,
        )



        
    def _worker_prepare_and_display(self, dir_path, force_refresh, thumbnail_time, render_id, sort_option, filter_option, sort_reverse=False):
        try:
            # Abort immediately if a newer load has already been requested
            if self._render_id != render_id:
                self._is_loading = False
                return

            # 1. Load data (file listing, sort) — heavy I/O, off main thread.
            # Abort checks inside prepare free the DirLoader pool when the user
            # clicks another folder before the previous scan/sort finishes.
            sorted_file_list = self._prepare_thumbnail_data(
                dir_path, sort_option, filter_option, render_id=render_id, sort_reverse=sort_reverse
            )

            # Check again after the potentially slow I/O
            if self._render_id != render_id:
                self._is_loading = False
                return

            if sorted_file_list is None:
                self._is_loading = False

                def _adjust_no_data():
                    self._thumb_reload_preserve_yview = None
                    self.adjust_scroll_region_and_filler()

                self.after(0, _adjust_no_data)
                return

            # Only the winning render_id may publish the file list.
            self.video_files = sorted_file_list
            self.after(0, self.update_status_bar)

            if not self.video_files:
                self._is_loading = False

                def _empty_ui_update():
                    try:
                        self.wide_folders_frame.pack_forget()
                        self.regular_thumbnails_frame.pack_forget()
                        self.regular_thumbnails_frame.pack(
                            side="top", fill="both", expand=True, padx=5, pady=5
                        )
                        self.adjust_scroll_region_and_filler()
                    except Exception as e:
                        logging.error(
                            "Error adjusting UI for empty directory %s: %s", dir_path, e
                        )
                    self._thumb_reload_preserve_yview = None

                self.after(0, _empty_ui_update)
                return

            # Build path→index map once here in background so UI chunks don't repeat this
            self.current_path_map = {vf['path']: i for i, vf in enumerate(self.video_files)}

            def finalize_on_main_thread():
                # Abort if preempted while waiting in the after(0) queue
                if self._render_id != render_id:
                    self._is_loading = False
                    return

                if not self.video_files:
                    self._is_loading = False
                    self._thumb_reload_preserve_yview = None
                    self.clear_thumbnails()
                    self.adjust_scroll_region_and_filler()
                    return

                # Canvas virtual grid (standard + wide rows): tuning lives in vtp_virtual_grid.init_virtual_grid
                try:
                    t0 = time.perf_counter()
                    self.activate_virtual_grid(list(self.video_files))
                    self._vg_start_async_generation(force_refresh, thumbnail_time, render_id)
                    logging.info(
                        "[TIMING rid=%s] finalize (virtual grid): %.3fs",
                        render_id,
                        time.perf_counter() - t0,
                    )
                except Exception as e:
                    logging.error("Virtual grid finalize failed: %s", e, exc_info=True)
                    self._thumb_reload_preserve_yview = None
                    self.clear_thumbnails()
                    self.adjust_scroll_region_and_filler()

                self._is_loading = False

            self.after(0, finalize_on_main_thread)

        except Exception as e:
            logging.error("Worker error: %s", e, exc_info=True)
            self._is_loading = False




        # Add this helper function
    def adjust_scroll_region_and_filler(self):
        """Calculates and sets the canvas scrollregion based on total content height."""
        if getattr(self, "_vg_active", False):
            return
        if getattr(self, "search_results_active", False):
            self._adjust_search_scrollregion_to_widgets()
            return

        # self.update_idletasks() # Ensure frame sizes are current

        total_content_height = 0
        pad_e = self.effective_thumb_cell_padding()
        padding_y = pad_e * 2

        # Calculate height from wide folders frame if visible
        try:
            if self.wide_folders_frame.winfo_ismapped():
                # self.wide_folders_frame.update_idletasks()
                total_content_height += self.wide_folders_frame.winfo_reqheight() + padding_y
        except Exception as e:
            logging.warning(f"Error getting wide_folders_frame height: {e}")


        # Calculate height from regular thumbnails frame
        num_regular_items = len([item for item in self.video_files if not (self.folder_view_mode.get() == "Wide" and item.get('is_folder'))])
        num_regular_rows = math.ceil(num_regular_items / max(1, self.columns))

        # Use the calculated total thumb height
        try:
            thumb_h = self.thumbnail_size[1]
            border_size = self.effective_thumb_border_size()
            label_space = 10
            canvas_height_single = thumb_h + (border_size * 2) + label_space # Includes label space
            total_thumb_height = canvas_height_single + padding_y
            if total_thumb_height <= 0: total_thumb_height = self.thumbnail_size[1] + 40 # Fallback
        except AttributeError:
             logging.warning("Thumbnail border/padding attributes not found for height calc, using fallback.")
             total_thumb_height = self.thumbnail_size[1] + 40 # Fallback

        total_content_height += num_regular_rows * total_thumb_height

        # Add some buffer
        total_content_height += 20

        # Set the scrollregion
        canvas_w = self.canvas.winfo_width()
        # Ensure width is positive
        canvas_w = max(1, canvas_w)
        self.canvas.configure(scrollregion=(0, 0, canvas_w, total_content_height))
        logging.info(f"Scrollregion set to (0, 0, {canvas_w}, {total_content_height})")

        # Adjust filler
        try:
            # Update needed before getting heights
            # self.update_idletasks()
            scrollable_h = self.scrollable_frame.winfo_reqheight()
            canvas_h = self.canvas.winfo_height()
            needed_filler = canvas_h - scrollable_h
            # Ensure filler height is at least 1
            filler_h = max(1, needed_filler)
            self.filler.configure(height=filler_h)
            logging.info(f"Filler height set to {filler_h} (CanvasH={canvas_h}, ScrollableH={scrollable_h})")
            # Ensure filler is packed at the top
            self.filler.pack_forget()
            self.filler.pack(side="top", fill="x")
        except Exception as e:
            logging.warning(f"Error adjusting filler: {e}")

     
        #
    # Legacy progressive render path (kept for reference)
    #
    def _worker_generate_all_thumbnails(self, dir_path, force_refresh=False, thumbnail_time=None):
        """
        This worker function now prepares and PRE-SORTS the data into folders and files.
        """
        thread_name = threading.current_thread().name
        logging.info("[%s] Starting to process folder: %s", thread_name, os.path.basename(dir_path))
        
        try:
            # Get the list of all files and folders
            if dir_path.startswith("virtual_library://"):
                self.process_virtual_library(dir_path)
            else:
                self.process_directory_contents(dir_path)
            
            # Sort all items together first to maintain the chosen sort order
            sorted_items = self.sort_thumbnails(self.video_files)
            
            if not sorted_items:
                logging.info("No media files found in directory.")
                # Schedule the renderer with empty lists to clear the view
                self.after(0, lambda: self._start_progressive_render({'folders': [], 'files': []}, force_refresh, thumbnail_time))
                return

            # Now, partition the sorted list into two separate lists
            folders_list = [item for item in sorted_items if item.get('is_folder')]
            files_list = [item for item in sorted_items if not item.get('is_folder')]
            
            # Create a dictionary to hold both lists
            final_data = {'folders': folders_list, 'files': files_list}
            
            # Schedule the rendering function with this new data structure
            self.after(0, lambda: self._start_progressive_render(final_data, force_refresh, thumbnail_time))

        except Exception as e:
            logging.error("Catastrophic error in worker for '%s': %s", dir_path, e, exc_info=True)



    def _start_progressive_render(self, sorted_data, force_refresh=False, thumbnail_time=None):
        """
        Renders content into separate frames using a GLOBAL UNIQUE INDEX for each item
        to prevent selection collisions.
        """
        
        try:
        
            num_folders = len(sorted_data['folders'])
            num_files = len(sorted_data['files'])
            logging.info("Rendering %d folders and %d files...", num_folders, num_files)
            
            self.video_files = sorted_data['folders'] + sorted_data['files']
            self.update_status_bar()
            self.calculate_grid()
            
            # Pack wide vs regular thumbnail frames
            self.wide_folders_frame.pack_forget()
            self.regular_thumbnails_frame.pack_forget()
            if self.folder_view_mode.get() == "Wide" and num_folders > 0:
                self.wide_folders_frame.pack(side="top", fill="x",expand=False, padx=5, pady=5)
            self.regular_thumbnails_frame.pack(side="top", fill="both", expand=True, padx=5, pady=5)

            items_for_grid = []
            if self.folder_view_mode.get() == "Wide":
                for index, folder_info in enumerate(sorted_data['folders']):
                    self.queue_thumbnail(
                        folder_info['path'], folder_info['name'],
                        index, 0, index,  # row index for wide folder strip
                        is_folder=True, target_frame=self.wide_folders_frame,
                        force_refresh=force_refresh, thumbnail_time=None
                    )
                items_for_grid = sorted_data['files']
            else:
                items_for_grid = self.video_files

            # Global unique index (avoids selection collisions with wide folders)
            for index, item_info in enumerate(items_for_grid):
                row, col = divmod(index, self.columns)
                
                # e.g. 10 wide folders → first file index 10, not 0
                global_index = num_folders + index  if self.folder_view_mode.get() == "Wide" else index

                self.queue_thumbnail(
                    item_info['path'], item_info['name'],
                    row, col, global_index,
                    is_folder=item_info.get('is_folder', False),
                    target_frame=self.regular_thumbnails_frame,
                    force_refresh=force_refresh, thumbnail_time=thumbnail_time
                )
            if not getattr(self, "_vg_active", False):
                self.update_idletasks()
                self.adjust_scroll_region_and_filler()
                self.canvas.yview_moveto(0)
        finally:
             self._is_loading = False


    def _start_search_progressive_render(self, sorted_data, force_refresh=False, thumbnail_time=None):
        """Render search results in small Tk chunks so the window stays responsive."""
        try:
            self._search_legacy_render_active = True
            num_folders = len(sorted_data['folders'])
            num_files = len(sorted_data['files'])
            logging.info("Rendering search results in chunks: %d folders and %d files...", num_folders, num_files)

            self.video_files = sorted_data['folders'] + sorted_data['files']
            self.update_status_bar()
            self.calculate_grid()

            self.wide_folders_frame.pack_forget()
            self.regular_thumbnails_frame.pack_forget()
            show_wide = self.folder_view_mode.get() == "Wide" and num_folders > 0
            if show_wide:
                self.wide_folders_frame.pack(side="top", fill="x", expand=False, padx=5, pady=5)
            self.regular_thumbnails_frame.pack(side="top", fill="both", expand=True, padx=5, pady=5)

            render_plan = []
            if show_wide:
                for index, folder_info in enumerate(sorted_data['folders']):
                    row, col = divmod(index, self.numwidefolders_in_col)
                    render_plan.append((folder_info, row, col, index, self.wide_folders_frame))
                for index, item_info in enumerate(sorted_data['files']):
                    row, col = divmod(index, self.columns)
                    render_plan.append((item_info, row, col, num_folders + index, self.regular_thumbnails_frame))
            else:
                for index, item_info in enumerate(self.video_files):
                    row, col = divmod(index, self.columns)
                    render_plan.append((item_info, row, col, index, self.regular_thumbnails_frame))

            try:
                self.canvas.yview_moveto(0)
            except Exception:
                pass
            self._render_search_results_chunk(
                render_plan,
                0,
                force_refresh,
                thumbnail_time,
                getattr(self, "_search_request_id", None),
            )
        except Exception as e:
            logging.error("Search chunked render failed: %s", e, exc_info=True)
            self._is_loading = False

    def _render_search_results_chunk(self, render_plan, start_index, force_refresh, thumbnail_time, search_id):
        if search_id is not None and search_id != getattr(self, "_search_request_id", None):
            return

        idx = start_index
        total = len(render_plan)
        chunk_started = time.perf_counter()
        processed = 0
        max_items = 4
        time_budget_s = 0.008

        while idx < total and processed < max_items and (time.perf_counter() - chunk_started) < time_budget_s:
            item_info, row, col, global_index, target_frame = render_plan[idx]
            if target_frame and target_frame.winfo_exists():
                self.queue_thumbnail(
                    item_info['path'],
                    item_info['name'],
                    row,
                    col,
                    global_index,
                    is_folder=item_info.get('is_folder', False),
                    target_frame=target_frame,
                    force_refresh=force_refresh,
                    thumbnail_time=thumbnail_time,
                )
            idx += 1
            processed += 1

        if total > 0 and hasattr(self, "status_bar") and self.status_bar:
            queued_pct = 90 + (idx / total) * 10
            self.status_bar.update_progress(min(99, queued_pct))
            found_total = self._search_total_available(total)
            self.status_bar.set_action_message(f"Found {found_total} | Queue {idx}/{total}")

        if idx < total:
            self.after(
                10,
                lambda nxt=idx: self._render_search_results_chunk(
                    render_plan,
                    nxt,
                    force_refresh,
                    thumbnail_time,
                    search_id,
                ),
            )
            return

        self._is_loading = False
        if hasattr(self, "status_bar") and self.status_bar:
            shown_count = int(getattr(self, "_search_loaded_count", 0) or len(getattr(self, "current_search_results", []) or []))
            self.status_bar.set_action_message(self._format_search_status_detail(shown_count))
        self._schedule_search_scrollregion_refresh()

    def _append_search_results_render(self, new_results, start_index, force_refresh=False, thumbnail_time=None):
        """Append one page of search results without rebuilding already visible widgets."""
        if not new_results:
            return
        try:
            if not getattr(self, "search_results_active", False):
                return
            self.update_status_bar()
            self.calculate_grid()

            if not self.regular_thumbnails_frame.winfo_ismapped():
                self.regular_thumbnails_frame.pack(side="top", fill="both", expand=True, padx=5, pady=5)

            render_plan = []
            for offset, item_info in enumerate(new_results):
                global_index = start_index + offset
                row, col = divmod(global_index, self.columns)
                render_plan.append((item_info, row, col, global_index, self.regular_thumbnails_frame))

            self._render_search_results_chunk(
                render_plan,
                0,
                force_refresh,
                thumbnail_time,
                getattr(self, "_search_request_id", None),
            )
        except Exception as e:
            logging.error("Search append render failed: %s", e, exc_info=True)

    def _schedule_search_scrollregion_refresh(self):
        for delay in (0, 120, 350, 800):
            try:
                self.after(delay, self._adjust_search_scrollregion_to_widgets)
            except Exception:
                pass

    def _request_search_scrollregion_refresh(self):
        if getattr(self, "_search_scrollregion_after_id", None):
            return
        try:
            self._search_scrollregion_after_id = self.after(120, self._run_search_scrollregion_refresh)
        except Exception:
            self._search_scrollregion_after_id = None

    def _run_search_scrollregion_refresh(self):
        self._search_scrollregion_after_id = None
        self._adjust_search_scrollregion_to_widgets()

    def _adjust_search_scrollregion_to_widgets(self):
        if not getattr(self, "search_results_active", False):
            return
        if getattr(self, "_vg_active", False):
            return
        try:
            self.update_idletasks()
            bbox = self.canvas.bbox("all")
            if bbox:
                self.canvas.configure(scrollregion=bbox)
            else:
                canvas_w = max(1, self.canvas.winfo_width())
                canvas_h = max(1, self.canvas.winfo_height())
                self.canvas.configure(scrollregion=(0, 0, canvas_w, canvas_h))
        except Exception as e:
            logging.debug("[Search] scrollregion refresh failed: %s", e)


    def _render_remaining(self, start_index, force_refresh=False, thumbnail_time=None):
        """
        Render remaining thumbnails in the background phase.
        """
        logging.debug(
            "Background render: %d thumbnails remaining",
            len(self.video_files) - start_index,
        )
        for index in range(start_index, len(self.video_files)):
            file_info = self.video_files[index]
            row, col = divmod(index, self.columns)
            self.queue_thumbnail(
                file_info['path'], file_info['name'], row, col, index,
                is_folder=file_info.get('is_folder', False),
                target_frame=self.regular_thumbnails_frame,
                force_refresh=force_refresh,
                thumbnail_time=thumbnail_time
            )


  
    def _render_all_at_once(self, file_list, thumbnail_data):
            """
            Runs in the main thread to quickly render pre-processed thumbnails.
            Optimized with get_grid_position to support Wide folders properly.
            """
            logging.debug("[GUI] Rendering %d prepared thumbnails", len(thumbnail_data))
            self.video_files = file_list
            self.update_status_bar()
            self.calculate_grid()
            self.regular_thumbnails_frame.pack(fill="both", expand=True, pady=5)

            show_wide = self.folder_view_mode.get() == "Wide"

            for data in thumbnail_data:
                idx = data["index"]
                is_folder = data["is_folder"]

                row, col = self.get_grid_position(idx, is_folder)
                
                target_frame = self.wide_folders_frame if (show_wide and is_folder) else self.regular_thumbnails_frame

                self.add_thumbnail_to_grid(
                    data["thumbnail"], 
                    data["file_path"], 
                    data["file_name"],
                    row, 
                    col, 
                    is_folder, 
                    idx,
                    target_frame
                )
            
            total_time = time.time() - self.load_start_time
            logging.debug("[GUI] Render finished in %.2fs", total_time)
      

            
    def get_grid_position(self, global_idx, is_folder):
        """
        Calculates grid coordinates (row, col) based on item type and view mode.
        Uses separate column counts for wide folders and regular thumbnails.
        """
        if self.folder_view_mode.get() == "Wide":
            if is_folder:
                # Use dedicated column count for wide folders from your menu
                return divmod(global_idx, self.numwidefolders_in_col)
            else:
                # Calculate offset for regular files so they start correctly after folders
                if getattr(self, '_last_folder_count_path', None) != self.current_directory:
                    self._cached_folder_count = sum(1 for item in self.video_files if item.get('is_folder', False))
                    self._last_folder_count_path = self.current_directory
                
                grid_idx = global_idx - self._cached_folder_count
                return divmod(grid_idx, self.columns)
        else:
            # Standard mode uses global columns for everything
            return divmod(global_idx, self.columns)        

    def _on_scroll(self, *args):
        """Called when the user moves the scrollbar."""
        self.canvas.yview(*args)

        top, bottom = self.scrollbar.get()

        # Near bottom (>80%), not already loading, more items left → load next chunk
        if bottom > 0.8 and not self.is_loading_more and self.currently_displayed_count < len(self.video_files):
            self.is_loading_more = True  # prevent re-entrancy
            logging.debug("Scroll near end; loading next thumbnail batch")
            self.after(100, self.load_next_thumbnail_batch)

    def load_next_thumbnail_batch(self):
        """Load and display the next chunk of thumbnails."""
        start_index = self.currently_displayed_count
        end_index = min(start_index + self.thumbnail_chunk_size, len(self.video_files))

        if start_index >= end_index:
            logging.debug("All thumbnails already loaded.")
            self.is_loading_more = False
            return

        logging.debug("Rendering thumbnails %s..%s", start_index, end_index - 1)
        for index in range(start_index, end_index):
            file_info = self.video_files[index]
            row, col = divmod(index, self.columns)
            self.queue_thumbnail(
                file_info['path'], file_info['name'], row, col, index,
                is_folder=file_info.get('is_folder', False),
                target_frame=self.regular_thumbnails_frame
            )
        
        self.currently_displayed_count = end_index
        self.after(500, lambda: setattr(self, 'is_loading_more', False))



    def process_virtual_library(self, dir_path):
        library_name = dir_path.split("://")[1]
        entries = load_virtual_folders()["virtual_folders"].get(library_name, [])
        for file_path in entries:
            self.process_entry(file_path)

    def process_entry(self, file_path):
        # Normalize the file path
        file_path = os.path.normpath(file_path)

        if os.path.isdir(file_path):
            # logging.info(f"Processing as directory: {file_path}")
            self.process_directory_entry(file_path, os.path.basename(file_path))
        elif os.path.isfile(file_path):
            # logging.info(f"Processing as file: {file_path}")
            if file_path.lower().endswith(VIDEO_FORMATS):
                self.process_video_file(file_path, os.path.basename(file_path))
            elif file_path.lower().endswith(IMAGE_FORMATS):
                self.process_image_file(file_path, os.path.basename(file_path))
        else:
            logging.info(f"Invalid path type for: {file_path}")



    def process_directory_entry(self, file_path, file_name):
        try:
            self.video_files.append({'path': file_path, 'name': file_name, 'is_folder': True})
            if self.recursive_tree_refresh:
                if self.contains_media_files(file_path):
                    self.database.update_cache_status(file_path, True)
                else:
                    self.database.update_cache_status(file_path, False)
            # Only update the tree icon if the path is already known to be in the tree.
            # Skips expensive full-tree traversal for folders not yet expanded by the user.
            key = self.normalize_path(file_path)
            if key in self._node_path_cache:
                self.refresh_folder_icon(file_path)
        except PermissionError as e:
            logging.info(f"Permission error accessing directory: {file_path}, error: {e}")
        except TypeError as e:
            logging.info(f"TypeError processing directory entry: {e} for file_path: {file_path}")

        


    def process_video_file(self, file_path, file_name):
        try:
            width, height = None, None  # Initialize width and height

            db_entry = self.database.get_entry(file_path)
            if db_entry:
                width = db_entry.get('width')
                height = db_entry.get('height')

            if not (width and height) and self.get_vidsize:
                width, height = get_video_size(file_path)

            # Provide default values if width and height are not set
            width = width if width else 0
            height = height if height else 0

            self.database.add_entry(file_name, file_path, width, height)
            self.video_files.append({'path': file_path, 'name': file_name, 'is_folder': False})
        except Exception as e:
            logging.info(f"Error processing video file {file_path}: {e}")


    def process_image_file(self, file_path, file_name):
        try:
            width, height = 0, 0

            if self.get_imgsize:
                w, h = self.get_image_size(file_path)
                width = w or 0
                height = h or 0

            self.database.add_entry(file_name, file_path, width, height)
            self.video_files.append({'path': file_path, 'name': file_name, 'is_folder': False})
            
        except Exception as e:
            logging.error(f"Error processing image file {file_path}: {e}")
    
    def clear_widgets_in_frame(self, frame):
        """Clear all widgets in a frame safely."""
        for widget in frame.winfo_children():
            try:
                if widget.winfo_exists():
                    widget.destroy()
            except Exception as e:
                logging.info(f"Error destroying widget: {e}")


    def update_cache_status(self, dir_path):
        if self.contains_media_files(dir_path):
            self.database.update_cache_status(dir_path, True)
        else:
            self.database.update_cache_status(dir_path, False)
        self.refresh_folder_icon(dir_path)
    
    def reset_display(self, frame=None, widget_type=None, widget_filter=None):
            """
            Clear widgets of a specific type in a frame with optional filtering.
            Recycles thumbnail frames for fast folder switching.
            """
            target_frame = frame if frame else self.scrollable_frame
            
            if not hasattr(self, '_recycled_frames'):
                self._recycled_frames = []

            # logging.info(f"Attempting to clear widgets in frame: {target_frame} of type: {widget_type}")
            for widget in target_frame.winfo_children():
                if (widget_type is None or isinstance(widget, widget_type)) and (widget_filter is None or widget_filter(widget)):
                    if widget.winfo_exists():  # Ensure the widget still exists
                        try:
                            if isinstance(widget, ctk.CTkFrame) and getattr(widget, 'is_thumbnail_frame', False):
                                widget.grid_forget()  # remove from grid, keep in memory for reuse
                                self._recycled_frames.append(widget)
                                
                                if hasattr(self, "thumbnail_widgets") and widget in self.thumbnail_widgets:
                                    self.thumbnail_widgets.remove(widget)
                                    
                            else:
                                # logging.info(f"Destroying widget: {widget}")
                                widget.destroy()
                                if hasattr(self, "thumbnail_widgets") and widget in self.thumbnail_widgets:
                                    self.thumbnail_widgets.remove(widget)
                        except Exception as e:
                            logging.info(f"Error destroying/recycling widget {widget}: {e}")
                    else:
                        logging.info(f"Skipping stale or non-existent widget: {widget}")




    def get_files_in_directory(self, directory, extensions=None):
        """
        Retrieves a list of file paths in the specified directory, optionally filtered by extensions.
        Uses os.scandir() for significantly improved performance by minimizing disk I/O.
        """
        try:
            if not isinstance(directory, str):
                logging.info(f"Error: Directory is not a string. Received: {type(directory)}")
                return []

            if not os.path.isdir(directory):
                logging.info(f"Error: Directory does not exist: {directory}")
                return []

            files = []
            # os.scandir is much faster than os.listdir because it caches file attributes
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_file():
                        if not extensions or os.path.splitext(entry.name)[1].lower() in extensions:
                            files.append(entry.path)
            return files
        except Exception as e:
            logging.info(f"Error reading directory {directory}: {e}")
            return []


    def process_directory_contents(self, dir_path):
        """
        Processes the contents of a directory (subdirectories, videos, images).
        Optimized using os.scandir() to eliminate redundant nt.stat and os.path.join calls.
        """
        try:
            # Ensure that dir_path is indeed a directory
            if not os.path.isdir(dir_path):
                logging.info(f"Skipping processing for {dir_path} as it is not a directory.")  # Debug
                return

            with os.scandir(dir_path) as entries:
                for entry in entries:
                    if entry.is_dir():
                        self.process_directory_entry(entry.path, entry.name)
                    elif entry.is_file():
                        # Handle files directly, to avoid mixing them with directories
                        name_lower = entry.name.lower()
                        if name_lower.endswith(VIDEO_FORMATS):
                            self.process_video_file(entry.path, entry.name)
                        elif name_lower.endswith(IMAGE_FORMATS):
                            self.process_image_file(entry.path, entry.name)
                        else:
                            logging.info(f"Skipping unknown/unsupported item: {entry.path}")

        except OSError as e:
            logging.info(f"Error accessing directory contents: {e}")
        except Exception as e:
            logging.info(f"Unexpected error processing directory: {dir_path}, error: {e}")


    def folder_contains_media(self, folder_path):
        """Check recursively whether folder or any subfolder contains media."""
        valid_extensions = set(ext.lower() for ext in VIDEO_FORMATS + IMAGE_FORMATS)

        def _walk_has_media(path):
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        try:
                            if entry.is_file(follow_symlinks=False):
                                if os.path.splitext(entry.name)[1].lower() in valid_extensions:
                                    return True
                            elif entry.is_dir(follow_symlinks=False):
                                if _walk_has_media(entry.path):
                                    return True
                        except (OSError, PermissionError):
                            continue
            except (OSError, PermissionError):
                return False
            return False

        return _walk_has_media(folder_path)

    def _folder_has_media_cached(self, folder_path: str) -> bool:
        key = self.database.normalize_path(folder_path)
        # Use the same signature as the wide filmstrip (folder mtime + immediate
        # sub-directory mtimes). Keying on the folder's own mtime alone missed media
        # added one level down (e.g. into a subfolder), so a parent's parent kept
        # showing no filmstrip for it.
        mtime = self._wide_strip_dir_signature(folder_path)
        cached = self._folder_media_presence_cache.get(key)
        # Entries are (dir_signature, has_media). Re-evaluate when the signature
        # changed so a folder that gained media starts showing its wide filmstrip.
        if isinstance(cached, tuple) and cached[0] == mtime:
            return cached[1]
        # Bounded probe (depth/dir/time limited) instead of an unbounded recursive
        # walk: this runs on the main thread for every folder item, and re-runs when
        # mtime changes, so an unbounded walk on a huge tree froze the UI. Media beyond
        # the preview's reach wouldn't render a filmstrip anyway, so this stays correct.
        has_media = bool(self._get_folder_content_for_preview(folder_path, num_files=1))
        self._folder_media_presence_cache[key] = (mtime, has_media)
        return has_media


    def set_wide_folder_columns(self, num_columns):
        """Set the number of columns for wide folders and refresh display."""
        self.numwidefolders_in_col = num_columns
        if hasattr(self, "save_preferences"):
            self.save_preferences()
        self.display_thumbnails(self.current_directory, preserve_scroll=True)  # Refresh the display

    def set_wide_folder_preview_count(self, num_slots):
        """Fixed number of uniform preview slots (with placeholders) in each wide strip."""
        n = max(3, min(10, int(num_slots)))
        self.vg_wide_preview_count = n
        if hasattr(self, "wide_folder_preview_count_var"):
            try:
                self.wide_folder_preview_count_var.set(n)
            except Exception:
                pass
        if hasattr(self, "save_preferences"):
            self.save_preferences()
        # Drop in-memory wide strips so they rebuild with the new slot count
        try:
            if getattr(self, "memory_cache", None) is not None:
                self.memory_cache.clear()
        except Exception:
            pass
        try:
            self._vg_wide_built_mtime.clear()
        except Exception:
            pass
        if getattr(self, "current_directory", None):
            self.display_thumbnails(self.current_directory, preserve_scroll=True)

    def set_wide_folder_gap(self, gap_px: int):
        """Horizontal spacing between tiles in the wide-folder filmstrip."""
        g = max(0, min(40, int(gap_px)))
        if int(getattr(self, "wide_folder_gap", -1)) == g:
            return
        self.wide_folder_gap = g
        self._refresh_wide_folder_strips()

    def _wide_tile_bg_rgba(self):
        """RGBA for filmstrip gutters / tile chrome (Preferences → Debug)."""
        raw = str(getattr(self, "wide_folder_tile_bg", "#000000") or "#000000").strip()
        if not raw.startswith("#"):
            raw = "#" + raw
        hex6 = raw[1:]
        if len(hex6) == 3:
            hex6 = "".join(c * 2 for c in hex6)
        try:
            r = int(hex6[0:2], 16)
            g = int(hex6[2:4], 16)
            b = int(hex6[4:6], 16)
        except (ValueError, IndexError):
            r, g, b = 0, 0, 0
        try:
            a = max(0, min(255, int(getattr(self, "wide_folder_tile_bg_alpha", 255))))
        except (TypeError, ValueError):
            a = 255
        return (r, g, b, a)

    @staticmethod
    def _wide_tile_bg_cache_tag(rgba) -> str:
        r, g, b, a = rgba
        return f"bg{r:02x}{g:02x}{b:02x}a{int(a)}"

    def _refresh_wide_folder_strips(self):
        """Drop cached filmstrips and redraw current folder (preserve scroll)."""
        try:
            if getattr(self, "memory_cache", None) is not None:
                self.memory_cache.clear()
        except Exception:
            pass
        try:
            self._vg_wide_built_mtime.clear()
        except Exception:
            pass
        if getattr(self, "current_directory", None):
            self.display_thumbnails(self.current_directory, preserve_scroll=True)

    def set_wide_folder_tile_bg(self, color_hex: str):
        """Filmstrip container / gutter color (#RRGGBB)."""
        raw = str(color_hex or "").strip()
        if not raw.startswith("#"):
            raw = "#" + raw
        hex6 = raw[1:]
        if len(hex6) == 3:
            hex6 = "".join(c * 2 for c in hex6)
        if len(hex6) != 6:
            return
        try:
            int(hex6, 16)
        except ValueError:
            return
        new = f"#{hex6.lower()}"
        if str(getattr(self, "wide_folder_tile_bg", "")).lower() == new:
            return
        self.wide_folder_tile_bg = new
        self._refresh_wide_folder_strips()

    def set_wide_folder_tile_bg_alpha(self, alpha: int):
        """Filmstrip container opacity (0..255)."""
        a = max(0, min(255, int(alpha)))
        if int(getattr(self, "wide_folder_tile_bg_alpha", -1)) == a:
            return
        self.wide_folder_tile_bg_alpha = a
        self._refresh_wide_folder_strips()

    def set_wide_folder_strip_end_pad(self, pad_px: int):
        """Extend container past first/last thumb (cinematic side bars)."""
        p = max(0, min(120, int(pad_px)))
        if int(getattr(self, "wide_folder_strip_end_pad_px", -1)) == p:
            return
        self.wide_folder_strip_end_pad_px = p
        self._refresh_wide_folder_strips()

    def set_wide_folder_tile_inset(self, inset_px: int):
        """Image inset inside each tile (visible chrome frame)."""
        v = max(0, min(24, int(inset_px)))
        if int(getattr(self, "wide_folder_tile_inset_px", -1)) == v:
            return
        self.wide_folder_tile_inset_px = v
        self._refresh_wide_folder_strips()

    def _refresh_wide_folder_layout(self):
        """Redraw wide folders without regenerating filmstrip PNGs."""
        if getattr(self, "current_directory", None):
            self.display_thumbnails(self.current_directory, preserve_scroll=True)

    def _wide_divider_width(self) -> int:
        try:
            return max(1, min(12, int(getattr(self, "wide_folder_divider_width", 1) or 1)))
        except (TypeError, ValueError):
            return 1

    def _wide_divider_color(self) -> str:
        raw = str(getattr(self, "wide_folder_divider_color", "#4a5056") or "#4a5056").strip()
        if not raw.startswith("#"):
            raw = "#" + raw
        hx = raw[1:]
        if len(hx) == 3:
            hx = "".join(c * 2 for c in hx)
        if len(hx) != 6:
            return "#4a5056"
        try:
            int(hx, 16)
        except ValueError:
            return "#4a5056"
        return f"#{hx.lower()}"

    def set_wide_folder_show_divider(self, show: bool):
        v = bool(show)
        if bool(getattr(self, "wide_folder_show_divider", False)) == v:
            self.vg_wide_show_divider = v
            return
        self.wide_folder_show_divider = v
        self.vg_wide_show_divider = v
        self._refresh_wide_folder_layout()

    def set_wide_folder_divider_color(self, color_hex: str):
        raw = str(color_hex or "").strip()
        if not raw.startswith("#"):
            raw = "#" + raw
        hx = raw[1:]
        if len(hx) == 3:
            hx = "".join(c * 2 for c in hx)
        if len(hx) != 6:
            return
        try:
            int(hx, 16)
        except ValueError:
            return
        new = f"#{hx.lower()}"
        if str(getattr(self, "wide_folder_divider_color", "")).lower() == new:
            return
        self.wide_folder_divider_color = new
        self._refresh_wide_folder_layout()

    def set_wide_folder_divider_width(self, width_px: int):
        w = max(1, min(12, int(width_px)))
        if int(getattr(self, "wide_folder_divider_width", -1)) == w:
            return
        self.wide_folder_divider_width = w
        self._refresh_wide_folder_layout()

    def set_wide_folder_inter_row_gap(self, gap_px: int):
        """Vertical spacing between wide-folder rows."""
        g = max(0, min(80, int(gap_px)))
        if int(getattr(self, "vg_wide_inter_row_gap", -1)) == g:
            return
        self.vg_wide_inter_row_gap = g
        self._refresh_wide_folder_layout()

    def _sync_wide_folder_border_flags(self):
        """Keep VG border flags in sync with wide_folder_borderWidth/Color."""
        try:
            bw = max(0, min(10, int(getattr(self, "wide_folder_borderWidth", 0) or 0)))
        except (TypeError, ValueError):
            bw = 0
        self.wide_folder_borderWidth = bw
        self.vg_wide_border_width = max(1, bw) if bw > 0 else 0
        self.vg_wide_show_border = bw > 0
        self.vg_wide_border_color = getattr(self, "wide_folder_borderColor", "#555555")

    def _refresh_wide_folder_chrome(self):
        """Redraw wide-card outlines without regenerating filmstrip PNGs."""
        self._sync_wide_folder_border_flags()
        refreshed = False
        for slot in getattr(self, "_vg_wide_pool", []) or []:
            if not slot.get("strip_canvas"):
                continue
            try:
                self._vg_redraw_wide_card(slot)
                refreshed = True
            except Exception:
                pass
        if not refreshed:
            self._refresh_wide_folder_layout()

    def set_wide_folder_border_width(self, width_px: int):
        w = max(0, min(10, int(width_px)))
        if int(getattr(self, "wide_folder_borderWidth", -1)) == w:
            return
        self.wide_folder_borderWidth = w
        self._refresh_wide_folder_chrome()

    def set_wide_folder_border_color(self, color_hex: str):
        raw = str(color_hex or "").strip()
        if not raw.startswith("#"):
            raw = "#" + raw
        hx = raw[1:]
        if len(hx) == 3:
            hx = "".join(c * 2 for c in hx)
        if len(hx) != 6:
            return
        try:
            int(hx, 16)
        except ValueError:
            return
        new = f"#{hx.lower()}"
        if str(getattr(self, "wide_folder_borderColor", "")).lower() == new:
            return
        self.wide_folder_borderColor = new
        self._refresh_wide_folder_chrome()

    def set_wide_folder_sel_outline_width(self, width_px: int):
        w = max(1, min(10, int(width_px)))
        if int(getattr(self, "wide_folder_sel_outline_width", -1)) == w:
            return
        self.wide_folder_sel_outline_width = w
        self._refresh_wide_folder_chrome()

    def set_wide_folder_sel_outline_color(self, color_hex: str):
        raw = str(color_hex or "").strip()
        if not raw.startswith("#"):
            raw = "#" + raw
        hx = raw[1:]
        if len(hx) == 3:
            hx = "".join(c * 2 for c in hx)
        if len(hx) != 6:
            return
        try:
            int(hx, 16)
        except ValueError:
            return
        new = f"#{hx.lower()}"
        if str(getattr(self, "wide_folder_sel_outline_color", "")).lower() == new:
            return
        self.wide_folder_sel_outline_color = new
        self._refresh_wide_folder_chrome()
    
    def update_load_time(self, cache_hits, cache_misses, from_cache):
        """Display and update load timing information."""
        load_time = time.time() - self.load_start_time
        load_source = "Cache" if from_cache else "Disk"
        logging.info(f"[Debug] Loaded from {load_source}: {load_time:.2f}s, Cache Hits: {cache_hits}, Cache Misses: {cache_misses}")

        # Update the debug overlay if available
        if hasattr(self, 'debug_overlay'):
            self.debug_overlay.add_load_time(load_time, load_source)


    
        
    # version with double index.. works well with wide folders, but standard folders are not displayed
    def display_visible_thumbnails(self):
        
        

         # Start time for tracking load duration
        self.load_start_time = time.time()
        # Clear the existing thumbnails.. its already cleared in reset_display
        # self.clear_thumbnails()

        allowed_extensions = VIDEO_FORMATS + IMAGE_FORMATS
        visible_files = [file for file in self.video_files[:self.thumbnail_chunk_size] if file['name'].lower().endswith(allowed_extensions) or file['is_folder']]

        # logging.info(f"#### num of visible_files in chunk: {len(visible_files)}")  # Debug

        # Separate files into folders and non-folders
        folder_files = [file for file in visible_files if file['is_folder']]
        regular_files = [file for file in visible_files if not file['is_folder']]

        # Create separate frames for wide folders and regular thumbnails
        # self.wide_folders_frame = ctk.CTkFrame(self.scrollable_frame, fg_color=self.BackroundColor)
        # self.regular_thumbnails_frame = ctk.CTkFrame(self.scrollable_frame, fg_color=self.BackroundColor) #self.thumbBGColor

        # Separate indices for wide folders and regular thumbnails
        wide_index = 0
        thumb_index = 0
        folder_row = 0
        folder_col = 0
        wide_folders_created = False  # Track if any wide folder was created

        # Handle wide folders AND display  folder as standard in case it dont contain media!!
        if self.folder_view_mode.get() == "Wide":
            folder_has_media = {
                folder["path"]: self._folder_has_media_cached(folder["path"])
                for folder in folder_files
            }
            wide_folders = [folder for folder in folder_files if folder_has_media.get(folder["path"], False)]
            if self.show_empty_strips_var.get():
                wide_folders.extend(
                    [folder for folder in folder_files if not folder_has_media.get(folder["path"], False)]
                )
            for folder in wide_folders:
                wide_folders_created = True
                folder_path = folder['path']
                self.wide_folders.append(folder_path)  # Add wide folder path to tracking list
                # Only proceed if the wide folder thumbnail is created
                self.queue_thumbnail(folder['path'], folder['name'], folder_row, folder_col, wide_index, is_folder=True, thumbnail_time=self.thumbnail_time, target_frame=self.wide_folders_frame)

                # Increment wide folder grid position
                folder_col += 1
                if folder_col >= self.numwidefolders_in_col:
                    folder_col = 0
                    folder_row += 1
                wide_index += 1  # Independent counter for wide folders

            if wide_folders_created:
                self.wide_folders_frame.pack(fill="x", pady=5)
            else:
                self.wide_folders_frame.pack_forget()

            # Handle non-media folders in the regular grid
            non_media_folders = [folder for folder in folder_files if not folder_has_media.get(folder["path"], False)]
            for folder in non_media_folders:
                if self.show_empty_strips_var.get():
                    continue
                row, col = divmod(thumb_index, self.columns)
                self.queue_thumbnail(folder['path'], folder['name'], row, col, thumb_index, is_folder=True, thumbnail_time=self.thumbnail_time, target_frame=self.regular_thumbnails_frame)
                thumb_index += 1
                
            self.regular_thumbnails_frame.pack(fill="both", expand=True, pady=5)

            #Handle regular files (images, videos) after wide folders
            for file in regular_files:
                row, col = divmod(thumb_index, self.columns)
                self.queue_thumbnail(file['path'], file['name'], row, col, thumb_index, is_folder=False, thumbnail_time=self.thumbnail_time, target_frame=self.regular_thumbnails_frame)
                thumb_index += 1    
                
  
        # Pack ONLY the regular thumbnails frame        
        else:
                            
           self.regular_thumbnails_frame.pack(fill="both", expand=True, pady=5)
           for index, file in enumerate(visible_files):
                row, col = divmod(index, self.columns)
                self.queue_thumbnail(file['path'], file['name'], row, col, index, is_folder=file['is_folder'], thumbnail_time=self.thumbnail_time,target_frame=self.regular_thumbnails_frame)
 
        
  
    def _on_folder_view_changed(self, *args):
        """
        Trace callback when self.folder_view_mode changes.
        """
        new_mode = self.folder_view_mode.get()
        logging.info(f"Folder view mode changed to '{new_mode}'. Refreshing display.")

        if hasattr(self, 'wide_folders_check_var'):
            is_wide = new_mode == "Wide"
            if self.wide_folders_check_var.get() != is_wide:
                self.wide_folders_check_var.set(is_wide)

        # load_preferences writes this var during init; do not open default_directory
        # before initialize_gui_content restores the last visited folder.
        if not getattr(self, "_initial_folder_loaded", False):
            logging.info("Skipping folder-view refresh until startup folder restore.")
            return

        self.display_thumbnails(self.current_directory, preserve_scroll=True)


    def _on_check_var_changed(self, *args):
        is_wide = self.wide_folders_check_var.get()
        new_mode = "Wide" if is_wide else "Standard"
        if self.folder_view_mode.get() != new_mode:
            self.folder_view_mode.set(new_mode)




        # Insert this function somewhere within the VideoThumbnailPlayer class
    def calculate_visible_grid(self):
        """
        Calculates the range of thumbnail indices currently visible in the viewport.
        Updates self.visible_range (start_index, end_index).
        Uses more precise total thumbnail dimensions including borders and padding.
        """
        # Ensure widgets have their current sizes
        # self.update_idletasks()

        # Viewport dimensions
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        if not canvas_w or not canvas_h or canvas_w <= 1 or canvas_h <= 1:
            # Canvas not rendered yet or has minimal size
            self.visible_range = (0, 0)
            logging.warning(f"calculate_visible_grid: Canvas not ready (w={canvas_w}, h={canvas_h}).")
            return

        thumb_w, thumb_h = self.thumbnail_size
        pad_e = self.effective_thumb_cell_padding()
        padding_x = pad_e * 2
        padding_y = pad_e * 2

        # --- Calculate total thumbnail dimensions including borders/padding ---
        try:
            # Use attributes if they exist (safer)
            border_size = self.effective_thumb_border_size()
            label_space = 10 # Approximate extra height for the label below the image
            canvas_width_single = thumb_w + (border_size * 2)
            total_thumb_width = canvas_width_single + padding_x
            canvas_height_single = thumb_h + (border_size * 2) + label_space
            total_thumb_height = canvas_height_single + padding_y

            # Safety check for zero/negative dimensions
            if total_thumb_width <= 0: total_thumb_width = thumb_w + 20
            if total_thumb_height <= 0: total_thumb_height = thumb_h + 40

        except AttributeError:
             # Fallback if border attributes don't exist yet
            logging.warning("Thumbnail border/padding attributes not found, using fallback dimensions.")
            total_thumb_width = thumb_w + 20 # Fallback width
            total_thumb_height = thumb_h + 40 # Fallback height including estimated label space

        # 1) Calculate columns
        cols = max(1, canvas_w // total_thumb_width)
        self.columns = cols # Update the class attribute

        # 2) Calculate how many rows are visible vertically
        # Add 1 to slightly overestimate, ensuring we fill the screen
        visible_rows = max(1, math.ceil(canvas_h / total_thumb_height))

        # 3) Determine the top visible row based on scroll position
        scroll_y = self.canvas.canvasy(0)
        first_row = max(0, int(scroll_y // total_thumb_height))

        # 4) Calculate the index of the last visible row
        last_row = first_row + visible_rows

        # 5) Calculate the index range in the self.video_files list
        start_idx = first_row * cols
        # Ensure end index doesn't exceed the list length
        end_idx = min(len(self.video_files), (last_row + 1) * cols)

        # Store the calculated range
        self.visible_range = (start_idx, end_idx)
        logging.info(f"Visible grid: Cols={cols}, RowsOnScreen={visible_rows}, ScrollY={scroll_y:.0f}, FirstRow={first_row}, LastRow={last_row}, IndexRange={start_idx}-{end_idx}")
            


    # Calculates the grid layout for thumbnails based on canvas size
    def calculate_grid(self):
        """
        Calculates the number of rows and columns for the thumbnail grid.
        Includes diagnostic prints to debug layout issues on initial load.
        """
        
        # Force geometry update to get real width instead of 1px
        
        # --- SMART STARTUP FIX ---
        # If canvas is not physically rendered yet (width <= 1), force a layout update.
        # This prevents the "single column on startup" issue without slowing down regular browsing.
        if not hasattr(self, '_initial_layout_fixed'):
            self.update_idletasks()  # force geometry pass before reading width
            self._initial_layout_fixed = True
            logging.debug("[Grid] Initial window width calibration done.")

        window_width = self.canvas.winfo_width()
        if window_width < 100:
            window_width = 800
        # Get the current width and height of the canvas widget
     
        window_height = self.canvas.winfo_height()
        
        if not window_width or not window_height or window_width == 1 or window_height == 1:
            self.columns = 1
            self.rows = 1
            return

        thumb_width, thumb_height = self.thumbnail_size

        try:
            b = self.effective_thumb_border_size()
            p = self.effective_thumb_cell_padding()
            canvas_width = thumb_width + (b * 2)
            total_thumb_width = canvas_width + (p * 2)
            canvas_height = thumb_height + (b * 2) + 10
            total_thumb_height = canvas_height + (p * 2)
            if total_thumb_width <= 0: total_thumb_width = thumb_width + 20
            if total_thumb_height <= 0: total_thumb_height = thumb_height + 20
        except AttributeError:
            total_thumb_width = (thumb_width + 14*2) + (6*2)
            total_thumb_height = (thumb_height + 14*2 + 10) + (6*2)

        columns = max(1, int(window_width // total_thumb_width))
        rows = max(1, int(window_height // total_thumb_height))

        self.columns = columns
        self.rows = rows




    
    
    def create_file_thumbnail(self, file_path, file_name, row, col, index, thumbnail_time=None, overwrite=False, target_frame=None, is_refresh=False, render_id=None):
        if not file_name:
            file_name = os.path.basename(file_path or "") or "unknown"
        def worker():
            if render_id is not None and render_id != self._render_id:
                return
            thumbnail = None
            try:
                video_health = "ok"
                if file_name.lower().endswith(VIDEO_FORMATS):
                    video_health = self._get_video_health(file_path)

                memory_cache = self.memory_cache
                # Check cache
                if not overwrite and memory_cache:
                    cached_thumbnail = thumbnail_cache.get(file_path, memory_cache=memory_cache)
                    if cached_thumbnail:
                        thumbnail = cached_thumbnail
                
                # Slow path: generate on background thread
                if thumbnail is None:
                    if file_name.lower().endswith(VIDEO_FORMATS):
                        # Empty files are always unusable; in strict mode we also mark broken videos.
                        if video_health == "empty" or (
                            video_health == "broken" and not bool(getattr(self, "play_broken_videos", True))
                        ):
                            thumbnail = self._create_corrupted_thumbnail_image()
                        else:
                            actual_thumbnail_time = thumbnail_time
                            if actual_thumbnail_time is None:
                                actual_thumbnail_time = self.calculate_thumbnail_time(file_path)
                            thumbnail = create_video_thumbnail(
                                file_path, self.thumbnail_size, self.thumbnail_format,
                                self.capture_method_var.get(), thumbnail_time=actual_thumbnail_time,
                                cache_enabled=self.cache_enabled, overwrite=overwrite,
                                cache_dir=self.thumbnail_cache_path,
                                database=self.database
                            )
                    else:
                        thumbnail = create_image_thumbnail(
                            file_path, self.thumbnail_size, database=self.database, 
                            cache_dir=self.thumbnail_cache_path, overwrite=overwrite
                        )
                    
                    if thumbnail is not None and memory_cache:
                        thumbnail_cache.set(file_path, thumbnail, memory_cache=memory_cache)

                if thumbnail is None:
                    if file_name.lower().endswith(VIDEO_FORMATS):
                        # Metadata can look OK while every frame grab fails — show explicit placeholder.
                        thumbnail = self._create_corrupted_thumbnail_image(
                            "Thumbnail could not be generated"
                        )
                    else:
                        try:
                            default_image_path = "image_icon.png"
                            default_image = Image.open(default_image_path)
                            thumbnail = ctk.CTkImage(
                                light_image=default_image, dark_image=default_image
                            )
                        except Exception as img_exc:
                            logging.info(
                                "image_icon.png fallback failed for %s: %s",
                                file_path,
                                img_exc,
                            )
                            thumbnail = self._create_corrupted_thumbnail_image(
                                "This file could not be read"
                            )

                def update_gui():
                    if render_id is not None and render_id != self._render_id:
                        return
                    if target_frame is not None and str(target_frame).startswith(".") and target_frame.winfo_exists():  
                        self.add_thumbnail_to_grid(thumbnail, file_path, file_name, row, col, is_folder=False, index=index, target_frame=target_frame)
                        if is_refresh:
                            self._restore_selection_visual()  # refresh replaced thumbnail, re-apply selection border
                        if getattr(self, "search_results_active", False):
                            self._request_search_scrollregion_refresh()
                    
                    self.finalize_thread()
                    
                    self.processed_files_count += 1
                    if self.total_files_to_process > 0:
                        progress = (self.processed_files_count / self.total_files_to_process) * 100
                        self.status_bar.update_progress(progress)
                        
                self.after(0, update_gui)
            except Exception as e:
                logging.info(f"Error in background thumb generation for {file_path}: {e}")

        self.executor.submit(worker)

    def _broken_placeholder_font(self, px: int):
        """Scaled TrueType font for broken-video placeholder (Windows + Linux fallbacks)."""
        from PIL import ImageFont

        paths: list[str] = []
        if os.name == "nt":
            windir = os.environ.get("WINDIR", r"C:\Windows")
            paths.extend(
                [
                    os.path.join(windir, "Fonts", "segoeui.ttf"),
                    os.path.join(windir, "Fonts", "arial.ttf"),
                    os.path.join(windir, "Fonts", "calibri.ttf"),
                ]
            )
        else:
            paths.extend(
                [
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                ]
            )
        for p in paths:
            if p and os.path.isfile(p):
                try:
                    return ImageFont.truetype(p, int(px))
                except OSError:
                    continue
        try:
            return ImageFont.load_default()
        except Exception:
            return None

    @staticmethod
    def _wrap_placeholder_lines(draw, msg: str, max_w: int, font) -> list[str]:
        """Word-wrap for PIL text with an optional TrueType font."""

        def line_w(s: str) -> int:
            bb = draw.textbbox((0, 0), s, font=font) if font else draw.textbbox((0, 0), s)
            return bb[2] - bb[0]

        if "\n" in msg:
            return [ln.strip() for ln in msg.splitlines() if ln.strip()]
        words = msg.split()
        if not words:
            return [msg]
        lines_out: list[str] = []
        cur: list[str] = []
        for word in words:
            trial = " ".join(cur + [word]) if cur else word
            if line_w(trial) <= max_w:
                cur.append(word)
            else:
                if cur:
                    lines_out.append(" ".join(cur))
                if line_w(word) > max_w:
                    chunk = ""
                    for ch in word:
                        t2 = chunk + ch
                        if line_w(t2) <= max_w:
                            chunk = t2
                        else:
                            if chunk:
                                lines_out.append(chunk)
                            chunk = ch
                    cur = [chunk] if chunk else []
                else:
                    cur = [word]
        if cur:
            lines_out.append(" ".join(cur))
        return lines_out or [msg]

    def _broken_video_placeholder_pil(self, text=None, size=None) -> Image.Image:
        """
        Shared black + red message bitmap for broken / unreadable videos.
        Used by grid thumbnails and the main video player fallback overlay.
        """
        if text is None:
            text = "This video seems to be broken"
        w, h = size if size is not None else tuple(self.thumbnail_size)
        w, h = max(32, int(w)), max(32, int(h))

        img = Image.new("RGB", (w, h), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        pad_x = max(10, w // 35)
        pad_y = max(10, h // 35)
        max_w = max(40, w - 2 * pad_x)
        max_h = max(40, h - 2 * pad_y)
        red = (230, 70, 70)

        def line_height(font) -> int:
            bb = draw.textbbox((0, 0), "Ay", font=font) if font else draw.textbbox((0, 0), "Ay")
            return max(14, bb[3] - bb[1] + 8)

        chosen_font = None
        chosen_lines: list[str] = []
        chosen_lh = 14

        start_px = min(54, max(22, min(w, h) // 4))
        for px in range(start_px, 11, -2):
            font = self._broken_placeholder_font(px)
            if font is None:
                continue
            lines = self._wrap_placeholder_lines(draw, text, max_w, font)
            lh = line_height(font)
            if lh * len(lines) <= max_h:
                chosen_font, chosen_lines, chosen_lh = font, lines, lh
                break

        if not chosen_lines:
            font = self._broken_placeholder_font(16) or self._broken_placeholder_font(12)
            chosen_font = font
            chosen_lines = self._wrap_placeholder_lines(draw, text, max_w, font)
            chosen_lh = line_height(font)

        total_h = chosen_lh * len(chosen_lines)
        y = max(pad_y, (h - total_h) // 2)
        for line in chosen_lines:
            bb = (
                draw.textbbox((0, 0), line, font=chosen_font)
                if chosen_font
                else draw.textbbox((0, 0), line)
            )
            txt_w = bb[2] - bb[0]
            x = max(pad_x, (w - txt_w) // 2)
            draw.text((x, y), line, fill=red, font=chosen_font)
            y += chosen_lh
            if y + chosen_lh > h - pad_y:
                break
        return img

    def _create_corrupted_thumbnail_image(self, text=None):
        """CTkImage wrapper for grid cells (same pixels as player overlay)."""
        pil = self._broken_video_placeholder_pil(text=text, size=tuple(self.thumbnail_size))
        return ctk.CTkImage(light_image=pil, dark_image=pil)

    def _get_video_health(self, video_path):
        """
        Classify video for playback policy:
        - 'empty'  : 0-byte or inaccessible file (always blocked)
        - 'broken' : metadata/duration check failed
        - 'ok'     : seems playable
        """
        norm = os.path.normcase(os.path.normpath(video_path))
        cache = getattr(self, "_video_health_cache", None)
        if cache is None:
            self._video_health_cache = {}
            cache = self._video_health_cache

        try:
            st = os.stat(video_path)
            size = int(st.st_size)
            mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
        except OSError:
            cache[norm] = {"k": None, "health": "empty"}
            return "empty"

        key = (size, mtime_ns)
        cached = cache.get(norm)
        if cached and cached.get("k") == key:
            return cached.get("health", "ok")

        if size <= 0:
            health = "empty"
        else:
            try:
                duration = float(get_video_duration_mediainfo(video_path))
                health = "ok" if duration > 0 else "broken"
            except Exception:
                health = "broken"

        cache[norm] = {"k": key, "health": health}
        return health

    def _can_attempt_video_playback(self, video_path, for_preview=False):
        """Central playback policy used by preview and main player open."""
        if bool(getattr(self, "play_broken_videos", True)):
            try:
                if os.path.getsize(video_path) <= 0:
                    return False, "Cannot play empty video file (0 B)."
            except OSError:
                return False, "Cannot play inaccessible video file."
            return True, ""

        health = self._get_video_health(video_path)
        if health == "empty":
            return False, "Cannot play empty video file (0 B)."
        if health == "broken" and not bool(getattr(self, "play_broken_videos", True)):
            target = "preview" if for_preview else "playback"
            return False, (
                f"Blocked {target}: video appears corrupted. "
                "Enable 'Play broken videos' in Preferences to override."
            )
        return True, ""




    def ensure_basic_thumbnails(self, folder_path, thumbnail_size, count=4):
        from pathlib import Path

        if not folder_path or not os.path.isdir(folder_path):
            return
        if preview_skip_subdir(os.path.basename(os.path.normpath(folder_path))):
            return

        media_extensions = set(VIDEO_FORMATS + IMAGE_FORMATS)
        try:
            entries = [f for f in Path(folder_path).iterdir() if f.suffix.lower() in media_extensions]
        except (OSError, PermissionError) as e:
            logging.debug("ensure_basic_thumbnails: cannot list %s: %s", folder_path, e)
            return

        for idx, path in enumerate(entries[:count]):
            try:
                if path.suffix.lower() in VIDEO_FORMATS:
                    create_video_thumbnail(
                        str(path), thumbnail_size, self.thumbnail_format,
                        self.capture_method_var.get(), thumbnail_time=self.thumbnail_time,
                        cache_enabled=self.cache_enabled, overwrite=False,
                        cache_dir=self.thumbnail_cache_path
                    )
                else:
                    create_image_thumbnail(
                        str(path), thumbnail_size,
                        cache_enabled=self.cache_enabled,
                        database=self.database,
                        cache_dir=self.thumbnail_cache_path
                    )
            except Exception as e:
                logging.debug("ensure_basic_thumbnails: skip %s: %s", path.name, e)


    def create_folder_thumb(self, file_path, file_name, row, col, index, target_frame, render_id=None):
        """
        Instantly adds a basic folder icon to the grid on the main thread,
        then spawns a background task to generate and apply the 2x2 preview grid.
        Uses a separate thread-safe Queue for GUI updates.
        """
        # Separate queue from thumb_queue for folder composite GUI updates
        if not hasattr(self, 'gui_update_queue'):
            import queue
            self.gui_update_queue = queue.Queue()
            
            def _process_gui_queue():
                try:
                    # Process all pending UI updates safely on the main thread
                    while True:
                        gui_task = self.gui_update_queue.get_nowait()
                        gui_task()
                except queue.Empty:
                    pass
                except Exception as e:
                    # "invalid command name" = widget already destroyed, safe to ignore
                    err = str(e)
                    if "invalid command name" not in err and "application has been destroyed" not in err:
                        logging.error("GUI queue update failed: %s", e)
                # Check the queue again in 50ms
                self.after(50, _process_gui_queue)
                
            _process_gui_queue() # Start the infinite checker loop
        # -----------------------------------------------------

        try:
            if render_id is not None and render_id != self._render_id:
                return
            logging.debug("[MAIN] Async folder thumb: %s (%s)", file_name, file_path)

            # 1. Fast, synchronous generation of an empty folder icon.
            basic_thumbnail = self.file_ops.create_folder_thumbnail(
                thumbnail_size=self.thumbnail_size,
                folder_path=None,
                cache_enabled=self.cache_enabled,
                cache_dir=self.thumbnail_cache_path,
                database=self.database,
                is_cached=False
            )

            # 2. Add the basic folder to the UI immediately.
            widget_reference = self.add_thumbnail_to_grid(
                basic_thumbnail, file_path, file_name, row, col,
                is_folder=True, index=index, target_frame=target_frame
            )
            logging.debug("[MAIN] Empty folder UI for %s, widget=%s", file_name, bool(widget_reference))

            def worker():
                """
                Background worker that performs heavy disk I/O and CPU tasks
                to generate the composite 2x2 folder preview grid.
                """
                try:
                    if render_id is not None and render_id != self._render_id:
                        return
                    logging.debug("[WORKER] Folder thread: %s", file_name)
                    is_cached = self.database.is_folder_cached(file_path)

                    # Heavy disk I/O: Ensure the first 4 thumbnails exist INSIDE the folder
                    logging.debug("[WORKER] Scanning '%s' for up to 4 media files", file_path)
                    try:
                        self.ensure_basic_thumbnails(file_path, self.thumbnail_size, count=4)
                    except (OSError, PermissionError) as e:
                        logging.debug(
                            "Folder thumb: ensure_basic_thumbnails skipped (access): %s — %s",
                            file_path,
                            e,
                        )

                    # Heavy CPU: Generate the final folder icon with the 2x2 grid
                    logging.debug("[WORKER] Generating 2x2 composite for '%s'", file_name)
                    composite_thumbnail = self.file_ops.create_folder_thumbnail(
                        thumbnail_size=self.thumbnail_size,
                        folder_path=file_path,
                        cache_enabled=self.cache_enabled,
                        cache_dir=self.thumbnail_cache_path,
                        database=self.database,
                        is_cached=is_cached,
                    )

                    if not composite_thumbnail:
                        logging.warning("[WORKER] Composite thumbnail failed for '%s' (empty folder?)", file_name)

                    def update_gui():
                        """
                        Updates the tk.Canvas with the newly generated composite image
                        on the main GUI thread.
                        """
                        if render_id is not None and render_id != self._render_id:
                            return
                        logging.debug("[GUI] Final thumbnail update for: %s", file_name)
                        if widget_reference:
                            canvas = widget_reference[0]

                            # In Wide-folder mode the widget is a Frame, not a Canvas —
                            # the image is already handled by _bg_generate inside
                            # run_thumbnail_to_grid_wide, so nothing to do here.
                            if not isinstance(canvas, tk.Canvas):
                                logging.debug("[GUI] Wide folder — image handled by async loader: %s", file_name)
                                return

                            # Canvas may be gone after folder change
                            try:
                                if not canvas.winfo_exists():
                                    logging.debug("[GUI] Canvas gone for %s, skip update", file_name)
                                    return
                            except Exception:
                                return

                            from PIL import ImageOps
                            from PIL import ImageTk
                            resized_img = ImageOps.contain(composite_thumbnail._light_image, self.thumbnail_size)
                            new_photo_image = ImageTk.PhotoImage(resized_img)

                            canvas.itemconfig("thumbnail", image=new_photo_image)
                            canvas.image = new_photo_image
                            self.image_references.append(new_photo_image)
                            logging.debug("[GUI] 2x2 grid applied for: %s", file_name)
                        else:
                            logging.error("[GUI] Lost widget ref for %s — add_thumbnail_to_grid returned no canvas?", file_name)

                        self.finalize_thread()
                        self.processed_files_count += 1

                        if self.total_files_to_process > 0:
                            progress = (self.processed_files_count / self.total_files_to_process) * 100
                            self.status_bar.update_progress(progress)

                    logging.debug("[WORKER] Queue GUI update for '%s'", file_name)
                    self.gui_update_queue.put(update_gui)

                except PermissionError as e:
                    logging.debug(
                        "Folder thumb worker: access denied for %r (%s): %s",
                        file_path,
                        file_name,
                        e,
                    )
                except OSError as e:
                    if getattr(e, "winerror", None) == 5:
                        logging.debug(
                            "Folder thumb worker: access denied for %r (%s): %s",
                            file_path,
                            file_name,
                            e,
                        )
                    else:
                        logging.error(
                            "[WORKER] Error processing folder '%s': %s",
                            file_name,
                            e,
                            exc_info=True,
                        )
                except Exception as e:
                    logging.error("[WORKER] Error processing folder '%s': %s", file_name, e, exc_info=True)

            self.executor.submit(worker)

        except Exception as e:
            logging.error("[MAIN] Error initializing folder thumb: %s", e)
    
   


    #move selection with support of BLOCK selection
    def move_selection(self, direction, shift=False, ctrl=False):
        cols = self.columns
        total = len(self.video_files)

        # Shift: extend selection across several thumbs
        if shift and len(self.selected_thumbnails) > 1:
            new_indices = set()
            for _, _, idx in self.selected_thumbnails:
                if direction == "up":
                    ni = idx - cols
                    if ni >= 0:
                        new_indices.add(ni)
                elif direction == "down":
                    ni = idx + cols
                    if ni < total:
                        new_indices.add(ni)
                elif direction == "left":
                    # Wrap to previous row's last item at left edge
                    if idx > 0:
                        new_indices.add(idx - 1)
                elif direction == "right":
                    # Wrap to next row's first item at right edge
                    if idx < total - 1:
                        new_indices.add(idx + 1)
            for ni in new_indices:
                file_data = self.video_files[ni]
                file_path = file_data['path'] if isinstance(file_data, dict) else file_data[0]
                thumb_info = self.thumbnail_labels[file_path]
                label = thumb_info.get("canvas")
                if not any(i == ni for _, _, i in self.selected_thumbnails):
                    self.selected_thumbnails.append((file_path, label, ni))
                    border_items = label.find_withtag("border")
                    if border_items:
                        label.itemconfig(border_items[0], outline=self.thumbSelColor, width=self.Select_outlinewidth)
            return

        # Single-select or start of multi-select
        idx = self.selected_thumbnail_index or 0
        if direction == "up":
            new_idx = idx - cols
            if new_idx < 0:
                return
        elif direction == "down":
            new_idx = idx + cols
            if new_idx >= total:
                return
        elif direction == "left":
            # Wrap to previous row's last item at left edge
            if idx <= 0:
                return
            new_idx = idx - 1
        elif direction == "right":
            # Wrap to next row's first item at right edge
            if idx >= total - 1:
                return
            new_idx = idx + 1
        else:
            return

        self.select_thumbnail(new_idx, shift=shift, ctrl=ctrl)

    
    # move selection without Block selection.. Still usable..  Curently not in use!!!
    def move_selectionSimple(self, direction, shift=False, ctrl=False):
        idx = self.selected_thumbnail_index or 0
        cols = self.columns
        total = len(self.video_files)

        if direction == "up":
            new_idx = idx - cols
            if new_idx < 0:
                return
        elif direction == "down":
            new_idx = idx + cols
            if new_idx >= total:
                return
        elif direction == "left":
            # Wrap to previous row's last item at left edge
            if idx <= 0:
                return
            new_idx = idx - 1
        elif direction == "right":
            # Wrap to next row's first item at right edge
            if idx >= total - 1:
                return
            new_idx = idx + 1
        else:
            return

        self.select_thumbnail(new_idx, shift=shift, ctrl=ctrl)







    def open_image_viewer(self, image_path, image_name):
        if not image_file_exists(image_path):
            notify_missing_image(self, image_path)
            return

        if hasattr(self, 'current_image_window') and self.current_image_window:
            self.current_image_window.image_window.destroy()

        use_pyglet = getattr(self, "image_viewer_use_pyglet", False)
        viewer = create_image_viewer(self, image_path, image_name, use_pyglet)
        if viewer is None:
            self.current_image_window = None
            return
        self.current_image_window = viewer

    def open_image_viewer_edit(self, image_path, action: str):
        """
        Open the image viewer and start an edit action.

        ``action``: ``"crop"`` | ``"resize"``.
        Crop uses the Canvas viewer (inline HUD is Legacy-only).
        """
        if not image_file_exists(image_path):
            notify_missing_image(self, image_path)
            return

        image_name = os.path.basename(image_path)
        if hasattr(self, "current_image_window") and self.current_image_window:
            try:
                self.current_image_window.image_window.destroy()
            except Exception:
                pass

        # Crop overlay exists only on ImageViewerLegacy.
        use_gpu = bool(getattr(self, "image_viewer_use_pyglet", False)) and action != "crop"
        viewer = create_image_viewer(self, image_path, image_name, use_gpu)
        if viewer is None:
            self.current_image_window = None
            return
        self.current_image_window = viewer

        delay_ms = 150 if getattr(self, "image_viewer_open_fullscreen", True) else 50

        def _start_edit():
            viewer = getattr(self, "current_image_window", None)
            if viewer is None:
                return
            if action == "crop":
                enter = getattr(viewer, "enter_crop_mode", None)
                if callable(enter):
                    enter()
                return
            if action == "resize":
                open_dlg = getattr(viewer, "open_resize_dialog", None)
                if callable(open_dlg):
                    open_dlg()

        self.after(delay_ms, _start_edit)

    def selected_image_paths_for_edit(self, primary_path):
        """Image paths for crop/resize from multi-select (selection order).

        When the RMB target is part of a multi-image selection, return all
        selected images; otherwise return only ``primary_path``.
        """
        primary = os.path.normpath(primary_path) if primary_path else None
        raw = list(getattr(self, "selected_thumbnails", []) or [])
        paths = []
        seen = set()
        for item in raw:
            p = item[0] if isinstance(item, tuple) and item else item
            if not p:
                continue
            p = os.path.normpath(str(p))
            key = os.path.normcase(p)
            if key in seen:
                continue
            if not os.path.isfile(p) or not p.lower().endswith(IMAGE_FORMATS):
                continue
            seen.add(key)
            paths.append(p)

        if (
            primary
            and len(paths) > 1
            and os.path.normcase(primary) in {os.path.normcase(p) for p in paths}
        ):
            return paths
        if primary and os.path.isfile(primary) and primary.lower().endswith(IMAGE_FORMATS):
            return [primary]
        return paths[:1] if paths else []

    def start_image_crop_from_grid(self, primary_path: str):
        """Crop from thumbnail RMB — multi-select is not supported yet."""
        paths = self.selected_image_paths_for_edit(primary_path)
        if len(paths) <= 1:
            target = paths[0] if paths else primary_path
            self.open_image_viewer_edit(target, "crop")
            return

        def _crop_this():
            self.open_image_viewer_edit(primary_path, "crop")
            return True

        self.universal_dialog(
            title="Crop is single-image only",
            message=(
                f"{len(paths)} images are selected.\n\n"
                "Batch crop is not available yet.\n"
                "Crop only the clicked file?"
            ),
            confirm_callback=_crop_this,
            confirm_text="Crop this file",
            cancel_text="Cancel",
            show_cancel=True,
        )

    def start_image_resize_from_grid(self, primary_path: str):
        """Resize from thumbnail RMB — batch when multiple images are selected."""
        paths = self.selected_image_paths_for_edit(primary_path)
        if len(paths) <= 1:
            target = paths[0] if paths else primary_path
            self.open_image_viewer_edit(target, "resize")
            return
        self.open_batch_image_resize(paths)

    def start_image_transform_from_grid(self, primary_path: str, op: str):
        """
        Rotate / flip from thumbnail RMB.

        Works with multi-select: same transform applied to every selected image
        (overwrite on disk). Confirms when multiple files are selected, or when
        any target would be re-encoded lossily (JPEG / lossy WebP).
        """
        if op not in IMAGE_TRANSFORM_LABELS:
            return
        paths = self.selected_image_paths_for_edit(primary_path)
        if not paths:
            if primary_path and os.path.isfile(primary_path):
                paths = [primary_path]
            else:
                return

        label = IMAGE_TRANSFORM_LABELS[op]
        lossy_paths = [p for p in paths if image_reencode_is_lossy(p)]
        needs_confirm = len(paths) > 1 or bool(lossy_paths)

        def _go():
            self._run_batch_image_transform(paths, op=op, action_label=label)
            return True

        if not needs_confirm:
            _go()
            return

        if len(paths) == 1:
            message = (
                f"Apply “{label}” and overwrite this file?\n\n"
                f"{os.path.basename(paths[0])}\n\n"
                "This format is re-encoded (not bit-exact lossless)."
            )
        elif lossy_paths and len(lossy_paths) == len(paths):
            message = (
                f"Apply “{label}” and overwrite {len(paths)} image files?\n"
                "JPEG / lossy WebP will be re-encoded (quality may change).\n"
                "This cannot be undone."
            )
        elif lossy_paths:
            message = (
                f"Apply “{label}” and overwrite {len(paths)} image files?\n"
                f"{len(lossy_paths)} are JPEG/lossy WebP and will be re-encoded.\n"
                "This cannot be undone."
            )
        else:
            message = (
                f"Apply “{label}” and overwrite {len(paths)} image files on disk?\n"
                "This cannot be undone."
            )

        self.universal_dialog(
            title=f"{label}?",
            message=message,
            confirm_callback=_go,
            confirm_text="Overwrite",
            cancel_text="Cancel",
            show_cancel=True,
        )

    def _run_batch_image_transform(self, paths, *, op: str, action_label: str):
        """Worker + progress UI for rotate/flip overwrite."""
        progress = open_file_op_progress_dialog(
            self, action_label, len(paths), action_label=action_label
        )
        errors = []

        def _worker():
            ok = 0
            for i, path in enumerate(paths, start=1):
                if progress.cancelled:
                    break
                name = os.path.basename(path)
                self.after(
                    0,
                    lambda i=i, name=name: progress.set_progress(i - 1, detail=name),
                )
                try:
                    transform_image_file(path, op)
                    ok += 1
                    self.after(
                        0,
                        lambda p=path: self.refresh_single_thumbnail(p, overwrite=True),
                    )
                except Exception as e:
                    logging.info("%s failed for %s: %s", action_label, path, e)
                    errors.append(f"{name}: {e}")
                self.after(
                    0,
                    lambda i=i, name=name: progress.set_progress(i, detail=name),
                )

            def _done():
                progress.close()
                if errors:
                    shown = "\n".join(errors[:8])
                    more = f"\n…and {len(errors) - 8} more" if len(errors) > 8 else ""
                    self.universal_dialog(
                        title=f"{action_label} finished with errors",
                        message=f"Updated {ok} / {len(paths)} files.\n\n{shown}{more}",
                        confirm_callback=lambda: True,
                        confirm_text="OK",
                        show_cancel=False,
                    )
                elif ok:
                    logging.info("%s: %s files OK", action_label, ok)

            self.after(0, _done)

        threading.Thread(
            target=_worker, daemon=True, name=f"batch-{op}"
        ).start()

    def open_batch_image_resize(self, paths: list):
        """Show batch resize dialog and overwrite selected image files."""
        paths = [p for p in (paths or []) if p and os.path.isfile(p)]
        if not paths:
            return
        if len(paths) == 1:
            self.open_image_viewer_edit(paths[0], "resize")
            return

        def _on_apply(unit, width_val, height_val, lock_aspect, resample_filter):
            def _confirmed():
                self._run_batch_image_resize(
                    paths,
                    unit=unit,
                    width_val=width_val,
                    height_val=height_val,
                    lock_aspect=lock_aspect,
                    resample_filter=resample_filter,
                )
                return True

            self.universal_dialog(
                title="Overwrite files?",
                message=(
                    f"Resize and overwrite {len(paths)} image files on disk?\n"
                    "This cannot be undone."
                ),
                confirm_callback=_confirmed,
                confirm_text="Overwrite",
                cancel_text="Cancel",
                show_cancel=True,
            )

        open_batch_resize_dialog(self, paths, on_apply=_on_apply)

    def _run_batch_image_resize(
        self,
        paths,
        *,
        unit,
        width_val,
        height_val,
        lock_aspect,
        resample_filter,
    ):
        """Worker + progress UI for batch resize overwrite."""
        progress = open_file_op_progress_dialog(
            self, "Resize Images", len(paths), action_label="Resizing"
        )
        errors = []

        def _worker():
            ok = 0
            for i, path in enumerate(paths, start=1):
                if progress.cancelled:
                    break
                name = os.path.basename(path)
                self.after(
                    0,
                    lambda i=i, name=name: progress.set_progress(i - 1, detail=name),
                )
                try:
                    resize_image_file(
                        path,
                        unit=unit,
                        width_val=width_val,
                        height_val=height_val,
                        lock_aspect=lock_aspect,
                        resample_filter=resample_filter,
                    )
                    ok += 1
                    self.after(
                        0,
                        lambda p=path: self.refresh_single_thumbnail(p, overwrite=True),
                    )
                except Exception as e:
                    logging.info("Batch resize failed for %s: %s", path, e)
                    errors.append(f"{name}: {e}")
                self.after(
                    0,
                    lambda i=i, name=name: progress.set_progress(i, detail=name),
                )

            def _done():
                progress.close()
                if errors:
                    shown = "\n".join(errors[:8])
                    more = f"\n…and {len(errors) - 8} more" if len(errors) > 8 else ""
                    self.universal_dialog(
                        title="Resize finished with errors",
                        message=f"Resized {ok} / {len(paths)} files.\n\n{shown}{more}",
                        confirm_callback=lambda: True,
                        confirm_text="OK",
                        show_cancel=False,
                    )
                elif ok:
                    logging.info("Batch resize: %s files OK", ok)

            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True, name="batch-resize").start()

    def open_batch_convert_dialog(self, primary_path: str | None = None):
        """Open Batch Convert / Rename for selected images (File menu or RMB)."""
        if primary_path:
            paths = self.selected_image_paths_for_edit(primary_path)
        else:
            paths = self.selected_image_paths_for_edit(
                getattr(self, "selected_file_path", None)
                or (self.selected_thumbnails[0][0] if self.selected_thumbnails else None)
            )
            if not paths:
                # Fall back: collect all selected images even if primary is missing.
                raw = list(getattr(self, "selected_thumbnails", []) or [])
                seen = set()
                paths = []
                for item in raw:
                    p = item[0] if isinstance(item, tuple) and item else item
                    if not p:
                        continue
                    p = os.path.normpath(str(p))
                    key = os.path.normcase(p)
                    if key in seen:
                        continue
                    if os.path.isfile(p) and p.lower().endswith(IMAGE_FORMATS):
                        seen.add(key)
                        paths.append(p)

        if not paths:
            messagebox.showinfo(
                "Batch Convert",
                "No images selected.\n\nSelect one or more image thumbnails first.",
            )
            return

        open_batch_process_dialog(self, paths, on_start=self.start_batch_image_process)

    def _resolve_batch_convert_conflict(
        self,
        output_path: str,
        src_path: str,
        progress_dialog,
        conflict_policy: dict,
        *,
        ask_before_overwrite: bool = True,
    ) -> str | None:
        """
        Resolve destination conflicts (Replace / Rename / Skip / Cancel).

        Same-path in-place overwrite is allowed without prompting.
        When ``ask_before_overwrite`` is False, existing targets are replaced.
        Returns path to write, None to skip, or raises InterruptedError on cancel.
        """
        if not output_path:
            return None
        try:
            same = os.path.normcase(os.path.abspath(output_path)) == os.path.normcase(
                os.path.abspath(src_path)
            )
        except Exception:
            same = False
        if same:
            return output_path
        if not os.path.exists(output_path):
            return output_path

        # FastStone-style: unchecked "Ask before overwrite" → silent replace.
        if not ask_before_overwrite:
            return output_path

        action = conflict_policy.get("action")
        if action in ("replace", "rename", "skip") and conflict_policy.get("apply_all"):
            if action == "replace":
                return output_path
            if action == "rename":
                return get_conflict_rename_path(output_path)
            return None

        holder: dict = {}
        done = threading.Event()

        def _ask():
            try:
                try:
                    if progress_dialog is not None:
                        progress_dialog.grab_release()
                except Exception:
                    pass
                act, apply_all = open_conflict_dialog(
                    self, os.path.basename(output_path)
                )
                holder["action"] = act
                holder["apply_all"] = apply_all
            finally:
                try:
                    if progress_dialog is not None and progress_dialog.winfo_exists():
                        progress_dialog.grab_set()
                        progress_dialog.lift()
                except Exception:
                    pass
                done.set()

        self.after(0, _ask)
        done.wait()
        action = holder.get("action") or "cancel"
        apply_all = bool(holder.get("apply_all"))
        if apply_all and action in ("replace", "rename", "skip"):
            conflict_policy["action"] = action
            conflict_policy["apply_all"] = True

        if action == "replace":
            return output_path
        if action == "rename":
            return get_conflict_rename_path(output_path)
        if action == "skip":
            return None
        raise InterruptedError("Batch convert canceled at conflict dialog")

    def start_batch_image_process(self, job: dict):
        """Run batch convert / rename on a background thread with progress UI."""
        if not job:
            return
        if getattr(self, "_batch_convert_running", False):
            messagebox.showinfo("Batch Convert", "A batch conversion is already running.")
            return

        paths = [p for p in (job.get("paths") or []) if p and os.path.isfile(p)]
        if not paths:
            messagebox.showinfo("Batch Convert", "No valid image files to process.")
            return

        out_ext = job.get("out_ext") or ".jpg"
        output_dir = job.get("output_dir")
        rename_enabled = bool(job.get("rename_enabled"))
        rename_pattern = job.get("rename_pattern") or "image_###"
        rotate_op = job.get("rotate_op")
        flip_h = bool(job.get("flip_h"))
        flip_v = bool(job.get("flip_v"))
        crop_settings = job.get("crop_settings")
        resize_settings = job.get("resize_settings")
        canvas_settings = job.get("canvas_settings")
        quality = int(job.get("quality") or 90)
        png_compress = int(job.get("png_compress") if job.get("png_compress") is not None else 6)
        ask_before_overwrite = bool(job.get("ask_before_overwrite", True))

        self._batch_convert_running = True
        progress = open_file_op_progress_dialog(
            self, "Batch Convert", len(paths), action_label="Converting"
        )
        conflict_policy: dict = {"action": None, "apply_all": False}
        errors: list[str] = []
        written: list[str] = []

        def _worker():
            ok = 0
            skipped = 0
            aborted = False
            try:
                for i, src in enumerate(paths, start=1):
                    if progress.cancelled:
                        aborted = True
                        break
                    name = os.path.basename(src)
                    self.after(
                        0,
                        lambda i=i, name=name: progress.set_progress(
                            i - 1, detail=name
                        ),
                    )
                    try:
                        suggested = build_output_path(
                            src,
                            index=i,
                            out_ext=out_ext,
                            output_dir=output_dir,
                            rename_enabled=rename_enabled,
                            rename_pattern=rename_pattern,
                        )
                        dest = self._resolve_batch_convert_conflict(
                            suggested,
                            src,
                            progress,
                            conflict_policy,
                            ask_before_overwrite=ask_before_overwrite,
                        )
                    except InterruptedError:
                        aborted = True
                        break
                    except Exception as e:
                        logging.info("Batch convert conflict failed for %s: %s", src, e)
                        errors.append(f"{name}: {e}")
                        self.after(
                            0,
                            lambda i=i, name=name: progress.set_progress(
                                i, detail=name
                            ),
                        )
                        continue

                    if dest is None:
                        skipped += 1
                        self.after(
                            0,
                            lambda i=i, name=name: progress.set_progress(
                                i, detail=f"{name} (skipped)"
                            ),
                        )
                        continue

                    try:
                        process_one_image(
                            src,
                            dest,
                            rotate_op=rotate_op,
                            flip_h=flip_h,
                            flip_v=flip_v,
                            crop_settings=crop_settings,
                            resize_settings=resize_settings,
                            canvas_settings=canvas_settings,
                            quality=quality,
                            png_compress=png_compress,
                        )
                        ok += 1
                        written.append(dest)
                    except Exception as e:
                        logging.info("Batch convert failed for %s: %s", src, e)
                        errors.append(f"{name}: {e}")

                    self.after(
                        0,
                        lambda i=i, name=name: progress.set_progress(i, detail=name),
                    )
            finally:
                self.after(
                    0,
                    lambda: self._batch_convert_done(
                        progress,
                        ok=ok,
                        skipped=skipped,
                        total=len(paths),
                        errors=errors,
                        written=written,
                        aborted=aborted,
                        output_dir=output_dir,
                    ),
                )

        threading.Thread(target=_worker, daemon=True, name="batch-convert").start()

    def _batch_convert_done(
        self,
        progress,
        *,
        ok: int,
        skipped: int,
        total: int,
        errors: list,
        written: list,
        aborted: bool,
        output_dir: str | None,
    ):
        self._batch_convert_running = False
        try:
            progress.close()
        except Exception:
            pass

        # Reload grid so new/converted files appear (same folder as current view).
        if ok and written:
            try:
                cur = getattr(self, "current_directory", None)
                if cur:
                    cur_key = os.path.normcase(os.path.normpath(cur))
                    wrote_here = any(
                        os.path.normcase(os.path.normpath(os.path.dirname(p))) == cur_key
                        for p in written
                    )
                    if wrote_here:
                        self.display_thumbnails(
                            cur, force_refresh=True, preserve_scroll=True
                        )
            except Exception as e:
                logging.info("Batch convert folder refresh failed: %s", e)

        parts = [f"Batch Convert: {ok}/{total}"]
        if skipped:
            parts.append(f"skipped {skipped}")
        if aborted:
            parts.append("aborted")
        if errors:
            parts.append(f"{len(errors)} error(s)")
            for err in errors[:8]:
                logging.info("Batch convert error: %s", err)
        summary = ", ".join(parts)

        try:
            if hasattr(self, "status_bar") and self.status_bar is not None:
                color = "#ff6b6b" if errors or aborted else None
                self.status_bar.set_action_message(summary, color=color)
        except Exception as e:
            logging.info("Batch convert status message failed: %s", e)

    def setup_icons(self):
        """
        Load and scale tree/grid icons from the /icons subdirectory and prepare PhotoImage/CTkImage versions.
        """
        try:
            logging.debug("Loading and scaling tree icons from app/icons")
            
            P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
            
            dpi_s = float(getattr(self, "current_dpi_scale", 1.0) or 1.0)
            # Match perceived size on HiDPI (was 48/32/24 via tk scaling) while staying inside the row.
            if dpi_s >= 2.25:
                icon_size = 44
            elif dpi_s >= 2.0:
                icon_size = 40
            elif dpi_s >= 1.75:
                icon_size = 34
            elif dpi_s >= 1.5:
                icon_size = 30
            elif dpi_s >= 1.25:
                icon_size = 26
            else:
                icon_size = 24
            # ~30% larger folder glyphs on 4K-class DPI (same profile threshold as hi-dpi UI).
            if dpi_s >= 1.5:
                icon_size = int(round(icon_size * 1.3))
            try:
                rh = int(getattr(self, "row_height", 0) or 0)
                if rh > 0:
                    # Leave a couple px margin; floor soft enough for compact tree rows.
                    max_by_row = max(14, min(int(rh * 0.90), rh - 2))
                    icon_size = min(icon_size, max_by_row)
            except (TypeError, ValueError):
                pass

            logging.info(
                "[DEBUG] Tree icons: current_dpi_scale=%.3f row_height=%s -> icon_size=%d px",
                dpi_s,
                getattr(self, "row_height", "?"),
                icon_size,
            )

            # 1) Load PIL images from disk
            folder_tree_pil = Image.open(os.path.join(P, "tree_folder.PNG")).resize((icon_size, icon_size), Image.LANCZOS)
            folder_tree_green_pil = Image.open(os.path.join(P, "tree_folder_green.png")).resize((icon_size, icon_size), Image.LANCZOS)
            folder_virtual_pil = Image.open(os.path.join(P, "tree_folder_virtual.png")).resize((icon_size, icon_size), Image.LANCZOS)
            hdd_pil = Image.open(os.path.join(P, "tree_hdd.PNG")).resize((icon_size, icon_size), Image.LANCZOS)
            google_pil = Image.open(os.path.join(P, "tree_google.PNG")).resize((icon_size, icon_size), Image.LANCZOS)
            desktop_pil = Image.open(os.path.join(P, "tree_desktop.png")).resize((icon_size, icon_size), Image.LANCZOS)
            downloads_pil = Image.open(os.path.join(P, "tree_downloads.png")).resize((icon_size, icon_size), Image.LANCZOS)
            documents_pil = Image.open(os.path.join(P, "tree_documents.png")).resize((icon_size, icon_size), Image.LANCZOS)
            pictures_pil = Image.open(os.path.join(P, "tree_pictures.png")).resize((icon_size, icon_size), Image.LANCZOS)
            videos_pil = Image.open(os.path.join(P, "tree_videos.png")).resize((icon_size, icon_size), Image.LANCZOS)
            
            folder_grid_pil = Image.open(os.path.join(P, "folder.png")).resize((96, 96), Image.LANCZOS)
            folder_grid_green_pil = Image.open(os.path.join(P, "folder_g.png")).resize((96, 96), Image.LANCZOS)

            # 2) ttk.Treeview PhotoImage versions
            self.folder_treeicon = ImageTk.PhotoImage(folder_tree_pil)
            self.folder_treeicon_green = ImageTk.PhotoImage(folder_tree_green_pil)
            self.folder_virtual_icon = ImageTk.PhotoImage(folder_virtual_pil)
            self.hdd_icon = ImageTk.PhotoImage(hdd_pil)
            self.google_icon = ImageTk.PhotoImage(google_pil)
            self.desktop_icon = ImageTk.PhotoImage(desktop_pil)
            self.downloads_icon = ImageTk.PhotoImage(downloads_pil)
            self.documents_icon = ImageTk.PhotoImage(documents_pil)
            self.pictures_icon = ImageTk.PhotoImage(pictures_pil)
            self.videos_icon = ImageTk.PhotoImage(videos_pil)

            # 3) CustomTkinter CTkImage versions
            self.folder_icon_ctk = ctk.CTkImage(light_image=folder_grid_pil, size=(96, 96))
            self.folder_icon_green_ctk = ctk.CTkImage(light_image=folder_grid_green_pil, size=(96, 96))
            
            logging.debug("All icon variants created from app/icons")

        except FileNotFoundError as e:
            logging.error("Icon file missing in app/icons: %s", e)
            messagebox.showerror("Icons", f"Icon file not found:\n{e}")
            self.quit()
        except Exception as e:
            logging.error("Unexpected error loading icons: %s", e)
            messagebox.showerror("Icons", f"Failed to load icons:\n{e}")
            self.quit()

    def _refresh_all_tree_icons_after_icon_reload(self) -> None:
        """Re-apply tree PhotoImages after setup_icons() replaced them (DPI / monitor change)."""
        if not hasattr(self, "tree") or not hasattr(self, "database"):
            return
        try:
            if not self.tree.winfo_exists():
                return
        except tk.TclError:
            return

        def _root_icon(path_val: str):
            p = os.path.normcase(os.path.normpath(str(path_val)))
            up = os.path.expanduser("~")
            specials = (
                (os.path.normcase(os.path.join(up, "Desktop")), self.desktop_icon),
                (os.path.normcase(os.path.join(up, "Downloads")), self.downloads_icon),
                (os.path.normcase(os.path.join(up, "Documents")), self.documents_icon),
                (os.path.normcase(os.path.join(up, "Pictures")), self.pictures_icon),
                (os.path.normcase(os.path.join(up, "Videos")), self.videos_icon),
            )
            for mp, ic in specials:
                if p == mp and os.path.exists(path_val):
                    return ic
            try:
                g = self.find_google_drive_path()
            except Exception:
                g = None
            if g and p == os.path.normcase(os.path.normpath(g)):
                return self.google_icon
            try:
                drives = self.get_available_drives()
            except Exception:
                drives = []
            for d in drives:
                dn = os.path.normcase(os.path.normpath(d))
                pv = p.rstrip(os.sep).upper()
                dv = dn.rstrip(os.sep).upper()
                if pv == dv:
                    return self.hdd_icon
            return self.folder_treeicon

        def _walk(parent: str) -> None:
            for item in self.tree.get_children(parent):
                vals = self.tree.item(item, "values")
                if vals and vals[0] and vals[0] != "dummy":
                    path_val = vals[0]
                    par = self.tree.parent(item)
                    try:
                        if par == "":
                            img = _root_icon(path_val)
                        else:
                            cached = self.database.is_folder_cached(path_val)
                            img = (
                                self.folder_treeicon_green
                                if cached
                                else self.folder_treeicon
                            )
                        self.tree.item(item, image=img)
                    except tk.TclError:
                        pass
                _walk(item)

        try:
            _walk("")
        except Exception as exc:
            logging.warning("_refresh_all_tree_icons_after_icon_reload: %s", exc)

    def is_cache_empty(self, file_path):
        """Check if the cache folder is empty or if the thumbnail for a specific file doesn't exist"""
        cache_thumbnail_path = os.path.join(self.thumbnail_cache_path, f"{os.path.basename(file_path)}.jpg")  # Example for JPG cache
        return not os.path.exists(cache_thumbnail_path)  # Returns True if the thumbnail doesn't exist

    def finalize_debug_overlay(self):
        load_time = time.time() - self.load_start_time
        load_source = "Cache" if self.debug_overlay.cache_hits > 0 else "Disk"
        self.debug_overlay.add_load_time(load_time, load_source)
        self.debug_overlay.update_text()  # Update with final load time
        
        

    def finalize_thread(self):
        """Finalizes the thumbnail creation process by updating debug stats."""
        self.debug_overlay.increment_thread_count()
        
        self.debug_overlay.add_load_time(time.time() - self.load_start_time, load_source="disk")
        
      
      
  
    def _load_remaining_v2(self, items_to_load, start_index, force_refresh, thumbnail_time, wide_mode_active, render_id=None):
        import time, queue, os

        # Abort immediately if preempted by a newer folder selection
        if render_id is not None and self._render_id != render_id:
            return

        # Disconnect scrollbar during chunk rendering to avoid expensive reflows
        self.canvas.configure(yscrollcommand="")

        path_map = getattr(self, 'current_path_map', {})

        total_items = len(items_to_load)
        chunk_start_time = time.perf_counter()
        items_processed_in_chunk = 0
        idx = start_index

        while idx < total_items:
            # Check cancellation at the start of every item
            if render_id is not None and self._render_id != render_id:
                return

            item_info = items_to_load[idx]

            global_idx = path_map.get(item_info['path'])
            if global_idx is None:
                idx += 1
                continue

            is_folder = item_info.get('is_folder', False)
            row, col = self.get_grid_position(global_idx, is_folder)
            target_frame = self.wide_folders_frame if (wide_mode_active and is_folder) else self.regular_thumbnails_frame

            actual_time_for_video = None
            if thumbnail_time is not None and not is_folder and item_info['path'].lower().endswith(VIDEO_FORMATS):
                if not force_refresh and self.database.get_cache_status(item_info['path']):
                    actual_time_for_video = 0
                else:
                    actual_time_for_video = self.calculate_thumbnail_time(item_info['path'])

            if target_frame and target_frame.winfo_exists():
                self.queue_thumbnail(
                    item_info['path'], item_info['name'], row, col, global_idx,
                    is_folder=is_folder, target_frame=target_frame,
                    force_refresh=force_refresh, thumbnail_time=actual_time_for_video,
                    render_id=render_id,
                )
                items_processed_in_chunk += 1
            idx += 1

            # Yield back to the main loop after each time slice
            if (time.perf_counter() - chunk_start_time) > self.chunk_time_limit and items_processed_in_chunk >= self.min_chunk_size:
                break

        if idx < total_items:
            # Schedule next chunk — yields control to the main loop between batches
            self.after(10, lambda nxt=idx: self._load_remaining_v2(
                items_to_load, nxt, force_refresh, thumbnail_time, wide_mode_active, render_id
            ))
        else:
            # All chunks done — reconnect scrollbar and finalize
            self.canvas.configure(yscrollcommand=self.scrollbar.set)
            region = self.canvas.bbox("all")
            self.canvas.configure(scrollregion=region)
            logging.info(f"Background load finished [rid={render_id}].")
            if hasattr(self, 'load_start_time'):
                load_duration = time.time() - self.load_start_time
                logging.info(f"FINAL FOLDER LOADING TIME: {load_duration:.3f}s")
  
      


    def queue_thumbnail(self, file_path, file_name, row, col, index, is_folder=False, thumbnail_time=None, force_refresh=False,overwrite=False, target_frame=None, render_id=None):
        """Add thumbnail task to queue."""
        if render_id is None and target_frame is not None:
            render_id = self._render_id
        task = (file_path, file_name, row, col, index, is_folder, thumbnail_time, force_refresh, target_frame, render_id)
        self.thumb_queue.put(task)
        if not self.thumb_queue_running:
            self.process_thumbnail_batch()


    def process_thumbnail_batch(self):
            """Processes a batch of thumbnail tasks."""
            self.thumb_queue_running = True
            count = 0
            
            batch_limit = 4 if getattr(self, "search_results_active", False) else 24
            
            while not self.thumb_queue.empty() and count < batch_limit:
                file_path, file_name, row, col, index, is_folder, thumbnail_time, force_refresh, target_frame, render_id = self.thumb_queue.get()
                if render_id is not None and render_id != self._render_id:
                    count += 1
                    continue
                overwrite = force_refresh

                if is_folder:
                    self.create_folder_thumb(file_path, file_name, row, col, index, target_frame, render_id=render_id)
                else:
                    self.create_file_thumbnail(file_path, file_name, row, col, index, thumbnail_time, overwrite, target_frame, render_id=render_id)
                
                count += 1

            if not self.thumb_queue.empty():
                self.after(10, self.process_thumbnail_batch)  
            else:
                self.thumb_queue_running = False
                
                if hasattr(self, 'current_directory') and self.current_directory:
                    cd = self.current_directory
                    _blocked = getattr(self, "_folder_cache_auto_mark_is_blocked", None)
                    if not (callable(_blocked) and _blocked(cd)):
                        self.database.update_cache_status(cd, True)
                        self.refresh_folder_icon(cd)

      

    def update_all_scaling(self, scale_factor):
            """
            Applies all scaling settings to the application based on the new scale_factor.
            """
            profile = self._get_scaling_profile(scale_factor)
            hi = "hi-dpi" if scale_factor >= 1.5 else "std"
            logging.info(
                "[DPI] update_all_scaling: scale_factor=%.4f profile=%s widget_scale=%s window_scale=%s",
                float(scale_factor),
                hi,
                profile["widget_scale"],
                profile["window_scale"],
            )

            widget_scale = profile["widget_scale"]
            window_scale = profile["window_scale"]

            # Tk keeps its own pixels-per-point factor; after per-monitor DPI change it can
            # lag behind GetDpiForWindow so ttk / tkfont metrics still behave like the old
            # monitor (huge tree + caption measurements -> rh=400+ and broken layouts).
            try:
                dpi = int(round(float(scale_factor) * 96.0))
                dpi = max(72, min(768, dpi))
                px_per_pt = dpi / 72.0
                try:
                    self.tk.call("tk", "scaling", "-displayof", self, px_per_pt)
                except tk.TclError:
                    self.tk.call("tk", "scaling", px_per_pt)
                logging.info(
                    "[DPI] tk scaling -> pixels_per_pt=%.6f (dpi=%s, scale_factor=%.4f)",
                    px_per_pt,
                    dpi,
                    float(scale_factor),
                )
            except Exception as exc:
                logging.warning("[DPI] tk scaling sync failed: %s", exc)

            ctk.set_widget_scaling(widget_scale)
            ctk.set_window_scaling(window_scale)

            # Keep all font/row/indent sizing idempotent and based on base values.
            self._apply_thumb_font_scaling(scale_factor)
            if hasattr(self, 'update_treeview_scaling'):
                # Pass scale_factor explicitly so tree profile matches this apply even if
                # self.current_dpi_scale is updated in the same tick elsewhere.
                self.update_treeview_scaling(widget_scale, dpi_scale=scale_factor)

            # Tree bitmaps were sized at first setup_icons(); reload when logical DPI changes
            # so folder glyphs match row height (tk scaling alone is not enough).
            if hasattr(self, "setup_icons"):
                try:
                    self.setup_icons()
                except SystemExit:
                    raise
                except Exception as exc:
                    logging.error("[DPI] setup_icons after scaling failed: %s", exc, exc_info=True)
            if hasattr(self, "_refresh_all_tree_icons_after_icon_reload"):
                try:
                    self._refresh_all_tree_icons_after_icon_reload()
                except Exception as exc:
                    logging.warning("[DPI] refresh tree icons: %s", exc)

    def _get_scaling_profile(self, scale_factor):
            """
            Returns a single source of truth for all scale-sensitive UI values.
            Must stay idempotent (same DPI => same sizes, no drift).
            """
            # scale_factor = GetDpiForWindow/96 (e.g. 1.5 at 150 %, 2.0 at 200 %).
            # On a 4K panel you are almost always in THIS branch (>= 1.5), not the else below.
            if scale_factor >= 1.5:
                return {
                    "widget_scale": 1.2,
                    "window_scale": 1.1,
                    "tree_font_multiplier": 1.2,
                    "thumb_font_multiplier": 1.15,
                    # ~25% tighter than prior 48; default font 11 (effective ~13).
                    "tree_row_base": 36,
                    "tree_indent_base": 22,
                }
            # 100–125 % displays only (scale_factor < 1.5)
            return {
                "widget_scale": 0.9,
                "window_scale": 1.0,
                "tree_font_multiplier": 1.0,
                "thumb_font_multiplier": 1.0,
                # ~25% tighter than prior 30 → ~20 px row at default font 11.
                "tree_row_base": 22,
                "tree_indent_base": 16,
            }

    def _apply_thumb_font_scaling(self, scale_factor):
            profile = self._get_scaling_profile(scale_factor)
            thumb_size = max(7, int(round(self.thumbFontSize * profile["thumb_font_multiplier"])))
            folder_title_size = max(9, int(round(self.folder_title_font_base_size * profile["thumb_font_multiplier"])))

            # Update dynamic thumb labels already present in UI.
            labels = getattr(self, "thumbnail_labels", None)
            if not isinstance(labels, dict):
                labels = {}
            for _, info in labels.items():
                label = info.get("label")
                if label:
                    try:
                        label.configure(font=("Helvetica", thumb_size, "normal"))
                    except Exception:
                        pass

            # Keep folder titles in thumbnail grid visually consistent across monitors.
            try:
                self.folder_title_font.configure(size=folder_title_size)
            except Exception:
                pass

    def _get_effective_thumb_font_size(self):
            profile = self._get_scaling_profile(self.current_dpi_scale)
            return max(7, int(round(self.thumbFontSize * profile["thumb_font_multiplier"])))

    def _thumb_pixel_scale(self) -> float:
        """1.0 at 320px-wide thumbs; scales chrome for smaller tile presets (e.g. 160x120)."""
        w, _h = getattr(self, "thumbnail_size", (320, 240))
        return max(0.42, min(1.0, float(w) / 320.0))

    def effective_thumb_border_size(self) -> int:
        base = int(getattr(self, "thumb_BorderSize", 14))
        s = self._thumb_pixel_scale()
        return max(4, min(14, int(round(base * s))))

    def effective_thumb_outlinewidth(self) -> int:
        base = int(getattr(self, "outlinewidth", 2))
        s = self._thumb_pixel_scale()
        return max(1, min(base, int(round(base * max(s, 0.55)))))

    def effective_thumb_cell_padding(self) -> int:
        base = int(getattr(self, "thumb_Padding", 2))
        s = self._thumb_pixel_scale()
        return max(0, min(8, int(round(base * max(s, 0.45)))))

    def effective_thumb_frame_radius(self) -> int:
        s = self._thumb_pixel_scale()
        return max(5, min(16, int(round(16 * s))))



    def _apply_geometry_fix(self):
        """
        Applies geometry fixes (like forcing canvas width)
        after a short delay. This is safe to call even when
        the window size hasn't changed (e.g., on focus change).
        """
        try:
            if hasattr(self, 'canvas') and hasattr(self, 'scrollable_frame'):
                new_canvas_width = self.canvas.winfo_width()
                if new_canvas_width > 1:
                    # Force the canvas window to exactly canvas width.
                    # This propagates to wide_folders_frame via pack(fill="x")
                    # without locking its height (unlike CTkFrame.configure(width=X)
                    # which freezes both width AND height via _desired_width/_desired_height).
                    if hasattr(self, 'scrollable_frame_window_id'):
                        self.canvas.itemconfigure(self.scrollable_frame_window_id, width=new_canvas_width)
                    else:
                        self.scrollable_frame.configure(width=new_canvas_width)
                    self.update_idletasks()
        except Exception as e:
            logging.warning(f"[GEOMETRY_FIX_ERROR] Failed to update scrollable_frame width: {e}")


    def _on_main_canvas_configure(self, event):
        """
        Called whenever self.canvas resizes (<Configure> bind).
        event.width is the NEW canvas width — no delay, no winfo_width() needed.
        This catches ALL resize scenarios: manual drag, double-click maximize,
        OS fullscreen button, and programmatic state('zoomed').
        """
        if getattr(self, "_vg_active", False):
            try:
                self._vg_on_canvas_resize(event)
            except Exception:
                pass
            return
        if event.width > 1 and hasattr(self, 'scrollable_frame_window_id'):
            self.canvas.itemconfigure(self.scrollable_frame_window_id, width=event.width)


    def on_window_resize(self, event):
        """
        Handles window <Configure> events (resize, move, focus OR DPI change).
        Schedules actions ONLY if a real size or DPI change is detected.
        """
        
        if event.widget != self:
            return

        # --- [NEW FLAG] ---
        dpi_changed = False 

        # --- [DPI LOGIC] ---
        try:
            hwnd = self.winfo_id()
            if hwnd != 0:
                current_dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
                new_scale = current_dpi / 96.0

                if new_scale != self.current_dpi_scale:
                    logging.info(
                        "[DPI] on_window_resize: GetDpiForWindow scale %.4f -> %.4f (scheduling apply)",
                        float(self.current_dpi_scale),
                        float(new_scale),
                    )
                    self._pending_dpi_scale = new_scale
                    dpi_changed = True 
                    
        except Exception as e:
            logging.info(f"[DEBUG] DPI check failed (transient error): {e}")
        # --- [END OF DPI LOGIC] ---

        # --- [RESIZE CHECK LOGIC] ---
        new_size = (event.width, event.height)
        size_changed = (new_size != self._previous_size)

        if size_changed:
            self._previous_size = new_size
        
        # Only size/DPI changes schedule work — pure focus <Configure> is ignored
        if size_changed or dpi_changed:
            
            logging.info(f"Scheduling geometry fix and content reload (Size changed: {size_changed}, DPI changed: {dpi_changed})")

            if hasattr(self, '_geometry_fix_timer_id'):
                self.after_cancel(self._geometry_fix_timer_id)
            self._geometry_fix_timer_id = self.after(100, self._apply_geometry_fix)
            
            if hasattr(self, '_resize_timer_id'):
                self.after_cancel(self._resize_timer_id)
            self._resize_timer_id = self.after(250, self._perform_resize_actions)
            
        else:
            pass


    def _perform_resize_actions(self):
            """ Handles recalculating after resize or DPI change. """
            # Per-monitor DPI can change without a reliable <Configure> ordering vs size.
            # Re-read from the window HWND so we never apply split geometry on stale scale.
            try:
                hwnd = self.winfo_id()
                if hwnd:
                    detected = ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0
                    if abs(detected - getattr(self, "current_dpi_scale", 1.0)) > 0.01:
                        self._pending_dpi_scale = detected
            except Exception:
                pass

            dpi_applied = False
            if self._pending_dpi_scale is not None:
                pending_scale = self._pending_dpi_scale
                prev_scale = getattr(self, "current_dpi_scale", None)
                self._pending_dpi_scale = None
                # Commit logical DPI before applying so tree/thumb helpers that read
                # current_dpi_scale stay aligned with this monitor.
                self.current_dpi_scale = pending_scale
                logging.info(
                    "[DPI] _perform_resize_actions: applying pending_scale=%.4f (previous current_dpi_scale=%s)",
                    float(pending_scale),
                    prev_scale,
                )
                self.update_all_scaling(pending_scale)
                # Ensure root window remains fully opaque after monitor move.
                try:
                    self.attributes("-alpha", 1.0)
                except Exception:
                    pass
                dpi_applied = True
                logging.info("Running update_idletasks() after scaling change...")
                self.update_idletasks()
                logging.info("...update_idletasks() finished.")
                # Virtual grid column count uses canvas winfo_width; after DPI move Tk/CTk
                # geometry can lag one frame — nudge recalc so thumbs are not stuck in 1-wide layout.
                if getattr(self, "_vg_active", False):

                    def _vg_nudge_dpi():
                        if not getattr(self, "_vg_active", False):
                            return
                        try:
                            self._vg_on_canvas_resize()
                        except Exception as exc:
                            logging.debug("[DPI] virtual grid nudge after DPI: %s", exc)

                    self.after(120, _vg_nudge_dpi)

            # CTk DPI/window scaling can empty the main tk.PanedWindow; fix before sashes.
            self._repair_main_horizontal_panes()

            if hasattr(self, 'set_initial_split_heights'):
                # 50ms is too aggressive after monitor move; inner frames lag behind Tk.
                delay = 350 if dpi_applied else 120
                logging.info(f"Scheduling split heights recalculation with {delay}ms delay...")
                if hasattr(self, '_split_fix_timer_id'):
                    self.after_cancel(self._split_fix_timer_id)
                self._split_fix_timer_id = self.after(delay, self.set_initial_split_heights)

            # ... redraw thumbnails ...
            # current_path = self.current_directory
            # if not current_path: return
            # if self._is_loading:
                # logging.info("Resize-triggered reload skipped: loading already in progress.")
                # return
            # logging.info("Resize detected, triggering thumbnail redisplay.")
            # self.display_thumbnails(current_path)





    # Helper function to reliably get the current path
    def get_current_selected_path(self):
        """Gets the path of the currently selected item in the tree."""
        selection = self.tree.selection()
        if selection:
            return self.tree.item(selection[0], 'values')[0]
        return None
        
        def adjust_info_panel_height(self, desired_info_height=320):
            if hasattr(self, "left_split") and hasattr(self, "info_panel"):
                total_height = self.left_split.winfo_height()
                if total_height > desired_info_height + 50:
                    sash_y = total_height - desired_info_height
                    try:
                        if self.left_split.sash_coord(0):  # kontrola existence sashes
                            self.left_split.sash_place(0, 0, sash_y)
                            logging.info(f"[DEBUG] Adjusted info panel height to {desired_info_height}px (sash Y={sash_y})")
                    except tk.TclError:
                        logging.info("adjust_info_panel_height: sash index invalid — UI not fully ready")

    

    def set_tree_font_size(self, size):
        """
        Sets the base font size for the treeview and updates its scaling.
        (Comments added by Gemini)

        Args:
            size (int): The new base font size (e.g., 11).
        """
        self.base_font_size = size
        
        # --- FIX ---
        # We must now pass the *current* widget_scale to update_treeview_scaling,
        # otherwise, it will raise a TypeError.
        
        # 1. Determine current widget scale using the same centralized profile
        current_widget_scale = self._get_scaling_profile(self.current_dpi_scale)["widget_scale"]
            
        # 2. Call the function with the required argument
        # Do NOT reload tree icons here: ttk.Treeview grows rows to fit images, and
        # re-creating PhotoImages mid-session can permanently inflate row spacing.
        self.update_treeview_scaling(current_widget_scale)


    def _tree_icon_pixel_height(self) -> int:
        """Tallest currently loaded tree PhotoImage, or 0 if none yet."""
        tallest = 0
        for attr in (
            "folder_treeicon",
            "folder_treeicon_green",
            "folder_virtual_icon",
            "hdd_icon",
            "google_icon",
            "desktop_icon",
            "downloads_icon",
            "documents_icon",
            "pictures_icon",
            "videos_icon",
        ):
            img = getattr(self, attr, None)
            if img is None:
                continue
            try:
                tallest = max(tallest, int(img.height()))
            except Exception:
                pass
        return tallest


    def set_thumb_font_size(self, size):
        self.thumbFontSize = size
        self._apply_thumb_font_scaling(self.current_dpi_scale)


    def update_treeview_scaling(self, widget_scale, dpi_scale=None):
        """
        Applies scaling to the treeview.
        Uses widget_scale for row height, but a *custom* multiplier
        for the font size to allow independent scaling.
        
        Args:
            widget_scale (float): The base CTk scale (e.g., 0.9 or 1.2).
            dpi_scale (float | None): Logical DPI / 96 for profile lookup; defaults to
                self.current_dpi_scale (must match the monitor driving tree_font_multiplier).
        """
        
        if not hasattr(self, 'base_font_size') or not hasattr(self, 'LTreeBGColor'):
            logging.warning("[update_treeview_scaling] Skipped (preferences not loaded yet)")
            return 

        scale_for_profile = self.current_dpi_scale if dpi_scale is None else dpi_scale
        profile = self._get_scaling_profile(scale_for_profile)
        hi = "hi-dpi" if float(scale_for_profile) >= 1.5 else "std"

        tree_indent = max(10, int(round(profile["tree_indent_base"] * widget_scale)))
        new_font_size = max(7, int(round(self.base_font_size * profile["tree_font_multiplier"])))

        # Measure with a real Font (tk scaling), but apply size as a plain tuple —
        # that is what used to update Treeview text live. Named/anonymous Font
        # objects were changing metrics (rowheight) without repainting glyphs.
        measure_font = tkfont.Font(self, family="Helvetica", size=new_font_size)
        linespace = int(measure_font.metrics("linespace"))

        default_tree_font = 11
        font_ratio = max(7, int(self.base_font_size)) / float(default_tree_font)
        base_row = max(16, int(round(profile["tree_row_base"] * widget_scale)))
        default_font_px = max(7, int(round(default_tree_font * profile["tree_font_multiplier"])))
        default_linespace = int(
            tkfont.Font(self, family="Helvetica", size=default_font_px).metrics("linespace")
        )
        default_pad = max(2, base_row - default_linespace)
        pad = max(2, int(round(default_pad * font_ratio)))
        desired_row = max(16, linespace + pad)
        # Treeview will not shrink below image size — never ask for a shorter row
        # than the glyphs already attached to items.
        icon_h = self._tree_icon_pixel_height()
        self.row_height = max(desired_row, icon_h + 2) if icon_h else desired_row

        # Prefer the same Style instance used when the tree was created.
        style = getattr(self, "tree_style", None) or ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "NoBorder.Treeview",
            background=self.LTreeBGColor,
            fieldbackground=self.LTreeBGColor,
            foreground=self.tree_TextColor,
            rowheight=self.row_height,
            font=("Helvetica", new_font_size),
            indent=tree_indent,
        )
        # Windows/clam often keeps the old glyph cache until the style is rebound.
        tree = getattr(self, "tree", None)
        if tree is not None:
            try:
                tree.configure(style="NoBorder.Treeview")
            except tk.TclError:
                pass

        applied = None
        try:
            applied = style.lookup("NoBorder.Treeview", "font")
        except tk.TclError:
            pass
        logging.info(
            "[update_treeview_scaling] Applied profile=%s dpi_scale=%.4f widget_scale=%s "
            "row_height=%s font_px=%s style_font=%r linespace=%s pad=%s icon_h=%s indent=%s",
            hi,
            float(scale_for_profile),
            widget_scale,
            self.row_height,
            new_font_size,
            applied,
            linespace,
            pad,
            icon_h,
            tree_indent,
        )






    def show_thumbnail_context_menu(self, event, file_path):
        """
        Show a context menu specifically for thumbnail (file) actions.
        """
        menu = tk.Menu(self, tearoff=0)
        _hk = getattr(self, "hotkeys_map", None) or DEFAULT_HOTKEYS

        video_name = os.path.basename(file_path)
        
        
            
        mimetype, _ = mimetypes.guess_type(file_path)
        lower_path = file_path.lower()

        if (mimetype and mimetype.startswith("video")) or lower_path.endswith(VIDEO_FORMATS):
            menu.add_command(label="▶ Play Video", command=lambda:  self.play_video_selection(file_path) )   #self.open_video_player(file_path, video_name)
            convert_paths = self.selected_video_paths_for_convert(file_path)
            _cv_label = (
                "Convert Video…"
                if len(convert_paths) <= 1
                else f"Convert Videos… ({len(convert_paths)})"
            )
            menu.add_command(
                label=_cv_label,
                command=lambda paths=convert_paths: self.open_convert_video_dialog(paths),
            )
        elif (mimetype and mimetype.startswith("image")) or lower_path.endswith(IMAGE_FORMATS):
            menu.add_command(label="🖼 Show Image", command=lambda: self.open_image_viewer(file_path, os.path.basename(file_path)))
            menu.add_separator()
            # Image edit tools (multi-select: resize batches; crop is single-image).
            edit_paths = self.selected_image_paths_for_edit(file_path)
            n_edit = len(edit_paths)
            _crop_acc = menu_accel(_hk, "image_crop")
            _crop_opts = {
                "label": "Crop…",
                "command": lambda fp=file_path: self.start_image_crop_from_grid(fp),
            }
            if _crop_acc:
                _crop_opts["accelerator"] = _crop_acc
            menu.add_command(**_crop_opts)
            _rs_acc = menu_accel(_hk, "image_resize")
            _rs_label = "Resize Image…" if n_edit <= 1 else f"Resize Images… ({n_edit})"
            _rs_opts = {
                "label": _rs_label,
                "command": lambda fp=file_path: self.start_image_resize_from_grid(fp),
            }
            if _rs_acc:
                _rs_opts["accelerator"] = _rs_acc
            menu.add_command(**_rs_opts)

            # Rotate / flip — batch-safe (same transform on all selected images).
            def _xf_label(base: str) -> str:
                return base if n_edit <= 1 else f"{base} ({n_edit})"

            _rl_acc = menu_accel(_hk, "image_rotate_left")
            _rl_opts = {
                "label": _xf_label("Rotate Left"),
                "command": lambda fp=file_path: self.start_image_transform_from_grid(
                    fp, "rotate_left"
                ),
            }
            if _rl_acc:
                _rl_opts["accelerator"] = _rl_acc
            menu.add_command(**_rl_opts)

            _rr_acc = menu_accel(_hk, "image_rotate_right")
            _rr_opts = {
                "label": _xf_label("Rotate Right"),
                "command": lambda fp=file_path: self.start_image_transform_from_grid(
                    fp, "rotate_right"
                ),
            }
            if _rr_acc:
                _rr_opts["accelerator"] = _rr_acc
            menu.add_command(**_rr_opts)

            _fh_acc = menu_accel(_hk, "image_flip_h")
            _fh_opts = {
                "label": _xf_label("Flip Horizontal"),
                "command": lambda fp=file_path: self.start_image_transform_from_grid(
                    fp, "flip_h"
                ),
            }
            if _fh_acc:
                _fh_opts["accelerator"] = _fh_acc
            menu.add_command(**_fh_opts)

            _fv_acc = menu_accel(_hk, "image_flip_v")
            _fv_opts = {
                "label": _xf_label("Flip Vertical"),
                "command": lambda fp=file_path: self.start_image_transform_from_grid(
                    fp, "flip_v"
                ),
            }
            if _fv_acc:
                _fv_opts["accelerator"] = _fv_acc
            menu.add_command(**_fv_opts)

            compare_paths = self.selected_image_paths_for_compare(file_path)
            if len(compare_paths) >= 2:
                _cmp_acc = menu_accel(_hk, "image_compare")
                _cmp_opts = {
                    "label": "Compare Images…",
                    "command": lambda paths=compare_paths: self.open_image_compare(paths),
                }
                if _cmp_acc:
                    _cmp_opts["accelerator"] = _cmp_acc
                menu.add_command(**_cmp_opts)

            _bc_label = (
                "Batch Convert / Rename…"
                if n_edit <= 1
                else f"Batch Convert / Rename… ({n_edit})"
            )
            menu.add_command(
                label=_bc_label,
                command=lambda fp=file_path: self.open_batch_convert_dialog(fp),
            )

        else:
            menu.add_command(label="Open", command=lambda: os.startfile(file_path))  # fallback

        append_external_apps_cascade(menu, self, file_path)

        compare_video_paths = self.selected_video_paths_for_compare(file_path)
        if len(compare_video_paths) >= 2:
            _vcmp_acc = menu_accel(_hk, "image_compare")
            _vcmp_opts = {
                "label": "Compare Videos…",
                "command": lambda paths=compare_video_paths: self.open_video_compare(paths),
            }
            if _vcmp_acc:
                _vcmp_opts["accelerator"] = _vcmp_acc
            menu.add_command(**_vcmp_opts)

        merge_paths = self.selected_video_paths_for_merge(file_path)
        if len(merge_paths) >= 2:
            menu.add_command(
                label="Merge Videos…",
                command=lambda paths=merge_paths: self.open_merge_videos_dialog(paths),
            )

        menu.add_command(label="Refresh Thumbnail", command=self.refresh_selected_thumbnails)
        
        # menu.add_command(   label="Refresh Thumbnail",command=lambda: self.refresh_single_thumbnail(file_path,True))
        

        _kw = menu_accel(_hk, "keywords")
        _kw_opts = {"label": "Add Keywords", "command": lambda: self.open_keyword_window(file_path)}
        if _kw:
            _kw_opts["accelerator"] = _kw
        menu.add_command(**_kw_opts)
        menu.add_command(label="Remove Keywords", command=lambda: self.open_remove_keyword_window(file_path))
        _pl = menu_accel(_hk, "add_to_playlist")
        _pl_opts = {"label": "Add to Existing Playlist", "command": lambda: self.add_selected_to_playlist()}
        if _pl:
            _pl_opts["accelerator"] = _pl
        menu.add_command(**_pl_opts)
        _pn = menu_accel(_hk, "new_playlist")
        _pn_opts = {
            "label": "Add to New Playlist",
            "command": lambda: self.add_selected_to_playlist(event, new_playlist=True),
        }
        if _pn:
            _pn_opts["accelerator"] = _pn
        menu.add_command(**_pn_opts)
        def _apply_thumb_rating(rating, fp=file_path):
            """Rate whole selection when the clicked thumb is selected; else just that file."""
            selected = getattr(self, "selected_thumbnails", None) or []
            db = getattr(self, "database", None)
            if selected and db is not None:
                norm = db.normalize_path(fp)
                if any(db.normalize_path(p) == norm for p, _, _ in selected):
                    self.set_rating(rating)
                    return
            self.save_rating(fp, rating)

        append_rating_submenu(
            menu,
            self,
            file_path,
            hotkeys_map=_hk,
            apply_fn=_apply_thumb_rating,
        )
        _rn = rename_accelerators_label(_hk)
        _rename_opts = {"label": "Rename", "command": lambda: self.rename_item(file_path)}
        if _rn:
            _rename_opts["accelerator"] = _rn
        menu.add_command(**_rename_opts)
        action_paths = self.paths_for_file_action_context(file_path, event)
        _del = menu_accel(_hk, "delete")
        _del_opts = {
            "label": "Delete",
            "command": lambda paths=action_paths: self.confirm_delete_item(paths=paths),
        }
        if _del:
            _del_opts["accelerator"] = _del
        menu.add_command(**_del_opts)

        menu.add_separator()
        _cp = menu_accel(_hk, "files_clipboard_copy")
        _ct = menu_accel(_hk, "files_clipboard_cut")
        _copy_opts = {"label": "Copy", "command": lambda fp=file_path: self.copy_thumb_paths_to_clipboard(fp)}
        if _cp:
            _copy_opts["accelerator"] = _cp
        menu.add_command(**_copy_opts)
        menu.add_command(
            label="Copy full file path",
            command=lambda fp=file_path: self.copy_full_file_path_as_text(fp),
        )
        _cut_opts = {"label": "Cut", "command": lambda fp=file_path: self.copy_thumb_paths_to_clipboard(fp, cut=True)}
        if _ct:
            _cut_opts["accelerator"] = _ct
        menu.add_command(**_cut_opts)
        self.add_clipboard_paste_cascade(menu, getattr(self, "current_directory", None))

        # Plugin-based auto-tagging / offline AI upscale
        if hasattr(self, "plugin_manager") and (
            self.plugin_manager.plugins or getattr(self.plugin_manager, "upscale_plugins", None)
        ):
            menu.add_separator()
        menu.add_command(
            label="Auto Tag",
            # command=lambda: self.auto_tag_with_plugin_from_file(file_path)
            command=lambda: self.auto_tag_selected_items(file_path)
        )
        if hasattr(self, "open_upscale_dialog"):
            menu.add_command(
                label="Upscale…",
                command=lambda: self.open_upscale_dialog(file_path),
            )

        # Add to / Remove from Virtual Library
        virtual_libraries = list(load_virtual_folders()["virtual_folders"].keys())
        active_vl = None
        cd = getattr(self, "current_directory", None)
        if isinstance(cd, str) and cd.startswith("virtual_library://"):
            active_vl = cd.split("://", 1)[1].strip() or None
            if active_vl and active_vl not in virtual_libraries:
                active_vl = None

        add_menu = tk.Menu(menu, tearoff=0)
        for name in virtual_libraries:
            add_menu.add_command(
                label=name,
                command=lambda name=name: self.add_to_virtual_library(
                    self.selected_thumbnails, name
                ),
            )
        if virtual_libraries:
            add_menu.add_separator()
        add_menu.add_command(
            label="Create New Virtual Library",
            command=self.create_virtual_library,
        )
        menu.add_cascade(label="Add to Virtual Library", menu=add_menu)

        # Remove: only the library currently open (listing every VL is confusing).
        if active_vl:
            menu.add_command(
                label=f"Remove from Virtual Library ({active_vl})",
                command=lambda name=active_vl: self.remove_from_virtual_library(
                    self.selected_thumbnails, name
                ),
            )
        else:
            menu.add_command(
                label="Remove from Virtual Library",
                state=tk.DISABLED,
            )

        menu.tk_popup(event.x_root, event.y_root)


    def selected_image_paths_for_compare(self, primary_path=None):
        """Image paths from multi-select (selection order) for the compare dialog.

        When ``primary_path`` is set (context menu), require that path to be part of
        the selection when more than one image is selected — same pattern as merge.
        """
        primary = os.path.normpath(primary_path) if primary_path else None
        raw = list(getattr(self, "selected_thumbnails", []) or [])
        paths = []
        seen = set()
        for item in raw:
            p = item[0] if isinstance(item, tuple) and item else item
            if not p:
                continue
            p = os.path.normpath(str(p))
            key = os.path.normcase(p)
            if key in seen:
                continue
            if not os.path.isfile(p) or not p.lower().endswith(IMAGE_FORMATS):
                continue
            seen.add(key)
            paths.append(p)
        if primary is None:
            return paths
        if len(paths) > 1 and os.path.normcase(primary) in {
            os.path.normcase(p) for p in paths
        }:
            return paths
        return []

    def open_image_compare(self, paths=None):
        """Open the dual-mode image compare dialog for ``paths`` or current selection."""
        if paths is None:
            paths = self.selected_image_paths_for_compare(None)
        open_image_compare_dialog(self, paths)

    def selected_video_paths_for_compare(self, primary_path=None):
        """Video paths from multi-select (selection order) for the compare dialog.

        When ``primary_path`` is set (context menu), require that path to be part of
        the selection when more than one video is selected — same pattern as images.
        """
        primary = os.path.normpath(primary_path) if primary_path else None
        raw = list(getattr(self, "selected_thumbnails", []) or [])
        paths = []
        seen = set()
        for item in raw:
            p = item[0] if isinstance(item, tuple) and item else item
            if not p:
                continue
            p = os.path.normpath(str(p))
            key = os.path.normcase(p)
            if key in seen:
                continue
            if not os.path.isfile(p) or not p.lower().endswith(VIDEO_FORMATS):
                continue
            seen.add(key)
            paths.append(p)
        if primary is None:
            return paths
        if len(paths) > 1 and os.path.normcase(primary) in {
            os.path.normcase(p) for p in paths
        }:
            return paths
        return []

    def open_video_compare(self, paths=None):
        """Open the Side-by-Side video compare dialog for ``paths`` or current selection."""
        if paths is None:
            paths = self.selected_video_paths_for_compare(None)
        open_video_compare_dialog(self, paths)

    def selected_video_paths_for_merge(self, primary_path):
        """Video paths from multi-select (selection order) when RMB target is part of it."""
        primary = os.path.normpath(primary_path) if primary_path else None
        raw = list(getattr(self, "selected_thumbnails", []) or [])
        paths = []
        seen = set()
        for item in raw:
            p = item[0] if isinstance(item, tuple) and item else item
            if not p:
                continue
            p = os.path.normpath(str(p))
            key = os.path.normcase(p)
            if key in seen:
                continue
            if not os.path.isfile(p) or not p.lower().endswith(VIDEO_FORMATS):
                continue
            seen.add(key)
            paths.append(p)
        if primary and len(paths) > 1 and os.path.normcase(primary) in {
            os.path.normcase(p) for p in paths
        }:
            return paths
        return []

    def open_merge_videos_dialog(self, video_paths):
        open_merge_videos_dialog(self, video_paths, controller=self)

    def selected_video_paths_for_convert(self, clicked_path=None):
        """Video paths for Convert: multi-select when RMB target is in it, else clicked file."""
        primary = os.path.normpath(clicked_path) if clicked_path else None
        raw = list(getattr(self, "selected_thumbnails", []) or [])
        paths = []
        seen = set()
        for item in raw:
            p = item[0] if isinstance(item, tuple) and item else item
            if not p:
                continue
            p = os.path.normpath(str(p))
            key = os.path.normcase(p)
            if key in seen:
                continue
            if not os.path.isfile(p) or not p.lower().endswith(VIDEO_FORMATS):
                continue
            seen.add(key)
            paths.append(p)
        if primary and len(paths) > 1 and os.path.normcase(primary) in {
            os.path.normcase(p) for p in paths
        }:
            return paths
        if primary and os.path.isfile(primary) and primary.lower().endswith(VIDEO_FORMATS):
            return [primary]
        return paths

    def open_convert_video_dialog(self, video_paths):
        """Whole-file convert / remux from thumbnail RMB (single or batch)."""
        if isinstance(video_paths, (str, os.PathLike)):
            video_paths = [video_paths]
        open_convert_video_dialog(self, video_paths, controller=self)

    def reveal_merged_file(self, file_path):
        """Refresh current folder (if output is there) and select the merged file."""
        if not file_path or not os.path.isfile(file_path):
            return
        file_path = os.path.normpath(file_path)
        out_dir = os.path.dirname(file_path)
        cur = getattr(self, "current_directory", None)
        if not cur or not os.path.isdir(cur):
            return
        if os.path.normcase(os.path.normpath(cur)) != os.path.normcase(out_dir):
            logging.info(
                "[Merge] Output is outside current folder (%s); skip grid reveal",
                out_dir,
            )
            return

        self._pending_select_path = file_path
        try:
            self.display_thumbnails(cur, force_refresh=False, preserve_scroll=False)
        except Exception:
            logging.exception("[Merge] display_thumbnails after merge failed")
            self._pending_select_path = None
            return
        self.after(200, lambda: self._try_select_pending_path(retries=20))

    def _index_for_grid_path(self, file_path):
        if not file_path:
            return None
        if getattr(self, "_vg_active", False) and getattr(self, "_vg_data_index_by_path", None):
            try:
                idx = self._vg_data_index_by_path.get(self._vg_norm_path(file_path), -1)
            except Exception:
                idx = -1
            if idx is not None and idx >= 0:
                return int(idx)
        want = os.path.normcase(os.path.normpath(file_path))
        for i, vf in enumerate(getattr(self, "video_files", []) or []):
            try:
                if os.path.normcase(os.path.normpath(vf.get("path", ""))) == want:
                    return i
            except Exception:
                continue
        return None

    def _scroll_grid_to_index(self, idx):
        if idx is None or idx < 0:
            return
        if not getattr(self, "_vg_active", False):
            return
        try:
            cols = max(1, int(getattr(self, "_vg_cols", 1) or 1))
            rh = float(getattr(self, "_vg_row_height", 1) or 1)
            if getattr(self, "_vg_is_wide", False):
                fc = int(getattr(self, "_vg_folder_count", 0) or 0)
                wrh = float(getattr(self, "_vg_wide_row_height", 1) or 1)
                wide_cols = max(1, int(getattr(self, "_vg_wide_cols", 1) or 1))
                wide_rows = int(getattr(self, "_vg_wide_rows", 0) or 0)
                if idx < fc:
                    y = (idx // wide_cols) * wrh
                else:
                    y = wide_rows * wrh + ((idx - fc) // cols) * rh
            else:
                y = (idx // cols) * rh
            canvas_h = float(getattr(self, "_vg_canvas_h", 1) or 1)
            sr = float(getattr(self, "_vg_scrollregion_h", 1) or 1)
            y = max(0.0, y - canvas_h * 0.2)
            max_scroll = max(1.0, sr - canvas_h)
            frac = min(1.0, y / max_scroll) if sr > canvas_h else 0.0
            self.canvas.yview_moveto(frac)
            self._vg_layout_slots(frac * sr)
        except Exception:
            logging.debug("[Merge] scroll_grid_to_index failed", exc_info=True)

    def _try_select_pending_path(self, retries=20):
        path = getattr(self, "_pending_select_path", None)
        if not path:
            return
        if getattr(self, "_is_loading", False):
            if retries > 0:
                self.after(150, lambda: self._try_select_pending_path(retries - 1))
            return

        idx = self._index_for_grid_path(path)
        if idx is None:
            if retries > 0:
                self.after(150, lambda: self._try_select_pending_path(retries - 1))
            else:
                logging.warning("[Merge] Could not find merged file in grid: %s", path)
                self._pending_select_path = None
            return

        self._scroll_grid_to_index(idx)
        self.after(60, lambda p=path, i=idx: self._finish_select_pending_path(p, i))

    def _finish_select_pending_path(self, path, idx):
        if getattr(self, "_pending_select_path", None) is None:
            return
        label_info = self._thumbnail_label_info_for_path(path, idx)
        if not label_info:
            label_info = {"index": idx, "canvas": None, "path": path}
        self.selected_thumbnails = [(path, label_info, idx)]
        self.selected_thumbnail_index = idx
        self.selected_file_path = path
        try:
            self.update_thumbnail_selection()
        except Exception:
            logging.debug("[Merge] update_thumbnail_selection failed", exc_info=True)
        try:
            self.update_panel_info(path)
        except Exception:
            pass
        self._pending_select_path = None
        logging.info("[Merge] Selected merged file in grid: %s", path)

    def _try_restore_pending_selection(self, retries=20):
        """Restore multi-selection by path after an async grid reload (e.g. new folder)."""
        paths = getattr(self, "_pending_select_paths", None)
        if not paths:
            return
        if getattr(self, "_is_loading", False):
            if retries > 0:
                self.after(150, lambda: self._try_restore_pending_selection(retries - 1))
            return

        new_selection = []
        missing = 0
        for path in paths:
            idx = self._index_for_grid_path(path)
            if idx is None:
                missing += 1
                continue
            label_info = self._thumbnail_label_info_for_path(path, idx)
            if not label_info:
                label_info = {"index": idx, "canvas": None, "path": path}
            new_selection.append((path, label_info, idx))

        if not new_selection:
            if retries > 0 and missing:
                self.after(150, lambda: self._try_restore_pending_selection(retries - 1))
            else:
                self._pending_select_paths = None
            return

        self.selected_thumbnails = new_selection
        self.selected_thumbnail_index = new_selection[-1][2]
        self.selected_file_path = new_selection[-1][0]
        self._prev_selected_indices = set()
        try:
            self.update_thumbnail_selection()
        except Exception:
            logging.debug("restore selection: update_thumbnail_selection failed", exc_info=True)
        if getattr(self, "_vg_active", False):
            try:
                self._vg_reapply_selection()
            except Exception:
                logging.debug("restore selection: _vg_reapply_selection failed", exc_info=True)
        try:
            self.update_status_bar()
        except Exception:
            pass
        self._pending_select_paths = None
        logging.info("Restored thumbnail selection: %s item(s)", len(new_selection))

    def play_video_selection(self, file_path):
            """
            Plays the specific video or a group of videos if multiple are selected.
            Robust path matching (normalizes slashes) and forces UI refresh.
            Handles cases where selected_thumbnails contains tuples.
            """
            import os
            
            target_path = os.path.normpath(file_path)
            
            raw_selection = list(self.selected_thumbnails) if hasattr(self, "selected_thumbnails") else []
            
            cleaned_selection = []
            for item in raw_selection:
                if isinstance(item, tuple):
                    if len(item) > 0:
                        cleaned_selection.append(item[0])
                else:
                    cleaned_selection.append(item)
            
            selection_normalized = [os.path.normpath(str(p)) for p in cleaned_selection if p]
            
            video_exts = VIDEO_FORMATS
            
            logging.info(f"[Play-Selection] Target: {target_path}")
            logging.info(f"[Play-Selection] Selection size: {len(selection_normalized)}")

            def _n(p):
                return os.path.normcase(os.path.normpath(p))

            # Preserve selection order (unique videos only).
            playlist_videos = []
            seen = set()
            for p in selection_normalized:
                if not str(p).lower().endswith(video_exts):
                    continue
                key = _n(p)
                if key in seen:
                    continue
                seen.add(key)
                playlist_videos.append(p)

            multi = len(playlist_videos) > 1 and _n(target_path) in {_n(p) for p in playlist_videos}
            if multi:
                logging.info(f"[Multi-Play] Playing {len(playlist_videos)} selected videos as playlist.")
                self.playlist_manager.playlist = list(playlist_videos)
                if hasattr(self.playlist_manager, "original_playlist"):
                    self.playlist_manager.original_playlist = list(playlist_videos)

                try:
                    start_index = next(
                        i for i, p in enumerate(playlist_videos) if _n(p) == _n(target_path)
                    )
                except StopIteration:
                    start_index = 0

                self.playlist_manager.current_playing_index = start_index
                try:
                    self.playlist_manager.populate_playlist_box()
                except Exception:
                    pass

                start_path = playlist_videos[start_index]
                logging.info(
                    "[Multi-Play] Starting at index %s: %s",
                    start_index,
                    os.path.basename(start_path),
                )
                self.open_video_player(start_path, os.path.basename(start_path))
                return

            # Single-file play
            logging.info(f"[Single-Play] Playing single file: {target_path}")
            self.open_video_player(target_path, os.path.basename(target_path))

    @staticmethod
    def _parse_keyword_list_from_db(raw):
        """Split stored keywords the same way as save/update paths (comma-separated, strip)."""
        if raw is None:
            return []
        s = str(raw).strip()
        if not s or s == "No keywords":
            return []
        return [k.strip() for k in s.split(",") if k.strip()]

    def open_remove_keyword_window(self, file_path):
        if not self.selected_thumbnails:
            logging.info("No thumbnails selected")
            return

        # gather unique keywords across all selected thumbnails
        all_keywords = set()
        for thumb_path, _, _ in self.selected_thumbnails:
            all_keywords.update(self._parse_keyword_list_from_db(self.database.get_keywords(thumb_path)))

        if not all_keywords:
            logging.info("No keywords found in the selected thumbnails")
            return

        sorted_kw = sorted(all_keywords)

        # initialize keyword removal window
        self.remove_keyword_window = ctk.CTkToplevel(self)
        self.remove_keyword_window.title("Remove Keywords")
        self.remove_keyword_window.minsize(480, 200)
        self._center_toplevel_window(self.remove_keyword_window, 520, 240)
        self.remove_keyword_window.transient(self)
        self.remove_keyword_window.attributes("-topmost", True)
        self.remove_keyword_window.lift()
        self.remove_keyword_window.focus_force()

        # create a CTkFrame for consistent layout
        frame = ctk.CTkFrame(self.remove_keyword_window)
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        # label
        ctk.CTkLabel(frame, text="Select a keyword to remove:").pack(pady=5)

        # initialize keyword selection variable and optionmenu
        self.keyword_var = ctk.StringVar(self.remove_keyword_window)
        self.keyword_var.set(sorted_kw[0])

        # optionmenu
        self.option_menu = ctk.CTkOptionMenu(
            frame,
            variable=self.keyword_var,
            values=sorted_kw,
        )
        self.option_menu.pack(pady=5, fill="x")

        btn_row = ctk.CTkFrame(frame, fg_color="transparent")
        btn_row.pack(fill="x", pady=(10, 0))
        ctk.CTkButton(
            btn_row,
            text="Remove Selected Keyword",
            command=self.remove_keyword_from_selection,
        ).pack(side="left", expand=True, fill="x", padx=(0, 5))
        ctk.CTkButton(
            btn_row,
            text="Remove All",
            command=self.remove_all_keywords_from_selection,
        ).pack(side="left", expand=True, fill="x", padx=(5, 0))




    def remove_keyword_from_selection(self):
        """Remove the selected keyword from all selected thumbnails."""
        selected_keyword = self.keyword_var.get()
        # ensure `selected_keyword` is valid before proceeding
        if not selected_keyword or selected_keyword == "No keywords":
            logging.info("No keyword selected")
            return

        for file_path, _, _ in self.selected_thumbnails:
            raw_kw = self.database.get_keywords(file_path)
            keywords = self._parse_keyword_list_from_db(raw_kw)
            if selected_keyword in keywords:
                # remove the keyword and update the database
                keywords.remove(selected_keyword)
                updated_keywords = ", ".join(sorted(set(keywords)))
                self.database.update_keywords(file_path, updated_keywords)
                logging.info(f"Removed keyword '{selected_keyword}' from {file_path}")

                if getattr(self, "_vg_active", False):
                    self._vg_refresh_file_labels(file_path)
                else:
                    thumbnail_info = self.thumbnail_labels.get(file_path)
                    if thumbnail_info:
                        row, col = thumbnail_info["row"], thumbnail_info["col"]
                        thumbnail_frame = thumbnail_info["canvas"].master

                        is_folder_status = os.path.isdir(file_path)
                        self.update_thumbnail_label(
                            file_path=file_path,
                            file_name=os.path.basename(file_path),
                            thumbnail_frame=thumbnail_frame,
                            canvas=thumbnail_info["canvas"],
                            row=row,
                            col=col,
                            index=thumbnail_info["index"],
                            labelBGColor="gray",
                            thumb_backFill=False,
                            canvas_height=240,
                            canvas_width=320,
                            is_folder=is_folder_status,
                        )

        # refresh the OptionMenu with remaining keywords
        self.refresh_option_menu()



    
    def refresh_option_menu(self):
          
        # gather all remaining keywords across selected thumbnails
        remaining_keywords = set()
        for thumb_path, _, _ in self.selected_thumbnails:
            remaining_keywords.update(
                self._parse_keyword_list_from_db(self.database.get_keywords(thumb_path))
            )

        # refresh the CTkOptionMenu with updated keywords
        if remaining_keywords:
            sorted_keywords = sorted(remaining_keywords)  # optional: keep it sorted
            self.option_menu.configure(values=sorted_keywords)  # update values in the OptionMenu
            self.keyword_var.set(sorted_keywords[0])  # set to the first keyword
            self.option_menu.configure(state="normal")  # ensure menu is enabled
        else:
            # if no keywords left, disable the menu and set placeholder
            self.option_menu.configure(values=["No keywords"])
            self.keyword_var.set("No keywords")
            self.option_menu.configure(state="disabled")  # disable interaction



    def refresh_keyword_displays_for_paths(self, paths):
        """Repaint under-thumb captions for the given files after a global keyword change.

        Accepts any path casing; matching is done on normalized paths. Only files that are
        currently rendered in the grid are refreshed (others repaint naturally on next render).
        """
        if not paths:
            return
        try:
            affected = {self.database.normalize_path(p) for p in paths if p}
        except Exception:
            affected = set(paths)
        if not affected:
            return

        if getattr(self, "_vg_active", False):
            for path in list(self.thumbnail_labels.keys()):
                try:
                    if self.database.normalize_path(path) in affected:
                        self._vg_refresh_file_labels(path)
                except Exception:
                    continue
            return

        for path, info in list(self.thumbnail_labels.items()):
            try:
                if self.database.normalize_path(path) not in affected or not info:
                    continue
                row, col = info["row"], info["col"]
                thumbnail_frame = info["canvas"].master
                self.update_thumbnail_label(
                    file_path=path,
                    file_name=os.path.basename(path),
                    thumbnail_frame=thumbnail_frame,
                    canvas=info["canvas"],
                    row=row,
                    col=col,
                    index=info["index"],
                    labelBGColor="gray",
                    thumb_backFill=False,
                    canvas_height=240,
                    canvas_width=320,
                    is_folder=os.path.isdir(path),
                )
            except Exception:
                continue

    def remove_all_keywords_from_selection(self):
        """Remove all keywords from all selected thumbnails."""
        for file_path, _, _ in self.selected_thumbnails:
            self.database.update_keywords(file_path, '')
            logging.info(f"Removed all keywords from {file_path}")

            if getattr(self, "_vg_active", False):
                self._vg_refresh_file_labels(file_path)
            else:
                thumbnail_info = self.thumbnail_labels.get(file_path)
                if thumbnail_info:
                    row, col = thumbnail_info["row"], thumbnail_info["col"]
                    thumbnail_frame = thumbnail_info["canvas"].master
                    is_folder_status = os.path.isdir(file_path)
                    self.update_thumbnail_label(
                        file_path=file_path,
                        file_name=os.path.basename(file_path),
                        thumbnail_frame=thumbnail_frame,
                        canvas=thumbnail_info["canvas"],
                        row=row,
                        col=col,
                        index=thumbnail_info["index"],
                        labelBGColor="gray",
                        thumb_backFill=False,
                        canvas_height=240,
                        canvas_width=320,
                        is_folder=is_folder_status,
                    )

        # close the keyword removal window
        self._close_remove_keyword_window()







