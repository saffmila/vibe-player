"""
Timeline thumbnail generation and duration helpers for Vibe Player.

``TimelineManager`` builds strip thumbnails and reads duration via mediainfo/OpenCV/FFmpeg.
"""

import logging
import os

from PIL import Image, ImageOps

from file_operations import (
    captureFrameFFmpeg,
    captureFrameImageio,
    captureFrameOpenCV,
    create_video_thumbnail,
    get_video_duration_mediainfo,
)

class TimelineManager:
    def __init__(self, thumbnail_size=(320, 240), thumbnail_format="jpg", cache_dir="thumbnail_cache"):
        self.thumbnail_size = thumbnail_size
        self.thumbnail_format = thumbnail_format
        self.cache_dir = cache_dir
        self.capture_method = "opencv"
        logging.debug(
            "TimelineManager initialized (capture_method=%s)",
            self.capture_method,
        )

    def get_video_duration(self, video_path):
            """
            Retrieves video duration: 
            1. Checks DB (Persistent) 
            2. Checks File via MediaInfo/OpenCV (Expensive)
            3. Caches result back to DB.
            """
            if not video_path:
                return 0

            path_abs = os.path.abspath(video_path)

        # 1. Try to retrieve from database first
            if hasattr(self, 'controller') and hasattr(self.controller, 'database'):
                res = self.controller.database.get_entry(path_abs)
                if res and res.get('duration'):
                    return res['duration']

        # 🔴 2. If it's not in the DB (or DB does not exist), we need to actually inspect the file
            duration = get_video_duration_mediainfo(video_path)

        # 3. Store result in DB for next time
            if duration > 0 and hasattr(self, 'controller') and hasattr(self.controller, 'database'):
                self.controller.database.update_file_metadata(path_abs, duration=duration)
                
            return duration

    def set_capture_method(self, method_name):
        """
        Sets the thumbnail capture method from the main application.
        It converts the received name to lowercase for internal use.
        Args:
            method_name (str): The name of the method (e.g., "OpenCV", "FFMPEG").
        """
        # --- THIS IS THE KEY CHANGE ---
        # Convert the incoming string to lowercase to make the logic robust.
        safe_method_name = method_name.lower()
        logging.info(f"Capture method for timeline set to: '{safe_method_name}' (from input '{method_name}')")
        self.capture_method = safe_method_name
        # --- END OF CHANGE ---

    def set_thumbnail_size(self, size_str):
        """
        Parses a size string (e.g., "320x240") and updates the thumbnail size.
        """
        try:
            width, height = map(int, size_str.split('x'))
            self.thumbnail_size = (width, height)
            logging.info(f"TimelineManager thumbnail size set to: {self.thumbnail_size}")
        except ValueError:
            logging.error(f"Invalid size string format: '{size_str}'. Expected 'widthxheight'.")



    def get_or_generate_thumb_path(self, video_path, timestamp, duration=None):
        """
        Generates and saves a thumbnail if it doesn't exist.
        This version correctly handles FFmpeg failures and directory structure.
        """
        if isinstance(video_path, tuple):
            video_path = video_path[0]
            
        # --- PART 1: BUILDING THE CORRECT PATH (this is okay) ---
        video_dir_full = os.path.dirname(video_path)
        drive, path_tail = os.path.splitdrive(video_dir_full)
        drive_letter_clean = drive.replace(':', '')
        path_tail_clean = path_tail.lstrip('\\/')
        cache_dir_path = os.path.join(
            os.path.abspath(self.cache_dir),
            "timeline_widget",
            drive_letter_clean,
            path_tail_clean
        )
        cache_key = f"{os.path.basename(video_path)}_{int(timestamp)}s_{self.thumbnail_size[0]}x{self.thumbnail_size[1]}.{self.thumbnail_format}"
        cache_path = os.path.join(cache_dir_path, cache_key)

        # Nejdřív zkontrolujeme cache - makedirs voláme jen pokud musíme generovat nový náhled
        if os.path.exists(cache_path):
            return cache_path

        os.makedirs(cache_dir_path, exist_ok=True)
        # logging.debug(f"--> Calling captureFrameOpenCV with timestamp={timestamp}, duration={duration}")
        # --- PART 2: CALLING AND CHECKING FFMPEG (THIS IS THE KEY FIX) ---
        # frame = captureFrameFFmpeg(video_path, self.thumbnail_size, , thumbnail_time=timestamp, duration=duration)
        frame = captureFrameOpenCV(video_path, self.thumbnail_size,  thumbnail_time=timestamp, duration=duration)
        # IF FFMPEG FAILS, WE IMMEDIATELY RETURN AND DO NOT SAVE ANYTHING
        if frame is None:
            logging.warning(f"captureFrameFFmpeg failed for {video_path} at {timestamp}s. Not creating a cache file.")
            return None # <-- Important: we return None, nothing is saved

        # IF FFMPEG SUCCEEDS, WE PROCESS AND SAVE THE IMAGE
        try:
            image = Image.fromarray(frame)
            image = ImageOps.fit(image, self.thumbnail_size, Image.LANCZOS)
            
            save_format = "JPEG" if self.thumbnail_format.lower() == "jpg" else self.thumbnail_format.upper()
            
            
            image.save(cache_path, format=save_format, quality=85) # Quality added for JPG
            # logging.info(f"Successfully created thumbnail at {cache_path}")
            return cache_path
            
        except Exception as e:
            logging.error(f"Failed to process and save the frame from FFMPEG. Reason: {e}", exc_info=True)
            return None


    def get_timeline_thumbnails(self, video_path, num_thumbs=5):
        """
        Calculates timestamps and generates thumbnail paths for a given video.
        """
        if isinstance(video_path, tuple):
            video_path = video_path[0]

        if not video_path:
            logging.warning("[TimelineManager] get_timeline_thumbnails: video_path is None, skipping.")
            return []

        if not os.path.exists(video_path):
            logging.error(f"[TimelineManager] Video file not found at path: {video_path}")
            return []

        # Používáme self.get_video_duration() místo přímého volání mediainfo
        # - to zajistí čtení z DB cache a vyhne se pomalému skenování souboru
        duration = self.get_video_duration(video_path)
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = 0
        logging.info(f"==> get_timeline_thumbnails: Calculated duration = {duration} for {os.path.basename(video_path)}")
        if duration <= 0:
            logging.warning(f"[TimelineManager] Invalid video duration for {video_path}: {duration}")
            return []

        if num_thumbs <= 1:
            timestamps = [0.0]
        else:
            timestamps = [(i * duration) / (num_thumbs - 1) for i in range(num_thumbs)]
            timestamps[-1] = min(timestamps[-1], duration - 0.1 if duration > 0.1 else duration)

        thumb_paths = []
        for ts in timestamps:
            try:
                thumb_path = self.get_or_generate_thumb_path(video_path, ts, duration=duration)
            except Exception as e:
                logging.error(f"[TimelineManager] Failed to generate thumbnail at {ts:.2f}s for video {video_path}", exc_info=True)
                thumb_path = None

            thumb_paths.append((thumb_path, ts))
        return thumb_paths

