"""Thumbnail grid pipeline mixin for VideoThumbnailPlayer."""
from __future__ import annotations

import ctypes
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
import tkinter.ttk as ttk
from tkinter import messagebox
import tkinterdnd2 as dnd

from PIL import Image, ImageDraw, ImageOps, ImageTk

from file_operations import *
from gui_elements import create_search_window
from image_operations import create_image_viewer
from video_operations import VideoPlayer
from utils import get_video_size
from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS, preview_skip_subdir
from virtual_folders import load_virtual_folders


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

    def _prepare_thumbnail_data(self, dir_path, sort_option=None, filter_option=None):
        """
        Loads, processes, and sorts the list of files and folders for the given directory path.
        Handles virtual libraries and filesystem errors.
        sort_option, filter_option: must be passed when called from worker thread (Tkinter vars not thread-safe).
        Returns the sorted list of item dictionaries, or None if an error occurs or the directory is empty.
        Uses the _process_single_entry_for_list helper.
        """
        video_files_list = [] # Use a local list to gather items
        try:
            # Load file list (virtual or real)
            if dir_path.startswith("virtual_library://"):
                # Process virtual library contents into the local list
                library_name = dir_path.split("://")[1]
                # Ensure load_virtual_folders() returns the expected structure
                virtual_data = load_virtual_folders()
                entries = virtual_data.get("virtual_folders", {}).get(library_name, [])
                logging.info(f"Processing virtual library '{library_name}' with {len(entries)} entries.")
                for file_path in entries:
                    # Add processed entry to video_files_list
                    entry_data = self._process_single_entry_for_list(file_path)
                    if entry_data:
                        video_files_list.append(entry_data)
                    else:
                        # Log if an entry from virtual library is skipped (e.g., file deleted)
                        logging.warning(f"Skipping invalid or unsupported entry from virtual library '{library_name}': {file_path}")


            else:
                # Process real directory contents into the local list
                if not os.path.isdir(dir_path):
                     # This check might be redundant if _initialize_thumbnail_display worked, but good for safety
                     logging.error(f"Path is not a directory: {dir_path}")
                     # Raise specific error type consistent with os.listdir failure
                     raise FileNotFoundError(f"Directory not found or is not a directory: {dir_path}")

                logging.info(f"Processing directory contents for: {dir_path}")
                # Use scandir for potentially better performance on large directories
                # Wrap in try-except specifically for os.scandir permission issues
                try:
                    with os.scandir(dir_path) as it:
                        for entry in it:
                            # Add processed entry using the helper function
                            entry_data = self._process_single_entry_for_list(entry.path)
                            if entry_data:
                                video_files_list.append(entry_data)
                except PermissionError:
                     # Re-raise PermissionError to be caught by the outer try-except
                     raise
                except OSError as e:
                     # Catch other OS errors during scandir (e.g., path too long on Windows)
                     logging.error(f"OS Error scanning directory '{dir_path}': {e}", exc_info=True)
                     # Re-raise as a generic Exception or handle specifically
                     raise Exception(f"Failed to scan directory: {e}") from e


        except FileNotFoundError:
            # Error already logged in _initialize or caught here
            logging.error(f"Directory not found during data preparation: {dir_path}") # Log again for clarity
            return None # Return None on critical error
        except PermissionError:
            # Error already logged in _initialize or caught here
            logging.error(f"Permission denied during data preparation for: {dir_path}")
            # No need to show messagebox again if _initialize already did
            return None # Return None on critical error
        except Exception as e:
            # Catch errors from scandir or _process_single_entry_for_list
            logging.error(f"Unexpected error preparing thumbnail data for {dir_path}: {e}", exc_info=True)
            # messagebox must run on main thread — schedule from worker
            msg = f"Failed to read directory contents:\n{dir_path}"
            self.after(0, lambda m=msg: messagebox.showerror("Error", m))
            return None # Return None on unexpected error

        # Sort the collected files only if the list is not empty
        if not video_files_list:
            logging.info("No media files found or processed in directory.")
            # Update main list to empty
            self.video_files = []
        else:
            logging.info(f"Sorting {len(video_files_list)} collected items...")
            try:
                # Sorting might fail if sort_key accesses properties incorrectly
                sorted_items = self.sort_thumbnails(video_files_list, sort_option, filter_option)
                self.video_files = sorted_items # Update the main class attribute *after* sorting
            except Exception as e:
                logging.error(f"Error during sorting thumbnails for {dir_path}: {e}", exc_info=True)
                self.after(0, lambda: messagebox.showerror("Error", "Failed to sort directory items."))
                return None # Return None if sorting fails critically

        # Status bar updates must run on main thread
        self.after(0, self.update_status_bar)

        # Final check on self.video_files after potential sorting error handling
        if not self.video_files:
            # Empty list or sorting failed — adjust UI on main thread
            def _empty_ui_update():
                try:
                    self.wide_folders_frame.pack_forget()
                    self.regular_thumbnails_frame.pack_forget()
                    self.regular_thumbnails_frame.pack(side="top", fill="both", expand=True, padx=5, pady=5)
                    self.adjust_scroll_region_and_filler()
                except Exception as e:
                    logging.error(f"Error adjusting UI for empty directory {dir_path}: {e}")
            self.after(0, _empty_ui_update)
            return None # Return None if empty

        logging.info(f"Prepared and sorted {len(self.video_files)} items.")
        return self.video_files # Return the final sorted list (or unsorted list if sort failed and Option 1 was chosen)




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






    def display_thumbnails(
        self, dir_path, force_refresh=False, thumbnail_time=None, preserve_scroll=False
    ):
        """
        Async flow:
        1. Clear the grid immediately (user sees feedback).
        2. Worker loads and sorts the file list.
        3. Main thread renders the GUI.

        preserve_scroll: if True, restore vertical canvas scroll fraction after reload (virtual grid only). Used e.g. after in-place DnD refresh of the same folder.
        """
        # Capture before any clear — clear_thumbnails resets yview.
        if preserve_scroll:
            try:
                if getattr(self, "_vg_active", False):
                    self._thumb_reload_preserve_yview = max(
                        0.0, min(1.0, float(self.canvas.yview()[0]))
                    )
                else:
                    self._thumb_reload_preserve_yview = None
            except Exception:
                self._thumb_reload_preserve_yview = None
        else:
            self._thumb_reload_preserve_yview = None

        # Force the UI to calculate its actual dimensions before we start
        # self.update_idletasks()

        # 1. Init, cancel stale load, capture render_id (includes clear_thumbnails)
        self._initialize_thumbnail_display(dir_path)
        my_render_id = self._render_id  # Snapshot — older async phases will abort when they see this changed

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

        # 4. Heavy work (listdir, sort) in thread pool
        self.executor.submit(
            self._worker_prepare_and_display,
            dir_path,
            force_refresh,
            thumbnail_time,
            my_render_id,
            sort_option,
            filter_option,
        )



        
    def _worker_prepare_and_display(self, dir_path, force_refresh, thumbnail_time, render_id, sort_option, filter_option):
        try:
            # Abort immediately if a newer load has already been requested
            if self._render_id != render_id:
                self._is_loading = False
                return

            # 1. Load data (file listing, sort) — heavy I/O, off main thread
            sorted_file_list = self._prepare_thumbnail_data(dir_path, sort_option, filter_option)

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

        # self.update_idletasks() # Ensure frame sizes are current

        total_content_height = 0
        padding_y = self.thumb_Padding * 2 # Or your specific vertical padding

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
            border_size = getattr(self, 'thumb_BorderSize', 14)
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

                final_time_for_video = None
                if thumbnail_time is not None and item_info['path'].lower().endswith(VIDEO_FORMATS):
                    final_time_for_video = self.calculate_thumbnail_time(item_info['path'])
                
                self.queue_thumbnail(
                    item_info['path'], item_info['name'],
                    row, col, global_index,
                    is_folder=item_info.get('is_folder', False),
                    target_frame=self.regular_thumbnails_frame,
                    force_refresh=force_refresh, thumbnail_time=final_time_for_video
                )
            if not getattr(self, "_vg_active", False):
                self.update_idletasks()
                self.adjust_scroll_region_and_filler()
                self.canvas.yview_moveto(0)
        finally:
             self._is_loading = False






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
            final_time_for_video = None
            if thumbnail_time is not None and file_info['path'].lower().endswith(VIDEO_FORMATS):
                 final_time_for_video = self.calculate_thumbnail_time(file_info['path'])
                
            self.queue_thumbnail(
                file_info['path'], file_info['name'], row, col, index,
                is_folder=file_info.get('is_folder', False),
                target_frame=self.regular_thumbnails_frame,
                force_refresh=force_refresh,
                thumbnail_time=final_time_for_video
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
        if key not in self._folder_media_presence_cache:
            self._folder_media_presence_cache[key] = self.folder_contains_media(folder_path)
        return self._folder_media_presence_cache[key]


    def set_wide_folder_columns(self, num_columns):
        """Set the number of columns for wide folders and refresh display."""
        self.numwidefolders_in_col = num_columns
        self.display_thumbnails(self.current_directory, preserve_scroll=True)  # Refresh the display
    
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
            self.wide_folders_check_var.set(new_mode == "Wide")

        self.display_thumbnails(self.current_directory, preserve_scroll=True)


    def _on_check_var_changed(self, *args):
        is_wide = self.wide_folders_check_var.get()
        self.folder_view_mode.set("Wide" if is_wide else "Standard")




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
        padding_x = self.thumb_Padding * 2
        padding_y = self.thumb_Padding * 2

        # --- Calculate total thumbnail dimensions including borders/padding ---
        try:
            # Use attributes if they exist (safer)
            border_size = getattr(self, 'thumb_BorderSize', 14) # Default if not found
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
            canvas_width = thumb_width + (self.thumb_BorderSize * 2)
            total_thumb_width = canvas_width + (self.thumb_Padding * 2)
            canvas_height = thumb_height + (self.thumb_BorderSize * 2) + 10
            total_thumb_height = canvas_height + (self.thumb_Padding * 2)
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
                            thumbnail = create_video_thumbnail(
                                file_path, self.thumbnail_size, self.thumbnail_format,
                                self.capture_method_var.get(), thumbnail_time=thumbnail_time,
                                cache_enabled=self.cache_enabled, overwrite=overwrite,
                                cache_dir=self.thumbnail_cache_path,
                                database=self.database
                            )
                    else:
                        thumbnail = create_image_thumbnail(
                            file_path, self.thumbnail_size, database=self.database, 
                            cache_dir=self.thumbnail_cache_path
                        )
                    
                    if thumbnail is not None and memory_cache:
                        thumbnail_cache.set(file_path, thumbnail, memory_cache=memory_cache)

                if thumbnail is None:
                    if file_name.lower().endswith(VIDEO_FORMATS):
                        # Metadata can look OK while every frame grab fails — show explicit placeholder.
                        thumbnail = self._create_corrupted_thumbnail_image()
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
                        is_cached=True
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
                    if idx % cols != 0:
                        new_indices.add(idx - 1)
                elif direction == "right":
                    if (idx % cols != cols - 1) and (idx < total - 1):
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
            if idx % cols == 0:
                return
            new_idx = idx - 1
        elif direction == "right":
            if (idx % cols == cols - 1) or (idx == total - 1):
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
            if idx % cols == 0:
                return  # already left column
            new_idx = idx - 1
        elif direction == "right":
            if (idx % cols == cols - 1) or (idx == total - 1):
                return  # right column or last thumb
            new_idx = idx + 1
        else:
            return

        self.select_thumbnail(new_idx, shift=shift, ctrl=ctrl)







    def open_image_viewer(self, image_path, image_name):
        if hasattr(self, 'current_image_window') and self.current_image_window:
            self.current_image_window.image_window.destroy()

        use_pyglet = getattr(self, "image_viewer_use_pyglet", False)
        self.current_image_window = create_image_viewer(
            self, image_path, image_name, use_pyglet
        )


    def setup_icons(self):
        """
        Load and scale tree/grid icons from the /icons subdirectory and prepare PhotoImage/CTkImage versions.
        """
        try:
            logging.debug("Loading and scaling tree icons from app/icons")
            
            P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
            
            scaling = float(self.tk.call('tk', 'scaling')) or 1.0
            if scaling >= 2.0:
                icon_size = 48
            elif scaling >= 1.5:
                icon_size = 32
            else:
                icon_size = 24

            logging.info(f"[DEBUG] Tree icons: DPI scaling={scaling}, using icon_size={icon_size}")

            # 1) Load PIL images from disk
            folder_tree_pil = Image.open(os.path.join(P, "tree_folder.PNG")).resize((icon_size, icon_size), Image.LANCZOS)
            folder_tree_green_pil = Image.open(os.path.join(P, "tree_folder_green.png")).resize((icon_size, icon_size), Image.LANCZOS)
            folder_virtual_pil = Image.open(os.path.join(P, "tree_folder_virtual.png")).resize((icon_size, icon_size), Image.LANCZOS)
            hdd_pil = Image.open(os.path.join(P, "tree_hdd.PNG")).resize((icon_size, icon_size), Image.LANCZOS)
            google_pil = Image.open(os.path.join(P, "tree_google.PNG")).resize((icon_size, icon_size), Image.LANCZOS)
            desktop_pil = Image.open(os.path.join(P, "tree_desktop.png")).resize((icon_size, icon_size), Image.LANCZOS)
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
        if render_id is None:
            render_id = self._render_id
        task = (file_path, file_name, row, col, index, is_folder, thumbnail_time, force_refresh, target_frame, render_id)
        self.thumb_queue.put(task)
        if not self.thumb_queue_running:
            self.process_thumbnail_batch()


    def process_thumbnail_batch(self):
            """Processes a batch of thumbnail tasks."""
            self.thumb_queue_running = True
            count = 0
            
            batch_limit = 24 
            
            while not self.thumb_queue.empty() and count < batch_limit:
                file_path, file_name, row, col, index, is_folder, thumbnail_time, force_refresh, target_frame, render_id = self.thumb_queue.get()
                if render_id != self._render_id:
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
                    self.database.update_cache_status(self.current_directory, True)
                    self.refresh_folder_icon(self.current_directory)

      

    def update_all_scaling(self, scale_factor):
            """
            Applies all scaling settings to the application based on the new scale_factor.
            """
            logging.info(f"Applying new scaling factor: {scale_factor}")

            profile = self._get_scaling_profile(scale_factor)
            widget_scale = profile["widget_scale"]
            window_scale = profile["window_scale"]

            ctk.set_widget_scaling(widget_scale)
            ctk.set_window_scaling(window_scale)

            # Keep all font/row/indent sizing idempotent and based on base values.
            self._apply_thumb_font_scaling(scale_factor)
            if hasattr(self, 'update_treeview_scaling'):
                self.update_treeview_scaling(widget_scale)

    def _get_scaling_profile(self, scale_factor):
            """
            Returns a single source of truth for all scale-sensitive UI values.
            Must stay idempotent (same DPI => same sizes, no drift).
            """
            if scale_factor >= 1.5:  # 4K-ish
                return {
                    "widget_scale": 1.2,
                    "window_scale": 1.1,
                    "tree_font_multiplier": 1.2,
                    "thumb_font_multiplier": 1.15,
                    "tree_row_base": 55,
                    "tree_indent_base": 22,
                }
            return {
                "widget_scale": 0.9,
                "window_scale": 1.0,
                "tree_font_multiplier": 1.0,
                "thumb_font_multiplier": 1.0,
                "tree_row_base": 55,
                "tree_indent_base": 22,
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
                    logging.info(f"DPI change detected! Old: {self.current_dpi_scale}, New: {new_scale}")
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
                self.update_all_scaling(pending_scale)
                self.current_dpi_scale = pending_scale
                self._pending_dpi_scale = None
                # Ensure root window remains fully opaque after monitor move.
                try:
                    self.attributes("-alpha", 1.0)
                except Exception:
                    pass
                dpi_applied = True
                logging.info("Running update_idletasks() after scaling change...")
                self.update_idletasks()
                logging.info("...update_idletasks() finished.")

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
        self.update_treeview_scaling(current_widget_scale)




    def set_thumb_font_size(self, size):
        self.thumbFontSize = size
        self._apply_thumb_font_scaling(self.current_dpi_scale)


    def update_treeview_scaling(self, widget_scale):
        """
        Applies scaling to the treeview.
        Uses widget_scale for row height, but a *custom* multiplier
        for the font size to allow independent scaling.
        
        Args:
            widget_scale (float): The base CTk scale (e.g., 0.9 or 1.2).
        """
        
        if not hasattr(self, 'base_font_size') or not hasattr(self, 'LTreeBGColor'):
            logging.warning("[update_treeview_scaling] Skipped (preferences not loaded yet)")
            return 
            
        profile = self._get_scaling_profile(self.current_dpi_scale)

        # Keep row height and tree indentation tied to the same profile.
        self.row_height = max(20, int(round(profile["tree_row_base"] * widget_scale)))
        tree_indent = max(10, int(round(profile["tree_indent_base"] * widget_scale)))
        new_font_size = max(8, int(round(self.base_font_size * profile["tree_font_multiplier"])))

        style = ttk.Style(self)
        style.layout("NoBorder.Treeview", [('Treeview.treearea', {'sticky': 'nswe'})])
        style.configure(
            "NoBorder.Treeview",
            background=self.LTreeBGColor,
            fieldbackground=self.LTreeBGColor,
            foreground=self.tree_TextColor,
            rowheight=self.row_height, 
            font=("Helvetica", new_font_size),
            indent=tree_indent
        )
        logging.info(
            f"[update_treeview_scaling] Applied. Scale={widget_scale}, RowHeight={self.row_height}, "
            f"Font Size={new_font_size}, Indent={tree_indent}"
        )






    def show_thumbnail_context_menu(self, event, file_path):
        """
        Show a context menu specifically for thumbnail (file) actions.
        """
        menu = tk.Menu(self, tearoff=0)
        
        video_name = os.path.basename(file_path)
        
        
            
        mimetype, _ = mimetypes.guess_type(file_path)

        if mimetype and mimetype.startswith("video"):
            menu.add_command(label="▶ Play Video", command=lambda:  self.play_video_selection(file_path) )   #self.open_video_player(file_path, video_name)
        elif mimetype and mimetype.startswith("image"):
            menu.add_command(label="🖼 Show Image", command=lambda: self.open_image_viewer(file_path, os.path.basename(file_path)))

        else:
            menu.add_command(label="Open", command=lambda: os.startfile(file_path))  # fallback
            
                
        menu.add_command(label="Refresh Thumbnail", command=self.refresh_selected_thumbnails)
        
        # menu.add_command(   label="Refresh Thumbnail",command=lambda: self.refresh_single_thumbnail(file_path,True))
        

        menu.add_command(label="Add Keywords", command=lambda: self.open_keyword_window(file_path))
        menu.add_command(label="Remove Keywords", command=lambda: self.open_remove_keyword_window(file_path))
        menu.add_command(label="Add to Existing Playlist", command=lambda: self.add_selected_to_playlist())
        menu.add_command(label="Add to New Playlist", command=lambda: self.add_selected_to_playlist(event, new_playlist=True))
        menu.add_command(label="Edit Rating", command=lambda: self.edit_rating(file_path))
        menu.add_command(label="Rename", command=lambda: self.rename_item(file_path))
        menu.add_command(label="Delete", command=lambda: self.confirm_delete_item(paths=[file_path]))

        menu.add_separator()
        menu.add_command(
            label="Copy",
            command=lambda fp=file_path: self.copy_thumb_paths_to_clipboard(fp),
        )
        self.add_clipboard_paste_cascade(menu, getattr(self, "current_directory", None))

        # Plugin-based auto-tagging (only if plugin is loaded)
        if hasattr(self, "plugin_manager") and self.plugin_manager.plugins:
            menu.add_separator()
        menu.add_command(
            label="Auto Tag",
            # command=lambda: self.auto_tag_with_plugin_from_file(file_path)
            command=lambda: self.auto_tag_selected_items(file_path)
        )

        # menu.add_command(label="Create New Virtual Library", command=self.create_virtual_library)
        # Add to / Remove from Virtual Library
        virtual_libraries = load_virtual_folders()["virtual_folders"].keys()
        if virtual_libraries:
            add_menu = tk.Menu(menu, tearoff=0)
            remove_menu = tk.Menu(menu, tearoff=0)
            for name in virtual_libraries:
                add_menu.add_command(label=name, command=lambda name=name: self.add_to_virtual_library(self.selected_thumbnails, name))
                remove_menu.add_command(label=name, command=lambda name=name: self.remove_from_virtual_library(self.selected_thumbnails, name))
            menu.add_cascade(label="Add to Virtual Library", menu=add_menu)
            menu.add_cascade(label="Remove from Virtual Library", menu=remove_menu)
        else:
            menu.add_command(label="Add to Virtual Library", state=tk.DISABLED)
            menu.add_command(label="Remove from Virtual Library", state=tk.DISABLED)

        menu.tk_popup(event.x_root, event.y_root)


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
            
            if len(selection_normalized) > 1 and target_path in selection_normalized:
                logging.info(f"[Multi-Play] Detected selection of {len(selection_normalized)} items.")
                
                playlist_videos = [p for p in selection_normalized if p.lower().endswith(video_exts)]
                playlist_videos.sort()
                
                if playlist_videos:
                    self.playlist_manager.playlist = list(playlist_videos)
                    
                    if hasattr(self.playlist_manager, "original_playlist"):
                        self.playlist_manager.original_playlist = list(playlist_videos)
                    
                    self.playlist_manager.is_playlist_open = True 
                    
                    try:
                        start_index = playlist_videos.index(target_path)
                    except ValueError:
                        start_index = 0
                    
                    self.playlist_manager.current_playing_index = start_index
                    
                    if hasattr(self.playlist_manager, "populate_playlist"):
                        self.playlist_manager.populate_playlist()
                    elif hasattr(self.playlist_manager, "refresh_playlist"):
                        self.playlist_manager.refresh_playlist()
                    elif hasattr(self.playlist_manager, "update_playlist"):
                        self.playlist_manager.update_playlist()

                    logging.info(f"[Multi-Play] Playlist populated with {len(playlist_videos)} videos. Starting at index {start_index}.")
                    
                    self.open_video_player(target_path, os.path.basename(target_path))
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







    # The user's function with the missing 'path' logic added.
    def joininfotexts(self, file_path, file_name):
        """
        Constructs a list of informational texts about a file based on user-selected options.
        Args:
            self: The application instance.
            file_path (str): The full path to the file.
            file_name (str): The name of the file.
        Returns:
            list: A list of tuples, where each tuple contains (text_to_display, color).
        """
        width = None
        height = None
        info_texts = []

        # Check if the 'name' option is enabled in the menu.
        if self.file_info_vars.get("name").get():
            info_texts.append((file_name, "gray70")) # Display file name.

        # === THIS IS THE FIX ===
        # Check if the 'path' option is enabled in the menu.
        if self.file_info_vars.get("path").get():
            # If checked, add the full file path to the list.
            info_texts.append((f"Path: {file_path}", "#9ec5e8"))

        # Check if the 'file_size' option is enabled.
        if self.file_info_vars.get("file_size").get():
            info_texts.append((f"Size: {os.path.getsize(file_path)} bytes", "#9ec5e8"))

        # Check if the 'date_time' option is enabled.
        if self.file_info_vars.get("date_time").get():
            mod_time = time.localtime(os.path.getmtime(file_path))
            info_texts.append((f"Modified: {time.strftime('%Y-%m-%d %H:%M:%S', mod_time)}", "#9ec5e8"))

        # Check if the 'dimensions' option is enabled.
        if self.file_info_vars.get("dimensions").get():
            db_entry = self.database.get_entry(file_path) # Fetch entry from database.
            if db_entry:
                width = db_entry.get('width')
                height = db_entry.get('height')
            if width and height:
                info_texts.append((f"Dimensions: {width}x{height}", "#9ec5e8"))

        # Check if the 'keywords' option is enabled.
        if self.file_info_vars.get("keywords").get():
            keywords = self.database.get_keywords(file_path)
            if keywords and keywords != "No keywords":
                # Clean up the keywords string by removing leading commas and whitespace.
                keywords = keywords.lstrip(",").strip()
                info_texts.append((f"Keywords: {keywords}", "#8ecae6"))

        return info_texts
    
    




    def contains_media_files(self,path):
        allowed_extensions = VIDEO_FORMATS + IMAGE_FORMATS  # ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.mp4', '.avi', '.mov', '.wmv', '.mpg', '.mpeg')
        for root, dirs, files in os.walk(path):
            for file in files:
                if file.lower().endswith(allowed_extensions):
                    return True
        return False


    @staticmethod
    def _round_rect_polygon_points(x1, y1, x2, y2, radius, steps_per_corner=12):
        """
        Flat list of vertices tracing a rounded rectangle (true circular corners).
        Tk's smooth-spline duplicate-point trick was unreliable on Windows (sharp corners).
        """
        import math

        x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
        r = min(max(0.0, float(radius)), (x2 - x1) / 2.0, (y2 - y1) / 2.0)
        if r <= 0.5:
            return [x1, y1, x2, y1, x2, y2, x1, y2]
        n = max(6, int(steps_per_corner))
        pts: list[float] = []

        def arc(cx: float, cy: float, th0: float, th1: float) -> None:
            for i in range(1, n + 1):
                th = th0 + (th1 - th0) * (i / n)
                pts.extend([cx + r * math.cos(th), cy + r * math.sin(th)])

        # Counter-clockwise from top edge: (x1+r,y1) -> ... -> back to start.
        pts.extend([x1 + r, y1, x2 - r, y1])
        arc(x2 - r, y1 + r, 1.5 * math.pi, 2 * math.pi)
        pts.extend([x2, y2 - r])
        arc(x2 - r, y2 - r, 0.0, 0.5 * math.pi)
        pts.extend([x1 + r, y2])
        arc(x1 + r, y2 - r, 0.5 * math.pi, math.pi)
        pts.extend([x1, y1 + r])
        arc(x1 + r, y1 + r, math.pi, 1.5 * math.pi)
        return pts

    def create_rounded_rectangle(self, canvas, x1, y1, x2, y2, radius=60, **kwargs):
        points = self._round_rect_polygon_points(x1, y1, x2, y2, radius)
        opts = dict(kwargs)
        opts["smooth"] = False
        opts.pop("splinesteps", None)
        try:
            return canvas.create_polygon(points, **opts)
        except (TypeError, tk.TclError):
            return canvas.create_polygon(points, **opts)
          
          
    def update_thumbnail_label(self, file_path, file_name, thumbnail_frame, canvas, row, col, index, labelBGColor, thumb_backFill, canvas_height, canvas_width, is_folder):
        # validate the thumbnail frame
        if not thumbnail_frame.winfo_exists():
            logging.info(f"Thumbnail frame for {file_path} no longer exists. Removing stale reference.")
            if file_path in self.thumbnail_labels:
                del self.thumbnail_labels[file_path]  # clean up stale reference
            return
        
     
        # Initialize name_label to None. This ensures the variable always exists,
        # even if no text info is displayed and the loop below is skipped.
        name_label = None
        
        try:
            self.reset_display(frame=thumbnail_frame, widget_type=ctk.CTkLabel)
        except Exception as e:
            logging.info(f"Error clearing labels in frame for {file_path}: {e}")

        info_text = self.joininfotexts(file_path, file_name)
        max_characters = 30  # Limit for label text to avoid resizing issues
        try:
            # If info_text is empty, this loop will be skipped, but name_label will remain None.
            for text, color in info_text:
                display_text = text if len(text) <= max_characters else text[:max_characters - 3] + "..."

                label_font = self.folder_title_font if is_folder else ("Helvetica", self._get_effective_thumb_font_size(), "normal")
                caption_fill = self.BackroundColor
                if isinstance(caption_fill, (tuple, list)):
                    caption_fill = caption_fill[1] if len(caption_fill) > 1 else caption_fill[0]
                if not caption_fill or caption_fill == "transparent":
                    caption_fill = "#101215"
                label_fg_color = "transparent" if is_folder else (caption_fill if thumb_backFill else "transparent")
                # For folders, always use the default text color (like empty strips)
                # For files, use the color determined by joininfotexts (usually 'gray70' or 'lightblue')
                label_text_color = None if is_folder else color # None uses the default theme color
                # The name_label variable is overwritten in each iteration.
                # Only the last created label will be stored.
                name_label = ctk.CTkLabel(
                    thumbnail_frame,
                    text=display_text,
                    font=label_font, # Use the determined font
                    wraplength=self.thumbnail_size[0],
                    fg_color=label_fg_color, # Apply the determined background color
                    text_color=label_text_color # Apply the determined text color
                )
                name_label.pack(pady=(9, 3))

            # Now, this part is safe. 'name_label' is either a CTkLabel widget or None.
            # This prevents the UnboundLocalError crash.
            self.thumbnail_labels[file_path] = {
                "row": row,
                "col": col,
                "index": index,
                "canvas": canvas,
                "label": name_label 
            }
            
            if self.folder_view_mode.get() == "Wide":
                canvas.create_rectangle(0, 0, canvas_width, canvas_height, outline=self.thumbBorderColor, width=self.outlinewidth, tags="border")

        except Exception as e:
            logging.info(f"Error creating label for {file_name}: {e}")            
                  
          
          
                
            
    def add_rating_bar(self, file_path, thumbnail_frame, rating, canvas_width):
        """
        Add a rating bar to the thumbnail frame.

        Args:
            file_path (str): Path of the file associated with the thumbnail.
            thumbnail_frame (tk.Widget): The frame containing the thumbnail.
            rating (int): The rating to display (0-5).
            canvas_width (int): Width of the rating bar.
        """
        # Ensure rating is a valid integer, default to 0 if None
        if rating is None:
            rating = 0

        # Remove any existing rating bars to prevent duplication
        # for widget in thumbnail_frame.winfo_children():
            # if isinstance(widget, tk.Canvas) and widget.winfo_height() == 3:
                # widget.destroy()  # Remove old rating bars
        # Use the reset_display method to clear existing rating bars
        try:
            self.reset_display(frame=thumbnail_frame, widget_type=tk.Canvas)
        except Exception as e:
            logging.info(f"Error clearing widgets in frame for {file_path}: {e}")

        # Create a new canvas for the rating bar
        rating_canvas = tk.Canvas(thumbnail_frame, width=canvas_width, height=3, bd=0, highlightthickness=0)

        # Define the 5 colors for the rating bar
        colors = ["lightblue", "lightgreen", "yellow", "purple", "red"]

        # Define the background color (same as the thumbnail frame background)
        background_color = "#2d2d2d"  # Match labelBGColor or thumbnail background color

        # Calculate the width of each rating section
        section_width = canvas_width // 5

        # Draw only up to the current rating level
        for i in range(5):  # Loop through all 5 segments
            if i < rating:
                color = colors[i]
            else:
                color = background_color  # For non-rated sections, use background color

            rating_canvas.create_rectangle(i * section_width, 0, (i + 1) * section_width, 3, fill=color, outline="")

        # Pack the rating canvas under the thumbnail
        rating_canvas.pack(pady=(0, 5))




    def add_rating_circle(self, file_path, thumbnail_frame, rating, canvas_width):
        """
        Adds, updates, or removes a rating circle on the thumbnail frame.
        This version correctly destroys the old widget before creating a new one.
        """
        if rating is None:
            rating = 0

        # Remove any existing circle for this file (widget dict key must match grid path; also
        # clear stale keys that only differ by normalization/casing from earlier builds).
        try:
            norm_fp = self.database.normalize_path(file_path)
        except Exception:
            norm_fp = file_path
        for key in list(self.thumbnail_rating_widgets.keys()):
            try:
                if self.database.normalize_path(key) != norm_fp:
                    continue
            except Exception:
                if key != file_path:
                    continue
            old_widget = self.thumbnail_rating_widgets[key]
            if old_widget.winfo_exists():
                old_widget.destroy()
            del self.thumbnail_rating_widgets[key]

        if rating == 0:
            return

        background_color = self.thumbBGColor
        circle_canvas = tk.Canvas(thumbnail_frame, width=32, height=32, bd=0, highlightthickness=0, bg=background_color)
        
        colors = ["lightblue", "lightgreen", "yellow", "purple", "red"]
        color = colors[rating - 1]
        outlinecolor = "white"

        circle_canvas.create_oval(10, 0, 30, 20, fill=color, outline=outlinecolor, width=2)
        circle_canvas.create_text(20, 10, text=str(rating), fill="white", font=("Helvetica", 12, "bold"))

        circle_canvas.place(x=canvas_width - 38, y=6)

        self.thumbnail_rating_widgets[file_path] = circle_canvas

                
  







    def _get_folder_content_for_preview(self, folder_path, num_files=5):
        """
        Gets a list of the first N media file paths from a directory for preview purposes.
        This function is a fast, non-blocking way to get the source files.

        Args:
            folder_path (str): The path to the folder.
            num_files (int): The maximum number of file paths to return.

        Returns:
            list: A list of full paths to the media files.
        """
        valid_extensions = set(ext.lower() for ext in VIDEO_FORMATS + IMAGE_FORMATS)
        collected = []

        def _collect(path):
            if len(collected) >= num_files:
                return
            try:
                with os.scandir(path) as entries:
                    # deterministic order for stable preview output/cache
                    for entry in sorted(entries, key=lambda e: e.name.lower()):
                        if len(collected) >= num_files:
                            return
                        try:
                            if entry.is_file(follow_symlinks=False):
                                if os.path.splitext(entry.name)[1].lower() in valid_extensions:
                                    collected.append(entry.path)
                            elif entry.is_dir(follow_symlinks=False):
                                if preview_skip_subdir(entry.name):
                                    continue
                                _collect(entry.path)
                        except (OSError, PermissionError):
                            continue
            except (OSError, PermissionError):
                return

        _collect(folder_path)
        return collected


    def create_wide_folder_thumbnail(self, folder_path, folderthumbnail_size=None, num_thumbnails=5):
        """
        Generates a wide composite thumbnail for folders using global styling variables for gaps and rounded corners.
        """
        target_size = folderthumbnail_size or self.widefolder_size
        target_height = target_size[1]  # height drives layout
        gap_val = getattr(self, "wide_folder_gap", 18)
        radius_val = getattr(self, "wide_folder_innerThumbRadius", 10)

        cache_dir_path, _ = get_cache_dir_path(folder_path, self.thumbnail_cache_path)
        os.makedirs(cache_dir_path, exist_ok=True)
        
        
        
        # Inner thumb radius (included in cache key for invalidation)
        RADIUS = max(12, min(radius_val, max(10, target_height // 4)))

        wide_thumbnail_path = os.path.join(
            cache_dir_path,
            f"!folder_wide_{os.path.basename(folder_path)}_h{target_height}_g{gap_val}_r{RADIUS}.png",
        )

        if os.path.exists(wide_thumbnail_path):
            try:
                with Image.open(wide_thumbnail_path) as img:
                    img.verify() 
                return wide_thumbnail_path
            except Exception:
                pass

        source_file_paths = self._get_folder_content_for_preview(folder_path, num_thumbnails)
        if not source_file_paths:
            return None

        thumbnails = []
        thumbnail_size = (self.thumbnail_size[0] // 2, self.thumbnail_size[1] // 2)
        
        for file_path in source_file_paths:
            thumb = None
            if file_path.lower().endswith(VIDEO_FORMATS):
                thumb = create_video_thumbnail(
                    file_path, thumbnail_size, self.thumbnail_format,
                    self.capture_method_var.get(), thumbnail_time=self.thumbnail_time,
                    cache_enabled=self.cache_enabled, overwrite=False,
                    cache_dir=self.thumbnail_cache_path
                )
            elif file_path.lower().endswith(IMAGE_FORMATS):
                thumb = create_image_thumbnail(
                    file_path, thumbnail_size, cache_enabled=self.cache_enabled,
                    database=self.database, cache_dir=self.thumbnail_cache_path
                )
            
            if thumb:
                thumbnails.append(thumb)

        if not thumbnails:
            return None

        GAP = gap_val

        total_width = sum(
            int(target_height * (thumb._light_image.width / thumb._light_image.height))
            for thumb in thumbnails
        ) + (len(thumbnails) - 1) * GAP

        wide_image = Image.new('RGBA', (total_width, target_height), (0, 0, 0, 0))
        x_offset = 0

        for thumbnail in thumbnails:
            pil_img = thumbnail._light_image.convert("RGBA")
            aspect_ratio = pil_img.width / pil_img.height
            thumb_width = int(target_height * aspect_ratio)
            resized_thumb = pil_img.resize((thumb_width, target_height), Image.LANCZOS)

            # Rounded corners for each tile
            r_tile = int(max(8, min(RADIUS, thumb_width // 2, target_height // 2)))
            mask = Image.new("L", (thumb_width, target_height), 0)
            draw = ImageDraw.Draw(mask)
            draw.rounded_rectangle(
                (0, 0, thumb_width, target_height), radius=r_tile, fill=255
            )
            
            rounded_thumb = Image.new("RGBA", (thumb_width, target_height), (0, 0, 0, 0))
            rounded_thumb.paste(resized_thumb, (0, 0), mask=mask)

            wide_image.paste(rounded_thumb, (x_offset, 0))
            x_offset += thumb_width + GAP

        wide_image.save(wide_thumbnail_path, format="PNG", compress_level=1)
        return wide_thumbnail_path







    def debug_button_release(self, event, copy_mode):
        logging.info(f"ButtonRelease-2 detected at ({event.x}, {event.y}) with copy_mode={copy_mode}")
        self.drop_item(event, copy_mode=copy_mode)

        #now with support of drag and drop
    def bind_canvas_events(self, canvas, file_path, file_name, is_folder, index=None):
        
        # Explicitly mark this canvas as part of the thumbnail frame. Needed for Drag and Drop
        canvas.is_thumbnail_frame = True  # Ensure drag-drop logic can identify this as a thumbnail frame
        canvas.file_path = file_path  # Store the file path directly in the canvas object
        
        if is_folder:
            # Folder behavior: double-click opens the folder
            canvas.bind("<Double-Button-1>", lambda e, path=file_path: self.display_thumbnails(path))
            canvas.bind("<Button-3>", lambda e, path=file_path: self.show_tree_context_menu(e, self.find_node_by_path(path)))
        else:
            # File behavior: add Shift+S, Shift+K, Hover actions, and double-click to open video/image
            canvas.bind("<Button-3>", lambda e, path=file_path: self.show_thumbnail_context_menu(e, path))
            canvas.bind("<Shift-S>", lambda e, path=file_path, name=file_name: self.debug_selected_thumbnail(path))
            # canvas.bind("<Shift-K>", lambda e, path=file_path, name=file_name: self.open_keyword_window(path))
            canvas.bind("<Enter>", lambda e, path=file_path: self.show_hover_info(e, path))
            canvas.bind("<Leave>", lambda e: self.hide_hover_info())

            # if file_name.lower().endswith(VIDEO_FORMATS):
                # canvas.bind('<Double-Button-1>', lambda e, path=file_path: self.on_thumbnail_double_click(e, path))
            # else:
                # canvas.bind("<Double-Button-1>", lambda e, path=file_path, name=file_name: self.open_image_viewer(path, name))

            if file_name.lower().endswith(VIDEO_FORMATS):
                canvas.bind('<Return>', lambda e, path=file_path: self.on_thumbnail_enter_key(e, path))
            else:
                canvas.bind("<Return>", lambda e, path=file_path, name=file_name: self.open_image_viewer(path, name))


        # Add selection and context menu bindings for both wide folders and standard thumbnails
        # canvas.bind("<Button-1>", lambda e, path=file_path, lbl=canvas, idx=index: self.select_thumbnail(e, path, lbl, idx))
        # canvas.bind("<Control-Button-1>", lambda e, path=file_path, lbl=canvas, idx=index: self.select_thumbnail(e, path, lbl, idx))
        
        canvas.bind("<Button-1>", lambda e, path=file_path, lbl=canvas, idx=index: self.on_thumb_click(e, path, lbl, idx))
        canvas.bind("<Control-Button-1>", lambda e, path=file_path, lbl=canvas, idx=index: self.on_thumb_click(e, path, lbl, idx))
        canvas.bind("<ButtonPress-1>", lambda e, fp=file_path: self._dnd_mark_thumb_press(e, fp), add="+")
    
       
        canvas.bind('<ButtonRelease-1>', lambda e, path=file_path: self.on_thumbnail_click(e, path))
        # canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        # canvas.bind("<Shift-MouseWheel>", self._on_shift_mouse_wheel)

        # canvas.bind("<Delete>", lambda : self.confirm_delete_item(file_path))
        # canvas.bind("<Delete>", lambda e, path=file_path: self.confirm_delete_item(paths=[path]))

        # Drag-and-drop: middle mouse (in-app move)
        canvas.bind("<ButtonPress-2>", lambda e: self.start_drag(e, source_type="thumbnail"))
 
        # canvas.bind("<ButtonRelease-2>", lambda e: self.drop_item(e, copy_mode=False))
        canvas.bind("<ButtonRelease-2>", lambda e: [self.drop_item(e, copy_mode=False), self.end_drag(e)])
        canvas.bind("<Control-ButtonRelease-2>", lambda e: self.drop_item(e, copy_mode=True))
        canvas.bind('<B2-Motion>', self.drag_motion_tree)
        # canvas.bind("<ButtonRelease-2>", self.end_drag)

        # tkinterdnd2 — drag out to Explorer / other apps
        canvas.drag_source_register(dnd.DND_FILES)
        canvas.dnd_bind("<<DragInitCmd>>", lambda e, fp=file_path: self._dnd_thumb_drag_init(e, fp))
        canvas.dnd_bind("<<DragEndCmd>>", self._dnd_drag_end)




      
                           
    def run_thumbnail_to_grid(self, thumbnail, file_path, file_name, row, col, is_folder, index, target_frame):
        # logging.info(f"[DEBUG] run_thumbnail_to_grid: {file_name!r} @ row={row},col={col}, folder={is_folder}, frame_exists={target_frame.winfo_exists()}")
        thumb_backFill = True
        thumb_FrameSize = 16
        thumb_BorderSize = 14
        thumb_Padding = 6

        thumb_TextColor =  self.thumb_TextColor

        labelBGColor = self.labelBGColor #  "#2d2d2d" #BG color  around label text 
        y_offset = 0 # thumb vertical shift
        wideFolderColor = "gold"
        thumb_BorderThickness = self.outlinewidth
        fill_color = self.thumbBGColor if thumb_backFill else ""
        
        # Ensure target_frame still exists
        if not target_frame.winfo_exists():
            logging.info(f"Warning: target_frame for {file_path} does not exist.")
            return

        canvas_width = self.thumbnail_size[0] + thumb_BorderSize * 2
        canvas_height = self.thumbnail_size[1] + thumb_BorderSize * 2 + 10

        thumbnail_frame = None
        canvas = None
        recycled = False

        if hasattr(self, '_recycled_frames') and self._recycled_frames:
            for i in range(len(self._recycled_frames) - 1, -1, -1):
                if self._recycled_frames[i].master == target_frame:
                    thumbnail_frame = self._recycled_frames.pop(i)
                    recycled = True
                    break

        def safe_tk_color(color, fallback="#2d2d2d"):
            if isinstance(color, (tuple, list)):
                return color[1]  # dark mode color
            if not color or color == "transparent":
                return fallback
            return color

        # Same as right-panel canvas / scroll host — not labelBGColor (avoids boxed caption strip).
        caption_panel_bg = safe_tk_color(self.BackroundColor, "#101215")

        if recycled:
            thumbnail_frame.grid(row=row, column=col, padx=thumb_Padding, pady=thumb_Padding, sticky="n")
            try:
                thumbnail_frame.configure(bg=caption_panel_bg)
            except tk.TclError:
                pass

            for child in thumbnail_frame.winfo_children():
                if isinstance(child, tk.Canvas):
                    canvas = child
                    break

            if canvas is None:
                recycled = False

        if not recycled:
            safe_bg = caption_panel_bg

            thumbnail_frame = tk.Frame(target_frame, bg=safe_bg)
            thumbnail_frame.is_thumbnail_frame = True
            thumbnail_frame.grid(row=row, column=col, padx=thumb_Padding, pady=thumb_Padding, sticky="n")

            safe_canvas_bg = safe_tk_color(self.BackroundColor if thumb_backFill else safe_bg, safe_bg)

            canvas = tk.Canvas(thumbnail_frame, width=canvas_width, height=canvas_height, 
                               bg=safe_canvas_bg, bd=0, highlightthickness=0)
            canvas.pack()
            
            self.create_rounded_rectangle(canvas, thumb_BorderThickness, thumb_BorderThickness, 
                                          canvas_width - thumb_BorderThickness, canvas_height - thumb_BorderThickness, 
                                          radius=thumb_FrameSize, outline=self.thumbBorderColor, 
                                          width=thumb_BorderThickness, fill=fill_color, tags="border")
            
            canvas.create_image((canvas_width // 2, (canvas_height // 2) - y_offset), tags="thumbnail")


        # Associate the file path with the canvas object
        canvas.file_path = file_path  # Store file_path directly in the canvas object
        canvas.is_folder = is_folder  # Store whether it's a folder for later use

        resized_img = ImageOps.contain(thumbnail._light_image, self.thumbnail_size)
        thumbnail_image = ImageTk.PhotoImage(resized_img)
                    
        canvas.itemconfig("thumbnail", image=thumbnail_image)
        canvas.image = thumbnail_image
        self.image_references.append(thumbnail_image)

        self.bind_canvas_events(canvas, file_path, file_name, is_folder, index=index)
        canvas.bind("<Shift-Button-1>", lambda e, path=file_path, lbl=canvas, idx=index: self.select_range(path, idx))
        
        # Update thumbnail label and other UI elements
        self.update_thumbnail_label(file_path, file_name, thumbnail_frame, canvas, row, col, index, labelBGColor, thumb_backFill, canvas_height, canvas_width, is_folder=is_folder)
        
        # Fetch rating from the database and display it
        rating = self.database.get_rating(file_path)
        self.add_rating_circle(file_path, thumbnail_frame, rating, canvas_width)

        return canvas, row, col


                 

    def _create_empty_folder_strip(self, parent_frame, folder_name, file_path, index, row, col):
        """
        Creates an empty folder strip using grid layout for consistency.
        Matches the look of full Wide Folders but for directories without content.
        """
        wide_folder_frame = ctk.CTkFrame(
            parent_frame,
            fg_color=self.folder_color_media, 
            border_width=self.wide_folder_borderWidth,
            border_color=self.wide_folder_borderColor,
            corner_radius=self.wide_folder_cornerRadius
        )
        
        wide_folder_frame.default_border_color = self.wide_folder_borderColor
        wide_folder_frame.default_border_width = self.wide_folder_borderWidth
        
        # FIX: Using .grid instead of .pack
        wide_folder_frame.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
        parent_frame.grid_columnconfigure(col, weight=1)

        name_label = ctk.CTkLabel(
            wide_folder_frame,
            text=f"{folder_name} (empty)",
            font=self.folder_title_font,
            text_color="#dbdee1",
            anchor="w",
            height=30 
        )
        name_label.pack(side="top", fill="x", padx=15, pady=5)

        self.thumbnail_labels[file_path] = {
            "canvas": wide_folder_frame, 
            "row": row, "col": col, "index": index, 
            "label": name_label
        }

        click_handler = lambda e, p=file_path, lbl=wide_folder_frame, idx=index: self.on_thumb_click(e, p, lbl, idx)
        wide_folder_frame.bind("<Button-1>", click_handler)
        name_label.bind("<Button-1>", click_handler)
        
        self.bind_canvas_events(wide_folder_frame, file_path, folder_name, is_folder=True, index=index)

    def _wide_folder_stats_nonempty(self, stats: dict) -> bool:
        if not stats:
            return False
        return not (
            stats.get("video_count", 0) == 0
            and stats.get("image_count", 0) == 0
            and not stats.get("ratings")
            and not (stats.get("keywords") or "").strip()
            and stats.get("extra_keyword_count", 0) == 0
        )

    def _get_wide_folder_db_stats(self, folder_path: str) -> dict:
        key = self.database.normalize_path(folder_path)
        if key not in self._wide_folder_stats_cache:
            self._wide_folder_stats_cache[key] = self.database.get_folder_descendant_media_stats(
                folder_path, VIDEO_FORMATS, IMAGE_FORMATS, max_keywords=12
            )
        return self._wide_folder_stats_cache[key]

    def _attach_wide_folder_stats_panel(
        self,
        parent,
        folder_path: str,
        folder_bg_hex: str,
        click_handler,
        double_click_handler,
        stats: dict | None = None,
        *,
        left_col_px: int = 200,
    ) -> None:
        """Show DB aggregates for all catalogued files under this folder (recursive)."""
        try:
            stats = stats if stats is not None else self._get_wide_folder_db_stats(folder_path)
        except Exception as exc:
            logging.debug(f"Wide folder stats: {exc}")
            return
        if not self._wide_folder_stats_nonempty(stats):
            return

        muted = "#9aa4ad"
        kw_color = "#8ecae6"
        _lc = max(120, int(left_col_px))
        _kw_wrap = max(60, _lc - 28)

        stats_host = ctk.CTkFrame(parent, fg_color=folder_bg_hex, corner_radius=0)
        stats_host.pack(side="top", fill="none", anchor="nw", pady=(4, 0))

        def _bind_click(w):
            w.bind("<Button-1>", click_handler)
            w.bind("<Double-Button-1>", double_click_handler)
            for ch in w.winfo_children():
                try:
                    _bind_click(ch)
                except tk.TclError:
                    pass

        if stats.get("ratings"):
            rating_row = ctk.CTkFrame(stats_host, fg_color=folder_bg_hex, corner_radius=0)
            rating_row.pack(anchor="w", fill="none", pady=(0, 2))
            ctk.CTkLabel(
                rating_row,
                text="Rating:",
                font=self.wide_folder_stats_font,
                text_color=muted,
                width=54,
                anchor="w",
            ).grid(row=0, column=0, rowspan=99, sticky="nw", padx=(0, 4))
            colors = ["lightblue", "lightgreen", "yellow", "purple", "red"]
            ratings_list = [int(r) for r in stats["ratings"] if 1 <= int(r) <= 5]
            max_cols = max(1, (_lc - 58) // 28)
            for idx, ri in enumerate(ratings_list):
                rr = idx // max_cols
                cc = (idx % max_cols) + 1
                badge = tk.Canvas(
                    rating_row,
                    width=24,
                    height=17,
                    bd=0,
                    highlightthickness=0,
                    bg=folder_bg_hex,
                )
                badge.grid(row=rr, column=cc, padx=(0, 4), pady=1, sticky="nw")
                badge.create_oval(2, 1, 20, 15, fill=colors[ri - 1], outline="white", width=1)
                badge.create_text(11, 8, text=str(ri), fill="white", font=("Helvetica", 8, "bold"))

        counts_row = ctk.CTkFrame(stats_host, fg_color=folder_bg_hex, corner_radius=0)
        counts_row.pack(anchor="w", fill="none", pady=(0, 1))
        ctk.CTkLabel(
            counts_row,
            text=f"Videos: {stats['video_count']}     Images: {stats['image_count']}",
            font=self.wide_folder_stats_font,
            text_color=muted,
            anchor="w",
        ).pack(anchor="w")

        kw_body = (stats.get("keywords") or "").strip()
        extra_kw = int(stats.get("extra_keyword_count") or 0)
        if kw_body and extra_kw:
            kw_body = f"{kw_body} (+{extra_kw})"
        elif not kw_body and extra_kw:
            kw_body = f"+{extra_kw} tags"
        if kw_body:
            ctk.CTkLabel(
                stats_host,
                text=f"Keywords: {kw_body}",
                font=self.wide_folder_stats_font,
                text_color=kw_color,
                anchor="w",
                justify="left",
                wraplength=_kw_wrap,
                width=max(72, _lc - 12),
            ).pack(anchor="w", pady=(2, 0))

        _bind_click(stats_host)

    def _get_wide_folder_left_column_px(self, target_frame) -> int:
        """
        Left column width for wide folders in a row: ~25% of one grid cell
        (wide_folders_frame width / column count). Keeps separator alignment in px.
        """
        nc = max(1, getattr(self, "numwidefolders_in_col", 2))
        try:
            tw = int(target_frame.winfo_width())
        except tk.TclError:
            tw = 0
        if tw < 80:
            tw = max(640, int(self.winfo_width() or 900) - 140)
        cell_w = max(220.0, (tw - 8 * nc) / nc)
        return int(max(160, min(420, round(cell_w * 0.25))))

    def _wide_folder_resolve_gutter_px(self, target_frame) -> int:
        """
        Shared left gutter width for the current wide-folder batch.
        target_frame width changes as grid fills — cache so separator and labels stay aligned.
        """
        g = getattr(self, "_wide_folder_left_gutter_px", None)
        if g is not None and g >= 160:
            logging.debug(
                "[WIDE_GUTTER] resolve CACHE_HIT gutter=%s tf_w=%s",
                g,
                target_frame.winfo_width() if target_frame.winfo_exists() else "?",
            )
            return int(g)
        try:
            tw = int(target_frame.winfo_width())
        except tk.TclError:
            tw = 0
        tw_raw = tw
        if tw < 120:
            try:
                tw = int(target_frame.winfo_toplevel().winfo_width()) - 200
            except tk.TclError:
                tw = 680
        tw = max(tw, 400)
        nc = max(1, getattr(self, "numwidefolders_in_col", 2))
        cell_w = max(220.0, (tw - 8 * nc) / nc)
        g = int(max(160, min(420, round(cell_w * 0.25))))
        self._wide_folder_left_gutter_px = g
        logging.debug(
            "[WIDE_GUTTER] resolve NEW gutter=%s tw_raw=%s tw_used=%s nc=%s cell_w=%.1f "
            "alt_get_left_col_px=%s",
            g,
            tw_raw,
            tw,
            nc,
            cell_w,
            self._get_wide_folder_left_column_px(target_frame),
        )
        return g

    def _wide_folder_apply_left_wraplength(self, left_root, left_col_px: int) -> None:
        """Keyword wraplength from fixed left column width (px)."""
        wrap = max(72, int(left_col_px) - 24)
        try:
            if isinstance(left_root, ctk.CTkLabel):
                wl = left_root.cget("wraplength")
                try:
                    wi = int(float(wl)) if wl is not None else 0
                except (TypeError, ValueError):
                    wi = 0
                if wi > 0:
                    left_root.configure(wraplength=wrap)
        except (tk.TclError, ValueError, TypeError):
            pass
        try:
            for w in left_root.winfo_children():
                self._wide_folder_apply_left_wraplength(w, left_col_px)
        except tk.TclError:
            pass

    def _wide_folder_schedule_left_wrap(self, wide_folder_frame, left_panel) -> None:
        """After card width changes, recompute left-column wraplength (~25% gutter)."""
        job_attr = "_wide_wrap_after_id"
        prev = getattr(wide_folder_frame, job_attr, None)
        if prev is not None:
            try:
                self.after_cancel(prev)
            except Exception:
                pass

        def _apply():
            setattr(wide_folder_frame, job_attr, None)
            try:
                if not wide_folder_frame.winfo_exists() or not left_panel.winfo_exists():
                    return
                lw = getattr(self, "_wide_folder_left_gutter_px", None)
                if lw is None or lw < 40:
                    tf = getattr(wide_folder_frame, "_wide_target_frame", self.wide_folders_frame)
                    lw = self._wide_folder_resolve_gutter_px(tf)
                self._wide_folder_apply_left_wraplength(left_panel, lw)
            except tk.TclError:
                pass

        setattr(wide_folder_frame, job_attr, self.after(80, _apply))

    def _wide_folder_debug_snapshot(
        self,
        phase: str,
        file_name: str,
        row: int,
        col: int,
        target_frame,
        wide_folder_frame,
        left_holder,
        sep,
        left_px_set: int,
    ) -> None:
        """Debug alignment for left column / separator — search logs for [WIDE_GUTTER]."""
        try:
            tfw = int(target_frame.winfo_width())
            topw = int(target_frame.winfo_toplevel().winfo_width())
            wfw = int(wide_folder_frame.winfo_width())
            lhw = int(left_holder.winfo_width())
            lhr = int(left_holder.winfo_reqwidth())
            lpw = int(left_holder.winfo_rootx())
            spx = int(sep.winfo_rootx())
            wpx = int(wide_folder_frame.winfo_rootx())
            sep_rel = int(sep.winfo_x())
            alt = self._get_wide_folder_left_column_px(target_frame)
            gcached = getattr(self, "_wide_folder_left_gutter_px", None)
            logging.debug(
                "[WIDE_GUTTER] %-5s name=%r r=%s c=%s left_set=%s cached_gutter=%s alt_get_col_px=%s | "
                "tf_w=%s top_w=%s wff_w=%s lh_w=%s lh_reqw=%s sep_rel_x=%s sep_root_x=%s wff_root_x=%s delta_sep_minus_wff=%s",
                phase,
                (file_name or "")[:48],
                row,
                col,
                left_px_set,
                gcached,
                alt,
                tfw,
                topw,
                wfw,
                lhw,
                lhr,
                sep_rel,
                spx,
                wpx,
                spx - wpx,
            )
        except tk.TclError as e:
            logging.debug("[WIDE_GUTTER] %-5s name=%r TclError: %s", phase, (file_name or "")[:48], e)

    def run_thumbnail_to_grid_wide(self, thumbnail, file_path, file_name, row, col, is_folder, index, target_frame):
        """
        Renders Wide Folders with a perfect shadow wrap and edge-to-edge width.
        ASYNC: Instantly builds the UI skeleton and dispatches heavy image processing
        to the background executor, then updates the image label via .after(0, ...).
        """
        if not target_frame.winfo_exists():
            return row, col

        PERMANENT_BORDER_COLOR = self.wide_folder_borderColor
        CORNER_RADIUS = self.wide_folder_cornerRadius

        # --- FIX WIDTH: Calculate span and force grid expansion ---
        span = max(1, self.columns // self.numwidefolders_in_col)
        actual_col = col * span

        # Configure columns for this render pass.
        # minsize gives each column a startup floor (~850px total per row);
        # weight=1 lets columns expand freely when the window grows.
        # _configured_cols_count is read by clear_thumbnails() so it knows
        # exactly how many columns to reset on the next directory switch.
        _min_col_px = max(50, 850 // max(1, self.columns))
        for i in range(self.columns):
            target_frame.grid_columnconfigure(i, weight=1, minsize=_min_col_px)
        target_frame._configured_cols_count = max(
            self.columns,
            getattr(target_frame, '_configured_cols_count', 0)
        )

        SHADOW_OFFSET = 4

        if is_folder:
            has_media = self._folder_has_media_cached(file_path)

            folder_bg_color = self.folder_color_media
            try:
                _st = self._get_wide_folder_db_stats(file_path)
            except Exception:
                _st = None
            is_compact_empty = not has_media
            # Strip height = thumb band only; stats sit in a left column (no vertical crop).
            _outer_vpad = 16
            TARGET_HEIGHT = (82 if is_compact_empty else int(self.widefolder_size[1]) + _outer_vpad)

            # ── PHASE 1: Build UI skeleton immediately (no blocking) ────────────

            # Wrapper container – transparent layout helper only; use plain tk.Frame
            # to avoid CTkCanvas's rounded-rect redraw overhead (zero visual impact).
            try:
                raw_color = target_frame.cget("fg_color")
                if isinstance(raw_color, (list, tuple)):
                    import customtkinter as _ctk
                    container_bg = raw_color[0] if _ctk.get_appearance_mode() == "Light" else raw_color[1]
                elif raw_color == "transparent":
                    container_bg = target_frame.winfo_rgb(target_frame.winfo_toplevel().cget("bg"))
                    container_bg = "#{:02x}{:02x}{:02x}".format(*[v >> 8 for v in container_bg])
                else:
                    container_bg = raw_color
            except Exception:
                container_bg = "#1a1a1a"

            container = tk.Frame(target_frame, bg=container_bg,
                                 height=TARGET_HEIGHT, bd=0, highlightthickness=0)
            container.grid(row=row, column=actual_col, columnspan=span, sticky="ew", padx=2, pady=5)
            container.grid_propagate(False)
            container.grid_columnconfigure(0, weight=1)
            container.grid_rowconfigure(0, weight=1)

            # 1. SHADOW FRAME
            shadow_frame = ctk.CTkFrame(container, fg_color="#080808", corner_radius=CORNER_RADIUS)
            shadow_frame.grid(row=0, column=0, sticky="nsew",
                              padx=(SHADOW_OFFSET, 0), pady=(SHADOW_OFFSET, 0))

            # 2. MAIN FOLDER FRAME
            THINNED_BORDER_WIDTH = 1
            wide_folder_frame = ctk.CTkFrame(
                container,
                fg_color=folder_bg_color,
                border_width=THINNED_BORDER_WIDTH,
                border_color=PERMANENT_BORDER_COLOR,
                corner_radius=CORNER_RADIUS
            )
            wide_folder_frame.default_border_color = PERMANENT_BORDER_COLOR
            wide_folder_frame.default_border_width = THINNED_BORDER_WIDTH
            wide_folder_frame.grid(row=0, column=0, sticky="nsew",
                                   padx=(0, SHADOW_OFFSET), pady=(0, SHADOW_OFFSET))
            wide_folder_frame.grid_rowconfigure(0, weight=1)

            click_handler = lambda e, p=file_path, lbl=wide_folder_frame, idx=index: self.on_thumb_click(
                e, p, lbl, idx
            )
            open_folder_handler = lambda e, p=file_path: self.display_thumbnails(p)

            if is_compact_empty:
                name_label = ctk.CTkLabel(
                    wide_folder_frame,
                    text=file_name,
                    font=self.folder_title_font,
                    text_color="#7f848a",
                    anchor="w",
                    justify="left",
                    wraplength=320,
                    width=300,
                )
                name_label.pack(side="top", fill="x", padx=15, pady=(10, 0))
                # CTkFrame often leaves a large interior hit-target on its internal canvas;
                # clicks there skip the frame's bindings. Tk filler catches the empty band.
                _wide_empty_click = tk.Frame(
                    wide_folder_frame,
                    bg=folder_bg_color,
                    bd=0,
                    highlightthickness=0,
                )
                _wide_empty_click.pack(fill="both", expand=True)
                self.bind_canvas_events(
                    _wide_empty_click, file_path, file_name, is_folder=True, index=index
                )
                self.thumbnail_labels[file_path] = {
                    "canvas": wide_folder_frame,
                    "row": row, "col": actual_col, "index": index,
                    "label": name_label
                }
                wide_folder_frame.bind("<Button-1>", click_handler)
                wide_folder_frame.bind("<Double-Button-1>", open_folder_handler)
                name_label.bind("<Button-1>", click_handler)
                name_label.bind("<Double-Button-1>", open_folder_handler)
                self.bind_canvas_events(wide_folder_frame, file_path, file_name, is_folder=True, index=index)
                return container, row, actual_col

            _left_px = self._wide_folder_resolve_gutter_px(target_frame)
            wide_folder_frame._wide_target_frame = target_frame
            _PAD_L_OUTER = 10
            _PAD_SEP_TO_RIGHT = 4
            _PAD_R_OUTER = 10
            _VPAD_INNER = 8

            left_holder = tk.Frame(
                wide_folder_frame,
                bg=folder_bg_color,
                width=_left_px,
                bd=0,
                highlightthickness=0,
            )
            left_holder.grid_propagate(False)

            left_panel = ctk.CTkFrame(left_holder, fg_color=folder_bg_color, corner_radius=0)
            left_panel.pack(fill="both", expand=True, padx=(4, 6), pady=(2, 0))

            sep_color = "#4a5056"
            sep = tk.Frame(
                wide_folder_frame,
                width=1,
                bg=sep_color,
                bd=0,
                highlightthickness=0,
            )

            right_panel = ctk.CTkFrame(wide_folder_frame, fg_color=folder_bg_color, corner_radius=0)
            right_panel.grid_rowconfigure(0, weight=1)
            right_panel.grid_columnconfigure(0, weight=1)

            _wide_place_after = [None]

            def _layout_wide_place():
                try:
                    if not wide_folder_frame.winfo_exists():
                        return
                    W = int(wide_folder_frame.winfo_width())
                    H = int(wide_folder_frame.winfo_height())
                    if W < 4 or H < 4:
                        return
                    # Keep inner rects inside the parent's rounded-rect fill. Opaque child
                    # widgets were painting over CTkFrame's curved corners; selection only
                    # looked "round" because the thicker border redraws on top.
                    _cr_in = max(0, int(CORNER_RADIUS * 0.9))
                    ph = max(1, H - 2 * _VPAD_INNER - 2 * _cr_in)
                    x0 = _PAD_L_OUTER + _cr_in
                    x_sep = x0 + _left_px
                    x_right = x_sep + 1 + _PAD_SEP_TO_RIGHT
                    rw = max(80, W - x_right - _PAD_R_OUTER - _cr_in)
                    y0 = _VPAD_INNER + _cr_in
                    left_holder.place(x=x0, y=y0, width=_left_px, height=ph)
                    sep.place(x=x_sep, y=y0, width=1, height=ph)
                    right_panel.configure(width=int(rw), height=int(ph))
                    right_panel.place(x=x_right, y=y0)
                except (tk.TclError, ValueError):
                    pass

            def _schedule_wide_place():
                if _wide_place_after[0] is not None:
                    try:
                        self.after_cancel(_wide_place_after[0])
                    except Exception:
                        pass

                def _run():
                    _wide_place_after[0] = None
                    _layout_wide_place()

                _wide_place_after[0] = self.after(12, _run)

            _name_wrap = max(40, _left_px - 28)
            name_label = ctk.CTkLabel(
                left_panel,
                text=file_name,
                font=self.folder_title_font,
                text_color="#dbdee1",
                anchor="w",
                justify="left",
                wraplength=_name_wrap,
                width=max(52, _left_px - 14),
            )
            name_label.pack(side="top", fill="x", anchor="w", pady=(0, 2))

            if self._wide_folder_stats_nonempty(_st or {}):
                self._attach_wide_folder_stats_panel(
                    left_panel,
                    file_path,
                    folder_bg_color,
                    click_handler,
                    open_folder_handler,
                    stats=_st,
                    left_col_px=_left_px,
                )

            _inner_thumb_h = max(44, int(self.widefolder_size[1] * 0.72))
            target_size = (self.widefolder_size[0], _inner_thumb_h)

            image_label = ctk.CTkLabel(
                right_panel,
                text="",
                fg_color="transparent",
            )
            image_label.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

            # Register with the selection system right away
            self.thumbnail_labels[file_path] = {
                "canvas": wide_folder_frame,
                "row": row, "col": actual_col, "index": index,
                "label": name_label
            }

            def _bind_wide_strip(w):
                w.bind("<Button-1>", click_handler)
                w.bind("<Double-Button-1>", open_folder_handler)

            _bind_wide_strip(wide_folder_frame)
            _bind_wide_strip(left_holder)
            _bind_wide_strip(left_panel)
            _bind_wide_strip(right_panel)
            _bind_wide_strip(sep)
            _bind_wide_strip(name_label)
            image_label.bind("<Button-1>", click_handler)
            image_label.bind("<Double-Button-1>", open_folder_handler)
            self.bind_canvas_events(image_label, file_path, file_name, is_folder=True, index=index)

            def _on_wide_card_configure(e):
                if e.widget is not wide_folder_frame:
                    return
                _schedule_wide_place()
                self._wide_folder_schedule_left_wrap(wide_folder_frame, left_panel)

            wide_folder_frame.bind("<Configure>", _on_wide_card_configure)
            self.after_idle(_schedule_wide_place)
            self.after(
                120,
                lambda lp=left_panel, lw=_left_px: self._wide_folder_apply_left_wraplength(lp, lw),
            )

            # ── PHASE 2: Generate / fetch composite image in the background ─────

            def _bg_generate(fp=file_path, ts=target_size,
                             lbl=image_label, tf=target_frame):
                try:
                    wide_folder_thumb_path = self.create_wide_folder_thumbnail(fp, ts)
                    if not wide_folder_thumb_path:
                        return

                    def _update_ui(img_path=wide_folder_thumb_path):
                        try:
                            if lbl.winfo_exists():
                                # IMPORTANT: Tk PhotoImage must be created on Tk main thread.
                                with Image.open(img_path) as combined_image:
                                    photo = ImageTk.PhotoImage(combined_image.copy())
                                lbl.configure(image=photo)
                                lbl.image = photo
                                self.image_references.append(photo)
                        except Exception:
                            pass

                    if tf.winfo_exists():
                        tf.after(0, _update_ui)

                except Exception as exc:
                    logging.error(f"Wide thumb bg load error for {fp!r}: {exc}")

            self.executor.submit(_bg_generate)

            return container, row, actual_col
        else:
            return self.run_thumbnail_to_grid(thumbnail, file_path, file_name, row, col, is_folder, index, target_frame)




    def add_thumbnail_to_grid(self, thumbnail, file_path, file_name, row, col, is_folder, index, target_frame):
            """
            Routes the thumbnail creation to the appropriate layout function (wide or regular).
            Returns the created canvas widget and grid coordinates so it can be updated later.
            """
            if self.folder_view_mode.get() == "Wide":
                # Note: Make sure run_thumbnail_to_grid_wide also returns the canvas, row, col!
                return self.run_thumbnail_to_grid_wide(thumbnail, file_path, file_name, row, col, is_folder=is_folder, index=index, target_frame=target_frame)
            else:
                return self.run_thumbnail_to_grid(thumbnail, file_path, file_name, row, col, is_folder=is_folder, index=index, target_frame=target_frame)
            

    def skip_global_next(self):
        if self.current_video_window:
            self.current_video_window.skip_next()
        elif hasattr(self, "current_image_window") and self.current_image_window:
            self.current_image_window.show_next_image()
        else:
            logging.info("[DEBUG] No active viewer for skip_next")

    def global_play_pause(self):
        """Toggle play/pause on active video player (resume from pause when possible)."""
        active_video = getattr(self, "current_video_window", None) or getattr(self, "active_player", None)
        if active_video and hasattr(active_video, "toggle_play"):
            active_video.toggle_play()
        else:
            logging.info("[DEBUG] No active video player for play/pause")

    def skip_to_next_bookmark_global(self):
        """Jump to the next bookmark on the active video player."""
        active_video = getattr(self, "current_video_window", None) or getattr(self, "active_player", None)
        if active_video and hasattr(active_video, "skip_to_next_bookmark"):
            active_video.skip_to_next_bookmark()
        else:
            logging.info("[DEBUG] No active video player for next bookmark")

    def skip_to_previous_bookmark_global(self):
        """Jump to the previous bookmark on the active video player."""
        active_video = getattr(self, "current_video_window", None) or getattr(self, "active_player", None)
        if active_video and hasattr(active_video, "skip_to_previous_bookmark"):
            active_video.skip_to_previous_bookmark()
        else:
            logging.info("[DEBUG] No active video player for previous bookmark")

    def long_seek_forward_global(self):
        """Long seek forward on the active video player."""
        active_video = getattr(self, "current_video_window", None) or getattr(self, "active_player", None)
        if active_video and hasattr(active_video, "long_seek"):
            active_video.long_seek(direction=1)
        else:
            logging.info("[DEBUG] No active video player for long seek forward")

    def long_seek_backward_global(self):
        """Long seek backward on the active video player."""
        active_video = getattr(self, "current_video_window", None) or getattr(self, "active_player", None)
        if active_video and hasattr(active_video, "long_seek"):
            active_video.long_seek(direction=-1)
        else:
            logging.info("[DEBUG] No active video player for long seek backward")

    def skip_global_back(self):
        if self.current_video_window:
            self.current_video_window.skip_back()
        elif hasattr(self, "current_image_window") and self.current_image_window:
            self.current_image_window.show_prev_image()
        else:
            logging.info("[DEBUG] No active viewer for skip_back")




    def Open_playlist (self):
        self._demo_toast("demo_organize")
        self.playlist_manager.show_playlist()


    def add_selected_to_playlist(self, event=None, new_playlist=False):
        """
        Adds selected thumbnail files to the playlist.
        Can optionally reset the playlist first if 'new_playlist' is True.

        Args:
            event (tk.Event, optional): The event that triggered this call (e.g., mouse click). Defaults to None.
            new_playlist (bool, optional): If True, the current playlist will be cleared 
                                         before adding new items. Defaults to False.
        """
        
        # for adding to new playlist we reset array and json 
        if new_playlist:
            logging.info("Resetting playlist")
            # Clear the playlist data in the manager
            self.playlist_manager.playlist = [] 
            
            # Remove the persistent playlist file
            if os.path.exists('playlist.json'):
                try:
                    os.remove('playlist.json')
                    logging.info("Playlist file deleted successfully.")
                except Exception as e:
                    logging.info(f"Error deleting playlist file: {e}")
                    
        try:
            # Filter selected thumbnails to get only video file paths
            selected_files = [
                file_path for (file_path, _, _) in self.selected_thumbnails 
                if file_path.lower().endswith(VIDEO_FORMATS)
            ]
            
            if selected_files:
                # --- BUG FIX ---
                # The call was: self.playlist_manager.add_to_playlist(selected_files, new_playlist)
                # This sent 3 arguments (self, selected_files, new_playlist).
                # The method in PlaylistManager only expects 2 (self, selected_files).
                # The 'new_playlist' logic is already handled above, so we just pass the files.
                
                self.playlist_manager.add_to_playlist(selected_files)
                
        except Exception as e:
            # Log the specific error (which was the '3 arguments given' error)
            logging.info(f"Error adding to playlist: {e}")


    def view_catalog(self):
        # Run the database query in a separate thread to avoid freezing the UI
        threading.Thread(target=self._view_catalog_thread).start()

    def _view_catalog_thread(self):
        try:
            entries = self.database.table.all()
            logging.info("files Entries:")
            for entry in entries:
                logging.info(entry)
        except Exception as e:
            logging.info(f"Error accessing database: {e}")

    


    def zoom_thumbnail_ctrl_wheel(self, event):
        """
        Handles changing thumbnail size using Ctrl + Mouse Wheel.
        Prevents refresh if size is already at the min/max limit.
        """
        # List of allowed sizes (must match what your menu supports)
        sizes = ["160x120", "240x180", "320x240", "400x300", "480x360" ]

        # Get the current size string from the Tkinter variable
        current_size = self.thumbnail_size_option.get()
        
        try:
            # Find the index of the current size
            idx = sizes.index(current_size)
        except ValueError:
            # Fallback to default (320x240) if current size isn't in the list
            idx = 2 

        # Store the original index to check if it changes
        original_idx = idx

        # Adjust index based on scroll direction (event.delta)
        if event.delta > 0 and idx < len(sizes) - 1:
            # Zoom in (increase index), but only if not at max
            idx += 1
        elif event.delta < 0 and idx > 0:
            # Zoom out (decrease index), but only if not at min
            idx -= 1

        # --- KEY CHANGE ---
        # Only proceed if the index *actually* changed
        if idx != original_idx:
            # Get the new size string
            new_size = sizes[idx]
            
            # Update the variable and call the refresh function
            self.thumbnail_size_option.set(new_size)
            
            if self.folder_view_mode.get() == "Wide":
            
                self.change_wide_folder_size(new_size)
         
            
            self.change_thumbnail_size(new_size)
        
        # else: 
        #   If index is the same (we are at min/max limit), do nothing.
        #   This prevents the unnecessary refresh.




        

    def calculate_font_size(self): 
        base_width = self.thumbnail_size[0]
        if base_width < 240:
            return 10
        elif base_width < 400:
            return 12
        else:
            return 14






    def debug_selected_thumbnail(self):
        selected_item = self.tree.focus()
        if not selected_item:
            logging.info("No item selected")
            return

        file_path = self.tree.item(selected_item, "values")[0]
        if not file_path:
            logging.info("No valid file selected")
            return

        entry = self.database.get_entry(file_path)
        if entry:
            self.show_debug_info(entry)
        else:
            logging.info(f"No database entry found for {file_path}")
    
    def show_debug_info(self, entry):
        debug_window = tk.Toplevel(self)
        debug_window.title("Debug Info")

        debug_text = tk.Text(debug_window, width=60, height=20)
        debug_text.pack(padx=10, pady=10)

        debug_info = "\n".join(f"{key}: {value}" for key, value in entry.items())
        debug_text.insert(tk.END, debug_info)
        debug_text.config(state=tk.DISABLED)    
   
    def open_keyword_window(self, file_path):
        if not self.selected_thumbnails:
            logging.info("No thumbnails selected")
            return

        # Create keyword input window
        self.keyword_window = ctk.CTkToplevel(self)
        self.keyword_window.title("Add Keywords")
        self._center_toplevel_window(self.keyword_window, 420, 220)
        self.keyword_window.transient(self)  # proper focus return when closed
        self.keyword_window.attributes('-topmost', True)
        # Instruction label
        ctk.CTkLabel(
            self.keyword_window,
            text="Enter keywords (separated by commas):",
            anchor="w",
            font=("Segoe UI", 11)
        ).pack(pady=(12, 6), padx=20, anchor="w")

        # Entry field
        self.keyword_entry = ctk.CTkEntry(self.keyword_window, width=360)
        self.keyword_entry.pack(pady=(4, 12), padx=20)

        # Enable typing spaces (previously blocked!)
        # Removed: self.keyword_entry.bind("<space>", lambda e: "break")

        # Allow Enter to confirm
        self.keyword_entry.bind("<Return>", self.save_keywords)

        # Auto-focus (safe against rapid dialog close)
        self.keyword_window.after(
            50,
            lambda: (
                self.keyword_entry.focus_set()
                if (
                    hasattr(self, "keyword_window")
                    and self.keyword_window.winfo_exists()
                    and hasattr(self, "keyword_entry")
                    and self.keyword_entry.winfo_exists()
                )
                else None
            ),
        )

        # Save button
        save_button = ctk.CTkButton(self.keyword_window, text="Save", command=self.save_keywords)
        save_button.pack(pady=(0, 14))

    def calculate_thumbnail_time(self, file_path):
        """
        Seconds into the video for the thumbnail frame.
        Uses per-file thumbnail_timestamp in DB when set (Shift+T / timeline), else the
        Thumbnail Time slider as a fraction of duration.
        """
        try:
            if hasattr(self, "database") and self.database:
                entry = self.database.get_entry(file_path)
                if entry:
                    ts = entry.get("thumbnail_timestamp")
                    if ts is not None:
                        try:
                            abs_sec = float(ts)
                        except (TypeError, ValueError):
                            abs_sec = None
                        if abs_sec is not None and abs_sec >= 0:
                            total_duration = get_video_duration_mediainfo(file_path)
                            if total_duration and total_duration > 0:
                                return max(0.0, min(abs_sec, total_duration - 0.1))
                            return max(0.0, abs_sec)

            time_percentage = self.thumbnail_time
            total_duration = get_video_duration_mediainfo(file_path)
            if total_duration and total_duration > 0:
                actual_time = min(total_duration * time_percentage, total_duration - 0.1)
                return max(0.0, actual_time)
        except Exception as e:
            logging.info(f"[ERROR] Could not compute thumbnail time for {file_path}: {e}")

        return None




    def refresh_single_thumbnail(self, file_path, overwrite=True, at_time=None):
        # Virtual grid: old refresh path renders into regular_thumbnails_frame (hidden),
        # so the refreshed image isn't visible until the folder is reloaded.
        if getattr(self, "_vg_active", False):
            file_name = os.path.basename(file_path)
            actual_time = (
                float(at_time) if at_time is not None else self.calculate_thumbnail_time(file_path)
            )

            def _worker_vg_refresh():
                try:
                    if file_name.lower().endswith(VIDEO_FORMATS):
                        thumb = create_video_thumbnail(
                            file_path, self.thumbnail_size, self.thumbnail_format,
                            self.capture_method_var.get(), thumbnail_time=actual_time,
                            cache_enabled=self.cache_enabled, overwrite=overwrite,
                            cache_dir=self.thumbnail_cache_path, database=self.database,
                        )
                    else:
                        thumb = create_image_thumbnail(
                            file_path, self.thumbnail_size, database=self.database,
                            cache_dir=self.thumbnail_cache_path,
                        )
                    if thumb is None:
                        if file_name.lower().endswith(VIDEO_FORMATS):
                            thumb = self._create_corrupted_thumbnail_image()
                        else:
                            try:
                                img = Image.open("image_icon.png")
                                thumb = ctk.CTkImage(light_image=img, dark_image=img)
                            except Exception:
                                thumb = self._create_corrupted_thumbnail_image(
                                    "This file could not be read"
                                )
                    if thumb is not None and self.memory_cache:
                        thumbnail_cache.set(file_path, thumb, memory_cache=self.memory_cache)
                except Exception as e:
                    logging.info("Virtual grid refresh failed for %s: %s", file_path, e)
                    return

                def _apply():
                    if getattr(self, "_vg_active", False):
                        try:
                            self._vg_apply_generated_thumb(file_path)
                        except Exception:
                            pass
                        try:
                            self._vg_refresh_file_labels(file_path)
                        except Exception:
                            pass
                self.after(0, _apply)

            try:
                self.executor.submit(_worker_vg_refresh)
            except Exception:
                _worker_vg_refresh()
            return

        normalized_path = os.path.normcase(os.path.normpath(file_path))
        thumbnail_info = None

        for key in self.thumbnail_labels:
            if os.path.normcase(os.path.normpath(key)) == normalized_path:
                thumbnail_info = self.thumbnail_labels[key]
                break

        logging.info(f"***** REFRESH thumbnail_info for: {file_path} → {thumbnail_info}")

        if not thumbnail_info:
            logging.info("No thumbnail found for: %s", file_path)
            return

        row, col = thumbnail_info["row"], thumbnail_info["col"]
        file_name = os.path.basename(file_path)

        index = thumbnail_info.get("index")
        if index is None:
            index = next((idx for fp, lbl, idx in self.selected_thumbnails if fp == file_path), None)
            if index is None:
                logging.info("Index not found for %s", file_path)
                return

        actual_time = (
            float(at_time) if at_time is not None else self.calculate_thumbnail_time(file_path)
        )

        self.create_file_thumbnail(
            file_path=file_path,
            file_name=file_name,
            row=row,
            col=col,
            index=index,
            thumbnail_time=actual_time,
            overwrite=overwrite,
            target_frame=self.regular_thumbnails_frame,
            is_refresh=True
        )

        logging.info("Thumbnail refreshed for %s", file_name)

    
    
    


    def refresh_selected_thumbnails(self):
            """
            Refreshes thumbnails for all selected items.
            This function is robust and handles cases where self.selected_thumbnails
            contains either tuples (with file_path as the first element) or
            metadata dictionaries (which require a reverse lookup to find the file_path).
            """
            # Log the start of the operation.
            logging.info(f"Starting to refresh {len(self.selected_thumbnails)} selected thumbnails.")

            if not self.selected_thumbnails:
                logging.warning("No thumbnails selected to refresh.")
                return

            # Iterate through each item in the selection list.
            for selected_item in self.selected_thumbnails:
                file_path = None  # Reset file_path for each item in the loop.

                # --- Determine the file_path based on the item type ---

                # Case 1: The item is a tuple or list, e.g., ('/path/to/file.mp4', label, index)
                # We assume the file_path is the first element.
                if isinstance(selected_item, (list, tuple)):
                    if selected_item:  # Ensure the tuple/list is not empty.
                        file_path = selected_item[0]
                    else:
                        logging.error("Skipping an empty list/tuple item in selection.")
                        continue  # Move to the next item.

                # Case 2: The item is a metadata dictionary, e.g., {'row': 0, 'col': 0, ...}
                # We need to find the corresponding file path by searching self.thumbnail_labels.
                elif isinstance(selected_item, dict):
                    found = False
                    # Iterate through the main thumbnail dictionary which maps paths to metadata.
                    for path, info_dict in self.thumbnail_labels.items():
                        # Compare unique identifiers. A full dictionary comparison might fail
                        # due to different widget object instances.
                        if (info_dict.get('index') == selected_item.get('index') and
                            info_dict.get('row') == selected_item.get('row') and
                            info_dict.get('col') == selected_item.get('col')):
                            file_path = path  # We found the corresponding path!
                            found = True
                            break  # Exit the inner loop once found.
                    
                    if not found:
                        logging.error(f"Could not find a file path for selected thumbnail metadata: {selected_item}")
                        continue # Move to the next item.

                # Case 3: The item is of an unrecognized type.
                else:
                    logging.error(f"Unrecognized item type in selection: {type(selected_item)} - {selected_item}")
                    continue # Move to the next item.

                # --- If we have a file_path, perform the refresh ---
                
                if file_path:
                    try:
                        # Ensure the path is a string before proceeding.
                        if not isinstance(file_path, str):
                            logging.error(f"Resolved path is not a string: {file_path}. Skipping.")
                            continue
                        
                        self.refresh_single_thumbnail(file_path, overwrite=True)
                    except Exception as e:
                        # Catch any errors during the actual refresh of a single thumbnail.
                        logging.error(f"An error occurred while refreshing thumbnail for path {file_path}: {e}")
                else:
                    # This log is a fallback, but the 'continue' statements should prevent this.
                    logging.warning(f"Could not determine file path for selected item: {selected_item}")

            logging.info("Finished refreshing all selected thumbnails.")


    def save_keywords(self, event=None):
        keywords = self.keyword_entry.get()
        cleaned_keywords = [kw.strip() for kw in keywords.split(",") if kw.strip()]

        formatted_thumbnails = [(file_path, str(label), index) for file_path, label, index in self.selected_thumbnails]
        logging.info(f"Selected thumbnails: {formatted_thumbnails}")  # Debug
        logging.info(f"Keywords to be assigned: {cleaned_keywords}")  # Debug

        for file_path, label, index in self.selected_thumbnails:
            if os.path.isdir(file_path):
                logging.info(f"Skipping directory: {file_path}")
                continue

            try:
                existing_keywords_raw = self.database.get_keywords(file_path)
                if (
                    not existing_keywords_raw
                    or existing_keywords_raw == "No keywords"
                ):
                    existing_keywords = []
                else:
                    existing_keywords = [
                        kw.strip()
                        for kw in existing_keywords_raw.split(",")
                        if kw.strip()
                    ]

                combined_keywords = sorted(set(existing_keywords + cleaned_keywords))
                final_keywords = ", ".join(combined_keywords)

                self.database.update_keywords(file_path, final_keywords)
                # overwrite=False: preserves any custom thumbnail set via Shift+T
                self.refresh_single_thumbnail(file_path, overwrite=False)

            except Exception as e:
                logging.info(f"Error updating keywords for {file_path}: {e}")

        self.keyword_window.destroy()
        # If a video player window is active, keep it in front; otherwise focus main app.
        self.after(220, self._focus_video_window_after_dialog)





    def on_thumb_click(self, event, file_path, label, index):
        if time.monotonic() < getattr(self, "_ignore_pointer_navigation_until", 0.0):
            return
        shift = (event.state & 0x0001) != 0
        ctrl  = (event.state & 0x0004) != 0
        logging.debug("[Thumb] on_thumb_click idx=%s path=%s", index, os.path.basename(file_path) if file_path else "?")

        # Multi-select (e.g. Ctrl+A): do not collapse to single-select when starting drag on a selected item
        if not shift and not ctrl and len(self.selected_thumbnails) > 1:
            selected_indices = {i for _, _, i in self.selected_thumbnails}
            if index in selected_indices:
                self.selected_thumbnail_index = index
                self.selected_file_path = file_path
                return

        self.select_thumbnail(index, shift=shift, ctrl=ctrl, trigger_preview=False, click_widget=label)
        if file_path and os.path.isdir(file_path):
            self.current_directory = file_path
            self._schedule_tree_sync_for_current_dir()



    def _schedule_tree_sync_for_current_dir(self, delay_ms: int = 10):
        """Debounce folder -> tree synchronization."""
        try:
            if self._tree_sync_after_id:
                self.after_cancel(self._tree_sync_after_id)
        except Exception:
            pass
        self._tree_sync_after_id = self.after(delay_ms, self.select_current_folder_in_tree)


    def _thumbnail_label_info_for_path(self, file_path: str, idx: int | None = None):
        """
        Look up thumbnail_labels entry for path (handles normpath/casing mismatches).
        """
        if not file_path:
            return None
        if file_path in self.thumbnail_labels:
            return self.thumbnail_labels[file_path]
        norm = os.path.normcase(os.path.normpath(file_path))
        for k, v in self.thumbnail_labels.items():
            try:
                if os.path.normcase(os.path.normpath(k)) == norm:
                    return v
            except Exception:
                continue
        if idx is not None:
            want_name = os.path.basename(file_path)
            for k, v in self.thumbnail_labels.items():
                if not isinstance(v, dict):
                    continue
                if v.get("index") != idx:
                    continue
                if os.path.basename(k) == want_name:
                    return v
        return None

    def select_thumbnail(self, idx, shift=False, ctrl=False, trigger_preview=True, click_widget=None):
            """
            Handles the LOGIC of selection. It only manages the self.selected_thumbnails list.
            The visual update is delegated to update_thumbnail_selection().
            
            --- THIS IS THE CORRECTED VERSION ---
            This function now works EXCLUSIVELY with 3-element tuples (path, label, index)
            to maintain data consistency across the entire application.
            """
            if idx < 0 or idx >= len(self.video_files):
                # Index is out of bounds
                return

            file_path = self.video_files[idx]['path']

            label_info = self._thumbnail_label_info_for_path(file_path, idx)
            if not label_info and click_widget is not None:
                for _k, v in self.thumbnail_labels.items():
                    if isinstance(v, dict) and v.get("canvas") is click_widget:
                        label_info = v
                        break
            if not label_info:
                logging.info(
                    "[Thumb] select_thumbnail SKIP: no label for path=%s (idx=%s)",
                    file_path, idx,
                )
                return

            thumb_tuple = (file_path, label_info, idx)

            # --- SHIFT-SELECT LOGIC (Rewritten to use tuples) ---
            if shift and self.selected_thumbnail_index is not None:
                anchor = self.selected_thumbnail_index
                start, end = sorted([anchor, idx])
                
                new_selection = []
                for i in range(start, end + 1):
                    path = self.video_files[i]['path']
                    lbl_widget = self._thumbnail_label_info_for_path(path, i)
                    if lbl_widget:
                        # Create the standard tuple and add it
                        new_tuple = (path, lbl_widget, i) 
                        new_selection.append(new_tuple)
                self.selected_thumbnails = new_selection

            # --- CTRL-SELECT LOGIC (Rewritten to use tuples) ---
            elif ctrl:
                # OLD/BAD: found = [info for info in self.selected_thumbnails if info.get("index") == idx]
                # NEW/GOOD: We check the 3rd element (at index 2) of each tuple
                found = [info for info in self.selected_thumbnails if info[2] == idx]
                
                if found:
                    # Remove the tuple if it's already selected
                    self.selected_thumbnails.remove(found[0])
                else:
                    # OLD/BAD: self.selected_thumbnails.append(thumb_info)
                    # NEW/GOOD: Append our standard tuple
                    self.selected_thumbnails.append(thumb_tuple)
            
            # --- SINGLE-SELECT LOGIC (Rewritten to use tuples) ---
            else:
                # OLD/BAD: self.selected_thumbnails = [thumb_info]
                # NEW/GOOD: Set the list to contain only our standard tuple
                self.selected_thumbnails = [thumb_tuple]
                
                if trigger_preview:
                    self._handle_thumbnail_single_click(file_path)

            # --- Update state for the next selection (This part was fine) ---
            self.selected_thumbnail_index = idx
            self.selected_file_path = file_path

            # --- DELEGATE VISUAL UPDATE (This part was fine) ---
            self.update_thumbnail_selection()
            
            # --- Update other UI parts (This part was fine) ---
            self.update_panel_info(file_path)







    def save_tree_state(self, event=None):
        tree_state = []
        logging.info("[DEBUG] Will save tree state")
        for item in self.tree.get_children(''):
            self.save_tree_state_recursive(item, tree_state)
        with open('tree_state.json', 'w') as f:
            json.dump(tree_state, f)

    def save_tree_state_recursive(self, item, tree_state):
        if self.tree.item(item, 'open'):
            tree_state.append(self.tree.item(item, 'values')[0])  # Save the path of the expanded item
            for child in self.tree.get_children(item):
                self.save_tree_state_recursive(child, tree_state)


    def expand_tree_to_path(self, path, select_final_node=True):
        """
        Expands the tree to a specific path.
        Can now optionally skip the final selection to prevent triggering a load.
        (THIS IS THE COMPLETE, CORRECT VERSION)
        """
        try:
            if path.startswith("virtual_library://"):
                for item_id in self.tree.get_children(''):
                    item_values = self.tree.item(item_id, 'values')
                    if item_values and item_values[0] == path:
                        self.tree.item(item_id, open=True)
                        
                        if select_final_node:
                            self.tree.selection_set(item_id)
                            self.tree.focus(item_id)
                            self.tree.see(item_id)
                        return

                logging.warning(f"Could not find virtual library to expand: {path}")
                return

            normalized_path = os.path.normpath(path)
            drive, tail = os.path.splitdrive(normalized_path)
            if not drive:
                logging.warning(f"Invalid path format for expansion (no drive found): {path}")
                return
                
            path_parts = [drive + os.sep] + [part for part in tail.strip(os.path.sep).split(os.path.sep) if part]
            current_item_id = ""  

            for i, _ in enumerate(path_parts):
                current_path_to_find = os.path.join(*path_parts[:i+1])
                found_next_item = False
                
                for child_id in self.tree.get_children(current_item_id):
                    child_values = self.tree.item(child_id, 'values')
                    
                    if child_values and os.path.normcase(child_values[0]) == os.path.normcase(current_path_to_find):
                        self.process_directory(child_id, current_path_to_find)
                        self.tree.item(child_id, open=True)
                        current_item_id = child_id
                        found_next_item = True
                        break

                if not found_next_item:
                    logging.warning(f"Could not expand to path '{path}'. Part '{current_path_to_find}' not found.")
                    return

            if current_item_id and select_final_node:
                self.tree.selection_set(current_item_id)
                self.tree.focus(current_item_id)
                self.tree.see(current_item_id)
                
        except Exception as e:
            logging.error(f"Failed to expand tree to path '{path}': {e}", exc_info=True)



    # --- ADD THIS NEW FUNCTION ---
    # Function to get the most recent directory from the correct JSON file
    def get_last_recent_directory(self):
        """
        Loads the list of recent directories and returns the most recent one.
        """
        try:
            with open('recent_directories.json', 'r') as f:
                data = json.load(f)
            # The most recent path is the FIRST one in the list
            recent_list = data.get("recent_directories", [])
            if recent_list:
                # Instead of [0] for the first item, use [-1] for the LAST item
                return recent_list[-1]
        except FileNotFoundError:
            logging.info("No recent_directories.json file found.")
        except Exception as e:
            logging.error(f"Failed to get last recent directory: {e}")
        return None # Return None if anything fails


    def restore_tree_state(self):
        """Restores the tree's expanded state from tree_state.json WITHOUT triggering loads."""
        try:
            with open('tree_state.json', 'r') as f:
                tree_state = json.load(f)
            for path in tree_state:
                self.expand_tree_to_path(path, select_final_node=False) 
        except FileNotFoundError:
            logging.info("No tree_state.json found. Starting with a fresh tree.")
        except Exception as e:
            logging.error(f"Failed to restore tree state: {e}")











    def is_wide_folder(self, file_path):
        return file_path in self.wide_folders


    def select_range(self, file_path, end_index):
        """
        Selects all items between the last selected item (anchor) and the
        newly shift-clicked item.
        
        --- THIS IS THE CORRECTED VERSION ---
        This function now works EXCLUSIVELY with 3-element tuples (path, label, index)
        to maintain data consistency.
        """
        # Get the starting point of the selection (the "anchor")
        start_index = self.selected_thumbnail_index

        # Safety check: if there's no anchor, just select the clicked item
        if start_index is None:
            # 'select_thumbnail' is already fixed and works with tuples, so this is safe.
            self.select_thumbnail(end_index)
            return

        # Determine the actual start and end of the range
        start, end = sorted([start_index, end_index])
        
        # Create a new list for the selection
        new_selection = []
        
        # Iterate through all items within the range
        for i in range(start, end + 1):
            # Get the path from our main file list
            path = self.video_files[i]['path']
            
            # Get the corresponding label widget from our thumbnail registry
            # We assume 'self.thumbnail_labels.get(path)' returns the WIDGET,
            # just like we established in 'select_thumbnail'
            label_widget = self.thumbnail_labels.get(path)
            
            # If the widget exists, create our standard tuple
            if label_widget:
                # --- THIS IS THE FIX ---
                # Instead of appending 'info', we build the standard 3-element tuple
                new_tuple = (path, label_widget, i)
                new_selection.append(new_tuple)
                
            # else: if label_widget is None (e.g., file exists but no widget), we skip it

        # Replace the old selection with the new range
        # 'self.selected_thumbnails' now contains ONLY our standard 3-element tuples
        self.selected_thumbnails = new_selection
        
        # Delegate the visual update
        # This will now work, because 'update_thumbnail_selection'
        # expects tuples and reads them with 'info[2]', which is correct.
        self.update_thumbnail_selection()








    def _apply_selection_border(self, widget, is_selected):
        """Apply or clear selection border on one thumbnail widget."""
        if not widget or not widget.winfo_exists():
            return
        if isinstance(widget, ctk.CTkFrame):
            if is_selected:
                widget.configure(border_width=2, border_color=self.thumbSelColor)
            else:
                def_width = getattr(widget, "default_border_width", 0)
                def_color = getattr(widget, "default_border_color", None)
                if def_color == "transparent" or def_color is None:
                    widget.configure(border_width=0)
                else:
                    widget.configure(border_width=def_width, border_color=def_color)
        elif isinstance(widget, tk.Canvas):
            border_items = widget.find_withtag("border")
            if border_items:
                if is_selected:
                    widget.itemconfig(border_items[0], outline=self.thumbSelColor, width=self.Select_outlinewidth)
                else:
                    widget.itemconfig(border_items[0], outline=self.thumbBorderColor, width=self.outlinewidth)
        elif isinstance(widget, tk.Frame):
            slot = getattr(widget, "_vg_slot", None)
            if slot and slot.get("strip_canvas"):
                try:
                    self._vg_redraw_wide_card(slot)
                except Exception:
                    pass
                return
            if not hasattr(widget, "default_border_color"):
                return
            wsel = int(getattr(self, "Select_outlinewidth", 2) or 2)
            if is_selected:
                widget.configure(
                    highlightthickness=max(wsel, 2),
                    highlightbackground=self.thumbSelColor,
                    highlightcolor=self.thumbSelColor,
                )
            else:
                try:
                    self._vg_sync_wide_strip_border(widget)
                except Exception:
                    try:
                        widget.configure(highlightthickness=0)
                    except Exception:
                        pass

    def update_thumbnail_selection(self):
        """
        Visual selection: update thumbnails whose selected state changed vs _prev_selected_indices.
        Single pass avoids missing deselects (e.g. after Ctrl+A left _prev empty while all showed selected).
        """
        new_indices: set = set()
        for item in self.selected_thumbnails:
            if isinstance(item, (list, tuple)) and len(item) > 2:
                new_indices.add(item[2])
            elif isinstance(item, dict):
                idx = item.get("index")
                if idx is not None:
                    new_indices.add(idx)

        prev_indices = getattr(self, "_prev_selected_indices", set())

        for _fp, info in self.thumbnail_labels.items():
            idx = info.get("index")
            if idx is None:
                continue
            canvas = info.get("canvas")
            if not canvas or not canvas.winfo_exists():
                continue
            is_sel = idx in new_indices
            was_sel = idx in prev_indices
            if is_sel != was_sel:
                self._apply_selection_border(canvas, is_sel)

        self._prev_selected_indices = set(new_indices)
        self.update_status_bar()





     
    def seek_video(self, seek_time):
        if self.current_video_window is not None:
            self.current_video_window.seek_to_time(seek_time)
        elif self.info_panel and self.info_panel.preview_player is not None:
            self.info_panel.preview_player.seek_to_time(seek_time)
        else:
            logging.debug("seek_video: no active player")

     
    def set_loop_start_shortcut(self, event=None):
        logging.info("[DEBUG] Shortcut: Shift+S pressed → Set LOOP START")
        if hasattr(self, "current_video_window") and self.current_video_window:
            self.current_video_window.set_loop_start()
        else:
            logging.info("[DEBUG] current_video_window not available")

    def set_loop_end_shortcut(self, event=None):
        logging.info("[DEBUG] Shortcut: Shift+E pressed → Set LOOP END")
        if hasattr(self, "current_video_window") and self.current_video_window:
            self.current_video_window.set_loop_end()
        else:
            logging.info("[DEBUG] current_video_window not available")

    def toggle_loop_shortcut(self, event=None):
        logging.info("[DEBUG] Shortcut: Shift+L pressed → Toggle LOOP")
        if hasattr(self, "current_video_window") and self.current_video_window:
            self.current_video_window.toggle_loop()

    def speed_up_shortcut(self, event=None):
        logging.info("[DEBUG] Shortcut: Shift+Right pressed → Speed UP")
        if hasattr(self, "current_video_window") and self.current_video_window:
            self.current_video_window.speed_step(+1)

    def speed_down_shortcut(self, event=None):
        logging.info("[DEBUG] Shortcut: Shift+Left pressed → Speed DOWN")
        if hasattr(self, "current_video_window") and self.current_video_window:
            self.current_video_window.speed_step(-1)



    def open_video_player(self, video_path, video_name):
        can_play, reason = self._can_attempt_video_playback(video_path, for_preview=False)
        if not can_play:
            logging.warning("[Playback Blocked] %s | path=%s", reason, video_path)
            if hasattr(self, "status_bar") and self.status_bar:
                self.status_bar.set_action_message(reason, color="#ff6b6b")
                self.after(4500, self.status_bar.clear_action_message)
            return
        
        if hasattr(self, "info_panel") and hasattr(self.info_panel, "preview_player"):
            try:
                pp = self.info_panel.preview_player
                if pp and getattr(pp, "playing", False):
                    logging.info("[DEBUG] Preview is playing, will stop it before opening main player.")
                    pp.stop_video()
                else:
                    logging.info("[DEBUG] Preview is not playing, nothing to stop.")
            except Exception as e:
                logging.warning("Could not stop preview player: %s", e)

            
        if self.current_video_window is not None:
            self.close_video_player()

        logging.info(f"Opening video player for {video_name} with path {video_path}")  # Debug

        self.current_video_index = next(
            (index for (index, d) in enumerate(self.video_files) if d["path"] == video_path),
            None
        )

        self.current_video_window = VideoPlayer(
            parent=self,
            controller=self,
            video_path=video_path,
            video_name=video_name,
            initial_volume=self.current_volume,
            vlc_video_output=self.video_output_var.get(),
            vlc_audio_output=self.audio_output_var.get(),
            vlc_hw_decoding=self.hardware_decoding_var.get(),
            vlc_audio_device=self.audio_device_var.get(),
            auto_play=self.auto_play,
            subtitles_enabled=self.subtitles_enabled,
            use_gpu_upscale=getattr(self, "gpu_upscale", False)
            # playlist_manager=self.playlist_manager
        )
        self._demo_toast("demo_playback")
        # Start playback in next Tk tick so window paints first (reduces perceived freezes).
        self.after(1, lambda: self.current_video_window and self.current_video_window.show_and_play())
        
        if self.ShowTWidget and hasattr(self, "timeline_widget"):
            self.current_video_window.timeline_widget = self.timeline_widget
        self.active_player = self.current_video_window
        
        if self.ShowTWidget and hasattr(self, "timeline_widget"):
  
            self.timeline_widget.reload_all_markers_and_redraw(video_path)

        logging.info("[DEBUG] current_video_window created: %s", self.current_video_window)

        # Ensure the fullscreen state is consistent
        if self.is_fullscreen:
            logging.info("Applying fullscreen state to the new video window.")  # Debug
            self.current_video_window.toggle_fullscreen()
        



    def update_current_volume(self, volume):
            self.current_volume = volume

    
 


    # modified with separate index for wide thumb
    def refresh_thumbnail(self, file_path, thumbnail_time=None):
        thumbnail_label = self.thumbnail_labels.get(file_path)
        logging.info(f"thumbnail_label: '{thumbnail_label}' from: {file_path}")

        # Clear out the old thumbnail label if it exists
        for path, info in self.thumbnail_labels.items():
            if path == file_path:
                try:
                    canvas = info["canvas"]
                    thumbnail_frame = canvas.master
                    if thumbnail_frame.winfo_exists():  # Check if the widget exists
                        thumbnail_frame.grid_forget()
                    else:
                        logging.info(f"Widget {thumbnail_frame} does not exist.")
                except Exception as e:
                    logging.info(f"Error hiding old thumbnail: {e}")

                # Remove the entry from `thumbnail_labels`
                del self.thumbnail_labels[file_path]
                break

        # Separate index tracking for wide folders and regular thumbnails
        wide_folder_index = 0
        thumbnail_index = 0

        # Loop over all video files to refresh
        for file in self.video_files:
            if file['is_folder'] and self.folder_contains_media(file['path']):  # Wide folder logic
                row, col = divmod(wide_folder_index, self.numwidefolders_in_col)
                self.create_file_thumbnail(file['path'], file['name'], row, col, wide_folder_index, thumbnail_time=thumbnail_time, overwrite=True, target_frame=self.wide_folders_frame)
                wide_folder_index += 1
            else:  # Regular thumbnail logic
                row, col = divmod(thumbnail_index, self.columns)
                fp_norm = os.path.normcase(os.path.normpath(file.get("path", "")))
                target_norm = os.path.normcase(os.path.normpath(file_path))
                if fp_norm == target_norm:
                    tt = (
                        thumbnail_time
                        if thumbnail_time is not None
                        else self.calculate_thumbnail_time(file["path"])
                    )
                    self.create_file_thumbnail(
                        file["path"],
                        file["name"],
                        row,
                        col,
                        thumbnail_index,
                        thumbnail_time=tt,
                        overwrite=True,
                        target_frame=self.regular_thumbnails_frame,
                    )
                thumbnail_index += 1







    def close_video_player(self):
        if self.current_video_window is not None:
            self.current_video_window.close_video_player()
            self.current_video_window = None
            logging.info("[DEBUG] current_video_window closed/set to None")

        if hasattr(self, "info_panel") and hasattr(self.info_panel, "preview_player"):
            try:
                DUMMY_VIDEO_PATH = "assets/black_placeholder.mp4"
                self.info_panel.preview_player.video_path = DUMMY_VIDEO_PATH
                self.info_panel.preview_player.play_video()
                logging.debug("InfoPanel dummy preview restarted after main player closed")
            except Exception as e:
                logging.warning("Could not restart dummy preview: %s", e)


    
  
            
    def open_search_window(self):
        """
        Opens the search window. If it's already open, brings it to the front.
        Uses the factory function from gui_elements.
        """
        # If the window exists and is not destroyed, just focus it
        if self.search_window is not None and self.search_window.winfo_exists():
            self.search_window.lift()
            self.search_window.focus_force()
            return

        self._demo_toast("demo_search")
        # Create and store the new window object
        self.search_window = create_search_window(self)    
            
 

    def search_database(self, search_param, keyword, and_or, operator=None):
            """
            Executes a search query and formats results for the progressive renderer.
            Ensures that results are split into folders and files to prevent UI crashes.
            """
            
            # Check the state of the checkbox at the beginning of the search
            if self.clear_search_var.get():
                # If checked, clear the UI
                self.clear_thumbnails()
                # And also clear our internal list of results
                self.current_search_results = []

            # Perform the database search to get NEW results
            new_results = self.database.search_entries(search_param, keyword, and_or, operator)
            new_results = list(new_results)
            logging.info(f"Search found {len(new_results)} new results.")

            if not new_results:
                logging.info("No new search results to display.")
                return

            # Convert the new database results into the format expected by the renderer
            new_files_to_render = [{'path': r['file_path'], 'name': r['filename']} for r in new_results]
            
            # Add the new results to our persistent list
            self.current_search_results.extend(new_files_to_render)
            
            # If we didn't clear the thumbnails at the start, we need to do it now
            if not self.clear_search_var.get():
                self.clear_thumbnails()

            # --- FIX: Format data for _start_progressive_render ---
            # The renderer expects a dictionary with 'folders' and 'files' keys
            formatted_data = {
                'folders': [f for f in self.current_search_results if os.path.isdir(f['path'])],
                'files': [f for f in self.current_search_results if not os.path.isdir(f['path'])]
            }

            # Call the progressive render function with the formatted dictionary
            self._start_progressive_render(formatted_data, force_refresh=True)


            
        

    #new optimized function    
        
    def display_search_results(self, results):
        """
        Display search results in the thumbnail grid.
        Args:
            results (list): List of database query results containing file information.
        """

        try:
            # Clear the existing thumbnails
            self.clear_thumbnails()

            row, col = 0, 0
            for index, result in enumerate(results):
                file_path = result['file_path']
                file_name = result['filename']

                # Use create_file_thumbnail to generate or retrieve thumbnails
                self.create_file_thumbnail(
                    file_path=file_path,
                    file_name=file_name,
                    row=row,
                    col=col,
                    index=index,
                    overwrite=False,  # Ensure we respect caching
                    target_frame=self.scrollable_frame
                )

                # Adjust grid position for the next thumbnail
                col += 1
                if col >= self.columns:
                    col = 0
                    row += 1

        except Exception as e:
            logging.info(f"Error displaying search results: {e}")
        
        



        


    def clear_thumbnails(self):
        """
        Clears all thumbnail widgets and associated data.
        Destroys child widgets and un-packs the main container frames.
        """
        for job in self.after_jobs:
            self.after_cancel(job)
        self.after_jobs.clear()
        # Flush queued thumbnail jobs from a previous folder switch.
        try:
            while not self.thumb_queue.empty():
                self.thumb_queue.get_nowait()
        except Exception:
            pass

        for frame in (self.regular_thumbnails_frame, self.wide_folders_frame):
            if frame: 
                for child in frame.winfo_children():
                    child.destroy()

        # Reset grid column configuration on wide_folders_frame so that stale
        # weight=1 columns from a previous directory (which may have had a
        # different self.columns count) don't silently consume horizontal space
        # in the next directory's render.
        if hasattr(self, 'wide_folders_frame') and self.wide_folders_frame.winfo_exists():
            _prev_cols = getattr(self.wide_folders_frame, '_configured_cols_count', 0)
            for i in range(max(_prev_cols, 30)):
                self.wide_folders_frame.grid_columnconfigure(i, weight=0, minsize=0)
            self.wide_folders_frame._configured_cols_count = 0

        if hasattr(self, 'filler'):
            self.filler.pack_forget()
        if hasattr(self, 'wide_folders_frame'):
            self.wide_folders_frame.pack_forget()
        if hasattr(self, 'regular_thumbnails_frame'):
            self.regular_thumbnails_frame.pack_forget()

        # Full virtual-grid teardown: moving slots off-screen left their canvas
        # create_window items in bbox("all"), so scrollregion covered y≈-10000 and
        # yview(0) showed empty space — search / legacy grid looked blank after VG.
        if getattr(self, "_vg_active", False) or (
            getattr(self, "_vg_std_pool", None) and len(self._vg_std_pool) > 0
        ) or (getattr(self, "_vg_wide_pool", None) and len(self._vg_wide_pool) > 0):
            try:
                self.deactivate_virtual_grid()
            except Exception:
                logging.debug("deactivate_virtual_grid during clear failed", exc_info=True)

        self.thumbnail_labels.clear()
        self.video_files = []
        self.selected_thumbnails = [] 
        self.selected_thumbnail_index = None
        self._prev_selected_indices = set()
        self._wide_folder_stats_cache.clear()
        self._folder_media_presence_cache.clear()
        self._wide_folder_left_gutter_px = None
        logging.debug("[WIDE_GUTTER] gutter cache cleared (clear_thumbnails)")

        self.canvas.yview_moveto(0)    
