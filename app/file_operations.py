"""
Thumbnail generation, cache paths, duration lookup, and folder previews for Vibe Player.

Implements video/image/folder thumbnails, ffprobe/OpenCV helpers, and ``FileOperations``.
"""

import os
import sys
import json
from PIL import Image, ImageTk, ImageOps, ImageDraw, UnidentifiedImageError
from PIL import ImageColor
import time
import customtkinter as ctk
from database import Database
from utils import get_video_size
from utils import ThumbnailCache
import subprocess
from io import BytesIO
import shutil
import logging
import datetime


# --- Add these env vars to suppress OpenCV/FFmpeg noise ---
os.environ["OPENCV_LOG_LEVEL"] = "OFF"
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "loglevel;quiet"


startupinfo = None
if os.name == 'nt':  # Use this logic only on Windows
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE


thumbnail_cache = ThumbnailCache()

_duration_cache = {}





class FileOperations:
    """Handles file-related operations: thumbnails (image/video/folder), cache paths, and metadata."""

    def __init__(self, parent,video_formats, image_formats):
        self.parent = parent
        
        self.VIDEO_FORMATS = video_formats # <-- Store video formats
        self.IMAGE_FORMATS = image_formats # <-- Store image formats
        self._folder_icon_cache = {}





    def _get_cached_folder_base_icon(self, icon_name, size):
        """
        Loads the base folder icon from the /icons subfolder using the parent's path.
        """
        # 1. Check in-memory cache
        key = (icon_name, size)
        if not hasattr(self, '_folder_icon_cache'):
            self._folder_icon_cache = {}
            
        if key in self._folder_icon_cache:
            return self._folder_icon_cache[key]
        
        # 2. Build path via parent object (main app)
        # BUG FIX: Must use self.parent.default_directory
        icon_path = os.path.join(self.parent.default_directory, "icons", icon_name)
        
        # 3. Load the image
        if os.path.exists(icon_path):
            try:
                img = Image.open(icon_path).convert("RGBA")
                img = img.resize(size, Image.LANCZOS)
                self._folder_icon_cache[key] = img
                return img
            except Exception as e:
                logging.error(f"Error opening icon {icon_path}: {e}")
        
        # 4. Fallback (when file doesn't exist, create yellow placeholder in memory)
        logging.warning(f"Icon {icon_name} not found at {icon_path}. Using yellow fallback.")
        fallback = Image.new("RGBA", size, (255, 255, 0, 255))
        self._folder_icon_cache[key] = fallback
        return fallback

    def create_folder_preview_thumbnail(self, folder_path, thumbnail_size, cache_enabled=True, cache_dir=None, database=None):
        """
        Generates a 2x2 preview grid for a folder icon.
        This modern version uses the fast helper function to get file paths and
        relies on the cache to generate individual thumbnails quickly.
        """
        try:
            
            
            # --- FIX: Ensure we use the absolute cache path from parent if not provided ---
            if cache_dir is None or cache_dir == "thumbnail_cache":
                # Try to get path from main app to avoid relative path issues
                cache_dir = getattr(self.parent, "thumbnail_cache_path", "thumbnail_cache")
            # --- MAJOR CHANGE PART 1: Get file paths, not ready-made thumbnails ---
            # Use the fast helper function to get a list of up to 4 media file paths.
            # This replaces the call to the old, slow 'create_thumbnails_for_wide_folder'.
            source_file_paths = self.parent._get_folder_content_for_preview(
                folder_path=folder_path,
                num_files=4
            )

            if not source_file_paths:
                logging.debug("No media files found to create preview grid for: %s", folder_path)
                return None

            # --- MAJOR CHANGE PART 2: Generate thumbnails from paths ---
            # This will be very fast as it will almost always hit the cache.
            thumbnails = []
            # We need small thumbnails for the grid, so we define a smaller size.
            small_thumb_size = (thumbnail_size[0] // 2, thumbnail_size[1] // 2)

            for file_path in source_file_paths:
                thumb_obj = None
                if file_path.lower().endswith(self.VIDEO_FORMATS):
                    thumb_obj = create_video_thumbnail(
                        file_path, small_thumb_size, self.parent.thumbnail_format,
                        self.parent.capture_method_var.get(),
                        cache_enabled=cache_enabled, cache_dir=cache_dir
                    )
                elif file_path.lower().endswith(self.IMAGE_FORMATS):
                    thumb_obj = create_image_thumbnail(
                        file_path, small_thumb_size,
                        cache_enabled=cache_enabled, cache_dir=cache_dir, database=database
                    )
                
                if thumb_obj:
                    thumbnails.append(thumb_obj._light_image) # We only need the raw PIL Image object

            if not thumbnails:
                logging.debug("Could not generate any thumbnails for preview grid: %s", folder_path)
                return None
    
            # --- Grid composition logic with rounded corners ---
            # Ensure you have 'from PIL import ImageDraw' at the top of your file
            rows, cols = 2, 2
            gap = 6
            thumb_w, thumb_h = 90, 60
            top_offset = 14
            
            # Define how round the corners should be
            corner_radius = 6

            total_grid_width = cols * thumb_w + (cols - 1) * gap
            total_grid_height = rows * thumb_h + (rows - 1) * gap

            offset_x = (thumbnail_size[0] - total_grid_width) // 2
            offset_y = (thumbnail_size[1] - total_grid_height) // 2

            grid_image = Image.new('RGBA', thumbnail_size, (0, 0, 0, 0))

            # Create a reusable mask with rounded corners for the thumbnails
            mask = Image.new("L", (thumb_w, thumb_h), 0)
            draw = ImageDraw.Draw(mask)
            draw.rounded_rectangle((0, 0, thumb_w, thumb_h), radius=corner_radius, fill=255)

            for i, thumb_image in enumerate(thumbnails):
                """
                Resizes the raw image, ensures it has an alpha channel, 
                and pastes it onto the main grid using the rounded mask.
                """
                # Convert to RGBA to safely handle transparent corners
                resized = thumb_image.resize((thumb_w, thumb_h), Image.LANCZOS).convert("RGBA")

                col = i % cols
                row = i // cols

                x_slot = offset_x + col * (thumb_w + gap)
                y_slot = offset_y + row * (thumb_h + gap)

                # Positioning logic remains identical
                if i == 0:   # top-left
                    x = x_slot
                    y = y_slot + top_offset
                elif i == 1: # top-right
                    x = x_slot + thumb_w - resized.width
                    y = y_slot + top_offset
                elif i == 2: # bottom-left
                    x = x_slot
                    y = y_slot + thumb_h - resized.height + top_offset
                elif i == 3: # bottom-right
                    x = x_slot + thumb_w - resized.width
                    y = y_slot + thumb_h - resized.height + top_offset

                # Paste the thumbnail using the rounded mask
                grid_image.paste(resized, (x, y), mask=mask)

            return grid_image

        except Exception as e:
            logging.error(f"❌ Error creating folder preview thumbnail: {e}", exc_info=True)
            return None        
        
    def create_folder_thumbnail(self, thumbnail_size, folder_path=None, cache_enabled=True, cache_dir="thumbnail_cache", database=None, is_cached=False):
        """
        Creates the folder icon with an optional preview grid.
        OPTIMIZED: Uses memory cache for base icons + fast return if not scanned.
        """
        try:
            # 1. Load base icon (green for cache, yellow/standard for others)
            icon_name = "folder_g.png" if is_cached else "folder.png"
            
            # Helper must look in self.default_directory/icons/
            folder_base = self._get_cached_folder_base_icon(icon_name, thumbnail_size)

            # 2. Prepare canvas (RGBA ensures transparency for icon corners)
            canvas = Image.new("RGBA", thumbnail_size, (0, 0, 0, 0))
            canvas.paste(folder_base, (0, 0), folder_base)

            # 3. If folder is scanned (in DB), insert preview grid
            if folder_path and os.path.isdir(folder_path) and is_cached:
                grid_img = self.create_folder_preview_thumbnail(
                    folder_path=folder_path,
                    thumbnail_size=thumbnail_size
                )
                if grid_img:
                    # Overlay grid onto folder icon
                    canvas.paste(grid_img, (0, 0), grid_img)

            # 4. Return CTkImage with defined size for correct scaling
            return ctk.CTkImage(light_image=canvas, dark_image=canvas, size=thumbnail_size)

        except Exception as e:
            logging.error(f"❌ Error in create_folder_thumbnail: {e}")
            # Creates yellow rectangle when something goes wrong
            fallback = Image.new("RGB", thumbnail_size, (255, 255, 0))
            return ctk.CTkImage(light_image=fallback, dark_image=fallback, size=thumbnail_size)


def sanitize_thumbnail_time(thumbnail_time, duration):
    """
    Clamp thumbnail_time to be slightly before the end of duration.
    """
    if duration is None:
        return thumbnail_time
    try:
        thumbnail_time = float(thumbnail_time)
    except Exception:
        return thumbnail_time
    if thumbnail_time >= duration:
        return max(0, duration - 0.1)
    return thumbnail_time

def get_video_duration_mediainfo(video_path):
    """
    Gets the video duration using OpenCV (cv2.VideoCapture) with caching.
    """
    # --- 1. Fast return from cache ---
    if video_path in _duration_cache:
        # logging.info(f"[Duration] Cache HIT for: {os.path.basename(str(video_path))}")
        return _duration_cache[video_path]
    
    # --- Guard Clauses ---
    if not video_path:
        return 0.0

    if isinstance(video_path, tuple):
        video_path = video_path[0]
        if not video_path: return 0.0

    if not isinstance(video_path, str) or not os.path.exists(video_path):
        return 0.0
    
    # --- Get duration (heavy work) ---
    base_name = os.path.basename(video_path)
    duration = 0.0
    cap = None 

    try:
        import cv2  # lazy import — only paid on first duration lookup
        cap = cv2.VideoCapture(video_path) 
        if cap.isOpened():
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            if fps > 0 and frame_count > 0:
                duration = frame_count / fps
            else:
                # Fallback to FFmpeg if OpenCV fails to detect
                duration = get_duration_with_ffmpeg(video_path)
        else:
            duration = get_duration_with_ffmpeg(video_path)

    except Exception as e:
        logging.error(f"[Duration] Error getting duration for '{base_name}': {e}")
        duration = get_duration_with_ffmpeg(video_path)
    
    finally:
        if cap is not None:
            cap.release()

    # --- 2. Save to cache ---
    if duration > 0:
        _duration_cache[video_path] = duration
        
    return duration


def discard_duration_cache_entry(video_path: str) -> None:
    """Remove cached duration for a path (avoids stale assumptions; drops probe pressure on re-scan)."""
    if not video_path:
        return
    _duration_cache.pop(video_path, None)


def get_duration_with_ffmpeg(video_path):
    """
    Gets video duration using ffprobe. Version with timeout to prevent hangs.
    """
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path
    ]
    try:
        # Add timeout=10 to prevent hangs
        result = subprocess.run(
            command, 
            capture_output=True,
            text=True,
            startupinfo=startupinfo,
            timeout=10
        )
        duration_str = result.stdout.strip()
        
        if not duration_str:
            return 0.0

        return float(duration_str)

    except subprocess.TimeoutExpired:
        logging.info(f"[Timeout] ffprobe hung on file: {os.path.basename(video_path)}")
        return 0.0
    except (ValueError, TypeError):
        logging.info(f"[FFmpeg Fallback] ffprobe returned invalid value for: {os.path.basename(video_path)}")
        return 0.0
    except Exception as e:
        logging.info(f"[FFmpeg Fallback] ffprobe error for {os.path.basename(video_path)}: {e}")
        return 0.0






def load_recent_directories(settings_file):
    """Load list of recently opened directories from JSON settings file."""
    logging.info("WILL LOAD RECENT DIRECTORIES")
    
    if os.path.exists(settings_file):
        try:
            with open(settings_file, "r") as file:
                recent_directories = json.load(file)
                logging.info(f"Recent directories loaded successfully: {recent_directories}")
                
                return recent_directories.get("recent_directories", [])
        except json.JSONDecodeError as e:
            logging.info(f"JSON Decode Error: {e} - The file might be empty or corrupted. Initializing an empty list.")
        except PermissionError as e:
            logging.info(f"Error loading recent directories due to permission issues: {e}")
    return []




def save_recent_directories(settings_file, recent_directories):
    """Save list of recently opened directories to JSON settings file."""
    try:
        with open(settings_file, "w") as file:
            json.dump({"recent_directories": recent_directories}, file)
            logging.info("Recent directories saved successfully.")  # Debug info
    except PermissionError as e:
        logging.info(f"Error saving settings: {e}")




def get_cache_dir_path(file_path, cache_dir):
    """Compute cache directory path and full cache file path for a given file. Returns (cache_dir_path, full_path)."""
    # Normalize the file path
    abs_path = os.path.abspath(file_path)
    drive, tail = os.path.splitdrive(abs_path)
    
    # Clean up the drive part
    root_name = drive.replace(':', '') if drive else tail.split(os.sep)[0]
    
    # Split the path into components
    path_components = abs_path.split(os.sep)
    
    # Reconstruct the path under the cache directory
    cache_dir_path = os.path.join(cache_dir, root_name, *path_components[1:-1])
    
    # Reconstruct the full path under the cache directory
    full_path = os.path.join(cache_dir, root_name, *path_components[1:])
    
    # logging.info(f"get_cache_dir_path - drive: {drive}, tail: {tail}")
    # logging.info(f"get_cache_dir_path - root_name: {root_name}")
    # logging.info(f"get_cache_dir_path - abs_path: {abs_path}")
    # logging.info(f"get_cache_dir_path - path_components: {path_components}")
    # logging.info(f"get_cache_dir_path - cache_dir_path: {cache_dir_path}")
    # logging.info(f"get_cache_dir_path - full_path: {full_path}")
    
    return cache_dir_path, full_path



def create_image_thumbnail(image_path, thumbnail_size, cache_enabled=True, database=None, cache_dir="thumbnail_cache"):
    """Generate or retrieve cached image thumbnail. Registers file in database if provided."""
    cached_thumbnail = thumbnail_cache.get(image_path)
    # print (f"IMAGE THUMB cached_thumbnail {cached_thumbnail}")
    if cached_thumbnail:
        # print (f"create_IMAGE_thumbnail : returning cached thumbnails   {cached_thumbnail}")
        return cached_thumbnail
    # Otherwise, generate and cache the thumbnail
    else: 
        try:
            cache_dir_path, full_path = get_cache_dir_path(image_path, cache_dir)
            os.makedirs(cache_dir_path, exist_ok=True)
            
            thumbnail_format = "jpg"
            # logging.info(f"IMAGE thumbnail_format: {thumbnail_format}")

            cache_key = f"{os.path.basename(image_path)}_{thumbnail_size[0]}x{thumbnail_size[1]}.{thumbnail_format}"
            cache_path = os.path.join(cache_dir_path, cache_key)
            
            # logging.info(f"create_image_thumbnail - cache_dir_path: {cache_dir_path}")
            # logging.info(f"create_image_thumbnail - cache_key: {cache_key}")
            # logging.info(f"create_image_thumbnail - final cache_path: {cache_path}")

            if cache_enabled and os.path.exists(cache_path):
                _cached_img = Image.open(cache_path)
                _cached_img.load()
                return ctk.CTkImage(light_image=_cached_img, dark_image=_cached_img)

            image = Image.open(image_path)
            
            image.thumbnail(thumbnail_size, Image.LANCZOS)

            save_format = "JPEG" if thumbnail_format.lower() == "jpg" else thumbnail_format.upper()
            # bg = Image.new('RGB', thumbnail_size, (0, 0, 0, 0))  # Change background color as needed
            #thumbBGColor = "gray28"  #should be imported from main PY, fix!!
            # should be  self.thumbBGColor , need to fix!!
            bg = Image.new('RGB', thumbnail_size, (71, 71, 71))
            bg.paste(image, ((thumbnail_size[0] - image.size[0]) // 2, (thumbnail_size[1] - image.size[1]) // 2))
            bg.save(cache_path, format=save_format)
            
            thumbnail = ctk.CTkImage(light_image=bg, dark_image=bg)  # Create the CTkImage object
            

            if database:
                width, height = image.size
                database.add_entry(os.path.basename(image_path), image_path, width, height)
            
             # After generating the thumbnail
            if thumbnail is not None:
                thumbnail_cache.set(image_path, thumbnail)  # Save to cache
            
            # logging.info(f"Thumbnail created and saved at: {cache_path}")
            return ctk.CTkImage(light_image=bg, dark_image=bg)
        except UnidentifiedImageError as e:
            logging.debug("create_image_thumbnail: not a valid image (skip): %s — %s", image_path, e)
            return None
        except OSError as e:
            logging.debug("create_image_thumbnail: OS error (skip): %s — %s", image_path, e)
            return None
        except Exception as e:
            logging.info(f"Error creating image thumbnail for {image_path}: {e}")
            return None


def create_video_thumbnail(video_path, thumbnail_size, thumbnail_format, capture_method, thumbnail_time=10, cache_enabled=True, overwrite=False, cache_dir="thumbnail_cache", database=None):
    """
    Generates a video thumbnail. Uses cache if available.
    Now accepts a 'database' argument to automatically register the file.
    FIXED: Implemented actual fallback to FFmpeg when primary methods fail.
    OPTIMIZED: Bypasses OpenCV for known problematic formats (wmv, avi) to prevent UI freezes.
    """
    # Set default values
    if thumbnail_size is None:
        thumbnail_size = (180, 120)
    if thumbnail_time is None:
        thumbnail_time = 0.1

    # --- Helper function: avoid writing DB entry 3x in different places ---
    def _finalize_and_return(thumb_obj):
        if database:
            # Save only basic (0, 0); app will fetch dimensions later in background.
            database.add_entry(os.path.basename(video_path), video_path, 0, 0)
            
            # Fetch duration (extremely fast here due to _duration_cache)
            duration = get_video_duration_mediainfo(video_path)
            
            # Save duration permanently to the database to speed up Timeline selections
            if duration > 0:
                database.update_file_metadata(video_path, duration=duration)
        return thumb_obj

    # --- Simplified caching logic ---
    if not overwrite:
        cached_thumbnail = thumbnail_cache.get(video_path)
        if cached_thumbnail:
            # FIX: Return via our helper function
            return _finalize_and_return(cached_thumbnail)

        if cache_enabled:
            cache_dir_path, _ = get_cache_dir_path(video_path, os.path.abspath(cache_dir))
            cache_key = f"{os.path.basename(video_path)}_{thumbnail_size[0]}x{thumbnail_size[1]}.jpg"
            cache_path = os.path.join(cache_dir_path, cache_key)
            
            if os.path.exists(cache_path):
                image = Image.open(cache_path)
                thumbnail = ctk.CTkImage(light_image=image, dark_image=image)
                thumbnail_cache.set(video_path, thumbnail)
                
                # FIX: Return via our helper function when loading from disk too
                return _finalize_and_return(thumbnail)

    # --- Generate new thumbnail ---
    try:
        frame = None
        
        # --- Protection: OpenCV hangs on WMV/AVI. Skip it ---
        if capture_method == "OpenCV" and video_path.lower().endswith(('.wmv', '.avi')):
            logging.info(f"[⚠️] Skipping OpenCV for {os.path.basename(video_path)}, forcing FFmpeg to avoid lag.")
            capture_method = "FFmpeg"

        # Select primary method
        if capture_method == "Imageio":
            frame = captureFrameImageio(video_path, thumbnail_size, thumbnail_time)
        elif capture_method == "OpenCV":
            frame = captureFrameOpenCV(video_path, thumbnail_size, thumbnail_time)
        else:  # Default is FFmpeg
            frame = captureFrameFFmpeg(video_path, thumbnail_size, thumbnail_time)

        # --- Fallback fix: if primary fails and it wasn't FFmpeg, try FFmpeg ---
        if frame is None and capture_method != "FFmpeg":
            logging.info(f"[⚠️] Primary method '{capture_method}' failed, ACTUALLY trying FFmpeg fallback...")
            frame = captureFrameFFmpeg(video_path, thumbnail_size, thumbnail_time)

        # If all attempts fail completely
        if frame is None:
            logging.info(f"[❌] All capture methods failed for {os.path.basename(video_path)}.")
            return None

        # Process and save frame
        cache_dir_path, _ = get_cache_dir_path(video_path, os.path.abspath(cache_dir))
        os.makedirs(cache_dir_path, exist_ok=True)
        cache_key = f"{os.path.basename(video_path)}_{thumbnail_size[0]}x{thumbnail_size[1]}.jpg"
        cache_path = os.path.join(cache_dir_path, cache_key)
        
        image = Image.fromarray(frame)
        image = ImageOps.contain(image, thumbnail_size, Image.LANCZOS)
        bg = Image.new('RGB', thumbnail_size, (71, 71, 71))
        bg.paste(image, ((thumbnail_size[0] - image.size[0]) // 2, (thumbnail_size[1] - image.size[1]) // 2))
        bg.save(cache_path, format="JPEG")

        thumbnail = ctk.CTkImage(light_image=bg, dark_image=bg)
        thumbnail_cache.set(video_path, thumbnail)

        # FIX: Even when newly generated, return via our helper function, so video duration gets saved.
        return _finalize_and_return(thumbnail)

    except Exception as e:
        logging.info(f"❌ Error creating video thumbnail for {video_path}: {e}")
        return None
def create_video_thumbnailOld(video_path, thumbnail_size, thumbnail_format, capture_method, thumbnail_time=10, cache_enabled=True, overwrite=False, cache_dir="thumbnail_cache", database=None):
    """
    Generates a video thumbnail. Uses cache if available.
    Now accepts a 'database' argument to automatically register the file.
    FIXED: Implemented actual fallback to FFmpeg when primary methods fail.
    OPTIMIZED: Bypasses OpenCV for known problematic formats (wmv, avi) to prevent UI freezes.
    """
    # Set default values
    if thumbnail_size is None:
        thumbnail_size = (180, 120)
    if thumbnail_time is None:
        thumbnail_time = 0.1

    # --- Simplified caching logic ---
    if not overwrite:
        cached_thumbnail = thumbnail_cache.get(video_path)
        if cached_thumbnail:
            # --- FIX: Even with cache, ensure file is in DB ---
            if database:
                # Use 0,0 for speed (dimensions filled later); main goal is to have a record for keywords.
                database.add_entry(os.path.basename(video_path), video_path, 0, 0)
                
                    
            return cached_thumbnail

        if cache_enabled:
            cache_dir_path, _ = get_cache_dir_path(video_path, os.path.abspath(cache_dir))
            cache_key = f"{os.path.basename(video_path)}_{thumbnail_size[0]}x{thumbnail_size[1]}.jpg"
            cache_path = os.path.join(cache_dir_path, cache_key)
            
            if os.path.exists(cache_path):
                image = Image.open(cache_path)
                thumbnail = ctk.CTkImage(light_image=image, dark_image=image)
                thumbnail_cache.set(video_path, thumbnail)
                
                # --- FIX: Also when loading from disk ---
                if database:
                    database.add_entry(os.path.basename(video_path), video_path, 0, 0)
                return thumbnail

    # --- Generate new thumbnail ---
    try:
        frame = None
        
        # --- Protection: OpenCV hangs on WMV/AVI. Skip it ---
        if capture_method == "OpenCV" and video_path.lower().endswith(('.wmv', '.avi')):
            logging.info(f"[⚠️] Skipping OpenCV for {os.path.basename(video_path)}, forcing FFmpeg to avoid lag.")
            capture_method = "FFmpeg"

        # Select primary method
        if capture_method == "Imageio":
            frame = captureFrameImageio(video_path, thumbnail_size, thumbnail_time)
        elif capture_method == "OpenCV":
            frame = captureFrameOpenCV(video_path, thumbnail_size, thumbnail_time)
        else:  # Default is FFmpeg
            frame = captureFrameFFmpeg(video_path, thumbnail_size, thumbnail_time)

        # --- Fallback fix: if primary fails and it wasn't FFmpeg, try FFmpeg ---
        if frame is None and capture_method != "FFmpeg":
            logging.info(f"[⚠️] Primary method '{capture_method}' failed, ACTUALLY trying FFmpeg fallback...")
            frame = captureFrameFFmpeg(video_path, thumbnail_size, thumbnail_time)

        # If all attempts fail completely
        if frame is None:
            logging.info(f"[❌] All capture methods failed for {os.path.basename(video_path)}.")
            return None

        # Process and save frame

        cache_dir_path, _ = get_cache_dir_path(video_path, os.path.abspath(cache_dir))
        os.makedirs(cache_dir_path, exist_ok=True)
        cache_key = f"{os.path.basename(video_path)}_{thumbnail_size[0]}x{thumbnail_size[1]}.jpg"
        cache_path = os.path.join(cache_dir_path, cache_key)
        
        image = Image.fromarray(frame)
        image = ImageOps.contain(image, thumbnail_size, Image.LANCZOS)
        bg = Image.new('RGB', thumbnail_size, (71, 71, 71))
        bg.paste(image, ((thumbnail_size[0] - image.size[0]) // 2, (thumbnail_size[1] - image.size[1]) // 2))
        bg.save(cache_path, format="JPEG")

        thumbnail = ctk.CTkImage(light_image=bg, dark_image=bg)
        thumbnail_cache.set(video_path, thumbnail)

        # --- FIX: Write to database ---
        if database:
            # REMOVED slow get_video_size!
            # Save only basic (0, 0); app will fetch dimensions later in background.
            database.add_entry(os.path.basename(video_path), video_path, 0, 0)

        return thumbnail

    except Exception as e:
        logging.info(f"❌ Error creating video thumbnail for {video_path}: {e}")
        return None


# def capture_frame(video_path, thumbnail_time=10, method="FFmpeg"):
    # """
    # Captures a video frame using the selected method with fallback options.

    # :param video_path: Path to the video file.
    # :param thumbnail_time: Time in the video (as a percentage, 0.0 to 1.0) to capture the frame.
    # :param method: Preferred method for capturing ("FFmpeg", "OpenCV", "Imageio").
    # :return: Captured frame (as a numpy array) or None if all methods fail.
    # """
    # def ffmpeg_capture():
        # try:
            # cap = cv2.VideoCapture(video_path, cv2.CAP_FFMPEG)
            # cap.set(cv2.CAP_PROP_POS_MSEC, thumbnail_time * 1000)
            # ret, frame = cap.read()
            # cap.release()
            # return frame if ret else None
        # except Exception as e:
            # logging.info(f"[FFmpeg Error] {e}")
            # return None

    # def opencv_capture():
        # try:
            # cap = cv2.VideoCapture(video_path)
            # cap.set(cv2.CAP_PROP_POS_MSEC, thumbnail_time * 1000)
            # ret, frame = cap.read()
            # cap.release()
            # return frame if ret else None
        # except Exception as e:
            # logging.info(f"[OpenCV Error] {e}")
            # return None

    # def imageio_capture():
        # try:
            # reader = imageio.get_reader(video_path, "ffmpeg")
            # fps = reader.get_meta_data()["fps"]
            # frame_number = int(thumbnail_time * fps)
            # return reader.get_data(frame_number)
        # except Exception as e:
            # logging.info(f"[ImageIO Error] {e}")
            # return None

    # methods = {
        # "FFmpeg": ffmpeg_capture,
        # "OpenCV": opencv_capture,
        # "Imageio": imageio_capture,
    # }

    # Try the preferred method first, fallback if it fails
    # selected_method = methods.get(method, ffmpeg_capture)
    # frame = selected_method()
    # if frame is not None:
        # return frame

    # Try fallback methods
    # for fallback_method in methods.values():
        # if fallback_method is not selected_method:  # Avoid re-trying the same method
            # frame = fallback_method()
            # if frame is not None:
                # return frame

    # logging.info("[Error] All capture methods failed.")
    # return None


def captureFrameImageio(video_path, thumbnail_size, thumbnail_time=10, crop_to_fit=False):
    """Capture a video frame using imageio/ffmpeg. Returns numpy array (RGB) or None."""
    try:
        logging.info(f" capturing frame with ImageIO for {video_path}  thumbnail_time: {thumbnail_time} ")

        import imageio  # lazy import — only paid when Imageio capture method is used

        duration = get_video_duration_mediainfo(video_path)
        thumbnail_time = sanitize_thumbnail_time(thumbnail_time, duration)

        reader = imageio.get_reader(video_path, 'ffmpeg')
        fps = reader.get_meta_data()['fps']
        frame_number = int(thumbnail_time * fps)

        try:
            frame = reader.get_data(frame_number)
        except IndexError:
            logging.info(f"Error: Frame number {frame_number} out of range for video {video_path}")
            return None
        finally:
            reader.close()

        frame = resize_and_crop_frame(frame, thumbnail_size, crop_to_fit=crop_to_fit)
        return frame

    except Exception as e:
        logging.info(f"Error capturing frame with Imageio for {video_path}: {e}")
        return None





# In file_operations.py

def captureFrameOpenCV(video_path, thumbnail_size, thumbnail_time=10, crop_to_fit=False, duration=None): # Still accepts duration
    """
    Captures a video frame using OpenCV (cv2.VideoCapture).
    This is the simpler version, but retains the duration optimization.
    ADDED extra logging to verify received duration.
    OPTIMIZED: Avoids double-opening the file by checking duration on the active capture object.
    """

    # logging.info(f"===> captureFrameOpenCV RECEIVED duration = {duration} (type: {type(duration)}) for {os.path.basename(video_path)}")

    import cv2  # lazy import — only paid when OpenCV capture method is used

    cap = None
    try:
        cap = cv2.VideoCapture(video_path, cv2.CAP_FFMPEG)
        if not cap.isOpened():
             logging.error(f"[OpenCV Simplified] Failed to open video: {os.path.basename(video_path)}")
             return None

        # OPTIMIZATION: Get duration from the already open cap if not provided
        if duration is None:
            try:
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                if fps > 0 and frame_count > 0:
                    duration = frame_count / fps
                else:
                    # Fallback to cache or 0 if we can't determine it easily without re-opening
                    duration = _duration_cache.get(video_path, 0.0)
            except Exception:
                duration = 0.0

        sanitized_time = sanitize_thumbnail_time(thumbnail_time, duration)
        time_msec = sanitized_time * 1000

        cap.set(cv2.CAP_PROP_POS_MSEC, time_msec)
        ret, frame = cap.read()

        if not ret:
            logging.warning(f"[OpenCV Simplified] Unable to read frame at {sanitized_time:.2f}s from video {os.path.basename(video_path)}")
            return None

        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return resize_and_crop_frame(frame, thumbnail_size, crop_to_fit=crop_to_fit)

    except Exception as e:
        logging.error(f"[OpenCV Simplified] Error capturing frame for {os.path.basename(video_path)}: {e}", exc_info=False)
        return None
    finally:
        if cap is not None and cap.isOpened():
            cap.release()





def captureFrameFFmpeg(video_path, thumbnail_size, thumbnail_time=10, crop_to_fit=True, duration=None):
    """
    Optimized: "Optimistic" approach. 
    Added Windows startupinfo to reduce subprocess spawning overhead.
    """
    FULLRES_SIZE = (99999, 99999)
    _low = video_path.lower()
    _seek_sensitive = _low.endswith((".wmv", ".asf"))

    def _run_ffmpeg(t_time, seek_before_input: bool):
        head = [
            get_ffmpeg_path(),
            '-err_detect', 'ignore_err',
            '-fflags', '+genpts',
            '-analyzeduration', '5000000',
            '-probesize', '5M',
        ]
        if seek_before_input:
            cmd = head + ['-ss', str(t_time), '-i', video_path, '-frames:v', '1']
        else:
            cmd = head + ['-i', video_path, '-ss', str(t_time), '-frames:v', '1']

        if thumbnail_size != FULLRES_SIZE:
            w, h = thumbnail_size
            if crop_to_fit:
                vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
            else:
                vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
            cmd.extend(['-vf', vf])

        cmd.extend(['-f', 'image2pipe', '-vcodec', 'png', '-an', '-loglevel', 'warning', 'pipe:1'])
        
        # --- Speed up on Windows (saves milliseconds at process start) ---
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
        try:
            # Added startupinfo to subprocess call
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, startupinfo=startupinfo)
            return proc.stdout, proc.stderr
        except Exception as e:
            return None, str(e)

    def _try_time(t_time) -> bytes | None:
        for seek_first in (True, False):
            if not seek_first and not _seek_sensitive:
                break
            stdout_data, _stderr_data = _run_ffmpeg(t_time, seek_before_input=seek_first)
            if stdout_data:
                return stdout_data
        return None

    def _decode_png(stdout_data: bytes):
        import numpy as np  # lazy import — only paid when FFmpeg capture method is used
        image = Image.open(BytesIO(stdout_data)).convert('RGB')
        return np.array(image)

    # 1. Optimistic attempt
    stdout_data = _try_time(thumbnail_time)

    if stdout_data:
        try:
            return _decode_png(stdout_data)
        except Exception:
            pass 

    # 2. Fallback
    if duration is None:
        duration = get_video_duration_mediainfo(video_path)
    
    safe_time = sanitize_thumbnail_time(thumbnail_time, duration)
    
    if safe_time == thumbnail_time:
        return None 

    stdout_data = _try_time(safe_time)
    
    if stdout_data:
        try:
            return _decode_png(stdout_data)
        except Exception as e:
            logging.info(f"[FFMPEG] Fallback parse error: {e}")
            return None
    else:
        return None


def capture_fullres_frame(video_path, method="ffmpeg", time_sec=10.0):
    """
    Wrapper that reuses existing capture functions but requests full-res frame (no resize).
    """
    # fullres = very large size -> crop_to_fit effectively ignored, so original aspect ratio is preserved
    fullres_size = (99999, 99999)

    if method == "opencv":
        return captureFrameOpenCV(video_path, fullres_size, thumbnail_time=time_sec, crop_to_fit=False)
    elif method == "imageio":
        return captureFrameImageio(video_path, fullres_size, thumbnail_time=time_sec, crop_to_fit=False)
    else:
        return captureFrameFFmpeg(video_path, fullres_size, thumbnail_time=time_sec, crop_to_fit=False)


#
# --- The updated save_capture_image function ---
#
# It now accepts the 'player' object directly instead of 'time_sec'.
# The logic for getting the current time is now inside this function.
#
def save_capture_image(controller, video_path, player, method="ffmpeg"):
    """
    Captures a frame from the video at the current playback time and saves it.
    """
    # --- New logic to get time from the player object ---
    time_sec = 10.0 # Default fallback value
    if player and hasattr(player, "get_time"):
        current_time_ms = player.get_time()
        if current_time_ms > 0: # Make sure time is valid
             time_sec = current_time_ms / 1000.0

    if not video_path or not os.path.exists(video_path):
        logging.info(f"[Capture] Video path not provided or file not found: {video_path}")
        return

    # The rest of the function remains the same...
    frame = capture_fullres_frame(video_path, method=method, time_sec=time_sec)
    if frame is None:
        logging.info(f"[Capture] Failed to capture frame from: {video_path}")
        return

    # ... rest of code for generating filename and saving
    out_dir = os.getcwd()
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    now_str = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    h, m, s = int(time_sec // 3600), int((time_sec % 3600) // 60), int(time_sec % 60)
    time_code = f"{h:02d}-{m:02d}-{s:02d}"
    out_name = f"{base_name}_{now_str}_{time_code}.png"
    out_path = os.path.join(out_dir, out_name)

    try:
        Image.fromarray(frame).save(out_path, format="PNG", optimize=True)
        logging.info(f"[Capture] Frame saved: {out_path}")
    except Exception as e:
        logging.info(f"[Capture] Error saving frame for {video_path}: {e}")

        

def resize_and_crop_frame(frame, target_size, crop_to_fit=True):
    """
    Resize and optionally crop/pad the frame to match the target_size (w, h).
    Works with RGB frames (NumPy arrays).
    """
    import cv2
    import numpy as np

    h, w, _ = frame.shape
    target_w, target_h = target_size
    src_aspect = w / h
    dst_aspect = target_w / target_h

    if crop_to_fit:
        # Fill + crop (cover)
        if src_aspect > dst_aspect:
            new_h = target_h
            new_w = int(w * (target_h / h))
        else:
            new_w = target_w
            new_h = int(h * (target_w / w))

        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
        x_start = (new_w - target_w) // 2
        y_start = (new_h - target_h) // 2
        return resized[y_start:y_start+target_h, x_start:x_start+target_w]
    else:
        # Fit + pad (contain)
        if src_aspect > dst_aspect:
            new_w = target_w
            new_h = int(h * (target_w / w))
        else:
            new_h = target_h
            new_w = int(w * (target_h / h))

        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
        result = np.zeros((target_h, target_w, 3), dtype=np.uint8)
        x_offset = (target_w - new_w) // 2
        y_offset = (target_h - new_h) // 2
        result[y_offset:y_offset+new_h, x_offset:x_offset+new_w] = resized
        return result



def get_ffmpeg_path():
    """Returns the full path to ffmpeg.exe (either from PATH or local tools folder)."""
    candidates: list[str] = []
    if getattr(sys, "frozen", False):
        _base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        candidates.append(os.path.join(_base, "tools", "ffmpeg", "bin", "ffmpeg.exe"))
        candidates.append(os.path.join(_base, "tools", "ffmpeg", "ffmpeg.exe"))
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(_repo_root, "tools", "ffmpeg", "bin", "ffmpeg.exe"))
    candidates.append(os.path.join(_repo_root, "tools", "ffmpeg", "ffmpeg.exe"))
    candidates.append(os.path.abspath(os.path.join("tools", "ffmpeg", "bin", "ffmpeg.exe")))
    candidates.append(os.path.abspath(os.path.join("tools", "ffmpeg", "ffmpeg.exe")))
    for local_ffmpeg in candidates:
        if os.path.isfile(local_ffmpeg):
            return local_ffmpeg
    path = shutil.which("ffmpeg")
    if path:
        return path
    raise FileNotFoundError("FFmpeg not found! Please run install.bat or add ffmpeg to PATH.")



def get_file_info(path):
    """Return formatted string with file name, dimensions, size, and modification time."""
    file_name = os.path.basename(path)
    file_size = os.path.getsize(path)
    file_mtime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(path)))
    
    if os.path.isfile(path):
        if path.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif')):
            image = Image.open(path)
            width, height = image.size
            dimensions = f"{width}x{height}"
        else:
            import cv2  # lazy import — only paid when file info for a video is requested
            cap = cv2.VideoCapture(path)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            dimensions = f"{width}x{height}"
            cap.release()  # Release the video capture object
    else:
        dimensions = "Folder"
    
    file_info = f"Name: {file_name}\nDimensions: {dimensions}\nSize: {file_size} bytes\nModified: {file_mtime}"
    return file_info

def get_file_metadata(path):
    """Return dict with file metadata (name, size, modified, width, height, duration, description). Used for infopanel in left tree."""
    try:
        metadata = {
            "name": os.path.basename(path),
            "file_size": os.path.getsize(path) if os.path.isfile(path) else 0,
            "modified": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(path))),
            "width": None,
            "height": None,
            "duration": None,
            "description": None,  
        }

        if os.path.isfile(path):
            ext = path.lower()
            if ext.endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif')):
                with Image.open(path) as img:
                    metadata["width"], metadata["height"] = img.size
                    try:
                        exif_data = img.getexif()
                        metadata["description"] = exif_data.get(270)
                    except Exception as e:
                        metadata["description"] = f"EXIF Error: {e}"
            elif ext.endswith(('.avi', '.mp4', '.mkv', '.mov', '.wmv', '.flv', '.mpeg', '.mpg', '.webm',
                               '.3gp', '.ogv', '.vob', '.asf', '.rm', '.qt')):
                import cv2  # lazy import — only paid when video metadata is requested
                cap = cv2.VideoCapture(path)
                metadata["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                metadata["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                metadata["duration"] = cap.get(cv2.CAP_PROP_FRAME_COUNT) / max(cap.get(cv2.CAP_PROP_FPS), 1)
                cap.release()

        return metadata
    except Exception as e:
        logging.info(f"[ERROR] get_file_metadata failed for {path}: {e}")
        return {
            "name": os.path.basename(path),
            "file_size": 0,
            "modified": None,
            "width": None,
            "height": None,
            "duration": None,
            "description": None,
        }
