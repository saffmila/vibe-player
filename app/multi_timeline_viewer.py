"""
Multi-row timeline strip viewer for Vibe Player (shared horizontal scroll).

Stacks several video timelines vertically with synced horizontal scrolling,
Shift+wheel zoom, and click/double-click routing to the main app.
"""

import customtkinter as ctk
import tkinter as tk
from PIL import Image, ImageTk
import os
import threading
import logging


class MultiTimelineViewer(ctk.CTkFrame):
    """
    Displays multiple video timeline strips vertically with a shared horizontal scrollbar.

    Features:
    - Vertical scroll: MouseWheel (strips up/down)
    - Shared CTk horizontal scrollbar at bottom: all strips scroll in sync
    - Shift+Scroll: resize thumbnails (scrollbar visible only when zoomed)
    - Single click: select video in main app
    - Double click: play video in main app
    - Selection border matches main thumbnail style
    - Filename shown as hover tooltip (reuses controller.show_hover_info if available)
    - show_timeline_texts=False: labels hidden by default
    """

    SEL_COLOR        = "#4f575f"
    BORDER_COLOR     = "#2a2a2a"
    BG_STRIP         = "#242424"
    BG_CANVAS        = "#1a1a1a"
    THUMB_W_DEFAULT  = 100

    def __init__(self, master, timeline_manager, controller=None,
                 show_timeline_texts=False, **kwargs):
        super().__init__(master, **kwargs)
        self.timeline_manager    = timeline_manager
        self.controller          = controller
        self.show_timeline_texts = show_timeline_texts
        self.image_references     = []
        self._current_load_id    = 0
        self._current_video_paths = []
        self._selected_paths     = set()
        self._anchor_path        = None
        self._strip_frames       = {}      # path → tk.Frame
        self._thumb_canvases     = []      # shared horizontal scroll
        self._syncing            = False
        self._h_visible          = False
        self._user_zoomed        = False

        self.thumb_w = self.THUMB_W_DEFAULT
        self.thumb_h = 56

        self._build_layout()

    def _build_layout(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._v_scroll = ctk.CTkScrollbar(self, orientation="vertical")
        self._v_scroll.grid(row=0, column=1, sticky="ns")

        # Horizontální CTk scrollbar – skrytý dokud není zoom
        self._h_scroll = ctk.CTkScrollbar(
            self, orientation="horizontal", command=self._hscroll_all
        )
        self._v_canvas = tk.Canvas(
            self,
            bg=self.BG_CANVAS,
            yscrollcommand=self._v_scroll.set,
            highlightthickness=0,
        )
        self._v_canvas.grid(row=0, column=0, sticky="nsew")
        self._v_scroll.configure(command=self._v_canvas.yview)

        # Vnitřní frame s obsahem (stripy)
        self._inner = tk.Frame(self._v_canvas, bg=self.BG_CANVAS)
        self._inner_id = self._v_canvas.create_window(0, 0, anchor="nw", window=self._inner)

        self._inner.bind("<Configure>", self._on_inner_configure)
        self._v_canvas.bind("<Configure>", self._on_canvas_configure)

        for w in (self, self._v_canvas, self._inner):
            w.bind("<MouseWheel>",       self._on_vscroll)
            w.bind("<Shift-MouseWheel>", self._on_resize_scroll)

    def _on_inner_configure(self, event):
        self._v_canvas.configure(scrollregion=self._v_canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._v_canvas.itemconfig(self._inner_id, width=event.width)

    def _on_vscroll(self, event):
        self._v_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ------------------------------------------------------------------ #
    #  SDÍLENÝ HORIZONTÁLNÍ SCROLL                                         #
    # ------------------------------------------------------------------ #

    def _hscroll_all(self, *args):
        """CTkScrollbar táhne → přesuň všechny thumb canvasy najednou."""
        if self._syncing:
            return
        self._syncing = True
        for c in self._thumb_canvases:
            if c.winfo_exists():
                c.xview(*args)
        self._syncing = False
        self._refresh_hscroll()

    def _on_thumb_xscroll(self, *args):
        """Jeden canvas se posunul → sync ostatní + update scrollbar."""
        self._h_scroll.set(*args)
        if self._syncing:
            return
        self._syncing = True
        try:
            pos = float(args[0])
        except (IndexError, ValueError):
            pos = 0.0
        for c in self._thumb_canvases:
            if c.winfo_exists():
                c.xview_moveto(pos)
        self._syncing = False

    def _refresh_hscroll(self):
        if self._thumb_canvases and self._thumb_canvases[0].winfo_exists():
            self._h_scroll.set(*self._thumb_canvases[0].xview())

    def _update_hscroll_visibility(self):
        """Scrollbar viditelný jen při zoomu a jen když obsah přesahuje."""
        if self.thumb_w <= self.THUMB_W_DEFAULT:
            if self._h_visible:
                self._h_scroll.grid_remove()
                self._h_visible = False
            return

        needs = any(
            c.winfo_exists() and c.xview() != (0.0, 1.0)
            for c in self._thumb_canvases
        )
        if needs and not self._h_visible:
            self._h_scroll.grid(row=1, column=0, sticky="ew")
            self._h_visible = True
        elif not needs and self._h_visible:
            self._h_scroll.grid_remove()
            self._h_visible = False

    # ------------------------------------------------------------------ #
    #  RESIZE (Shift+Scroll)                                               #
    # ------------------------------------------------------------------ #

    def _on_resize_scroll(self, event):
        delta = 1 if event.delta > 0 else -1
        new_w = max(60, min(320, self.thumb_w + delta * 12))
        new_h = new_w * 56 // 100
        if new_w == self.thumb_w:
            return
        self._user_zoomed = True   # uživatel explicitně zoomoval → auto-fit se přeskočí
        self.thumb_w = new_w
        self.thumb_h = new_h
        if self._current_video_paths:
            self.load_videos(self._current_video_paths)
        self.after(120, self._update_hscroll_visibility)

    # ------------------------------------------------------------------ #
    #  LOAD / RENDER                                                       #
    # ------------------------------------------------------------------ #

    def load_videos(self, video_paths, num_thumbs=8):
        """Smaže aktuální obsah a načte nové stripy."""
        # Auto-fit: pokud uživatel nezoomoval, přizpůsob šířku thumbu panelu
        if not self._user_zoomed:
            avail_w = self._v_canvas.winfo_width()
            if avail_w <= 1:
                # Canvas ještě nemá reálnou šířku – počkej na první layout a zkus znovu
                self.after(30, lambda: self.load_videos(video_paths, num_thumbs))
                return
            new_w = max(60, (avail_w - 20) // num_thumbs)
            self.thumb_w = new_w
            self.thumb_h = new_w * 56 // 100

        self._current_video_paths = list(video_paths)
        self._selected_paths.clear()
        self._anchor_path = None
        self._strip_frames.clear()
        self._thumb_canvases.clear()

        for w in self._inner.winfo_children():
            w.destroy()
        self.image_references.clear()

        self._current_load_id += 1
        load_id = self._current_load_id

        threading.Thread(
            target=self._render_strips_thread,
            args=(video_paths, num_thumbs, load_id),
            daemon=True
        ).start()

    def _render_strips_thread(self, video_paths, num_thumbs, load_id):
        for path in video_paths:
            if self._current_load_id != load_id:
                break
            if not os.path.exists(path):
                continue
            thumb_paths = self.timeline_manager.get_timeline_thumbnails(path, num_thumbs)
            if thumb_paths and self._current_load_id == load_id:
                self.after(0, self._add_strip_to_gui, path, thumb_paths)

    def _add_strip_to_gui(self, video_path, thumb_paths):
        is_sel = video_path in self._selected_paths

        strip_frame = tk.Frame(
            self._inner,
            bg=self.BG_STRIP,
            highlightbackground=self.SEL_COLOR if is_sel else self.BORDER_COLOR,
            highlightthickness=2,
        )
        strip_frame.pack(fill="x", padx=5, pady=(4, 0))
        self._strip_frames[video_path] = strip_frame

        # Horizontální canvas – napojený na sdílený scrollbar
        thumb_canvas = tk.Canvas(
            strip_frame,
            height=self.thumb_h + 8,
            bg=self.BG_STRIP,
            xscrollcommand=self._on_thumb_xscroll,
            highlightthickness=0,
        )
        thumb_canvas.pack(fill="x", padx=4, pady=(4, 2))
        self._thumb_canvases.append(thumb_canvas)

        inner_row = tk.Frame(thumb_canvas, bg=self.BG_STRIP)
        thumb_canvas.create_window(0, 0, anchor="nw", window=inner_row)

        inner_row.bind(
            "<Configure>",
            lambda e, tc=thumb_canvas: tc.configure(scrollregion=tc.bbox("all"))
        )

        for item in thumb_paths:
            tp = item[0] if isinstance(item, tuple) else item
            if not tp or not os.path.exists(tp):
                continue
            try:
                img = Image.open(tp)
                img.thumbnail((self.thumb_w, self.thumb_h))
                photo = ImageTk.PhotoImage(img)
                self.image_references.append(photo)
                lbl = tk.Label(inner_row, image=photo, bg=self.BG_STRIP, bd=0)
                lbl.pack(side="left", padx=2)
                self._bind_events(lbl, video_path)
            except Exception as e:
                logging.error(f"MultiTimelineViewer: error loading {tp}: {e}")

        # Volitelný textový popisek pod stripy
        if self.show_timeline_texts:
            font_size = max(8, self.thumb_w // 10)
            name_lbl = tk.Label(
                strip_frame,
                text=os.path.basename(video_path),
                anchor="w",
                font=("Arial", font_size),
                fg="#666666",
                bg=self.BG_STRIP,
            )
            name_lbl.pack(fill="x", padx=6, pady=(0, 3))
            self._bind_events(name_lbl, video_path)

        # Sync scroll pozice s existujícími canvasy
        if len(self._thumb_canvases) > 1:
            ref = self._thumb_canvases[0]
            if ref.winfo_exists():
                thumb_canvas.after(
                    50, lambda tc=thumb_canvas, r=ref: tc.xview_moveto(r.xview()[0])
                )

        for w in (strip_frame, thumb_canvas, inner_row):
            self._bind_events(w, video_path)

        self.after(120, self._update_hscroll_visibility)

    # ------------------------------------------------------------------ #
    #  EVENT BINDING                                                       #
    # ------------------------------------------------------------------ #

    def _bind_events(self, widget, video_path):
        widget.bind("<Button-1>",        lambda e, p=video_path: self._on_strip_click(e, p))
        widget.bind("<Double-Button-1>", lambda e, p=video_path: self._on_strip_double_click(p))
        widget.bind("<Shift-MouseWheel>", self._on_resize_scroll)
        widget.bind("<MouseWheel>",       self._on_vscroll)
        widget.bind("<Enter>", lambda e, p=video_path: self._on_hover_enter(e, p))
        widget.bind("<Leave>", lambda e: self._on_hover_leave())

    # ------------------------------------------------------------------ #
    #  TOOLTIP                                                             #
    # ------------------------------------------------------------------ #

    def _on_hover_enter(self, event, video_path):
        if self.controller and hasattr(self.controller, "show_hover_info"):
            self.controller.show_hover_info(event, video_path)
        else:
            self._show_simple_tooltip(event, os.path.basename(video_path))

    def _on_hover_leave(self):
        if self.controller and hasattr(self.controller, "hide_hover_info"):
            self.controller.hide_hover_info()
        else:
            self._hide_simple_tooltip()

    def _show_simple_tooltip(self, event, text):
        self._hide_simple_tooltip()
        self._tooltip = tk.Toplevel(self)
        self._tooltip.wm_overrideredirect(True)
        self._tooltip.geometry(f"+{event.x_root + 12}+{event.y_root + 12}")
        self._tooltip.configure(bg="#2b2b2b", highlightbackground="#777", highlightthickness=1)
        tk.Label(
            self._tooltip, text=text,
            bg="#2b2b2b", fg="#d0d0d0",
            font=("Segoe UI", 9), padx=8, pady=4
        ).pack()

    def _hide_simple_tooltip(self):
        if hasattr(self, "_tooltip") and self._tooltip:
            try:
                self._tooltip.destroy()
            except Exception:
                pass
            self._tooltip = None

    # ------------------------------------------------------------------ #
    #  SELECTION & PLAY                                                    #
    # ------------------------------------------------------------------ #

    def _update_all_borders(self):
        """Překreslí selection border na všech stripech podle _selected_paths."""
        for path, frame in self._strip_frames.items():
            if frame.winfo_exists():
                color = self.SEL_COLOR if path in self._selected_paths else self.BORDER_COLOR
                frame.configure(highlightbackground=color)

    def _sync_to_controller(self):
        """Synchronizuje vybraná videa do controller.selected_thumbnails."""
        if not self.controller:
            return
        # Seřadíme dle pořadí zobrazení (current_video_paths)
        ordered = [p for p in self._current_video_paths if p in self._selected_paths]
        new_sel = []
        for path in ordered:
            norm = os.path.normpath(path)
            for i, vf in enumerate(self.controller.video_files):
                if os.path.normpath(vf["path"]) == norm:
                    lbl = self.controller.thumbnail_labels.get(path)
                    if lbl:
                        new_sel.append((path, lbl, i))
                    break
        if new_sel:
            self.controller.selected_thumbnails = new_sel
            last_path = new_sel[-1][0]
            self.controller.selected_file_path = last_path
            self.controller.selected_thumbnail_index = new_sel[-1][2]
            self.controller.update_thumbnail_selection()

    def _on_strip_click(self, event, video_path):
        shift = bool(event.state & 0x0001)
        ctrl  = bool(event.state & 0x0004)

        if shift and self._anchor_path and self._anchor_path in self._current_video_paths:
            # Shift+klik → range select od kotvy po aktuální
            i1 = self._current_video_paths.index(self._anchor_path)
            i2 = self._current_video_paths.index(video_path) \
                 if video_path in self._current_video_paths else i1
            start, end = sorted([i1, i2])
            self._selected_paths = set(self._current_video_paths[start:end + 1])

        elif ctrl:
            # Ctrl+klik → toggle
            if video_path in self._selected_paths:
                self._selected_paths.discard(video_path)
            else:
                self._selected_paths.add(video_path)
                self._anchor_path = video_path

        else:
            # Prostý klik → single select
            self._selected_paths = {video_path}
            self._anchor_path = video_path

        self._update_all_borders()
        self._sync_to_controller()

    def _on_strip_double_click(self, video_path):
        # Double-click vždy přehraje to konkrétní video
        self._selected_paths = {video_path}
        self._anchor_path = video_path
        self._update_all_borders()
        if not self.controller:
            return
        self.controller.open_video_player(video_path, os.path.basename(video_path))
