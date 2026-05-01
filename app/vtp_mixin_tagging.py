"""Autotagging orchestration mixin for VideoThumbnailPlayer."""
from __future__ import annotations

import logging
import os
import threading
from tkinter import messagebox

from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS


class VtpTaggingMixin:


    ########################################
    ###########****   TAGGING    ****##############
    ########################################

    def _show_ai_initialization_hint(self):
            """Show a one-time info message about first-run model initialization/download."""
            if getattr(self, "_ai_init_hint_shown", False):
                return
            self._ai_init_hint_shown = True
            self.after(0, lambda: self.status_bar.set_action_message("Initializing AI models (first run may take a moment)..."))


    def tag_single_image(self, file_path, plugin, update_ui=True):
            """
            Tags a single image file, updates the database, and refreshes the UI.
            Includes error handling to prevent thread crashes during AI processing.
            """
            if not os.path.exists(file_path):
                logging.error(f"[AutoTag] File not found: {file_path}")
                return

            try:
                # Running the AI model logic
                result = plugin.run(file_path)
                tags = result.get("tags", [])

                if tags:
                    cleaned = ", ".join(tag.strip() for tag in tags if tag.strip())
                    self.database.update_keywords(file_path, cleaned)
                    
                    if update_ui:
                        # Refreshing the thumbnail to show new tags/icon
                        self.after(0, lambda: self.refresh_single_thumbnail(file_path, True))
                    
                    logging.info("[OK] %s: %s", os.path.basename(file_path), tags)
                else:
                    logging.info("No tags returned for %s", file_path)
            
            except Exception as e:
                logging.error(f"[AutoTag] Critical error during tagging {file_path}: {e}")


    def auto_tag_with_plugin_from_file(self, file_path):
            """
            Initiates the tagging process for a single file in a separate thread.
            Provides detailed error feedback if the plugin is missing from the manager.
            """
            if not os.path.isfile(file_path):
                messagebox.showerror("Error", f"Path is not a file: {file_path}")
                return

            plugin_name = self.get_plugin_name_for_engine()
            plugin = self.plugin_manager.get_plugin(plugin_name)
            
            # --- DEBUG LOGIC ---
            if not plugin:
                # See what the manager actually loaded
                loaded = list(self.plugin_manager.plugins.keys())
                error_msg = f"Plugin '{plugin_name}' not found.\n\nLoaded plugins: {loaded}\n\nCheck app.log for import errors!"
                logging.error(f"[PluginManager] {error_msg}")
                messagebox.showerror("Plugin Error", error_msg)
                return

            def tag_one():
                # Setup UI for single processing
                self.after(0, self.status_bar.enable_stop)
                self.after(0, lambda: self.status_bar.set_action_message(f"Tagging: {os.path.basename(file_path)}"))
                self._show_ai_initialization_hint()

                self.tag_single_image(file_path, plugin, update_ui=True)

                # Cleanup UI
                self.after(0, self.status_bar.clear_action_message)
                self.after(0, self.status_bar.disable_stop)
                self.after(0, lambda: self.status_bar.set_progress(0))
                self.after(0, lambda: messagebox.showinfo("Done", f"Tagged {os.path.basename(file_path)}"))

            threading.Thread(target=tag_one, daemon=True).start()


    def auto_tag_video_with_plugin(self, video_path, num_thumbs=3, run_in_thread=True):
        """
        Runs the image autotagging plugin on N video thumbnails.
        Updates the UI status bar dynamically strictly following UI guidelines.
        """
        plugin_name = self.get_plugin_name_for_engine()
        plugin = self.plugin_manager.get_plugin(plugin_name)
        if not plugin:
            messagebox.showerror("Error", f"Plugin '{plugin_name}' not found.")
            return

        thumbs_raw = self.timeline_manager.get_timeline_thumbnails(video_path, num_thumbs)
        if not thumbs_raw:
            messagebox.showinfo("Info", "No thumbnails found for this video.")
            return

        def tag_worker():
            all_tags = set()
            self._show_ai_initialization_hint()
            
            # --- UI UPDATE: Standard prefix according to manual ---
            self.after(0, lambda: self.status_bar.set_action_message("Processing thumbnails..."))
            
            for idx, (thumb_path, timestamp) in enumerate(thumbs_raw, start=1):
                if thumb_path is None or not os.path.exists(thumb_path):
                    logging.warning(f"[AutoTag] Skipping invalid thumb path at {timestamp}s")
                    continue

                if getattr(self, "stop_requested", False):
                    logging.info("STOP requested — exiting video autotag")
                    # --- UI UPDATE: Abort message ---
                    self.after(0, lambda: self.status_bar.set_action_message("Tagging aborted."))
                    break
                
                try:
                    result = plugin.run(thumb_path)
                    tags = result.get("tags", [])
                    if tags:
                        all_tags.update(tag.strip() for tag in tags if tag.strip())
                        
                    # --- UI UPDATE: Concise progress message ---
                    self.after(0, lambda i=idx, t=len(thumbs_raw): self.status_bar.set_action_message(f"Processing thumbnails: {i}/{t}"))
                        
                except Exception as e:
                    logging.error(f"[AutoTag] Plugin error on {thumb_path}: {e}")

            # Finalize only if not aborted
            if not getattr(self, "stop_requested", False):
                keywords = ", ".join(sorted(all_tags))
                if keywords:
                    self.database.update_keywords(video_path, keywords)
                    logging.info("[OK] %s: %s", os.path.basename(video_path), keywords)
                    
                    self.after(0, lambda: self.refresh_single_thumbnail(video_path, True))
                    # --- UI UPDATE: Standard finished message ---
                    self.after(0, lambda: self.status_bar.set_action_message("Tagging completely finished!"))
                else:
                    logging.info("No tags generated for %s", video_path)
                    self.after(0, lambda: self.status_bar.set_action_message("Tagging completely finished! (No tags)"))
            
            if run_in_thread:
                # Keep the message visible for 3 seconds before clearing
                self.after(3000, self.status_bar.clear_action_message)
                self.after(0, self.status_bar.disable_stop)

        if run_in_thread:
            threading.Thread(target=tag_worker, daemon=True).start()
        else:
            tag_worker()


    def auto_tag_with_plugin_from_folder(self, folder_path):
        """
        Batch tags all images and videos in a folder.
        """
        self.stop_requested = False
        self.status_bar.set_stop_callback(lambda: setattr(self, "stop_requested", True))
        self.status_bar.enable_stop()

        plugin_name = self.get_plugin_name_for_engine()
        plugin = self.plugin_manager.get_plugin(plugin_name)
        if not plugin:
            messagebox.showerror("Error", f"Plugin '{plugin_name}' not found.")
            return

        image_paths = []
        video_paths = []
        ignored_dirs = {"__pycache__", "img_metadata", ".git", "cache", "outputs"}

        for root, dirs, files in os.walk(folder_path):
            dirs[:] = [d for d in dirs if d not in ignored_dirs]
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                path = os.path.join(root, file)
                if ext in IMAGE_FORMATS:
                    image_paths.append(path)
                elif ext in VIDEO_FORMATS:
                    video_paths.append(path)

        total = len(image_paths) + len(video_paths)
        if not total:
            messagebox.showinfo("Info", "No images or videos found in folder.")
            return

        def tag_worker():
            idx = 0
            for path in image_paths:
                if self.stop_requested: break
                idx += 1
                status_text = f"Tagging image {idx}/{total}   {os.path.basename(path)}"
                self.after(0, lambda txt=status_text: self.status_bar.set_action_message(txt))
                self.after(0, lambda val=idx/total: self.status_bar.set_progress(val))
                self.tag_single_image(path, plugin)

            for path in video_paths:
                if self.stop_requested: break
                idx += 1
                status_text = f"Tagging video {idx}/{total}   {os.path.basename(path)}"
                self.after(0, lambda txt=status_text: self.status_bar.set_action_message(txt))
                self.after(0, lambda val=idx/total: self.status_bar.set_progress(val))
                
                orig_passes = getattr(plugin, 'number_of_passes', 2)
                if hasattr(plugin, 'number_of_passes'): plugin.number_of_passes = 1
                
                # We run this synchronously inside this thread
                self.auto_tag_video_with_plugin(path, run_in_thread=False)
                
                if hasattr(plugin, 'number_of_passes'): plugin.number_of_passes = orig_passes

            # --- FIX: MessageBox MUST be called via self.after ---
            final_msg = f"{plugin_name} tagged {idx} files."
            if self.stop_requested:
                final_msg = f"Stopped! Tagged {idx} files."
                
            self.after(0, self.status_bar.clear_action_message)
            self.after(0, lambda: self.status_bar.set_progress(0))
            self.after(0, lambda: messagebox.showinfo("Done", final_msg))
            self.after(0, self.status_bar.disable_stop)

        threading.Thread(target=tag_worker, daemon=True).start()
        

    def get_plugin_name_for_engine(self):
        engine = getattr(self, "tagging_engine", "CLIP")
        return {
            "CLIP": "clip_yolo_plugin",
            "YOLO": "clip_yolo_plugin",
            "VIT":  "clip_yolo_plugin"
        }.get(engine, "clip_yolo_plugin")

