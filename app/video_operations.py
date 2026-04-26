"""
VLC-backed video player, audio device listing, and GPU vendor hint for Vibe Player.

``VideoPlayer`` handles playback, seek, volume, bookmarks, loop regions, and playlist hooks;
supports embedded preview mode for the info panel.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import PhotoImage, ttk

import chardet
import vlc
from pynput import mouse
from PIL import Image, ImageTk
from screeninfo import get_monitors

from file_operations import *
from playlist import PlaylistManager
import tempfile
import os
import subprocess
import time
import tkinter.font as tkfont
import sys
import ctypes
# from gui_elements import create_menu
from utils import create_menu
import logging

import tkinterdnd2 as dnd
from vtp_constants import VIDEO_FORMATS
from vtp_mixin_dnd import VtpDndMixin

_audio_devices_cache = None


def _dnd_tk_surface(widget):
    """Plain Tk widget for tkinterdnd2 (CustomTkinter hosts use ``_canvas``)."""
    if widget is None:
        return None
    if hasattr(widget, "drop_target_register"):
        return widget
    inner = getattr(widget, "_canvas", None)
    if inner is not None and hasattr(inner, "drop_target_register"):
        return inner
    return None


def get_gpu_vendor() -> str:
    """
    Detects the GPU vendor by querying Windows WMI.

    Returns 'NVIDIA', 'AMD', or 'OTHER' based on the installed GPU name.
    Falls back to 'OTHER' if detection fails.
    """
    try:
        result = subprocess.run(
            ["wmic", "path", "win32_VideoController", "get", "name"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout.upper()
        if "NVIDIA" in output:
            return "NVIDIA"
        if "AMD" in output or "RADEON" in output:
            return "AMD"
    except Exception as e:
        logging.warning(f"[GPU] Failed to detect GPU vendor: {e}")
    return "OTHER"

def get_audio_devices():
    """
    Returns a cached list of AUDIO OUTPUT devices.
    If the cache is empty, it queries the sounddevice library.
    """
    global _audio_devices_cache
    if _audio_devices_cache is None:
        try:
            import sounddevice as sd
            all_devices = sd.query_devices()
            # Filter only devices that have output channels
            _audio_devices_cache = [
                d for d in all_devices if d['max_output_channels'] > 0
            ]
            logging.info(f"[AudioDevice] Found and filtered {len(_audio_devices_cache)} output devices.")
        except Exception as e:
            logging.info(f"[AudioDevice] Failed to query devices: {e}")
            _audio_devices_cache = []
    return _audio_devices_cache

class VideoPlayer:
    def __init__(self, parent, controller, video_path, video_name, initial_volume, 
                 vlc_video_output, vlc_audio_output, vlc_hw_decoding, vlc_audio_device,
                 auto_play=False, subtitles_enabled=False, playlist_manager=None, embed=False,
                 show_video_button_bar=True, use_gpu_upscale=False):
            
        #assign variables from other libraries, for example from video_operations.py
        self.parent = parent
        self.controller = controller    # <--- Ulož si controller!
        self.current_volume = initial_volume  # load saved volume when video player is opened and closed
        # self.hud_enabled = self.controller.video_show_hud
        self.loop_start = None
        self.loop_end = None
        self.loop_active = False
        self.show_video_button_bar = show_video_button_bar
        self.playlist_manager = playlist_manager
        self.video_path = video_path
        self.video_name = video_name
        self.auto_play = auto_play
        self.subtitles_enabled = subtitles_enabled 
        self.is_repeating = False
        self.playing = False
        self.embed = embed
        self.current_speed = 1.0
        
        self.is_fullscreen = False
        self.previous_geometry = ""
        self.listener = None  # Initialize the listener as None
        self.bookmarks = []
        if video_path:
            self.bookmark_file = f"bookmarks/{os.path.basename(video_path)}.json"
            self.load_bookmarks()
        else:
            self.bookmark_file = None

        
        # Use grab_set to capture all events
        # self.video_window.grab_set()
        self.last_position = 0  # Variable to store the last played position
        self.controls_frame_visible = False

        # Initialize VLC settings attributes
        self.video_output = vlc_video_output
        self.audio_output = vlc_audio_output
        self.hardware_decoding = vlc_hw_decoding
        self.audio_device = vlc_audio_device
        self.use_gpu_upscale = use_gpu_upscale
        self.timeline_widget = None
        self._loaded_video_path = None
        self._duration_retry_count = 0
        self._init_cinematic_theme()

        if embed:
            self.video_window = tk.Frame(parent, bg="black")
            self.video_window.pack(fill="both", expand=True)
            self.video_window.bind("<Configure>", self.on_panel_resize)
        
        else:
        
            self.video_window = ctk.CTkToplevel(self.parent)
            self.video_window.title((video_name or "").lower())
            self.video_window.geometry("800x600")
            self.video_window.configure(fg_color=self.surface_dim)
            self._configure_window_chrome()
            self._apply_acrylic_if_available()
            # Make sure the video window is raised above all other windows

        self.video_area = ctk.CTkFrame(self.video_window, fg_color="black")
        self.video_area.pack(side=ctk.TOP, fill=ctk.BOTH, expand=True)

        # Single stack so VLC host + Tk overlay occupy the same pixel rect (pack would split 50/50).
        self._video_stack = ctk.CTkFrame(self.video_area, fg_color="black")
        self._video_stack.pack(fill=ctk.BOTH, expand=True)

        # VLC is created lazily on first play (see _ensure_vlc_player). Do not call
        # create_vlc_instance() here — it blocks the Tk thread and the old code
        # wrongly logged "Failed to create VLC instance" whenever self.instance was still None.
        self.instance = None
        self.player = None

        # pynput runs on a worker thread — never touch Tk from that thread (not even .after()).
        # Bridge mouse events through a queue; _pynput_queue_pump runs only on the Tk main thread.
        self._pynput_bridge_dead = False
        self._pynput_queue: queue.Queue[tuple[str, int, int]] = queue.Queue(maxsize=256)
        self._pynput_pump_job = None

        self.global_listener = mouse.Listener(on_click=self._on_global_click)
        self.global_listener.start()
        try:
            self._pynput_pump_job = self.video_window.after(25, self._pynput_queue_pump)
        except Exception:
            self._pynput_pump_job = None

        self.video_label = ctk.CTkLabel(self._video_stack, text="")
        self.video_label.place(relx=0, rely=0, relwidth=1, relheight=1)

        # Full-window fallback when VLC has no decoded video plane (corrupt / black output).
        self._broken_playback_overlay = ctk.CTkLabel(self._video_stack, text="", fg_color="black")
        self._broken_playback_overlay.place_forget()
        self._broken_playback_overlay_img = None
        self._broken_playback_overlay_active = False
        self._broken_check_after_ids = []
        self._broken_decode_check_gen = 0

        self.load_icons()

        self._setup_video_playback_dnd()






        if not embed:
            
            
            # --- TOOLBAR/SUBFRAME (slider/timer atd.) ---
            self.controls_frame = ctk.CTkFrame(
                self.video_window,
                fg_color=self.surface_low,
                corner_radius=0,
                border_width=0,
            )
            self.controls_frame.pack(side=ctk.BOTTOM, fill=ctk.X, padx=0, pady=0)

            logging.info("Video label created and packed.")  # Debug
                            # Replace ttk.Frame with CTkFrame
            self.sub_frame = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
            # sub frame is frame only for timer and slider, so both will be packed in same row
            self.sub_frame.pack(fill=ctk.X, side=ctk.TOP, padx=8, pady=(4, 2))
            
                                         # Add a label to show the timer (current time / total duration)
            self.timer_label = ctk.CTkLabel(
                self.sub_frame,
                text="00:00 / 00:00",
                text_color=self.cyan,
                font=self.telemetry_font,
            )
            self.timer_label.pack(side=ctk.LEFT, padx=(0, 8))
            # Tall hit strip so clicks/drags work above/below the 2px scrubber (same idea as volume bar).
            self.timeline_hit_host = ctk.CTkFrame(
                self.sub_frame,
                fg_color="transparent",
                height=22,
                cursor="hand2",
            )
            self.timeline_hit_host.pack(side=ctk.LEFT, fill=ctk.X, expand=True, padx=(0, 8))
            self.timeline_hit_host.pack_propagate(False)

            self.slider = ctk.CTkSlider(
                self.timeline_hit_host,
                from_=0,
                to=100,
                orientation="horizontal",
                command=self.slider_update,
                fg_color=self.outline_soft,
                progress_color=self.primary,
                button_color=self.outline_soft,
                button_hover_color=self.outline_soft,
                button_corner_radius=0,
                button_length=2,
                corner_radius=0,
                height=2,
                cursor="hand2",
            )
            self.slider.set(0)
            self.slider.pack(fill=ctk.X, expand=True, pady=(10, 10))

            self.loop_bar_canvas = tk.Canvas(
                self.sub_frame,
                height=2,
                highlightthickness=0,
                bd=0,
                bg=self.surface_low,
                cursor="hand2",
            )

            self.loop_bar_canvas.place(in_=self.slider, relx=0, rely=1.1, relwidth=1.0)
            self.loop_bar_canvas.bind("<Configure>", lambda e: self.draw_loop_bar())
            self.slider.bind("<Configure>", lambda e: self.draw_loop_bar())

            for _w in (self.timeline_hit_host, self.slider, self.loop_bar_canvas):
                _w.bind("<Button-1>", self._timeline_drag)
                _w.bind("<B1-Motion>", self._timeline_drag)
                _w.bind("<ButtonRelease-1>", self.slider_update)
         

        
        # Bind Shift+T to generate a thumbnail
        self.video_window.bind('<Shift-T>', self.generate_thumbnail)
        # Bind Shift+C to save the current frame as an image
        # Bind Shift+C to save the current frame as an image
        self.video_window.bind('<Shift-C>', lambda e: save_capture_image(self.controller, self.video_path, self.player, method="ffmpeg"))
        self.video_window.bind("<Control-w>", lambda e: self.close_video_player())
        self.video_window.bind("<Shift-F>", self.toggle_fullscreen)
        self.video_window.bind("<Alt-Return>", self.toggle_fullscreen)
        self.video_window.bind("<Alt-KP_Enter>", self.toggle_fullscreen)
        self.video_window.bind("<Escape>", self._handle_escape_key)
        self.video_window.bind("<Double-Button-1>", self.toggle_fullscreen)
                # Bring the video player to the top
        self.video_window.bind("<Shift-B>", lambda e: self.add_bookmark())                
        

        # Loop bindings when this window has focus
        self.video_window.bind("<Shift-S>", lambda e: self.controller.set_loop_start_shortcut())
        self.video_window.bind("<Shift-E>", lambda e: self.controller.set_loop_end_shortcut())
        self.video_window.bind("<Shift-L>", lambda e: self.controller.toggle_loop_shortcut())
        self.video_window.bind("<Alt-Right>", lambda e: self.skip_to_next_bookmark())
        self.video_window.bind("<Alt-Left>", lambda e: self.skip_to_previous_bookmark())
        self.video_window.bind("<Shift-Right>", lambda e: self.long_seek(direction=1))
        self.video_window.bind("<Shift-Left>", lambda e: self.long_seek(direction=-1))

        # Speed bindings when this window has focus
        self.video_window.bind("<greater>", lambda e: self.speed_step(+1))
        self.video_window.bind("<less>",    lambda e: self.speed_step(-1))
                
        
        if self.player:
            self.player.audio_set_volume(self.current_volume)
        
        
        if self.show_video_button_bar:
            self.setup_volume_slider()
            self.volume_slider.set(self.current_volume)  # Update the slider to match the current volume
        
        # self.video_window.bind("<Shift-S>", self.set_loop_start)
        # self.video_window.bind("<Shift-E>", self.set_loop_end)
        # self.video_window.bind("<Shift-L>", self.clear_loop)


        if self.show_video_button_bar:
            self.create_buttons()


        self.video_window.bind("<Configure>", self.on_resize)
        if not embed:
            self.video_window.protocol("WM_DELETE_WINDOW", self.close_video_player)

         # TOTO PŘIDEJ: Navázání událostí pro focus okna
        self.video_window.bind("<FocusIn>", self.on_focus_in)
        self.video_window.bind("<FocusOut>", self.on_focus_out)

        self.video_window.focus_set()
        
        # if self.video_path:  
            # if self.auto_play:
                # self.play_video()
            # else:
                # self.display_first_frame()
    
    # SOUBOR: video_operations.py

    def show_and_play(self):
        """
        Zobrazí okno a spustí přehrávání. Volá se až poté, co je __init__ hotový
        a okno je plně připraveno operačním systémem.
        """
        # Tento kód byl přesunut z konce __init__
        if self.video_path:
            if self.auto_play:
                self.play_video()
            else:
                self.display_first_frame()

    def _setup_video_playback_dnd(self):
        """Drop video files onto the playback area (Explorer or in-app); always COPY semantics."""
        targets = []
        for w in (self.video_label, self._video_stack, self.video_area, self.video_window):
            surf = _dnd_tk_surface(w)
            if surf is not None and surf not in targets:
                targets.append(surf)
        if not targets:
            return
        for surf in targets:
            try:
                surf.drop_target_register(dnd.DND_FILES)
                surf.dnd_bind("<<Drop>>", self._dnd_on_video_playback_drop)
                surf.dnd_bind("<<DropPosition>>", self._dnd_on_video_playback_drop_position)
            except Exception as e:
                logging.warning("[DnD] video playback drop target failed: %s", e)

    def _dnd_on_video_playback_drop_position(self, event):
        # COPY only: never treat as file-manager MOVE (would delete source on internal drags).
        return dnd.COPY

    def _dnd_on_video_playback_drop(self, event):
        paths = VtpDndMixin._dnd_parse_paths(event.data)
        videos = [
            p
            for p in paths
            if isinstance(p, str) and os.path.isfile(p) and p.lower().endswith(VIDEO_FORMATS)
        ]
        if not videos:
            return
        if len(videos) > 1:
            pm = getattr(self.controller, "playlist_manager", None)
            if pm is not None:
                added = pm.add_to_playlist(videos[1:])
                if added > 0:
                    ctrl = self.controller
                    sb = getattr(ctrl, "status_bar", None)
                    if sb is not None:
                        msg = (
                            "Added 1 video to playlist."
                            if added == 1
                            else f"Added {added} videos to playlist."
                        )
                        sb.set_action_message(msg)
                        try:
                            ctrl.after(4500, sb.clear_action_message)
                        except Exception:
                            pass
        self._load_dropped_video_path(videos[0])

    def _load_dropped_video_path(self, path: str):
        if not path or not os.path.isfile(path):
            return
        name = os.path.basename(path)
        if self.player and self.instance:
            self.safe_switch_video(path, name)
            return
        self.video_path = path
        self.video_name = name
        self.bookmark_file = f"bookmarks/{os.path.basename(path)}.json"
        try:
            self.load_bookmarks()
        except Exception:
            self.bookmarks = []
        try:
            if not self.embed:
                self.video_window.title((name or "").lower())
        except Exception:
            pass
        self.play_video()

    def _cancel_broken_decode_checks(self) -> None:
        for aid in getattr(self, "_broken_check_after_ids", []) or []:
            try:
                self.video_window.after_cancel(aid)
            except Exception:
                pass
        self._broken_check_after_ids = []

    def _hide_broken_playback_overlay(self) -> None:
        ov = getattr(self, "_broken_playback_overlay", None)
        if ov is None:
            return
        try:
            ov.place_forget()
        except Exception:
            pass
        self._broken_playback_overlay_active = False

    def _playback_has_video_dimensions(self) -> bool:
        if not self.player:
            return False
        vg = getattr(self.player, "video_get_size", None)
        if not callable(vg):
            return False
        try:
            for i in range(8):
                try:
                    wh = vg(i)
                except Exception:
                    continue
                if wh and len(wh) >= 2 and int(wh[0]) > 0 and int(wh[1]) > 0:
                    return True
        except Exception:
            return False
        return False

    def _show_broken_playback_overlay(self) -> None:
        if getattr(self, "_broken_playback_overlay_active", False):
            return
        ctrl = getattr(self, "controller", None)
        if ctrl is None or not hasattr(ctrl, "_broken_video_placeholder_pil"):
            return
        try:
            self._video_stack.update_idletasks()
            aw = max(int(self._video_stack.winfo_width()), 360)
            ah = max(int(self._video_stack.winfo_height()), 240)
            pil = ctrl._broken_video_placeholder_pil(size=(aw, ah))
            self._broken_playback_overlay_img = ctk.CTkImage(
                light_image=pil, dark_image=pil, size=(aw, ah)
            )
            self._broken_playback_overlay.configure(
                image=self._broken_playback_overlay_img, text=""
            )
            try:
                self._broken_playback_overlay.place_forget()
            except Exception:
                pass
            self._broken_playback_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            self._broken_playback_overlay.lift()
            self._broken_playback_overlay.update_idletasks()
            self._broken_playback_overlay_active = True
            logging.info(
                "[BrokenOverlay] shown aw=%s ah=%s (VLC video stays underneath Tk overlay)",
                aw,
                ah,
            )
        except Exception as e:
            logging.info("[BrokenOverlay] failed: %s", e, exc_info=True)

    def _schedule_decode_placeholder_checks(self) -> None:
        """If VLC never exposes a video bitmap (common on badly damaged MP4), show the same red-on-black card as in the grid."""
        self._cancel_broken_decode_checks()
        self._hide_broken_playback_overlay()
        self._broken_decode_check_gen = int(getattr(self, "_broken_decode_check_gen", 0)) + 1
        gen = self._broken_decode_check_gen
        a1 = self.video_window.after(750, lambda: self._decode_placeholder_probe(gen, strict=False))
        a2 = self.video_window.after(1900, lambda: self._decode_placeholder_probe(gen, strict=True))
        self._broken_check_after_ids = [a1, a2]
        logging.info("[BrokenOverlay] scheduled probes gen=%s ids=%s", gen, self._broken_check_after_ids)

    def _decode_placeholder_probe(self, generation: int, strict: bool = False) -> None:
        if generation != getattr(self, "_broken_decode_check_gen", 0):
            return
        if getattr(self, "_broken_playback_overlay_active", False):
            return
        if not self.player:
            return
        try:
            st = self.player.get_state()
        except Exception:
            st = None
        try:
            dur = int(self.player.get_length())
        except Exception:
            dur = -1
        has_dims = self._playback_has_video_dimensions()
        logging.info(
            "[BrokenOverlay] probe gen=%s strict=%s state=%s dur_ms=%s has_dims=%s",
            generation,
            strict,
            st,
            dur,
            has_dims,
        )
        if st == vlc.State.Error:
            self._show_broken_playback_overlay()
            return
        if has_dims:
            return
        if not strict:
            return

        # Final pass: no decoded video plane. Duration 0 is typical for badly damaged MP4
        # even while VLC state stays "Playing" with a black D3D surface.
        if dur <= 0:
            logging.info("[BrokenOverlay] strict: duration unknown/zero -> overlay")
            self._show_broken_playback_overlay()
            return

        if st in (
            vlc.State.Playing,
            vlc.State.Buffering,
            vlc.State.Paused,
            vlc.State.Opening,
            vlc.State.Ended,
        ):
            logging.info("[BrokenOverlay] strict: state=%s no dims -> overlay", st)
            self._show_broken_playback_overlay()
    
    def show_hud(self):
        """
        Zobrazí Overlay HUD (Info) přímo v obraze pomocí OSD funkce VLC.
        Je to bezpečné, neinvazivní a funguje i ve Fullscreenu.
        """
            
        if not getattr(self.controller, "video_show_hud", True) or not self.player:
            return    
            
     

        try:
            # 1. Získání informací
            # Index (např. [1/12])
            index_str = ""
            if self.playlist_manager and self.playlist_manager.playlist:
                current = self.playlist_manager.current_playing_index + 1
                total = len(self.playlist_manager.playlist)
                index_str = f"[{current}/{total}] "
            elif self.controller.video_files:
                 # Fallback pro složku
                 current = self.controller.current_video_index + 1
                 total = len(self.controller.video_files)
                 index_str = f"[{current}/{total}] "

            # Jméno a Čas
            name = self.video_name
            
            # Formátování času (volitelné, pokud to tam chceš)
            # t_curr = int(self.player.get_time() / 1000)
            # t_len = int(self.player.get_length() / 1000)
            # time_str = f"{t_curr//60:02}:{t_curr%60:02} / {t_len//60:02}:{t_len%60:02}"
            
            full_text = f"{index_str}{name}".lower()

            # 2. Poslání do VLC (Marquee - běžící text, ale my ho zastavíme)
            # Konstanty pro VLC Marquee (aby to fungovalo i bez importu vlc enumů)
            _Enable = 0
            _Text = 1
            _Color = 2
            _Opacity = 3
            _Position = 4
            _Size = 30
            _Timeout = 7

            # Zapnout Marquee
            self.player.video_set_marquee_int(_Enable, 1)
            
            # Nastavit text
            self.player.video_set_marquee_string(_Text, full_text)
            
            # Nastavení vzhledu
            self.player.video_set_marquee_int(_Color, 0xB0B3B8)
            self.player.video_set_marquee_int(_Opacity, 200)     # Průhlednost (0-255)
            self.player.video_set_marquee_int(_Size, 24)         # Velikost písma (px)
            self.player.video_set_marquee_int(_Timeout, 3000)    # Zmizí za 3000 ms (3s)
            
            # Pozice: 5 = Vlevo nahoře (TopLeft), 6 = Vpravo nahoře, 10 = Vpravo dole
            self.player.video_set_marquee_int(_Position, 5) 
            
        except Exception as e:
            logging.info(f"[HUD] Chyba při zobrazování OSD: {e}")


    def show_gpu_upscale_diagnostics(self):
        """
        Displays a diagnostic dialog with GPU upscaling status information.
        Checks VLC version, active video output, hardware decoding, GPU vendor,
        and provides actionable tips if RTX VSR / FSR appears inactive.
        """
        import tkinter.messagebox as mb

        vlc_version = vlc.libvlc_get_version().decode("utf-8") if hasattr(vlc, "libvlc_get_version") else "unknown"
        gpu_vendor = get_gpu_vendor()

        upscale_enabled = getattr(self, "use_gpu_upscale", False)
        vout = "--vout=direct3d11" if upscale_enabled else f"--vout={self.video_output}"
        hw_dec = "d3d11va (for RTX VSR)" if upscale_enabled else self.hardware_decoding
        player_state = str(self.player.get_state()) if self.player else "no player"

        # Check VLC version meets minimum requirement (3.0.19)
        try:
            ver_parts = [int(x) for x in vlc_version.split(".")[:3]]
            vlc_ok = ver_parts >= [3, 0, 19]
        except Exception:
            vlc_ok = False
        vlc_status = "OK" if vlc_ok else f"TOO OLD – need 3.0.19+, you have {vlc_version}"

        lines = [
            f"VLC version:        {vlc_version}  [{vlc_status}]",
            f"GPU vendor:         {gpu_vendor}",
            f"GPU Upscale flag:   {'ENABLED' if upscale_enabled else 'disabled'}",
            f"Video output arg:   {vout}",
            f"HW decoding arg:    {hw_dec}",
            f"Player state:       {player_state}",
            "",
            "── RTX VSR checklist ─────────────────────────────────",
            f"{'✅' if vlc_ok else '❌'} VLC 3.0.19+ installed",
            f"{'✅' if upscale_enabled else '❌'} GPU Upscale enabled in Preferences",
            f"{'✅' if gpu_vendor == 'NVIDIA' else '⚠️'} NVIDIA GPU detected ({gpu_vendor})",
            "",
            "⚠️  NVIDIA Control Panel steps required:",
            "   Video → Adjust video image settings",
            "   → RTX video enhancement → Super Resolution → ON",
            "   → Quality: 4 (Ultra Quality)",
            "",
            "⚠️  RTX VSR only activates when:",
            "   • Video resolution < display resolution (e.g. 1080p on 4K screen)",
            "   • VLC uses D3D11VA HW decoding (not software/dxva2)",
            "   • VLC renders via Direct3D11 output",
            "",
            "Tip: check vlc-log.txt for detailed VLC errors.",
        ]

        report = "\n".join(lines)
        logging.info("[GPU Diag]\n" + report)

        mb.showinfo("GPU Upscaling Diagnostics", report)


    def load_vlc_preferences(self):
        # Load VLC preferences
        if os.path.exists("settings.json"):
            with open("settings.json", "r") as pref_file:
                settings = json.load(pref_file)
                if isinstance(settings, dict):
                    self.video_output = settings.get("video_output", self.video_output) or self.video_output
                    self.audio_output = settings.get("audio_output", self.audio_output) or self.audio_output
                    self.hardware_decoding = settings.get("hardware_decoding", self.hardware_decoding) or self.hardware_decoding
                    self.audio_device = settings.get("audio_device", self.audio_device) or self.audio_device
                   
        logging.info(f"Loaded VLC Settings: video_output={self.video_output}, audio_output={self.audio_output}, hardware_decoding={self.hardware_decoding}, audio_device={self.audio_device}")

    # def load_vlc_preferences(self):
        # Load VLC preferences
        # if os.path.exists("settings.json"):
            # with open("settings.json", "r") as pref_file:
                # settings = json.load(pref_file)
                # for entry in settings:
                    # if isinstance(entry, dict):
                        # self.video_output = entry.get("video_output", self.video_output) or self.video_output
                        # self.audio_output = entry.get("audio_output", self.audio_output) or self.audio_output
                        # self.hardware_decoding = entry.get("hardware_decoding", self.hardware_decoding) or self.hardware_decoding
                        # self.audio_device = entry.get("audio_device", self.audio_device) or self.audio_device
        # logging.info(f"Loaded VLC Settings: video_output={self.video_output}, audio_output={self.audio_output}, hardware_decoding={self.hardware_decoding}, audio_device={self.audio_device}")
    
    # _audio_devices_cache = None

    # def get_audio_devices():
        # """Returns a cached list of audio output devices via sounddevice."""
        # global _audio_devices_cache
        # if _audio_devices_cache is None:
            # try:
                # import sounddevice as sd
                # _audio_devices_cache = sd.query_devices()
            # except Exception as e:
                # logging.info(f"[AudioDevice] Failed to query devices: {e}")
                # _audio_devices_cache = []
        # return _audio_devices_cache
    
    
    #  button for start stop video in middle of video window
        
    # Pridaj túto novú metódu do triedy VideoPlayer
    def cleanup(self):
        """
        Zastaví všetky bežiace procesy a uvoľní všetky systémové zdroje.
        """
        logging.info("[Cleanup] Spúšťam upratovanie pre video prehrávač...")
        # 1. Zastav prehrávanie a všetky nekonečné slučky (tým, že playing=False)
        self.playing = False
        
        # 2. Zastav globálny listener myši, ak beží
        if self.listener:
            self.listener.stop()
            self.listener = None
            logging.info("[Cleanup] Listener myši zastavený.")

        # 3. Explicitne uvoľni VLC prehrávač a inštanciu
        if hasattr(self, 'player') and self.player:
            try:
                import time
                self.player.stop()
                deadline = time.time() + 0.3
                while self.player.is_playing() and time.time() < deadline:
                    time.sleep(0.05)
                self.player.release()
                self.player = None
                logging.info("[Cleanup] VLC prehrávač uvoľnený.")
            except Exception as e:
                logging.info(f"[Cleanup] Chyba pri uvoľňovaní prehrávača: {e}")

        if hasattr(self, 'instance') and self.instance:
            try:
                self.instance.release()
                self.instance = None
                logging.info("[Cleanup] VLC inštancia uvoľnená.")
            except Exception as e:
                logging.info(f"[Cleanup] Chyba pri uvoľňovaní VLC inštancie: {e}")

        # 4. Znič okno
        if hasattr(self, 'video_window') and self.video_window.winfo_exists():
            self.video_window.destroy()
            logging.info("[Cleanup] Okno prehrávača zničené.")

    def on_focus_in(self, event=None):
        """Když okno získá focus, nastaví se jako 'vždy nahoře'."""
        if self.video_window.winfo_exists():
            # self.video_window.attributes("-topmost", True)
            self.video_window.winfo_toplevel().attributes("-topmost", True)
            
            logging.info("[Focus] Video player získal focus, je v popředí.")

    def on_focus_out(self, event=None):
        """Když okno ztratí focus, přestane být 'vždy nahoře'."""
        if self.video_window.winfo_exists():
            self.video_window.winfo_toplevel().attributes("-topmost", False)
            logging.info("[Focus] Video player ztratil focus, už není v popředí.")

    def on_panel_resize(self, event=None):
        embed_target = getattr(self, "video_label", self.video_window)
        if self.playing:
            self._safe_set_hwnd(embed_target)
        else:
            # Pokud video neběží, překresli čistě černou
            self.show_placeholder()

    # def toggle_loop_shortcut(self, event=None):
        # logging.info("[DEBUG] toggle_loop_shortcut triggered")
        
        # Toggle loop in TIMELINE WIDGET
        # if hasattr(self, "timeline_widget") and self.timeline_widget:
            # self.timeline_widget.toggle_loop()

        # Toggle loop bar in VIDEO WINDOW
        # if hasattr(self, "current_video_window") and self.current_video_window:
            # vp = self.current_video_window
            # if hasattr(self.timeline_widget, "loop_mode"):
                # is_on = self.timeline_widget.loop_mode
                # logging.info(f"[DEBUG] toggle_loop_shortcut → loop_mode = {is_on}, updating video window bar")
                # if is_on:
                    # vp.draw_loop_bar()
                # else:
                    # vp.clear_loop()


    def draw_loop_bar(self):
        self.loop_bar_canvas.delete("all")

        # Kresli pouze pokud je smyčka aktivní a má platné hranice
        if not self.loop_active or self.loop_start is None or self.loop_end is None:
            return

        duration = self.player.get_length()
        if duration <= 0:
            return

        canvas_width = self.loop_bar_canvas.winfo_width()
        x1 = int(self.loop_start / (duration / 1000.0) * canvas_width)
        x2 = int(self.loop_end / (duration / 1000.0) * canvas_width)
        
        if x1 >= x2:
            return

        self.loop_bar_canvas.create_line(x1, 2, x2, 2, fill="#1ee810", width=5)



    # Soubor: video_operations.py
    # Uvnitř třídy VideoPlayer

    def set_loop_start(self, event=None):
        if not self.player:
            return
        self.loop_start = self.player.get_time() / 1000.0
        self.loop_active = True
        logging.info(f"[🔁] Loop START nastaven: {self.loop_start:.2f}s. Smyčka je aktivní.")
        self.update_loop_bar_display() # <-- ZMĚNA

        # PŘIDAT: Informuj timeline, aby se překreslila
        if self.timeline_widget:
            self.timeline_widget.redraw_timeline()

    def set_loop_end(self, event=None):
        if not self.player:
            return
        self.loop_end = self.player.get_time() / 1000.0
        self.loop_active = True
        logging.info(f"[🔁] Loop END nastaven: {self.loop_end:.2f}s. Smyčka je aktivní.")
        self.update_loop_bar_display() # <-- ZMĚNA
      
        # PŘIDAT: Informuj timeline, aby se překreslila
        if self.timeline_widget:
            self.timeline_widget.redraw_timeline()



    def toggle_loop(self, event=None):
        """
        Toggles the internal loop state and seeks to the start if activated.
        Also ensures the loop bar display is updated.
        """
        _prev_loop = getattr(self, "loop_active", False)
        self.loop_active = not _prev_loop
        logging.info(f"[VideoPlayer] Loop is now: {'ACTIVE' if self.loop_active else 'INACTIVE'}")

        # If loop was just activated, seek to the start.
        if self.loop_active and self.loop_start is not None:
            if hasattr(self, "player") and self.player:
                start_time_ms = int(self.loop_start * 1000)
                self.player.set_time(start_time_ms)
                logging.info(f"[VideoPlayer] Seeked to loop start: {self.loop_start:.2f}s")
        
        # --- THIS IS THE KEY LINE ---
        # Always update the visual loop bar display, regardless of state change.
        self.update_loop_bar_display()




    # def clear_loop(self, event=None):
        # self.loop_start = None
        # self.loop_end = None
        # self.loop_active = False # <-- Vypnout smyčku
        # logging.info("🔁 Smyčka byla zrušena.")
        # self.draw_loop_bar()



    # Soubor: video_operations.py
    # Uvnitř třídy VideoPlayer

# Soubor: video_operations.py
# Uvnitř třídy VideoPlayer

    def set_loop_start_from_timeline(self, new_time):
        """Metoda volaná z timeline widgetu při dragování."""
        duration_s = self.player.get_length() / 1000.0
        min_dist = max(0.1, duration_s * 0.01) if duration_s > 0 else 0.1
        
        self.loop_start = min(new_time, self.loop_end - min_dist)
        self.redraw_timeline_widget() # Okamžitě překreslí timeline pro plynulou odezvu

    def set_loop_end_from_timeline(self, new_time):
        """Metoda volaná z timeline widgetu při dragování."""
        duration_s = self.player.get_length() / 1000.0
        min_dist = max(0.1, duration_s * 0.01) if duration_s > 0 else 0.1

        self.loop_end = max(new_time, self.loop_start + min_dist)
        self.redraw_timeline_widget() # Okamžitě překreslí timeline pro plynulou odezvu

    def redraw_timeline_widget(self):
        """Pomocná funkce pro překreslení timeline."""
        if hasattr(self, 'timeline_widget') and self.timeline_widget:
            self.timeline_widget.redraw_timeline()

    def update_loop_bar_display(self):
            """
            Vynutí překreslení zelené čáry smyčky pod hlavním posuvníkem.
            Tuto metodu je bezpečné volat z externích widgetů.
            """
            if hasattr(self, 'loop_bar_canvas') and self.loop_bar_canvas.winfo_exists():
                self.draw_loop_bar()



    # SOUBOR: video_operations.py

    def create_vlc_instance(self):
        """
        Creates a VLC instance with robust audio parameter validation
        to prevent errors and ghost instances.

        If self.use_gpu_upscale is True, forces Direct3D11 output and enables
        GPU upscaling (NVIDIA RTX Super Resolution or AMD FSR) with full
        hardware acceleration via --avcodec-hw=any.
        Requires VLC 3.0.19+ and GPU drivers with upscaling enabled in the
        NVIDIA Control Panel or AMD Software. A player restart is needed
        when this setting is toggled.
        """
        logging.info("--- [VLC PRE-CHECK] ---")
        logging.info(f"  > Video Output: {self.video_output}")
        logging.info(f"  > Audio Output: {self.audio_output}")
        logging.info(f"  > HW Decoding: {self.hardware_decoding}")
        logging.info(f"  > Audio Device: {self.audio_device}")
        logging.info(f"  > GPU Upscale: {self.use_gpu_upscale}")
        logging.info("-------------------------")

        if self.use_gpu_upscale:
            gpu_vendor = get_gpu_vendor()
            logging.info(f"[GPU Upscale] Detected vendor: {gpu_vendor}. Enabling GPU upscaling mode.")
            # RTX Video Super Resolution requires the D3D11VA hardware decoder specifically.
            # --avcodec-hw=any may fall back to the older DXVA2 path which bypasses RTX VSR.
            # d3d11va keeps decoded frames in GPU memory as D3D11 textures, which is the
            # prerequisite for the NVIDIA driver to intercept and apply Super Resolution.
            # AMD FSR similarly needs the D3D11 presentation path.
            vlc_options = [
                '--vout=direct3d11',
                '--avcodec-hw=d3d11va',
                '--file-logging',
                '--logfile=vlc-log.txt',
            ]
            logging.info(f"[GPU Upscale] Args: --vout=direct3d11 --avcodec-hw=d3d11va  (vendor={gpu_vendor})")
        else:
            vlc_options = [
                f'--vout={self.video_output}',
                f'--avcodec-hw={self.hardware_decoding}',
                '--file-logging',
                '--logfile=vlc-log.txt'
            ]

        # Optional video quality filters configured in app preferences.
        # VLC accepts multiple filters as a colon-separated chain.
        filter_chain = []
        if getattr(self.controller, "vlc_enable_postproc", False):
            filter_chain.append("postproc")
            postproc_q = int(getattr(self.controller, "vlc_postproc_quality", 6))
            postproc_q = max(0, min(6, postproc_q))
            vlc_options.append(f'--postproc-q={postproc_q}')
        if getattr(self.controller, "vlc_enable_gradfun", False):
            filter_chain.append("gradfun")
        if filter_chain:
            vlc_options.append(f'--video-filter={":".join(filter_chain)}')
        if getattr(self.controller, "vlc_enable_deinterlace", False):
            vlc_options.append('--deinterlace=1')
        if getattr(self.controller, "vlc_skiploopfilter_disable", False):
            vlc_options.append('--avcodec-skiploopfilter=0')

        # 1. Audio výstupní modul (--aout).
        #
        #    VLC na Windows zkouší postupně: wasapi → directsound → waveout.
        #    Pokud sounddevice/PortAudio drží handle na WASAPI nebo DirectSound
        #    (inicializace audio zařízení v _delayed_audio_init), VLC dostane:
        #      "wasapi error: unsupported audio format"
        #      "directsound error: cannot open directx audio device"
        #    a audio selže úplně.
        #
        #    WaveOut (Windows Multimedia API) funguje vždy – nepotřebuje
        #    exkluzivní přístup, Windows sám řeší resampling a routování
        #    na správné výstupní zařízení (respektuje Windows default).
        has_explicit_device = bool(self.audio_device and str(self.audio_device).strip())

        if self.audio_output and self.audio_output not in ("", "default", "directsound"):
            # Některé moduly (zejména wasapi/mmdevice) umí na části sestav viset
            # nebo failnout při initu bez explicitního zařízení.
            # V takovém případě raději použijeme stabilní waveout.
            if self.audio_output in ("wasapi", "mmdevice") and not has_explicit_device:
                vlc_options.append('--aout=waveout')
                logging.info(
                    "[VLC Audio] '%s' bez explicitního zařízení -> fallback na waveout",
                    self.audio_output,
                )
            else:
                # Uživatel zvolil konkrétní modul (např. directsound s device) – respektuj ho.
                vlc_options.append(f'--aout={self.audio_output}')
        elif self.audio_output == 'directsound' and has_explicit_device:
            # directsound s explicitním zařízením – použij ho.
            vlc_options.append('--aout=directsound')
        else:
            # Žádné explicitní audio nebo directsound bez zařízení →
            # waveout je nejkompatibilnější volba na Windows.
            vlc_options.append('--aout=waveout')
            logging.info("[VLC Audio] Používám waveout (nejkompatibilnější, nevyžaduje exkluzivní přístup)")

        # 2. Přidej konkrétní DirectSound zařízení, pouze pokud je explicitně nastaveno.
        if self.audio_output == 'directsound' and has_explicit_device:
            vlc_options.append(f'--directx-audio-device={self.audio_device}')

        # 3. Přehledný výpis finálního příkazu pro snadné ladění.
        complete_vlc_command = " ".join(vlc_options)
        logging.info(f"Creating VLC instance with command: {complete_vlc_command}")

        # 4. Safe instance creation with fallback.
        # If GPU upscale args fail, retry with standard settings so playback is not blocked.
        try:
            instance = vlc.Instance(*vlc_options)
            if instance:
                return instance
            raise RuntimeError("vlc.Instance() returned None")
        except Exception as e:
            logging.error(f"[VLC] Error creating instance: {e}")
            if self.use_gpu_upscale:
                logging.warning("[GPU Upscale] GPU upscale instance failed. Falling back to standard VLC settings.")
                fallback_options = [
                    f'--vout={self.video_output}',
                    f'--avcodec-hw={self.hardware_decoding}',
                    '--file-logging',
                    '--logfile=vlc-log.txt',
                    f'--aout=waveout',
                ]
                if getattr(self.controller, "vlc_enable_postproc", False):
                    postproc_q = int(getattr(self.controller, "vlc_postproc_quality", 6))
                    postproc_q = max(0, min(6, postproc_q))
                    fallback_options.append(f'--postproc-q={postproc_q}')
                filter_chain = []
                if getattr(self.controller, "vlc_enable_postproc", False):
                    filter_chain.append("postproc")
                if getattr(self.controller, "vlc_enable_gradfun", False):
                    filter_chain.append("gradfun")
                if filter_chain:
                    fallback_options.append(f'--video-filter={":".join(filter_chain)}')
                if getattr(self.controller, "vlc_enable_deinterlace", False):
                    fallback_options.append('--deinterlace=1')
                if getattr(self.controller, "vlc_skiploopfilter_disable", False):
                    fallback_options.append('--avcodec-skiploopfilter=0')
                try:
                    instance = vlc.Instance(*fallback_options)
                    if instance:
                        logging.info("[VLC] Fallback instance created successfully.")
                        return instance
                except Exception as e2:
                    logging.error(f"[VLC] Fallback instance also failed: {e2}")
            return None

    def _ensure_vlc_player(self) -> bool:
        """Lazily create VLC Instance and MediaPlayer (must run on the Tk main thread)."""
        if self.instance and self.player:
            return True
        if not self.instance:
            self.instance = self.create_vlc_instance()
            if not self.instance:
                logging.error("[VLC] Failed to create VLC instance.")
                return False
        if not self.player:
            self.player = self.instance.media_player_new()
        return True

    def apply_preferences(self):
        self.load_vlc_preferences()
        self.instance = self.create_vlc_instance()
        if self.instance:
            self.player = self.instance.media_player_new()
            logging.info("VLC settings applied and new instance created.")  # Debug
        else:
            logging.info("Failed to apply VLC preferences.")  # Debug

    def add_bookmark(self):
        """
        Ask for name and add bookmark at current playback time using app's universal dialog.
        """
        if not self.player:
            return

        current_time = self.player.get_time() / 1000
        default_name = f"Bookmark {len(self.bookmarks)+1}"

        def on_confirm(name):
            name = name.strip()
            if name:
                if len(self.bookmarks) < 500:
                    self.bookmarks.append({"name": name, "time": current_time})
                    self.save_bookmarks()
                    logging.info(f"Added bookmark: {name} @ {current_time}s")
                    if hasattr(self, "timeline_widget"):
                        logging.info("[DEBUG] ⟳ update_bookmarks + redraw_timeline kvůli novému bookmarku")
                        self.timeline_widget.update_bookmarks()
                        self.timeline_widget.redraw_timeline()
                    else:
                        logging.info("[DEBUG] timeline_widget není dostupný v VideoPlayer")

        # Use the controller's universal_dialog
        if hasattr(self.controller, 'universal_dialog'):
            self.controller.universal_dialog(
                title="Add Bookmark",
                message="Enter bookmark name:",
                confirm_callback=on_confirm,
                input_field=True,
                default_input=default_name
            )
        else:
            logging.info("universal_dialog not available")

    def skip_to_next_bookmark(self):
        """Seek to the nearest bookmark after current playback time."""
        if not self.player:
            return
        if not self.bookmarks:
            logging.info("[Bookmark] No bookmarks available.")
            return

        current_time = max(0.0, self.player.get_time() / 1000.0)
        sorted_times = sorted(
            float(b.get("time", 0.0))
            for b in self.bookmarks
            if isinstance(b, dict) and b.get("time") is not None
        )
        next_time = next((t for t in sorted_times if t > current_time + 0.05), None)
        if next_time is None:
            logging.info("[Bookmark] Already at or beyond the last bookmark.")
            return

        self.player.set_time(int(next_time * 1000))
        self.last_position = int(next_time * 1000)
        logging.info("[Bookmark] Jumped to next bookmark: %.2fs", next_time)

    def skip_to_previous_bookmark(self):
        """Seek to the nearest bookmark before current playback time."""
        if not self.player:
            return
        if not self.bookmarks:
            logging.info("[Bookmark] No bookmarks available.")
            return

        current_time = max(0.0, self.player.get_time() / 1000.0)
        sorted_times = sorted(
            float(b.get("time", 0.0))
            for b in self.bookmarks
            if isinstance(b, dict) and b.get("time") is not None
        )
        prev_candidates = [t for t in sorted_times if t < current_time - 0.05]
        prev_time = prev_candidates[-1] if prev_candidates else None
        if prev_time is None:
            logging.info("[Bookmark] Already at or before the first bookmark.")
            return

        self.player.set_time(int(prev_time * 1000))
        self.last_position = int(prev_time * 1000)
        logging.info("[Bookmark] Jumped to previous bookmark: %.2fs", prev_time)

    def long_seek(self, direction: int, seconds: float = 10.0):
        """Seek by a larger fixed delta. direction: +1 forward, -1 backward."""
        if not self.player:
            return
        try:
            current_ms = max(0, int(self.player.get_time()))
            duration_ms = max(0, int(self.player.get_length() or 0))
        except Exception:
            return

        delta_ms = int(max(0.1, float(seconds)) * 1000) * (1 if direction >= 0 else -1)
        target_ms = current_ms + delta_ms
        if duration_ms > 0:
            target_ms = min(max(0, target_ms), duration_ms)
        else:
            target_ms = max(0, target_ms)

        self.player.set_time(target_ms)
        self.last_position = target_ms
        logging.info("[Seek] Long seek %s by %.1fs to %.2fs", "forward" if direction >= 0 else "backward", seconds, target_ms / 1000.0)



    def remove_bookmark_at(self, timestamp):
            """Removes the bookmark closest to the given timestamp (threshold 2s)."""
            threshold = 2.0 
            new_bookmarks = [b for b in self.markers if not (b["type"] == "bookmark" and abs(b["timestamp"] - timestamp) < threshold)]
            
            if len(new_bookmarks) < len(self.markers):
                # Tady pozor: self.markers obsahuje i titulky atd. Musíme aktualizovat 
                # zdroj dat v přehrávači!
                if hasattr(self.controller, 'current_video_window'):
                    player_win = self.controller.current_video_window
                    # Filtrujeme jen ty, co zbyly
                    player_win.bookmarks = [b for b in player_win.bookmarks if abs(b["time"] - timestamp) >= threshold]
                    player_win.save_bookmarks()
                    self.update_bookmarks()
                    self.redraw_timeline()
                    logging.info(f"Bookmark removed near {timestamp}s")


    def save_bookmarks(self):
        """
        Save all bookmarks to a JSON file.
        """
        os.makedirs("bookmarks", exist_ok=True)
        try:
            with open(self.bookmark_file, "w", encoding="utf-8") as f:
                json.dump(self.bookmarks, f, indent=2)
        except Exception as e:
            logging.info(f"Failed to save bookmarks: {e}")

    def load_bookmarks(self):
        """
        Load bookmarks from JSON file if available.
        """
        if os.path.exists(self.bookmark_file):
            try:
                with open(self.bookmark_file, "r", encoding="utf-8") as f:
                    self.bookmarks = json.load(f)
            except Exception as e:
                logging.info(f"Failed to load bookmarks: {e}")
                self.bookmarks = []



    def generate_thumbnail(self, event=None):
        # Ensure a video is currently playing
        if not (self.player and self.player.is_playing()):
            logging.info("No video is currently playing.")
            return

        logging.info("Will create thumbnail for the currently playing video.")
        current_video = self.video_path  # path to the currently playing video

        # Check if we have a valid video path
        if not current_video:
            logging.info("No video path available.")
            return

        logging.info(f"current video: {current_video}")

        # Match grid entry (normcase: player path vs tree key can differ on Windows)
        thumbnail_info = self.controller.thumbnail_labels.get(current_video)
        if not thumbnail_info:
            nv = os.path.normcase(os.path.normpath(current_video))
            for key, info in self.controller.thumbnail_labels.items():
                try:
                    if os.path.normcase(os.path.normpath(key)) == nv:
                        thumbnail_info = info
                        current_video = key
                        break
                except Exception:
                    continue
        if not thumbnail_info:
            logging.info("Thumbnail not found in current layout.")
            return

        row, col = thumbnail_info["row"], thumbnail_info["col"]
        index = thumbnail_info["index"]

        thumbnail_format = self.controller.thumbnail_format
        current_time = self.player.get_time() / 1000
        total_duration = get_video_duration_mediainfo(current_video)

        if not total_duration or total_duration <= 0:
            logging.info(f"[WARN] Could not retrieve total duration for {current_video}, using fallback 1s.")
            total_duration = 1

        thumbnail_time = current_time

        logging.info(f"Generating thumbnail at {thumbnail_time:.1f}s of {self.video_name}")

        # Virtual grid draws on canvas slots; regular_thumbnails_frame is hidden — use same path as RMB refresh.
        if getattr(self.controller, "_vg_active", False):
            if hasattr(self.controller, "database"):
                self.controller.database.set_thumbnail_timestamp(
                    current_video, float(thumbnail_time)
                )
                logging.info(
                    "[DEBUG] Saved thumbnail timestamp: %.2fs", float(thumbnail_time)
                )
            self.controller.refresh_single_thumbnail(
                current_video, overwrite=True, at_time=float(thumbnail_time)
            )
            if hasattr(self.controller, "_demo_toast"):
                self.controller._demo_toast("demo_thumbs")
            return

        thumbnail = create_video_thumbnail(
            video_path=current_video,
            thumbnail_size=self.controller.thumbnail_size,
            thumbnail_format=thumbnail_format,
            capture_method=self.controller.capture_method_var.get(),
            thumbnail_time=thumbnail_time,
            cache_enabled=self.controller.cache_enabled,
            overwrite=False,
            cache_dir=self.controller.thumbnail_cache_path,
            database=getattr(self.controller, "database", None),
        )

        if thumbnail:
            logging.info(f"Thumbnail generated for {self.video_name} at {thumbnail_time:.1f}s")

            if hasattr(self.controller, "database"):
                self.controller.database.set_thumbnail_timestamp(
                    current_video, float(thumbnail_time)
                )
                logging.info(
                    "[DEBUG] Saved thumbnail timestamp: %.2fs", float(thumbnail_time)
                )

            self.controller.create_file_thumbnail(
                file_path=current_video,
                file_name=self.video_name,
                row=row,
                col=col,
                index=index,
                thumbnail_time=thumbnail_time,
                overwrite=True,
                target_frame=self.controller.regular_thumbnails_frame,
            )
            if hasattr(self.controller, "_demo_toast"):
                self.controller._demo_toast("demo_thumbs")
        else:
            logging.info(f"Failed to generate thumbnail for {self.video_name}. Format: {thumbnail_format}")



        
    
    # def generate_thumbnail(self, event=None):
        # current_time = self.player.get_time() // 1000  # Get current playback time in seconds
        # thumbnail = create_video_thumbnail(self.video_path, self.parent.thumbnail_size, thumbnail_time=current_time, overwrite=True)
        # if thumbnail:
            # logging.info(f"Thumbnail generated for {self.video_name} at {current_time} seconds.")
            # self.controller.refresh_thumbnail(self.video_path)  # Call to refresh the thumbnail browser
        # else:
            # logging.info(f"Failed to generate thumbnail for {self.video_name} at {current_time} seconds.")
    
    
    # def toggle_repeat(self):
            # self.is_repeating = not self.is_repeating
            # if self.is_repeating:
                # self.repeat_button.configure(text="Repeat On")
                # self.player.event_manager().event_attach(vlc.EventType.MediaPlayerEndReached, self.on_video_end)
            # else:
                # self.repeat_button.configure(text="Repeat Off")
                # self.player.event_manager().event_detach(vlc.EventType.MediaPlayerEndReached)

    def toggle_subtitles(self):
        self.subtitles_enabled = not self.subtitles_enabled
        self.refresh_subtitles(restart_media=True)
        logging.info(f"Subtitles are now {'on' if self.subtitles_enabled else 'off'}.")




    def toggle_repeat(self):
        self.is_repeating = not self.is_repeating

        if self.is_repeating:
            self.repeat_button.configure(
                text="",
                image=self.repeat_icon,
                fg_color=self.surface_high,
                hover_color=self.surface_high,
            )
            threading.Thread(target=self.monitor_video, daemon=True).start()  # Start monitoring in a new thread
        else:
            self.repeat_button.configure(
                text="",
                image=self.repeat_icon,
                fg_color=self.surface_low,
                hover_color=self.surface_low,
            )

        logging.info(f"Repeat is now {'on' if self.is_repeating else 'off'}.")

    def monitor_video(self):
        check_interval = 0.05  # Check every 50 ms
        end_threshold = 900  # 100 ms before the end

        while self.is_repeating:
            current_time = self.player.get_time()
            duration = self.player.get_length()
            
            # print (f"monitoring   duration  {duration}   time  {current_time}   duration- time :  {duration - current_time}  ")  
            
            if duration - current_time <= end_threshold:  # Smaller threshold for better precision
                self.player.set_time(0)
                self.player.play()
                logging.info(f"Video is restarting... (End detected at {current_time}/{duration} ms)")
           
            time.sleep(check_interval)  # Increased frequency of checks




    # def on_video_end(self, event):
        # if self.is_repeating:
            # def restart_video():
                # self.player.set_time(0)  # Seek to the start of the video

                # self.player.set_time(0)

                # logging.info("Video has ended, restarting...")

            # threading.Thread(target=restart_video).start()
                    
    def update_timer(self):
        if self.player and self.playing:
            current_time = int(self.player.get_time() / 1000)  # sekundy, int!
            total_time = int(self.player.get_length() / 1000)  # sekundy, int!
            
            # Format the time as MM:SS
            current_time_formatted = f"{current_time // 60:02}:{current_time % 60:02}"
            total_time_formatted = f"{total_time // 60:02}:{total_time % 60:02}"
            
            # Update the timer label
            if hasattr(self, "timer_label") and self.timer_label.winfo_exists():
                self.timer_label.configure(
                text=f"{current_time_formatted} / {total_time_formatted}".lower()
            )
        
        # Schedule the next update
        self.video_window.after(1000, self.update_timer)

    

    def setup_volume_slider(self):
        """
        Sets up the volume controls with a speaker icon and a shorter slider.
        The speaker icon also acts as a mute button.
        """
        # Create a frame for the volume controls
        volume_frame = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        # Right edge aligns with timeline's right edge:
        # sub_frame has 8px outer padding + slider has 8px right padding => 16px.
        volume_frame.pack(side=ctk.RIGHT, padx=(8, 16), pady=(0, 2))

        # Create a clickable label with the speaker icon
        self.volume_label = ctk.CTkLabel(
            volume_frame,
            text="",
            image=self.volume_icon,
            cursor="hand2",
            text_color=self.on_surface,
        )
        self.volume_label.pack(side=ctk.LEFT, padx=(2, 2))
        self.volume_label.bind("<Button-1>", lambda e: self.toggle_mute())
        self._bind_icon_hover(
            self.volume_label,
            getattr(self, "volume_icon", None),
            getattr(self, "volume_icon_hover", None),
        )
        self.last_volume_before_mute = self.current_volume  # Store the initial volume

        # Taller hit strip (~timeline thickness in the middle, generous vertical drag target).
        self.volume_hit_host = ctk.CTkFrame(volume_frame, fg_color="transparent", width=80, height=20)
        self.volume_hit_host.pack(side=ctk.LEFT, padx=(2, 0))
        self.volume_hit_host.pack_propagate(False)

        self.volume_slider = ctk.CTkSlider(
            self.volume_hit_host,
            from_=0,
            to=100,
            orientation="horizontal",
            width=80,
            command=self.update_volume,
            fg_color=self.outline_soft,
            progress_color=self.volume_primary,
            button_color=self.outline_soft,
            button_hover_color=self.outline_soft,
            button_corner_radius=0,
            button_length=2,
            corner_radius=0,
            height=2,
        )
        self.volume_slider.set(self.current_volume)
        self.volume_slider.place(relx=0.5, rely=0.5, anchor="center")

        for _seq in ("<Button-1>", "<B1-Motion>"):
            self.volume_hit_host.bind(_seq, self._volume_drag)
            self.volume_slider.bind(_seq, self._volume_drag)

    def toggle_mute(self):
        """
        Toggles the volume between mute (0) and the last known volume level.
        """
        if self.player.audio_get_volume() > 0:
            # If sound is on, mute it
            self.last_volume_before_mute = self.player.audio_get_volume()
            self.player.audio_set_volume(0)
            self.volume_slider.set(0)
            logging.info("Volume muted.")
        else:
            # If sound is muted, restore it to the last known volume
            self.player.audio_set_volume(self.last_volume_before_mute)
            self.volume_slider.set(self.last_volume_before_mute)
            logging.info(f"Volume unmuted to {self.last_volume_before_mute}%.")
        

    
    
    # def setup_volume_slider(self):
        # style = ttk.Style()
        # style.theme_use('default')

        # Load the custom images for the slider trough and thumb
        # self.volume_trough_image = PhotoImage(file="volume.png")
        # self.triangle_thumb_image = PhotoImage(file="triangle_thumb.png")

        # Check if the custom element already exists before creating it
        # try:
            # style.element_create("custom.Horizontal.Scale.trough", "image", self.volume_trough_image)
            # style.element_create("custom.Horizontal.Scale.slider", "image", self.triangle_thumb_image)
        # except tk.TclError:
            # pass  # Elements already exist

        # style.layout("Custom.Horizontal.TScale",
                     # [('custom.Horizontal.Scale.trough', {'sticky': 'nswe'}),
                      # ('custom.Horizontal.Scale.slider', {'side': 'left', 'sticky': ''})])

        # style.configure("Custom.Horizontal.TScale",
                        # troughcolor='grey',
                        # sliderthickness=15)

        # volume_frame = ttk.Frame(self.controls_frame)
        # volume_frame.pack(side=tk.RIGHT, padx=5)

        # Adding a label to indicate volume
        # volume_label = ttk.Label(volume_frame, text="Volume:")
        # volume_label.pack(side=tk.LEFT, padx=5)

        # self.volume_slider = ttk.Scale(volume_frame, from_=0, to=100, orient=tk.HORIZONTAL, style="Custom.Horizontal.TScale",
                                       # command=self.update_volume)
        # self.volume_slider.set(100)  # Default volume level
        # self.volume_slider.pack(side=tk.LEFT, padx=5)

        # self.update_volume(self.volume_slider.get())

    def display_first_frame(self):
        if not self.video_path:
            logging.info("[VideoPlayer] display_first_frame: no video_path, skipped.")
            return
        if not self._ensure_vlc_player():
            return

        media = self.instance.media_new(self.video_path)
        self.player.set_media(media)
        self._mark_media_loaded()
        self._safe_set_hwnd(self.video_label)

        # In some VLC builds we must briefly play first, then pause.
        # Keep this non-blocking to avoid UI stalls.
        self.player.play()
        self.video_window.after(120, lambda: self.player.pause() if self.player else None)
        self._schedule_decode_placeholder_checks()

        logging.info("First frame displayed (paused).")  # Debug


    def load_icons(self):
            """
            Loads player control icons from the /icons subdirectory using the parent's default directory.
            NOTE: overwrites original paths to look into the new icons folder.
            """
            try:
                # Sestavíme cestu k icons adresáři
                # self.parent je hlavní VideoThumbnailPlayer, který má nastavenou cestu v default_directory
                P = os.path.join(self.parent.default_directory, "icons")
                
                # Načtení ikon s novou cestou (normál + hover cyan)
                self.play_button_icon, self.play_button_icon_hover = self._icon_hover_pair(
                    os.path.join(P, "play.png")
                )
                self.stop_button_icon, self.stop_button_icon_hover = self._icon_hover_pair(
                    os.path.join(P, "stop.png")
                )
                self.rewind_start_button_icon, self.rewind_start_button_icon_hover = (
                    self._icon_hover_pair(os.path.join(P, "rewind_start.png"))
                )
                self.rewind_end_button_icon, self.rewind_end_button_icon_hover = (
                    self._icon_hover_pair(os.path.join(P, "rewind_end.png"))
                )
                self.skip_next_button_icon, self.skip_next_button_icon_hover = (
                    self._icon_hover_pair(os.path.join(P, "skip_next.png"))
                )
                self.skip_back_button_icon, self.skip_back_button_icon_hover = (
                    self._icon_hover_pair(os.path.join(P, "skip_back.png"))
                )
                self.fullscreen_button_icon, self.fullscreen_button_icon_hover = (
                    self._icon_hover_pair(os.path.join(P, "fullscreen.png"))
                )
                self.repeat_icon, self.repeat_icon_hover = self._icon_hover_pair(
                    os.path.join(P, "repeat.png"), alpha_mult=0.8
                )
                self.settings_icon, self.settings_icon_hover = self._icon_hover_pair(
                    os.path.join(P, "settings.png"), tint_color="#545959"
                )
                self.loop_menu_icon = None
                self.loop_menu_icon_hover = None
                for _loop_name in ("loop_ab.png", "loop_range.png", "loop_menu.png"):
                    _lp = os.path.join(P, _loop_name)
                    if os.path.isfile(_lp):
                        self.loop_menu_icon, self.loop_menu_icon_hover = (
                            self._icon_hover_pair(_lp, tint_color="#545959")
                        )
                        break
                self.playlist_icon = self._load_tinted_icon(os.path.join(P, "playlist_ico.png"))
                self.subtitles_icon = self._load_tinted_icon(os.path.join(P, "subtitles.png"))
                self.volume_icon, self.volume_icon_hover = self._icon_hover_pair(
                    os.path.join(P, "volume.png")
                )
                
                logging.info("Player icons loaded successfully from /icons.")
            except Exception as e:
                logging.error("Icon files not found in /icons, using text buttons: %s", e)
    
    def load_iconsOld(self):
        try:
            # Use CTkImage instead of PhotoImage
            self.play_button_icon = ctk.CTkImage(Image.open("play.png"), size=(20, 20))
            self.stop_button_icon = ctk.CTkImage(Image.open("stop.png"), size=(20, 20))
            self.rewind_start_button_icon = ctk.CTkImage(Image.open("rewind_start.png"), size=(20, 20))
            self.rewind_end_button_icon = ctk.CTkImage(Image.open("rewind_end.png"), size=(20, 20))
            self.skip_next_button_icon = ctk.CTkImage(Image.open("skip_next.png"), size=(20, 20))
            self.skip_back_button_icon = ctk.CTkImage(Image.open("skip_back.png"), size=(20, 20))
            self.fullscreen_button_icon = ctk.CTkImage(Image.open("fullscreen.png"), size=(20, 20))
            self.repeat_icon = ctk.CTkImage(Image.open("repeat.png"), size=(20, 20))
            self.playlist_icon = ctk.CTkImage(Image.open("playlist_ico.png"), size=(20, 20))
            self.subtitles_icon = ctk.CTkImage(Image.open("subtitles.png"), size=(20, 20))
            self.volume_icon = ctk.CTkImage(Image.open("volume.png"), size=(20, 20))
            
            logging.info("Icons loaded successfully.")  # Debug
        except Exception as e:
            logging.info("Icon files not found, using text buttons instead: %s", e)  # Debug

    # def load_icons(self):
        # try:
            # self.play_button_icon = tk.PhotoImage(file="play.png")
            # self.stop_button_icon = tk.PhotoImage(file="stop.png")
            # self.rewind_start_button_icon = tk.PhotoImage(file="rewind_start.png")
            # self.rewind_end_button_icon = tk.PhotoImage(file="rewind_end.png")
            # self.skip_next_button_icon = tk.PhotoImage(file="skip_next.png")
            # self.skip_back_button_icon = tk.PhotoImage(file="skip_back.png")
            # self.fullscreen_button_icon = tk.PhotoImage(file="fullscreen.png")
            # logging.info("Icons loaded successfully.")  # Debug
        # except Exception as e:
            # logging.info("Icon files not found, using text buttons instead:", e)  # Debug



    def create_buttons(self):
        
        # def try_show_playlist():
            # if hasattr(self, "playlist_manager") and callable(getattr(self.playlist_manager, "show_playlist", None)):
                # self.playlist_manager.show_playlist()
            # else:
                # logging.info("[❌] Cannot open playlist – playlist_manager or show_playlist is not available.")

        
        
        buttonWidth = 40
        # CTkButton rejects transparent for hover_color; match controls bar so hover = no extra chrome.
        toolbar_bg = self.surface_low
        buttonFG_color = toolbar_bg
        icon_hover = toolbar_bg
        button_text = self.on_surface
        btn_padx = 6
        # Hover feedback = cyan icon only (see _wire_toolbar_icon_hovers).
        # Replace ttk.Button with CTkButton and remove text for buttons with icons
        self.play_button = ctk.CTkButton(self.controls_frame, width=buttonWidth, height=28, text="", image=self.play_button_icon, command=self.toggle_play, fg_color=buttonFG_color, hover_color=icon_hover, text_color=button_text, corner_radius=4)
        self.rewind_start_button = ctk.CTkButton(self.controls_frame, width=buttonWidth, height=28, fg_color=buttonFG_color, hover_color=icon_hover, text_color=button_text, corner_radius=4, text="", image=self.rewind_start_button_icon, command=self.rewind_to_start)
        self.rewind_end_button = ctk.CTkButton(self.controls_frame, width=buttonWidth, height=28, fg_color=buttonFG_color, hover_color=icon_hover, text_color=button_text, corner_radius=4, text="", image=self.rewind_end_button_icon, command=self.rewind_to_end)
        # self.close_button = ctk.CTkButton(self.controls_frame,width= 65, text="Close", fg_color=buttonFG_color, command=self.close_video_player)  # No icon, so keep the text
        self.skip_back_button = ctk.CTkButton(self.controls_frame, width=buttonWidth, height=28, fg_color=buttonFG_color, hover_color=icon_hover, text_color=button_text, corner_radius=4, text="", image=self.skip_back_button_icon, command=self.skip_back)
        self.skip_next_button = ctk.CTkButton(self.controls_frame, width=buttonWidth, height=28, fg_color=buttonFG_color, hover_color=icon_hover, text_color=button_text, corner_radius=4, text="", image=self.skip_next_button_icon, command=self.skip_next)
        self.fullscreen_button = ctk.CTkButton(self.controls_frame, width=buttonWidth, height=28, fg_color=buttonFG_color, hover_color=icon_hover, text_color=button_text, corner_radius=4, text="", image=self.fullscreen_button_icon, command=self.toggle_fullscreen)
        self.repeat_button = ctk.CTkButton(self.controls_frame, width=buttonWidth, height=28, fg_color=buttonFG_color, hover_color=icon_hover, text_color=button_text, corner_radius=4, text="", image=self.repeat_icon, command=self.toggle_repeat)
        _settings_ico = getattr(self, "settings_icon", None)
        self.video_menu_button = ctk.CTkButton(
            self.controls_frame,
            width=buttonWidth,
            height=28,
            fg_color=buttonFG_color,
            hover_color=icon_hover,
            text_color=button_text,
            corner_radius=4,
            text="" if _settings_ico else "⋮",
            image=_settings_ico,
            command=self.show_video_menu
        )

        # )
        _loop_ico = getattr(self, "loop_menu_icon", None)
        self.loop_menu_button = ctk.CTkButton(
            self.controls_frame,
            width=buttonWidth,
            height=28,
            fg_color=buttonFG_color,
            hover_color=icon_hover,
            text_color="#545959" if not _loop_ico else button_text,
            corner_radius=4,
            text="" if _loop_ico else "a>b",
            image=_loop_ico,
            command=self.show_loop_menu
        )


        # self.subtitles_button = ctk.CTkButton(self.controls_frame,width= buttonWidth,fg_color=buttonFG_color, text="", image=self.subtitles_icon, command=self.toggle_subtitles)

        # Pack the buttons
        self.play_button.pack(side=ctk.LEFT, padx=btn_padx, pady=(0, 2))
        self.rewind_start_button.pack(side=ctk.LEFT, padx=btn_padx, pady=(0, 2))
        self.rewind_end_button.pack(side=ctk.LEFT, padx=btn_padx, pady=(0, 2))
        # self.close_button.pack(side=ctk.RIGHT, padx=5)
        self.skip_back_button.pack(side=ctk.LEFT, padx=btn_padx, pady=(0, 2))
        self.skip_next_button.pack(side=ctk.LEFT, padx=btn_padx, pady=(0, 2))
        self.fullscreen_button.pack(side=ctk.LEFT, padx=btn_padx, pady=(0, 2))
        self.repeat_button.pack(side=ctk.LEFT, padx=btn_padx, pady=(0, 2))
        # self.playlist_button.pack(side=ctk.LEFT, padx=5)
        # self.subtitles_button.pack(side=ctk.LEFT, padx=5)
        self.video_menu_button.pack(side=ctk.RIGHT, padx=btn_padx, pady=(0, 2))
        self.loop_menu_button.pack(side=ctk.RIGHT, padx=btn_padx, pady=(0, 2))
        

        # Apply icon to play button if icon is available
        self.play_button.configure(image=self.play_button_icon if self.play_button_icon else None, text="" if self.play_button_icon else "play")

        self._wire_toolbar_icon_hovers()

    # def create_buttons(self):
        # self.play_button = ttk.Button(self.controls_frame, text="Play", command=self.toggle_play)
        # self.rewind_start_button = ttk.Button(self.controls_frame, text="Rewind to Start", command=self.rewind_to_start)
        # self.rewind_end_button = ttk.Button(self.controls_frame, text="Rewind to End", command=self.rewind_to_end)
        # self.close_button = ttk.Button(self.controls_frame, text="Close", command=self.close_video_player)
        # self.skip_back_button = ttk.Button(self.controls_frame, image=self.skip_back_button_icon, command=self.skip_back)
        # self.skip_next_button = ttk.Button(self.controls_frame, image=self.skip_next_button_icon, command=self.skip_next)
        # self.fullscreen_button = ttk.Button(self.controls_frame, image=self.fullscreen_button_icon, command=self.toggle_fullscreen)

        # self.play_button.pack(side=tk.LEFT, padx=5)
        # self.rewind_start_button.pack(side=tk.LEFT, padx=5)
        # self.rewind_end_button.pack(side=tk.LEFT, padx=5)
        # self.close_button.pack(side=tk.RIGHT, padx=5)
        # self.skip_back_button.pack(side=tk.LEFT, padx=5)
        # self.skip_next_button.pack(side=tk.LEFT, padx=5)
        # self.fullscreen_button.pack(side=tk.LEFT, padx=5)

        # self.configure_buttons_with_icons()

        # self.play_button.config(image=self.play_button_icon if self.play_button_icon else None, text="" if self.play_button_icon else "Play")

    def toggle_hud(self):
        # Změníme nastavení přímo v hlavní aplikaci
        current = getattr(self.controller, "video_show_hud", True)
        self.controller.video_show_hud = not current

        logging.info(f"[HUD] Přepnuto na: {self.controller.video_show_hud}")

        # Reagujeme na změnu
        if self.controller.video_show_hud:
            self.show_hud()
        else:
            if self.player:
                self.player.video_set_marquee_int(0, 0) # Skrýt

    def _init_cinematic_theme(self):
        """Design tokens for the Cinematic Observer player skin."""
        available_fonts = {name.lower() for name in tkfont.families()}
        preferred_family = "Manrope" if "manrope" in available_fonts else "Segoe UI"

        # Base colors
        self.surface_dim = "#131313"
        self.surface_low = "#1A1C1E"
        self.surface_high = "#2A2D30"
        self.button_hover = "#2A2D30"
        self.on_surface = "#B0B3B8"

        # Accent colors
        self.primary = "#65E5F9"
        self.primary_container = "#41BCCA"
        self.volume_primary = "#3A8FA5"  # timeline cyan, ztlumená / tmavší pro volume fill
        self.cyan = "#87D7E8"
        self.orange = "#FF8C00"
        self.orange_hover = "#FF9A26"

        # Subtle line fallback
        self.outline_soft = "#35393F"
        self.surface_outline = "#272A2D"
        self.dwm_border = "#1A1C1E"

        # Typography
        self.telemetry_font = ctk.CTkFont(family=preferred_family, size=11, weight="normal")
        
    def _apply_acrylic_if_available(self):
        """Schedule acrylic after DWM chrome — immediate acrylic can fight apply_style('dark')."""
        if self.embed or sys.platform != "win32":
            return
        flag = os.environ.get("VIBE_PLAYER_ACRYLIC", "").strip().lower()
        if flag not in ("1", "true", "yes", "on"):
            logging.info(
                "[UI] Acrylic off by default (set env VIBE_PLAYER_ACRYLIC=1 to enable). "
                "Acrylic + embedded VLC HWND can crash with access violation on some setups."
            )
            return
        try:
            self.video_window.after(150, self._apply_acrylic_deferred)
        except Exception as exc:
            logging.info("[UI] Could not schedule acrylic: %s", exc)

    def _apply_acrylic_deferred(self):
        if self.embed or not self.video_window.winfo_exists():
            return
        try:
            import pywinstyles
            self.video_window.update_idletasks()
            self.video_window.update()
            pywinstyles.apply_style(self.video_window, "acrylic")
            logging.info("[UI] Acrylic style applied (deferred).")
        except Exception as exc:
            logging.info("[UI] Acrylic deferred apply failed: %s", exc)

    def _get_player_native_hwnd(self) -> int | None:
        """
        HWND for the real Win32 toplevel. pywinstyles uses GetParent(winfo_id()) only;
        CTk sometimes needs GetAncestor(..., GA_ROOT) like the main app titlebar helper.
        """
        if self.embed or sys.platform != "win32":
            return None
        try:
            self.video_window.update_idletasks()
            self.video_window.update()
            cid = int(self.video_window.winfo_id())
            user32 = ctypes.windll.user32
            GA_ROOT = 2
            parent = int(user32.GetParent(cid) or 0)
            root = int(user32.GetAncestor(cid, GA_ROOT) or 0)
            hwnd = root or parent or cid
            logging.info(
                "[UI] Player Win32 HWND: child=0x%x parent=0x%x root=0x%x → using 0x%x",
                cid,
                parent,
                root,
                hwnd,
            )
            return hwnd if hwnd else None
        except Exception as exc:
            logging.info("[UI] Failed to resolve player HWND: %s", exc)
            return None

    def _dwm_force_immersive_dark(self, hwnd: int | None) -> None:
        if not hwnd or sys.platform != "win32":
            return
        try:
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            use_dark = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(use_dark),
                ctypes.sizeof(use_dark),
            )
            logging.info("[UI] DwmSetWindowAttribute(USE_IMMERSIVE_DARK_MODE) on 0x%x OK.", hwnd)
        except Exception as exc:
            logging.info("[UI] DwmSetWindowAttribute dark mode failed: %s", exc)

    def _apply_win32_window_chrome_deferred(self, _evt=None):
        """
        Run after the CTk toplevel exists on screen so pywinstyles.detect() sees a valid HWND.
        apply_style('dark') alone often leaves a light caption on some builds; pair with
        change_header_color + explicit DWM immersive dark on the root HWND.
        """
        if self.embed or sys.platform != "win32":
            return
        if not self.video_window.winfo_exists():
            return

        hwnd = self._get_player_native_hwnd()
        self._dwm_force_immersive_dark(hwnd)

        try:
            import pywinstyles
        except ImportError:
            logging.info("[UI] pywinstyles not installed — only ctypes DWM dark was applied.")
            return

        w = self.video_window
        try:
            pywinstyles.apply_style(w, "dark")
            logging.info("[UI] pywinstyles apply_style('dark') OK (deferred).")
        except Exception as exc:
            logging.info("[UI] pywinstyles apply_style('dark') failed: %s", exc)

        try:
            pywinstyles.change_header_color(w, self.surface_dim)
            logging.info("[UI] pywinstyles change_header_color(%s) OK.", self.surface_dim)
        except Exception as exc:
            logging.info("[UI] pywinstyles change_header_color failed: %s", exc)

        try:
            pywinstyles.change_border_color(w, self.dwm_border)
            logging.info("[UI] pywinstyles change_border_color(%s) OK.", self.dwm_border)
        except Exception as exc:
            logging.info("[UI] pywinstyles change_border_color failed: %s", exc)

        try:
            pywinstyles.change_title_color(w, self.on_surface)
            logging.info("[UI] pywinstyles change_title_color OK.")
        except Exception as exc:
            logging.info("[UI] pywinstyles change_title_color failed: %s", exc)

        if os.environ.get("VIBE_PLAYER_TRY_SET_OPACITY", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            try:
                pywinstyles.set_opacity(w, 1.0, color=self.surface_dim)
                logging.info("[UI] pywinstyles.set_opacity (experimental) applied.")
            except Exception as exc:
                logging.info("[UI] pywinstyles.set_opacity failed: %s", exc)

    def _configure_window_chrome(self):
        """
        Modern DWM look: always keep native frame (rounded corners, shadow, resize, close).
        On Windows: pywinstyles dark title chrome + thin border matching Cinematic Observer.
        Captionless / overrideredirect is intentionally not supported (poor UX).
        """
        if self.embed:
            return

        for key in ("VIBE_PLAYER_CAPTIONLESS", "VIBE_PLAYER_BORDERLESS"):
            if os.environ.get(key, "").strip().lower() in ("1", "true", "yes", "on"):
                logging.info(
                    "[UI] %s is set but ignored — using standard window with DWM styling.", key
                )
                break

        try:
            self.video_window.overrideredirect(False)
        except Exception as exc:
            logging.info("[UI] overrideredirect(False) failed: %s", exc)

        if sys.platform != "win32":
            logging.info("[UI] Non-Windows: standard window chrome only.")
            return

        try:
            self.video_window.after(10, self._apply_win32_window_chrome_deferred)
            self.video_window.after(300, self._apply_win32_window_chrome_deferred)
        except Exception as exc:
            logging.info("[UI] Could not schedule deferred window chrome: %s", exc)

    def _load_tinted_icon(
        self,
        path,
        size=(24, 24),
        alpha_mult: float = 1.0,
        tint_color: str | None = None,
    ):
        """Load icon and tint non-transparent pixels (default: on_surface; optional hex)."""
        base = Image.open(path).convert("RGBA").resize(size, Image.LANCZOS)
        alpha = base.getchannel("A")
        if alpha_mult != 1.0:
            alpha = alpha.point(lambda p: int(max(0, min(255, round(float(p) * alpha_mult)))))
        fill = tint_color if tint_color is not None else self.on_surface
        tint_rgb = Image.new("RGBA", base.size, fill)
        tint_rgb.putalpha(alpha)
        return ctk.CTkImage(tint_rgb, size=size)

    def _icon_hover_pair(
        self,
        path,
        size=(24, 24),
        *,
        alpha_mult: float = 1.0,
        tint_color: str | None = None,
    ):
        """Normal + hover (primary cyan) tint for toolbar icons."""
        normal = self._load_tinted_icon(
            path, size=size, alpha_mult=alpha_mult, tint_color=tint_color
        )
        hover = self._load_tinted_icon(
            path, size=size, alpha_mult=alpha_mult, tint_color=self.primary
        )
        return normal, hover

    def _bind_icon_hover(self, widget, normal_img, hover_img):
        if not normal_img or not hover_img:
            return

        def _in(_e):
            widget.configure(image=hover_img)

        def _out(_e):
            widget.configure(image=normal_img)

        widget.bind("<Enter>", _in)
        widget.bind("<Leave>", _out)

    def _play_button_hover_enter(self, _e):
        if not getattr(self, "play_button_icon", None):
            return
        if getattr(self, "playing", False):
            self.play_button.configure(
                image=getattr(self, "stop_button_icon_hover", None) or self.stop_button_icon
            )
        else:
            self.play_button.configure(
                image=getattr(self, "play_button_icon_hover", None) or self.play_button_icon
            )

    def _play_button_hover_leave(self, _e):
        if not getattr(self, "play_button_icon", None):
            return
        if getattr(self, "playing", False):
            self.play_button.configure(image=self.stop_button_icon)
        else:
            self.play_button.configure(image=self.play_button_icon)

    def _wire_toolbar_icon_hovers(self):
        """Cyan icon on hover; toolbar bg matches fg/hover so CTk shows no extra chrome."""
        self.play_button.bind("<Enter>", self._play_button_hover_enter)
        self.play_button.bind("<Leave>", self._play_button_hover_leave)

        self._bind_icon_hover(
            self.rewind_start_button,
            getattr(self, "rewind_start_button_icon", None),
            getattr(self, "rewind_start_button_icon_hover", None),
        )
        self._bind_icon_hover(
            self.rewind_end_button,
            getattr(self, "rewind_end_button_icon", None),
            getattr(self, "rewind_end_button_icon_hover", None),
        )
        self._bind_icon_hover(
            self.skip_back_button,
            getattr(self, "skip_back_button_icon", None),
            getattr(self, "skip_back_button_icon_hover", None),
        )
        self._bind_icon_hover(
            self.skip_next_button,
            getattr(self, "skip_next_button_icon", None),
            getattr(self, "skip_next_button_icon_hover", None),
        )
        self._bind_icon_hover(
            self.fullscreen_button,
            getattr(self, "fullscreen_button_icon", None),
            getattr(self, "fullscreen_button_icon_hover", None),
        )
        self._bind_icon_hover(
            self.repeat_button,
            getattr(self, "repeat_icon", None),
            getattr(self, "repeat_icon_hover", None),
        )

        si, sih = getattr(self, "settings_icon", None), getattr(
            self, "settings_icon_hover", None
        )
        if si and sih:
            self._bind_icon_hover(self.video_menu_button, si, sih)
        else:

            def _vm_in(_e):
                self.video_menu_button.configure(text_color=self.primary)

            def _vm_out(_e):
                self.video_menu_button.configure(text_color=self.on_surface)

            self.video_menu_button.bind("<Enter>", _vm_in)
            self.video_menu_button.bind("<Leave>", _vm_out)

        li, lih = getattr(self, "loop_menu_icon", None), getattr(
            self, "loop_menu_icon_hover", None
        )
        if li and lih:
            self._bind_icon_hover(self.loop_menu_button, li, lih)
        else:

            def _lm_in(_e):
                self.loop_menu_button.configure(text_color=self.primary)

            def _lm_out(_e):
                self.loop_menu_button.configure(text_color="#545959")

            self.loop_menu_button.bind("<Enter>", _lm_in)
            self.loop_menu_button.bind("<Leave>", _lm_out)

    def _normalized_media_path(self, path):
        if not path:
            return None
        try:
            return os.path.normcase(os.path.abspath(path))
        except OSError:
            return os.path.normcase(path)

    def _mark_media_loaded(self):
        self._loaded_video_path = self._normalized_media_path(self.video_path)

    def _safe_set_hwnd(self, widget):
        if not self.player or not widget or not widget.winfo_exists():
            return
        try:
            self.player.set_hwnd(widget.winfo_id())
        except Exception as exc:
            logging.info("[VLC] set_hwnd failed: %s", exc)
           
           
    def show_video_menu(self):
        # menu = tk.Menu(self.controls_frame, tearoff=0)
        # menu = create_menu(self, self.controls_frame)
        menu = create_menu(self.controller, self.controls_frame)

        menu.add_command(label="Toggle Subtitles", command=self.toggle_subtitles)
        menu.add_command(label="Create Thumbnail", command=self.generate_thumbnail)
        
        menu.add_command(label="Add Bookmark", command=self.add_bookmark)
        menu.add_command(label="Previous Bookmark", command=self.skip_to_previous_bookmark)
        menu.add_command(label="Next Bookmark", command=self.skip_to_next_bookmark)

        if getattr(self, "use_gpu_upscale", False):
            menu.add_separator()
            menu.add_command(label="GPU Upscale Diagnostics...", command=self.show_gpu_upscale_diagnostics)

        menu.add_command(
            label="Save Frame as Image",
            command=lambda: save_capture_image(self.controller, self.video_path, self.player, method="ffmpeg")
        )
        menu.add_command(label="Show Playlist", command=self.controller.Open_playlist)
        menu.add_command(label="Add to Existing Playlist", command=self.controller.add_selected_to_playlist)
        menu.add_command(label="Add to New Playlist", command=lambda: self.controller.add_selected_to_playlist(new_playlist=True))

        menu.add_separator()

        speed_menu = tk.Menu(menu, tearoff=0)
        speeds = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2]
        for spd in speeds:
            label = f"{'▶  ' if spd == self.current_speed else '      '}{spd:.1f}x"
            speed_menu.add_command(
                label=label,
                command=lambda s=spd: self.set_playback_speed(s)
            )
        menu.add_cascade(label=f"Speed  [{self.current_speed:.1f}x]", menu=speed_menu)


        # Budoucí rozšíření
        # menu.add_command(label="📁 Add to Playlist", command=...)
        # menu.add_command(label="🔎 Auto Tag Files", command=...)

        # Zobraz menu pod tlačítkem
        x = self.video_menu_button.winfo_rootx()
        y = self.video_menu_button.winfo_rooty() + self.video_menu_button.winfo_height()
        menu.tk_popup(x, y)




    def show_loop_menu(self):
        """
        Creates and displays a dropdown menu for loop controls.
        The menu is positioned directly under the button that called it.
        """
        # Create the menu using the helper function, parented to the controller
        menu = create_menu(self.controller, self.controls_frame)

        # Add commands that call existing methods within this VideoPlayer instance
        menu.add_command(label="Set Loop Start", command=self.set_loop_start)
        menu.add_command(label="Set Loop End", command=self.set_loop_end)
        menu.add_separator()
        menu.add_command(label="Toggle Loop ON/OFF", command=self.toggle_loop)

        # Get the screen coordinates of the loop menu button
        x = self.loop_menu_button.winfo_rootx()
        y = self.loop_menu_button.winfo_rooty() + self.loop_menu_button.winfo_height()

        # Display the menu at the calculated position
        menu.tk_popup(x, y)


    def configure_buttons_with_icons(self):
        if self.rewind_start_button_icon:
            self.rewind_start_button.config(image=self.rewind_start_button_icon, text="")
            logging.info("Rewind start button configured with icon.")  # Debug
        if self.rewind_end_button_icon:
            self.rewind_end_button.config(image=self.rewind_end_button_icon, text="")
            logging.info("Rewind end button configured with icon.")  # Debug

    def on_resize(self, event):
        if event.widget == self.video_window:
            self.resizing = True
            self.resizing = False

    def toggle_play(self):
        logging.info(f"[DEBUG] toggle_play: playing={self.playing}, hovered={getattr(self, '_hovered', False)}")
        if not hasattr(self, 'player') or self.player is None:
            logging.info("[DEBUG] No player instance!")
            return
        
        if not self.playing:
            self.play_video()
        else:
            self.pause_video()

    def set_playback_speed(self, speed: float):
        """Set playback speed while preserving audio pitch via --audio-time-stretch."""
        self.current_speed = speed
        if self.player:
            self.player.set_rate(speed)
        self.show_speed_hud(speed)
        logging.info(f"[Speed] Playback speed set to {speed}x")

    def show_speed_hud(self, speed: float):
        """Zobrazí aktuální rychlost přehrávání jako OSD overlay pomocí VLC marquee."""
        if not self.player:
            return
        try:
            _Enable   = 0
            _Text     = 1
            _Color    = 2
            _Opacity  = 3
            _Position = 4
            _Size     = 30
            _Timeout  = 7

            label = "▶▶" if speed > 1.0 else ("▶" if speed == 1.0 else "◀◀")
            text = f"{label}  {speed:.1f}x".lower()

            self.player.video_set_marquee_int(_Enable, 1)
            self.player.video_set_marquee_string(_Text, text)
            self.player.video_set_marquee_int(_Color, 0xFFFF00)   # Žlutá – odlišná od bílého titulku
            self.player.video_set_marquee_int(_Opacity, 220)
            self.player.video_set_marquee_int(_Size, 28)
            self.player.video_set_marquee_int(_Timeout, 1500)     # Zmizí za 1.5 s
            self.player.video_set_marquee_int(_Position, 8)       # Střed dole
        except Exception as e:
            logging.info(f"[SpeedHUD] Chyba při zobrazování OSD: {e}")

    def speed_step(self, direction: int):
        """Change speed by one step. direction: +1 = faster, -1 = slower."""
        steps = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        try:
            idx = steps.index(self.current_speed)
        except ValueError:
            idx = steps.index(1.0)
        new_idx = max(0, min(len(steps) - 1, idx + direction))
        self.set_playback_speed(steps[new_idx])
        logging.info(f"[Speed] Step {'+' if direction > 0 else ''}{direction} → {steps[new_idx]}x")

    def toggle_fullscreen(self, event=None):
        self.is_fullscreen = not self.is_fullscreen
        if self.is_fullscreen:
            self.previous_geometry = self.video_window.geometry()
            
            # Maximize the window before going fullscreen
            self.video_window.state("zoomed")
            self.video_window.attributes("-fullscreen", True)
            self.video_window.attributes('-topmost', False)
            self.video_label.place(relx=0, rely=0, relwidth=1, relheight=1)
            if getattr(self, "_broken_playback_overlay_active", False):
                self._broken_playback_overlay.lift()

            # self.video_window.bind("<Escape>", self.exit_fullscreen)
            
            # Hide controls during fullscreen
            self.hide_controls_frame()
            logging.info("Entered fullscreen mode.")
                        # Start the global mouse listener
            if not self.listener:
                self.listener = mouse.Listener(on_move=self.check_mouse_position)
                self.listener.start()
            
            # Ensure the video resizes to fullscreen
            self.resize_video_to_fullscreen()

            # Reassign video output to the fullscreen window
            self._safe_set_hwnd(self.video_label)

        else:
            self.exit_fullscreen()
    
    def _handle_escape_key(self, event=None):
        if self.is_fullscreen:
            self.exit_fullscreen()
            return "break"
        return None


    def is_cursor_over_toolbar(self, x, y):
        if hasattr(self, "controls_frame"):
            try:
                abs_x = self.controls_frame.winfo_rootx()
                abs_y = self.controls_frame.winfo_rooty()
                w = self.controls_frame.winfo_width()
                h = self.controls_frame.winfo_height()
                return abs_x <= x <= abs_x + w and abs_y <= y <= abs_y + h
            except Exception as e:
                logging.info(f"[DEBUG] Error in controls_frame hitbox: {e}")
        return False
        
            
    def is_cursor_over_video(self, x, y):
        # Pro floating player:
        if hasattr(self, "video_label"):
            try:
                abs_x = self.video_label.winfo_rootx()
                abs_y = self.video_label.winfo_rooty()
                w = self.video_label.winfo_width()
                h = self.video_label.winfo_height()
                if abs_x <= x <= abs_x + w and abs_y <= y <= abs_y + h:
                    return True
            except Exception:
                pass
        # Pro embedded preview:
        if hasattr(self, "video_window"):
            try:
                abs_x = self.video_window.winfo_rootx()
                abs_y = self.video_window.winfo_rooty()
                w = self.video_window.winfo_width()
                h = self.video_window.winfo_height()
                if abs_x <= x <= abs_x + w and abs_y <= y <= abs_y + h:
                    return True
            except Exception:
                pass
        return False
                
            



    def _global_click_toggle_if_over_video(self, x: int, y: int) -> None:
        """Hit-test + play toggle; must run on the Tk main thread only."""
        try:
            if not self.video_window.winfo_exists():
                return
        except Exception:
            return
        if self.is_cursor_over_video(x, y) and not self.is_cursor_over_toolbar(x, y):
            logging.info(f"[DEBUG] Global mouse click on video at ({x},{y})")
            self.toggle_play()

    def _pynput_queue_pump(self) -> None:
        """Drain pynput → main thread queue (Tk thread only)."""
        self._pynput_pump_job = None
        if getattr(self, "_pynput_bridge_dead", True):
            return
        try:
            if not self.video_window.winfo_exists():
                return
        except Exception:
            return
        try:
            for _ in range(48):
                try:
                    kind, x, y = self._pynput_queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "click":
                    self._global_click_toggle_if_over_video(x, y)
                elif kind == "move":
                    self._check_mouse_position_main(x, y)
        except Exception as e:
            logging.debug("[pynput bridge] drain: %s", e)
        if getattr(self, "_pynput_bridge_dead", True):
            return
        try:
            if self.video_window.winfo_exists():
                self._pynput_pump_job = self.video_window.after(20, self._pynput_queue_pump)
        except Exception:
            self._pynput_pump_job = None

    def _on_global_click(self, x, y, button, pressed):
        from pynput.mouse import Button
        # Worker thread: queue only — no Tk / no .after().
        if not (pressed and button == Button.left):
            return
        if getattr(self, "_pynput_bridge_dead", True):
            return
        try:
            self._pynput_queue.put_nowait(("click", int(x), int(y)))
        except queue.Full:
            pass




    def resize_video_to_fullscreen(self):
        # Get correct screen dimensions using screeninfo
        screen_width, screen_height = 0, 0
        for monitor in get_monitors():
            screen_width = monitor.width
            screen_height = monitor.height
            break  # Assuming you want the primary monitor

        # Debug prints
        logging.info(f"Fullscreen window geometry: {screen_width}x{screen_height}")
        logging.info(f"Video label size: {self.video_label.winfo_width()}x{self.video_label.winfo_height()}")
        logging.info(f"Screen width: {screen_width}, Screen height: {screen_height}")
        
        # Set the video output to the resized label
        self._safe_set_hwnd(self.video_label)

                    
            
    # def resize_video_to_fullscreen(self):
        
                # Debug prints
        # logging.info(f"Fullscreen window geometry: {self.video_window.geometry()}")
        # logging.info(f"Video label size: {self.video_label.winfo_width()}x{self.video_label.winfo_height()}")
        # logging.info(f"Screen width: {self.video_window.winfo_screenwidth()}, Screen height: {self.video_window.winfo_screenheight()}")
        # Assuming self.video_label is where the video is being displayed
        # self.video_label.configure(width=self.video_window.winfo_screenwidth(), height=self.video_window.winfo_screenheight())
        # self.video_label.pack(fill=tk.BOTH, expand=True)

    def exit_fullscreen(self, event=None):
        
                # Stop the mouse listener when exiting fullscreen
        if self.listener:
            self.listener.stop()
            self.listener = None

        self.is_fullscreen = False
        self.video_window.attributes("-fullscreen", False)
        self.video_window.state("normal")  # Reset the window state to normal (unmaximized)
        self.video_window.geometry(self.previous_geometry)
        # Show controls again
        self.show_controls_frame()
        self.video_label.place(relx=0, rely=0, relwidth=1, relheight=1)
        if getattr(self, "_broken_playback_overlay_active", False):
            self._broken_playback_overlay.lift()
        logging.info("Exited fullscreen mode.")
        
        # Reassign video output back to the original window
        self._safe_set_hwnd(self.video_label)



    # def toggle_fullscreen(self, event=None):
        # self.is_fullscreen = not self.is_fullscreen
        # if self.is_fullscreen:
            # self.previous_geometry = self.video_window.geometry()
            # self.video_window.attributes("-fullscreen", True)
            # self.video_label.pack(fill=tk.BOTH, expand=True)
            # self.video_window.bind("<Escape>", self.exit_fullscreen)
            # hidding toolbar
            # self.hide_controls_frame()
            # logging.info("Entered fullscreen mode.")  # Debug

            # Start the global mouse listener
            # if not self.listener:
                # self.listener = mouse.Listener(on_move=self.check_mouse_position)
                # self.listener.start()
        # else:
            # self.exit_fullscreen()

    # def exit_fullscreen(self, event=None):
        # self.is_fullscreen = False
        # self.video_window.attributes("-fullscreen", False)
        # self.video_window.geometry(self.previous_geometry)
        # showing toolbar
        # self.show_controls_frame()
        # self.video_label.pack(expand=True)
        # self.video_window.unbind("<Escape>")
        # logging.info("Exited fullscreen mode.")  # Debug

        # Stop the listener if it is running
        # if self.listener:
            # self.listener.stop()
            # self.listener = None

    def _check_mouse_position_main(self, x: int, y: int) -> None:
        """Fullscreen autohide toolbar; Tk-only — must run on main thread."""
        try:
            screen_height = self.video_window.winfo_screenheight()
        except Exception:
            return
        if y >= screen_height - 50:
            self.show_controls_frame()
        else:
            self.hide_controls_frame()

    def check_mouse_position(self, x, y):
        # Worker thread: throttle + queue only — no Tk.
        if getattr(self, "_pynput_bridge_dead", True):
            return
        t = time.monotonic()
        if t - getattr(self, "_fullscreen_mousemove_throttle_ts", 0.0) < 0.04:
            return
        self._fullscreen_mousemove_throttle_ts = t
        try:
            self._pynput_queue.put_nowait(("move", int(x), int(y)))
        except queue.Full:
            pass

    # def check_mouse_position(self, event):
        # screen_width, screen_height = pyautogui.size()  # Get the screen size
        # mouse_x, mouse_y = pyautogui.position()  # Get the mouse position in absolute screen coordinates

        # logging.info(f"Screen size: width={screen_width}, height={screen_height}")  # Debug
        # logging.info(f"Mouse absolute position: x={mouse_x}, y={mouse_y}")  # Debug

        # if mouse_y >= screen_height - 50:  # Adjust the threshold as needed
            # self.show_controls_frame()
        # else:
            # self.hide_controls_frame()

    def show_controls_frame(self):
        if not self.controls_frame_visible:
            self.controls_frame.pack(side=tk.BOTTOM, fill=tk.X)
            self.controls_frame_visible = True
            logging.info("Showing controls frame.")  # Debug

    def hide_controls_frame(self):
        if self.embed:
            return  # Nikdy neschovávej controls_frame v preview!
        
        if self.controls_frame_visible:
            self.controls_frame.pack_forget()
            self.controls_frame_visible = False
            logging.info("Hiding controls frame.")  # Debug




    # def detect_encoding(self, file_path):
        # with open(file_path, 'rb') as f:
            # raw = f.read(4096)
        # result = chardet.detect(raw)
        # encoding = result['encoding']
        # logging.info(f"Detected encoding: {encoding}")
        # return encoding

    #maybe not needed anymore
    def convert_subtitle_to_utf8_no_bom(self, original_path, source_encoding="windows-1250"):
        print ("will convert utf-8")
        with open(original_path, "r", encoding=source_encoding, errors="replace") as src:
            content = src.read()
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".srt", mode="w", encoding="utf-8")
        temp.write(content)
        temp.close()
        logging.info(f"Subtitle converted to UTF-8 (no BOM): {temp.name}")
        return temp.name

 #maybe not needed anymore
    def convert_subtitle_preserved(self, original_path):
        with open(original_path, "r", encoding="windows-1250", errors="replace") as src:
            content = src.read()
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".srt", mode="w", encoding="windows-1250")
        temp.write(content)
        temp.close()
        logging.info(f"Subtitle copied in native encoding (windows-1250): {temp.name}")
        return temp.name
 #maybe not needed anymore
    def refresh_subtitles(self, restart_media=False):
        if not self.player:
            logging.info("Player not initialized.")
            return

        if self.subtitles_enabled:
            subtitle_path = os.path.splitext(self.video_path)[0] + ".srt"
            print (f"sub path: {subtitle_path}")
            if os.path.exists(subtitle_path):
                # Convert to UTF-8 without BOM
                converted_path = self.convert_subtitle_to_utf8_no_bom(subtitle_path, source_encoding="windows-1250")
                print (f"converted_path: {converted_path}")
                # Track for later cleanup
                if not hasattr(self, "temp_subtitles"):
                    self.temp_subtitles = []
                self.temp_subtitles.append(converted_path)

                if restart_media:
                    was_playing = self.playing
                    self.pause_video() if was_playing else setattr(self, "last_position", self.player.get_time())

                    media = self.instance.media_new(self.video_path)
                    media.add_option(f"sub-file={converted_path}")

                    self.player.set_media(media)
                    self._mark_media_loaded()
                    self._safe_set_hwnd(self.video_label)
                    self.play_video()
                else:
                    logging.info("Subtitles converted and will apply on next play.")
            else:
                logging.info("Subtitle file not found.")
        else:
            logging.info("Disabling subtitles")
            self.player.video_set_spu(-1)


    # only for timelinebar_plugin!
    def seek_to_time(self, timestamp):
        if self.player:
            self.player.set_time(int(timestamp * 1000))  # ms
            self.last_position = int(timestamp * 1000)
            logging.info(f"[VideoPlayer] Seek to {timestamp:.2f}s")


    def preview_or_play(self):
        if not self.video_path or not os.path.exists(self.video_path):
            logging.info("[DEBUG] preview_or_play skipped: no valid video_path")
            return
        
        if self.auto_play:
            self.play_video()
        else:
            self.display_first_frame()

    # Uvnitř třídy VideoPlayer v souboru video_operations.py


    # SOUBOR: video_operations.py

    def play_video(self):
        logging.info(f"[DEBUG] play_video: playing={self.playing}")
        if not self.video_path:
            return

        # 1. Lazy VLC init (same path as display_first_frame).
        if not self._ensure_vlc_player():
            return

        norm = self._normalized_media_path(self.video_path)

        # Resume from pause: do NOT call set_media again. Re-loading media on every
        # unpause breaks duration parsing on some formats (e.g. WMV) and can trigger
        # native VLC crashes (Windows access violation).
        if self.player and self._loaded_video_path == norm:
            try:
                st = self.player.get_state()
            except Exception:
                st = None
            if st == vlc.State.Paused:
                video_widget = getattr(self, "video_label", self.video_window)
                if not video_widget.winfo_exists():
                    return
                self._cancel_broken_decode_checks()
                self._safe_set_hwnd(video_widget)
                self.player.play()
                self._duration_retry_count = 0
                if self.current_speed != 1.0:
                    self.video_window.after(
                        300, lambda: self.player.set_rate(self.current_speed) if self.player else None
                    )
                self.playing = True
                self.show_hud()
                if self.show_video_button_bar and hasattr(self, "play_button"):
                    self.play_button.configure(
                        image=self.stop_button_icon if self.stop_button_icon else None,
                        text="" if self.stop_button_icon else "stop",
                    )
                self.update_time_slider()
                self.update_timer()
                logging.info("[DEBUG] play_video: resumed from VLC paused state (no set_media).")
                return

        self._cancel_broken_decode_checks()

        # 2. Vytvoření média a zbytek logiky (zůstává stejné)
        media = self.instance.media_new(self.video_path)

        if self.last_position:
            start_time_seconds = self.last_position / 1000.0
            media.add_option(f"start-time={start_time_seconds}")

        if self.current_speed != 1.0:
            media.add_option(":audio-time-stretch")

        video_widget = getattr(self, "video_label", self.video_window)
        if not video_widget.winfo_exists():
            return

        self.player.set_media(media)
        self._mark_media_loaded()
        self._safe_set_hwnd(video_widget)
        self.player.play()
        self._duration_retry_count = 0

        if self.current_speed != 1.0:
            self.video_window.after(300, lambda: self.player.set_rate(self.current_speed) if self.player else None)

        self.playing = True
        self.show_hud()
        
        if self.show_video_button_bar and hasattr(self, "play_button"):
            self.play_button.configure(
                image=self.stop_button_icon if self.stop_button_icon else None,
                text="" if self.stop_button_icon else "stop"
            )
        self.update_time_slider()
        self.update_timer()

        self._schedule_decode_placeholder_checks()

        if self.subtitles_enabled:
            threading.Thread(target=self._load_and_apply_subtitles, daemon=True).start()





    def _load_and_apply_subtitles(self):
        """
        Vyhledá, zkonvertuje a aplikuje titulky na pozadí,
        aniž by blokovala přehrávání.
        """
        subtitle_path = os.path.splitext(self.video_path)[0] + ".srt"
        if not os.path.exists(subtitle_path):
            logging.info("[Subtitles] Soubor s titulky nenalezen.")
            return

        try:
            # Pomalá operace: konverze souboru
            converted_sub_path = self.convert_subtitle_to_utf8_no_bom(subtitle_path)
            logging.info(f"[Subtitles] Titulky připraveny: {converted_sub_path}")

            # Aplikace titulků na již běžící video
            # Musí být provedeno v hlavním vlákně přes `after`
            self.video_window.after(0, lambda: self.player.set_subtitle_file(converted_sub_path))
            
        except Exception as e:
            logging.info(f"[ERROR] Nepodařilo se načíst nebo aplikovat titulky: {e}")





    def stop_video(self):
        logging.info("[DEBUG][stop_video] called. Current playing: %s", self.playing)
        if hasattr(self, "player") and self.player is not None:
            try:
                logging.info(f"[DEBUG][stop_video] Will call self.player.stop()! State: {self.player.get_state()}")
                self.player.stop()
                self.playing = False
                logging.info("[DEBUG][stop_video] self.player.stop() DONE, playing set to False.")
            except Exception as e:
                logging.info("[DEBUG][stop_video] Exception: %s", e)
        else:
            logging.info("[DEBUG][stop_video] No player instance.")

  



    def pause_video(self):
        logging.info(f"[DEBUG] Click: pause video.  self.playing :{self.playing} ")
        logging.info(f"[DEBUG] self.player: {self.player}")
        logging.info(f"[DEBUG] self.player.get_state(): {self.player.get_state() if self.player else 'no player'}")
        if self.playing:
            self.last_position = self.player.get_time()  # Save the last played position
            logging.info(f"Saving last position: {self.last_position} ms")  # Debugging
            self.player.pause()
            self.show_hud()
            
            logging.info("[DEBUG] Called self.player.pause()!")
            self.playing = False
            if hasattr(self, "play_button"):
                self.play_button.configure(image=self.play_button_icon if self.play_button_icon else None, text="" if self.play_button_icon else "play")
            logging.info("Pausing video.")  # Debug
        else:
            logging.info("[DEBUG] pause_video called, but self.playing is False.")


        
    
    # def pause_video(self):
        # if self.playing:
            # self.last_position = self.player.get_time()  # Save the last played position
            # logging.info(f"Saving last position: {self.last_position} ms")  # Debugging
            # self.player.pause()
            # self.playing = False
            # self.play_button.config(image=self.play_button_icon if self.play_button_icon else tk.PhotoImage(), text="" if self.play_button_icon else "Play")
            # logging.info("Pausing video.")  # Debug


    # def stop_video(self):
        # if self.playing:
            # self.last_position = self.player.get_time()  # Save the last played position
            # self.player.stop()
            # self.playing = False



    # def stop_video(self):
        # self.playing = False
        # self.player.stop()
        # logging.info("Stopped video.")  # Debug

    def rewind_to_start(self):
        self.player.set_time(0)
        logging.info("Rewind to start.")  # Debug

    def rewind_to_end(self):
        length = self.player.get_length()
        self.player.set_time(length - 1000)
        logging.info("Rewind to end.")  # Debug




    def safe_switch_video(self, path, name):
            """
            Přepne na nové video BEZ nutnosti ničit a znovu vytvářet přehrávač.
            Toto je stabilní a doporučovaná metoda.
            Zároveň aktualizuje zvýraznění v playlistu.
            """
            logging.info(f"[DEBUG] Switching media to: {name}")

            if not self.player or not self.instance:
                logging.info("[ERROR] Player or instance not initialized. Cannot switch video.")
                return

            # 1. Aktualizujeme interní cesty a jméno
            self.video_path = path
            self.video_name = name
            self.last_position = 0
            self.video_window.title((name or "").lower())

            # 2. Vytvoříme NOVÉ MÉDIUM, ale POUŽIJEME STÁVAJÍCÍ PŘEHRÁVAČ.
            new_media = self.instance.media_new(self.video_path)
            self.player.set_media(new_media)
            self._mark_media_loaded()
            if hasattr(self, "video_label") and self.video_label.winfo_exists():
                self._safe_set_hwnd(self.video_label)

            # 3. Spustíme přehrávání nového média
            self.player.play()
            self.playing = True
            # Displays HUD
            self.show_hud()

            # 4. Resetujeme UI (časovač a slider)
            if not self.embed and self.show_video_button_bar:
                self.slider.set(0)
                self.timer_label.configure(text="00:00 / 00:00".lower())
                self.play_button.configure(
                    image=self.stop_button_icon if self.stop_button_icon else None,
                    text="" if self.stop_button_icon else "stop"
                )
            
            # 5. Resetujeme loop stav přehrávače - selekce patřila ke starému videu
            self.loop_active = False
            self.loop_start = None
            self.loop_end = None

            # 6. Aktualizujeme timeline pro nové video
            if self.timeline_widget:
                self.timeline_widget.clear_selection()
                self.timeline_widget.reload_all_markers_and_redraw(path)

            # 6. --- NOVÉ: SYNCHRONIZACE PLAYLISTU ---
            # Pokud je playlist aktivní, najdeme index aktuálního videa a zvýrazníme ho
            if self.playlist_manager and self.playlist_manager.is_playlist_open and self.playlist_manager.playlist:
                try:
                    # Najdeme index aktuálního videa v playlistu
                    current_idx = self.playlist_manager.playlist.index(path)
                    self.playlist_manager.current_playing_index = current_idx
                    
                    # Zavoláme metodu pro grafickou aktualizaci (tu musíš mít v playlist.py)
                    if hasattr(self.playlist_manager, "update_ui_selection"):
                        self.playlist_manager.update_ui_selection(current_idx)
                        
                except ValueError:
                    logging.info(f"[Playlist Sync] Video {name} není v aktuálním playlistu.")
                    
                
                
    def safe_switch_videoOld(self, path, name):
        """
        Přepne na nové video BEZ nutnosti ničit a znovu vytvářet přehrávač.
        Toto je stabilní a doporučovaná metoda.
        """
        logging.info(f"[DEBUG] Switching media to: {name}")

        if not self.player or not self.instance:
            logging.info("[ERROR] Player or instance not initialized. Cannot switch video.")
            return

        # 1. Aktualizujeme interní cesty a jméno
        self.video_path = path
        self.video_name = name
        self.last_position = 0
        self.video_window.title((name or "").lower())

        # 2. Vytvoříme NOVÉ MÉDIUM, ale POUŽIJEME STÁVAJÍCÍ PŘEHRÁVAČ.
        new_media = self.instance.media_new(self.video_path)
        self.player.set_media(new_media)

        # 3. Spustíme přehrávání nového média
        self.player.play()
        self.playing = True

        # 4. Resetujeme UI (časovač a slider)
        if not self.embed and self.show_video_button_bar:
            self.slider.set(0)
            self.timer_label.configure(text="00:00 / 00:00".lower())
            self.play_button.configure(
                image=self.stop_button_icon if self.stop_button_icon else None,
                text="" if self.stop_button_icon else "stop"
            )
        
        # 5. Aktualizujeme timeline pro nové video
        if self.timeline_widget:
            self.timeline_widget.reload_all_markers_and_redraw(path)






    def skip_next(self, event=None):
        if self.playlist_manager and self.playlist_manager.is_playlist_open and self.playlist_manager.playlist:
            self.playlist_manager.current_playing_index = (self.playlist_manager.current_playing_index + 1) % len(self.playlist_manager.playlist)
            path = self.playlist_manager.playlist[self.playlist_manager.current_playing_index]
            name = os.path.basename(path)
        else:
            current_index = self.controller.current_video_index
            video_files = self.controller.video_files
            if not video_files:
                return
            next_index = (current_index + 1) % len(video_files)
            self.controller.current_video_index = next_index
            path = video_files[next_index]['path']
            name = video_files[next_index]['name']

        self.safe_switch_video(path, name)


    


    def skip_back(self, event=None):
        if self.playlist_manager and self.playlist_manager.is_playlist_open and self.playlist_manager.playlist:
            self.playlist_manager.current_playing_index = (self.playlist_manager.current_playing_index - 1) % len(self.playlist_manager.playlist)
            path = self.playlist_manager.playlist[self.playlist_manager.current_playing_index]
            name = os.path.basename(path)
        else:
            current_index = self.controller.current_video_index
            video_files = self.controller.video_files
            if not video_files:
                return
            previous_index = (current_index - 1) % len(video_files)
            self.controller.current_video_index = previous_index
            path = video_files[previous_index]['path']
            name = video_files[previous_index]['name']

        self.safe_switch_video(path, name)




    def skip_next_in_playlist(self):
        if self.playlist_manager and self.playlist_manager.playlist:
            self.playlist_manager.current_playing_index = (self.playlist_manager.current_playing_index + 1) % len(self.playlist_manager.playlist)
            next_video = self.playlist_manager.playlist[self.playlist_manager.current_playing_index]
            self.safe_switch_video(next_video, os.path.basename(next_video))
            self.playlist_manager.update_playlist_selection()

    def skip_previous_in_playlist(self):
        if self.playlist_manager and self.playlist_manager.playlist:
            self.playlist_manager.current_playing_index = (self.playlist_manager.current_playing_index - 1) % len(self.playlist_manager.playlist)
            previous_video = self.playlist_manager.playlist[self.playlist_manager.current_playing_index]
            self.safe_switch_video(previous_video, os.path.basename(previous_video))
            self.playlist_manager.update_playlist_selection()


    # def skip_next(self):
        # current_index = self.controller.current_video_index
        # video_files = self.controller.video_files
        # if video_files:
            # next_index = (current_index + 1) % len(video_files)
            # self.controller.current_video_index = next_index
            # next_video = video_files[next_index]
            # self.controller.open_video_player(next_video['path'], next_video['name'])
            # logging.info(f"Skipped to next video: {next_video['path']}")  # Debug

    # def skip_back(self):
        # current_index = self.controller.current_video_index
        # video_files = self.controller.video_files
        # if video_files:
            # previous_index = (current_index - 1) % len(video_files)
            # self.controller.current_video_index = previous_index
            # previous_video = video_files[previous_index]
            # self.controller.open_video_player(previous_video['path'], previous_video['name'])
            # logging.info(f"Skipped back to previous video: {previous_video['path']}")  # Debug


    # Uvnitř třídy VideoPlayer

    def release_held_media(self):
        """
        Stop playback and release the VLC Media object so Windows can close the file.
        python-vlc often keeps the file locked until Media.release(), not only player.release().
        """
        if not getattr(self, "player", None):
            return
        try:
            if os.name == "nt" and hasattr(self.player, "set_hwnd"):
                self.player.set_hwnd(0)
        except Exception:
            pass
        self._loaded_video_path = None
        try:
            self.player.stop()
        except Exception:
            pass
        try:
            t0 = time.time()
            while self.player.is_playing() and (time.time() - t0) < 0.6:
                time.sleep(0.04)
        except Exception:
            pass
        try:
            m = self.player.get_media()
            if m is not None:
                self.player.set_media(None)
                m.release()
        except Exception as e:
            logging.debug("[Cleanup] release_held_media: %s", e)

    def cleanup(self):
        """
        Zastaví všechny běžící procesy a uvolní všechny systémové zdroje.
        Tato metoda je navržena tak, aby byla odolná proti chybám.
        """
        logging.info("[Cleanup] Spouštím úklid pro video přehrávač...")
        self._pynput_bridge_dead = True
        pj = getattr(self, "_pynput_pump_job", None)
        if pj is not None:
            try:
                if hasattr(self, "video_window") and self.video_window.winfo_exists():
                    self.video_window.after_cancel(pj)
            except Exception:
                pass
            self._pynput_pump_job = None
        try:
            self._cancel_broken_decode_checks()
            self._hide_broken_playback_overlay()
        except Exception:
            pass
        self.playing = False  # Zastaví všechny smyčky `update_time_slider`

        if self.global_listener:
            self.global_listener.stop()
            self.global_listener = None
            logging.info("[Cleanup] Globální listener myši zastaven.")

        if hasattr(self, 'player') and self.player:
            try:
                self.release_held_media()
                self.player.release()
                logging.info("[Cleanup] VLC přehrávač uvolněn.")
            except Exception as e:
                logging.info(f"[Cleanup] Chyba při uvolňování přehrávače: {e}")
            finally:
                self.player = None
                time.sleep(0.2)

        if hasattr(self, 'instance') and self.instance:
            try:
                self.instance.release()
                logging.info("[Cleanup] VLC instance uvolněna.")
            except Exception as e:
                logging.info(f"[Cleanup] Chyba při uvolňování VLC instance: {e}")
            finally:
                self.instance = None

        if hasattr(self, 'video_window') and self.video_window.winfo_exists():
            self.video_window.destroy()
            logging.info("[Cleanup] Okno přehrávače zničeno.")

    def close_video_player(self):
        """Nyní pouze volá robustní cleanup metodu."""
        self.cleanup()
        if self.controller:
            self.controller.current_video_window = None
            self.controller.after(150, self.controller._focus_back_after_dialog)



    def _timeline_drag(self, event):
        """Map pointer X to playhead using full hit-host width (not only the 2px track)."""
        host = getattr(self, "timeline_hit_host", None)
        if host is None:
            return
        try:
            w = host.winfo_width()
        except tk.TclError:
            return
        if w <= 1:
            return
        x = event.x_root - host.winfo_rootx()
        x = max(0, min(x, w))
        val = (x / float(w)) * 100.0
        self.slider.set(val)
        self.slider_update(event)

    def slider_update(self, event=None):
        # Spočítej nový čas podle pozice slideru
        slider_val = self.slider.get()
        duration = self.player.get_length()
        if duration <= 0:
            # logging.info("[DEBUG] slider_update: Invalid duration.")
            return

        new_time = (slider_val / 100.0) * (duration / 1000.0)  # v sekundách
        # logging.info(f"[DEBUG] slider_update: Seeking video to {new_time:.2f}s")

        if self.player:
            self.player.set_time(int(new_time * 1000))
            self.last_position = int(new_time * 1000)
            # logging.info(f"[DEBUG] slider_update: Set player time to {int(new_time * 1000)} ms")

        if hasattr(self, "timeline_widget") and self.timeline_widget is not None:
            # logging.info(f"[DEBUG] slider_update → timeline_widget.set_current_time({new_time:.2f})")
            self.timeline_widget.set_current_time(new_time)

    # def slider_update(self, event):
        # pos = self.slider.get()
        # duration = self.player.get_length()
        # new_time = int(duration * pos / 100)
        # self.player.set_time(new_time)
        # logging.info(f"Updated video position to {new_time} ms.")  # Debug
        
        
    def _volume_drag(self, event):
        """Map pointer X to volume using the full hit host width (easier than hitting the 2px bar)."""
        host = self.volume_hit_host
        try:
            w = host.winfo_width()
        except tk.TclError:
            return
        if w <= 1:
            return
        x = event.x_root - host.winfo_rootx()
        x = max(0, min(x, w))
        val = (x / float(w)) * 100.0
        self.volume_slider.set(val)
        self.update_volume(val)

    def update_volume(self, volume):
        self.current_volume = int(float(volume))
        if self.player:
            self.player.audio_set_volume(self.current_volume)
            logging.info(f"Volume set to: {self.current_volume}")  # Debug

            # Update the parent volume, but only if controller exists
            if self.controller is not None:
                self.controller.update_current_volume(self.current_volume)
            





    def get_current_time(self):
        """
        Returns the current playback time in seconds.
        """
        if hasattr(self, "player") and self.player is not None:
            return self.player.get_time() / 1000.0
        return 0.0




    def _handle_loop_if_needed(self, current_time_ms):
        """
        Hlavní logika pro smyčku. Pokud čas přehrávání překročí koncový bod,
        skočí zpět na začátek. Používá interní stav VideoPlayeru.
        """
        # 1. Pokud smyčka není aktivní nebo nejsou definovány hranice, nic nedělej
        if not self.loop_active or self.loop_start is None or self.loop_end is None:
            return

        # 2. Ujisti se, že rozsah je platný
        if self.loop_end <= self.loop_start:
            return

        # 3. Převeď aktuální čas z milisekund na sekundy pro porovnání
        now_seconds = current_time_ms / 1000.0

        # 4. KLÍČOVÉ POROVNÁNÍ: Pokud aktuální čas přesáhl konec smyčky...
        if now_seconds >= self.loop_end:
            # Přidána malá tolerance (0.5s), aby se zabránilo zacyklení, pokud by byl update pomalý
            if now_seconds < self.loop_end + 0.5:
                logging.info(f"[LOOP] Konec smyčky dosažen. Skok zpět na {self.loop_start:.2f}s.")
                # ...skoč na začátek smyčky (čas je třeba v milisekundách)
                self.player.set_time(int(self.loop_start * 1000))


    def update_time_slider(self):
        
          # POISTKA: Ak okno už neexistuje, okamžite skonči
        if not hasattr(self, 'video_window') or not self.video_window.winfo_exists():
            return
        
       # Spouštěj jen pokud hraje video A máme validní video_path
        if not self.playing or not self.video_path:
            return
        
        if self.playing:
            duration = self.player.get_length()
            current_time = self.player.get_time()

            self._handle_loop_if_needed(current_time)

            # --- Slider position ---
            if duration > 0:
                self._duration_retry_count = 0
                pos = current_time / duration * 100  # percent
                if hasattr(self, "slider") and self.slider and self.slider.winfo_exists():
                    self.slider.set(pos)
            else:
                self._duration_retry_count = getattr(self, "_duration_retry_count", 0) + 1
                if self._duration_retry_count <= 80:
                    logging.info("Warning: Video duration is zero or not available. Retrying...")
                    logging.info("[DEBUG] update_time_slider: Duration not available.")
                    self.video_window.after(500, self.update_time_slider)
                else:
                    logging.info("[DEBUG] update_time_slider: duration still 0 — stop retry spam.")
                return

            # --- SYNC TIMELINE WIDGETS ---
            if hasattr(self, "timeline_widget") and self.timeline_widget is not None:
                try:
                    self.timeline_widget.set_current_time(current_time / 1000.0)
                except Exception as e:
                    logging.info(f"[DEBUG] timeline_widget update failed: {e}")

            if hasattr(self.parent, "timeline_window") and self.parent.timeline_window is not None:
                try:
                    self.parent.timeline_window.set_current_time(current_time / 1000.0)
                except Exception as e:
                    logging.info(f"[DEBUG] timeline_window update failed: {e}")
                    self.parent.timeline_window = None

        self.video_window.after(300, self.update_time_slider)






    # Function to keep the slider in sync with video playback
    # def update_time_slider(self):
        # if self.playing:
            # duration = self.player.get_length()
            # if duration > 0:
                # pos = self.player.get_time() / duration * 100
                # self.slider.set(pos)
            # else:
                # logging.info("Warning: Video duration is zero or not available.")  # Debug message for zero duration
        # self.parent.after(1000, self.update_time_slider)
    
    
