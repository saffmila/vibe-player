"""Virtual-scroll grid using native Tk Canvas scrolling.

Slots are individual create_window items on the main canvas.
No intermediate container frame — this bypasses the Win32 GDI
32,767-pixel height limit.  Scrolling uses canvas.yview (C-level,
zero flicker).  Data rebinding happens only when the visible row
range changes.

Two slot types for Wide folder mode:
  - wide slots: full-width folder strips
  - standard slots: regular thumbnail cells
"""
from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import customtkinter as ctk
from PIL import Image, ImageOps, ImageTk
import tkinterdnd2 as dnd

from file_operations import (
    thumbnail_cache,
    create_image_thumbnail,
    create_video_thumbnail,
)
from vtp_constants import VIDEO_FORMATS, IMAGE_FORMATS, preview_skip_subdir

_OVERSCAN_ROWS = 3
_OFFSCREEN_Y = -10000

# Defaults are stored on the instance (see init_virtual_grid).
# Module-level values stay only as safe fallbacks.
_WIDE_PREVIEW_PAD_SIDE = 10
_WIDE_PREVIEW_MARGIN_Y = 12
_WIDE_PREVIEW_INNER_PAD = 6


class VtpVirtualGridMixin:

    # ------------------------------------------------------------------
    # 1. Initialisation
    # ------------------------------------------------------------------

    def init_virtual_grid(self):
        self._vg_active = False
        self._vg_std_pool: list[dict] = []
        self._vg_wide_pool: list[dict] = []
        self._vg_std_pool_size = 0
        self._vg_wide_pool_size = 0

        self._vg_row_height = 1
        self._vg_wide_row_height = 1
        self._vg_visible_rows = 0
        self._vg_cols = 1
        self._vg_canvas_h = 600
        # During tk.PanedWindow / CTk repair, canvas winfo_width can be far too small; avoid cols=1 glitches.
        self._vg_last_good_canvas_w = 0
        self._vg_last_good_cols = 0

        self._vg_scroll_px = 0.0
        self._vg_max_scroll_px = 0.0
        self._vg_scrollregion_h = 1
        self._vg_last_first_row = -1

        self._vg_data: list[dict] = []
        self._vg_folder_count = 0
        self._vg_is_wide = False

        self._vg_scroll_job = None
        self._vg_render_id = 0
        self._vg_pending_gen: set[str] = set()
        self._vg_gen_queue: list[dict] = []
        self._vg_gen_active = 0
        self._vg_gen_limit = 6
        self._vg_gen_force_refresh = False
        self._vg_gen_thumbnail_time = None
        self._vg_y_offset = 0
        self._vg_dynamic_label_h = 48
        self._vg_data_index_by_path: dict[str, int] = {}
        self._vg_visible_std_slots_by_path: dict[str, dict] = {}
        self._vg_visible_wide_slots_by_path: dict[str, dict] = {}
        self._vg_label_font_cache: dict[int, tkfont.Font | None] = {}
        self._vg_label_measure_cache: dict[tuple, int] = {}
        self._vg_info_state_key: tuple = ()
        self._preview_status_cache: dict[tuple, str] = {}
        self._folder_preview_pending_render_ids: dict[tuple, int | None] = {}
        self._folder_preview_queue: queue.Queue = queue.Queue()
        self._folder_preview_worker_started = False
        self._folder_preview_idle_delay_ms = 650
        self._folder_preview_worker_sleep_s = 0.10
        self._folder_preview_max_depth = 5
        self._folder_preview_max_dirs = 250
        self._folder_preview_max_entries = 3000
        self._folder_preview_scan_budget_s = 0.75
        self._folder_preview_empty_retry_s = 30.0
        self._folder_preview_empty_seen_at: dict[tuple, float] = {}
        self._start_folder_preview_worker()

        # ------------------------------------------------------------------
        # Wide folder tuning (easy tweaking)
        # ------------------------------------------------------------------
        # Preview (right section) paddings
        self.vg_wide_preview_pad_side = _WIDE_PREVIEW_PAD_SIDE
        self.vg_wide_preview_margin_y = _WIDE_PREVIEW_MARGIN_Y
        self.vg_wide_preview_inner_pad = _WIDE_PREVIEW_INNER_PAD

        # Composite generator: number of tiles in the wide thumbnail strip
        self.vg_wide_preview_count = 5

        # Fonts
        self.vg_wide_title_scale = 1   # title (folder icon + name)
        self.vg_wide_stats_scale = 1.0   # stats/keywords labels

        # Label row spacing inside left panel (stats / keywords / rating blocks)
        self.vg_wide_label_row_gap = 7

        # Standard thumbnail: vertical padding (top, bottom) per info line under the image
        self.vg_std_label_row_pady = (9, 3)
        # Extra px added to measured caption block (font/wrap / Tk vs heuristic)
        self.vg_std_label_height_fudge = 14
        # Extra per rendered text line to avoid platform-specific clipping.
        self.vg_std_label_line_reserve_px = 1
        # Per stacked info label beyond the first (spacing Tk adds between widgets)
        self.vg_std_label_stack_margin = 6
        # Hard cap so one extreme title does not blow up row height
        self.vg_std_label_max_px = 280
        # Extra space below folder title before first stats line; None = use label_row_gap
        self.vg_wide_title_bottom_gap = None

        # Divider (vertical line) visibility
        self.vg_wide_show_divider = False

        # Strip / card chrome (None = fall back to folder_color_media / wide_folder_borderColor)
        self.vg_wide_bg_color = None
        self.vg_wide_border_color = None
        self.vg_wide_show_border = False
        self.vg_wide_border_width = 1

        self.vg_wide_show_shadow = False
        self.vg_wide_shadow_color = "#080808"
        self.vg_wide_shadow_offset = 4

        # Preview tiles (right, inside card): PIL; radius 0 = auto from thumb size
        self.vg_wide_round_preview_corners = True
        self.vg_wide_preview_corner_radius = 0

        # Outer wide-folder card (container): rounded polygon on strip_canvas
        self.vg_wide_round_container = True
        self.vg_wide_container_corner_radius = 10

        # Vertical gap between wide folder cards (bands stacked in scroll direction)
        self.vg_wide_inter_row_gap = 10
        # Top/bottom inset of each card inside its row (adds to row height via _vg_recalc)
        self.vg_wide_outer_pad_x = 0
        self.vg_wide_outer_pad_y = 0
        # pixels inset from canvas left+right (mimics old pack padx); None = thumb_Padding + 2
        self.vg_wide_canvas_gutter = None

    # ------------------------------------------------------------------
    # 2. Pool creation — standard slots
    # ------------------------------------------------------------------

    def _vg_build_std_pool(self, pool_size: int):
        for slot in self._vg_std_pool:
            try:
                self.canvas.delete(slot["win_id"])
            except Exception:
                pass
            slot["frame"].destroy()
        self._vg_std_pool.clear()

        thumb_w, thumb_h = self.thumbnail_size
        border = self.effective_thumb_border_size()
        border_thick = self.effective_thumb_outlinewidth()

        canvas_w = thumb_w + border * 2
        canvas_h = thumb_h + border * 2 + 10
        # Caption band + outer shell: match main thumb panel (avoids separate “label card” look).
        bg = self._vg_safe_color(
            getattr(self, "BackroundColor", None),
            self._vg_safe_color(self.labelBGColor),
        )
        canvas_bg = self._vg_safe_color(self.BackroundColor, bg)
        lbl_font = ("Helvetica", self._get_effective_thumb_font_size())

        for _ in range(pool_size):
            frame = tk.Frame(self.canvas, bg=bg, bd=0,
                             highlightthickness=0)

            thumb_canvas = tk.Canvas(
                frame, width=canvas_w, height=canvas_h,
                bg=canvas_bg, bd=0, highlightthickness=0,
            )
            thumb_canvas.pack()

            rr = self.effective_thumb_frame_radius()
            self.create_rounded_rectangle(
                thumb_canvas, border_thick, border_thick,
                canvas_w - border_thick, canvas_h - border_thick,
                radius=rr, outline=self.thumbBorderColor,
                width=border_thick, fill=self.thumbBGColor, tags="border",
            )
            thumb_canvas.create_image(canvas_w // 2, canvas_h // 2, tags="thumbnail")

            cap_h = int(getattr(self, "_vg_dynamic_label_h", 48))
            cap_h = max(34, cap_h)
            caption_shell = tk.Frame(frame, bg=bg, bd=0, highlightthickness=0, height=cap_h)
            caption_shell.pack(fill="x")
            caption_shell.pack_propagate(False)

            labels_frame = tk.Frame(caption_shell, bg=bg, bd=0, highlightthickness=0)
            labels_frame.pack(side="top", anchor="n", fill="x")

            rating_canvas = tk.Canvas(
                frame, width=32, height=32, bd=0,
                highlightthickness=0, bg=bg,
            )
            rating_canvas.create_oval(10, 0, 30, 20, fill="gray", outline="white", width=2, tags="circle")
            rating_canvas.create_text(20, 10, text="", fill="white", font=("Helvetica", 12, "bold"), tags="rtext")

            win_id = self.canvas.create_window(
                0, _OFFSCREEN_Y, window=frame, anchor="nw")

            self._vg_std_pool.append({
                "frame": frame, "canvas": thumb_canvas, "caption_shell": caption_shell,
                "labels_frame": labels_frame,
                "rating_canvas": rating_canvas, "photo": None,
                "data_idx": -1, "canvas_w": canvas_w, "canvas_h": canvas_h,
                "slot_type": "std", "win_id": win_id,
            })

        self._vg_std_pool_size = pool_size

    # ------------------------------------------------------------------
    # 3. Pool creation — wide folder slots
    # ------------------------------------------------------------------

    def _vg_build_wide_pool(self, pool_size: int):
        for slot in self._vg_wide_pool:
            try:
                self.canvas.delete(slot["win_id"])
            except Exception:
                pass
            slot["frame"].destroy()
        self._vg_wide_pool.clear()

        strip_h = int(self.widefolder_size[1]) + 16
        bg_raw = getattr(self, "vg_wide_bg_color", None)
        card_bg = self._vg_safe_color(bg_raw, getattr(self, "folder_color_media", "#2a3a4a"))
        # Shell behind the rounded card must differ from card_bg; otherwise the rounded
        # corners visually disappear (looks rectangular until selection outline appears).
        shell_bg = self._vg_safe_color(
            getattr(self, "thumbBGColor", None),
            getattr(self, "BackroundColor", "#181a1d"),
        )
        border_raw = getattr(self, "vg_wide_border_color", None)
        border_c = self._vg_safe_color(border_raw, getattr(self, "wide_folder_borderColor", "#555555"))
        show_shadow = getattr(self, "vg_wide_show_shadow", True)
        shadow_off = int(getattr(self, "vg_wide_shadow_offset", 4)) if show_shadow else 0
        shadow_color = getattr(self, "vg_wide_shadow_color", "#080808")
        show_border = getattr(self, "vg_wide_show_border", True)
        border_w = int(getattr(self, "vg_wide_border_width", 1)) if show_border else 0
        left_px = 220
        sep_color = "#4a5056"
        muted = "#9aa4ad"
        kw_color = "#8ecae6"

        # Fonts (scaled via tuning params)
        base_title = getattr(self, "folder_title_font", ("Helvetica", 13, "bold"))
        title_scale = float(getattr(self, "vg_wide_title_scale", 2.5))
        try:
            fam = base_title.cget("family")
            sz = int(base_title.cget("size") or 13)
            weight = str(base_title.cget("weight") or "bold")
            title_font = (fam, max(10, int(round(sz * title_scale))), "bold" if weight.lower() == "bold" else weight)
        except Exception:
            # tuple fallback
            try:
                fam = base_title[0]
                sz = int(base_title[1])
                w = base_title[2] if len(base_title) > 2 else "bold"
                title_font = (fam, max(10, int(round(sz * title_scale))), w)
            except Exception:
                title_font = ("Helvetica", 32, "bold")
        stats_font = ("Helvetica", 10)
        try:
            stats_scale = float(getattr(self, "vg_wide_stats_scale", 1.0))
            sf = self.wide_folder_stats_font
            stats_font = (sf.cget("family"),
                          max(8, int(round(int(sf.cget("size")) * stats_scale))))
        except Exception:
            pass

        for _ in range(pool_size):
            outer = tk.Frame(self.canvas, bg=shell_bg,
                             height=strip_h)

            shadow = tk.Frame(outer, bg=shadow_color)
            if show_shadow and shadow_off > 0:
                shadow.place(x=shadow_off, y=shadow_off,
                             relwidth=1.0, relheight=1.0)
            else:
                try:
                    shadow.place_forget()
                except Exception:
                    pass

            strip = tk.Frame(outer, bg=shell_bg, bd=0, highlightthickness=0)
            strip.default_border_color = border_c
            strip.default_border_width = border_w

            strip_canvas = tk.Canvas(
                strip, bg=shell_bg, bd=0, highlightthickness=0)
            strip_canvas.place(x=0, y=0, relwidth=1.0, relheight=1.0)
            strip_canvas.bind("<Configure>", self._vg_on_wide_strip_canvas_configure)

            if shadow_off > 0:
                strip.place(x=0, y=0, relwidth=1.0, relheight=1.0,
                            width=-shadow_off, height=-shadow_off)
            else:
                strip.place(x=0, y=0, relwidth=1.0, relheight=1.0)

            left_panel = tk.Frame(strip, bg=card_bg, width=left_px)
            left_panel.place(x=10, y=8, width=left_px, relheight=1.0, height=-16)

            name_label = tk.Label(
                left_panel, text="", bg=card_bg, fg="#dbdee1",
                font=title_font, anchor="w", justify="left",
                wraplength=left_px - 10,
            )
            row_gap = int(getattr(self, "vg_wide_label_row_gap", 4))
            title_bot = getattr(self, "vg_wide_title_bottom_gap", None)
            title_pad = int(title_bot) if title_bot is not None else row_gap
            name_label.pack(side="top", anchor="w", pady=(0, title_pad))

            stats_label = tk.Label(
                left_panel, text="", bg=card_bg, fg=muted,
                font=stats_font, anchor="nw", justify="left",
                wraplength=left_px - 10,
            )
            stats_label.pack(side="top", anchor="w", pady=(0, row_gap))

            kw_label = tk.Label(
                left_panel, text="", bg=card_bg, fg=kw_color,
                font=stats_font, anchor="nw", justify="left",
                wraplength=left_px - 10,
            )
            kw_label.pack(side="top", anchor="w", pady=(0, row_gap))

            rating_canvas = tk.Canvas(
                left_panel, bg=card_bg, bd=0, highlightthickness=0,
                height=20, width=left_px - 10,
            )
            rating_canvas.pack(side="top", anchor="w", pady=(0, row_gap))
            rating_canvas.pack_forget()

            sep = tk.Frame(strip, bg=sep_color, width=1)
            if getattr(self, "vg_wide_show_divider", False):
                sep.place(x=left_px + 14, y=8, width=1, relheight=1.0, height=-16)

            img_canvas = tk.Canvas(strip, bg=card_bg, bd=0, highlightthickness=0)
            # We'll reposition "wideimg" with coords when slot geometry changes.
            img_canvas.create_image(0, 0, anchor="center", tags="wideimg")

            win_id = self.canvas.create_window(
                0, _OFFSCREEN_Y, window=outer, anchor="nw")

            slot_entry = {
                "frame": outer, "strip": strip, "shadow": shadow,
                "strip_canvas": strip_canvas,
                "left_panel": left_panel, "sep": sep,
                "name_label": name_label, "stats_label": stats_label,
                "kw_label": kw_label, "rating_canvas": rating_canvas,
                "img_canvas": img_canvas,
                "card_bg": card_bg,
                "photo": None, "data_idx": -1,
                "strip_h": strip_h, "left_px": left_px,
                "slot_type": "wide", "win_id": win_id,
            }
            self._vg_wide_pool.append(slot_entry)
            strip._vg_slot = slot_entry

        self._vg_wide_pool_size = pool_size

    # ------------------------------------------------------------------
    # 4. Geometry
    # ------------------------------------------------------------------

    @staticmethod
    def _vg_safe_color(c, fallback="#2d2d2d"):
        if isinstance(c, (tuple, list)):
            return c[1]
        return fallback if (not c or c == "transparent") else c

    def _vg_wide_cache_key(self, file_path: str, nprev: int) -> str:
        """Memory-cache key for generated wide strips, including size-sensitive inputs."""
        try:
            wide_w, wide_h = self.widefolder_size
        except Exception:
            wide_w, wide_h = 0, 0
        gap = int(getattr(self, "wide_folder_gap", 18))
        radius = int(getattr(self, "wide_folder_innerThumbRadius", 10))
        return (
            f"{file_path}\x00wide\x00n={int(nprev)}"
            f"\x00size={int(wide_w)}x{int(wide_h)}"
            f"\x00g={gap}\x00r={radius}"
        )

    @staticmethod
    def _vg_flatten_rgba_for_tk(img: Image.Image, bg_rgb: tuple[int, int, int]) -> Image.Image:
        """Tk PhotoImage treats RGBA holes as black on Windows; composite onto card color."""
        if img.mode == "RGBA":
            base = Image.new("RGB", img.size, bg_rgb)
            base.paste(img, mask=img.split()[3])
            return base
        if img.mode != "RGB":
            return img.convert("RGB")
        return img

    def _vg_on_wide_strip_canvas_configure(self, event: tk.Event) -> None:
        strip = event.widget.master
        slot = getattr(strip, "_vg_slot", None)
        if slot and event.widget.winfo_width() > 2 and event.widget.winfo_height() > 2:
            self._vg_redraw_wide_card(slot)

    def _vg_redraw_wide_card(self, slot: dict) -> None:
        """Draw rounded wide-folder container (fill + outline + selection) on strip_canvas."""
        sc = slot.get("strip_canvas")
        if not sc:
            return
        try:
            w, h = sc.winfo_width(), sc.winfo_height()
        except Exception:
            return
        if w < 4 or h < 4:
            return
        try:
            sc.delete("vgcard")
        except Exception:
            return

        card_bg = self._vg_safe_color(
            getattr(self, "vg_wide_bg_color", None),
            getattr(self, "folder_color_media", "#2a3a4a"),
        )
        show_border = getattr(self, "vg_wide_show_border", True)
        bw = int(getattr(self, "vg_wide_border_width", 1)) if show_border else 0
        border_raw = getattr(self, "vg_wide_border_color", None)
        border_c = self._vg_safe_color(
            border_raw, getattr(self, "wide_folder_borderColor", "#555555"))

        idx = int(slot.get("data_idx", -1))
        selected = False
        for item in getattr(self, "selected_thumbnails", []):
            if isinstance(item, (list, tuple)) and len(item) > 2 and int(item[2]) == idx:
                selected = True
                break

        if selected:
            outline_c = self.thumbSelColor
            ow = max(int(getattr(self, "Select_outlinewidth", 2) or 2), 2)
        elif show_border and bw > 0:
            outline_c = border_c
            ow = max(bw, 1)
        else:
            outline_c = card_bg
            ow = 0

        inset = max(1, (ow + 1) // 2) if ow else 1
        x1 = y1 = float(inset)
        x2, y2 = float(w - 1 - inset), float(h - 1 - inset)
        if x2 <= x1 + 2 or y2 <= y1 + 2:
            return

        round_on = getattr(self, "vg_wide_round_container", True)
        _rr_pref = getattr(self, "wide_folder_cornerRadius", None)
        rr_req = int(
            _rr_pref if _rr_pref is not None else getattr(self, "vg_wide_container_corner_radius", 10)
        )
        max_rr = max(2, int(min(x2 - x1, y2 - y1) // 2) - 1)
        r = max(0, min(rr_req, max_rr)) if round_on else 0

        try:
            if r >= 2 and round_on:
                self.create_rounded_rectangle(
                    sc, int(x1), int(y1), int(x2), int(y2), radius=int(r),
                    fill=card_bg, outline=outline_c, width=ow, tags="vgcard",
                )
            else:
                sc.create_rectangle(
                    int(x1), int(y1), int(x2), int(y2),
                    fill=card_bg,
                    outline=outline_c if ow else card_bg,
                    width=ow,
                    tags="vgcard",
                )
        except Exception as e:
            logging.debug("[VGrid] wide card draw: %s", e, exc_info=True)

    def _vg_sync_wide_strip_border(self, strip: tk.Frame) -> None:
        """Refresh wide card chrome (canvas polygon) or legacy tk highlight."""
        slot = getattr(strip, "_vg_slot", None)
        if slot and slot.get("strip_canvas"):
            show_border = getattr(self, "vg_wide_show_border", True)
            bw = int(getattr(self, "vg_wide_border_width", 1)) if show_border else 0
            border_raw = getattr(self, "vg_wide_border_color", None)
            border_c = self._vg_safe_color(
                border_raw, getattr(self, "wide_folder_borderColor", "#555555"))
            try:
                strip.default_border_width = bw
                ombg = strip.cget("bg")
                strip.default_border_color = border_c if show_border else ombg
            except Exception:
                pass
            self._vg_redraw_wide_card(slot)
            return

        show_border = getattr(self, "vg_wide_show_border", True)
        bw = int(getattr(self, "vg_wide_border_width", 1)) if show_border else 0
        border_raw = getattr(self, "vg_wide_border_color", None)
        border_c = self._vg_safe_color(
            border_raw, getattr(self, "wide_folder_borderColor", "#555555"))
        try:
            bg = strip.cget("bg")
        except Exception:
            bg = self._vg_safe_color(
                getattr(self, "vg_wide_bg_color", None),
                getattr(self, "folder_color_media", "#2a3a4a"),
            )
        hl = border_c if show_border and bw > 0 else bg
        try:
            strip.configure(
                highlightthickness=bw,
                highlightbackground=hl,
                highlightcolor=hl,
            )
            strip.default_border_width = bw
            strip.default_border_color = border_c if show_border else bg
        except Exception:
            pass

    def _vg_sync_wide_divider(self, slot: dict, left_px: int) -> None:
        """Show/hide vertical rule; must run even when geom_changed is False."""
        sep = slot.get("sep")
        if not sep:
            return
        try:
            if getattr(self, "vg_wide_show_divider", False):
                sep.place(x=left_px + 14, y=8, width=1, relheight=1.0, height=-16)
            else:
                sep.place_forget()
        except Exception:
            pass

    def _vg_compute_wide_left_px(self, strip_w: int) -> int:
        """Match _vg_bind_wide_slot left panel width (for divider sync on layout)."""
        show_div = getattr(self, "vg_wide_show_divider", False)
        left_px = max(160, min(420, int(round(strip_w * 0.25))))
        pad_side = int(getattr(self, "vg_wide_preview_pad_side", _WIDE_PREVIEW_PAD_SIDE))
        if show_div:
            sep_x = left_px + 14
        else:
            sep_x = 10 + left_px
        min_preview_w = 140
        if (strip_w - (sep_x + pad_side)) < min_preview_w:
            if show_div:
                left_px = max(160, strip_w - (14 + pad_side + min_preview_w))
            else:
                left_px = max(160, strip_w - (10 + pad_side + min_preview_w))
        return left_px

    def _vg_apply_wide_outer_chrome(self, slot: dict, strip_inner_w: int) -> None:
        """shadow.place + strip.place + border + divider from current vg_wide_* flags."""
        shadow = slot.get("shadow")
        strip = slot.get("strip")
        if not strip:
            return
        show_shadow = getattr(self, "vg_wide_show_shadow", True)
        shadow_off = int(getattr(self, "vg_wide_shadow_offset", 4)) if show_shadow else 0
        shadow_color = getattr(self, "vg_wide_shadow_color", "#080808")
        if shadow:
            try:
                shadow.configure(bg=shadow_color)
            except Exception:
                pass
            try:
                if show_shadow and shadow_off > 0:
                    shadow.place(x=shadow_off, y=shadow_off,
                                 relwidth=1.0, relheight=1.0)
                    strip.place(x=0, y=0, relwidth=1.0, relheight=1.0,
                                width=-shadow_off, height=-shadow_off)
                else:
                    try:
                        shadow.place_forget()
                    except Exception:
                        pass
                    strip.place(x=0, y=0, relwidth=1.0, relheight=1.0)
            except Exception:
                pass
        self._vg_sync_wide_strip_border(strip)
        self._vg_sync_wide_divider(slot, self._vg_compute_wide_left_px(strip_inner_w))

    def _vg_sync_std_caption_shell_heights(self, label_h: int | None = None):
        """Same fixed caption band height for every std slot (gray band even with fewer lines)."""
        h = int(label_h if label_h is not None else getattr(self, "_vg_dynamic_label_h", 48))
        h = max(34, h)
        for slot in getattr(self, "_vg_std_pool", []):
            shell = slot.get("caption_shell")
            if not shell:
                continue
            try:
                if shell.winfo_exists():
                    shell.configure(height=h)
            except Exception:
                pass

    def _vg_recalc(self):
            thumb_w, thumb_h = self.thumbnail_size
            border = self.effective_thumb_border_size()
            padding = self.effective_thumb_cell_padding()
            
            # --- Výška popisku: pixely (oddělené labely + pady + zalamování) ---
            label_h = int(getattr(self, "_vg_dynamic_label_h", 40))
            if label_h < 34:
                lines = getattr(self, "_vg_dynamic_label_lines", 1)
                row_px = int(getattr(self, "vg_std_label_line_px", 22))
                block_pad = int(getattr(self, "vg_std_label_block_pad", 10))
                label_h = max(34, lines * row_px + block_pad)
            # -------------------------------------

            cell_w = thumb_w + border * 2 + padding * 2
            cell_h = thumb_h + border * 2 + 10 + padding * 2 + label_h

            raw_w = int(self.canvas.winfo_width())
            raw_h = int(self.canvas.winfo_height())
            canvas_h = raw_h if raw_h >= 100 else 600
            if raw_w < 100:
                canvas_w = 800
            else:
                canvas_w = raw_w

            min_w_stable = max(120, cell_w + 8)
            last_c = int(getattr(self, "_vg_last_good_cols", 0) or 0)
            last_w = int(getattr(self, "_vg_last_good_canvas_w", 0) or 0)
            # Sudden collapse (pane repair) vs user slowly narrowing: only reuse cols if width
            # dropped sharply from a previously sane value.
            glitch_width = (
                raw_w < min_w_stable
                and last_c >= 1
                and last_w >= min_w_stable
                and (raw_w + 120 < last_w)
            )
            if glitch_width:
                self._vg_cols = last_c
                logging.info(
                    "[VGrid] recalc: raw_canvas_w=%d < stable_min=%d — keeping cols=%d (last_good_w=%d)",
                    raw_w,
                    min_w_stable,
                    last_c,
                    last_w,
                )
            else:
                eff_w = canvas_w if raw_w < 100 else raw_w
                self._vg_cols = max(1, int(eff_w) // cell_w)
                if raw_w >= min_w_stable:
                    self._vg_last_good_cols = self._vg_cols
                    self._vg_last_good_canvas_w = raw_w

            self._vg_row_height = cell_h
            self._vg_canvas_h = canvas_h
            inner = int(self.widefolder_size[1]) + 16
            gap = int(getattr(self, "vg_wide_inter_row_gap", 10))
            apy = int(getattr(self, "vg_wide_outer_pad_y", 0))
            self._vg_wide_strip_inner_h = inner
            self._vg_wide_row_height = inner + gap + 2 * apy
            self._vg_visible_rows = max(1, math.ceil(canvas_h / cell_h))
            self.columns = self._vg_cols

            if self._vg_is_wide and self._vg_folder_count > 0:
                wide_cols = max(1, getattr(self, "numwidefolders_in_col", 2))
                self._vg_wide_rows = math.ceil(self._vg_folder_count / wide_cols)
                self._vg_wide_cols = wide_cols
                file_count = len(self._vg_data) - self._vg_folder_count
                self._vg_file_rows = max(0, math.ceil(file_count / self._vg_cols))
            else:
                self._vg_wide_rows = 0
                self._vg_wide_cols = 1
                self._vg_file_rows = max(1, math.ceil(len(self._vg_data) / self._vg_cols))

            wide_total_h = self._vg_wide_rows * self._vg_wide_row_height
            file_total_h = self._vg_file_rows * cell_h
            self._vg_total_h = wide_total_h + file_total_h
            self._vg_wide_section_h = wide_total_h

            needed_std = (self._vg_visible_rows + _OVERSCAN_ROWS * 2 + 1) * self._vg_cols
            if needed_std > self._vg_std_pool_size:
                self._vg_build_std_pool(needed_std)

            if self._vg_is_wide:
                vis_wide = max(1, math.ceil(canvas_h / self._vg_wide_row_height))
                needed_wide = (vis_wide + _OVERSCAN_ROWS * 2 + 1) * self._vg_wide_cols
                if needed_wide > self._vg_wide_pool_size:
                    self._vg_build_wide_pool(needed_wide)

            self._vg_sync_std_caption_shell_heights(label_h)

            sr_h = self._vg_total_h + cell_h
            self._vg_scrollregion_h = sr_h
            self.canvas.configure(scrollregion=(0, 0, canvas_w, sr_h))
            logging.info(
                "[VGrid] recalc: items=%d cols=%d file_rows=%d cell_w=%d rh=%d data_h=%d sr_h=%d "
                "canvas_w=%d(raw=%d) canvas_h=%d",
                len(self._vg_data),
                self._vg_cols,
                self._vg_file_rows,
                int(cell_w),
                self._vg_row_height,
                int(self._vg_total_h),
                int(sr_h),
                canvas_w,
                raw_w,
                canvas_h,
            )

    # ------------------------------------------------------------------
    # 5. Scrollbar — use native canvas yview
    # ------------------------------------------------------------------

    def _vg_wire_scrollbar(self):
        self.canvas.configure(yscrollcommand=self._vg_on_scroll_changed)
        self.scrollbar.configure(command=self._vg_on_scrollbar_cmd)

    def _vg_unwire_scrollbar(self):
        self.scrollbar.configure(command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

    def _vg_on_scrollbar_cmd(self, *args):
        self.canvas.yview(*args)

    def _vg_on_scroll_changed(self, first, last):
        self.scrollbar.set(first, last)
        self._vg_check_visible()

    def _vg_on_mousewheel(self, event):
        if event.num == 4:
            delta = 1
        elif event.num == 5:
            delta = -1
        else:
            delta = event.delta
        if delta == 0:
            return "break"
        direction = -1 if delta > 0 else 1
        notches = max(1, abs(delta) // 120) if abs(delta) >= 120 else 1
        # Clamp wheel scrolling at edges; on some systems wheel can momentarily
        # drive canvas to a visual gap above row 0 although scrollbar remains sane.
        try:
            first, last = self.canvas.yview()
            if direction < 0 and first <= 0.0:
                return "break"
            if direction > 0 and last >= 1.0:
                return "break"
        except Exception:
            pass
        self.canvas.yview_scroll(direction * notches * 3, "units")
        return "break"

    # ------------------------------------------------------------------
    # 6. Visible-range check & slot layout
    # ------------------------------------------------------------------

    def _vg_check_visible(self):
        if not self._vg_active:
            return
        top_frac = self.canvas.yview()[0]
        scroll_px = top_frac * self._vg_scrollregion_h
        rh = self._vg_row_height
        wrh = self._vg_wide_row_height
        wide_h = self._vg_wide_section_h

        if self._vg_is_wide and self._vg_wide_rows > 0:
            first_wide = max(0, int(scroll_px / wrh)) if scroll_px < wide_h else -1
        else:
            first_wide = -1

        scroll_in_files = max(0.0, scroll_px - wide_h)
        first_file = int(scroll_in_files / rh) if rh > 0 else 0

        key = (first_wide, first_file)
        if key == self._vg_last_first_row:
            return
        self._vg_last_first_row = key
        self._vg_layout_slots(scroll_px)

    def _vg_layout_slots(self, scroll_px):
        """Modulo-mapped slot layout using canvas.coords / canvas.itemconfigure.

        Each content row maps to a fixed pool row via modulo, so scrolling
        by one row only rebinds/repositions ~cols slots instead of all.
        Slots whose data_idx is unchanged AND position hasn't changed are
        completely skipped.
        """
        padding = self.effective_thumb_cell_padding()
        canvas_h = self._vg_canvas_h
        rh = self._vg_row_height
        wrh = self._vg_wide_row_height
        wide_h = self._vg_wide_section_h

        # ── Wide folder slots (modulo mapping) ─────────────────────
        used_wide_slots = set()
        pad_x = int(getattr(self, "vg_wide_outer_pad_x", 0))
        pad_y = int(getattr(self, "vg_wide_outer_pad_y", 0))
        inner_h = int(getattr(self, "_vg_wide_strip_inner_h", int(self.widefolder_size[1]) + 16))

        if self._vg_is_wide and self._vg_wide_rows > 0:
            wcols = self._vg_wide_cols
            cw = max(100, self.canvas.winfo_width())
            wg_raw = getattr(self, "vg_wide_canvas_gutter", None)
            if wg_raw is None:
                wide_gutter = int(self.effective_thumb_cell_padding()) + 2
            else:
                wide_gutter = int(wg_raw)
            cw_inner = max(50, cw - 2 * wide_gutter)
            strip_w = (cw_inner - padding * 2) // wcols - padding
            pool_wrows = max(1, self._vg_wide_pool_size // wcols)
            show_shadow = getattr(self, "vg_wide_show_shadow", True)
            shadow_off = int(getattr(self, "vg_wide_shadow_offset", 4)) if show_shadow else 0

            vis_first = max(0, int(scroll_px / wrh) - _OVERSCAN_ROWS) if scroll_px < wide_h else self._vg_wide_rows
            vis_last = min(self._vg_wide_rows - 1,
                           int((scroll_px + canvas_h) / wrh) + _OVERSCAN_ROWS)

            vis_wrows = vis_last - vis_first + 1
            pool_wrows = max(vis_wrows, pool_wrows)

            for wr in range(vis_first, vis_last + 1):
                abs_y = wr * wrh
                for wc in range(wcols):
                    pool_idx = (wr % pool_wrows) * wcols + wc
                    if pool_idx >= self._vg_wide_pool_size:
                        continue
                    used_wide_slots.add(pool_idx)
                    data_idx = wr * wcols + wc
                    slot = self._vg_wide_pool[pool_idx]
                    win_w = max(1, strip_w - 2 * pad_x)
                    # Binder uses inner geometry; shadow inset matches pool build.
                    content_w = max(1, win_w - shadow_off)
                    content_h = max(1, inner_h - shadow_off)
                    slot["_strip_w"] = content_w
                    slot["_strip_h"] = content_h
                    if data_idx < self._vg_folder_count:
                        self._vg_apply_wide_outer_chrome(slot, content_w)
                        changed = (slot["data_idx"] != data_idx)
                        # Also update layout if strip width/height changed.
                        geom_changed = (
                            (slot.get("_geom_w") != content_w)
                            or (slot.get("_geom_h") != content_h)
                        )
                        if changed or geom_changed:
                            self._vg_bind_wide_slot(slot, data_idx)
                        x = wc * (strip_w + padding) + padding + wide_gutter + pad_x
                        y_win = abs_y + pad_y
                        place_key = (x, y_win, win_w, inner_h)
                        if changed or slot.get("_wide_place_key") != place_key:
                            self.canvas.coords(slot["win_id"], x, y_win)
                            self.canvas.itemconfigure(slot["win_id"],
                                                      width=win_w,
                                                      height=inner_h)
                            slot["_wide_place_key"] = place_key
                            slot["_last_y"] = abs_y
                    else:
                        if slot["data_idx"] != -1:
                            self._vg_forget_slot_path(slot, self._vg_visible_wide_slots_by_path)
                            self.canvas.coords(slot["win_id"], 0, _OFFSCREEN_Y)
                            slot["data_idx"] = -1
                            slot["_last_y"] = _OFFSCREEN_Y

        for i in range(self._vg_wide_pool_size):
            if i not in used_wide_slots and self._vg_wide_pool[i]["data_idx"] != -1:
                self._vg_forget_slot_path(self._vg_wide_pool[i], self._vg_visible_wide_slots_by_path)
                self.canvas.coords(self._vg_wide_pool[i]["win_id"], 0, _OFFSCREEN_Y)
                self._vg_wide_pool[i]["data_idx"] = -1
                self._vg_wide_pool[i]["_last_y"] = _OFFSCREEN_Y

        # ── Standard file slots (modulo mapping) ───────────────────
        cols = self._vg_cols
        file_start_idx = self._vg_folder_count if self._vg_is_wide else 0
        scroll_in_files = max(0.0, scroll_px - wide_h)
        first_file = max(0, (int(scroll_in_files / rh) if rh > 0 else 0) - _OVERSCAN_ROWS)
        vis_last_fr = min(self._vg_file_rows - 1,
                          first_file + int(canvas_h / rh) + _OVERSCAN_ROWS * 2 + 1)

        vis_rows = vis_last_fr - first_file + 1
        pool_rows = max(vis_rows, self._vg_std_pool_size // max(1, cols))

        used_std_slots = set()
        for fr in range(first_file, max(first_file, vis_last_fr + 1)):
            abs_y = wide_h + fr * rh
            for fc in range(cols):
                pool_idx = (fr % pool_rows) * cols + fc
                if pool_idx >= self._vg_std_pool_size:
                    continue
                used_std_slots.add(pool_idx)
                slot = self._vg_std_pool[pool_idx]
                data_idx = file_start_idx + fr * cols + fc
                if data_idx < len(self._vg_data):
                    changed = (slot["data_idx"] != data_idx)
                    if changed:
                        self._vg_bind_slot(slot, data_idx)
                    # Safety: ensure click/selection bindings exist even if data_idx didn't change
                    # (e.g. after a backup/undo or partial pool reuse).
                    if not slot.get("_events_bound"):
                        try:
                            self._vg_bind_slot_events(slot)
                            slot["_events_bound"] = True
                        except Exception:
                            pass
                    x = fc * (slot["canvas_w"] + padding * 2) + padding
                    if changed or slot.get("_last_y") != abs_y:
                        self.canvas.coords(slot["win_id"], x, abs_y)
                        slot["_last_y"] = abs_y
                else:
                    if slot["data_idx"] != -1:
                        self._vg_forget_slot_path(slot, self._vg_visible_std_slots_by_path)
                        self.canvas.coords(slot["win_id"], 0, _OFFSCREEN_Y)
                        slot["data_idx"] = -1
                        slot["_last_y"] = _OFFSCREEN_Y

        for i in range(self._vg_std_pool_size):
            if i not in used_std_slots and self._vg_std_pool[i]["data_idx"] != -1:
                self._vg_forget_slot_path(self._vg_std_pool[i], self._vg_visible_std_slots_by_path)
                self.canvas.coords(self._vg_std_pool[i]["win_id"], 0, _OFFSCREEN_Y)
                self._vg_std_pool[i]["data_idx"] = -1
                self._vg_std_pool[i]["_last_y"] = _OFFSCREEN_Y

        self._vg_reapply_selection()

    # ------------------------------------------------------------------
    # 6b. Standard slot labels (multi-line colors + live keyword updates)
    # ------------------------------------------------------------------

    @staticmethod
    def _vg_norm_path(file_path: str) -> str:
        return os.path.normcase(os.path.normpath(file_path or ""))

    def _vg_file_info_state_key(self) -> tuple:
        keys = ("name", "path", "file_size", "date_time", "dimensions", "keywords")
        state = []
        vars_map = getattr(self, "file_info_vars", {})
        for key in keys:
            try:
                state.append((key, bool(vars_map.get(key).get())))
            except Exception:
                state.append((key, False))
        return tuple(state)

    def _vg_get_label_font(self, font_size: int) -> tkfont.Font | None:
        font_size = int(font_size)
        if font_size not in self._vg_label_font_cache:
            try:
                self._vg_label_font_cache[font_size] = tkfont.Font(font=("Helvetica", font_size))
            except Exception:
                self._vg_label_font_cache[font_size] = None
        return self._vg_label_font_cache[font_size]

    def _vg_get_item_info_parts(self, item: dict) -> list[tuple[str, str]]:
        state_key = self._vg_file_info_state_key()
        if item.get("_vg_info_key") == state_key and "_vg_info_parts" in item:
            return item.get("_vg_info_parts") or []
        try:
            parts = self.joininfotexts(
                item["path"],
                item["name"],
                db_entry=item.get("_vg_db_entry"),
            )
        except Exception:
            parts = []
        item["_vg_info_key"] = state_key
        item["_vg_info_parts"] = parts
        return parts

    def _vg_index_data(self) -> None:
        self._vg_data_index_by_path = {}
        for idx, item in enumerate(self._vg_data):
            try:
                self._vg_data_index_by_path[self._vg_norm_path(item["path"])] = idx
            except Exception:
                continue

    def _vg_forget_slot_path(self, slot: dict, slot_map: dict[str, dict]) -> None:
        old_idx = int(slot.get("data_idx", -1))
        if old_idx < 0 or old_idx >= len(self._vg_data):
            return
        try:
            old_key = self._vg_norm_path(self._vg_data[old_idx]["path"])
        except Exception:
            return
        if slot_map.get(old_key) is slot:
            slot_map.pop(old_key, None)

    def _vg_get_item_photo(self, item: dict, file_path: str, force_rebuild: bool = False):
        photo = item.get("_photo")
        if photo is not None and not force_rebuild:
            return photo
        if not self.memory_cache:
            return None
        cached = thumbnail_cache.get(file_path, memory_cache=self.memory_cache)
        if cached is None:
            return None
        source = getattr(cached, "_light_image", None)
        if source is None:
            return None
        target_size = tuple(self.thumbnail_size)
        cache_key = (id(source), getattr(source, "size", None), target_size)
        if item.get("_photo_cache_key") == cache_key and item.get("_photo") is not None:
            return item["_photo"]
        if getattr(source, "size", None) == target_size:
            resized = source
        else:
            resized = ImageOps.contain(source, target_size)
        photo = ImageTk.PhotoImage(resized)
        item["_photo"] = photo
        item["_photo_cache_key"] = cache_key
        return photo

    def _vg_warm_item_metadata(self) -> None:
        paths = [item.get("path") for item in self._vg_data if item.get("path")]
        entries = {}
        try:
            getter = getattr(self.database, "get_entries_bulk", None)
            if callable(getter):
                entries = getter(paths)
        except Exception as e:
            logging.debug("[VGrid] bulk metadata load failed: %s", e)
            entries = {}

        self._vg_info_state_key = self._vg_file_info_state_key()
        for item in self._vg_data:
            fp = item.get("path")
            if not fp:
                continue
            entry = entries.get(self.database.normalize_path(fp)) if entries else None
            item["_vg_db_entry"] = entry
            try:
                item["_rating"] = int(entry.get("rating") or 0) if entry else 0
            except Exception:
                item["_rating"] = 0
            try:
                item["_vg_info_parts"] = self.joininfotexts(fp, item["name"], db_entry=entry)
            except Exception:
                item["_vg_info_parts"] = []
            item["_vg_info_key"] = self._vg_info_state_key

    @staticmethod
    def _vg_wrap_text_lines(text: str, font_obj: tkfont.Font, wrap_px: int) -> list[str]:
        """Return wrapped lines using real font measurement."""
        if font_obj is None:
            return [text or ""]
        max_w = max(8, int(wrap_px))
        raw = (text or "").strip()
        if not raw:
            return [""]

        lines_out: list[str] = []
        for segment in raw.split("\n"):
            seg = segment.strip()
            if not seg:
                lines_out.append("")
                continue
            words = seg.split()
            if not words:
                lines_out.append("")
                continue

            line = ""
            for word in words:
                trial = word if not line else f"{line} {word}"
                if font_obj.measure(trial) <= max_w:
                    line = trial
                    continue
                if line:
                    lines_out.append(line)
                    line = ""
                if font_obj.measure(word) <= max_w:
                    line = word
                    continue
                chunk = ""
                for ch in word:
                    trial_chunk = f"{chunk}{ch}"
                    if chunk and font_obj.measure(trial_chunk) > max_w:
                        lines_out.append(chunk)
                        chunk = ch
                    else:
                        chunk = trial_chunk
                line = chunk
            lines_out.append(line if line else "")
        return lines_out or [""]

    @staticmethod
    def _vg_clamp_wrapped_text(text: str, font_obj: tkfont.Font, wrap_px: int, max_lines: int) -> str:
        """Wrap text and clamp to max lines with ellipsis."""
        max_lines = max(1, int(max_lines))
        lines = VtpVirtualGridMixin._vg_wrap_text_lines(text, font_obj, wrap_px)
        if len(lines) <= max_lines:
            return "\n".join(lines)
        kept = lines[:max_lines]
        last = kept[-1].rstrip()
        ell = "..."
        max_w = max(8, int(wrap_px))
        if font_obj is None:
            kept[-1] = (last + ell) if last else ell
            return "\n".join(kept)
        if not last:
            kept[-1] = ell
            return "\n".join(kept)
        while last and font_obj.measure(last + ell) > max_w:
            last = last[:-1].rstrip()
        kept[-1] = (last + ell) if last else ell
        return "\n".join(kept)

    def _vg_measure_item_label_height_px(
        self,
        fp: str,
        name: str,
        is_folder: bool,
        thumb_w: int,
        parts: list[tuple[str, str]] | None = None,
    ) -> int:
        """Estimate stacked caption height using actual Tk font metrics."""
        top, bot = getattr(self, "vg_std_label_row_pady", (9, 3))
        pad_block = top + bot
        fudge = int(getattr(self, "vg_std_label_height_fudge", 14))
        line_reserve = int(getattr(self, "vg_std_label_line_reserve_px", 1))
        stack_margin = int(getattr(self, "vg_std_label_stack_margin", 6))
        try:
            fs = int(self._get_effective_thumb_font_size())
        except Exception:
            fs = 11
        wrap_w = max(8, int(thumb_w))
        lbl_font = self._vg_get_label_font(fs)
        if parts is None:
            try:
                data_idx = self._vg_data_index_by_path.get(self._vg_norm_path(fp), -1)
                if 0 <= data_idx < len(self._vg_data):
                    parts = self._vg_get_item_info_parts(self._vg_data[data_idx])
                else:
                    parts = self.joininfotexts(fp, name)
            except Exception:
                parts = []
        label_texts = tuple(text for text, _ in (parts or [])) or (name,)
        cache_key = (
            label_texts,
            fs,
            wrap_w,
            pad_block,
            fudge,
            line_reserve,
            stack_margin,
            self._vg_file_info_state_key(),
        )
        cached_h = self._vg_label_measure_cache.get(cache_key)
        if cached_h is not None:
            return cached_h
        # Adaptive cap keeps last info rows visible when many metadata lines are enabled.
        cap = self._vg_effective_label_cap_px(thumb_w, info_rows=max(1, len(parts)))
        if not parts:
            display = self._vg_clamp_wrapped_text(name, lbl_font, wrap_w, max_lines=2)
            line_h = max(1, int(lbl_font.metrics("linespace"))) if lbl_font else max(17, int(round(fs * 1.68)))
            line_count = max(1, display.count("\n") + 1)
            text_h = line_count * (line_h + line_reserve)
            measured = min(cap, max(34, text_h + pad_block + fudge))
            self._vg_label_measure_cache[cache_key] = measured
            return measured
        total = 0
        line_h = max(1, int(lbl_font.metrics("linespace"))) if lbl_font else max(17, int(round(fs * 1.68)))
        for idx, (text, _) in enumerate(parts):
            max_lines = 2 if (idx == 0 and text == name) else 1
            display = self._vg_clamp_wrapped_text(text, lbl_font, wrap_w, max_lines=max_lines)
            line_count = max(1, display.count("\n") + 1)
            text_h = line_count * (line_h + line_reserve)
            total += text_h + pad_block
        stack_extra = max(0, len(parts) - 1) * stack_margin
        measured = min(cap, max(34, total + fudge + stack_extra))
        self._vg_label_measure_cache[cache_key] = measured
        return measured

    def _vg_effective_label_cap_px(self, thumb_w: int, info_rows: int = 1) -> int:
        """
        Effective caption cap for current info-density.
        Prevents global clipping when many info rows are enabled.
        """
        base_cap = int(getattr(self, "vg_std_label_max_px", 280))
        tw = max(1, int(thumb_w))
        rows = max(1, int(info_rows))

        # Count active options from menu state (excluding helper toggle).
        enabled_fields = 0
        try:
            for key, var in getattr(self, "file_info_vars", {}).items():
                if key == "all_fields":
                    continue
                if bool(var.get()):
                    enabled_fields += 1
        except Exception:
            enabled_fields = 0

        # Use stronger cap when grid is dense with text metadata.
        density = max(rows, enabled_fields)
        if density >= 5:
            return max(base_cap, 620)
        if density >= 4:
            return max(base_cap, 520)
        if density >= 3:
            return max(base_cap, 420 if tw <= 240 else 380)
        return base_cap

    def _vg_fill_std_labels_frame(self, slot: dict, file_path: str, file_name: str, is_folder: bool):
        frame = slot["labels_frame"]
        for w in list(frame.winfo_children()):
            w.destroy()
        bg = self._vg_safe_color(
            getattr(self, "BackroundColor", None),
            self._vg_safe_color(self.labelBGColor),
        )
        thumb_w = self.thumbnail_size[0]
        font_size = self._get_effective_thumb_font_size()
        lbl_font = ("Helvetica", font_size)
        top, bot = getattr(self, "vg_std_label_row_pady", (9, 3))
        measure_font = self._vg_get_label_font(int(font_size))
        try:
            data_idx = self._vg_data_index_by_path.get(self._vg_norm_path(file_path), -1)
            if 0 <= data_idx < len(self._vg_data):
                parts = self._vg_get_item_info_parts(self._vg_data[data_idx])
            else:
                parts = self.joininfotexts(file_path, file_name)
        except Exception:
            parts = []
        if not parts:
            parts = [(file_name, "#7f848a" if is_folder else "gray70")]
        first_lbl = None
        for idx, (text, color) in enumerate(parts):
            max_lines = 2 if (idx == 0 and text == file_name) else 1
            display = self._vg_clamp_wrapped_text(text, measure_font, thumb_w, max_lines=max_lines)
            lbl = tk.Label(
                frame,
                text=display,
                bg=bg,
                fg=color,
                font=lbl_font,
                wraplength=thumb_w,
                anchor="center",
                justify="center",
            )
            lbl.pack(pady=(top, bot))
            if first_lbl is None:
                first_lbl = lbl
        slot["_first_label_widget"] = first_lbl

    def _vg_refresh_file_labels(self, file_path: str):
        """Rebuild under-thumb captions for one file (e.g. after keyword edit). Virtual grid only."""
        if not getattr(self, "_vg_active", False) or not self._vg_data:
            return
        norm = self._vg_norm_path(file_path)
        data_idx = self._vg_data_index_by_path.get(norm, -1)
        if data_idx < 0:
            return
        item = self._vg_data[data_idx]
        item.pop("_vg_info_parts", None)
        item.pop("_vg_info_key", None)
        try:
            item["_vg_db_entry"] = self.database.get_entry(item["path"])
        except Exception:
            item["_vg_db_entry"] = None
        self._vg_label_measure_cache.clear()
        thumb_w = self.thumbnail_size[0]
        item["_vg_label_h_px"] = self._vg_measure_item_label_height_px(
            item["path"],
            item["name"],
            item.get("is_folder", False),
            thumb_w,
            parts=self._vg_get_item_info_parts(item),
        )
        for slot in self._vg_std_pool:
            if slot["data_idx"] == data_idx:
                self._vg_fill_std_labels_frame(
                    slot, item["path"], item["name"], item.get("is_folder", False)
                )
                try:
                    self.thumbnail_labels[item["path"]] = {
                        "row": data_idx // max(1, self._vg_cols),
                        "col": data_idx % max(1, self._vg_cols),
                        "index": data_idx,
                        "canvas": slot["canvas"],
                        "label": slot.get("_first_label_widget"),
                    }
                except Exception:
                    pass
        self._vg_recompute_scroll_layout_if_label_height_changed()

    def _vg_recompute_scroll_layout_if_label_height_changed(self):
        thumb_w = self.thumbnail_size[0]
        max_rows = 1
        try:
            for it in self._vg_data:
                parts = self._vg_get_item_info_parts(it)
                max_rows = max(max_rows, max(1, len(parts) if parts else 1))
        except Exception:
            pass
        cap_px = self._vg_effective_label_cap_px(thumb_w, info_rows=max_rows)
        max_h = 34
        for it in self._vg_data:
            h = it.get("_vg_label_h_px")
            if h is None:
                h = self._vg_measure_item_label_height_px(
                    it["path"],
                    it["name"],
                    it.get("is_folder", False),
                    thumb_w,
                    parts=self._vg_get_item_info_parts(it),
                )
                it["_vg_label_h_px"] = h
            max_h = max(max_h, int(h))
        max_h = min(max_h, cap_px)
        old = int(getattr(self, "_vg_dynamic_label_h", 34))
        if max_h == old:
            return
        self._vg_dynamic_label_h = max_h
        self._vg_recalc()
        try:
            top_frac = float(self.canvas.yview()[0])
        except Exception:
            top_frac = 0.0
        scroll_px = top_frac * self._vg_scrollregion_h
        self._vg_last_first_row = None
        self._vg_layout_slots(scroll_px)

    # ------------------------------------------------------------------
    # 7. Standard slot binding
    # ------------------------------------------------------------------

    def _vg_bind_slot(self, slot: dict, data_idx: int):
        if slot["data_idx"] == data_idx:
            try:
                self._vg_visible_std_slots_by_path[
                    self._vg_norm_path(self._vg_data[data_idx]["path"])
                ] = slot
            except Exception:
                pass
            return
        self._vg_forget_slot_path(slot, self._vg_visible_std_slots_by_path)
        slot["data_idx"] = data_idx
        item = self._vg_data[data_idx]
        file_path, file_name = item["path"], item["name"]
        is_folder = item.get("is_folder", False)
        self._vg_visible_std_slots_by_path[self._vg_norm_path(file_path)] = slot

        canvas = slot["canvas"]
        canvas.file_path = file_path
        canvas.is_folder = is_folder

        photo = self._vg_get_item_photo(item, file_path)
        if photo:
            canvas.itemconfig("thumbnail", image=photo)
            canvas.image = photo
            slot["photo"] = photo
        else:
            canvas.itemconfig("thumbnail", image="")
            canvas.image = None
            slot["photo"] = None

        self._vg_fill_std_labels_frame(slot, file_path, file_name, is_folder)

        border_items = canvas.find_withtag("border")
        if border_items:
            canvas.itemconfig(border_items[0],
                              outline="gold" if is_folder else self.thumbBorderColor)

        rating = item.get("_rating", 0)
        rc = slot["rating_canvas"]
        if rating and rating > 0:
            colors = ["lightblue", "lightgreen", "yellow", "purple", "red"]
            rc.itemconfig("circle", fill=colors[min(rating, 5) - 1])
            rc.itemconfig("rtext", text=str(rating))
            rc.place(x=slot["canvas_w"] - 38, y=6)
        else:
            rc.place(x=0, y=_OFFSCREEN_Y)

        if not slot.get("_events_bound"):
            self._vg_bind_slot_events(slot)
            slot["_events_bound"] = True

        self.thumbnail_labels[file_path] = {
            "row": data_idx // max(1, self._vg_cols),
            "col": data_idx % max(1, self._vg_cols),
            "index": data_idx,
            "canvas": canvas,
            "label": slot.get("_first_label_widget"),
        }

        if is_folder:
            self._queue_folder_preview_if_needed(file_path, self._vg_render_id)

        # tkinterdnd2 drag-out (Explorer / other apps): classic grid does this in bind_canvas_events;
        # virtual grid pools only call _vg_bind_slot_events once per canvas, so register here on every rebind.
        try:
            canvas.drag_source_register(dnd.DND_FILES)
            canvas.dnd_bind(
                "<<DragInitCmd>>",
                lambda e: self._dnd_thumb_drag_init(e, getattr(e.widget, "file_path", None)),
            )
            canvas.dnd_bind("<<DragEndCmd>>", self._dnd_drag_end)
        except tk.TclError:
            logging.debug("[VGrid] drag_source_register failed for pooled canvas", exc_info=True)

    def _vg_bind_slot_events(self, slot):
        """Bind events once using dynamic attribute lookup on canvas."""
        canvas = slot["canvas"]

        def _get_path():
            return getattr(canvas, "file_path", "")

        def _get_idx():
            return slot["data_idx"]

        def _is_folder():
            return getattr(canvas, "is_folder", False)

        canvas.is_thumbnail_frame = True
        canvas.bind("<Button-1>",
                     lambda e: self.on_thumb_click(e, _get_path(), canvas, _get_idx()))
        canvas.bind("<Control-Button-1>",
                     lambda e: self.on_thumb_click(e, _get_path(), canvas, _get_idx()))
        canvas.bind("<Shift-Button-1>",
                     lambda e: self.select_range(_get_path(), _get_idx()))
        canvas.bind("<Double-Button-1>",
                     lambda e: (self.display_thumbnails(_get_path()) if _is_folder()
                                else self.on_thumbnail_click(e, _get_path())))
        canvas.bind("<ButtonRelease-1>",
                     lambda e: None if _is_folder() else self.on_thumbnail_click(e, _get_path()))
        canvas.bind("<Button-3>",
                     lambda e: (self.show_tree_context_menu(e, self.find_node_by_path(_get_path()))
                                if _is_folder()
                                else self.show_thumbnail_context_menu(e, _get_path())))
        canvas.bind("<Enter>",
                     lambda e: None if _is_folder() else self.show_hover_info(e, _get_path()))
        canvas.bind("<Leave>",
                     lambda e: self.hide_hover_info())
        canvas.bind("<ButtonPress-1>",
                     lambda e: self._dnd_mark_thumb_press(e, _get_path()), add="+")
        canvas.bind("<ButtonPress-2>",
                     lambda e: self.start_drag(e, source_type="thumbnail"))
        canvas.bind("<ButtonRelease-2>",
                     lambda e: [self.drop_item(e, copy_mode=False), self.end_drag(e)])
        canvas.bind("<B2-Motion>", self.drag_motion_tree)

    # ------------------------------------------------------------------
    # 8. Wide slot binding
    # ------------------------------------------------------------------

    def _vg_bind_wide_slot(self, slot: dict, data_idx: int):
        # Allow geometry-only refresh (preview sizing/centering) even if data_idx didn't change.
        strip_w = slot.get("_strip_w") or slot.get("strip_w") or slot["strip"].winfo_width()
        strip_h = slot.get("_strip_h") or slot.get("strip_h") or slot["strip"].winfo_height()
        strip_w = max(1, int(strip_w))
        strip_h = max(1, int(strip_h))

        show_div = getattr(self, "vg_wide_show_divider", False)
        left_px = max(160, min(420, int(round(strip_w * 0.25))))
        pad_side = int(getattr(self, "vg_wide_preview_pad_side", _WIDE_PREVIEW_PAD_SIDE))
        # Without divider: no extra 14px gutter (it looked like a vertical rule).
        if show_div:
            sep_x = left_px + 14
        else:
            sep_x = 10 + left_px
        min_preview_w = 140
        if (strip_w - (sep_x + pad_side)) < min_preview_w:
            if show_div:
                left_px = max(160, strip_w - (14 + pad_side + min_preview_w))
                sep_x = left_px + 14
            else:
                left_px = max(160, strip_w - (10 + pad_side + min_preview_w))
                sep_x = 10 + left_px

        geom_changed = (
            slot.get("_geom_w") != strip_w
            or slot.get("_geom_h") != strip_h
            or slot.get("_geom_left_px") != left_px
        )

        # Chrome flags can change without pool rebuild or geometry; sync before early-return.
        self._vg_sync_wide_strip_border(slot["strip"])
        self._vg_sync_wide_divider(slot, left_px)

        item = self._vg_data[data_idx]
        file_path, file_name = item["path"], item["name"]

        # Skip rebind only for the same folder cell (same index alone is wrong after directory change).
        if (
            slot["data_idx"] == data_idx
            and slot.get("_vg_wide_slot_path") == file_path
            and not geom_changed
            and slot.get("_wide_photo_key") is not None
        ):
            self._vg_visible_wide_slots_by_path[self._vg_norm_path(file_path)] = slot
            return

        self._vg_forget_slot_path(slot, self._vg_visible_wide_slots_by_path)
        slot["data_idx"] = data_idx
        slot["_vg_wide_slot_path"] = file_path
        self._vg_visible_wide_slots_by_path[self._vg_norm_path(file_path)] = slot

        has_media = item.get("_has_media", False)

        icon = "\U0001F4C2" if has_media else "\U0001F4C1"
        slot["name_label"].configure(text=f"{icon}  {file_name}",
                                     fg="#dbdee1" if has_media else "#7f848a")

        row_gap = int(getattr(self, "vg_wide_label_row_gap", 4))
        tbg = getattr(self, "vg_wide_title_bottom_gap", None)
        title_pad = int(tbg) if tbg is not None else row_gap
        try:
            slot["name_label"].pack_configure(pady=(0, title_pad))
            slot["stats_label"].pack_configure(pady=(0, row_gap))
            slot["kw_label"].pack_configure(pady=(0, row_gap))
        except Exception:
            pass

        stats = item.get("_stats", {})

        if self._wide_folder_stats_nonempty(stats):
            parts = []
            vc = stats.get("video_count", 0)
            ic_count = stats.get("image_count", 0)
            if vc or ic_count:
                parts.append(f"Videos: {vc}   Images: {ic_count}")
            slot["stats_label"].configure(text="\n".join(parts) if parts else "")

            kw_body = (stats.get("keywords") or "").strip()
            extra_kw = int(stats.get("extra_keyword_count") or 0)
            if kw_body and extra_kw:
                kw_body = f"Keywords: {kw_body} (+{extra_kw})"
            elif kw_body:
                kw_body = f"Keywords: {kw_body}"
            elif extra_kw:
                kw_body = f"+{extra_kw} tags"
            slot["kw_label"].configure(text=kw_body)

            rc = slot["rating_canvas"]
            rc.delete("all")
            ratings = stats.get("ratings", [])
            if ratings:
                colors = ["lightblue", "lightgreen", "yellow", "purple", "red"]
                for ri_idx, ri in enumerate(ratings):
                    ri = int(ri)
                    if ri < 1 or ri > 5:
                        continue
                    x0 = ri_idx * 26
                    rc.create_oval(x0 + 2, 1, x0 + 20, 15,
                                   fill=colors[ri - 1], outline="white", width=1)
                    rc.create_text(x0 + 11, 8, text=str(ri), fill="white",
                                   font=("Helvetica", 8, "bold"))
                rc.pack(side="top", anchor="w", pady=(2, 0))
            else:
                rc.pack_forget()
        else:
            slot["stats_label"].configure(text="")
            slot["kw_label"].configure(text="")
            slot["rating_canvas"].pack_forget()

        # --- Update wide left/right geometry (wrap lengths + separator pos) ---
        # Keep separator alignment and ensure preview starts exactly after it.
        if geom_changed:
            try:
                slot["left_panel"].place_configure(x=10, y=8, width=left_px, relheight=1.0, height=-16)
            except Exception:
                pass
            try:
                slot["name_label"].configure(wraplength=max(1, left_px - 10))
                slot["stats_label"].configure(wraplength=max(1, left_px - 10))
                slot["kw_label"].configure(wraplength=max(1, left_px - 10))
            except Exception:
                pass
            slot["left_px"] = left_px
            slot["_geom_w"] = strip_w
            slot["_geom_h"] = strip_h
            slot["_geom_left_px"] = left_px

        photo = None
        if has_media and self.memory_cache:
            nprev = int(getattr(self, "vg_wide_preview_count", 5))
            wide_key = self._vg_wide_cache_key(file_path, nprev)
            cached = thumbnail_cache.get(wide_key, memory_cache=self.memory_cache)
            if cached is None:
                # Backwards compatibility with older cache key
                cached = thumbnail_cache.get(file_path + "\x00wide\x00n=" + str(nprev), memory_cache=self.memory_cache)
            if cached is None:
                cached = thumbnail_cache.get(file_path + "\x00wide", memory_cache=self.memory_cache)
            # Never fall back to plain file_path: after Standard view that key is the 2x2 folder composite,
            # which then fills the wide strip and looks like "standard folder" instead of a filmstrip.
            if cached is not None:
                # Preview area: between separator and right edge, vertically inside the strip.
                top_margin = int(getattr(self, "vg_wide_preview_margin_y", _WIDE_PREVIEW_MARGIN_Y))
                bottom_margin = top_margin
                preview_x = sep_x + pad_side
                preview_y = top_margin
                preview_w = max(10, strip_w - preview_x - pad_side)
                preview_h = max(10, strip_h - top_margin - bottom_margin)

                # Keep padding from the wide-strip edges via preview_x/preview_y.
                # Inner padding reduces the preview size inside the available area.
                # This is what the user perceives as "thumbs are too big".
                inner_pad = int(getattr(self, "vg_wide_preview_inner_pad", _WIDE_PREVIEW_INNER_PAD))
                target_w = max(10, preview_w - 2 * inner_pad)
                target_h = max(10, preview_h - 2 * inner_pad)

                card_bg_hex = slot.get("card_bg") or self._vg_safe_color(
                    getattr(self, "vg_wide_bg_color", None),
                    getattr(self, "folder_color_media", "#2a3a4a"),
                )
                bg_rgb = (45, 58, 74)
                if isinstance(card_bg_hex, str) and card_bg_hex.startswith("#") and len(card_bg_hex) == 7:
                    bg_rgb = (
                        int(card_bg_hex[1:3], 16),
                        int(card_bg_hex[3:5], 16),
                        int(card_bg_hex[5:7], 16),
                    )
                resized = self._vg_flatten_rgba_for_tk(
                    ImageOps.contain(cached._light_image, (target_w, target_h)),
                    bg_rgb,
                )

                round_on = getattr(self, "vg_wide_round_preview_corners", True)
                rr_user = int(getattr(self, "vg_wide_preview_corner_radius", 0))
                if round_on and rr_user <= 0:
                    rr_draw = int(min(target_w, target_h) * 0.12)
                    rr_draw = max(6, min(18, rr_draw))
                elif round_on:
                    rr_draw = max(1, rr_user)
                else:
                    rr_draw = 0

                rw, rh = resized.size
                max_r = max(1, min(rw, rh) // 2)
                rr_eff = min(rr_draw, max_r) if round_on and rr_draw > 0 else 0

                # Bust cache when tuning changes (rr_user kept even if clamped to rr_eff).
                cache_key = (file_path, target_w, target_h, rr_user, rr_eff, round_on)
                if slot.get("_wide_photo_key") == cache_key and slot.get("photo") is not None:
                    photo = slot["photo"]
                else:
                    try:
                        from PIL import ImageDraw
                        rgba = resized.convert("RGBA")
                        if round_on and rr_eff > 0:
                            mask = Image.new("L", rgba.size, 0)
                            draw = ImageDraw.Draw(mask)
                            x1, y1 = rw - 1, rh - 1
                            draw.rounded_rectangle(
                                (0, 0, x1, y1),
                                radius=int(rr_eff),
                                fill=255,
                            )
                            bg = Image.new("RGBA", rgba.size, (bg_rgb[0], bg_rgb[1], bg_rgb[2], 255))
                            bg.paste(rgba, (0, 0), mask)
                            rounded = bg.convert("RGB")
                        else:
                            bg = Image.new("RGBA", rgba.size, (bg_rgb[0], bg_rgb[1], bg_rgb[2], 255))
                            if rgba.mode == "RGBA":
                                bg.paste(rgba, (0, 0), rgba)
                            else:
                                bg.paste(rgba, (0, 0))
                            rounded = bg.convert("RGB")

                        photo = ImageTk.PhotoImage(rounded)
                    except Exception as e:
                        logging.debug("[VGrid] wide preview rounded mask failed: %s", e, exc_info=True)
                        try:
                            photo = ImageTk.PhotoImage(
                                self._vg_flatten_rgba_for_tk(resized, bg_rgb))
                        except Exception:
                            redo = ImageOps.contain(
                                cached._light_image, (target_w, target_h))
                            photo = ImageTk.PhotoImage(
                                self._vg_flatten_rgba_for_tk(redo, bg_rgb))
                    slot["_wide_photo_key"] = cache_key
        img_c = slot["img_canvas"]

        # Place the preview canvas itself (so the image is centered in the right section).
        top_margin = int(getattr(self, "vg_wide_preview_margin_y", _WIDE_PREVIEW_MARGIN_Y))
        bottom_margin = top_margin
        preview_x = sep_x + pad_side
        preview_y = top_margin
        preview_w = max(10, strip_w - preview_x - pad_side)
        preview_h = max(10, strip_h - top_margin - bottom_margin)

        img_c.place(x=preview_x, y=preview_y, width=preview_w, height=preview_h)

        # Center the image within the preview canvas.
        if photo:
            img_c.itemconfig("wideimg", image=photo)
            img_c.image = photo
            slot["photo"] = photo
            img_c.coords("wideimg", preview_w // 2, preview_h // 2)
        else:
            img_c.itemconfig("wideimg", image="")
            img_c.image = None
            slot["photo"] = None
            if not has_media:
                img_c.place_forget()
            else:
                img_c.place(x=preview_x, y=preview_y, width=preview_w, height=preview_h)

        strip = slot["strip"]
        strip.file_path = file_path
        strip.is_folder = True
        self.bind_canvas_events(strip, file_path, file_name, True, index=data_idx)

        # Right-side image canvas must also be bound.
        # Otherwise clicks on the image area won't reach on_thumb_click,
        # so selection border/tree selection doesn't update.
        img_c = slot["img_canvas"]
        img_c.file_path = file_path
        img_c.is_folder = True
        self.bind_canvas_events(img_c, file_path, file_name, True, index=data_idx)

        # strip_canvas is placed full-size under the title column and preview; it only had
        # <Configure> for drawing the card. Clicks in uncovered areas (empty-folder strip,
        # horizontal pad between column and preview) hit this canvas and otherwise did nothing.
        sc = slot.get("strip_canvas")
        if sc is not None:
            self.bind_canvas_events(sc, file_path, file_name, True, index=data_idx)

        for w in (slot["name_label"], slot["stats_label"], slot["kw_label"],
                  slot["left_panel"]):
            w.bind("<Button-1>",
                   lambda e, p=file_path, s=strip, i=data_idx: self.on_thumb_click(e, p, s, i))
            w.bind("<Double-Button-1>",
                   lambda e, p=file_path: self.display_thumbnails(p))

        self.thumbnail_labels[file_path] = {
            "row": data_idx, "col": 0, "index": data_idx,
            "canvas": strip, "label": slot["name_label"],
        }

    # ------------------------------------------------------------------
    # 9. Selection support
    # ------------------------------------------------------------------

    def _vg_reapply_selection(self):
        selected_indices = set()
        for item in getattr(self, "selected_thumbnails", []):
            if isinstance(item, (list, tuple)) and len(item) > 2:
                selected_indices.add(item[2])

        for slot in self._vg_std_pool:
            idx = slot["data_idx"]
            if idx >= 0:
                self._apply_selection_border(slot["canvas"], idx in selected_indices)

        for slot in self._vg_wide_pool:
            idx = slot["data_idx"]
            if idx >= 0:
                self._apply_selection_border(slot["strip"], idx in selected_indices)

    # ------------------------------------------------------------------
    # 9b. Low-priority recursive folder previews
    # ------------------------------------------------------------------

    def _start_folder_preview_worker(self) -> None:
        if self._folder_preview_worker_started:
            return
        self._folder_preview_worker_started = True
        worker = threading.Thread(
            target=self._folder_preview_worker_loop,
            name="folder-preview-worker",
            daemon=True,
        )
        worker.start()
        logging.debug("[FolderPreview] worker thread started")

    def _folder_preview_is_green(self, folder_path: str) -> bool:
        try:
            return bool(self.database.is_folder_cached(folder_path))
        except Exception:
            return False

    def _folder_preview_cache_key(self, folder_path: str, thumbnail_size=None, is_green=None) -> tuple:
        size = tuple(thumbnail_size or self.thumbnail_size)
        green = self._folder_preview_is_green(folder_path) if is_green is None else bool(is_green)
        return (self._vg_norm_path(folder_path), size, green)

    def _queue_folder_preview_if_needed(self, folder_path: str, render_id=None) -> None:
        if not folder_path or not os.path.isdir(folder_path):
            logging.debug("[FolderPreview] skip queue: not a directory path=%s", folder_path)
            return
        thumbnail_size = tuple(self.thumbnail_size)
        is_green = self._folder_preview_is_green(folder_path)
        key = self._folder_preview_cache_key(folder_path, thumbnail_size, is_green)
        status = self._preview_status_cache.get(key)
        if status == "READY":
            logging.debug("[FolderPreview] queue sees READY path=%s", folder_path)
            self._folder_preview_apply_ready(folder_path, render_id, thumbnail_size)
            return
        if status == "PENDING":
            pending_rid = self._folder_preview_pending_render_ids.get(key)
            if pending_rid == render_id:
                logging.debug("[FolderPreview] skip queue: already PENDING path=%s rid=%s", folder_path, render_id)
                return
            logging.debug(
                "[FolderPreview] replace stale PENDING path=%s old_rid=%s new_rid=%s",
                folder_path,
                pending_rid,
                render_id,
            )
        if status == "EMPTY":
            last_empty = self._folder_preview_empty_seen_at.get(key, 0.0)
            retry_after = float(getattr(self, "_folder_preview_empty_retry_s", 30.0))
            if time.monotonic() - last_empty < retry_after:
                logging.debug("[FolderPreview] skip queue: recent EMPTY path=%s", folder_path)
                return

        try:
            if self.file_ops.folder_preview_cache_exists(
                folder_path,
                thumbnail_size,
                cache_dir=self.thumbnail_cache_path,
                for_green_folder_icon=is_green,
            ):
                self._preview_status_cache[key] = "READY"
                logging.debug("[FolderPreview] disk cache hit path=%s green=%s", folder_path, is_green)
                self._folder_preview_apply_ready(folder_path, render_id, thumbnail_size)
                return
        except Exception:
            logging.debug("[FolderPreview] disk cache check failed path=%s", folder_path, exc_info=True)

        self._preview_status_cache[key] = "PENDING"
        self._folder_preview_pending_render_ids[key] = render_id
        logging.debug(
            "[FolderPreview] scheduled delayed enqueue path=%s rid=%s green=%s size=%s",
            folder_path,
            render_id,
            is_green,
            thumbnail_size,
        )

        def _enqueue_if_still_visible():
            try:
                if render_id is not None and render_id != self._vg_render_id:
                    if (
                        self._preview_status_cache.get(key) == "PENDING"
                        and self._folder_preview_pending_render_ids.get(key) == render_id
                    ):
                        self._preview_status_cache.pop(key, None)
                        self._folder_preview_pending_render_ids.pop(key, None)
                    logging.debug(
                        "[FolderPreview] delayed enqueue cancelled stale path=%s rid=%s current=%s",
                        folder_path,
                        render_id,
                        self._vg_render_id,
                    )
                    return
                norm = self._vg_norm_path(folder_path)
                if not getattr(self, "_vg_active", False) or norm not in self._vg_visible_std_slots_by_path:
                    if (
                        self._preview_status_cache.get(key) == "PENDING"
                        and self._folder_preview_pending_render_ids.get(key) == render_id
                    ):
                        self._preview_status_cache.pop(key, None)
                        self._folder_preview_pending_render_ids.pop(key, None)
                    logging.debug(
                        "[FolderPreview] delayed enqueue cancelled not visible path=%s active=%s",
                        folder_path,
                        getattr(self, "_vg_active", False),
                    )
                    return
                self._folder_preview_queue.put(
                    {
                        "folder_path": folder_path,
                        "thumbnail_size": thumbnail_size,
                        "is_green": is_green,
                        "render_id": render_id,
                        "key": key,
                    }
                )
                logging.debug("[FolderPreview] enqueued path=%s qsize=%s", folder_path, self._folder_preview_queue.qsize())
            except Exception as exc:
                logging.debug("[FolderPreview] enqueue failed path=%s error=%s", folder_path, exc, exc_info=True)
                self._preview_status_cache.pop(key, None)
                self._folder_preview_pending_render_ids.pop(key, None)

        try:
            self.after(int(self._folder_preview_idle_delay_ms), _enqueue_if_still_visible)
        except Exception:
            logging.debug("[FolderPreview] after() schedule failed path=%s", folder_path, exc_info=True)
            self._preview_status_cache.pop(key, None)
            self._folder_preview_pending_render_ids.pop(key, None)

    def _folder_preview_worker_loop(self) -> None:
        while True:
            item = self._folder_preview_queue.get()
            status = "EMPTY"
            try:
                time.sleep(float(getattr(self, "_folder_preview_worker_sleep_s", 0.10)))
                folder_path = item["folder_path"]
                thumbnail_size = tuple(item["thumbnail_size"])
                is_green = bool(item["is_green"])
                logging.debug(
                    "[FolderPreview] worker job start path=%s green=%s size=%s",
                    folder_path,
                    is_green,
                    thumbnail_size,
                )
                sources = self._collect_folder_preview_sources(folder_path)
                logging.debug(
                    "[FolderPreview] worker sources=%d folder=%s first=%s",
                    len(sources),
                    folder_path,
                    sources[0] if sources else "",
                )
                if sources:
                    preview_path = self.file_ops.create_cached_folder_preview(
                        folder_path=folder_path,
                        thumbnail_size=thumbnail_size,
                        source_file_paths=sources,
                        cache_enabled=self.cache_enabled,
                        cache_dir=self.thumbnail_cache_path,
                        database=self.database,
                        for_green_folder_icon=is_green,
                    )
                    status = "READY" if preview_path else "EMPTY"
                    logging.debug(
                        "[FolderPreview] worker write status=%s folder=%s cache=%s",
                        status,
                        folder_path,
                        preview_path,
                    )
            except Exception as exc:
                logging.debug("[FolderPreview] worker failed error=%s item=%s", exc, item, exc_info=True)
                status = "EMPTY"
            finally:
                try:
                    self._folder_preview_queue.task_done()
                except Exception:
                    pass
                try:
                    self.after(
                        0,
                        lambda it=item, st=status: self._folder_preview_worker_finished(it, st),
                    )
                except Exception:
                    logging.debug("[FolderPreview] worker finish after() failed item=%s", item, exc_info=True)

    def _collect_folder_preview_sources(self, folder_path: str) -> list[str]:
        started = time.monotonic()
        valid_extensions = set(ext.lower() for ext in VIDEO_FORMATS + IMAGE_FORMATS)
        max_files = 4
        max_depth = int(getattr(self, "_folder_preview_max_depth", 3))
        max_dirs = int(getattr(self, "_folder_preview_max_dirs", 80))
        max_entries = int(getattr(self, "_folder_preview_max_entries", 800))
        deadline = time.monotonic() + float(getattr(self, "_folder_preview_scan_budget_s", 0.25))

        collected: list[str] = []
        dirs_seen = 0
        entries_seen = 0
        queue_dirs: list[tuple[str, int]] = [(folder_path, 0)]

        while queue_dirs and len(collected) < max_files:
            if time.monotonic() > deadline or dirs_seen >= max_dirs or entries_seen >= max_entries:
                break
            current, depth = queue_dirs.pop(0)
            dirs_seen += 1
            try:
                with os.scandir(current) as entries:
                    child_dirs: list[str] = []
                    for entry in entries:
                        if len(collected) >= max_files:
                            break
                        if time.monotonic() > deadline or entries_seen >= max_entries:
                            break
                        entries_seen += 1
                        try:
                            if entry.is_file(follow_symlinks=False):
                                if os.path.splitext(entry.name)[1].lower() in valid_extensions:
                                    collected.append(entry.path)
                            elif depth < max_depth and entry.is_dir(follow_symlinks=False):
                                if not preview_skip_subdir(entry.name):
                                    child_dirs.append(entry.path)
                        except (OSError, PermissionError):
                            continue
                    if depth < max_depth:
                        queue_dirs.extend((path, depth + 1) for path in child_dirs)
            except (OSError, PermissionError):
                continue
        logging.debug(
            "[FolderPreview] scan done path=%s found=%d dirs=%d entries=%d elapsed=%.3fs",
            folder_path,
            len(collected),
            dirs_seen,
            entries_seen,
            time.monotonic() - started,
        )
        return collected

    def _folder_preview_worker_finished(self, item: dict, status: str) -> None:
        key = item.get("key")
        if key is not None:
            self._preview_status_cache[key] = status
            self._folder_preview_pending_render_ids.pop(key, None)
            if status == "EMPTY":
                self._folder_preview_empty_seen_at[key] = time.monotonic()
            elif status == "READY":
                self._folder_preview_empty_seen_at.pop(key, None)
        logging.debug(
            "[FolderPreview] worker finished status=%s path=%s",
            status,
            item.get("folder_path"),
        )
        if status != "READY":
            return
        self._folder_preview_apply_ready(
            item.get("folder_path"),
            item.get("render_id"),
            tuple(item.get("thumbnail_size") or self.thumbnail_size),
        )

    def _folder_preview_apply_ready(self, folder_path: str, render_id=None, thumbnail_size=None) -> None:
        if not folder_path or not getattr(self, "_vg_active", False):
            logging.debug(
                "[FolderPreview] apply skipped inactive path=%s active=%s",
                folder_path,
                getattr(self, "_vg_active", False),
            )
            return
        if render_id is not None and render_id != self._vg_render_id:
            logging.debug(
                "[FolderPreview] apply skipped stale path=%s rid=%s current=%s",
                folder_path,
                render_id,
                self._vg_render_id,
            )
            return
        thumbnail_size = tuple(thumbnail_size or self.thumbnail_size)
        if thumbnail_size != tuple(self.thumbnail_size):
            logging.debug(
                "[FolderPreview] apply skipped size changed path=%s job_size=%s current_size=%s",
                folder_path,
                thumbnail_size,
                self.thumbnail_size,
            )
            return
        norm = self._vg_norm_path(folder_path)
        slot = self._vg_visible_std_slots_by_path.get(norm)
        data_idx = self._vg_data_index_by_path.get(norm, -1)
        if not slot or data_idx < 0 or data_idx >= len(self._vg_data):
            logging.debug(
                "[FolderPreview] apply skipped not visible path=%s slot=%s data_idx=%s",
                folder_path,
                bool(slot),
                data_idx,
            )
            return
        item = self._vg_data[data_idx]
        if not item.get("is_folder", False):
            logging.debug("[FolderPreview] apply skipped not folder path=%s", folder_path)
            return
        is_green = self._folder_preview_is_green(folder_path)
        thumb = self.file_ops.create_folder_thumbnail(
            thumbnail_size=self.thumbnail_size,
            folder_path=folder_path,
            cache_enabled=self.cache_enabled,
            cache_dir=self.thumbnail_cache_path,
            database=self.database,
            is_cached=is_green,
        )
        if thumb is None:
            logging.debug("[FolderPreview] apply failed create_folder_thumbnail returned None path=%s", folder_path)
            return
        item.pop("_photo", None)
        item.pop("_photo_cache_key", None)
        if self.memory_cache:
            thumbnail_cache.set(folder_path, thumb, memory_cache=self.memory_cache)
            self._vg_apply_generated_thumb(folder_path)
            logging.debug("[FolderPreview] applied via memory cache path=%s", folder_path)
            return

        source = getattr(thumb, "_light_image", None)
        if source is None:
            return
        resized = ImageOps.contain(source, tuple(self.thumbnail_size))
        photo = ImageTk.PhotoImage(resized)
        item["_photo"] = photo
        item["_photo_cache_key"] = (id(source), getattr(source, "size", None), tuple(self.thumbnail_size))
        slot["canvas"].itemconfig("thumbnail", image=photo)
        slot["canvas"].image = photo
        slot["photo"] = photo
        logging.debug("[FolderPreview] applied direct path=%s", folder_path)

    # ------------------------------------------------------------------
    # 10. Async thumbnail generation
    # ------------------------------------------------------------------

    def _vg_start_async_generation(self, force_refresh, thumbnail_time, render_id):
        self._vg_render_id = render_id
        self._vg_pending_gen.clear()
        self._vg_gen_queue = []
        self._vg_gen_active = 0
        self._vg_gen_force_refresh = force_refresh
        self._vg_gen_thumbnail_time = thumbnail_time

        if getattr(self, "_thumb_grid_suppress_decode_until_nav", False):
            logging.info(
                "[VGrid] Thumbnail decode suppressed after Clear — switch folder or use Refresh/Scan to rebuild."
            )
            return

        try:
            visible_folder_count = 0
            for slot in list(self._vg_visible_std_slots_by_path.values()):
                data_idx = int(slot.get("data_idx", -1))
                if 0 <= data_idx < len(self._vg_data):
                    item = self._vg_data[data_idx]
                    if item.get("is_folder", False):
                        visible_folder_count += 1
                        self._queue_folder_preview_if_needed(item.get("path"), render_id)
            logging.debug(
                "[FolderPreview] start_async visible standard folders=%d rid=%s",
                visible_folder_count,
                render_id,
            )
        except Exception:
            logging.debug("[FolderPreview] start_async queue visible failed", exc_info=True)

        items_to_generate = []
        for item in self._vg_data:
            fp = item["path"]
            if force_refresh or not self.memory_cache:
                items_to_generate.append(item)
            elif self._vg_is_wide and item.get("is_folder", False):
                # Wide strips never draw the standard folder-preview cache stored under fp.
                # Require the dedicated wide cache key so folders with only nested media
                # still get their filmstrip generated after Standard mode cached the 2x2 icon.
                if item.get("_has_media") is False:
                    continue
                nprev = int(getattr(self, "vg_wide_preview_count", 5))
                wide_key = self._vg_wide_cache_key(fp, nprev)
                cached = thumbnail_cache.get(wide_key, memory_cache=self.memory_cache)
                if cached is None:
                    items_to_generate.append(item)
            elif thumbnail_cache.get(fp, memory_cache=self.memory_cache) is None:
                items_to_generate.append(item)

        if not items_to_generate:
            logging.info("[VGrid] All %d items cached.", len(self._vg_data))
            # Legacy thumb queue marks current_directory cached when work finishes; virtual grid
            # used to skip this when everything was already in memory — tree stayed yellow.
            self.after(0, lambda rid=render_id: self._vg_try_finish_folder_cache_mark(rid))
            return
        logging.info("[VGrid] Async gen: %d / %d", len(items_to_generate), len(self._vg_data))

        self._vg_pending_gen = {item["path"] for item in items_to_generate}
        self._vg_gen_queue = list(items_to_generate)
        self._vg_pump_async_generation(render_id)

    def _vg_pump_async_generation(self, render_id):
        """Submit thumbnail jobs gradually so stale decoders do not pile up during navigation."""
        if render_id != self._vg_render_id:
            return
        limit = max(1, int(getattr(self, "_vg_gen_limit", 6) or 6))
        while self._vg_gen_queue and self._vg_gen_active < limit:
            item = self._vg_gen_queue.pop(0)
            fp, fn = item["path"], item["name"]
            self._vg_gen_active += 1
            if item.get("is_folder", False):
                self.executor.submit(self._vg_worker_folder, fp, fn, render_id)
            else:
                self.executor.submit(
                    self._vg_worker_file,
                    fp,
                    fn,
                    self._vg_gen_force_refresh,
                    self._vg_gen_thumbnail_time,
                    render_id,
                )

    def _vg_generation_job_done(self, file_path: str, render_id) -> None:
        if render_id != self._vg_render_id:
            return
        self._vg_gen_active = max(0, int(getattr(self, "_vg_gen_active", 0)) - 1)
        self._vg_pending_gen.discard(file_path)
        self._vg_pump_async_generation(render_id)
        self._vg_try_finish_folder_cache_mark(render_id)

    def _vg_mark_current_directory_cached(self) -> None:
        """Match legacy process_thumbnail_batch: flag current folder after thumbs are ready."""
        cd = getattr(self, "current_directory", None)
        if not cd or not os.path.isdir(cd):
            return
        if isinstance(cd, str) and cd.startswith("virtual_library://"):
            return
        _blocked = getattr(self, "_folder_cache_auto_mark_is_blocked", None)
        if callable(_blocked) and _blocked(cd):
            return
        try:
            self.database.update_cache_status(cd, True)
            self.refresh_folder_icon(cd)
        except Exception as e:
            logging.debug("[VGrid] folder cache flag update failed: %s", e)

    def _vg_try_finish_folder_cache_mark(self, render_id) -> None:
        """When async generation for this view is done, set DB/tree green for current_directory."""
        if render_id != self._vg_render_id:
            return
        if not getattr(self, "_vg_active", False):
            return
        if self._vg_pending_gen:
            return
        self._vg_mark_current_directory_cached()

    def _vg_worker_file(self, file_path, file_name, force_refresh, thumbnail_time, render_id):
        try:
            if render_id != self._vg_render_id:
                return
            thumb = None
            if file_name.lower().endswith(VIDEO_FORMATS):
                # Always resolve capture time like normal loads. If thumbnail_time is None (most
                # display_thumbnails calls), leaving actual_time unset made create_video_thumbnail
                # default to 0.1 *seconds* instead of slider % / DB timestamp — wrong frame after refresh.
                if not force_refresh and self.database.get_cache_status(file_path):
                    actual_time = 0
                else:
                    actual_time = self.calculate_thumbnail_time(file_path)
                thumb = create_video_thumbnail(
                    file_path, self.thumbnail_size, self.thumbnail_format,
                    self.capture_method_var.get(), thumbnail_time=actual_time,
                    cache_enabled=self.cache_enabled, overwrite=force_refresh,
                    cache_dir=self.thumbnail_cache_path, database=self.database,
                )
            else:
                thumb = create_image_thumbnail(
                    file_path, self.thumbnail_size, database=self.database,
                    cache_dir=self.thumbnail_cache_path, overwrite=force_refresh,
                )
            if thumb is None:
                if file_name.lower().endswith(VIDEO_FORMATS):
                    thumb = self._create_corrupted_thumbnail_image(
                        "Thumbnail could not be generated"
                    )
                else:
                    try:
                        img = Image.open("image_icon.png")
                        thumb = ctk.CTkImage(light_image=img, dark_image=img)
                    except Exception:
                        thumb = self._create_corrupted_thumbnail_image(
                            "This file could not be read"
                        )
            if self.memory_cache:
                thumbnail_cache.set(file_path, thumb, memory_cache=self.memory_cache)
            self.after(0, lambda fp=file_path: self._vg_apply_generated_thumb(fp))
        except Exception as e:
            logging.debug("[VGrid] File error %s: %s", file_path, e)
        finally:
            if render_id == self._vg_render_id:
                self.after(0, lambda fp=file_path, rid=render_id: self._vg_generation_job_done(fp, rid))

    def _vg_worker_folder(self, file_path, file_name, render_id):
        try:
            if render_id != self._vg_render_id:
                return
            basic = self.file_ops.create_folder_thumbnail(
                thumbnail_size=self.thumbnail_size, folder_path=None,
                cache_enabled=self.cache_enabled,
                cache_dir=self.thumbnail_cache_path,
                database=self.database, is_cached=False,
            )
            # Wide mode: do not publish the generic folder.png under file_path — bind falls back to
            # it and flashes the yellow icon before the real preview is ready.
            if basic and self.memory_cache and not self._vg_is_wide:
                thumbnail_cache.set(file_path, basic, memory_cache=self.memory_cache)
                self.after(0, lambda fp=file_path: self._vg_apply_generated_thumb(fp))

            if render_id != self._vg_render_id:
                return

            is_cached = self.database.is_folder_cached(file_path)
            composite = self.file_ops.create_folder_thumbnail(
                thumbnail_size=self.thumbnail_size, folder_path=file_path,
                cache_enabled=self.cache_enabled,
                cache_dir=self.thumbnail_cache_path,
                database=self.database, is_cached=is_cached,
            )
            if composite and self.memory_cache:
                thumbnail_cache.set(file_path, composite, memory_cache=self.memory_cache)
                # Wide strip uses wide_key; refreshing here would show folder+grid then swap again.
                if not self._vg_is_wide:
                    self.after(0, lambda fp=file_path: self._vg_apply_generated_thumb(fp))

            wide_applied = False
            if self._vg_is_wide:
                try:
                    nprev = int(getattr(self, "vg_wide_preview_count", 5))
                    wide_path = self.create_wide_folder_thumbnail(
                        file_path, self.widefolder_size, num_thumbnails=nprev)
                    if wide_path:
                        with Image.open(wide_path) as img:
                            wide_ctk = ctk.CTkImage(
                                light_image=img.copy(), dark_image=img.copy())
                        wide_key = self._vg_wide_cache_key(file_path, nprev)
                        thumbnail_cache.set(wide_key, wide_ctk,
                                            memory_cache=self.memory_cache)
                        self.after(0, lambda fp=file_path:
                                   self._vg_apply_generated_thumb(fp))
                        wide_applied = True
                except Exception as exc:
                    logging.debug("[VGrid] Wide composite error %s: %s",
                                  file_path, exc)

            if self._vg_is_wide and not wide_applied and composite and self.memory_cache:
                self.after(0, lambda fp=file_path: self._vg_apply_generated_thumb(fp))
        except Exception as e:
            logging.debug("[VGrid] Folder error %s: %s", file_path, e)
        finally:
            if render_id == self._vg_render_id:
                self.after(0, lambda fp=file_path, rid=render_id: self._vg_generation_job_done(fp, rid))

    def _vg_apply_generated_thumb(self, file_path: str):
        if not self._vg_active:
            return
        norm = self._vg_norm_path(file_path)
        data_idx = self._vg_data_index_by_path.get(norm, -1)
        if data_idx < 0:
            for idx, item in enumerate(self._vg_data):
                if self._vg_norm_path(item.get("path", "")) == norm:
                    data_idx = idx
                    self._vg_data_index_by_path[norm] = idx
                    break
        if data_idx < 0 or data_idx >= len(self._vg_data):
            return

        item = self._vg_data[data_idx]
        photo = self._vg_get_item_photo(item, item["path"], force_rebuild=True)

        slot = self._vg_visible_std_slots_by_path.get(norm)
        if slot and slot.get("data_idx") == data_idx:
            if photo:
                slot["canvas"].itemconfig("thumbnail", image=photo)
                slot["canvas"].image = photo
                slot["photo"] = photo
            return

        slot = self._vg_visible_wide_slots_by_path.get(norm)
        if slot and slot.get("data_idx") == data_idx:
            # Re-render wide preview using the same centering/padding/rounded logic
            # as the normal binder (fixes small/un-centered/sharp corners on startup).
            slot.pop("_wide_photo_key", None)
            try:
                self._vg_bind_wide_slot(slot, data_idx)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 11. Activation / deactivation
    # ------------------------------------------------------------------

    def activate_virtual_grid(self, video_files: list[dict]):
        self._vg_last_first_row = -1
        self._vg_active = True
        self._vg_y_offset = 0
        self._vg_visible_std_slots_by_path.clear()
        self._vg_visible_wide_slots_by_path.clear()
        self._vg_label_measure_cache.clear()

        self._vg_is_wide = self.folder_view_mode.get() == "Wide"
        if self._vg_is_wide:
            folders = [v for v in video_files if v.get("is_folder")]
            files = [v for v in video_files if not v.get("is_folder")]
            self._vg_data = folders + files
            self._vg_folder_count = len(folders)
            for item in folders:
                fp = item["path"]
                item["_has_media"] = self._folder_has_media_cached(fp)
                try:
                    item["_stats"] = self._get_wide_folder_db_stats(fp)
                except Exception:
                    item["_stats"] = {}
        else:
            self._vg_data = video_files
            self._vg_folder_count = 0

        self._vg_index_data()
        self._vg_warm_item_metadata()

        if hasattr(self, "scrollable_frame_window_id"):
            try:
                self.canvas.itemconfigure(self.scrollable_frame_window_id, state="hidden")
            except Exception:
                pass

        for attr in ("wide_folders_frame", "regular_thumbnails_frame", "filler"):
            w = getattr(self, attr, None)
            if w:
                try:
                    w.pack_forget()
                except Exception:
                    pass

        for slot in self._vg_std_pool:
            slot["data_idx"] = -1
        for slot in self._vg_wide_pool:
            slot["data_idx"] = -1
            slot.pop("_wide_photo_key", None)
            slot.pop("_vg_wide_slot_path", None)

        for item in self._vg_data:
            fp = item["path"]
            parts = self._vg_get_item_info_parts(item)

            # --- 1. Odhad výšky popisku (px) pro jednotnou výšku řádku mřížky ---
            item["_vg_label_h_px"] = self._vg_measure_item_label_height_px(
                fp,
                item["name"],
                item.get("is_folder", False),
                self.thumbnail_size[0],
                parts=parts,
            )

        # --- 2. Nejvyšší blok popisků v této složce ---
        max_rows = 1
        try:
            for item in self._vg_data:
                parts = self._vg_get_item_info_parts(item)
                max_rows = max(max_rows, max(1, len(parts) if parts else 1))
        except Exception:
            pass
        cap_px = self._vg_effective_label_cap_px(self.thumbnail_size[0], info_rows=max_rows)
        max_h = 34
        for item in self._vg_data:
            max_h = max(max_h, int(item.get("_vg_label_h_px", 34)))
        self._vg_dynamic_label_h = min(max_h, cap_px)

        self._vg_recalc()
        self._vg_wire_scrollbar()

        frac = getattr(self, "_thumb_reload_preserve_yview", None)
        if frac is not None:
            try:
                frac = max(0.0, min(1.0, float(frac)))
            except (TypeError, ValueError):
                frac = None
        if frac is not None:
            self._thumb_reload_preserve_yview = None
            self.canvas.yview_moveto(frac)
            scroll_px = frac * float(self._vg_scrollregion_h or 1)
        else:
            self.canvas.yview_moveto(0)
            scroll_px = 0.0
        self._vg_layout_slots(scroll_px)

        logging.info("[VGrid] Activated: %d items (wide=%s, folders=%d), pool std=%d wide=%d",
                     len(video_files), self._vg_is_wide, self._vg_folder_count,
                     self._vg_std_pool_size, self._vg_wide_pool_size)
    def deactivate_virtual_grid(self):
        self._vg_active = False
        self._vg_pending_gen.clear()
        self._vg_gen_queue = []
        self._vg_gen_active = 0
        self._vg_last_first_row = -1
        self._vg_data_index_by_path.clear()
        self._vg_visible_std_slots_by_path.clear()
        self._vg_visible_wide_slots_by_path.clear()
        self._vg_label_measure_cache.clear()
        self._vg_unwire_scrollbar()

        for slot in self._vg_std_pool:
            try:
                self.canvas.delete(slot["win_id"])
            except Exception:
                pass
            try:
                slot["frame"].destroy()
            except Exception:
                pass
        self._vg_std_pool.clear()
        self._vg_std_pool_size = 0

        for slot in self._vg_wide_pool:
            try:
                self.canvas.delete(slot["win_id"])
            except Exception:
                pass
            try:
                slot["frame"].destroy()
            except Exception:
                pass
        self._vg_wide_pool.clear()
        self._vg_wide_pool_size = 0

        if hasattr(self, "scrollable_frame_window_id"):
            try:
                self.canvas.itemconfigure(self.scrollable_frame_window_id, state="normal")
            except Exception:
                pass

    def _vg_on_canvas_resize(self, event=None):
        if not self._vg_active:
            return
        self._vg_recalc()
        self._vg_last_first_row = -1
        self._vg_check_visible()
