"""
Right-side info panel for Vibe Player (metadata, tags, EXIF, video, database, preview).

Hosts tabbed metadata, optional embedded preview player, and multi-timeline controls.
"""

import customtkinter as ctk
import tkinter as tk
from database import Database
from video_operations import VideoPlayer
from PIL import Image, ImageTk
import os
db = Database()
import logging

import threading


class InfoPanelFrame(ctk.CTkFrame):
    def __init__(self, parent):
        super().__init__(parent, height=150)
        logging.debug("InfoPanel initializing")
        
        self.pack_propagate(False)
        self.collapsed = False

        self.content_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.content_frame.pack(fill="both", expand=True)

        self.tabs = ctk.CTkTabview(self.content_frame)
        self.tabs.pack(fill="both", expand=True, padx=5, pady=5)
        
        self.tab_preview = self.tabs.add("Preview")

        

        self.preview_player = None

        # Bottom bar: Auto Play + VIDEO/STRIPS toggle
        self._preview_bottom_bar = ctk.CTkFrame(self.tab_preview, fg_color="transparent", height=28)
        self._preview_bottom_bar.pack(side="bottom", fill="x", pady=(2, 4), padx=6)
        self._preview_bottom_bar.pack_propagate(False)

        # Initialize Auto Play checkbox
        self.preview_auto_play_var = ctk.BooleanVar(value=True)
        self.auto_play_checkbox = ctk.CTkCheckBox(
            self._preview_bottom_bar,
            text="Auto Play",
            variable=self.preview_auto_play_var,
            checkbox_width=14,
            checkbox_height=14,
            border_width=2,
            font=("Arial", 11),
            text_color="gray"
        )
        self.auto_play_checkbox.pack(side="left", padx=(4, 0))

        self.multiTimeline_limit_var = ctk.BooleanVar(value=True)
        self.multiTimeline_limit_checkbox = ctk.CTkCheckBox(
            self._preview_bottom_bar,
            text="Limit",
            variable=self.multiTimeline_limit_var,
            checkbox_width=14,
            checkbox_height=14,
            border_width=2,
            font=("Arial", 11),
            text_color="gray",
        )
        self.multiTimeline_limit_checkbox.pack(side="left", padx=(10, 0))
        self.multiTimeline_limit_checkbox.configure(
            state="disabled",
            text_color=("#3d3d3d", "#3d3d3d"),
            border_color=("#3d3d3d", "#3d3d3d"),
        )

        # VIDEO / STRIPS mode toggle
        self.preview_mode_var = ctk.StringVar(value="Video")
        self.preview_mode_switch = ctk.CTkSegmentedButton(
            self._preview_bottom_bar,
            values=["Video", "Strips"],
            variable=self.preview_mode_var,
            width=120,
            height=22,
            font=ctk.CTkFont(size=11),
            fg_color=("gray75", "gray25"),
            selected_color=("gray55", "gray40"),
            selected_hover_color=("gray50", "gray45"),
            unselected_color=("gray75", "gray25"),
            text_color=("gray20", "gray80"),
        )
        self.preview_mode_switch.pack(side="right", padx=(0, 4))
        
        self.tab_database = self.tabs.add("Database")
        self.database_labels = []

        # Tabs: GENERAL
        self.tab_general = self.tabs.add("General")
        self.label_name = ctk.CTkLabel(self.tab_general, text="Name:")
        self.label_size = ctk.CTkLabel(self.tab_general, text="Size:")
        self.label_dim = ctk.CTkLabel(self.tab_general, text="Resolution:")
        self.label_date = ctk.CTkLabel(self.tab_general, text="Modified:")

        for w in [self.label_name, self.label_size, self.label_dim, self.label_date]:
            w.pack(anchor="w", padx=10, pady=2)

        # Tabs: METADATA / Tags
        self.tab_tags = self.tabs.add("Tags")
        self.label_rating = ctk.CTkLabel(self.tab_tags, text="Rating:")
        self.label_keywords = ctk.CTkLabel(self.tab_tags, text="Keywords:")
        for w in [self.label_rating, self.label_keywords]:
            w.pack(anchor="w", padx=10, pady=2)

        # Tabs: EXIF
        self.tab_exif = self.tabs.add("EXIF")
        self.exif_labels = []

        # Tabs: VIDEO
        self.tab_video = self.tabs.add("Video")

        def _section(text):
            lbl = ctk.CTkLabel(self.tab_video, text=text,
                               font=("Arial", 10, "bold"), text_color="gray60")
            lbl.pack(anchor="w", padx=8, pady=(6, 1))
            return lbl

        def _row(text):
            lbl = ctk.CTkLabel(self.tab_video, text=text, anchor="w")
            lbl.pack(anchor="w", padx=16, pady=1)
            return lbl

        _section("── Video ──")
        self.label_codec       = _row("Codec: ?")
        self.label_duration    = _row("Duration: ?")
        self.label_fps         = _row("FPS: ?")
        self.label_compression = _row("Compression: ?")
        self.label_bitrate     = _row("Bitrate: ?")

        _section("── Color ──")
        self.label_bit_depth          = _row("Bit Depth: ?")
        self.label_color_space        = _row("Color Space: ?")
        self.label_chroma_subsampling = _row("Chroma: ?")

        _section("── Audio ──")
        self.label_audio_codec       = _row("Audio Codec: ?")
        self.label_audio_channels    = _row("Channels: ?")
        self.label_audio_sample_rate = _row("Sample Rate: ?")
        self.label_audio_bitrate     = _row("Audio Bitrate: ?")

        _section("── Details ──")
        self.label_scan_type         = _row("Scan Type: ?")
        self.label_encoder_library   = _row("Encoder: ?")
        self.label_compression_ratio = _row("Comp. Ratio: ?")

    def show_image_preview(self, image_path):
        # Stop video preview
        if self.preview_player is not None:
            self.preview_player.stop_video()
            self.preview_player.video_window.pack_forget()

        if hasattr(self, "preview_canvas") and self.preview_canvas is not None:
            try:
                if self.preview_canvas.winfo_exists():
                    self.preview_canvas.destroy()
            except Exception as e:
                logging.warning(f"InfoPanel: Error destroying previous canvas: {e}")
        self.preview_canvas = None

        image = Image.open(image_path)

        self.tab_preview.update_idletasks()
        target_width = self.tab_preview.winfo_width() - 20
        target_height = self.tab_preview.winfo_height() - 20

        if target_width <= 0: target_width = 400
        if target_height <= 0: target_height = 300

        img_w, img_h = image.size
        img_ratio = img_w / img_h if img_h != 0 else 1
        panel_ratio = target_width / target_height

        if panel_ratio > img_ratio:
            new_height = target_height
            new_width = int(new_height * img_ratio)
        else:
            new_width = target_width
            new_height = int(new_width / img_ratio)

        image = image.resize((new_width, new_height), Image.LANCZOS)

        self.preview_image_tk = ImageTk.PhotoImage(image)
        self.preview_canvas = tk.Label(self.tab_preview, image=self.preview_image_tk, bg="black")
        self.preview_canvas.pack(padx=10, pady=10, anchor="center")


                    
                    

    def start_video_preview(self, video_path):
        """
        Starts video preview in the info panel. 
        Checks the auto-play variable: if disabled, pauses the video right after loading the first frame
        using a delayed force pause to avoid VLC engine race conditions.
        """
        self._pending_preview_path = video_path

        def load_media_and_prepare():
            vp = video_path
            if getattr(self, "_pending_preview_path", None) != vp:
                return
            media = None
            try:
                media = self.preview_player.instance.media_new(vp)
            except Exception as e:
                logging.info("[THREAD] VLC media_new failed: %s", e)
                media = None

            if getattr(self, "_pending_preview_path", None) != vp:
                if media is not None:
                    try:
                        media.release()
                    except Exception:
                        pass
                logging.debug("[THREAD] Preview cancelled before embed (path=%s).", vp)
                return

            def embed_and_play_in_gui():
                if getattr(self, "_pending_preview_path", None) != vp:
                    if media is not None:
                        try:
                            media.release()
                        except Exception:
                            pass
                    logging.info("[GUI] Preview request obsolete (user clicked elsewhere).")
                    return

                frame = self.preview_player.video_window
                if not frame.winfo_exists():
                    logging.info("[GUI] Preview video_window does not exist.")
                    return

                try:
                    # Remove previous image preview if exists
                    if getattr(self, "preview_canvas", None) is not None:
                        self.preview_canvas.destroy()
                        self.preview_canvas = None

                    # Show video window if hidden
                    if not frame.winfo_ismapped():
                        frame.pack(expand=True, fill="both")

                    if media is not None:
                        self.preview_player.player.set_media(media)
                        frame.update()
                        self.preview_player.player.set_hwnd(frame.winfo_id())
                        self.preview_player.player.audio_set_volume(0)
                        
                        # Start playback to load the first frame
                        self.preview_player.player.play()
                        self.preview_player.playing = True

                        # Pause after a short delay if Auto Play is disabled
                        # The delay must be long enough for VLC to actually start decoding
                        if hasattr(self, "preview_auto_play_var") and not self.preview_auto_play_var.get():
                            def force_pause():
                                if self.preview_player and self.preview_player.player:
                                    self.preview_player.player.pause()
                                    self.preview_player.playing = False
                            
                            # Increased timeout to 400ms to prevent race conditions with VLC engine
                            self.preview_player.video_window.after(400, force_pause)

                        # Bind play/pause toggle
                        if hasattr(self.preview_player, "video_label"):
                            self.preview_player.video_label.bind("<Button-1>", self.preview_player.toggle_play)
                        self.preview_player.video_window.bind("<Button-1>", self.preview_player.toggle_play)
                    else:
                        self.show_preview_placeholder("Failed to load video.")

                except Exception as e:
                    logging.info("[GUI] set_hwnd/play failed:%s ", e)
                    self.show_preview_placeholder("Error playing video.")

            self.preview_player.video_window.after(0, embed_and_play_in_gui)

        threading.Thread(target=load_media_and_prepare, daemon=True).start()




    def stop_video_preview(self):
        """Stop current video preview, if running."""
        pp = getattr(self, "preview_player", None)
        if pp is None:
            self._pending_preview_path = None
            return
        try:
            if hasattr(pp, "release_held_media"):
                pp.release_held_media()
            elif hasattr(pp, "stop_video"):
                pp.stop_video()
        except Exception as e:
            logging.debug("[InfoPanel] stop_video_preview: %s", e)
        self._pending_preview_path = None

    def show_preview_placeholder(self, text="Loading preview..."):
        """Show placeholder in the preview panel (always uses preview_player.video_window)."""
        frame = getattr(self, "preview_player", None)
        if not frame or not hasattr(self.preview_player, "video_window"):
            logging.info("[WARN] InfoPanel has no preview_player.video_window — placeholder not shown.")
            return

        video_frame = self.preview_player.video_window
        for child in video_frame.winfo_children():
            child.destroy()
        label = tk.Label(video_frame, text=text, fg="gray", bg="black")
        label.pack(expand=True, fill="both")







    def update_info(self, metadata, rating=None, keywords=None):
        # === General ===
        self.label_name.configure(text=f"Name: {metadata.get('name', '?')}")
        size = metadata.get("file_size")
        self.label_size.configure(text=f"Size: {size / (1024 * 1024):.2f} MB" if size else "Size: ?")

        if metadata.get("width") and metadata.get("height"):
            self.label_dim.configure(text=f"Resolution: {metadata['width']} x {metadata['height']}")
        else:
            self.label_dim.configure(text="Resolution: ?")

        self.label_date.configure(text=f"Modified: {metadata.get('modified', '?')}")

        # === Tags ===
        self.label_rating.configure(text=f"Rating: {rating if rating is not None else 'Unknown'}")

        if isinstance(keywords, list):
            keywords_final = ", ".join(keywords)
        else:
            keywords_final = str(keywords) if keywords else "None"

        self.label_keywords.configure(text=f"Keywords: {keywords_final}")

        # === Video ===
        duration = metadata.get("duration")
        self.label_duration.configure(text=f"Duration: {duration:.1f} s" if duration else "Duration: ?")
        # Rich video metadata (codec/fps/bitrate) is populated separately via update_video_tab()
        # when the Video tab is active — avoid overwriting with '?' if already filled
        if metadata.get("codec"):
            self.label_codec.configure(text=f"Codec: {metadata.get('codec')}")
        if metadata.get("fps"):
            self.label_fps.configure(text=f"FPS: {metadata.get('fps')}")
        if metadata.get("compression"):
            self.label_compression.configure(text=f"Compression: {metadata.get('compression')}")
        if metadata.get("bitrate"):
            self.label_bitrate.configure(text=f"Bitrate: {metadata.get('bitrate')}")
        if metadata.get("audio_bitrate"):
            self.label_audio_bitrate.configure(text=f"Audio Bitrate: {metadata.get('audio_bitrate')}")

        # === EXIF ===
        for lbl in self.exif_labels:
            lbl.destroy()
        self.exif_labels.clear()
        exif = metadata.get("exif")
        if exif:
            for k, v in exif.items():
                lbl = ctk.CTkLabel(self.tab_exif, text=f"{k}: {v}", anchor="w")
                lbl.pack(anchor="w", padx=10, pady=1)
                self.exif_labels.append(lbl)
        else:
            lbl = ctk.CTkLabel(self.tab_exif, text="No EXIF data", anchor="w")
            lbl.pack(anchor="w", padx=10)
            self.exif_labels.append(lbl)

        # === DATABASE TAB ===
        for lbl in self.database_labels:
            lbl.destroy()
        self.database_labels.clear()

        db_fields = [
            ("Filename", metadata.get("filename")),
            ("Path", metadata.get("file_path")),
            ("Resolution", f"{metadata.get('width')} x {metadata.get('height')}"),
            ("Rating", rating),
            ("Keywords", keywords_final),
            ("Cached", "Yes" if metadata.get("is_cached") else "No"),
            ("Thumb Time", metadata.get("thumbnail_timestamp")),
        ]

        for label, value in db_fields:
            lbl = ctk.CTkLabel(self.tab_database, text=f"{label}: {value}", anchor="w")
            lbl.pack(anchor="w", padx=10, pady=1)
            self.database_labels.append(lbl)

    def update_video_tab(self, video_info):
        """Update only the Video tab with rich metadata.
        Called from background extraction when the Video tab is active."""
        def _v(key, fallback="?"):
            val = video_info.get(key)
            return val if val is not None else fallback

        # Video
        self.label_codec.configure(text=f"Codec: {_v('codec')}")
        self.label_fps.configure(text=f"FPS: {_v('fps')}")
        self.label_compression.configure(text=f"Compression: {_v('compression')}")
        bitrate = video_info.get("bitrate")
        self.label_bitrate.configure(
            text=f"Bitrate: {bitrate // 1000} kbps" if bitrate else "Bitrate: ?"
        )

        # Color
        bit_depth = video_info.get("bit_depth")
        self.label_bit_depth.configure(
            text=f"Bit Depth: {bit_depth}-bit" if bit_depth else "Bit Depth: ?"
        )
        self.label_color_space.configure(text=f"Color Space: {_v('color_space')}")
        self.label_chroma_subsampling.configure(text=f"Chroma: {_v('chroma_subsampling')}")

        # Audio
        self.label_audio_codec.configure(text=f"Audio Codec: {_v('audio_codec')}")
        self.label_audio_channels.configure(text=f"Channels: {_v('audio_channels')}")
        self.label_audio_sample_rate.configure(text=f"Sample Rate: {_v('audio_sample_rate')}")
        audio_br = video_info.get("audio_bitrate")
        self.label_audio_bitrate.configure(
            text=f"Audio Bitrate: {audio_br // 1000} kbps" if audio_br else "Audio Bitrate: ?"
        )

        # Details
        self.label_scan_type.configure(text=f"Scan Type: {_v('scan_type')}")
        encoder = video_info.get("encoder_library")
        self.label_encoder_library.configure(
            text=f"Encoder: {encoder[:40] if encoder and len(encoder) > 40 else _v('encoder_library')}"
        )
        ratio = video_info.get("compression_ratio")
        self.label_compression_ratio.configure(
            text=f"Comp. Ratio: {ratio}:1" if ratio else "Comp. Ratio: ?"
        )

        logging.info("[INFO] Video tab updated: codec=%s fps=%s bitrate=%s bit_depth=%s",
                     video_info.get("codec"), video_info.get("fps"),
                     video_info.get("bitrate"), video_info.get("bit_depth"))

    def reset_video_tab(self):
        """Reset all Video tab fields to '?' when a new file is selected."""
        self.label_codec.configure(text="Codec: ?")
        self.label_fps.configure(text="FPS: ?")
        self.label_compression.configure(text="Compression: ?")
        self.label_bitrate.configure(text="Bitrate: ?")
        self.label_bit_depth.configure(text="Bit Depth: ?")
        self.label_color_space.configure(text="Color Space: ?")
        self.label_chroma_subsampling.configure(text="Chroma: ?")
        self.label_audio_codec.configure(text="Audio Codec: ?")
        self.label_audio_channels.configure(text="Channels: ?")
        self.label_audio_sample_rate.configure(text="Sample Rate: ?")
        self.label_audio_bitrate.configure(text="Audio Bitrate: ?")
        self.label_scan_type.configure(text="Scan Type: ?")
        self.label_encoder_library.configure(text="Encoder: ?")
        self.label_compression_ratio.configure(text="Comp. Ratio: ?")


