"""
Bottom status bar for Vibe Player.

Shows folder/file counts, selection size, progress, optional thumbnail-time controls,
and a stop action for long-running tasks (tagging, scanning).
"""

import customtkinter as ctk
import os
import logging


class StatusBar(ctk.CTkFrame):
    def __init__(self, parent):
        super().__init__(master=parent)
        self.main_app_window = parent   
        
        # Create a frame to hold the status label, progress bar, and stop button
        self.main_frame = ctk.CTkFrame(self)
        self.main_frame.pack(side=ctk.BOTTOM, fill=ctk.X)

        # Status label
        self.status_var = ctk.StringVar()
        self.status_label = ctk.CTkLabel(self.main_frame, textvariable=self.status_var, anchor="w")
        self.status_label.pack(side=ctk.LEFT, fill=ctk.X, expand=True)

        # Blue action label TEXTS INFO (for tasks like tagging)
        self.action_var = ctk.StringVar()
        self._action_info_color = "skyblue"
        self._action_error_color = "#ff6b6b"
        self.action_label = ctk.CTkLabel(
            self.main_frame,
            textvariable=self.action_var,
            anchor="w",
            text_color=self._action_info_color,
        )
        self.action_label.pack(side=ctk.LEFT, padx=(10, 20))

        # Stop button: idle = neutral gray; active (long-running task) = blue
        self._stop_bg_idle = "#4a4a4a"
        self._stop_hover_idle = "#4a4a4a"
        self._stop_text_idle = "#9a9a9a"
        self._stop_bg_active = "#3a7ebf"
        self._stop_hover_active = "#4a8fd4"
        self._stop_text_active = "#ffffff"
        self.stop_button = ctk.CTkButton(
            self.main_frame,
            text="Stop",
            state="disabled",
            width=75,
            height=19,
            command=self.stop_scan,
            fg_color=self._stop_bg_idle,
            hover_color=self._stop_hover_idle,
            text_color=self._stop_text_idle,
        )
        self.stop_button.pack(side=ctk.LEFT, padx=(15, 6))

        # Progress bar
        self.progress = ctk.CTkProgressBar(self.main_frame, orientation="horizontal", mode='determinate', width=270)
        self.progress.pack(side=ctk.LEFT, padx=20)
        self.progress.set(0)

        # Tiny Thumbnail Time toggle (experimental), now right after progress bar.
        self.thumb_time_open = False
        self.thumb_time_toggle = ctk.CTkButton(
            self.main_frame,
            text="▶",
            width=11,   # ~2x smaller than previous toolbar button
            height=13,
            fg_color="gray30",
            hover_color="gray40",
            text_color="gray80",
            command=self.toggle_thumb_time_panel,
        )
        self.thumb_time_toggle.pack(side=ctk.LEFT, padx=(2, 6))

        self.thumb_time_panel = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        initial_thumb_pct = self._get_thumb_time_percent()
        self.thumb_time_label_var = ctk.StringVar(value=f"T: {initial_thumb_pct}%")
        self.thumb_time_label = ctk.CTkLabel(
            self.thumb_time_panel,
            textvariable=self.thumb_time_label_var,
            width=42,
            anchor="w",
        )
        self.thumb_time_label.pack(side=ctk.LEFT, padx=(0, 4))
        # Keep a local variable to avoid startup crashes when main app has not
        # created thumbnail_time_var yet.
        self.local_thumb_time_var = ctk.IntVar(value=initial_thumb_pct)
        self.thumb_time_slider = ctk.CTkSlider(
            self.thumb_time_panel,
            from_=1,
            to=95,
            number_of_steps=94,
            variable=self.local_thumb_time_var,
            command=self.on_thumb_time_slider,
            width=120,
            height=12,
        )
        self.thumb_time_slider.pack(side=ctk.LEFT, padx=(0, 4))

        self.pack(side=ctk.BOTTOM, fill=ctk.X)
        
        self.stop_scan_flag = False
        self.stop_callback = None

    def set_message(self, text: str):
        self.set_status(text)

    def _get_thumb_time_percent(self) -> int:
        """Read current thumbnail time safely even during early startup."""
        try:
            var = getattr(self.main_app_window, "thumbnail_time_var", None)
            if var is not None:
                return int(var.get())
        except Exception:
            pass
        try:
            # Fallback to float ratio stored on app (0.0-1.0)
            ratio = float(getattr(self.main_app_window, "thumbnail_time", 0.1))
            return max(1, min(95, int(round(ratio * 100))))
        except Exception:
            return 10

    def on_thumb_time_slider(self, value):
        pct = int(float(value))
        self.thumb_time_label_var.set(f"T: {pct}%")
        try:
            self.main_app_window.update_thumbnail_time(float(value))
        except Exception:
            # If app is still booting, keep local value only.
            pass

    def toggle_thumb_time_panel(self):
        self.thumb_time_open = not self.thumb_time_open
        if self.thumb_time_open:
            pct = self._get_thumb_time_percent()
            self.local_thumb_time_var.set(pct)
            self.thumb_time_label_var.set(f"T: {pct}%")
            self.thumb_time_panel.pack(side=ctk.LEFT, padx=(0, 6))
            self.thumb_time_toggle.configure(text="◀")
        else:
            self.thumb_time_panel.pack_forget()
            self.thumb_time_toggle.configure(text="▶")

    def set_progress(self, value: float):
        self.after(0, self.progress.set, value)

    def set_action_message(self, text: str, color: str | None = None):
        self.action_label.configure(text_color=(color or self._action_info_color))
        self.action_var.set(text)

    def clear_action_message(self):
        self.action_label.configure(text_color=self._action_info_color)
        self.action_var.set("")

    def set_status(self, message):
        self.after(0, self.status_var.set, message)
    
    def update_progress(self, value):
        self.after(0, self.progress.set, value / 100)
        
    def reset_progress(self):
        self.after(0, self.progress.set, 0)

    def enable_stop(self):
        self.stop_button.configure(
            state="normal",
            fg_color=self._stop_bg_active,
            hover_color=self._stop_hover_active,
            text_color=self._stop_text_active,
        )

    def disable_stop(self):
        self.stop_button.configure(
            state="disabled",
            fg_color=self._stop_bg_idle,
            hover_color=self._stop_hover_idle,
            text_color=self._stop_text_idle,
        )

    def stop_scan(self):
        if self.stop_callback:
            self.stop_callback()
        self.stop_scan_flag = True
        self.disable_stop()
                              
    def set_stop_callback(self, callback):
        self.stop_callback = callback
        
    def update_status(self, folder_count, file_count, total_size, selected_count, selected_size):
        status_message = f"{folder_count} Folders | {file_count} files ({total_size:.2f} MB) | Selected: {selected_count} / {file_count} ({selected_size:.2f} MB)"
        self.set_status(status_message)
    
    def count_folders_and_files(self, dir_path):
        """Count folders, files, and total size under the given directory."""
        folder_count = 0
        file_count = 0
        total_size = 0
        
        for root, dirs, files in os.walk(dir_path):
            folder_count += len(dirs)
            file_count += len(files)
            for name in files:
                file_path = os.path.join(root, name)
                try:
                    total_size += os.path.getsize(file_path)
                except FileNotFoundError:
                    logging.debug("File not found (skip in size sum): %s", file_path)
                except PermissionError:
                    logging.info(f"Permission denied: {file_path}")
        
        return folder_count, file_count, total_size / (1024 * 1024)

    def count_selected_files_and_size(self, selected_thumbnails):
        """Return count and total size of selected thumbnail items."""
        total_bytes = 0
        present = 0
        for fp, _, _ in selected_thumbnails:
            try:
                total_bytes += os.path.getsize(fp)
                present += 1
            except (FileNotFoundError, OSError):
                continue
        selected_size = total_bytes / (1024 * 1024)
        return present, selected_size