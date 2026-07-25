"""
Image viewers for Vibe Player: stable Canvas (Tk) and optional GPU (Pyglet).

- ``ImageViewerLegacy`` — primary, Canvas + scrollbars (always reliable).
- ``ImageViewerGPU`` — optional OpenGL viewer on a dedicated worker thread.
- ``create_image_viewer`` / ``ImageViewer`` — pick implementation from preferences.

Pyglet is imported only after ``_ensure_pyglet_worker()`` runs (first GPU attempt),
so Canvas-only use never loads GL.
"""

import io
import logging
import os
import queue as _Q
import sys
import threading
import time

from PIL import Image as PILImage, ImageTk
from screeninfo import get_monitors
import tkinter as tk

from gui_elements import (
    CTkFlatContextMenu,
    append_rating_cascade_to_flat_menu,
    append_rating_submenu,
    format_hud_rating_suffix,
    rating_color_name,
    rating_pyglet_rgba,
    _current_file_rating,
)
from image_crop_hud import CropModeController
from image_edit_guard import confirm_leave_image_edit
from image_loader import load_pil_frames, load_pil_image
from image_resize_dialog import open_resize_image_dialog
from vtp_constants import IMAGE_FORMATS


class ImageViewerLegacy:
   
    def __init__(self, parent, image_path, image_name):
        self.parent = parent
        self.controller = parent  # Reference na hlavní aplikaci (pro přístup k hotkeys)
        self.image_path = image_path
        self.image_name = image_name
        self.is_fullscreen = False
        try:
            _mons = get_monitors()
            self.screen_width = _mons[0].width
            self.screen_height = _mons[0].height
        except Exception:
            self.screen_width = 1280
            self.screen_height = 720   

        # Vytvoření okna
        self.image_window = tk.Toplevel(self.parent)
        self.image_window.lift()
        self.image_window.focus_force()
        self.image_window.attributes('-topmost', True)
        self.image_window.title(self.image_name)

        # Canvas and scrollbars
        self.canvas = tk.Canvas(self.image_window, bg='black')
        self.canvas.grid(row=0, column=0, sticky='nsew')

        self.hbar = tk.Scrollbar(self.image_window, orient=tk.HORIZONTAL, command=self._on_canvas_xscroll)
        self.hbar.grid(row=1, column=0, sticky='ew')
        self.canvas.config(xscrollcommand=self.hbar.set)

        self.vbar = tk.Scrollbar(self.image_window, orient=tk.VERTICAL, command=self._on_canvas_yscroll)
        self.vbar.grid(row=0, column=1, sticky='ns')
        self.canvas.config(yscrollcommand=self.vbar.set)

        self.image_window.grid_rowconfigure(0, weight=1)
        self.image_window.grid_columnconfigure(0, weight=1)

        # Animation (GIF / animated WebP) — empty until _apply_loaded_frames
        self._anim_frames: list = []
        self._anim_durations: list = []
        self._anim_index = 0
        self._anim_after_id = None

        frames, durations = load_pil_frames(self.image_path)
        self._apply_loaded_frames(frames, durations, reset_title=False)
        self.photo = ImageTk.PhotoImage(self.original_image)

        self.canvas_image = self.canvas.create_image(0, 0, image=self.photo, anchor=tk.NW)

        self.zoom_factor = 1.0
        # Uživatelsky zvolený režim zobrazení — při přechodu na další obrázek se znovu aplikuje,
        # dokud uživatel nezmění zoom (manual) nebo nezvolí jiný režim.
        self._view_fit_mode = "none"  # none | best_fit | fit_width | actual | manual
        self._windowed_geometry = None  # restored when leaving fullscreen

        # Proměnná pro časovač HQ renderu
        self._hq_timer = None
        self._overlay_after_id = None
        self._zoom_timer = None
        
        # --- NOVÉ PROMĚNNÉ ---
        self.bg_colors = ['black', '#303030', 'white']
        self.bg_index = 0
        self.show_info = True # Defaultně zapnuto
        self.info_text_id = None
        self.zoom_text_id = None
        self.minimap_max_size = 150
        self.minimap_padding = 12

        # Inline crop mode (bottom HUD + canvas overlay); inactive until enter_crop_mode().
        self.crop = CropModeController(self)
        self._resize_dialog = None  # open ResizeImageDialog instance, if any
        self._image_dirty = False   # True after in-memory resize until save/reload

        # --- BINDINGS (Napojení na centrální hotkeys) ---
        # Funkce pro bezpečné získání klávesy z hlavního nastavení
        def hk(action_name, default=None):
            if hasattr(self.controller, 'hotkeys_map'):
                return self.controller.hotkeys_map.get(action_name, default)
            return default

        def consume(callback):
            def handler(event):
                callback(event)
                return "break"
            return handler

        # 1. Myš a základní ovládání
        # self.image_window.bind(hk('zoom_thumb', "<Control-MouseWheel>"), self.zoom)
        
        self.image_window.bind("<MouseWheel>", self._wheel_handler)
        
        self.image_window.bind("<ButtonPress-2>", self.start_pan)
        self.image_window.bind("<ButtonRelease-2>", self.end_pan)
        self.image_window.bind("<B2-Motion>", self.do_pan)
        self.image_window.bind("<Button-3>", self.show_context_menu)
        self.canvas.bind("<Configure>", self.resize_canvas)

        # 2. Navigace (Z `hotkeys.py`)
        self.image_window.bind(hk('image_next', '<Right>'), consume(self._hotkey_next_image))
        self.image_window.bind(hk('image_prev', '<Left>'), consume(self._hotkey_prev_image))
        # Escape: cancel crop when active, otherwise close the viewer.
        self.image_window.bind(hk('close_window', '<Escape>'), consume(self._on_escape_key))
        self.image_window.bind(hk('image_delete', '<Delete>'), consume(self._hotkey_delete))
        # Enter confirms crop (default = overwrite with confirmation).
        self.image_window.bind("<Return>", consume(self._on_return_key))
        self.image_window.bind("<KP_Enter>", consume(self._on_return_key))

        # 3. Manipulace s obrázkem (Z `hotkeys.py`)
        self.image_window.bind(hk('image_actual_size', 'a'), consume(lambda e: self._hotkey_unless_crop(self.actual_size)))
        self.image_window.bind(hk('image_toggle_bg', 'c'), consume(lambda e: self._hotkey_unless_crop(self.toggle_background, e)))
        self.image_window.bind(hk('image_toggle_info', 'i'), consume(lambda e: self._hotkey_unless_crop(self.toggle_info, e)))
        
        self.image_window.bind(hk('image_fit_best', 'b'), consume(lambda e: self._hotkey_unless_crop(self.best_fit)))
        self.image_window.bind(hk('image_fit_width', 'w'), consume(lambda e: self._hotkey_unless_crop(self.fit_width)))
        
        self.image_window.bind(hk('image_zoom_in', '+'), consume(self.zoom_in))
        self.image_window.bind(hk('image_zoom_out', '-'), consume(self.zoom_out))
        # Alternativní Control +/- pro zoom
        self.image_window.bind("<Control-plus>", consume(self.zoom_in))
        self.image_window.bind("<Control-minus>", consume(self.zoom_out))

        self.image_window.bind(hk('image_rotate_left', 'l'), consume(lambda e: self._hotkey_unless_crop(self.rotate_left)))
        self.image_window.bind(hk('image_rotate_right', 'r'), consume(lambda e: self._hotkey_unless_crop(self.rotate_right)))
        self.image_window.bind(hk('image_flip_h', 'h'), consume(lambda e: self._hotkey_unless_crop(self.flip_horizontal)))
        self.image_window.bind(hk('image_flip_v', 'v'), consume(lambda e: self._hotkey_unless_crop(self.flip_vertical)))
        self.image_window.bind(hk('image_crop', 'x'), consume(self._hotkey_crop))
        self.image_window.bind('X', consume(self._hotkey_crop))
        self.image_window.bind(hk('image_resize', '<Control-r>'), consume(self._hotkey_resize))
        self.image_window.bind('<Control-R>', consume(self._hotkey_resize))
        
        self.image_window.bind(hk('image_copy', '<Control-c>'), consume(lambda e: self._hotkey_unless_crop(self.copy_image_to_clipboard)))
        self.image_window.bind(hk('image_save', '<Control-s>'), consume(lambda e: self._hotkey_unless_crop(self.save_image_to_folder)))
        if callable(getattr(self.controller, "open_library", None)):
            self.image_window.bind("<Control-l>", consume(lambda e: self.controller.open_library()))
            self.image_window.bind("<Control-L>", consume(lambda e: self.controller.open_library()))
        
        # 4. Fullscreen
        self.image_window.bind(hk('image_fullscreen', '<F11>'), consume(self.toggle_fullscreen))
        # Fallback pro "F" (běžné v prohlížečích) a Alt-Enter
        self.image_window.bind("f", consume(lambda e: self._hotkey_unless_crop(self.toggle_fullscreen, e)))
        self.image_window.bind("<Alt-Return>", consume(self.toggle_fullscreen))

        self.image_window.bind("<F10>", lambda e: self.debug_print_monitor())

        self.update_scrollbars()
        self._set_image_scrollregion_only()

        # Same flags as Pyglet viewer — used by main.py fast-open and delete flow
        self._running = True
        self._start_animation_if_needed()

        open_fs = bool(getattr(self.controller, "image_viewer_open_fullscreen", True))
        self._layout_initial_window(open_fullscreen=open_fs)

        #Na konci initu vynutíme první vykreslení HUDu
        self._overlay_after_id = self.image_window.after(100, self._refresh_overlays)

        def _on_toplevel_close():
            self._do_close()

        self.image_window.protocol("WM_DELETE_WINDOW", _on_toplevel_close)

    def _monitor_at(self, x, y):
        """Return the monitor containing screen point (x, y), or the primary."""
        try:
            monitors = get_monitors()
        except Exception:
            return None
        for mon in monitors:
            if mon.x <= x < mon.x + mon.width and mon.y <= y < mon.y + mon.height:
                return mon
        return monitors[0] if monitors else None

    def _layout_initial_window(self, *, open_fullscreen: bool = False):
        """
        Size the window so the image fits the canvas without needless scrollbars.

        Tk ``geometry(WxH)`` on Windows sizes the *outer* frame (incl. title bar),
        so a naive image-sized geometry leaves the client smaller than the photo.
        We measure chrome and grow the outer size accordingly; if the image is
        larger than the work area, we best-fit and size the window to that.
        """
        try:
            px = self.parent.winfo_rootx() + max(1, self.parent.winfo_width()) // 2
            py = self.parent.winfo_rooty() + max(1, self.parent.winfo_height()) // 2
        except Exception:
            px, py = self.screen_width // 2, self.screen_height // 2

        mon = self._monitor_at(px, py)
        if mon is None:
            mon_w, mon_h, mon_x, mon_y = self.screen_width, self.screen_height, 0, 0
        else:
            mon_w, mon_h, mon_x, mon_y = mon.width, mon.height, mon.x, mon.y

        margin = 56
        max_w = max(320, mon_w - margin * 2)
        max_h = max(240, mon_h - margin * 2)

        iw, ih = self.original_image.size
        if iw < 1 or ih < 1:
            return

        if iw <= max_w and ih <= max_h:
            self.zoom_factor = 1.0
            self._view_fit_mode = "actual"
            client_w, client_h = iw, ih
        else:
            scale = min(max_w / iw, max_h / ih)
            self.zoom_factor = scale
            self._view_fit_mode = "best_fit"
            client_w = max(1, int(round(iw * scale)))
            client_h = max(1, int(round(ih * scale)))

        # First pass — place roughly, then correct for window chrome.
        x = mon_x + max(0, (mon_w - client_w) // 2)
        y = mon_y + max(0, (mon_h - client_h) // 2)
        self.image_window.geometry(f"{client_w}x{client_h}+{x}+{y}")
        self.image_window.update_idletasks()

        self.hbar.grid_remove()
        self.vbar.grid_remove()
        self.image_window.update_idletasks()

        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        ow = max(1, self.image_window.winfo_width())
        oh = max(1, self.image_window.winfo_height())
        chrome_w = max(0, ow - cw)
        chrome_h = max(0, oh - ch)

        outer_w = min(mon_w, client_w + chrome_w)
        outer_h = min(mon_h, client_h + chrome_h)
        x = mon_x + max(0, (mon_w - outer_w) // 2)
        y = mon_y + max(0, (mon_h - outer_h) // 2)
        geom = f"{outer_w}x{outer_h}+{x}+{y}"
        self.image_window.geometry(geom)
        self._windowed_geometry = geom

        self.update_image(center=True, high_quality=True)

        if open_fullscreen:
            # Defer until the window is mapped so monitor detection is stable.
            self.image_window.after(30, self._enter_fullscreen_initial)

    def _enter_fullscreen_initial(self):
        if not getattr(self, "_running", False) or self.is_fullscreen:
            return
        self.set_fullscreen(True)

    def _is_animated(self) -> bool:
        return len(getattr(self, "_anim_frames", []) or []) > 1

    def _apply_loaded_frames(self, frames, durations, *, reset_title: bool = True):
        """Install decoded frames as the current image (does not start the timer)."""
        if not frames:
            raise ValueError("no image frames")
        self._stop_animation()
        self._anim_frames = list(frames)
        self._anim_durations = list(durations) if durations else [0] * len(frames)
        if len(self._anim_durations) < len(self._anim_frames):
            self._anim_durations.extend(
                [100] * (len(self._anim_frames) - len(self._anim_durations))
            )
        self._anim_index = 0
        self.image = self._anim_frames[0]
        self.original_image = self._anim_frames[0]
        if reset_title:
            self.image_window.title(self.image_name)

    def _stop_animation(self):
        job = getattr(self, "_anim_after_id", None)
        if job is None:
            return
        try:
            self.image_window.after_cancel(job)
        except Exception:
            pass
        self._anim_after_id = None

    def _start_animation_if_needed(self):
        self._stop_animation()
        if not getattr(self, "_running", False) or not self._is_animated():
            return
        self._schedule_next_anim_frame()

    def _schedule_next_anim_frame(self):
        if not getattr(self, "_running", False) or not self._is_animated():
            return
        delay = self._anim_durations[self._anim_index % len(self._anim_durations)]
        self._anim_after_id = self.image_window.after(delay, self._advance_anim_frame)

    def _advance_anim_frame(self):
        self._anim_after_id = None
        if not getattr(self, "_running", False) or not self._is_animated():
            return
        try:
            if not self.image_window.winfo_exists():
                return
        except tk.TclError:
            return
        self._anim_index = (self._anim_index + 1) % len(self._anim_frames)
        self.original_image = self._anim_frames[self._anim_index]
        # Fast path only — HQ debounce / HUD redraw would fight the frame timer.
        self.update_image(high_quality=False, refresh_overlays=False)
        self._schedule_next_anim_frame()

    def _map_anim_frames(self, transform):
        """Apply a PIL transform to every animation frame (and the current view)."""
        was_animated = self._is_animated()
        if was_animated:
            self._stop_animation()
        self._anim_frames = [transform(f) for f in self._anim_frames]
        if not self._anim_frames:
            return
        self._anim_index = min(self._anim_index, len(self._anim_frames) - 1)
        self.original_image = self._anim_frames[self._anim_index]
        self.image = self.original_image
        if was_animated:
            self._start_animation_if_needed()

    def _cancel_pending_image_timers(self):
        for attr in ("_hq_timer", "_overlay_after_id", "_zoom_timer", "_anim_after_id"):
            job = getattr(self, attr, None)
            if job is None:
                continue
            try:
                self.image_window.after_cancel(job)
            except Exception:
                pass
            setattr(self, attr, None)

    def _do_close(self):
        """Match Pyglet viewer API for controller / fast-open code paths."""
        if getattr(self, "crop", None) is not None and self.crop.active:
            self.crop.exit()
        self._running = False
        self._cancel_pending_image_timers()
        self._anim_frames = []
        self._anim_durations = []
        try:
            if self.image_window.winfo_exists():
                self.image_window.destroy()
        except tk.TclError:
            pass

    def _crop_active(self) -> bool:
        return bool(getattr(self, "crop", None) and self.crop.active)

    def enter_crop_mode(self):
        """Start inline crop overlay + bottom HUD toolbar."""
        self.crop.enter()

    def exit_crop_mode(self):
        """Cancel crop mode without applying."""
        self.crop.exit()

    def _on_escape_key(self, event=None):
        """Escape cancels crop when active; otherwise closes the viewer."""
        if self._crop_active():
            self.exit_crop_mode()
            return
        self._do_close()

    def _on_return_key(self, event=None):
        """Enter applies crop (overwrite + confirm) when crop mode is active."""
        if not self._crop_active():
            return
        hud = self.crop.hud
        if hud is not None:
            focused = self.image_window.focus_get()
            # CTkEntry focus is on an inner tk widget — commit size if editing W/H.
            for entry in (hud.width_entry, hud.height_entry):
                inner = getattr(entry, "_entry", None)
                if focused is not None and (focused is entry or focused is inner):
                    hud._commit_size()
                    break
        self.crop.apply("overwrite")

    def _hotkey_unless_crop(self, fn, event=None):
        """No-op for viewer actions that conflict with crop mode."""
        if self._crop_active():
            return
        if event is None:
            return fn()
        try:
            return fn(event)
        except TypeError:
            return fn()

    def _hotkey_next_image(self, event=None):
        self.show_next_image()

    def _hotkey_prev_image(self, event=None):
        self.show_prev_image()

    def _hotkey_delete(self, event=None):
        if not self._confirm_leave_edit_for_navigation():
            return
        self.delete_current_image(event)

    def _hotkey_crop(self, event=None):
        """Toggle crop mode with the image_crop hotkey (default: X)."""
        if self._crop_active():
            self.exit_crop_mode()
        else:
            self.enter_crop_mode()

    def _hotkey_resize(self, event=None):
        """Open the resize dialog (blocked while cropping)."""
        if self._crop_active():
            return
        self.open_resize_dialog()

    def _resize_dialog_open(self) -> bool:
        dlg = getattr(self, "_resize_dialog", None)
        if dlg is None:
            return False
        try:
            return bool(dlg.winfo_exists())
        except Exception:
            self._resize_dialog = None
            return False

    def _active_edit_processes(self) -> list:
        """Labels for the leave-edit confirmation (Crop / Resize)."""
        names = []
        if self._crop_active():
            names.append("Crop")
        if self._resize_dialog_open() or getattr(self, "_image_dirty", False):
            # Dialog open, or resize already applied but not saved.
            if "Resize" not in names:
                names.append("Resize")
        return names

    def _abandon_image_edits(self):
        """Cancel crop / close resize dialog / clear dirty flag (no save)."""
        if self._crop_active():
            self.exit_crop_mode()
        if self._resize_dialog_open():
            try:
                self._resize_dialog.force_close()
            except Exception:
                pass
            self._resize_dialog = None
        self._image_dirty = False

    def _confirm_leave_edit_for_navigation(self) -> bool:
        """Return True if navigation may proceed (possibly after user confirms)."""
        processes = self._active_edit_processes()
        if not processes:
            return True
        parent = self.image_window
        if not confirm_leave_image_edit(parent, processes):
            return False
        self._abandon_image_edits()
        return True

    def open_resize_dialog(self):
        """Show the Resize Image modal for the current image."""
        if self._crop_active():
            self.exit_crop_mode()
        if self._resize_dialog_open():
            try:
                self._resize_dialog.lift()
                self._resize_dialog.focus_force()
            except Exception:
                pass
            return
        w, h = self.original_image.size
        dlg = open_resize_image_dialog(
            self.image_window,
            orig_width=w,
            orig_height=h,
            on_apply=self.resize_image,
        )
        self._resize_dialog = dlg

        def _clear_ref(_event=None):
            if getattr(self, "_resize_dialog", None) is dlg:
                self._resize_dialog = None

        try:
            dlg.bind("<Destroy>", _clear_ref)
        except Exception:
            pass

    def resize_image(self, new_width, new_height, resampling_filter=PILImage.LANCZOS):
        """Resize all animation frames (or the still image) and refresh the view."""
        if self._crop_active():
            self.exit_crop_mode()
        w = max(1, int(new_width))
        h = max(1, int(new_height))
        filt = resampling_filter if resampling_filter is not None else PILImage.LANCZOS
        self._map_anim_frames(lambda im: im.resize((w, h), filt))
        self._image_dirty = True
        if self._view_fit_mode == "best_fit":
            self.best_fit()
        elif self._view_fit_mode == "fit_width":
            self.fit_width()
        elif self._view_fit_mode == "actual":
            self.zoom_factor = 1.0
            self.update_image(center=True)
        else:
            self.update_image(center=True)

    def delete_current_image(self, event=None):
        """Vyžádá smazání aktuálního obrázku."""
        logging.info(f"[Image] Requesting delete for: {self.image_path}")
        if hasattr(self.controller, 'confirm_delete_item'):
            # Zavřeme okno, protože soubor zmizí
            self._do_close()
            # Vyvoláme dialog v hlavním okně
            self.controller.confirm_delete_item(paths=[self.image_path])

    def center_image(self):
        if not getattr(self, "_running", False):
            return
        try:
            if not self.image_window.winfo_exists():
                return
        except tk.TclError:
            return
        self.canvas.update_idletasks()  # důležité, aby Canvas znal správné rozměry!

        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()

        image_width = self.photo.width()
        image_height = self.photo.height()

        x = max((canvas_width - image_width) // 2, 0)
        y = max((canvas_height - image_height) // 2, 0)

        # logging.info(f"DEBUG Center image: Canvas={canvas_width}x{canvas_height}, Img={image_width}x{image_height}")
        self.canvas.coords(self.canvas_image, x, y)
        self._set_image_scrollregion_only()
        self._refresh_overlays()

    def _center_image_for_size(self, image_width, image_height):
        """Center canvas image using known resized dimensions (reduces visible recenter jitter)."""
        self.canvas.update_idletasks()
        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        x = max((canvas_width - int(image_width)) // 2, 0)
        y = max((canvas_height - int(image_height)) // 2, 0)
        self.canvas.coords(self.canvas_image, x, y)

    def _refresh_overlays(self):
        if not getattr(self, "_running", False):
            return
        try:
            if not self.image_window.winfo_exists():
                return
        except tk.TclError:
            return
        self.draw_info_hud()
        # Hide minimap / zoom HUD while cropping — bottom strip is owned by crop toolbar.
        if self._crop_active():
            self.canvas.delete("minimap")
            self.canvas.delete("zoom_hud")
            self.crop.redraw()
        else:
            self.draw_minimap()
            self.draw_zoom_overlay()

    def _on_canvas_xscroll(self, *args):
        self.canvas.xview(*args)
        self._refresh_overlays()

    def _on_canvas_yscroll(self, *args):
        self.canvas.yview(*args)
        self._refresh_overlays()

    def _set_image_scrollregion_only(self):
        """Scroll jen podle obrázku — HUD nesmí rozšiřovat scrollregion (posuny / skoky)."""
        r = self.canvas.bbox(self.canvas_image)
        if r:
            self.canvas.config(scrollregion=r)
        else:
            self.canvas.config(scrollregion=(0, 0, 1, 1))

    def load_image(self, path, name):
        if self._resize_dialog_open():
            try:
                self._resize_dialog.force_close()
            except Exception:
                pass
            self._resize_dialog = None
        if self._crop_active():
            self.exit_crop_mode()
        self._image_dirty = False
        self.image_path = path
        self.image_name = name

        try:
            frames, durations = load_pil_frames(path)
            self._apply_loaded_frames(frames, durations, reset_title=True)

            mode = self._view_fit_mode
            if mode == "best_fit":
                self.best_fit()
            elif mode == "fit_width":
                self.fit_width()
            elif mode == "actual":
                self.zoom_factor = 1.0
                self.update_image(center=True)
            elif mode == "manual":
                self.update_image(center=True)
            else:
                self.zoom_factor = 1.0
                self.update_image(center=True)

            self._start_animation_if_needed()

            # --- AKTUALIZACE HUD ---
            # Zavoláme to explicitně, aby se aktualizoval index souboru (např. 5/120)
            self.draw_info_hud()

        except Exception as e:
            logging.error(f"Failed to load image {name}: {e}")




    def skip(self, direction):
        if not self._confirm_leave_edit_for_navigation():
            return
        try:
            all_files = [f for f in self.controller.video_files if f['path'].lower().endswith(IMAGE_FORMATS)]
            # Najít index pomocí cesty k souboru
            current_index = next((i for i, f in enumerate(all_files) if f['path'] == self.image_path), None)
            
            if current_index is None:
                return
            new_index = (current_index + direction) % len(all_files)
            new_file = all_files[new_index]
            self.load_image(new_file['path'], new_file['name'])
        except Exception as e:
            logging.info("[DEBUG] ImageViewer skip error: %s", e)

    def show_next_image(self):
        self.skip(1)

    def show_prev_image(self):
        self.skip(-1)

    def resize_canvas(self, event):
        self.update_scrollbars()
        self._set_image_scrollregion_only()
        self._refresh_overlays()
        # self.canvas.update_idletasks() # Není nutné volat při každém pohybu, zpomaluje resize

    def zoom(self, event):
        self._view_fit_mode = "manual"
        scale = 1.1 if event.delta > 0 else 0.9
        self.zoom_factor *= scale
        self.update_image()

    def zoom_in(self, event=None):
        self._view_fit_mode = "manual"
        self.zoom_factor *= 1.1
        self.update_image()

    def zoom_out(self, event=None):
        self._view_fit_mode = "manual"
        self.zoom_factor *= 0.9
        self.update_image()

    def actual_size(self):
        self._view_fit_mode = "actual"
        self.zoom_factor = 1.0
        self.update_image(center=True)

    def best_fit(self):
        self._view_fit_mode = "best_fit"
        self.image_window.update_idletasks() # Update to get real dims
        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        img_width, img_height = self.original_image.size
        if img_width == 0 or img_height == 0: return
        
        scale_w = canvas_width / img_width
        scale_h = canvas_height / img_height
        self.zoom_factor = min(scale_w, scale_h)
        self.update_image(center=True)

    def fit_width(self):
        self._view_fit_mode = "fit_width"
        self.image_window.update_idletasks()
        canvas_width = self.canvas.winfo_width()
        img_width, _ = self.original_image.size
        if img_width == 0: return
        self.zoom_factor = canvas_width / img_width
        self.update_image(center=True)
        
    def rotate_left(self):
        if self._crop_active():
            self.exit_crop_mode()
        self._map_anim_frames(lambda im: im.rotate(90, expand=True))
        if self._view_fit_mode == "best_fit":
            self.best_fit()
        elif self._view_fit_mode == "fit_width":
            self.fit_width()
        elif self._view_fit_mode == "actual":
            self.zoom_factor = 1.0
            self.update_image(center=True)
        elif self._view_fit_mode == "manual":
            self.update_image(center=True)
        else:
            self.zoom_factor = 1.0
            self.update_image(center=True)

    def rotate_right(self):
        if self._crop_active():
            self.exit_crop_mode()
        self._map_anim_frames(lambda im: im.rotate(-90, expand=True))
        if self._view_fit_mode == "best_fit":
            self.best_fit()
        elif self._view_fit_mode == "fit_width":
            self.fit_width()
        elif self._view_fit_mode == "actual":
            self.zoom_factor = 1.0
            self.update_image(center=True)
        elif self._view_fit_mode == "manual":
            self.update_image(center=True)
        else:
            self.zoom_factor = 1.0
            self.update_image(center=True)

    def flip_horizontal(self):
        if self._crop_active():
            self.exit_crop_mode()
        self._map_anim_frames(lambda im: im.transpose(PILImage.FLIP_LEFT_RIGHT))
        self.update_image()

    def flip_vertical(self):
        if self._crop_active():
            self.exit_crop_mode()
        self._map_anim_frames(lambda im: im.transpose(PILImage.FLIP_TOP_BOTTOM))
        self.update_image()

    def save_image_to_folder(self):
        from tkinter import filedialog
        save_path = filedialog.asksaveasfilename(defaultextension=".png",
                                                  filetypes=[("PNG files", "*.png"), ("JPEG files", "*.jpg;*.jpeg"), ("All Files", "*.*")])
        if save_path:
            self.original_image.save(save_path)
            self._image_dirty = False

    def start_pan(self, event):
        self.canvas.scan_mark(event.x, event.y)
        self.canvas.config(cursor="hand2")

    def end_pan(self, event):
        self.canvas.config(cursor="arrow")

    def do_pan(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)
        self._refresh_overlays()
 
    def toggle_fullscreen(self, event=None):
        self.set_fullscreen(not self.is_fullscreen)

    def set_fullscreen(self, enabled: bool):
        """Enter or leave borderless fullscreen on the current monitor."""
        enabled = bool(enabled)
        if enabled == self.is_fullscreen:
            return
        self.image_window.update_idletasks()

        if enabled:
            try:
                self._windowed_geometry = self.image_window.geometry()
            except tk.TclError:
                pass
            x = self.image_window.winfo_x() + self.image_window.winfo_width() // 2
            y = self.image_window.winfo_y() + self.image_window.winfo_height() // 2
            target_monitor = self._monitor_at(x, y)

            self.is_fullscreen = True
            if target_monitor:
                self.image_window.overrideredirect(True)
                self.image_window.geometry(
                    f"{target_monitor.width}x{target_monitor.height}+{target_monitor.x}+{target_monitor.y}"
                )
            else:
                self.image_window.attributes("-fullscreen", True)
            # Fit the image to the fullscreen canvas.
            if self._view_fit_mode in ("none", "actual", "manual"):
                self._view_fit_mode = "best_fit"
            self.image_window.after(80, self.best_fit)
        else:
            self.is_fullscreen = False
            self.image_window.overrideredirect(False)
            self.image_window.attributes("-fullscreen", False)
            geom = getattr(self, "_windowed_geometry", None)
            if geom:
                self.image_window.geometry(geom)
            else:
                self._layout_initial_window(open_fullscreen=False)

        self.update_scrollbars()
        self._set_image_scrollregion_only()
        self.image_window.after(100, self.center_image)
        self._refresh_overlays()

    def debug_print_monitor(self):
        self.image_window.update_idletasks()
        x = self.image_window.winfo_x()
        y = self.image_window.winfo_y()
        logging.info(f"[DEBUG] Window Pos: {x}, {y}")

    def copy_image_to_clipboard(self):
        try:
            import io
            # Save current image to an in-memory BMP file (Windows clipboard loves BMP)
            output = io.BytesIO()
            self.original_image.convert("RGB").save(output, "BMP")
            data = output.getvalue()[14:]  # Remove BMP header (first 14 bytes)
            output.close()

            self.image_window.clipboard_clear()
            self.image_window.clipboard_append(data)
            self.image_window.update()
            logging.info("Image copied to clipboard.")
        except Exception as e:
            logging.info(f"Failed to copy image to clipboard: {e}")

    def _wheel_handler(self, event):
        """
        Robustní handler pro kolečko myši.
        Řeší Zoom (Ctrl/Shift) i Posun (bez kláves).
        """
        # Zjistíme stav kláves (bitové masky pro Windows/Linux se mohou lišit, toto je pro Windows)
        # 0x0004 je Control, 0x0001 je Shift
        ctrl_pressed = (event.state & 0x0004) != 0
        shift_pressed = (event.state & 0x0001) != 0

        if ctrl_pressed or shift_pressed:
            # --- ZOOM ---
            self.zoom(event)
        else:
            # --- POSUN (Scroll) ---
            # Pokud se obrázek vejde do okna, posunujeme další/předchozí? 
            # Nebo raději vertikální posun canvasu? Standard je posun canvasu.
            if self.vbar.get() != (0.0, 1.0):
                self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")
                self._refresh_overlays()


    def update_image(self, high_quality=False, center=False, refresh_overlays=True):
        """
        Aktualizuje obrázek na plátně.
        high_quality=False -> Použije rychlý BILINEAR (pro zoomování).
        high_quality=True  -> Použije pomalý LANCZOS (pro finální zobrazení).
        """
        if not getattr(self, "_running", False):
            return
        try:
            if not self.image_window.winfo_exists():
                return
        except tk.TclError:
            return

        # 1. Zrušíme jakýkoliv čekající HQ render (protože uživatel právě změnil stav)
        if self._hq_timer:
            try:
                self.image_window.after_cancel(self._hq_timer)
            except Exception:
                pass
            self._hq_timer = None

        width = int(self.original_image.width * self.zoom_factor)
        height = int(self.original_image.height * self.zoom_factor)
        
        # Ochrana proti příliš malým rozměrům (min 1px)
        width = max(1, width)
        height = max(1, height)

        # 2. Rozhodnutí o metodě
        # BILINEAR je rychlý a vypadá OK. LANCZOS je pomalý a vypadá skvěle.
        method = PILImage.LANCZOS if high_quality else PILImage.BILINEAR
        
        # 3. Samotný resize
        resized_image = self.original_image.resize((width, height), method)
        self.photo = ImageTk.PhotoImage(resized_image)
        self.canvas.itemconfig(self.canvas_image, image=self.photo)
        if center:
            self._center_image_for_size(width, height)

        # Scrollbary mění vnitřní rozměr canvasu — proto centrovat znovu po jejich grid_remove/grid.
        self.update_scrollbars()
        if center:
            self.canvas.update_idletasks()
            self._center_image_for_size(width, height)

        self._set_image_scrollregion_only()

        # 4. Naplánování HQ renderu (Debounce)
        # Pokud jsme teď jeli v rychlém režimu, řekneme: 
        # "Za 150ms to překresli do hezka, pokud do té doby uživatel nic neudělá."
        # Animace: žádný HQ debounce — soupeřil by s frame timerem.
        if (
            not high_quality
            and getattr(self, "_running", False)
            and not self._is_animated()
        ):
            self._hq_timer = self.image_window.after(150, self._render_hq)
            
        # --- ZDE MUSÍ BÝT TOTO: ---
        if refresh_overlays:
            self._refresh_overlays()

    def _render_hq(self):
        """Voláno časovačem, když je klid."""
        self._hq_timer = None
        if not getattr(self, "_running", False):
            return
        logging.info("[HQ Render] Refining image quality...")
        self.update_image(high_quality=True)




    def toggle_background(self, event=None):
        """Cyklicky mění barvu pozadí (Černá -> Šedá -> Bílá)."""
        self.bg_index = (self.bg_index + 1) % len(self.bg_colors)
        color = self.bg_colors[self.bg_index]
        self.canvas.configure(bg=color)
        self._refresh_overlays() # Překreslit info, aby bylo vidět (změna barvy textu)

    def toggle_info(self, event=None):
        """Zobrazí/Skryje info text."""
        self.show_info = not self.show_info
        self._refresh_overlays()

    def draw_info_hud(self):
        """Vykreslí textové info v levém horním rohu."""
        # Smazat celý HUD (včetně stínového řádku — jinak leak a rozbitý bbox).
        self.canvas.delete("hud")
        self.info_text_id = None

        if not self.show_info:
            return

        # Získání dat
        w, h = self.original_image.size
        
        # Zkusíme zjistit index souboru (např. 5/120)
        index_str = ""
        try:
            # Toto je trochu hack, saháme do controlleru, ale je to rychlé
            all_files = [f for f in self.controller.video_files if f['path'].lower().endswith(IMAGE_FORMATS)]
            total = len(all_files)
            # Najdeme index aktuálního
            idx = next((i for i, f in enumerate(all_files) if f['path'] == self.image_path), -1)
            if idx != -1:
                index_str = f"[{idx + 1}/{total}] "
        except:
            pass

        text = f"{index_str}{self.image_name}  |  {w}x{h} px"
        rating = _current_file_rating(self.controller, self.image_path)
        rating_suffix = format_hud_rating_suffix(rating)
        rating_color = rating_color_name(rating)
        
        # Barva textu podle pozadí (aby byl vždy čitelný)
        text_color = "black" if self.bg_colors[self.bg_index] == "white" else "white"
        
        # Pro jednoduchost a rychlost zkusíme canvas text s offsetem podle scrollu:
        cx = self.canvas.canvasx(10)
        cy = self.canvas.canvasy(10)
        font = ("Segoe UI", 10, "bold")
        
        # Vytvoření textu s lehkým stínem pro čitelnost
        self.canvas.create_text(cx + 1, cy + 1, text=text, anchor="nw", fill="black", font=font, tags="hud")
        self.info_text_id = self.canvas.create_text(cx, cy, text=text, anchor="nw", fill=text_color, font=font, tags="hud")

        if rating_suffix:
            try:
                import tkinter.font as tkfont
                base_w = tkfont.Font(font=font).measure(text)
            except Exception:
                base_w = len(text) * 6
            rx = cx + base_w
            self.canvas.create_text(
                rx + 1, cy + 1, text=rating_suffix, anchor="nw", fill="black", font=font, tags="hud"
            )
            self.canvas.create_text(
                rx, cy, text=rating_suffix, anchor="nw", fill=rating_color, font=font, tags="hud"
            )
        
        # Zajistit, že HUD je vždy nahoře
        self.canvas.tag_raise("hud")

    def notify_rating_changed(self, rating=None):
        """Refresh HUD after rating assign (visible even over fullscreen image)."""
        was_off = not getattr(self, "show_info", True)
        if was_off:
            self.show_info = True
        self.draw_info_hud()
        if was_off and hasattr(self, "image_window"):
            job = getattr(self, "_rating_hud_restore_job", None)
            if job is not None:
                try:
                    self.image_window.after_cancel(job)
                except Exception:
                    pass

            def _restore():
                self._rating_hud_restore_job = None
                self.show_info = False
                self.draw_info_hud()

            self._rating_hud_restore_job = self.image_window.after(3000, _restore)

    def _zoom_percent_text(self):
        return f"{int(round(self.zoom_factor * 100))}%"

    def draw_zoom_overlay(self):
        """Draw a compact zoom indicator above the bottom-right minimap/widget."""
        self.canvas.delete("zoom_hud")
        self.zoom_text_id = None

        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        if canvas_w <= 1 or canvas_h <= 1:
            return

        y_screen = canvas_h - self.minimap_padding
        image_bbox = self.canvas.bbox(self.canvas_image)
        if image_bbox:
            img_x1, img_y1, img_x2, img_y2 = image_bbox
            img_w = img_x2 - img_x1
            img_h = img_y2 - img_y1
            if img_w > canvas_w or img_h > canvas_h:
                mm_scale = self.minimap_max_size / max(img_w, img_h)
                mm_h = max(4, int(img_h * mm_scale))
                y_screen = canvas_h - self.minimap_padding - mm_h - 6

        cx = self.canvas.canvasx(canvas_w - 12)
        cy = self.canvas.canvasy(y_screen)
        text = self._zoom_percent_text()
        font = ("Segoe UI", 10, "bold")

        self.canvas.create_text(
            cx + 1, cy + 1, text=text, anchor="se",
            fill="black", font=font, tags="zoom_hud"
        )
        self.zoom_text_id = self.canvas.create_text(
            cx, cy, text=text, anchor="se",
            fill="white", font=font, tags="zoom_hud"
        )
        self.canvas.tag_raise("zoom_hud")

    def draw_minimap(self):
        """Draw bottom-right viewport overview for zoomed/panned legacy Canvas viewer."""
        self.canvas.delete("minimap")

        image_bbox = self.canvas.bbox(self.canvas_image)
        if not image_bbox:
            return

        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        if canvas_w <= 1 or canvas_h <= 1:
            return

        img_x1, img_y1, img_x2, img_y2 = image_bbox
        img_w = img_x2 - img_x1
        img_h = img_y2 - img_y1
        if img_w <= 0 or img_h <= 0:
            return

        if img_w <= canvas_w and img_h <= canvas_h:
            return

        mm_scale = self.minimap_max_size / max(img_w, img_h)
        mm_w = max(4, int(img_w * mm_scale))
        mm_h = max(4, int(img_h * mm_scale))

        view_x1 = self.canvas.canvasx(0)
        view_y1 = self.canvas.canvasy(0)
        view_x2 = self.canvas.canvasx(canvas_w)
        view_y2 = self.canvas.canvasy(canvas_h)

        mm_x1 = view_x2 - mm_w - self.minimap_padding
        mm_y1 = view_y2 - mm_h - self.minimap_padding
        mm_x2 = mm_x1 + mm_w
        mm_y2 = mm_y1 + mm_h

        self.canvas.create_rectangle(
            mm_x1, mm_y1, mm_x2, mm_y2,
            fill="#323232", outline="#747474", stipple="gray50",
            tags="minimap",
        )

        vp_l = max(img_x1, min(img_x2, view_x1))
        vp_t = max(img_y1, min(img_y2, view_y1))
        vp_r = max(img_x1, min(img_x2, view_x2))
        vp_b = max(img_y1, min(img_y2, view_y2))

        if vp_r <= vp_l or vp_b <= vp_t:
            return

        rx1 = mm_x1 + (vp_l - img_x1) * mm_scale
        ry1 = mm_y1 + (vp_t - img_y1) * mm_scale
        rx2 = mm_x1 + (vp_r - img_x1) * mm_scale
        ry2 = mm_y1 + (vp_b - img_y1) * mm_scale

        self.canvas.create_rectangle(
            rx1, ry1, rx2, ry2,
            outline="#f0f0f0", width=2,
            tags="minimap",
        )
        self.canvas.tag_raise("minimap")


    def update_scrollbars(self):
        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        image_bbox = self.canvas.bbox(self.canvas_image)

        if image_bbox:
            image_width = image_bbox[2] - image_bbox[0]
            image_height = image_bbox[3] - image_bbox[1]

            self.hbar.grid() if image_width > canvas_width else self.hbar.grid_remove()
            self.vbar.grid() if image_height > canvas_height else self.vbar.grid_remove()

    def show_context_menu(self, event):
        menu = tk.Menu(self.image_window, tearoff=0)
        
        def hk_label(name, default):
            if hasattr(self.controller, 'hotkeys_map'):
                key = self.controller.hotkeys_map.get(name, default)
                return key.replace("<", "").replace(">", "")
            return default

        # Vzájemně výlučné režimy — systémový „radio“ v menu (manual/none = žádná tečka).
        if not hasattr(self, "_ctx_fit_var"):
            self._ctx_fit_var = tk.StringVar(master=self.image_window)
        _m = self._view_fit_mode
        if _m in ("actual", "best_fit", "fit_width"):
            self._ctx_fit_var.set(_m)
        else:
            self._ctx_fit_var.set("")

        menu.add_radiobutton(
            label=f"Actual Size ({hk_label('image_actual_size', 'A')})",
            variable=self._ctx_fit_var,
            value="actual",
            command=self.actual_size,
        )
        menu.add_radiobutton(
            label=f"Best Fit ({hk_label('image_fit_best', 'B')})",
            variable=self._ctx_fit_var,
            value="best_fit",
            command=self.best_fit,
        )
        menu.add_radiobutton(
            label=f"Fit Width ({hk_label('image_fit_width', 'W')})",
            variable=self._ctx_fit_var,
            value="fit_width",
            command=self.fit_width,
        )
        menu.add_separator()
        menu.add_command(label=f"Previous Image ({hk_label('image_prev', 'Left')})", command=self.show_prev_image)
        menu.add_command(label=f"Next Image ({hk_label('image_next', 'Right')})", command=self.show_next_image)
        menu.add_separator()
        menu.add_command(label=f"Zoom In (+)", command=self.zoom_in)
        menu.add_command(label=f"Zoom Out (-)", command=self.zoom_out)
        menu.add_separator()
        menu.add_command(label=f"Rotate Left ({hk_label('image_rotate_left', 'L')})", command=self.rotate_left)
        menu.add_command(label=f"Rotate Right ({hk_label('image_rotate_right', 'R')})", command=self.rotate_right)
        menu.add_command(label=f"Flip Horizontal ({hk_label('image_flip_h', 'H')})", command=self.flip_horizontal)
        menu.add_command(label=f"Flip Vertical ({hk_label('image_flip_v', 'V')})", command=self.flip_vertical)
        menu.add_separator()
        if self._crop_active():
            menu.add_command(label="Cancel Crop (Esc)", command=self.exit_crop_mode)
        else:
            menu.add_command(
                label=f"Crop… ({hk_label('image_crop', 'X')})",
                command=self.enter_crop_mode,
            )
        menu.add_command(
            label=f"Resize Image… ({hk_label('image_resize', 'Ctrl+R')})",
            command=self.open_resize_dialog,
        )
        # Compare uses grid multi-select (controller.selected_thumbnails).
        _cmp_paths = []
        _open_cmp = getattr(self.controller, "open_image_compare", None)
        _sel_cmp = getattr(self.controller, "selected_image_paths_for_compare", None)
        if callable(_sel_cmp):
            try:
                _cmp_paths = _sel_cmp(None) or []
            except Exception:
                _cmp_paths = []
        if callable(_open_cmp) and len(_cmp_paths) >= 2:
            menu.add_command(
                label=f"Compare Images… ({hk_label('image_compare', 'Ctrl+Shift+C')})",
                command=lambda: _open_cmp(_cmp_paths),
            )
        menu.add_separator()
        menu.add_command(label=f"Save As ({hk_label('image_save', 'Ctrl+S')})", command=self.save_image_to_folder)
        menu.add_command(label=f"Copy ({hk_label('image_copy', 'Ctrl+C')})", command=self.copy_image_to_clipboard)
        menu.add_separator()
        _rating_path = getattr(self, "image_path", None)
        if _rating_path and os.path.isfile(_rating_path):
            append_rating_submenu(menu, self.controller, _rating_path)
            menu.add_separator()
        menu.add_command(label=f"Delete ({hk_label('image_delete', 'Del')})", command=self.delete_current_image)
        menu.add_separator()
        if callable(getattr(self.controller, "open_library", None)):
            menu.add_command(label="Open full app (Ctrl+L)", command=self.controller.open_library)
            menu.add_separator()
        menu.add_command(label="Toggle Fullscreen (F11)", command=self.toggle_fullscreen)
        
        menu.tk_popup(event.x_root, event.y_root)


# ---------------------------------------------------------------------------
# Public entry (Canvas default; GPU gated by use_gpu_viewer + bounded worker wait)
# ---------------------------------------------------------------------------

# Worker sets ``_pyglet_ready`` only after Pyglet submodules import; UI thread waits at most this long.
_GPU_STARTUP_TIMEOUT_S = 3.0


def create_image_viewer(parent, image_path, image_name, use_gpu_viewer: bool = False):
    """
    Open an image viewer. ``ImageViewerLegacy`` (Canvas) is the default.

    When ``use_gpu_viewer`` is True:
      * **Windows:** start the Pyglet worker under a bounded wait (``_GPU_STARTUP_TIMEOUT_S``);
        on timeout or error, return Legacy so hybrid-GPU laptops do not hang the UI thread.
      * **Other platforms:** construct ``ImageViewerGPU`` directly (same class still waits on
        the worker with its own timeout).
    """
    if not use_gpu_viewer:
        return ImageViewerLegacy(parent, image_path, image_name)

    logging.info("[ImageViewer] Attempting GPU startup (Timeout 3s)...")

    if sys.platform == "win32":
        try:
            _ensure_pyglet_worker()
        except Exception:
            logging.exception("[ImageViewer] GPU worker start failed; using Canvas viewer.")
            return ImageViewerLegacy(parent, image_path, image_name)

        if not _pyglet_ready.wait(timeout=_GPU_STARTUP_TIMEOUT_S):
            logging.warning(
                "[ImageViewer] GPU worker not ready within %.1fs; using Canvas viewer.",
                _GPU_STARTUP_TIMEOUT_S,
            )
            return ImageViewerLegacy(parent, image_path, image_name)

        try:
            return ImageViewerGPU(
                parent,
                image_path,
                image_name,
                gpu_init_timeout=_GPU_STARTUP_TIMEOUT_S,
                strict_gpu_init=True,
            )
        except Exception:
            logging.exception("[ImageViewer] GPU viewer failed; using Canvas viewer.")
            return ImageViewerLegacy(parent, image_path, image_name)

    return ImageViewerGPU(
        parent,
        image_path,
        image_name,
        gpu_init_timeout=_GPU_STARTUP_TIMEOUT_S,
        strict_gpu_init=True,
    )


def _use_gpu_from_parent(parent) -> bool:
    """Read GPU flag: ``use_gpu_viewer`` if set, else ``image_viewer_use_pyglet`` (settings.json)."""
    if getattr(parent, "use_gpu_viewer", None) is not None:
        return bool(getattr(parent, "use_gpu_viewer"))
    return bool(getattr(parent, "image_viewer_use_pyglet", False))


def ImageViewer(parent, image_path, image_name):
    """Backward-compatible: uses controller preference (``image_viewer_use_pyglet`` / ``use_gpu_viewer``)."""
    return create_image_viewer(parent, image_path, image_name, _use_gpu_from_parent(parent))


# --- Pyglet GPU (lazy worker) ---
_MAX_GPU_TEX = 8192
_FRAME_TIME  = 1.0 / 60

# Pyglet image window: double-click LMB → fit / center (same intent as ``center_image``)
_LMB_DOUBLE_MAX_S = 0.35
_LMB_DOUBLE_MAX_DIST = 22  # px between presses

# Placeholder — replaced by the worker thread's import.
pyglet = None

# -------------------------------------------------------------------
# Pyglet worker (started on first GPU viewer only)
# -------------------------------------------------------------------

_pyglet_cmd_queue = _Q.Queue()   # (callable, result_event | None)
_pyglet_ready     = threading.Event()
_pyglet_worker_lock = threading.Lock()
_pyglet_worker_started = False
_pyglet_active: list = []        # List[ImageViewerGPU] — only the worker touches this


def _ensure_pyglet_worker():
    """Start the Pyglet thread once; avoids importing GL for Canvas-only sessions."""
    global _pyglet_worker_started
    with _pyglet_worker_lock:
        if _pyglet_worker_started:
            return
        _pyglet_worker_started = True
        logging.info("[pyglet worker] starting background thread")
        threading.Thread(
            target=_run_pyglet_worker,
            daemon=True,
            name="pyglet-worker",
        ).start()


def _run_pyglet_worker():
    """
    Permanent daemon thread.
    Imports pyglet HERE so pyglet.app registers this thread as the
    Win32 event-loop owner.  All window creation, GL uploads, rendering
    and dispatch_events calls happen in this single thread.
    """
    global pyglet
    try:
        import pyglet  # noqa  ← registers this thread with pyglet.app

        # Must run immediately after ``import pyglet``, before any subpackages (pyglet docs).
        pyglet.options['shadow_window'] = False
        if sys.platform == 'win32':
            # Avoid slow DirectWrite font path on some hybrid-GPU setups.
            pyglet.options['win32_gdi_font'] = True
        pyglet.options['headless'] = False

        # CRITICAL FIX: Disable automatic garbage collection of GL objects.
        # In a multi-threaded app (Tkinter + Pyglet), the GC often runs on the
        # wrong thread, causing access violations when trying to delete GL buffers.
        pyglet.options['garbage_collect'] = False

        try:
            import pyglet.app  # noqa  ← explicit: fixes the thread-ID check
            import pyglet.window  # noqa
            import pyglet.sprite  # noqa
            import pyglet.text  # noqa
            import pyglet.graphics  # noqa
            import pyglet.image  # noqa
            import pyglet.canvas  # noqa  pyglet 2.x: display API lives in canvas, not pyglet.display
            import pyglet.shapes  # noqa  minimap + ensure shaders load on worker thread
        except Exception as sub_exc:
            logging.exception(
                "[pyglet worker] submodule import failed (window/display/etc.): %s",
                sub_exc,
            )
            raise

        # Win32: dispatch_events() calls platform_event_loop.start() every frame; the
        # stock implementation calls timeBeginPeriod each time — avoid stacking that.
        if sys.platform == "win32":
            try:
                from pyglet.app.win32 import Win32EventLoop
                from pyglet.libs.win32 import _kernel32

                _win32_time_period_done = [False]

                def _patched_win32_loop_start(self):
                    if _kernel32.GetCurrentThreadId() != self._event_thread:
                        raise RuntimeError(
                            "EventLoop.run() must be called from the same "
                            "thread that imports pyglet.app"
                        )
                    self._timer_func = None
                    if not _win32_time_period_done[0]:
                        self._winmm.timeBeginPeriod(self._timer_precision)
                        _win32_time_period_done[0] = True

                Win32EventLoop.start = _patched_win32_loop_start
            except Exception as exc:
                logging.info("[pyglet worker] Win32EventLoop.start patch skipped: %s", exc)

        # After successful imports only — ``_pyglet_ready.wait()`` then reflects real init progress.
        _pyglet_ready.set()

        while True:
            t0 = time.perf_counter()

            # ---- process commands from Tkinter threads -------------------------
            while True:
                try:
                    fn, result_ev = _pyglet_cmd_queue.get_nowait()
                    try:
                        fn()
                    except Exception as e:
                        logging.warning(f"[pyglet worker] command error: {e}")
                    finally:
                        if result_ev is not None:
                            result_ev.set()
                except _Q.Empty:
                    break

            # ---- render all active viewers ------------------------------------
            dead = []
            for v in list(_pyglet_active):
                if not v._running or v.window is None:
                    dead.append(v)
                    continue
                try:
                    # Pyglet 2: use Window.draw(dt) so context, on_draw, on_refresh, flip
                    # run in the order the library expects (manual _on_draw + flip was flaky).
                    v.window.dispatch_events()
                    if v._running:
                        v.window.draw(0.0)
                except Exception as e:
                    logging.warning(
                        f"[pyglet worker] render error for {v.image_name!r}: {e}")
                    dead.append(v)

            for v in dead:
                if v in _pyglet_active:
                    _pyglet_active.remove(v)
                if v.window is not None:
                    try:
                        v.window.close()
                    except Exception:
                        pass

            # ---- frame-rate cap -----------------------------------------------
            elapsed   = time.perf_counter() - t0
            remaining = _FRAME_TIME - elapsed
            if remaining > 0.001:
                time.sleep(remaining)

    except Exception as e:
        logging.exception(
            "[pyglet worker] fatal startup or run-loop error: %s", e
        )


# ---------------------------------------------------------------------------
# Compat shim — existing code calls  viewer.image_window.destroy()
# ---------------------------------------------------------------------------

class _WindowCompat:
    def __init__(self, viewer: "ImageViewerGPU"):
        self._v = viewer

    def destroy(self):
        self._v._do_close()   # thread-safe (just sets a flag)

    def after(self, ms, cb):
        return self._v.parent.after(ms, cb)

    def after_cancel(self, tid):
        self._v.parent.after_cancel(tid)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class ImageViewerGPU:
    """
    Optional OpenGL / Pyglet image window (worker-thread GL).

    ``ImageViewerGPU.__init__`` returns quickly (non-blocking).  The pyglet window
    appears once the worker thread finishes GL initialisation — on the very
    first open this may take a few seconds; subsequent opens are instant
    because the GL driver is already loaded.
    """

    # Middle preset matches app surface_low (VideoPlayer / DWM border family).
    _SURFACE_LOW_F = (26 / 255.0, 28 / 255.0, 30 / 255.0, 1.0)
    _BG_COLORS = [
        (0.0, 0.0, 0.0, 1.0),
        _SURFACE_LOW_F,
        (1.0, 1.0, 1.0, 1.0),
    ]
    _BG_HEX = ["black", "#1A1C1E", "white"]
    _HUD_ON_SURFACE = (176, 179, 184, 230)  # #B0B3B8
    _ZOOM_ON_WIDGET = (240, 240, 240, 235)

    # ------------------------------------------------------------------
    # Construction  (main / Tkinter thread — non-blocking)
    # ------------------------------------------------------------------

    def __init__(
        self,
        parent,
        image_path,
        image_name,
        gpu_init_timeout=30.0,
        strict_gpu_init=False,
    ):
        # Tk thread: load image and screen size; pyglet window is created on the worker.
        # strict_gpu_init: if worker is not ready within gpu_init_timeout, raise (for router fallback).
        self.parent     = parent
        self.controller = parent
        self.image_path = image_path
        self.image_name = image_name
        self.is_fullscreen = False

        # Load PIL image (no GL, safe in any thread)
        raw = load_pil_image(image_path)
        if raw.mode not in ('RGB', 'RGBA'):
            raw = raw.convert('RGBA')
        self.original_image = raw
        self._img_w, self._img_h = raw.size

        # View state — plain floats, GIL-safe
        self.zoom  = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        # Stejné významy jako ImageViewerLegacy._view_fit_mode; výchozí = best fit (worker ho hned aplikuje).
        self._view_fit_mode = "best_fit"

        # UI state
        self.bg_index  = 0
        self.show_info = True
        self._panning  = False

        # HUD cache
        self._hud_cache_path = None
        self._hud_index_str  = ""

        # Defaults only; worker re-queries screeninfo with a timeout in _worker_create
        # so the Tk thread never blocks on a stuck WMI/GDI enumeration.
        self.screen_w = 1200
        self.screen_h = 800

        # Compat stubs
        self._hq_timer             = None
        self._zoom_timer           = None
        self._pan_timer            = None
        self._viewport_crop_active = False
        self.proxy_image           = None
        self.proxy_scale           = 1.0

        # Pyglet objects (created by worker thread)
        self.window      = None
        self._batch      = None
        self._sprite     = None
        self._hud_label  = None
        self._hud_shadow = None
        self._hud_rating_label = None
        self._hud_rating_shadow = None
        self._zoom_label = None
        self._zoom_shadow = None
        self._keys       = None
        self._mm_bg_shape = None   # minimap: gray background rect
        self._mm_vp_shape = None   # minimap: viewport indicator (box outline)

        self._lmb_click_t = 0.0
        self._lmb_click_xy = None  # (x, y) in window coords, last LMB

        self._running = True
        self._resize_dialog = None
        self._image_dirty = False

        _ensure_pyglet_worker()

        # Set in worker only after Pyglet submodules import successfully.
        if not _pyglet_ready.wait(timeout=gpu_init_timeout):
            if strict_gpu_init:
                self._running = False
                logging.error(
                    "[ImageViewer] pyglet worker not ready within %.1fs (strict GPU init)",
                    gpu_init_timeout,
                )
                raise RuntimeError("Pyglet worker did not become ready in time")
            logging.warning(
                "[ImageViewer] pyglet worker ready event not set within %.1fs; "
                "queueing window create anyway (worker may still be starting)",
                gpu_init_timeout,
            )

        # Schedule window creation in the worker thread
        _pyglet_cmd_queue.put((self._worker_create, None))

        self.image_window = _WindowCompat(self)

    # ------------------------------------------------------------------
    # zoom_factor alias
    # ------------------------------------------------------------------

    @property
    def zoom_factor(self):
        return self.zoom

    @zoom_factor.setter
    def zoom_factor(self, v):
        self.zoom = float(v)

    # ==================================================================
    # PYGLET WORKER THREAD — all methods below are called from there
    # ==================================================================

    def _worker_create(self):
        """Called by the pyglet worker thread to create the window + GL resources."""
        # Worker-only: OpenGL window and resources; failures are logged and skipped.
        if not self._running:
            return

        # screeninfo on a worker thread can interact badly with WMI + Tk; keep it synchronous.
        try:
            mons = get_monitors()
            if mons:
                self.screen_w = mons[0].width
                self.screen_h = mons[0].height
            else:
                logging.warning("[ImageViewer] get_monitors returned empty; using 1200x800")
                self.screen_w, self.screen_h = 1200, 800
        except Exception as exc:
            logging.warning(
                "[ImageViewer] get_monitors failed (%s); using 1200x800", exc
            )
            self.screen_w, self.screen_h = 1200, 800

        win_w = min(self._img_w, self.screen_w - 100)
        win_h = min(self._img_h, self.screen_h - 100)

        try:
            self.window = pyglet.window.Window(
                width=win_w, height=win_h,
                caption=self.image_name,
                resizable=True,
                vsync=False,
            )
        except Exception as exc:
            logging.exception(
                "[ImageViewer] pyglet.window.Window failed (OpenGL/display): %s",
                exc,
            )
            self._running = False
            return

        if sys.platform == "win32":
            hwnd = getattr(self.window, "_hwnd", None)
            if hwnd:
                try:
                    import ctypes

                    DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                    use_dark = ctypes.c_int(1)
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd,
                        DWMWA_USE_IMMERSIVE_DARK_MODE,
                        ctypes.byref(use_dark),
                        ctypes.sizeof(use_dark),
                    )
                    logging.info(
                        "[ImageViewer] DwmSetWindowAttribute(USE_IMMERSIVE_DARK_MODE) on 0x%x OK.",
                        hwnd,
                    )
                except Exception as exc:
                    logging.info("[ImageViewer] DWM immersive dark failed: %s", exc)
                try:
                    import pywinstyles

                    pywinstyles.apply_style(hwnd, "dark")
                    pywinstyles.change_header_color(hwnd, "#131313")
                    pywinstyles.change_border_color(hwnd, "#1A1C1E")
                    logging.info("[ImageViewer] pywinstyles dark caption chrome applied.")
                except ImportError:
                    logging.info(
                        "[ImageViewer] pywinstyles not installed — DWM dark only for caption."
                    )
                except Exception as exc:
                    logging.info("[ImageViewer] pywinstyles chrome failed: %s", exc)

        self._batch = pyglet.graphics.Batch()
        self._upload_texture(self.original_image)

        hud_group = pyglet.graphics.Group(order=10)
        self._hud_shadow = pyglet.text.Label(
            '', font_name='Segoe UI', font_size=10, weight='bold',
            x=11, y=win_h - 21,
            color=(0, 0, 0, 200),
            batch=self._batch, group=hud_group,
        )
        self._hud_label = pyglet.text.Label(
            '', font_name='Segoe UI', font_size=10, weight='bold',
            x=10, y=win_h - 20,
            color=self._HUD_ON_SURFACE,
            batch=self._batch, group=hud_group,
        )
        self._hud_rating_shadow = pyglet.text.Label(
            '', font_name='Segoe UI', font_size=10, weight='bold',
            x=11, y=win_h - 21,
            color=(0, 0, 0, 200),
            batch=self._batch, group=hud_group,
        )
        self._hud_rating_label = pyglet.text.Label(
            '', font_name='Segoe UI', font_size=10, weight='bold',
            x=10, y=win_h - 20,
            color=self._HUD_ON_SURFACE,
            batch=self._batch, group=hud_group,
        )
        self._zoom_shadow = pyglet.text.Label(
            '', font_name='Segoe UI', font_size=10, weight='bold',
            x=win_w - 11, y=win_h - 21,
            anchor_x='right',
            color=(0, 0, 0, 200),
            batch=self._batch, group=hud_group,
        )
        self._zoom_label = pyglet.text.Label(
            '', font_name='Segoe UI', font_size=10, weight='bold',
            x=win_w - 12, y=20,
            anchor_x='right',
            color=self._ZOOM_ON_WIDGET,
            batch=self._batch, group=hud_group,
        )

        self._keys = pyglet.window.key.KeyStateHandler()
        self.window.push_handlers(self._keys)
        self.window.push_handlers(
            on_draw          = self._on_draw,
            on_resize        = self._on_resize,
            on_mouse_scroll  = self._on_mouse_scroll,
            on_mouse_press   = self._on_mouse_press,
            on_mouse_release = self._on_mouse_release,
            on_mouse_drag    = self._on_mouse_drag,
            on_key_press     = self._on_key_press,
            on_close         = self._on_close,
        )

        self._apply_viewport_fit(win_w, win_h)
        self._update_hud()
        self._build_hotkey_map()

        # Ensure viewport / projection match initial client size (Pyglet 2 UBO path).
        self.window.switch_to()
        self._on_resize(win_w, win_h)

        # Minimap shapes — reused every frame to avoid per-frame GPU allocations
        self._mm_bg_shape = pyglet.shapes.Rectangle(0, 0, 1, 1, color=(50, 50, 50))
        self._mm_bg_shape.opacity = 76   # ~30 %
        self._mm_vp_shape = pyglet.shapes.Box(0, 0, 1, 1, thickness=1,
                                               color=(210, 210, 210))
        self._mm_vp_shape.opacity = 200

        _pyglet_active.append(self)

        if bool(getattr(self.controller, "image_viewer_open_fullscreen", True)):
            self._do_toggle_fullscreen()
            self._do_best_fit()

    # ------------------------------------------------------------------
    # Hotkey map  (worker thread — built once after pyglet is ready)
    # ------------------------------------------------------------------

    def _build_hotkey_map(self):
        """
        Translates hotkeys_map (Tkinter key strings) to Pyglet key symbols.
        Result stored in self._hotkey_sym_map: {(symbol, ctrl): action_name}
        Called once from _worker_create after pyglet is imported.
        """
        k = pyglet.window.key
        # Map of Tkinter key strings → (pyglet_symbol, requires_ctrl)
        _tk_to_sym = {
            '<Right>':     (k.RIGHT,  False),
            '<Left>':      (k.LEFT,   False),
            '<space>':     (k.SPACE,  False),
            '<Space>':     (k.SPACE,  False),
            '<Escape>':    (k.ESCAPE, False),
            '<Delete>':    (k.DELETE, False),
            '<F11>':       (k.F11,    False),
            '<Control-c>': (k.C,      True),
            '<Control-s>': (k.S,      True),
            '<Control-r>': (k.R,      True),
            '<Control-R>': (k.R,      True),
            'a': (k.A, False), 'b': (k.B, False), 'c': (k.C, False), 'i': (k.I, False),
            'w': (k.W, False), '+': (k.PLUS,  False), '-': (k.MINUS, False),
            'l': (k.L, False), 'r': (k.R,     False), 'h': (k.H,     False),
            'v': (k.V, False), 'f': (k.F,     False), 'x': (k.X,     False),
        }
        hmap = getattr(self.controller, 'hotkeys_map', {})
        self._hotkey_sym_map: dict = {}
        for action, tk_key in hmap.items():
            if tk_key in _tk_to_sym:
                self._hotkey_sym_map[_tk_to_sym[tk_key]] = action

        # Space is always an additional alias for image_next (no conflict with
        # Tkinter play_pause — Pyglet captures keys only when its window is focused)
        self._hotkey_sym_map[(k.SPACE, False)] = 'image_next'

    # ------------------------------------------------------------------
    # Schedule helper  (any thread → worker)
    # ------------------------------------------------------------------

    def _schedule_pyglet(self, fn, *args, **kwargs):
        """Thread-safe: run fn(*args, **kwargs) in the pyglet worker thread."""
        def _cmd():
            fn(*args, **kwargs)
        _pyglet_cmd_queue.put((_cmd, None))

    # ------------------------------------------------------------------
    # GL helpers  (worker thread)
    # ------------------------------------------------------------------

    def _upload_texture(self, pil_img: PILImage.Image):
        if pil_img.mode != 'RGBA':
            pil_img = pil_img.convert('RGBA')
        W, H = pil_img.size
        if max(W, H) > _MAX_GPU_TEX:
            s       = _MAX_GPU_TEX / max(W, H)
            pil_img = pil_img.resize(
                (max(1, int(W * s)), max(1, int(H * s))), PILImage.LANCZOS,
            )
            W, H = pil_img.size
            logging.warning(f"[ImageViewer] downsampled to {W}×{H} for GPU limit")
        raw      = pil_img.tobytes()
        img_data = pyglet.image.ImageData(W, H, 'RGBA', raw, pitch=-W * 4)
        texture  = img_data.get_texture()
        if self._sprite is None:
            self._sprite = pyglet.sprite.Sprite(
                texture, x=0, y=0,
                batch=self._batch,
                group=pyglet.graphics.Group(order=0),
            )
        else:
            self._sprite.image = texture
        self._img_w = W
        self._img_h = H

    def _apply_best_fit(self, win_w=None, win_h=None):
        if win_w is None: win_w = self.window.width
        if win_h is None: win_h = self.window.height
        self.zoom  = min(win_w / self._img_w, win_h / self._img_h)
        self.pan_x = (win_w  - self._img_w * self.zoom) / 2
        self.pan_y = (win_h - self._img_h * self.zoom) / 2

    def _apply_viewport_fit(self, win_w=None, win_h=None):
        """Nastaví zoom/pan podle uloženého režimu a velikosti okna."""
        if win_w is None:
            win_w = self.window.width
        if win_h is None:
            win_h = self.window.height
        if self._img_w <= 0 or self._img_h <= 0:
            return
        mode = getattr(self, "_view_fit_mode", "best_fit")
        if mode == "best_fit":
            self._apply_best_fit(win_w, win_h)
        elif mode == "fit_width":
            self.zoom = win_w / self._img_w
            self.pan_x = 0.0
            self.pan_y = (win_h - self._img_h * self.zoom) / 2
        elif mode == "actual":
            self.zoom = 1.0
            self.pan_x = (win_w - self._img_w) / 2
            self.pan_y = (win_h - self._img_h) / 2
        elif mode == "manual":
            self.pan_x = (win_w - self._img_w * self.zoom) / 2
            self.pan_y = (win_h - self._img_h * self.zoom) / 2
        else:
            self._apply_best_fit(win_w, win_h)

    # ------------------------------------------------------------------
    # Render  (worker thread)
    # ------------------------------------------------------------------

    def _on_draw(self):
        from pyglet.gl import glClearColor
        r, g, b, a = self._BG_COLORS[self.bg_index]
        glClearColor(r, g, b, a)
        self.window.clear()
        self._sprite.update(
            x     = self.pan_x,
            y     = self.window.height - self.pan_y - self._img_h * self.zoom,
            scale = self.zoom,
        )
        self._hud_label.y  = self.window.height - 20
        self._hud_shadow.y = self.window.height - 21
        if self._hud_rating_label is not None:
            self._hud_rating_label.y = self.window.height - 20
            self._hud_rating_shadow.y = self.window.height - 21
        zoom_x, zoom_y = self._zoom_overlay_position()
        self._zoom_label.x = zoom_x
        self._zoom_label.y = zoom_y
        self._zoom_shadow.x = zoom_x + 1
        self._zoom_shadow.y = zoom_y - 1
        self._batch.draw()
        self._draw_minimap()

    def _zoom_overlay_position(self):
        win_w = self.window.width
        win_h = self.window.height
        y = 20
        if self._img_w > 0 and self._img_h > 0:
            if self._img_w * self.zoom > win_w or self._img_h * self.zoom > win_h:
                mm_scale = 150 / max(self._img_w, self._img_h)
                mm_h = max(4, int(self._img_h * mm_scale))
                y = 12 + mm_h + 8
        return win_w - 12, y

    def _on_resize(self, width, height):
        from pyglet.gl import glViewport
        glViewport(0, 0, width, height)
        self._apply_viewport_fit(width, height)
        self._update_hud()

    # ------------------------------------------------------------------
    # Minimap  (worker thread)
    # ------------------------------------------------------------------

    def _draw_minimap(self):
        """
        Draws a small navigator in the bottom-right corner when the image
        is zoomed in beyond the viewport.
        - Gray semi-transparent rectangle = full image
        - White box outline = currently visible area (viewport)
        """
        if self._mm_bg_shape is None:
            return

        win_w = self.window.width
        win_h = self.window.height
        img_w = self._img_w
        img_h = self._img_h

        # Hide when the full image fits inside the window — no need for a map
        if img_w * self.zoom <= win_w and img_h * self.zoom <= win_h:
            return

        MM_MAX  = 150   # max minimap dimension in px
        PADDING = 12    # distance from window edge

        # Scale the minimap to preserve the image aspect ratio
        mm_scale = MM_MAX / max(img_w, img_h)
        mm_w = max(4, int(img_w * mm_scale))
        mm_h = max(4, int(img_h * mm_scale))
        mm_x = win_w - mm_w - PADDING
        mm_y = PADDING  # Pyglet origin is bottom-left

        # --- Background (full image) ---
        self._mm_bg_shape.x      = mm_x
        self._mm_bg_shape.y      = mm_y
        self._mm_bg_shape.width  = mm_w
        self._mm_bg_shape.height = mm_h
        self._mm_bg_shape.draw()

        # --- Viewport rect in image pixel coords ---
        # sprite bottom-left y in window coords (Pyglet bottom-left origin)
        sprite_y = win_h - self.pan_y - img_h * self.zoom

        vp_l = max(0.0, -self.pan_x / self.zoom)
        vp_r = min(float(img_w), (win_w - self.pan_x) / self.zoom)
        vp_b = max(0.0, -sprite_y / self.zoom)
        vp_t = min(float(img_h), (win_h - sprite_y) / self.zoom)

        # Convert to minimap coords
        rx = mm_x + vp_l * mm_scale
        ry = mm_y + vp_b * mm_scale
        rw = max(2.0, (vp_r - vp_l) * mm_scale)
        rh = max(2.0, (vp_t - vp_b) * mm_scale)

        self._mm_vp_shape.x      = rx
        self._mm_vp_shape.y      = ry
        self._mm_vp_shape.width  = rw
        self._mm_vp_shape.height = rh
        self._mm_vp_shape.draw()

    # ------------------------------------------------------------------
    # Input  (worker thread — called from dispatch_events)
    # ------------------------------------------------------------------

    def _on_mouse_scroll(self, x, y, scroll_x, scroll_y):
        k     = pyglet.window.key
        ctrl  = self._keys[k.LCTRL]  or self._keys[k.RCTRL]
        shift = self._keys[k.LSHIFT] or self._keys[k.RSHIFT]
        if ctrl or shift:
            self._view_fit_mode = "manual"
            mx       = float(x)
            my       = float(self.window.height - y)
            new_zoom = max(0.01, min(50.0, self.zoom * (1.1 if scroll_y > 0 else 0.9)))
            ratio    = new_zoom / self.zoom
            self.pan_x = mx - (mx - self.pan_x) * ratio
            self.pan_y = my - (my - self.pan_y) * ratio
            self.zoom  = new_zoom
        else:
            self.pan_y -= scroll_y * 60
        self._update_hud()

    def _on_mouse_press(self, x, y, button, modifiers):
        if button == pyglet.window.mouse.LEFT:
            # LMB on Pyglet never reaches Tk bind_all — close flat menu on main thread
            self.parent.after(0, CTkFlatContextMenu.dismiss_current)
            now = time.perf_counter()
            prev_t = self._lmb_click_t
            prev_xy = self._lmb_click_xy
            if (
                prev_xy is not None
                and (now - prev_t) <= _LMB_DOUBLE_MAX_S
                and (x - prev_xy[0]) ** 2 + (y - prev_xy[1]) ** 2
                <= _LMB_DOUBLE_MAX_DIST**2
            ):
                self._lmb_click_t = 0.0
                self._lmb_click_xy = None
                self._do_best_fit()
                self._update_hud()
            else:
                self._lmb_click_t = now
                self._lmb_click_xy = (float(x), float(y))
        elif button == pyglet.window.mouse.MIDDLE:
            self._panning = True

    def _on_mouse_release(self, x, y, button, modifiers):
        if button == pyglet.window.mouse.MIDDLE:
            self._panning = False
        elif button == pyglet.window.mouse.RIGHT:
            wx, wy   = self.window.get_location()
            screen_x = int(wx + x)
            screen_y = int(wy + (self.window.height - y))
            # Replace menu at new cursor position (tk_popup dismisses previous)
            self.parent.after(0, lambda: self._show_context_menu(screen_x, screen_y))

    def _on_mouse_drag(self, x, y, dx, dy, buttons, modifiers):
        if buttons & pyglet.window.mouse.MIDDLE:
            self.pan_x += dx
            self.pan_y -= dy

    def _on_key_press(self, symbol, modifiers):
        k    = pyglet.window.key
        ctrl = bool(modifiers & k.MOD_CTRL)

        # Plain F is a viewer-local fullscreen alias; Ctrl+F stays reserved for Search.
        if not ctrl and symbol in (k.F,):
            self._do_toggle_fullscreen()
            return
        if ctrl and symbol in (k.L,) and callable(getattr(self.controller, "open_library", None)):
            self.parent.after(0, self.controller.open_library)
            return

        # Also handle zoom keys that have no simple Tkinter string equivalent
        if symbol in (k.PLUS, k.EQUAL, k.NUM_ADD):
            self._do_zoom_in(); return
        if symbol in (k.MINUS, k.NUM_SUBTRACT):
            self._do_zoom_out(); return

        # Look up action from hotkeys_map (with ctrl variant first, then without)
        action = (self._hotkey_sym_map.get((symbol, ctrl))
                  or self._hotkey_sym_map.get((symbol, False)))
        if not action:
            return

        _main = self.parent.after  # shortcut for scheduling on Tkinter thread

        if   action == 'image_next':         _main(0, self.show_next_image)
        elif action == 'image_prev':         _main(0, self.show_prev_image)
        elif action == 'close_window':       self._do_close()
        elif action == 'image_delete':       _main(0, self.delete_current_image)
        elif action in ('toggle_fullscreen', 'image_fullscreen'):
            self._do_toggle_fullscreen()
        elif action == 'image_actual_size':  self._do_actual_size()
        elif action == 'image_toggle_bg':    self._do_toggle_background()
        elif action == 'image_toggle_info':  self._do_toggle_info()
        elif action == 'image_fit_best':     self._do_best_fit()
        elif action == 'image_fit_width':    self._do_fit_width()
        elif action == 'image_zoom_in':      self._do_zoom_in()
        elif action == 'image_zoom_out':     self._do_zoom_out()
        elif action == 'image_rotate_left':  self._do_rotate_left()
        elif action == 'image_rotate_right': self._do_rotate_right()
        elif action == 'image_flip_h':       self._do_flip_h()
        elif action == 'image_flip_v':       self._do_flip_v()
        elif action == 'image_copy':         _main(0, self.copy_image_to_clipboard)
        elif action == 'image_save':         _main(0, self.save_image_to_folder)
        elif action == 'image_resize':       _main(0, self.open_resize_dialog)
        elif action == 'image_crop':         pass  # crop HUD is Legacy-only for now

    def _on_close(self):
        self._do_close()
        return pyglet.event.EVENT_HANDLED

    def _do_close(self):
        """Thread-safe: signal the worker to remove + close this viewer."""
        self._running = False

    # ------------------------------------------------------------------
    # Action implementations  (worker thread)
    # ------------------------------------------------------------------

    def _do_load_image(self, path, name):
        self.image_path = path
        self.image_name = name
        try:
            img = load_pil_image(path)
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGBA')
            self.original_image = img
            self._upload_texture(img)
            self._apply_viewport_fit()
            self.window.set_caption(name)
            self._hud_cache_path = None
            self._image_dirty = False
            self._update_hud()
        except Exception as e:
            logging.error(f"[ImageViewer] Failed to load {name}: {e}")

    def _do_zoom_in(self):
        self._view_fit_mode = "manual"
        self.zoom = min(50.0, self.zoom * 1.1);  self._update_hud()

    def _do_zoom_out(self):
        self._view_fit_mode = "manual"
        self.zoom = max(0.01, self.zoom * 0.9);  self._update_hud()

    def _do_actual_size(self):
        self._view_fit_mode = "actual"
        self._apply_viewport_fit()
        self._update_hud()

    def _do_best_fit(self):
        self._view_fit_mode = "best_fit"
        self._apply_viewport_fit()
        self._update_hud()

    def _do_fit_width(self):
        self._view_fit_mode = "fit_width"
        self._apply_viewport_fit()
        self._update_hud()

    def _do_rotate_left(self):
        self.original_image = self.original_image.rotate(90, expand=True)
        self._upload_texture(self.original_image)
        self._apply_viewport_fit()
        self._update_hud()

    def _do_rotate_right(self):
        self.original_image = self.original_image.rotate(-90, expand=True)
        self._upload_texture(self.original_image)
        self._apply_viewport_fit()
        self._update_hud()

    def _do_flip_h(self):
        self.original_image = self.original_image.transpose(PILImage.FLIP_LEFT_RIGHT)
        self._upload_texture(self.original_image);  self._update_hud()

    def _do_flip_v(self):
        self.original_image = self.original_image.transpose(PILImage.FLIP_TOP_BOTTOM)
        self._upload_texture(self.original_image);  self._update_hud()

    def _do_resize(self, new_width, new_height, resampling_filter):
        w = max(1, int(new_width))
        h = max(1, int(new_height))
        filt = resampling_filter if resampling_filter is not None else PILImage.LANCZOS
        self.original_image = self.original_image.resize((w, h), filt)
        self._img_w, self._img_h = self.original_image.size
        self._image_dirty = True
        self._upload_texture(self.original_image)
        self._apply_viewport_fit()
        self._update_hud()

    def _do_toggle_fullscreen(self):
        if not self.is_fullscreen:
            wx, wy = self.window.get_location()
            cx, cy = wx + self.window.width // 2, wy + self.window.height // 2
            target = None
            for s in pyglet.canvas.get_display().get_screens():
                if s.x <= cx < s.x + s.width and s.y <= cy < s.y + s.height:
                    target = s
                    break
            self.window.set_fullscreen(True, screen=target)
            self.is_fullscreen = True
        else:
            self.window.set_fullscreen(False)
            self.is_fullscreen = False

    def _do_toggle_background(self):
        self.bg_index = (self.bg_index + 1) % len(self._BG_COLORS)
        self._update_hud()

    def _do_toggle_info(self):
        self.show_info = not self.show_info
        self._update_hud()

    # ------------------------------------------------------------------
    # Public API — safe to call from any thread
    # ------------------------------------------------------------------

    def load_image(self, path, name):
        self._abandon_image_edits()
        self._schedule_pyglet(self._do_load_image, path, name)

    def skip(self, direction):
        if not self._confirm_leave_edit_for_navigation():
            return
        try:
            files = [f for f in self.controller.video_files
                     if f['path'].lower().endswith(IMAGE_FORMATS)]
            idx   = next((i for i, f in enumerate(files)
                          if f['path'] == self.image_path), None)
            if idx is None:
                return
            nf = files[(idx + direction) % len(files)]
            self.load_image(nf['path'], nf['name'])
        except Exception as e:
            logging.warning(f"[ImageViewer] skip error: {e}")

    def show_next_image(self):  self.skip(1)
    def show_prev_image(self):  self.skip(-1)

    def zoom_in(self, event=None):       self._schedule_pyglet(self._do_zoom_in)
    def zoom_out(self, event=None):      self._schedule_pyglet(self._do_zoom_out)
    def actual_size(self):               self._schedule_pyglet(self._do_actual_size)
    def best_fit(self):                  self._schedule_pyglet(self._do_best_fit)
    def fit_width(self):                 self._schedule_pyglet(self._do_fit_width)
    def rotate_left(self):               self._schedule_pyglet(self._do_rotate_left)
    def rotate_right(self):              self._schedule_pyglet(self._do_rotate_right)
    def flip_horizontal(self):           self._schedule_pyglet(self._do_flip_h)
    def flip_vertical(self):             self._schedule_pyglet(self._do_flip_v)
    def toggle_fullscreen(self, e=None): self._schedule_pyglet(self._do_toggle_fullscreen)
    def toggle_background(self, e=None): self._schedule_pyglet(self._do_toggle_background)
    def toggle_info(self, e=None):       self._schedule_pyglet(self._do_toggle_info)

    def _resize_dialog_open(self) -> bool:
        dlg = getattr(self, "_resize_dialog", None)
        if dlg is None:
            return False
        try:
            return bool(dlg.winfo_exists())
        except Exception:
            self._resize_dialog = None
            return False

    def _active_edit_processes(self) -> list:
        names = []
        if self._resize_dialog_open() or getattr(self, "_image_dirty", False):
            names.append("Resize")
        return names

    def _abandon_image_edits(self):
        if self._resize_dialog_open():
            try:
                self._resize_dialog.force_close()
            except Exception:
                pass
            self._resize_dialog = None
        self._image_dirty = False

    def _confirm_leave_edit_for_navigation(self) -> bool:
        processes = self._active_edit_processes()
        if not processes:
            return True
        if not confirm_leave_image_edit(self.parent, processes):
            return False
        self._abandon_image_edits()
        return True

    def open_resize_dialog(self):
        """Show resize dialog on the Tk thread; apply runs on the pyglet worker."""
        if self._resize_dialog_open():
            try:
                self._resize_dialog.lift()
                self._resize_dialog.focus_force()
            except Exception:
                pass
            return
        w, h = self.original_image.size
        dlg = open_resize_image_dialog(
            self.parent,
            orig_width=w,
            orig_height=h,
            on_apply=self.resize_image,
        )
        self._resize_dialog = dlg

        def _clear_ref(_event=None):
            if getattr(self, "_resize_dialog", None) is dlg:
                self._resize_dialog = None

        try:
            dlg.bind("<Destroy>", _clear_ref)
        except Exception:
            pass

    def resize_image(self, new_width, new_height, resampling_filter=PILImage.LANCZOS):
        self._schedule_pyglet(self._do_resize, new_width, new_height, resampling_filter)

    def delete_current_image(self, event=None):
        logging.info(f"[ImageViewer] Requesting delete: {self.image_path}")
        if hasattr(self.controller, 'confirm_delete_item'):
            self._do_close()
            self.controller.confirm_delete_item(paths=[self.image_path])

    def copy_image_to_clipboard(self, event=None):
        try:
            buf  = io.BytesIO()
            self.original_image.convert('RGB').save(buf, 'BMP')
            data = buf.getvalue()[14:]
            buf.close()
            self.parent.clipboard_clear()
            self.parent.clipboard_append(data)
            self.parent.update()
            logging.info("[ImageViewer] Copied to clipboard.")
        except Exception as e:
            logging.warning(f"[ImageViewer] Clipboard error: {e}")

    def save_image_to_folder(self, event=None):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            defaultextension='.png',
            filetypes=[('PNG', '*.png'), ('JPEG', '*.jpg;*.jpeg'), ('All', '*.*')],
        )
        if path:
            self.original_image.save(path)
            self._image_dirty = False

    # ------------------------------------------------------------------
    # HUD  (worker thread)
    # ------------------------------------------------------------------

    def _get_hud_index_str(self):
        if self._hud_cache_path == self.image_path:
            return self._hud_index_str
        self._hud_cache_path = self.image_path
        try:
            files = [f for f in self.controller.video_files
                     if f['path'].lower().endswith(IMAGE_FORMATS)]
            idx   = next((i for i, f in enumerate(files)
                          if f['path'] == self.image_path), -1)
            self._hud_index_str = f"[{idx+1}/{len(files)}] " if idx != -1 else ""
        except Exception:
            self._hud_index_str = ""
        return self._hud_index_str

    def _update_hud(self):
        if self._hud_label is None:
            return
        zoom_text = f"{int(round(self.zoom * 100))}%"
        if self._zoom_label is not None:
            self._zoom_label.text = zoom_text
            self._zoom_shadow.text = zoom_text
            self._zoom_label.color = self._ZOOM_ON_WIDGET
            self._zoom_shadow.color = (0, 0, 0, 200)
        if not self.show_info:
            self._hud_label.text  = ''
            self._hud_shadow.text = ''
            if self._hud_rating_label is not None:
                self._hud_rating_label.text = ''
                self._hud_rating_shadow.text = ''
            return
        base_text = (
            f"{self._get_hud_index_str()}{self.image_name}"
            f"  |  {self._img_w}×{self._img_h} px"
        )
        rating = _current_file_rating(self.controller, self.image_path)
        rating_suffix = format_hud_rating_suffix(rating)
        self._hud_label.text  = base_text
        self._hud_shadow.text = base_text
        if self._BG_HEX[self.bg_index] == "white":
            self._hud_label.color = (0, 0, 0, 230)
            self._hud_shadow.color = (255, 255, 255, 120)
        else:
            self._hud_label.color = self._HUD_ON_SURFACE
            self._hud_shadow.color = (0, 0, 0, 200)
        if self._hud_rating_label is not None:
            self._hud_rating_label.text = rating_suffix
            self._hud_rating_shadow.text = rating_suffix
            self._hud_rating_label.color = rating_pyglet_rgba(rating)
            self._hud_rating_shadow.color = (0, 0, 0, 200)
            rating_x = 10 + int(self._hud_label.content_width)
            self._hud_rating_label.x = rating_x
            self._hud_rating_shadow.x = rating_x + 1

    def draw_info_hud(self):
        self._schedule_pyglet(self._update_hud)

    def notify_rating_changed(self, rating=None):
        """Refresh HUD after rating assign (visible even over fullscreen image)."""
        was_off = not getattr(self, "show_info", True)
        if was_off:
            self.show_info = True
        self.draw_info_hud()
        if was_off:
            parent = getattr(self, "parent", None)
            if parent is None:
                return
            job = getattr(self, "_rating_hud_restore_job", None)
            if job is not None:
                try:
                    parent.after_cancel(job)
                except Exception:
                    pass

            def _restore():
                self._rating_hud_restore_job = None
                self.show_info = False
                self.draw_info_hud()

            self._rating_hud_restore_job = parent.after(3000, _restore)

    # ------------------------------------------------------------------
    # Context menu  (Tkinter thread)
    # ------------------------------------------------------------------

    def _show_context_menu(self, screen_x, screen_y):
        def hk(name, default):
            if hasattr(self.controller, 'hotkeys_map'):
                v = self.controller.hotkeys_map.get(name, default)
                return v.replace('<', '').replace('>', '')
            return default

        menu = CTkFlatContextMenu(self.parent, app=self.controller)
        mode = getattr(self, "_view_fit_mode", "best_fit")
        menu.add_command(
            label="Actual Size",
            accelerator=hk('image_actual_size', 'A'),
            command=self.actual_size,
            is_selected=(mode == "actual"),
        )
        menu.add_command(
            label="Best Fit",
            accelerator=hk('image_fit_best', 'B'),
            command=self.best_fit,
            is_selected=(mode == "best_fit"),
        )
        menu.add_command(
            label="Fit Width",
            accelerator=hk('image_fit_width', 'W'),
            command=self.fit_width,
            is_selected=(mode == "fit_width"),
        )
        menu.add_separator()
        menu.add_command(label="Previous Image", accelerator=hk('image_prev', 'Left'), command=self.show_prev_image)
        menu.add_command(label="Next Image", accelerator=hk('image_next', 'Right'), command=self.show_next_image)
        menu.add_separator()
        menu.add_command(label="Zoom In", accelerator="+", command=self.zoom_in)
        menu.add_command(label="Zoom Out", accelerator="-", command=self.zoom_out)
        menu.add_separator()
        menu.add_command(label="Rotate Left", accelerator=hk('image_rotate_left', 'L'), command=self.rotate_left)
        menu.add_command(label="Rotate Right", accelerator=hk('image_rotate_right', 'R'), command=self.rotate_right)
        menu.add_command(label="Flip H", accelerator=hk('image_flip_h', 'H'), command=self.flip_horizontal)
        menu.add_command(label="Flip V", accelerator=hk('image_flip_v', 'V'), command=self.flip_vertical)
        menu.add_separator()
        menu.add_command(
            label="Resize Image…",
            accelerator=hk('image_resize', 'Ctrl+R'),
            command=self.open_resize_dialog,
        )
        _cmp_paths = []
        _open_cmp = getattr(self.controller, "open_image_compare", None)
        _sel_cmp = getattr(self.controller, "selected_image_paths_for_compare", None)
        if callable(_sel_cmp):
            try:
                _cmp_paths = _sel_cmp(None) or []
            except Exception:
                _cmp_paths = []
        if callable(_open_cmp) and len(_cmp_paths) >= 2:
            menu.add_command(
                label="Compare Images…",
                accelerator=hk('image_compare', 'Ctrl+Shift+C'),
                command=lambda: _open_cmp(_cmp_paths),
            )
        menu.add_separator()
        menu.add_command(label="Save As…", accelerator=hk('image_save', 'Ctrl+S'), command=self.save_image_to_folder)
        menu.add_command(label="Copy", accelerator=hk('image_copy', 'Ctrl+C'), command=self.copy_image_to_clipboard)
        menu.add_separator()
        _rating_path = getattr(self, "image_path", None)
        if _rating_path and os.path.isfile(_rating_path):
            append_rating_cascade_to_flat_menu(menu, self.controller, _rating_path)
            menu.add_separator()
        menu.add_command(label="Delete", accelerator=hk('image_delete', 'Del'), command=self.delete_current_image)
        menu.add_separator()
        if callable(getattr(self.controller, "open_library", None)):
            menu.add_command(label="Open full app", accelerator="Ctrl+L", command=self.controller.open_library)
            menu.add_separator()
        menu.add_command(label="Toggle Fullscreen", accelerator="F11", command=self.toggle_fullscreen)
        menu.tk_popup(int(screen_x), int(screen_y))

    # ------------------------------------------------------------------
    # Compat stubs
    # ------------------------------------------------------------------

    def update_image(self, high_quality=False):
        self._schedule_pyglet(self._update_hud)

    def update_scrollbars(self):         pass
    def center_image(self):              self._schedule_pyglet(self._do_best_fit)
    def resize_canvas(self, e=None):     pass
    def start_pan(self, e=None):         pass
    def do_pan(self, e=None):            pass
    def end_pan(self, e=None):           pass
    def _do_zoom_render(self):           pass
    def _render_hq(self):                pass
    def _render_after_pan(self):         pass
    def _schedule_zoom_update(self):     pass

    def debug_print_monitor(self):
        if self.window:
            wx, wy = self.window.get_location()
            logging.info(f"[ImageViewer] pos={wx},{wy} "
                         f"size={self.window.width}×{self.window.height}")
