"""
Timeline bar UI (scrubber, loop in/out, export) for Vibe Player.

Hosts ``TimelineBarWidget`` and export dialogs on top of strip thumbnails.
"""

import json
import logging
import math
import os
import queue
import threading
import tkinter as tk
from functools import partial
from tkinter import filedialog, messagebox

import customtkinter as ctk
import cv2
from PIL import Image, ImageDraw, ImageFont, ImageTk

from file_operations import create_video_thumbnail, get_video_duration_mediainfo
from utils import create_menu, parse_srt_file


class VideoExportDialog(ctk.CTkToplevel):
    """
    Simple dialog for selecting video export preset or custom settings.
    """
    def __init__(self, parent, video_path, convert_callback, loop_start=None, loop_end=None):
            """
            Initializes the export dialog. 
            Sets up the UI elements, variables, and dynamically adjusts window size.
            """
            super().__init__(parent)
            self.title("Export Video")
            
            self.loop_mode = False
            self.fill_timeline_gaps = True  # Enables continuous filmstrip by default

            self.geometry("400x550") 
            self.video_path = video_path
            self.convert_callback = convert_callback
            self.loop_start = loop_start
            self.loop_end = loop_end
            
            self.resizable(True, True) 

            self.presets = {
                "MP4 1600x1200 HQ": { "ext": ".mp4", "width": 1600, "height": 1200, "fps": 30 },
                "MP4 1280x720":     { "ext": ".mp4", "width": 1280, "height": 720,  "fps": 30 },
                "AVI 640x480":      { "ext": ".avi", "width": 640,  "height": 480,  "fps": 25 },
            }

            self.preset_var = ctk.StringVar(value=list(self.presets.keys())[0])
            self.ext_var = ctk.StringVar(value=".mp4")
            self.width_var = ctk.StringVar(value="1600")
            self.height_var = ctk.StringVar(value="1200")
            self.fps_var = ctk.StringVar(value="30")
            self.sound_var = ctk.BooleanVar(value=True)

            ctk.CTkLabel(self, text="Choose preset:").pack(pady=(10, 5))
            self.preset_menu = ctk.CTkOptionMenu(self, variable=self.preset_var, values=list(self.presets.keys()), command=self.apply_preset)
            self.preset_menu.pack(pady=5)

            ctk.CTkLabel(self, text="Custom settings:").pack(pady=(15, 5))

            form_frame = ctk.CTkFrame(self)
            form_frame.pack(pady=5, padx=10, fill="x")

            self._add_entry(form_frame, "Width:", self.width_var)
            self._add_entry(form_frame, "Height:", self.height_var)
            self._add_entry(form_frame, "FPS:", self.fps_var)
            
            self.supported_formats = [".mp4", ".avi", ".mkv", ".mov", ".webm"]
            
            ctk.CTkLabel(self, text="Output Format:").pack(pady=(10, 2))
            self.format_menu = ctk.CTkOptionMenu(self, variable=self.ext_var, values=self.supported_formats)
            self.format_menu.pack(pady=2)

            ctk.CTkCheckBox(self, text="Include audio (not supported yet)", variable=self.sound_var, state="disabled").pack(pady=5)

            if self.loop_start is not None and self.loop_end is not None:
                ctk.CTkLabel(self, text=f"Exporting selection: {int(self.loop_start)}s - {int(self.loop_end)}s", text_color="#00bfff").pack(pady=5)

            # TOTO TLAČIDLO TERAZ UVIDÍŠ!
            ctk.CTkButton(self, text="Start Export", command=self.start_export).pack(pady=15)

            self.apply_preset(self.preset_var.get())
            self.lift()
            self.focus_force()
            self.grab_set()
            self.transient(self.master)

    def _add_entry(self, frame, label, var):
        row = ctk.CTkFrame(frame)
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(row, text=label, width=120, anchor="w").pack(side="left")
        ctk.CTkEntry(row, textvariable=var).pack(side="left", fill="x", expand=True)

    def apply_preset(self, preset_name):
        preset = self.presets[preset_name]
        self.ext_var.set(preset["ext"])
        self.width_var.set(str(preset["width"]))
        self.height_var.set(str(preset["height"]))
        self.fps_var.set(str(preset["fps"]))

    def start_export(self):
        try:
            settings = {
                "ext": self.ext_var.get(),
                "width": int(self.width_var.get()),
                "height": int(self.height_var.get()),
                "fps": float(self.fps_var.get()),
                "start_time": self.loop_start,
                "end_time": self.loop_end
            }
            self.convert_callback(self.video_path, settings)
            self.destroy()
        except Exception as e:
            messagebox.showerror("Invalid input", str(e))



class TimelineBarWidget(ctk.CTkFrame):
    def __init__(self, parent,  controller,video_path, timeline_manager, on_seek=None):
        super().__init__(parent)

        # ---- THREADING SETUP ----
        self.thumb_queue = queue.Queue()
        self.worker_thread = None
        
        self.video_path = video_path
        self.markers = []
        self.marker_types_visible = {"thumbnail": True, "subtitle": True, "tag": True, "bookmark": True}
        self.zoom_factor = 1.0
        self.min_zoom = 0.2
        self.max_zoom = 5.0
        self.pan_offset = 0.0
        self._pan_start_x = None
        self._pan_start_offset = 0.0
        self.BackroundColor = "#2b2b2b"
        self.thumb_TextColor = "#dddddd"
        self.timeline_manager = timeline_manager
        # Načteme aktuální velikost z manažeru
        self.THUMB_W, self.THUMB_H = self.timeline_manager.thumbnail_size
        # Přidáme proměnnou pro ukládání volby velikosti v menu
        initial_size_str = f"{self.THUMB_W}x{self.THUMB_H}"
        self.thumb_size_var = tk.StringVar(value=initial_size_str)
        self.parent = parent
        self.controller = controller
        self.num_thumbs = 5
        self.on_seek = on_seek
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)
        self.grid_columnconfigure(0, weight=1)
        self.loop_mode = False
        self.loop_drag = None
        self.loop_start = 0
        self.loop_end = None
        
        self.thumb_images = [] # This will now store PhotoImage objects.
        # self.THUMB_W, self.THUMB_H = 190, 130

        self.create_widgets()
       
        self.current_time = 0
        self.snap_types = ["none", "tick", "thumb"]
        self.snap_type = "none"
        self.magnet_mode = False
        self.toggle_all_label = tk.StringVar(value="Show No Markers")
        self.rotated_text_refs = []

        self.marker_canvas_ids = {}
        self.start_periodic_update()
        self._process_thumb_queue() # Start the queue checker loop

    def toggle_all_markers(self):
        any_off = any(not var.get() for var in self.marker_vars.values())
        for key, var in self.marker_vars.items():
            var.set(any_off)
            self.marker_types_visible[key] = any_off
        self.toggle_all_label.set("Show No Markers" if any_off else "Show All Markers")
        self.redraw_timeline()

    def generate_thumbnail_at_time(self, timestamp: float):
        """Generate and save a thumbnail for the current video at the given timestamp (seconds)."""
        video_path = self.video_path
        if not video_path or not os.path.isfile(video_path):
            logging.warning("generate_thumbnail_at_time: no valid video path.")
            return

        ctrl = self.controller
        labels = getattr(ctrl, "thumbnail_labels", {}) or {}
        thumbnail_info = labels.get(video_path)
        if not thumbnail_info:
            nv = os.path.normcase(os.path.normpath(video_path))
            for key, info in labels.items():
                try:
                    if os.path.normcase(os.path.normpath(key)) == nv:
                        thumbnail_info = info
                        video_path = key
                        break
                except Exception:
                    continue
        if not thumbnail_info:
            logging.warning("generate_thumbnail_at_time: %s not found in thumbnail_labels.", video_path)
            return

        row = thumbnail_info["row"]
        col = thumbnail_info["col"]
        index = thumbnail_info["index"]

        if hasattr(ctrl, "database"):
            ctrl.database.set_thumbnail_timestamp(video_path, float(timestamp))

        if getattr(ctrl, "_vg_active", False):
            ctrl.refresh_single_thumbnail(
                video_path, overwrite=True, at_time=float(timestamp)
            )
            logging.info(
                "Thumbnail generated at %s for %s (virtual grid)",
                self.format_time(timestamp),
                os.path.basename(video_path),
            )
            return

        thumbnail = create_video_thumbnail(
            video_path=video_path,
            thumbnail_size=ctrl.thumbnail_size,
            thumbnail_format=ctrl.thumbnail_format,
            capture_method=ctrl.capture_method_var.get(),
            thumbnail_time=timestamp,
            cache_enabled=ctrl.cache_enabled,
            overwrite=True,
            cache_dir=ctrl.thumbnail_cache_path,
            database=getattr(ctrl, "database", None),
        )

        if not thumbnail:
            logging.warning("generate_thumbnail_at_time: thumbnail creation failed for %s", video_path)
            return

        ctrl.create_file_thumbnail(
            file_path=video_path,
            file_name=os.path.basename(video_path),
            row=row,
            col=col,
            index=index,
            thumbnail_time=timestamp,
            overwrite=True,
            target_frame=ctrl.regular_thumbnails_frame,
        )
        logging.info(f"Thumbnail generated at {self.format_time(timestamp)} for {os.path.basename(video_path)}")

    def show_context_menu(self, event):
        """Displays the right-click context menu on the timeline."""
        clicked_time = self.get_time_at_x(event.x)
        duration = self._get_current_duration()
        active_player = getattr(self.controller, "current_video_window", None)

        menu = tk.Menu(self, tearoff=0, bg="#2b2b2b", fg="white", activebackground="#444")

        # --- PLAYBACK ---
        menu.add_command(label=f"Play from here ({self.format_time(clicked_time)})",
                         command=lambda: self.seek_and_play(clicked_time))
        menu.add_separator()

        # --- THUMBNAIL ---
        has_thumb_info = bool(
            self.video_path and
            os.path.isfile(self.video_path) and
            getattr(self.controller, 'thumbnail_labels', {}).get(self.video_path)
        )
        menu.add_command(
            label=f"📷 Generate thumbnail at {self.format_time(clicked_time)}",
            command=lambda t=clicked_time: self.generate_thumbnail_at_time(t),
            state="normal" if has_thumb_info else "disabled",
        )
        menu.add_separator()

        # --- LOOPING ---
        if active_player:
            def cmd_set_loop_start(t=clicked_time):
                active_player.loop_active = True
                self.loop_mode = True
                if hasattr(self, "loop_button"): self.loop_button.config(text="🔁 Loop: ON")
                
                if getattr(active_player, "loop_end", None) is None:
                    active_player.loop_end = getattr(self, "loop_end", None) or (min(t + 1.0, duration) if duration else t + 1.0)
                    
                active_player.set_loop_start_from_timeline(t)
                self.loop_start = active_player.loop_start
                self.loop_end = active_player.loop_end
                self.redraw_timeline()

            def cmd_set_loop_end(t=clicked_time):
                active_player.loop_active = True
                self.loop_mode = True
                if hasattr(self, "loop_button"): self.loop_button.config(text="🔁 Loop: ON")
                
                if getattr(active_player, "loop_start", None) is None:
                    active_player.loop_start = getattr(self, "loop_start", None) or max(t - 1.0, 0.0)
                    
                active_player.set_loop_end_from_timeline(t)
                self.loop_start = active_player.loop_start
                self.loop_end = active_player.loop_end
                self.redraw_timeline()

                
            def cmd_activate_selection():
                # Vezme naši pasivní modrou selekci a fyzicky ji pošle do přehrávače
                active_player.loop_active = True
                self.loop_mode = True
                if hasattr(self, "loop_button"): self.loop_button.config(text="🔁 Loop: ON")
                active_player.set_loop_start_from_timeline(self.loop_start)
                active_player.set_loop_end_from_timeline(self.loop_end)

            menu.add_command(label="Set LOOP START", command=cmd_set_loop_start)
            menu.add_command(label="Set LOOP END", command=cmd_set_loop_end)
            
            # Pokud máme vybranou modrou selekci, nabídneme její zasmartování
            if getattr(self, "loop_start", None) is not None and getattr(active_player, "loop_start", None) is None:
                menu.add_command(label="🔁 Loop Current Selection", command=cmd_activate_selection)

            loop_state = "Disable" if getattr(active_player, "loop_active", False) else "Enable"
            if hasattr(active_player, "toggle_loop"):
                menu.add_command(label=f"{loop_state} LOOP", command=active_player.toggle_loop)

        # --- EXPORT SELECTION ---
        loop_s = getattr(active_player, "loop_start", None) if active_player else getattr(self, "loop_start", None)
        loop_e = getattr(active_player, "loop_end", None) if active_player else getattr(self, "loop_end", None)
        
        # Pojistka pro pripad, ze prehravac ma smazany loop_start (ma hodnotu None), tak vezmeme lokální
        if not loop_s and self.loop_start: loop_s = self.loop_start
        if not loop_e and self.loop_end: loop_e = self.loop_end
        
        if loop_s is not None and loop_e is not None:
            menu.add_separator()
            menu.add_command(
                label=f"🎬 Export Selection ({self.format_time(loop_s)} - {self.format_time(loop_e)})", 
                command=lambda s=loop_s, e=loop_e: self.open_export_dialog(self.video_path, s, e)
            )

        menu.add_separator()

        # --- BOOKMARKS ---
        menu.add_command(label="Add Bookmark", command=lambda: self.add_bookmark_at(clicked_time))
        
        closest_marker = self.get_closest_marker(clicked_time, threshold=2.0)
        if closest_marker and closest_marker["type"] == "bookmark":
            menu.add_command(label=f"Remove Bookmark: {closest_marker['label']}", 
                             command=lambda: self.remove_bookmark_at(closest_marker))
        
        menu.add_separator()
        menu.add_command(label="Copy Timestamp", 
                         command=lambda: self.controller.clipboard_clear() or self.controller.clipboard_append(self.format_time(clicked_time)))

        menu.tk_popup(event.x_root, event.y_root)
   
   

    def add_bookmark_at(self, timestamp):
            """Adds bookmark even if the player window is closed."""
            # 🟢 Zjistíme, jestli máme přehrávač
            active_player = getattr(self.controller, "current_video_window", None)
            default_name = f"Marker {len(self.markers) + 1}"

            def on_confirm(name):
                name = name.strip()
                if not name: return

                if active_player:
                    # SCÉNÁŘ A: Přehrávač běží (standardní cesta)
                    active_player.bookmarks.append({"name": name, "time": timestamp})
                    active_player.save_bookmarks()
                else:
                    # SCÉNÁŘ B: Přehrávač JE ZAVŘENÝ
                    # 1. Načteme stávající záložky z disku
                    current_bookmarks = self.load_bookmarks_for_path(self.video_path)
                    # 2. Přidáme novou
                    current_bookmarks.append({"name": name, "time": timestamp})
                    # 3. Uložíme je zpět (použijeme pomocnou funkci níže)
                    self.save_bookmarks_standalone(self.video_path, current_bookmarks)
                
                # V obou případech aktualizujeme Timeline
                self.update_bookmarks()
                self.redraw_timeline()
                logging.info(f"Bookmark '{name}' added at {timestamp}s (Player active: {active_player is not None})")

            # Spustíme dialog
            if hasattr(self.controller, 'universal_dialog'):
                self.controller.universal_dialog(
                    title="Add Bookmark",
                    message=f"Name for bookmark at {self.format_time(timestamp)}:",
                    confirm_callback=on_confirm,
                    input_field=True,
                    default_input=default_name
                )

    def save_bookmarks_standalone(self, video_path, bookmarks):
            """Saves bookmarks to the CORRECT _bookmarks.json file."""
            import json
            # OPRAVA: Musí se shodovat s load_bookmarks_for_path!
            json_path = os.path.splitext(video_path)[0] + "_bookmarks.json"
            try:
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(bookmarks, f, indent=4, ensure_ascii=False)
                logging.info(f"[Timeline] Standalone save successful: {json_path}")
            except Exception as e:
                logging.error(f"Failed to save bookmarks standalone: {e}")

    def remove_bookmark_at(self, marker_to_remove):
            """Removes a specific bookmark even if player is closed."""
            active_player = getattr(self.controller, "current_video_window", None)
            timestamp = marker_to_remove["timestamp"]

            if active_player:
                active_player.bookmarks = [b for b in active_player.bookmarks if b["time"] != timestamp]
                active_player.save_bookmarks()
            else:
                # Práce bez přehrávače
                current_bookmarks = self.load_bookmarks_for_path(self.video_path)
                current_bookmarks = [b for b in current_bookmarks if b["time"] != timestamp]
                self.save_bookmarks_standalone(self.video_path, current_bookmarks)

            self.update_bookmarks()
            self.redraw_timeline()

    def seek_and_play(self, timestamp):
            """Seeks to timestamp. Opens player if it's closed."""
            active_player = getattr(self.controller, "current_video_window", None)
            
            if not active_player:
                # 🟢 Přehrávač neběží -> Spustíme ho
                video_name = os.path.basename(self.video_path)
                self.controller.open_video_player(self.video_path, video_name)
                
                # Musíme chvilku počkat, než se okno zinicializuje, pak seekneme
                # 300ms by mělo stačit pro vytvoření instance přehrávače
                self.after(300, lambda: self._delayed_seek(timestamp))
            else:
                # Přehrávač běží -> Jen seekneme a hrajeme
                if self.on_seek:
                    self.on_seek(timestamp)
                if hasattr(active_player, 'play_video'):
                    active_player.play_video()

    def _delayed_seek(self, timestamp):
        """Helper for seek after player opens."""
        if self.on_seek:
            self.on_seek(timestamp)
        active_player = getattr(self.controller, "current_video_window", None)
        if active_player and hasattr(active_player, 'play_video'):
            active_player.play_video()

    def get_closest_marker(self, timestamp, threshold=2.0):
        if not hasattr(self, "markers") or not self.markers:
            return None
        bookmarks = [m for m in self.markers if m["type"] == "bookmark"]
        if not bookmarks: return None
        closest = min(bookmarks, key=lambda m: abs(m["timestamp"] - timestamp))
        if abs(closest["timestamp"] - timestamp) <= threshold:
            return closest
        return None


    def create_widgets(self):
        """
        Initializes and lays out the UI widgets for the timeline bar.
        This includes the canvas for the timeline display and control buttons.
        """
        duration = self.timeline_manager.get_video_duration(self.video_path)
        if duration is not None:
            self.loop_end = duration
        else:
            self.loop_end = 1  # Default loop end if duration is not available.

        # ---  INFO TOOLBAR ( GRID) ---
        # corner_radius=0 a padx=0 to nalepí úplně do stran
        # self.toolbar_frame = ctk.CTkFrame(self, height=28, fg_color="#1e3a5f", corner_radius=0)
        # self.toolbar_frame.grid(row=0, column=0, columnspan=6, sticky="new", padx=0, pady=0)
        # self.toolbar_frame.pack_propagate(False)  # Zabrání vertikálnímu roztahování rámečku!
        
        # self.info_label = ctk.CTkLabel(self.toolbar_frame, text="Loading info...", anchor="w", font=("Segoe UI", 12, "bold"))
        # self.info_label.pack(side="left", fill="x", expand=True, padx=10)

        # self.selection_label = ctk.CTkLabel(self.toolbar_frame, text="", anchor="e", font=("Segoe UI", 12, "bold"), text_color="#FFA500")
        # self.selection_label.pack(side="right", padx=10)
        # ------------------------------------
        # ------------------------------------
        
        
        # --- NOVÝ INFO TOOLBAR (Transparentní + Linka vespod) ---
        self.toolbar_frame = ctk.CTkFrame(self, height=28, fg_color="#222222", corner_radius=0)
        self.toolbar_frame.grid(row=0, column=0, columnspan=6, sticky="new", padx=0, pady=0)
        self.toolbar_frame.pack_propagate(False)  # Zabrání vertikálnímu roztahování rámečku
        
        # Tenká šedá linka na spodní hraně toolbaru (výška 1 pixel)
        self.separator_line = tk.Frame(self.toolbar_frame, bg="#444444", height=1)
        self.separator_line.pack(side="bottom", fill="x")

        # Standardní, méně výrazný font (stejný jako zbytek appky)
        toolbar_font = ("Segoe UI", 10)

        self.info_label = ctk.CTkLabel(self.toolbar_frame, text="Loading info...", anchor="w", font=toolbar_font)
        self.info_label.pack(side="left", fill="x", expand=True, padx=10, pady=(0, 2))

        self.selection_label = ctk.CTkLabel(self.toolbar_frame, text="", anchor="e", font=toolbar_font, text_color="#FFA500")
        self.selection_label.pack(side="right", padx=10, pady=(0, 2))

        # Create the main canvas for drawing the timeline (Posunuto do row=1)
        self.canvas = tk.Canvas(self, width=900, height=280, bg="#222", highlightthickness=0)
        self.canvas.grid(row=1, column=0, columnspan=6, sticky="nsew")
        
        # Bind mouse events for interaction.
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.canvas.bind("<Button-1>", self.on_canvas_primary_click)
        self.canvas.bind("<Shift-MouseWheel>", self.on_ctrl_mousewheel)
        self.canvas.bind("<Button-2>", self.on_pan_start)
        self.canvas.bind("<B2-Motion>", self.on_pan_drag)
        self.canvas.bind("<ButtonRelease-2>", self.on_pan_end)
        self.canvas.bind("<Shift-Button-1>", self.on_shift_click)
        self.canvas.bind("<Shift-B1-Motion>", self.on_canvas_drag)
        
        self.canvas.bind("<Button-3>", self.show_context_menu)
        self.canvas.bind("<Motion>", self.on_mouse_move)
        self.canvas.bind("<space>", self.on_space_press)
        self.canvas.bind("<Enter>", lambda e: self.canvas.focus_set())

        # Define button styling.
        btn_style = {
            "font": ("Segoe UI", 10),
            "fg": "#dddddd",
            "bg": "#333333",
            "activebackground": "#444444",
            "activeforeground": "white",
            "relief": "flat",
            "padx": 10,
            "pady": 4,
            "width": 14
        }

        # Create and pack control buttons (Posunuto do row=2).
        self.loop_button = tk.Button(self, text="🔁 Loop: off", command=self.on_loop_button_click, **btn_style)
        self.loop_button.grid(row=2, column=0, pady=(2, 2))

        self.snap_btn = tk.Button(self, text="📐 SNAP: none", command=self.toggle_snap_type, **btn_style)
        self.snap_btn.grid(row=2, column=1, pady=(2, 2))

        self.magnet_btn = tk.Button(self, text="🧲 MAGNET: off", command=self.toggle_magnet, **btn_style)
        self.magnet_btn.grid(row=2, column=2, pady=(2, 2))

        self.grid_columnconfigure(3, weight=1)  # Configure column 3 to expand.
        self.grid_rowconfigure(0, weight=0)     # Toolbar nahoře se NESMÍ natahovat
        self.grid_rowconfigure(1, weight=1)     # Canvas (timeline) uprostřed se MUSÍ natahovat
        self.grid_rowconfigure(2, weight=0)     # Tlačítka dole se NESMÍ natahovat

        self.options_btn = tk.Button(self, text="⚙ Options", command=self.show_options_menu, **btn_style)
        self.options_btn.grid(row=2, column=4, pady=(2, 2))

        self.convert_btn = tk.Button(self, text="⏎ Convert", command=self.show_convert_menu, **btn_style)
        self.convert_btn.grid(row=2, column=5, pady=(2, 2))
        
        self.load_thumbnails()  # Initial load
        self.redraw_timeline()  # Redraw the timeline
        
        # První aktualizace panelu
        self.update_info_toolbar()

   
            
            
    def update_info_toolbar(self):
            """
            Updates the top info toolbar with the current video filename, format, 
            playback time, total duration, and current loop selection length.
            """
            if not hasattr(self, 'info_label') or not hasattr(self, 'selection_label'):
                return

            if not self.video_path:
                self.info_label.configure(text="No video selected")
                self.selection_label.configure(text="")
                return
            
            filename = os.path.basename(self.video_path)
            ext = os.path.splitext(filename)[1].upper().replace(".", "")
            
            # Duration fallback (since loop_end starts as duration)
            dur = getattr(self, 'loop_end', 0)
            
            cur_time_str = self.format_time(self.current_time) if self.current_time else "00:00:00.000"
            dur_str = self.format_time(dur) if dur > 1 else "00:00:00.000"
            
            cur_time_short = cur_time_str.split('.')[0]
            dur_short = dur_str.split('.')[0]

            info_text = f"{filename} [{ext}]  •  {cur_time_short} / {dur_short}"
            self.info_label.configure(text=info_text)
            
            # --- NOVÁ LOGIKA PRO ZOBRAZENÍ SMYČKY (LOOP) ---
            active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)
            loop_active = getattr(active_player, "loop_active", False) if active_player else False
            
            if self.loop_start is not None and self.loop_end is not None:
                sel_len = max(0, self.loop_end - self.loop_start)
                # Neukazovat selekci, pokud je přes celé video (není udělaný reálný výběr)
                if sel_len < dur - 0.5:
                    start_str = self.format_time(self.loop_start).split('.')[0]
                    end_str = self.format_time(self.loop_end).split('.')[0]
                    
                    state_str = "ON" if loop_active else "OFF"
                    color = "lime" if loop_active else "#FFA500"  # Zelená pokud běží, oranžová pokud je to jen výběr
                    
                    self.selection_label.configure(text=f"Loop {state_str}  |  {start_str} - {end_str}", text_color=color)
                else:
                    self.selection_label.configure(text="")
            else:
                self.selection_label.configure(text="")       
            
    def update_info_toolbarOld(self):
        """
        Updates the top info toolbar with the current video filename, format, 
        playback time, total duration, and current loop selection length.
        """
        if not hasattr(self, 'info_label') or not hasattr(self, 'selection_label'):
            return

        if not self.video_path:
            self.info_label.configure(text="No video selected")
            self.selection_label.configure(text="")
            return
        
        filename = os.path.basename(self.video_path)
        ext = os.path.splitext(filename)[1].upper().replace(".", "")
        
        # Format current time and total duration
        cur_time_str = self.format_time(self.current_time) if self.current_time else "00:00:00.000"
        dur_str = self.format_time(self.duration) if self.duration else "00:00:00.000"
        
        # Ořízneme milisekundy pro přehlednější zobrazení v toolbaru, pokud chceš
        cur_time_short = cur_time_str.split('.')[0]
        dur_short = dur_str.split('.')[0]

        info_text = f"{filename} [{ext}]  •  {cur_time_short} / {dur_short}"
        self.info_label.configure(text=info_text)
        
        # Calculate and display selection duration if loop markers are active
        if self.loop_start is not None and self.loop_end is not None:
            sel_len = max(0, self.loop_end - self.loop_start)
            sel_str = self.format_time(sel_len).split('.')[0] # again, hiding ms for cleanliness
            self.selection_label.configure(text=f"Selection: {sel_str}")
        else:
            self.selection_label.configure(text="")        
            
            
    def create_rotated_text_image(self, text, font_size=12, color="#FFA500"):
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except IOError:
            font = ImageFont.load_default()

        dummy_img = Image.new("RGBA", (1, 1))
        draw = ImageDraw.Draw(dummy_img)

        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except AttributeError:
            text_width, text_height = font.getsize(text)

        padding = 10
        canvas_width = text_width + padding * 2
        canvas_height = text_height + padding * 2

        img = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text((padding, padding), text, font=font, fill=color)

        rotated = img.rotate(90, expand=True)
        return ImageTk.PhotoImage(rotated)


    def open_export_dialog(self, video_path, loop_start=None, loop_end=None):
            def run_export(path, settings):
                self.convert_video_format(path, settings)

            VideoExportDialog(self.master, video_path, convert_callback=run_export, loop_start=loop_start, loop_end=loop_end)

  



    def show_convert_menu(self):
        """
        Displays the popup menu for conversion, gathering active loop data from the player.
        """
        active_player = getattr(self.controller, "current_video_window", None)
        loop_start = getattr(active_player, "loop_start", None) if active_player else None
        loop_end = getattr(active_player, "loop_end", None) if active_player else None
        loop_active = getattr(active_player, "loop_active", False) if active_player else False

        menu = create_menu(self, self)
        menu.add_command(label="Open Export Dialog", 
                         command=lambda: self.open_export_dialog(self.video_path, loop_start, loop_end, loop_active))
        
        x = self.convert_btn.winfo_rootx()
        y = self.convert_btn.winfo_rooty() + self.convert_btn.winfo_height()
        menu.tk_popup(x, y)

    def show_options_menu(self):
        if not hasattr(self, "marker_vars"):
            self.marker_vars = {}

        menu = create_menu(self, self)

        marker_menu = create_menu(self, menu)
        marker_types = ["bookmark", "thumbnail", "subtitle", "tag"]

        for marker_type in marker_types:
            if marker_type not in self.marker_types_visible:
                self.marker_types_visible[marker_type] = True

            var = tk.BooleanVar(value=self.marker_types_visible[marker_type])
            self.marker_vars[marker_type] = var

            marker_menu.add_checkbutton(
                label=marker_type.capitalize(),
                variable=var,
                command=partial(self.toggle_marker_type, marker_type)
            )

        any_off = any(not var.get() for var in self.marker_vars.values())
        toggle_label = "Show All Markers" if any_off else "Show No Markers"
        marker_menu.add_command(label=toggle_label, command=self.toggle_all_markers)
        menu.add_cascade(label="Markers", menu=marker_menu)

        preferences_menu = create_menu(self, menu)
        thumbs_count_menu = create_menu(self, preferences_menu)

        if not hasattr(self, "num_thumbs_var"):
            self.num_thumbs_var = tk.IntVar(value=self.num_thumbs)

        for n in [5, 10, 15]:
            thumbs_count_menu.add_radiobutton(
                label=str(n), value=n, variable=self.num_thumbs_var, command=lambda nn=n: self.set_num_thumbs(nn)
            )

        preferences_menu.add_cascade(label="Thumb count", menu=thumbs_count_menu)
        
        # Add Filmstrip toggle to Preferences
        if not hasattr(self, "fill_timeline_gaps"):
            self.fill_timeline_gaps = True
            
        self.filmstrip_var = tk.BooleanVar(value=self.fill_timeline_gaps)
        preferences_menu.add_checkbutton(
            label="Continuous Filmstrip", 
            variable=self.filmstrip_var,
            command=self.toggle_filmstrip
        )
        
             # --- NOVÉ: Podmenu pro velikost náhledů ---
        thumb_size_menu = create_menu(self, preferences_menu)
        # Synchronizujeme proměnnou s aktuálním stavem
        initial_size_str = f"{self.THUMB_W}x{self.THUMB_H}"
        self.thumb_size_var.set(initial_size_str)
        
        sizes = ["320x240", "400x300", "460x320"]
        for size in sizes:
            thumb_size_menu.add_radiobutton(
                label=size,
                value=size,
                variable=self.thumb_size_var,
                command=self._on_thumb_size_change # Nová funkce, kterou voláme
            )
        
        preferences_menu.add_cascade(label="Thumb size", menu=thumb_size_menu)
        # --- KONEC NOVÉHO PODMENU --
        
        
        preferences_menu.add_command(label="Settings (TODO)")
        menu.add_cascade(label="Preferences", menu=preferences_menu)

        x = self.options_btn.winfo_rootx()
        y = self.options_btn.winfo_rooty() + self.options_btn.winfo_height()
        menu.tk_popup(x, y)


    def toggle_filmstrip(self):
        """
        Toggles the continuous filmstrip mode on and off.
        """
        self.fill_timeline_gaps = self.filmstrip_var.get()
        self.redraw_timeline()

    def _on_thumb_size_change(self):
            """
            Handler for when the user selects a new thumbnail size from the menu.
            """
            new_size_str = self.thumb_size_var.get()
            logging.info(f"User selected new thumbnail size: {new_size_str}")
            
            # 1. Řekneme manažerovi, aby používal novou velikost
            self.timeline_manager.set_thumbnail_size(new_size_str)
            
            # 2. Aktualizujeme lokální proměnné pro šířku a výšku
            try:
                width, height = map(int, new_size_str.split('x'))
                self.THUMB_W = width
                self.THUMB_H = height
            except ValueError:
                logging.error(f"Could not parse new thumb size: {new_size_str}")
                return

            # 3. Spustíme přegenerování náhledů
            # (předpokládáme, že máš logiku pro vyčištění cache, jak jsi říkal)
            if self.video_path:
                logging.info("Forcing thumbnail reload due to size change.")
                self.load_thumbnails()



    def on_pan_start(self, event):
        self._pan_start_x = event.x
        self._pan_start_offset = self.pan_offset

    def on_pan_drag(self, event):
        dx = event.x - self._pan_start_x
        rel_dx = dx / self.canvas.winfo_width()
        self.pan_offset = self._pan_start_offset + rel_dx / self.zoom_factor
        self.pan_offset = max(-0.5 * (self.zoom_factor - 1), min(0.5 * (self.zoom_factor - 1), self.pan_offset))
        self.redraw_timeline()

    def on_pan_end(self, event):
        self._pan_start_x = None

    def x_to_rel(self, x, x0, x1):
        scaled_rel = (x - x0) / (x1 - x0)
        center_rel = 0.5
        if self.zoom_factor != 0:
            rel = ((scaled_rel - center_rel - self.pan_offset) / self.zoom_factor) + center_rel
        else:
            rel = center_rel
        return max(0, min(1, rel))

    def time_to_x(self, timestamp):
        duration = self._get_current_duration()
        if duration <= 0:
            return 0

        x0, x1 = self.get_timeline_bounds()
        
        rel = min(timestamp, duration) / duration
        center_rel = 0.5
        scaled_rel = (rel - center_rel) * self.zoom_factor + center_rel + self.pan_offset
        
        x = x0 + scaled_rel * (x1 - x0)
        return x


    def convert_video_format(self, input_path, settings):
        if not input_path or not os.path.isfile(input_path):
            messagebox.showerror("Error", "No video selected.")
            return

        target_ext = settings["ext"]
        save_path = filedialog.asksaveasfilename(
            defaultextension=target_ext,
            filetypes=[(f"{target_ext.upper()} files", f"*{target_ext}")]
        )
        if not save_path:
            return

        video_name = os.path.basename(input_path)
        t = threading.Thread(
            target=self._convert_worker,
            args=(input_path, save_path, settings, video_name),
            daemon=True
        )
        t.start()

    def _convert_worker(self, input_path, save_path, settings, video_name):
        status_bar = getattr(self.controller, "status_bar", None)

        def set_status(msg):
            if status_bar:
                self.after(0, lambda m=msg: status_bar.set_action_message(m))

        cap = None
        out = None
        try:
            cap = cv2.VideoCapture(input_path)
            if not cap.isOpened():
                self.after(0, lambda: messagebox.showerror("Error", "Cannot open video file."))
                return

            fourcc_map = {
                ".mp4": cv2.VideoWriter_fourcc(*"mp4v"),
                ".avi": cv2.VideoWriter_fourcc(*"XVID"),
                ".mov": cv2.VideoWriter_fourcc(*"mp4v"),
                ".mkv": cv2.VideoWriter_fourcc(*"mp4v"),
                ".webm": cv2.VideoWriter_fourcc(*"VP80"),
            }
            target_ext = settings["ext"]
            fourcc = fourcc_map.get(target_ext, cv2.VideoWriter_fourcc(*"mp4v"))
            out = cv2.VideoWriter(save_path, fourcc, settings["fps"], (settings["width"], settings["height"]))

            start_time = settings.get("start_time")
            end_time   = settings.get("end_time")
            start_ms   = (start_time * 1000) if start_time is not None else 0
            total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            fps_src      = cap.get(cv2.CAP_PROP_FPS) or 25
            end_ms = (end_time * 1000) if end_time is not None else (total_frames / fps_src * 1000)
            total_ms = max(end_ms - start_ms, 1)

            if start_time is not None:
                cap.set(cv2.CAP_PROP_POS_MSEC, start_ms)

            set_status(f"Exporting video: {video_name}  0%")
            last_pct = -1

            while True:
                current_msec = cap.get(cv2.CAP_PROP_POS_MSEC)
                if end_time is not None and current_msec > end_ms:
                    break

                ret, frame = cap.read()
                if not ret:
                    break

                resized = cv2.resize(frame, (settings["width"], settings["height"]))
                out.write(resized)

                pct = int((current_msec - start_ms) / total_ms * 100)
                pct = max(0, min(100, pct))
                if pct != last_pct:
                    last_pct = pct
                    set_status(f"Exporting video: {video_name}  {pct}%")

            set_status(f"Export complete: {video_name}")
            self.after(0, lambda: messagebox.showinfo("Done", f"Video saved to:\n{save_path}"))
            if status_bar:
                self.after(5000, status_bar.clear_action_message)

        except Exception as e:
            logging.error(f"[Export] Error during export: {e}")
            set_status(f"Export failed: {e}")
            self.after(0, lambda err=str(e): messagebox.showerror("Export Error", err))
        finally:
            if cap is not None:
                cap.release()
            if out is not None:
                out.release()


    def set_num_thumbs(self, num):
        self.load_thumbnails(num_thumbs=num)
        # self.redraw_timeline() # Redraw is now handled by the loader

    # --- NEW THREAD-SAFE THUMBNAIL LOADING ---
    def load_thumbnails(self, video_path=None, num_thumbs=None):
        if video_path is not None and video_path != self.video_path:
            self.video_path = video_path  # nejdřív nastavíme nové video_path...
            self.clear_selection()        # ...pak clear_selection() dotáže duration správného videa
        elif video_path is not None:
            self.video_path = video_path
        if num_thumbs is not None:
            self.num_thumbs = num_thumbs

        if self.worker_thread and self.worker_thread.is_alive():
            logging.info("Thumbnail generation is already in progress.")
            return

        self.thumb_images = []
        placeholder_img = Image.new("RGB", (self.THUMB_W, self.THUMB_H), (40, 40, 40))
        d = ImageDraw.Draw(placeholder_img)
        d.text((self.THUMB_W // 3, self.THUMB_H // 3), "...", fill=(150, 150, 150))
        placeholder_photo = ImageTk.PhotoImage(placeholder_img)
        
        for _ in range(self.num_thumbs):
            self.thumb_images.append((placeholder_photo, -1))
        
        self.redraw_timeline()

        self.worker_thread = threading.Thread(
            target=self._generate_thumbs_worker,
            args=(self.video_path, self.num_thumbs)
        )
        self.worker_thread.daemon = True
        self.worker_thread.start()
        
        
   

    def _generate_thumbs_worker(self, video_path, num_thumbs):
            logging.info(f"[WorkerThread] Starting thumbnail generation for {video_path}")
            thumbs_raw = self.timeline_manager.get_timeline_thumbnails(video_path, num_thumbs)
            
            for index, (thumb_path, timestamp) in enumerate(thumbs_raw):
                try:
                    if not thumb_path or not os.path.exists(thumb_path):
                        raise FileNotFoundError("Thumbnail path is missing.")
                    
                    img = Image.open(thumb_path)
                    # 🔥 TURBO TRIK 2: img.load() donutí Python dekomprimovat JPEG tady v pozadí!
                    # Zbaví hlavní vlákno zátěže a okno přestane drhnout.
                    img.load() 
                    
                    self.thumb_queue.put(('thumb', index, img, timestamp))
                except Exception as e:
                    logging.warning(f"[WorkerThread] Failed to generate thumb at index {index}. Reason: {e}")
                    self.thumb_queue.put(('error', index, None, timestamp))
            
            logging.info(f"[WorkerThread] Finished thumbnail generation.")
            self.thumb_queue.put(('done', None, None, None))



    def _process_thumb_queue(self):
            """
            Processes thumbnails from the background thread queue and updates the UI.
            Batching multiple updates into a single redraw for better performance.
            """
            needs_redraw = False
            try:
                # img.load() dekomprimuje JPEG v background threadu -> v main threadu je data už v RAM
                # ImageTk.PhotoImage() z předem načtených dat je rychlé, takže můžeme
                # zpracovat víc naráz bez znatelného záškubu.
                for _ in range(8):
                    try:
                        msg = self.thumb_queue.get_nowait()
                        # Unpack the message from the worker thread
                        msg_type, index, data, timestamp = msg 
                        
                        if msg_type == 'thumb':
                            # PhotoImage MUST be created in the main thread (here)
                            photo_image = ImageTk.PhotoImage(data)
                            if index < len(self.thumb_images):
                                # Store in our list so redraw_timeline can pick it up
                                self.thumb_images[index] = (photo_image, timestamp)
                                needs_redraw = True
                                
                        elif msg_type == 'error':
                            # Create an error placeholder if thumb generation failed
                            error_img = Image.new("RGB", (self.THUMB_W, self.THUMB_H), (64, 64, 64))
                            error_photo = ImageTk.PhotoImage(error_img)
                            if index < len(self.thumb_images):
                                self.thumb_images[index] = (error_photo, timestamp)
                                needs_redraw = True
                                
                        elif msg_type == 'done':
                            logging.info("[Timeline] Thumbnail generation worker finished.")
                            
                    except queue.Empty:
                        break # Queue is empty, exit the batch loop
            except Exception as e:
                logging.error(f"Error in _process_thumb_queue: {e}")
            
            # Překreslíme jen pokud přišel nový thumbnail
            if needs_redraw:
                self.redraw_timeline()

            # Schedule next queue check - 30ms je dostatečně rychlé a 2x méně CPU než 15ms
            self.after(30, self._process_thumb_queue)


    
    def on_ctrl_mousewheel(self, event):
        if event.delta > 0:
            self.zoom_factor = min(self.zoom_factor * 1.2, self.max_zoom)
        else:
            self.zoom_factor = max(self.zoom_factor / 1.2, self.min_zoom)
        logging.info(f"[DEBUG] Zoom changed to {self.zoom_factor:.2f}")
        self.redraw_timeline()

    def reset_zoom(self):
        self.zoom_factor = 1.0
        self.redraw_timeline()

    def toggle_marker_type(self, marker_type):
        current = self.marker_types_visible.get(marker_type, True)
        self.marker_types_visible[marker_type] = not current
        self.redraw_timeline() 

    def on_loop_button_click(self):
        active_player = getattr(self.controller, "current_video_window", None)
        if active_player and hasattr(active_player, "toggle_loop"):
            active_player.toggle_loop()
            new_loop_state = getattr(active_player, "loop_active", False)
            self.loop_button.config(text=f"🔁 Loop: {'ON' if new_loop_state else 'OFF'}")
            self.redraw_timeline()
        else:
            logging.warning("Could not toggle loop: No active player or toggle_loop method found.")
                    
    def toggle_snap_type(self):
        idx = self.snap_types.index(self.snap_type)
        idx = (idx + 1) % len(self.snap_types)
        self.snap_type = self.snap_types[idx]
        self.snap_btn.config(text=f"SNAP: {self.snap_type}")

    def toggle_magnet(self):
        self.magnet_mode = not self.magnet_mode
        self.magnet_btn.config(text=f"MAGNET: {'on' if self.magnet_mode else 'off'}")
                         
    def on_canvas_resize(self, event):
        logging.info(f"Canvas resized: {event.width}x{event.height}")
        self.redraw_timeline()


    def on_space_press(self, event):
            """Toggles play/pause: prioritizing standalone player, then embedded preview."""
            # 1. Zkusíme najít velké samostatné okno
            standalone_player = getattr(self.controller, "current_video_window", None)
            
            if standalone_player:
                logging.info("[Timeline] Space: Toggling standalone player.")
                if hasattr(standalone_player, 'toggle_play'):
                    standalone_player.toggle_play()
                return

            # 2. Pokud velké okno není, zkusíme ovládat vložený náhled (Preview)
            # Ten je v controlleru obvykle pod 'active_player'
            preview_player = getattr(self.controller, "active_player", None)
            
            if preview_player:
                logging.info("[Timeline] Space: Toggling embedded preview.")
                if hasattr(preview_player, 'toggle_play'):
                    preview_player.toggle_play()
            else:
                logging.info("[Timeline] Space: No player found to toggle.")

    def on_shift_click(self, event):
        """
        Handles Shift + Left Mouse Button click to start a new selection.
        """
        logging.info("[DEBUG] on_shift_click triggered!")
        active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)

        self.canvas.focus_set()
        duration = self._get_current_duration()
        if duration <= 0: return

        raw_time = self.get_time_at_x(event.x)
        clicked_time = self.apply_snap_and_magnet(event.x, raw_time, duration)

        self._selection_anchor = clicked_time
        
        # --- OPRAVA: Vymažeme loop z přehrávače, aby video neskákalo! ---
        if active_player:
            active_player.loop_active = False
            active_player.loop_start = None  # Schválně None
            active_player.loop_end = None    # Schválně None
        
        # Uložíme si to jen lokálně na osu pro modrou selekci
        self.loop_mode = False
        self.loop_start = clicked_time
        self.loop_end = clicked_time

        self.loop_drag = "select"
        self.canvas.config(cursor="sb_h_double_arrow")
        
        if hasattr(self, "loop_button"):
            self.loop_button.config(text="🔁 Loop: OFF")
        
        self.redraw_timeline()

    def on_canvas_primary_click(self, event):
        """
        Handles left-click on the timeline canvas.
        Prioritizes playhead drag, then loop/selection edges, then bookmarks, then seek.
        """
        self.canvas.focus_set()
        x = event.x
        active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)

        # 1. Priorita: Zachycení playheadu (červeného kurzoru) pro tažení
        margin_drag_playhead = 10
        px_playhead = self.time_to_x(self.current_time)
        if abs(x - px_playhead) < margin_drag_playhead:
            logging.info("[DEBUG] Zachycen DRAG PLAYHEADU (priorita).")
            self.loop_drag = None
            self.on_timeline_click(event)
            return

        # 2. Logika pro Loop (smyčky) a Selekci - start, end nebo posun
        # --- OPRAVA: Načteme časy buď ze zelené smyčky, nebo z naší modré pasivní selekce
        ls = getattr(self, "loop_start", None)
        le = getattr(self, "loop_end", None)
        
        if active_player and getattr(active_player, "loop_active", False):
            # Pokud běží zelená smyčka, mají přednost časy z přehrávače
            ls = getattr(active_player, "loop_start", None)
            le = getattr(active_player, "loop_end", None)

        if ls is not None and le is not None:
            margin_drag = 16
            px_s = self.time_to_x(ls)
            px_e = self.time_to_x(le)

            if abs(x - px_s) < margin_drag:
                logging.info("[DEBUG] Zachycen DRAG START okraje smyčky/selekce.")
                self.loop_drag = "start"
                self.canvas.config(cursor="sb_h_double_arrow")
                return

            if abs(x - px_e) < margin_drag:
                logging.info("[DEBUG] Zachycen DRAG END okraje smyčky/selekce.")
                self.loop_drag = "end"
                self.canvas.config(cursor="sb_h_double_arrow")
                return
            
            if px_s < x < px_e:
                logging.info("[DEBUG] Zachycen DRAG MOVE celé smyčky/selekce.")
                self.loop_drag = "move"
                x0, x1 = self.get_timeline_bounds()
                duration = self._get_current_duration() or 60
                clicked_time = self.x_to_rel(x, x0, x1) * duration
                self.drag_offset_time = clicked_time - ls
                self.canvas.config(cursor="fleur")
                return

        # 3. Kliknutí na marker (bookmark) - Skok na přesnou pozici
        # Hledáme objekty v těsné blízkosti kliknutí (rozsah 2px)
        overlapping = self.canvas.find_overlapping(event.x-2, event.y-2, event.x+2, event.y+2)
        for item_id in overlapping:
            marker = getattr(self, "marker_canvas_ids", {}).get(item_id)
            if marker:
                ts = marker.get("timestamp")
                if ts is not None:
                    logging.info(f"[Timeline] Jumping to marker: {marker.get('label')} at {ts:.2f}s")
                    # Provedeme seek v přehrávači
                    if getattr(self, "on_seek", None):
                        self.on_seek(ts)
                    # Aktualizujeme vizuální pozici playheadu na timeline
                    self.set_current_time(ts)
                    return # Ukončíme, aby se neprovedl standardní seek mimo marker

        # 4. Standardní seek na timeline, pokud jsme neklikli na marker nebo kurzor
        self.loop_drag = None
        self.on_timeline_click(event)

    

    def draw_markers(self, x0, x1, y_timeline, duration):
        self.marker_canvas_ids.clear()
        marker_spacing = {}
        for marker in self.markers:
            if not self.marker_types_visible.get(marker["type"], True):
                continue
            rel = marker["timestamp"] / duration
            x = x0 + rel * (x1 - x0)
            y_top = y_timeline - 32
            y_bot = y_timeline - 6
            
            rect_id = self.canvas.create_rectangle(x-6, y_top, x+6, y_bot, fill=marker["color"], outline="", tags="marker")
            arrow_id = self.canvas.create_polygon([x-6, y_bot, x+6, y_bot, x, y_bot+9], fill=marker["color"], outline="", tags="marker")
            x_int = int(x)
            offset = marker_spacing.get(x_int, 0)
            marker_spacing[x_int] = offset + 1
            label_id = self.canvas.create_text(x, y_top-10-offset*14, text=marker["label"], fill="#222" if marker["type"]=="subtitle" else "#FFF", font=("Arial", 9), anchor="s", tags="marker")
            self.marker_canvas_ids[rect_id] = marker
            self.marker_canvas_ids[arrow_id] = marker
            self.marker_canvas_ids[label_id] = marker


    def draw_thumbnails(self, x0, x1, y_bar_top, thumb_height, num_main):
        """
        Draws thumbnails as a filmstrip. Skip placeholders (timestamp -1) to keep the UI clean while loading.
        """
        thumb_centers = []
        if not self.thumb_images:
            return thumb_centers

        # Filter out placeholders (those with timestamp -1)
        valid_thumbs = [t for t in self.thumb_images if t[1] != -1]
        
        # --- FIX: If no real thumbs are loaded yet, just show the clean background ---
        if not valid_thumbs:
            return thumb_centers

        # If gaps filling is OFF, use sparse drawing but ONLY for valid thumbs
        if not getattr(self, 'fill_timeline_gaps', True):
            for i, (tk_img, timestamp) in enumerate(self.thumb_images):
                if timestamp == -1: continue # Skip placeholders even in sparse mode
                
                if num_main > 1:
                    rel = i / (num_main - 1)
                else:
                    rel = 0.5
                
                center_rel = 0.5
                scaled_rel = (rel - center_rel) * self.zoom_factor + center_rel + self.pan_offset
                x_thumb = x0 + scaled_rel * (x1 - x0)
                y_thumb = y_bar_top + thumb_height // 2
                thumb_centers.append((x_thumb, timestamp))
                self.canvas.create_image(x_thumb, y_thumb, image=tk_img, anchor="center", tags=f"thumb_{i}")
            return thumb_centers

        # --- FILMSTRIP LOGIC (remains same, but now only runs if valid_thumbs exist) ---
        tw = self.THUMB_W
        duration = self._get_current_duration() #
        px_start = self.time_to_x(0)
        px_end = self.time_to_x(duration)
        canvas_w = self.canvas.winfo_width()
        
        if tw <= 0: return thumb_centers
        
        total_tiles = int(math.ceil((px_end - px_start) / tw))
        
        for i in range(total_tiles):
            x_tile = px_start + (i * tw)
            if x_tile + tw < 0: continue
            if x_tile > canvas_w: break
                
            target_time = self.get_time_at_x(x_tile + tw / 2)
            best_img, best_ts = min(valid_thumbs, key=lambda t: abs(t[1] - target_time))
            
            self.canvas.create_image(x_tile, y_bar_top, image=best_img, anchor="nw", tags="thumb")
            thumb_centers.append((x_tile + tw / 2, best_ts))

        return thumb_centers

    

  
  
    def draw_overlays(self, x0, x1, y_bar_top, y_bar_bot, duration, num_main):
        """
        Draws markers, playhead, and a thin selection/loop bar ABOVE the thumbnails.
        The playhead line now extends to the very bottom of the canvas.
        """
        # Markery jen vykreslíme z již načteného self.markers - NERELOADUJEME z disku/DB
        # (reload_all_markers_and_redraw se volá explicitně při přepnutí videa nebo přidání záložky)
        if hasattr(self, "markers") and self.markers:
            self.draw_markers_above_thumbs(x0, x1, y_bar_top, duration)

        active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)
        start_time = getattr(self, "loop_start", None)
        end_time = getattr(self, "loop_end", None)
        is_repeating = active_player and getattr(active_player, "loop_active", False)

        # --- SELECTION STRIP (Updated height to 20) ---
        if start_time is not None and end_time is not None:
            x_s = self.time_to_x(start_time)
            x_e = self.time_to_x(end_time)
            
            strip_h = 20  # Updated per user request
            y_strip_top = y_bar_top - strip_h
            
            try:
                rect_w = int(x_e - x_s)
                if rect_w > 0:
                    alpha = 160 
                    r, g, b = (255, 165, 0) if is_repeating else (0, 191, 255)
                    
                    pixel_img = Image.new('RGBA', (1, 1), (r, g, b, alpha))
                    final_overlay_img = pixel_img.resize((rect_w, strip_h), Image.NEAREST)
                    
                    if not hasattr(self, "_active_selection_images_tk"):
                        self._active_selection_images_tk = []
                    
                    p_img = ImageTk.PhotoImage(final_overlay_img)
                    self._active_selection_images_tk.append(p_img)
                    self.canvas.create_image(x_s, y_strip_top, image=p_img, anchor="nw", tags="loop_rect_alpha")
            except Exception as e:
                self.canvas.create_rectangle(x_s, y_strip_top, x_e, y_bar_top, fill="#444", tags="loop_rect")

            border_col = "#FFA500" if is_repeating else "#00bfff"
            self.canvas.create_line(x_s, y_strip_top, x_s, y_bar_top, fill=border_col, width=2, tags="loop_bar")
            self.canvas.create_line(x_e, y_strip_top, x_e, y_bar_top, fill=border_col, width=2, tags="loop_bar")

        # --- YELLOW PLAYHEAD (Extended to bottom) ---
        if hasattr(self, "current_time") and duration > 0:
            cx = self.time_to_x(self.current_time)
            cursor_yellow = "#FFD700"
            
            # Use real canvas height to ensure the line is never cut off
            canvas_h = self.canvas.winfo_height()
            
            # Vertical line from top area to the very bottom
            self.canvas.create_line(cx, y_bar_top - 40, cx, canvas_h, fill=cursor_yellow, width=2, tags="time_cursor")
            
            # Stylish playhead handle (flag) at the top
            self.canvas.create_polygon([
                cx - 8, y_bar_top - 42, cx + 8, y_bar_top - 42, 
                cx + 8, y_bar_top - 30, cx, y_bar_top - 22, cx - 8, y_bar_top - 30
            ], fill=cursor_yellow, outline="black", width=1, tags="time_cursor")
        
   
                
                
    def draw_grid_and_axis(self, x0, x1, y_bar_top, y_bar_bot, duration, num_main):
        """
        Draws the track background from edge to edge (horizontally) and down to the bottom (vertically).
        Renders taller ticks and time labels in the upper marker area.
        """
        # Get canvas dimensions for full-screen stretching
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height() 
        
        # Modern subtle dark grey
        track_bg_color = "#1e1e21" 
        
        # Background: From x=0 to width, and from y_bar_top to the very bottom
        self.canvas.create_rectangle(0, y_bar_top, canvas_w, canvas_h, 
                                     fill=track_bg_color, outline="", tags="bg")

        if num_main <= 1:
            return

        major_tick_len = 40 
        minor_tick_len = 20
        
        for i in range(num_main):
            rel = i / (num_main - 1)
            center_rel = 0.5
            scaled_rel = (rel - center_rel) * self.zoom_factor + center_rel + self.pan_offset
            x_tick = x0 + scaled_rel * (x1 - x0)
            
            # Major ticks: Thicker and taller
            self.canvas.create_line(x_tick, y_bar_top, x_tick, y_bar_top - major_tick_len, 
                                    fill="#555555", width=2, tags="grid")
            
            # Time labels above ticks
            timestamp = duration * i / (num_main - 1)
            label = self.format_time(int(timestamp))
            self.canvas.create_text(x_tick, y_bar_top - major_tick_len - 12, text=label, 
                                    fill="#999999", font=("Segoe UI", 9, "bold"), tags="grid")

        # Minor ticks (subdivisions)
        num_subdivs = 10
        for i in range(num_main - 1):
            rel_start = i / (num_main - 1)
            rel_end = (i + 1) / (num_main - 1)
            center_rel = 0.5
            scaled_start = (rel_start - center_rel) * self.zoom_factor + center_rel + self.pan_offset
            scaled_end = (rel_end - center_rel) * self.zoom_factor + center_rel + self.pan_offset
            x_start = x0 + scaled_start * (x1 - x0)
            x_end = x0 + scaled_end * (x1 - x0)
            
            for s in range(1, num_subdivs):
                sub_rel = s / num_subdivs
                xx = x_start + (x_end - x_start) * sub_rel
                self.canvas.create_line(xx, y_bar_top, xx, y_bar_top - minor_tick_len, 
                                        fill="#3d3d42", width=1, tags="grid")
                
   
        


    def redraw_timeline(self, only_thumbs=False):
        """Redraws timeline with correct duration, dynamically scaling track background."""
        self.rotated_text_refs = [] # 🟢 Vyčistit staré obrázky z paměti
        w = self.canvas.winfo_width()
        x0, x1 = self.get_timeline_bounds()
        
        # 🟢 OPRAVA: Vynutíme zjištění reálné délky, aby markery nebyly mimo
        duration = self._get_current_duration()
        if duration <= 1: # Pokud je to pořád 1, zkusíme manažera
            duration = self.timeline_manager.get_video_duration(self.video_path) or 1

        num_main = self.num_thumbs
        
        # --- VIZUÁLNÍ OPRAVA: Padding osy ---
        thumb_y_top = 90               # Fyzický start samotných obrázků
        padding_y = 12                 # Přesah osy (v pixelech) nad a pod náhledy
        
        # Osa teď bude o "padding_y" vyšší nahoře i dole
        y_bar_top = thumb_y_top - padding_y
        y_bar_bot = thumb_y_top + self.THUMB_H + padding_y

        if not only_thumbs:
            self.canvas.delete("all")
            self.draw_grid_and_axis(x0, x1, y_bar_top, y_bar_bot, duration, num_main)
            self.draw_overlays(x0, x1, y_bar_top, y_bar_bot, duration, num_main)
        else:
            # Smažeme náhledy I markery, aby se markery nakreslily nad nové náhledy
            self.canvas.delete("thumb")
            self.canvas.delete("marker")
            # 🟢 Vždy překreslíme markery, i když se jen mění náhledy
            self.draw_markers_above_thumbs(x0, x1, y_bar_top, duration)

        # --- DŮLEŽITÉ: Náhledům předáme jejich původní souřadnici (thumb_y_top) ---
        self.thumb_centers = self.draw_thumbnails(x0, x1, thumb_y_top, self.THUMB_H, num_main)
        
        # --- Z-INDEX MAGIE ---
        # Ačkoli jsme náhledy nakreslili jako poslední, teď vytáhneme overlays úplně nahoru:
        self.canvas.tag_raise("loop_rect")        # Solidní barva selekce (fallback)
        self.canvas.tag_raise("loop_rect_alpha")  # Naše nová průhledná vrstva selekce
        self.canvas.tag_raise("loop_bar")         # Zelené/Zlaté/Modré okraje
        self.canvas.tag_raise("marker")           # Záložky
        self.canvas.tag_raise("time_cursor")      # Červený Playhead
        self.update_info_toolbar()

    def redraw_timelineOld(self, only_thumbs=False):
        """Redraws timeline with correct duration and layering."""
        self.rotated_text_refs = [] # 🟢 Vyčistit staré obrázky z paměti
        w = self.canvas.winfo_width()
        x0, x1 = self.get_timeline_bounds()
        
        # 🟢 OPRAVA: Vynutíme zjištění reálné délky, aby markery nebyly mimo
        duration = self._get_current_duration()
        if duration <= 1: # Pokud je to pořád 1, zkusíme manažera
            duration = self.timeline_manager.get_video_duration(self.video_path) or 1

        num_main = self.num_thumbs
        y_bar_top = 90
        y_bar_bot = y_bar_top + self.THUMB_H

        if not only_thumbs:
            self.canvas.delete("all")
            self.draw_grid_and_axis(x0, x1, y_bar_top, y_bar_bot, duration, num_main)
            self.draw_overlays(x0, x1, y_bar_top, y_bar_bot, duration, num_main)
        else:
            # Smažeme náhledy I markery, aby se markery nakreslily nad nové náhledy
            self.canvas.delete("thumb")
            self.canvas.delete("marker")
            # 🟢 Vždy překreslíme markery, i když se jen mění náhledy
            self.draw_markers_above_thumbs(x0, x1, y_bar_top, duration)

            
        # Náhledy se kreslí vždycky
        self.thumb_centers = self.draw_thumbnails(x0, x1, y_bar_top, self.THUMB_H, num_main)
        
        # --- Z-INDEX MAGIE ---
        # Ačkoli jsme náhledy nakreslili jako poslední, teď vytáhneme overlays úplně nahoru:
        self.canvas.tag_raise("loop_rect")        # Solidní barva selekce (fallback)
        self.canvas.tag_raise("loop_rect_alpha")  # Naše nová průhledná vrstva selekce
        self.canvas.tag_raise("loop_bar")         # Zelené/Zlaté/Modré okraje
        self.canvas.tag_raise("marker")           # Záložky
        self.canvas.tag_raise("time_cursor")      # Červený Playhead
        
   




    def apply_snap_and_magnet(self, x, raw_time, duration):
            """
            Calculates the exact timestamp based on active SNAP and MAGNET modes.
            Fixed "NoneType" crash and added sub-ticks for precise grid snapping.
            """
            if duration <= 0:
                return raw_time
                
            snap_points = []
            
            # 🧲 1. MAGNET: Přitahuje k markerům (záložky, tagy, titulky)
            if self.magnet_mode and hasattr(self, 'markers'):
                for m in self.markers:
                    if self.marker_types_visible.get(m.get("type"), True):
                        # 🔥 OPRAVA 1: Zamezení pádu, pokud je timestamp prázdný (None)
                        ts = m.get("timestamp")
                        if ts is not None:
                            snap_points.append(ts)
                        
            # 📐 2. SNAP: Přitahuje k mřížce (ticks) nebo středům náhledů (thumb)
            if self.snap_type == "thumb":
                for img_tuple in self.thumb_images:
                    if isinstance(img_tuple, tuple) and len(img_tuple) == 2 and img_tuple[1] != -1:
                        if img_tuple[1] is not None:
                            snap_points.append(img_tuple[1])
                            
            elif self.snap_type == "tick":
                if self.num_thumbs > 1:
                    # Hlavní ticky
                    for i in range(self.num_thumbs):
                        snap_points.append((i * duration) / (self.num_thumbs - 1))
                    
                    # 🔥 OPRAVA 2: Malé subdivizní ticky (viditelné čárky na mřížce)
                    num_subdivs = 10
                    for i in range(self.num_thumbs - 1):
                        start_time = (i * duration) / (self.num_thumbs - 1)
                        end_time = ((i + 1) * duration) / (self.num_thumbs - 1)
                        for s in range(1, num_subdivs):
                            sub_time = start_time + (end_time - start_time) * (s / num_subdivs)
                            snap_points.append(sub_time)

            # Pojistka pro jistotu, odfiltrujeme případné nesmysly
            snap_points = [p for p in snap_points if p is not None]

            # Pokud není k čemu se chytit (všechno je vypnuté), vrátíme volný čas
            if not snap_points:
                return raw_time

            # Najdeme nejbližší časový bod z těch, co jsme posbírali
            closest_time = min(snap_points, key=lambda t: abs(t - raw_time))
            
            # Zjistíme vzdálenost kurzoru myši od tohoto bodu v pixelech na obrazovce
            px_closest = self.time_to_x(closest_time)
            pixel_distance = abs(x - px_closest)
            
            # Gravitace - jak blízko (v pixelech) musí myš být, aby se kurzor přisál
            snap_threshold = 15 
            if pixel_distance <= snap_threshold:
                return closest_time
                
            return raw_time



    def get_time_at_x(self, x):
        """
        Calculates video timestamp for a given pixel X coordinate.
        Uses a mathematical formula instead of a heavy lookup table.
        """
        x0, x1 = self.get_timeline_bounds()
        if x1 <= x0: 
            return 0
        
        duration = self._get_current_duration()
        # Convert pixel to relative 0.0 - 1.0 position
        rel = self.x_to_rel(x, x0, x1)
        
        return rel * duration


    def load_bookmarks_for_path(self, video_path):
            if not video_path: return []
            # Sjednoceno na _bookmarks.json
            bookmarks_file = os.path.splitext(video_path)[0] + "_bookmarks.json"
            if os.path.exists(bookmarks_file):
                try:
                    with open(bookmarks_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        return data if isinstance(data, list) else []
                except Exception as e:
                    logging.error(f"Error loading bookmarks: {e}")
            return []
        
    def set_video_window(self, video_window):
        self.video_window = video_window
        
    def clear_selection(self):
        """Vymaže vizuální selekci z timeline (volá se při přepnutí videa)."""
        self.loop_mode = False
        self.loop_start = None
        self.loop_drag = None
        new_duration = self.timeline_manager.get_video_duration(self.video_path) or 1
        self.loop_end = new_duration
        self._cached_duration_value = new_duration
        self._cached_duration_path = self.video_path
        if hasattr(self, "loop_button"):
            self.loop_button.config(text="🔁 Loop: off")

    def reload_all_markers_and_redraw(self, video_path=None):
        if video_path is None:
            video_path = self.video_path
        self.video_path = video_path
        self.update_bookmarks()
        self.update_thumbnails()
        self.update_subtitles()

    def update_thumbnails(self):
        self.markers = [m for m in self.markers if m.get("type") != "thumbnail"]
        if not hasattr(self, "video_path") or not self.video_path or not os.path.isfile(self.video_path):
            return

        try:
            thumb = self.controller.database.get_single_thumbnail(self.video_path)
        except Exception as e:
            logging.info(f"[ERROR] Failed to load thumbnail from DB: {e}")
            thumb = None

        if thumb and "timestamp" in thumb:
            try:
                ts = float(thumb["timestamp"])
                self.markers.append({"type": "thumbnail", "timestamp": ts, "label": "Preview Frame", "color": "#00BFFF"})
            except (TypeError, ValueError):
                logging.warning(f"[Timeline] Invalid thumbnail timestamp: {thumb['timestamp']!r}")

    def update_subtitles(self):
        self.markers = [m for m in self.markers if m.get("type") != "subtitle"]
        if not hasattr(self, "video_path") or not self.video_path or not os.path.isfile(self.video_path):
            return

        try:
            subtitles = []
            if hasattr(self.controller, "current_video_window"):
                window = self.controller.current_video_window
                if hasattr(window, "subtitles") and isinstance(window.subtitles, list):
                    subtitles = window.subtitles
            if not subtitles:
                srt_path = os.path.splitext(self.video_path)[0] + ".srt"
                if os.path.exists(srt_path):
                    from utils import parse_srt_file
                    subtitles = parse_srt_file(srt_path)

            for s in subtitles:
                short_text = s.get("text", "").split("\n")[0][:20]
                timestamp = s.get("start")
                if timestamp is not None:
                    self.markers.append({"type": "subtitle", "timestamp": timestamp, "label": short_text, "color": "#32CD32"})
        except Exception as e:
            logging.info(f"[ERROR] Failed to load subtitle markers: {e}")

    def update_bookmarks(self):
            """Reloads bookmarks from player or JSON file into the widget's memory."""
            if not self.video_path or not os.path.exists(self.video_path):
                return

            # 🟢 VYČISTIT STARÉ MARKERY (Důležité!)
            self.markers = [m for m in self.markers if m.get("type") != "bookmark"]
            
            # 🟢 Načtení dat
            active_player = getattr(self.controller, "current_video_window", None)
            if active_player and active_player.video_path == self.video_path:
                # Bereme z běžícího přehrávače
                bookmarks_data = getattr(active_player, "bookmarks", [])
            else:
                # Bereme přímo z JSONu na disku
                bookmarks_data = self.load_bookmarks_for_path(self.video_path)

            # 🟢 Převod na markery pro vykreslení
            for b in bookmarks_data:
                # Podpora pro různé názvy klíčů (time vs timestamp)
                t = b.get("time") if b.get("time") is not None else b.get("timestamp")
                n = b.get("name") if b.get("name") is not None else b.get("label", "Marker")
                
                if t is not None:
                    self.markers.append({
                        "type": "bookmark",
                        "timestamp": float(t),
                        "label": str(n),
                        "color": "#FFA500" # Oranžová pro bookmarks
                    })
            
            logging.info(f"[Timeline] Loaded {len([m for m in self.markers if m['type']=='bookmark'])} bookmarks.")
            
            
    def draw_markers_above_thumbs(self, x0, x1, y_bar_top, duration):
            """Draws markers with shortened vertical labels and populates lookup map."""
            if not duration or duration <= 0:
                return

            self.marker_canvas_ids.clear()
            # rotated_text_refs se čistí v redraw_timeline
            
            marker_height = 18
            y_marker_bot = y_bar_top - 5 
            y_marker_top = y_marker_bot - marker_height
            
            marker_spacing = {} # Pro skládání popisků nad sebe při kolizi X

            for marker in self.markers:
                # Kontrola viditelnosti z OptionsWidgetu
                if not self.marker_types_visible.get(marker.get("type"), True):
                    continue

                ts = marker.get("timestamp")
                if ts is None:
                    continue
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    logging.warning(f"[Timeline] Skipping marker with invalid timestamp: {ts!r}")
                    continue

                # Výpočet X pozice (bere v úvahu Zoom a Pan)
                rel = ts / duration
                center_rel = 0.5
                scaled_rel = (rel - center_rel) * self.zoom_factor + center_rel + self.pan_offset
                x = x0 + scaled_rel * (x1 - x0)

                # Vykreslení jen pokud je v viditelné oblasti
                if x0 <= x <= x1:
                    color = marker.get("color", "#FFA500")
                    
                    # Logika pro zamezení překrývání popisků (stohování)
                    x_int = int(x / 10) # Seskupení blízkých markerů
                    offset = marker_spacing.get(x_int, 0)
                    marker_spacing[x_int] = offset + 1
                    y_text_stack = offset * 15 
                    
                    # 1. Vykreslení tvaru záložky
                    rect_id = self.canvas.create_rectangle(x - 6, y_marker_top, x + 6, y_marker_bot - 5, 
                                                 fill=color, outline="", tags="marker")
                    arrow_id = self.canvas.create_polygon([x - 6, y_marker_bot - 5, x + 6, y_marker_bot - 5, x, y_marker_bot], 
                                               fill=color, outline="", tags="marker")
                    
                    # 2. Vykreslení zkráceného popisku (max 10 slov)
                    full_label = marker.get("label", "")
                    words = str(full_label).split()
                    short_label = " ".join(words[:10])
                    if len(words) > 10: short_label += "..."
                    
                    # Použijeme tvůj pomocník pro rotaci textu
                    text_img = self.create_rotated_text_image(short_label, font_size=10, color=color)
                    self.rotated_text_refs.append(text_img) # Uložení reference proti smazání z paměti
                    
                    text_id = self.canvas.create_image(x, y_marker_top - 10 - y_text_stack, 
                                                       image=text_img, anchor="s", tags="marker")
                    
                    # 3. Naplnění mapy pro interakci (aby tooltip věděl, co zobrazit)
                    self.marker_canvas_ids[rect_id] = marker
                    self.marker_canvas_ids[arrow_id] = marker
                    self.marker_canvas_ids[text_id] = marker        
        
    
    

    def get_timeline_bounds(self):
        w = self.canvas.winfo_width()
        margin_x = int(0.06 * w)
        x0 = margin_x
        x1 = w - margin_x
        return x0, x1

    def start_periodic_update(self):
        self._periodic_update()

    def _periodic_update(self):
        """
        Periodically checks the active player's time and updates the timeline.
        This loop is optimized to only perform actions if the panel is expanded (visible).
        """
        # --- POJISTKA PROTI PÁDU (Crash Fix) ---
        # Pokud už widget neexistuje (aplikace se zavírá), okamžitě skonči.
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        # ---------------------------------------
       
        # Check if the parent (TogglePanelFrame) exists and is expanded.
        if not hasattr(self.parent, 'expanded') or not self.parent.expanded:
            # Just reschedule the check for later
            self.after(300, self._periodic_update)
            return 

          # The code below will ONLY run if the timeline is visible:
        active_player = getattr(self.controller, "active_player", None)
        
        # --- FIX: Kontrola viditelnosti ZDROJOVÉHO přehrávače ---
        # Pokud je to "embed" (náhled v InfoPanelu), musíme ověřit, zda je InfoPanel viditelný.
        # winfo_viewable() vrátí 0, pokud je widget (nebo jeho rodič) skrytý (pack_forget).
        if active_player and getattr(active_player, "embed", False):
            if hasattr(active_player, "video_window") and not active_player.video_window.winfo_viewable():
                # Přehrávač je sice aktivní, ale schovaný -> neposouvat kurzor
                self.after(300, self._periodic_update)
                return
        
      
        # Bezpečné získání cesty (ošetření proti None)
        video_path = getattr(active_player, "video_path", None) if active_player else None
        
        # Pokud není aktivní player nebo video, jen čekáme
        if not active_player or not video_path or not os.path.isfile(video_path):
            self.after(300, self._periodic_update)
            return

        current_time = active_player.get_current_time() if hasattr(active_player, "get_current_time") else None
        if current_time is not None:
            self.set_current_time(current_time)
        
        # Reschedule the next check (runs faster when active)
        self.after(100, self._periodic_update)

    def set_current_time(self, timestamp):
        """
        Updates the playhead position smoothly during video playback.
        Calculates pixel difference and moves the cursor items, avoiding redraws.
        """
        # Spočítáme, kde byl kurzor předtím
        old_cx = self.time_to_x(self.current_time)
        
        # Aktualizujeme čas a zjistíme, kde má být teď
        self.current_time = timestamp
        new_cx = self.time_to_x(timestamp)
        
        # Rozdíl v pixelech
        dx = new_cx - old_cx
        
        # Najdeme všechny prvky kurzoru (čára i ten červený trojúhelníček nahoře)
        cursor_items = self.canvas.find_withtag("time_cursor")
        
        if cursor_items:
            # Jen je "šoupneme" o vypočtený rozdíl pixelů doprava/doleva
            self.canvas.move("time_cursor", dx, 0)
        else:
            # Pokud kurzor vůbec neexistuje (např. úplně první spuštění), překreslíme
            self.redraw_timeline()
        self.update_info_toolbar()


    def _get_current_duration(self):
            """
            Retrieves video duration.
            Forces a refresh if cache is empty or belongs to a different video.
            """
            cached = getattr(self, '_cached_duration_value', 0)
            cached_path = getattr(self, '_cached_duration_path', None)

            # Refresh when cache is zero OR when it was computed for a different video
            if cached <= 0 or cached_path != self.video_path:
                if hasattr(self, 'timeline_manager') and self.video_path:
                    d = self.timeline_manager.get_video_duration(self.video_path)
                    if d > 0:
                        self._cached_duration_value = d
                        self._cached_duration_path = self.video_path
                        return d

            return max(cached, 1)  # vracíme aspoň 1, aby se nedělilo nulou

    def on_timeline_click(self, event):
            """
            Handles mouse clicks on the timeline to seek video position.
            Uses math calculation and applies Snap/Magnet logic.
            """
            duration = self._get_current_duration()
            if duration <= 0: 
                return

            # 1. Calculate raw time directly from X coordinate
            raw_time = self.get_time_at_x(event.x)
            
            # 2. 🔥 ZDE SE APLIKUJE PŘITAŽLIVOST
            clicked_time = self.apply_snap_and_magnet(event.x, raw_time, duration)
            
            if self.on_seek:
                self.on_seek(clicked_time)
            
            # Manually update playhead position for instant feedback
            self.set_current_time(clicked_time)


        
        
    # def on_timeline_click(self, event):
        # x = event.x
        # x0, x1 = self.get_timeline_bounds()
        # duration = self._get_current_duration()
        # relative_position = self.x_to_rel(x, x0, x1)
        # timestamp = relative_position * duration
        # logging.info(f"[TimelineBarWidget] Clicked timeline (duration: {duration:.2f}s) at x={event.x}, time={timestamp:.2f}s")
        # if self.on_seek:
            # self.on_seek(timestamp)
    
    def get_snap_times(self, duration, num_main, num_subdivs):
        snap_times = []
        if num_main > 1:
            for i in range(num_main - 1):
                t_start = duration * i / (num_main - 1)
                t_end = duration * (i + 1) / (num_main - 1)
                if i == 0:
                    snap_times.append(t_start)
                for s in range(1, num_subdivs):
                    t_sub = t_start + s * (t_end - t_start) / num_subdivs
                    snap_times.append(t_sub)
                snap_times.append(t_end)
        return snap_times
        
        
    def on_canvas_drag(self, event):
        """Handles mouse drag events for timeline playhead, and moving/resizing selections."""
        x = event.x
        x0, x1 = self.get_timeline_bounds()
        duration = self._get_current_duration() or 60
        raw_time = self.x_to_rel(x, x0, x1) * duration
        new_time = self.apply_snap_and_magnet(x, raw_time, duration)

        active_player = getattr(self.controller, "current_video_window", None) or getattr(self.controller, "active_player", None)
        is_active = active_player and getattr(active_player, "loop_active", False)

        if self.loop_drag == "select":
            new_start = min(self._selection_anchor, new_time)
            new_end = max(self._selection_anchor, new_time)
            if new_end - new_start < 0.1: new_end = new_start + 0.1

            self.loop_start = new_start
            self.loop_end = new_end
            if is_active:
                active_player.loop_start = new_start
                active_player.loop_end = new_end
            self.redraw_timeline()
            return

        if not active_player:
            self.on_timeline_click(event)
            return

        if self.loop_drag == "start":
            new_start = min(new_time, self.loop_end - 0.1)
            self.loop_start = new_start
            if is_active:
                if getattr(active_player, "loop_end", None) is None: active_player.loop_end = self.loop_end
                active_player.set_loop_start_from_timeline(new_start)
            self.redraw_timeline()
            return

        elif self.loop_drag == "end":
            new_end = max(new_time, self.loop_start + 0.1)
            self.loop_end = new_end
            if is_active:
                if getattr(active_player, "loop_start", None) is None: active_player.loop_start = self.loop_start
                active_player.set_loop_end_from_timeline(new_end)
            self.redraw_timeline()
            return

        elif self.loop_drag == "move":
            loop_duration = self.loop_end - self.loop_start
            new_start = raw_time - self.drag_offset_time
            new_end = new_start + loop_duration

            if new_start < 0:
                new_start = 0
                new_end = loop_duration
            if new_end > duration:
                new_end = duration
                new_start = duration - loop_duration

            self.loop_start = new_start
            self.loop_end = new_end

            if is_active:
                if getattr(active_player, "loop_start", None) is None: active_player.loop_start = self.loop_start
                if getattr(active_player, "loop_end", None) is None: active_player.loop_end = self.loop_end
                active_player.set_loop_start_from_timeline(new_start)
                active_player.set_loop_end_from_timeline(new_end)
            self.redraw_timeline()
            return

        self.on_timeline_click(event)

      

    def on_canvas_release(self, event):
        if self.loop_drag:
            active_player = getattr(self.controller, "current_video_window", None)
            if active_player and hasattr(active_player, "update_loop_bar_display"):
                active_player.update_loop_bar_display()
                logging.info("[DEBUG] Loop bar ve video přehrávači byl aktualizován.")
        self.loop_drag = None
        self.canvas.config(cursor="")

    def on_thumb_click_event(self, timestamp, event):
        self.on_thumb_click(timestamp)

    def on_timeline_drag(self, event):
        self.on_timeline_click(event)

    def format_time(self, seconds):
        m, s = divmod(seconds, 60)
        return f"{int(m):02}:{int(s):02}"

    def on_thumb_click(self, timestamp):
        logging.info(f"Clicked at {timestamp}s")
        if self.on_seek:
            logging.info("clicked ON SEEK")
            self.on_seek(timestamp)
            
    def on_close(self):
        if hasattr(self.parent, "timeline_window"):
            self.parent.timeline_window = None
        self.destroy()


    def on_mouse_move(self, event):
            """Checks if mouse is over a marker and shows the full tooltip."""
            # Najdeme objekty pod kurzorem (s malou tolerancí 2px)
            items = self.canvas.find_overlapping(event.x-2, event.y-2, event.x+2, event.y+2)
            
            found_marker = None
            for item_id in items:
                if item_id in self.marker_canvas_ids:
                    found_marker = self.marker_canvas_ids[item_id]
                    break
            
            if found_marker:
                self.show_marker_tooltip(event.x, event.y, found_marker)
                self.canvas.config(cursor="hand2")
            else:
                self.hide_marker_tooltip()
                # Vrátíme kurzor do původního stavu (pokud zrovna netaháme smyčku)
                if not self.loop_drag:
                    self.canvas.config(cursor="")

    def show_marker_tooltip(self, x, y, marker):
        """Displays a neat tooltip on the canvas with full text."""
        self.canvas.delete("marker_tooltip")
        
        full_text = f"{marker['label']} [{self.format_time(marker['timestamp'])}]"
        
        # Vytvoření textu (trochu posunutý od kurzoru)
        txt_id = self.canvas.create_text(x + 15, y - 15, text=full_text, fill="white", 
                                        anchor="sw", font=("Segoe UI", 10, "bold"), tags="marker_tooltip")
        
        # Vytvoření pozadí pod textem (bublina)
        bbox = self.canvas.bbox(txt_id)
        if bbox:
            bg_id = self.canvas.create_rectangle(bbox[0]-5, bbox[1]-2, bbox[2]+5, bbox[3]+2, 
                                                 fill="#333", outline="#FFA500", tags="marker_tooltip")
            self.canvas.tag_lower(bg_id, txt_id)

    def hide_marker_tooltip(self):
        """Cleans up the tooltip from canvas."""
        self.canvas.delete("marker_tooltip")
