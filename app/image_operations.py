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
import queue as _Q
import sys
import threading
import time

from PIL import Image as PILImage, ImageTk
from screeninfo import get_monitors
import tkinter as tk

from gui_elements import CTkFlatContextMenu


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

        self.hbar = tk.Scrollbar(self.image_window, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.hbar.grid(row=1, column=0, sticky='ew')
        self.canvas.config(xscrollcommand=self.hbar.set)

        self.vbar = tk.Scrollbar(self.image_window, orient=tk.VERTICAL, command=self.canvas.yview)
        self.vbar.grid(row=0, column=1, sticky='ns')
        self.canvas.config(yscrollcommand=self.vbar.set)

        self.image_window.grid_rowconfigure(0, weight=1)
        self.image_window.grid_columnconfigure(0, weight=1)

        self.image = PILImage.open(self.image_path)
        self.original_image = self.image.copy()
        self.photo = ImageTk.PhotoImage(self.image)

        self.canvas_image = self.canvas.create_image(0, 0, image=self.photo, anchor=tk.NW)
        self.canvas.config(scrollregion=self.canvas.bbox(tk.ALL))

        self.image_window.geometry(f"{self.image.width}x{self.image.height}")

        self.zoom_factor = 1.0
        
        # Proměnná pro časovač HQ renderu
        self._hq_timer = None
        
        # --- NOVÉ PROMĚNNÉ ---
        self.bg_colors = ['black', '#303030', 'white']
        self.bg_index = 0
        self.show_info = True # Defaultně zapnuto
        self.info_text_id = None

        # --- BINDINGS (Napojení na centrální hotkeys) ---
        # Funkce pro bezpečné získání klávesy z hlavního nastavení
        def hk(action_name, default=None):
            if hasattr(self.controller, 'hotkeys_map'):
                return self.controller.hotkeys_map.get(action_name, default)
            return default

        # 1. Myš a základní ovládání
        # self.image_window.bind(hk('zoom_thumb', "<Control-MouseWheel>"), self.zoom)
        
        self.image_window.bind("<MouseWheel>", self._wheel_handler)
        
        self.image_window.bind("<ButtonPress-2>", self.start_pan)
        self.image_window.bind("<ButtonRelease-2>", self.end_pan)
        self.image_window.bind("<B2-Motion>", self.do_pan)
        self.image_window.bind("<Button-3>", self.show_context_menu)
        self.canvas.bind("<Configure>", self.resize_canvas)

        # 2. Navigace (Z `hotkeys.py`)
        self.image_window.bind(hk('image_next', '<Right>'), lambda e: self.show_next_image())
        self.image_window.bind(hk('image_prev', '<Left>'), lambda e: self.show_prev_image())
        self.image_window.bind(hk('close_window', '<Escape>'), lambda e: self._do_close())
        self.image_window.bind(hk('image_delete', '<Delete>'), self.delete_current_image)

        # 3. Manipulace s obrázkem (Z `hotkeys.py`)
        self.image_window.bind(hk('image_actual_size', 'a'), lambda e: self.actual_size())
        self.image_window.bind(hk('image_toggle_bg', 'b'), self.toggle_background)
        self.image_window.bind(hk('image_toggle_info', 'i'), self.toggle_info)
        
        self.image_window.bind(hk('image_fit_best', 'b'), lambda e: self.best_fit())
        self.image_window.bind(hk('image_fit_width', 'w'), lambda e: self.fit_width())
        
        self.image_window.bind(hk('image_zoom_in', '+'), self.zoom_in)
        self.image_window.bind(hk('image_zoom_out', '-'), self.zoom_out)
        # Alternativní Control +/- pro zoom
        self.image_window.bind("<Control-plus>", self.zoom_in)
        self.image_window.bind("<Control-minus>", self.zoom_out)

        self.image_window.bind(hk('image_rotate_left', 'l'), lambda e: self.rotate_left())
        self.image_window.bind(hk('image_rotate_right', 'r'), lambda e: self.rotate_right())
        self.image_window.bind(hk('image_flip_h', 'h'), lambda e: self.flip_horizontal())
        self.image_window.bind(hk('image_flip_v', 'v'), lambda e: self.flip_vertical())
        
        self.image_window.bind(hk('image_copy', '<Control-c>'), lambda e: self.copy_image_to_clipboard())
        self.image_window.bind(hk('image_save', '<Control-s>'), lambda e: self.save_image_to_folder())
        
        # 4. Fullscreen
        self.image_window.bind(hk('toggle_fullscreen', '<F11>'), self.toggle_fullscreen)
        # Fallback pro "F" (běžné v prohlížečích) a Alt-Enter
        self.image_window.bind("f", self.toggle_fullscreen)
        self.image_window.bind("<Alt-Return>", self.toggle_fullscreen)

        self.image_window.bind("<F10>", lambda e: self.debug_print_monitor())

        self.update_scrollbars()
        
        #Na konci initu vynutíme první vykreslení HUDu
        self.image_window.after(100, self.draw_info_hud)

        # Same flags as Pyglet viewer — used by main.py fast-open and delete flow
        self._running = True

        def _on_toplevel_close():
            self._running = False
            try:
                self.image_window.destroy()
            except tk.TclError:
                pass

        self.image_window.protocol("WM_DELETE_WINDOW", _on_toplevel_close)

    def _do_close(self):
        """Match Pyglet viewer API for controller / fast-open code paths."""
        self._running = False
        try:
            self.image_window.destroy()
        except tk.TclError:
            pass

    def delete_current_image(self, event=None):
        """Vyžádá smazání aktuálního obrázku."""
        logging.info(f"[Image] Requesting delete for: {self.image_path}")
        if hasattr(self.controller, 'confirm_delete_item'):
            # Zavřeme okno, protože soubor zmizí
            self._do_close()
            # Vyvoláme dialog v hlavním okně
            self.controller.confirm_delete_item(paths=[self.image_path])

    def center_image(self):
        self.canvas.update_idletasks()  # důležité, aby Canvas znal správné rozměry!

        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()

        image_width = self.photo.width()
        image_height = self.photo.height()

        x = max((canvas_width - image_width) // 2, 0)
        y = max((canvas_height - image_height) // 2, 0)

        # logging.info(f"DEBUG Center image: Canvas={canvas_width}x{canvas_height}, Img={image_width}x{image_height}")
        self.canvas.coords(self.canvas_image, x, y)

    def load_image(self, path, name):
        self.image_path = path
        self.image_name = name

        try:
            self.image = PILImage.open(path)
            self.original_image = self.image.copy()
            self.zoom_factor = 1.0  # reset zoom při přeskoku

            self.update_image() # Toto by mělo HUD zavolat, ale pro jistotu...
            self.image_window.title(name)
            self.center_image()
            
            # --- AKTUALIZACE HUD ---
            # Zavoláme to explicitně, aby se aktualizoval index souboru (např. 5/120)
            self.draw_info_hud() 
            
        except Exception as e:
            logging.error(f"Failed to load image {name}: {e}")




    def skip(self, direction):
        try:
            all_files = [f for f in self.controller.video_files if f['path'].lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp'))]
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
        # self.canvas.update_idletasks() # Není nutné volat při každém pohybu, zpomaluje resize

    def zoom(self, event):
        scale = 1.1 if event.delta > 0 else 0.9
        self.zoom_factor *= scale
        self.update_image()

    def zoom_in(self, event=None):
        self.zoom_factor *= 1.1
        self.update_image()

    def zoom_out(self, event=None):
        self.zoom_factor *= 0.9
        self.update_image()

    def actual_size(self):
        self.zoom_factor = 1.0
        self.update_image()

    def best_fit(self):
        self.image_window.update_idletasks() # Update to get real dims
        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        img_width, img_height = self.original_image.size
        if img_width == 0 or img_height == 0: return
        
        scale_w = canvas_width / img_width
        scale_h = canvas_height / img_height
        self.zoom_factor = min(scale_w, scale_h)
        self.update_image()

    def fit_width(self):
        self.image_window.update_idletasks()
        canvas_width = self.canvas.winfo_width()
        img_width, _ = self.original_image.size
        if img_width == 0: return
        self.zoom_factor = canvas_width / img_width
        self.update_image()
        
    def rotate_left(self):
        self.original_image = self.original_image.rotate(90, expand=True)
        self.zoom_factor = 1.0
        self.update_image()

    def rotate_right(self):
        self.original_image = self.original_image.rotate(-90, expand=True)
        self.zoom_factor = 1.0
        self.update_image()

    def flip_horizontal(self):
        self.original_image = self.original_image.transpose(PILImage.FLIP_LEFT_RIGHT)
        self.update_image()

    def flip_vertical(self):
        self.original_image = self.original_image.transpose(PILImage.FLIP_TOP_BOTTOM)
        self.update_image()

    def save_image_to_folder(self):
        from tkinter import filedialog
        save_path = filedialog.asksaveasfilename(defaultextension=".png",
                                                  filetypes=[("PNG files", "*.png"), ("JPEG files", "*.jpg;*.jpeg"), ("All Files", "*.*")])
        if save_path:
            self.original_image.save(save_path)

    def start_pan(self, event):
        self.canvas.scan_mark(event.x, event.y)
        self.canvas.config(cursor="hand2")

    def end_pan(self, event):
        self.canvas.config(cursor="arrow")

    def do_pan(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)
 
    def toggle_fullscreen(self, event=None):
        self.is_fullscreen = not self.is_fullscreen
        self.image_window.update_idletasks()

        if self.is_fullscreen:
            # Zjisti, na kterém monitoru okno je
            x = self.image_window.winfo_x() + self.image_window.winfo_width() // 2
            y = self.image_window.winfo_y() + self.image_window.winfo_height() // 2
            
            target_monitor = None
            for monitor in get_monitors():
                if (monitor.x <= x < monitor.x + monitor.width and
                    monitor.y <= y < monitor.y + monitor.height):
                    target_monitor = monitor
                    break
            
            if target_monitor:
                self.image_window.overrideredirect(True)
                self.image_window.geometry(f"{target_monitor.width}x{target_monitor.height}+{target_monitor.x}+{target_monitor.y}")
            else:
                self.image_window.attributes("-fullscreen", True) # Fallback
        else:
            self.image_window.overrideredirect(False)
            self.image_window.attributes("-fullscreen", False)
            # Restore reasonable size
            self.image_window.geometry(f"{min(1200, self.original_image.width)}x{min(900, self.original_image.height)}")

        self.update_scrollbars()
        # Po změně velikosti vycentrujeme
        self.image_window.after(100, self.center_image)

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


    def update_image(self, high_quality=False):
        """
        Aktualizuje obrázek na plátně.
        high_quality=False -> Použije rychlý BILINEAR (pro zoomování).
        high_quality=True  -> Použije pomalý LANCZOS (pro finální zobrazení).
        """
        # 1. Zrušíme jakýkoliv čekající HQ render (protože uživatel právě změnil stav)
        if self._hq_timer:
            self.image_window.after_cancel(self._hq_timer)
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

        # Update GUI
        self.canvas.config(scrollregion=self.canvas.bbox(tk.ALL))
        
        # Pokud voláme z HQ timeru, už necentrujeme a neposouváme scrollbary, 
        # aby obraz "neskákal" pod rukama.
        if not high_quality:
            self.update_scrollbars()
            # self.center_image() # Volitelné - při zoomu na myš je lepší necentrovat

        # 4. Naplánování HQ renderu (Debounce)
        # Pokud jsme teď jeli v rychlém režimu, řekneme: 
        # "Za 150ms to překresli do hezka, pokud do té doby uživatel nic neudělá."
        if not high_quality:
            self._hq_timer = self.image_window.after(150, self._render_hq)
            
        # --- ZDE MUSÍ BÝT TOTO: ---
        self.draw_info_hud()    

    def _render_hq(self):
        """Voláno časovačem, když je klid."""
        logging.info("[HQ Render] Refining image quality...")
        self.update_image(high_quality=True)




    def toggle_background(self, event=None):
        """Cyklicky mění barvu pozadí (Černá -> Šedá -> Bílá)."""
        self.bg_index = (self.bg_index + 1) % len(self.bg_colors)
        color = self.bg_colors[self.bg_index]
        self.canvas.configure(bg=color)
        self.draw_info_hud() # Překreslit info, aby bylo vidět (změna barvy textu)

    def toggle_info(self, event=None):
        """Zobrazí/Skryje info text."""
        self.show_info = not self.show_info
        self.draw_info_hud()

    def draw_info_hud(self):
        """Vykreslí textové info v levém horním rohu."""
        # Smazat starý text
        if self.info_text_id:
            self.canvas.delete(self.info_text_id)
            self.info_text_id = None

        if not self.show_info:
            return

        # Získání dat
        zoom_pct = int(self.zoom_factor * 100)
        w, h = self.original_image.size
        
        # Zkusíme zjistit index souboru (např. 5/120)
        index_str = ""
        try:
            # Toto je trochu hack, saháme do controlleru, ale je to rychlé
            all_files = [f for f in self.controller.video_files if f['path'].lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp'))]
            total = len(all_files)
            # Najdeme index aktuálního
            idx = next((i for i, f in enumerate(all_files) if f['path'] == self.image_path), -1)
            if idx != -1:
                index_str = f"[{idx + 1}/{total}] "
        except:
            pass

        text = f"{index_str}{self.image_name}  |  {w}x{h} px  |  {zoom_pct}%"
        
        # Barva textu podle pozadí (aby byl vždy čitelný)
        text_color = "black" if self.bg_colors[self.bg_index] == "white" else "white"
        
        # Pozice (levý horní roh, ale fixní vůči oknu, ne plátnu)
        # Canvas v Tkinteru posouvá vše s objekty. 
        # Aby HUD "plaval" nad obrázkem a neujížděl při posunu, je lepší použít canvas.canvasx/y
        # NEBO jednodušeji: použít Label widget umístěný přes place(), což je robustnější.
        
        # Pro jednoduchost a rychlost zkusíme canvas text s offsetem podle scrollu:
        cx = self.canvas.canvasx(10)
        cy = self.canvas.canvasy(10)
        
        # Vytvoření textu s lehkým stínem pro čitelnost
        self.canvas.create_text(cx+1, cy+1, text=text, anchor="nw", fill="black", font=("Segoe UI", 10, "bold"), tags="hud")
        self.info_text_id = self.canvas.create_text(cx, cy, text=text, anchor="nw", fill=text_color, font=("Segoe UI", 10, "bold"), tags="hud")
        
        # Zajistit, že HUD je vždy nahoře
        self.canvas.tag_raise("hud")


    def update_scrollbars(self):
        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        image_bbox = self.canvas.bbox(self.canvas_image)

        if image_bbox:
            image_width = image_bbox[2] - image_bbox[0]
            image_height = image_bbox[3] - image_bbox[1]

            self.hbar.grid() if image_width > canvas_width else self.hbar.grid_remove()
            self.vbar.grid() if image_height > canvas_height else self.vbar.grid_remove()

        self.canvas.config(scrollregion=self.canvas.bbox(tk.ALL))

    def show_context_menu(self, event):
        menu = tk.Menu(self.image_window, tearoff=0)
        
        def hk_label(name, default):
            if hasattr(self.controller, 'hotkeys_map'):
                key = self.controller.hotkeys_map.get(name, default)
                return key.replace("<", "").replace(">", "")
            return default

        menu.add_command(label=f"Actual Size ({hk_label('image_actual_size', 'A')})", command=self.actual_size)
        menu.add_command(label=f"Best Fit ({hk_label('image_fit_best', 'B')})", command=self.best_fit)
        menu.add_command(label=f"Fit Width ({hk_label('image_fit_width', 'W')})", command=self.fit_width)
        menu.add_separator()
        menu.add_command(label=f"Zoom In (+)", command=self.zoom_in)
        menu.add_command(label=f"Zoom Out (-)", command=self.zoom_out)
        menu.add_separator()
        menu.add_command(label=f"Rotate Left ({hk_label('image_rotate_left', 'L')})", command=self.rotate_left)
        menu.add_command(label=f"Rotate Right ({hk_label('image_rotate_right', 'R')})", command=self.rotate_right)
        menu.add_command(label=f"Flip Horizontal ({hk_label('image_flip_h', 'H')})", command=self.flip_horizontal)
        menu.add_command(label=f"Flip Vertical ({hk_label('image_flip_v', 'V')})", command=self.flip_vertical)
        menu.add_separator()
        menu.add_command(label=f"Save As ({hk_label('image_save', 'Ctrl+S')})", command=self.save_image_to_folder)
        menu.add_command(label=f"Copy ({hk_label('image_copy', 'Ctrl+C')})", command=self.copy_image_to_clipboard)
        menu.add_separator()
        menu.add_command(label=f"Delete ({hk_label('image_delete', 'Del')})", command=self.delete_current_image)
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
        raw = PILImage.open(image_path)
        if raw.mode not in ('RGB', 'RGBA'):
            raw = raw.convert('RGBA')
        self.original_image = raw
        self._img_w, self._img_h = raw.size

        # View state — plain floats, GIL-safe
        self.zoom  = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0

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
        self._keys       = None
        self._mm_bg_shape = None   # minimap: gray background rect
        self._mm_vp_shape = None   # minimap: viewport indicator (box outline)

        self._lmb_click_t = 0.0
        self._lmb_click_xy = None  # (x, y) in window coords, last LMB

        self._running = True

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

        self._apply_best_fit(win_w, win_h)
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
            'a': (k.A, False), 'b': (k.B, False), 'i': (k.I, False),
            'w': (k.W, False), '+': (k.PLUS,  False), '-': (k.MINUS, False),
            'l': (k.L, False), 'r': (k.R,     False), 'h': (k.H,     False),
            'v': (k.V, False), 'f': (k.F,     False),
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
        self._batch.draw()
        self._draw_minimap()

    def _on_resize(self, width, height):
        from pyglet.gl import glViewport
        glViewport(0, 0, width, height)
        self._apply_best_fit(width, height)
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

        # Ctrl+F … main app maps to search; in image viewer we want fullscreen instead
        if ctrl and symbol in (k.F,):
            self._do_toggle_fullscreen()
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
            img = PILImage.open(path)
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGBA')
            self.original_image = img
            self._upload_texture(img)
            self._apply_best_fit()
            self.window.set_caption(name)
            self._hud_cache_path = None
            self._update_hud()
        except Exception as e:
            logging.error(f"[ImageViewer] Failed to load {name}: {e}")

    def _do_zoom_in(self):
        self.zoom = min(50.0, self.zoom * 1.1);  self._update_hud()

    def _do_zoom_out(self):
        self.zoom = max(0.01, self.zoom * 0.9);  self._update_hud()

    def _do_actual_size(self):
        self.zoom  = 1.0
        self.pan_x = (self.window.width  - self._img_w) / 2
        self.pan_y = (self.window.height - self._img_h) / 2
        self._update_hud()

    def _do_best_fit(self):
        self._apply_best_fit();  self._update_hud()

    def _do_fit_width(self):
        self.zoom  = self.window.width / self._img_w
        self.pan_x = 0.0
        self.pan_y = (self.window.height - self._img_h * self.zoom) / 2
        self._update_hud()

    def _do_rotate_left(self):
        self.original_image = self.original_image.rotate(90, expand=True)
        self._upload_texture(self.original_image);  self._apply_best_fit();  self._update_hud()

    def _do_rotate_right(self):
        self.original_image = self.original_image.rotate(-90, expand=True)
        self._upload_texture(self.original_image);  self._apply_best_fit();  self._update_hud()

    def _do_flip_h(self):
        self.original_image = self.original_image.transpose(PILImage.FLIP_LEFT_RIGHT)
        self._upload_texture(self.original_image);  self._update_hud()

    def _do_flip_v(self):
        self.original_image = self.original_image.transpose(PILImage.FLIP_TOP_BOTTOM)
        self._upload_texture(self.original_image);  self._update_hud()

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
        self._schedule_pyglet(self._do_load_image, path, name)

    def skip(self, direction):
        try:
            exts  = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp')
            files = [f for f in self.controller.video_files
                     if f['path'].lower().endswith(exts)]
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

    # ------------------------------------------------------------------
    # HUD  (worker thread)
    # ------------------------------------------------------------------

    def _get_hud_index_str(self):
        if self._hud_cache_path == self.image_path:
            return self._hud_index_str
        self._hud_cache_path = self.image_path
        try:
            exts  = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp')
            files = [f for f in self.controller.video_files
                     if f['path'].lower().endswith(exts)]
            idx   = next((i for i, f in enumerate(files)
                          if f['path'] == self.image_path), -1)
            self._hud_index_str = f"[{idx+1}/{len(files)}] " if idx != -1 else ""
        except Exception:
            self._hud_index_str = ""
        return self._hud_index_str

    def _update_hud(self):
        if self._hud_label is None:
            return
        if not self.show_info:
            self._hud_label.text  = ''
            self._hud_shadow.text = ''
            return
        zoom_pct = int(self.zoom * 100)
        text = (
            f"{self._get_hud_index_str()}{self.image_name}"
            f"  |  {self._img_w}×{self._img_h} px  |  {zoom_pct}%"
        )
        self._hud_label.text  = text
        self._hud_shadow.text = text
        if self._BG_HEX[self.bg_index] == "white":
            self._hud_label.color = (0, 0, 0, 230)
            self._hud_shadow.color = (255, 255, 255, 120)
        else:
            self._hud_label.color = self._HUD_ON_SURFACE
            self._hud_shadow.color = (0, 0, 0, 200)

    def draw_info_hud(self):
        self._schedule_pyglet(self._update_hud)

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
        menu.add_command(label="Actual Size", accelerator=hk('image_actual_size', 'A'), command=self.actual_size)
        menu.add_command(label="Best Fit", accelerator=hk('image_fit_best', 'B'), command=self.best_fit)
        menu.add_command(label="Fit Width", accelerator=hk('image_fit_width', 'W'), command=self.fit_width)
        menu.add_separator()
        menu.add_command(label="Zoom In", accelerator="+", command=self.zoom_in)
        menu.add_command(label="Zoom Out", accelerator="-", command=self.zoom_out)
        menu.add_separator()
        menu.add_command(label="Rotate Left", accelerator=hk('image_rotate_left', 'L'), command=self.rotate_left)
        menu.add_command(label="Rotate Right", accelerator=hk('image_rotate_right', 'R'), command=self.rotate_right)
        menu.add_command(label="Flip H", accelerator=hk('image_flip_h', 'H'), command=self.flip_horizontal)
        menu.add_command(label="Flip V", accelerator=hk('image_flip_v', 'V'), command=self.flip_vertical)
        menu.add_separator()
        menu.add_command(label="Save As…", accelerator=hk('image_save', 'Ctrl+S'), command=self.save_image_to_folder)
        menu.add_command(label="Copy", accelerator=hk('image_copy', 'Ctrl+C'), command=self.copy_image_to_clipboard)
        menu.add_separator()
        menu.add_command(label="Delete", accelerator=hk('image_delete', 'Del'), command=self.delete_current_image)
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
