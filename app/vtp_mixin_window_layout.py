"""Window geometry, DPI scaling, paned splitters, and basic CTk dialogs."""
from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
import time
import tkinter as tk

import customtkinter as ctk
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class DirectoryChangeHandler(FileSystemEventHandler):
    """Forward filesystem events to the UI (callback must marshal to the Tk thread)."""

    _IGNORE_EVENT_TYPES = frozenset({"opened", "closed"})
    _IGNORE_SUFFIXES = (
        ".tmp",
        ".temp",
        ".crdownload",
        ".part",
        ".partial",
        ".log",
        ".log.1",
        "~",
    )

    def __init__(self, on_change_callback, is_active_callback=None):
        self.on_change_callback = on_change_callback
        self.is_active_callback = is_active_callback

    def on_any_event(self, event):
        if self.is_active_callback is not None and not self.is_active_callback():
            return
        event_type = getattr(event, "event_type", None)
        if event_type in self._IGNORE_EVENT_TYPES:
            return

        paths = [event.src_path]
        dest = getattr(event, "dest_path", None)
        if dest:
            paths.append(dest)

        for path in paths:
            if not path:
                continue
            lower = path.lower()
            if lower.endswith(self._IGNORE_SUFFIXES):
                continue
            logging.debug("Watchdog %s: %s", event_type or "event", path)
            self.on_change_callback(path)


class VtpWindowLayoutMixin:
    def initialize_gui_content(self):
        """
        Orchestrates initial GUI loading in two deferred phases so the main
        window finishes rendering before any blocking work begins.
        """
        logging.info("[DEBUG] Running `initialize_gui_content` after delay...")

        def _phase2():
            """
            Phase 2: Restore tree state and load thumbnails for the last
            visited directory. Runs after populate_tree completes.
            """
            self.refresh_virtual_libraries()
            self.restore_tree_state()

            start_path = os.environ.pop("VIBE_START_DIRECTORY", "").strip()
            if start_path and os.path.isfile(start_path):
                start_path = os.path.dirname(os.path.abspath(start_path))
            if start_path and os.path.isdir(start_path):
                logging.info(f"[STARTUP] Opening requested folder: {start_path}")
                self.expand_tree_to_path(start_path, select_final_node=False)
                self.display_thumbnails(start_path)
                self.update_quick_access_combo(start_path)
                try:
                    self.select_current_folder_in_tree()
                except Exception:
                    pass
                try:
                    self.add_to_recent_directories(start_path)
                except Exception:
                    pass
                return

            last_path = self.get_last_recent_directory()

            if last_path and os.path.exists(last_path):
                logging.info(f"[STARTUP] Restoring last folder: {last_path}")
                # Do not pre-assign current_directory — display_thumbnails would treat
                # it as same-folder and skip restoring the saved scroll position.
                self.expand_tree_to_path(last_path, select_final_node=False)
                self.display_thumbnails(last_path)
                self.update_quick_access_combo(last_path)
                try:
                    self.select_current_folder_in_tree()
                except Exception:
                    pass
            else:
                logging.info(f"[STARTUP] No history, loading default: {self.current_directory}")
                self.display_thumbnails(self.current_directory)
                self.update_quick_access_combo(self.current_directory)

        def _phase1():
            """
            Phase 1: Build the filesystem tree (drives + special folders).
            Deferred via after(0) so the window is fully visible first.
            """
            self.populate_tree()
            # Schedule Phase 2 after the tree has been inserted into the UI
            self.after(0, _phase2)

        # Yield one more frame to the Tk event loop before any blocking work
        self.after(0, _phase1)

        # Catalog stats: instant paint from cache, cheap recount in background,
        # heavy per-file disk scan deferred to idle time.
        self._init_catalog_panel()

    # ------------------------------------------------------------------ #
    #  CATALOG STATS (counts + idle disk scan + disk cache)               #
    # ------------------------------------------------------------------ #

    CATALOG_IDLE_THRESHOLD_S = 20.0   # seconds of inactivity before a disk scan
    CATALOG_IDLE_POLL_MS     = 4000   # how often to check for idleness

    def _catalog_cache_path(self):
        base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "catalog_stats_cache.json")

    def _load_catalog_cache(self):
        try:
            with open(self._catalog_cache_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_catalog_cache(self, data):
        try:
            with open(self._catalog_cache_path(), "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError as e:
            logging.warning(f"[Catalog] cache save failed: {e}")

    def _init_catalog_panel(self):
        info_panel = getattr(self, "info_panel", None)
        if info_panel is None:
            return

        self._catalog_disk_running = False
        self._last_activity_ts = time.time()
        self._catalog_cache = self._load_catalog_cache()

        # Instant paint from last session's cache (no disk work).
        cache = self._catalog_cache
        if cache.get("stats") and hasattr(info_panel, "update_catalog_stats"):
            info_panel.update_catalog_stats(cache["stats"])
        if cache.get("disk_usage") and hasattr(info_panel, "update_disk_usage"):
            info_panel.update_disk_usage(cache["disk_usage"], cache.get("disk_computed_at"))

        # Cheap recount (single SQL scan) refreshes the numbers right away.
        self.refresh_catalog_stats()

        # Track activity so heavy disk work only runs while the user is idle.
        for seq in ("<Motion>", "<KeyPress>", "<ButtonPress>", "<MouseWheel>"):
            try:
                self.bind_all(seq, self._mark_user_activity, add="+")
            except Exception:
                pass
        self.after(self.CATALOG_IDLE_POLL_MS, self._catalog_idle_tick)

    def _mark_user_activity(self, _event=None):
        self._last_activity_ts = time.time()

    def refresh_catalog_stats(self):
        """Recompute cheap catalog counts in a worker thread and update the info panel.

        Kept off the Tk main thread (scans the 'files' table); results are pushed back
        via after(0, ...) since Tk widgets are not thread-safe.
        """
        info_panel = getattr(self, "info_panel", None)
        if info_panel is None or not hasattr(info_panel, "update_catalog_stats"):
            return

        def _worker():
            try:
                stats = self.database.get_global_catalog_stats()
            except Exception as e:
                logging.error(f"[Catalog] Failed to compute global stats: {e}")
                return
            self.after(0, lambda: self._apply_catalog_stats(stats))

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_catalog_stats(self, stats):
        info_panel = getattr(self, "info_panel", None)
        if info_panel is not None and hasattr(info_panel, "update_catalog_stats"):
            info_panel.update_catalog_stats(stats)
        cache = getattr(self, "_catalog_cache", {}) or {}
        cache["stats"] = stats
        self._catalog_cache = cache
        self._save_catalog_cache(cache)

    def _catalog_disk_scan_stale(self):
        """True if the disk-usage cache is missing or the file count changed since it ran."""
        cache = getattr(self, "_catalog_cache", {}) or {}
        if not cache.get("disk_usage"):
            return True
        cur_total = (cache.get("stats") or {}).get("total_files")
        return cache.get("disk_signature") != cur_total

    def _catalog_idle_tick(self):
        """Periodic check: if the user has been idle long enough, run the disk scan once."""
        try:
            if not getattr(self, "_catalog_disk_running", False):
                idle_s = time.time() - getattr(self, "_last_activity_ts", time.time())
                if idle_s >= self.CATALOG_IDLE_THRESHOLD_S and self._catalog_disk_scan_stale():
                    self._start_idle_disk_scan()
        except Exception as e:
            logging.debug(f"[Catalog] idle tick error: {e}")
        finally:
            try:
                self.after(self.CATALOG_IDLE_POLL_MS, self._catalog_idle_tick)
            except Exception:
                pass

    def _start_idle_disk_scan(self):
        self._catalog_disk_running = True
        logging.info("[Catalog] idle disk-usage scan started")

        def _worker():
            t0 = time.perf_counter()
            try:
                usage = self.database.get_disk_usage_by_drive()
            except Exception as e:
                logging.error(f"[Catalog] disk scan failed: {e}")
                usage = {}
            elapsed = time.perf_counter() - t0
            logging.info(
                "[Catalog] idle disk-usage scan done in %.2fs: %s",
                elapsed,
                {k: round(v / (1024 ** 3), 2) for k, v in usage.items()},
            )
            self.after(0, lambda: self._on_idle_disk_done(usage))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_idle_disk_done(self, usage):
        self._catalog_disk_running = False
        ts = time.time()
        info_panel = getattr(self, "info_panel", None)
        if info_panel is not None and hasattr(info_panel, "update_disk_usage"):
            info_panel.update_disk_usage(usage, ts)
        cache = getattr(self, "_catalog_cache", {}) or {}
        cache["disk_usage"] = usage
        cache["disk_computed_at"] = ts
        cache["disk_signature"] = (cache.get("stats") or {}).get("total_files")
        self._catalog_cache = cache
        self._save_catalog_cache(cache)





    def get_windows_scaling_factor(self):
        """Returns current Windows DPI scaling factor (e.g. 1.25 for 125%)."""
        try:
            user32 = ctypes.windll.user32
            hdc = user32.GetDC(0)
            LOGPIXELSX = 88
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
            user32.ReleaseDC(0, hdc)
            scale = dpi / 96.0  # 96 is standard DPI
            return scale
        except Exception as e:
            logging.info(f"[ERROR] Failed to get DPI scaling: {e}")
            return 1.0

    def _repair_main_horizontal_panes(self) -> bool:
        """
        CustomTkinter DPI/window scaling can detach slaves from a classic tk.PanedWindow,
        leaving panes() empty and a short gray strip. Re-add left_frame / right_frame.
        """
        try:
            pw = self.paned_window
            if not pw.winfo_exists():
                return False
            panes = pw.panes()
            if len(panes) >= 2:
                return True

            logging.warning(
                "[SPLITTER REPAIR] Main PanedWindow has %d pane(s); re-adding left/right.",
                len(panes),
            )
            lf, rf = self.left_frame, self.right_frame
            if not lf.winfo_exists() or not rf.winfo_exists():
                logging.error("[SPLITTER REPAIR] left_frame or right_frame missing.")
                return False

            for path in list(panes):
                try:
                    slave = self.nametowidget(path)
                except (KeyError, tk.TclError):
                    slave = path
                try:
                    pw.forget(slave)
                except tk.TclError:
                    pass

            for w in (lf, rf):
                try:
                    pw.forget(w)
                except tk.TclError:
                    pass

            pw.add(lf)
            pw.add(rf)

            try:
                pw.pack_info()
            except tk.TclError:
                pw.pack(fill=ctk.BOTH, expand=True, padx=0, pady=0)

            self.update_idletasks()
            ok = len(pw.panes()) >= 2
            if not ok:
                logging.error("[SPLITTER REPAIR] Still only %d pane(s) after re-add.", len(pw.panes()))
            return ok
        except Exception as e:
            logging.error("[SPLITTER REPAIR] failed: %s", e)
            return False

    def set_initial_split_heights(self, top_fraction=0.75, _split_retry=0):
        """
        Sets the sash positions for the PanedWindows.
        Uses saved fractions from preferences if available, else top_fraction.
        """
        try:
            # Let Tk finish pending geometry (critical after monitor / DPI change).
            self.update_idletasks()

            if not self._repair_main_horizontal_panes():
                if _split_retry < 15:
                    self.after(
                        100,
                        lambda tf=top_fraction, r=_split_retry + 1: self.set_initial_split_heights(
                            tf, _split_retry=r
                        ),
                    )
                return

            main_window_height = self.winfo_height()
            parent_left_h = self.left_frame.winfo_height()
            parent_right_h = self.right_frame.winfo_height()

            frac_main = getattr(self, "_saved_main_sash_fraction", None)
            frac_left = getattr(self, "_saved_left_sash_fraction", None)
            frac_right = getattr(self, "_saved_right_sash_fraction", None)

            # Use actual widget heights for sash calculation, not parent frame heights
            actual_left_h = self.left_split.winfo_height()
            actual_right_h = self.right_split.winfo_height()
            pw_h = self.paned_window.winfo_height()
            logging.info(f"[SPLITTER APPLY] frame_h: left={parent_left_h}, right={parent_right_h} | actual_split_h: left={actual_left_h}, right={actual_right_h} | paned_h={pw_h} | panes_main={len(self.paned_window.panes())} | saved: main={frac_main}, left={frac_left}, right={frac_right}, top_fraction={top_fraction}")

            if parent_left_h < (main_window_height * 0.8) or parent_right_h < (main_window_height * 0.8):
                logging.warning(f"[DEBUG] Parent Frame heights ({parent_left_h} / {parent_right_h}) still seem potentially stale compared to Main Window ({main_window_height}). Proceeding.")

            # Use actual split heights if > 10, else fall back to parent frame height
            eff_left_h = actual_left_h if actual_left_h > 10 else parent_left_h
            eff_right_h = actual_right_h if actual_right_h > 10 else parent_right_h

            # After moving between monitors, winfo_height on inner frames often lags behind
            # the real PanedWindow height — using the smaller value breaks vertical sashes
            # (gray empty band + content pushed down). Prefer the paned window height when
            # it is clearly taller than what the children report.
            if pw_h > 50:
                if eff_left_h + 30 < pw_h:
                    eff_left_h = pw_h
                if eff_right_h + 30 < pw_h:
                    eff_right_h = pw_h

            # If panes exist but height is still implausible, repair again and retry (CTk race).
            # Use larger top-UI allowance to prevent repeated false "too low"
            # detections and infinite Tkinter splitter repair/retry loops.
            expected_min_paned_h = max(80, main_window_height - 350)
            if (
                _split_retry < 15
                and main_window_height > 120
                and len(self.paned_window.panes()) >= 2
                and pw_h + 50 < expected_min_paned_h
            ):
                logging.info(
                    f"[SPLITTER APPLY] paned_h={pw_h} still low vs root (expect ~{expected_min_paned_h}); repair+retry {_split_retry}"
                )
                self._repair_main_horizontal_panes()
                self.after(
                    120,
                    lambda tf=top_fraction, r=_split_retry + 1: self.set_initial_split_heights(
                        tf, _split_retry=r
                    ),
                )
                return

            # Main splitter (folder tree vs thumbnails) - horizontal
            if frac_main is not None and 0.05 <= frac_main <= 0.95:
                try:
                    pw = self.paned_window.winfo_width()
                    main_panes = self.paned_window.panes()
                    if pw > 10 and len(main_panes) >= 2:
                        x_sash = int(pw * frac_main)
                        self.paned_window.sash_place(0, x_sash, 0)
                        logging.info(f"[SPLITTER APPLY] main: applied frac={frac_main} -> x_sash={x_sash} (pw={pw})")
                    else:
                        logging.info(
                            f"[SPLITTER APPLY] main: skip (panes={len(main_panes)}, pw={pw}), will retry"
                        )
                        self._repair_main_horizontal_panes()
                        if _split_retry < 15:
                            self.after(
                                100,
                                lambda tf=top_fraction, r=_split_retry + 1: self.set_initial_split_heights(
                                    tf, _split_retry=r
                                ),
                            )
                            return
                        logging.warning("[SPLITTER APPLY] main: giving up after retries; skip vertical sashes this pass.")
                        return
                except Exception as e:
                    logging.info(f"[SPLITTER APPLY] main failed: {e}")
                    self._repair_main_horizontal_panes()
                    if _split_retry < 15:
                        self.after(
                            100,
                            lambda tf=top_fraction, r=_split_retry + 1: self.set_initial_split_heights(
                                tf, _split_retry=r
                            ),
                        )
                        return
                    logging.warning("[SPLITTER APPLY] main: exception, giving up; skip vertical sashes.")
                    return

            # Use saved fractions if available (clamp to valid range)
            if frac_left is not None and not (0.05 <= frac_left <= 0.95):
                frac_left = None
            if frac_right is not None and not (0.05 <= frac_right <= 0.95):
                frac_right = None

            # Left splitter (folder tree vs preview)
            try:
                left_panes = self.left_split.panes()
                if len(left_panes) > 1:
                    info_panel = getattr(self, "info_panel_container", None)
                    if info_panel is not None and not getattr(info_panel, "expanded", True):
                        logging.info(
                            "[SPLITTER APPLY] left: info panel collapsed — skip frac sash, enforce header height"
                        )
                    else:
                        frac = frac_left if frac_left is not None else top_fraction
                        if (
                            info_panel is not None
                            and getattr(info_panel, "expanded", True)
                            and hasattr(info_panel, "get_restore_height")
                        ):
                            restore_h = int(info_panel.get_restore_height(prefer_current=False))
                            y_sash_left = max(24, min(eff_left_h - 24, eff_left_h - restore_h))
                            logging.info(
                                f"[SPLITTER APPLY] left: using restore_height={restore_h} -> y_sash={y_sash_left} (eff_h={eff_left_h})"
                            )
                        else:
                            y_sash_left = int(eff_left_h * frac)
                        self.left_split.sash_place(0, 0, y_sash_left)
                        logging.info(f"[SPLITTER APPLY] left: frac={frac} -> y_sash={y_sash_left} (eff_h={eff_left_h})")
                        # Verify: read back actual sash coord after placement
                        actual_coord = self.left_split.sash_coord(0)
                        logging.info(f"[SPLITTER APPLY] left: sash_coord after set = {actual_coord}")
                else:
                    logging.info("[SPLITTER APPLY] left: skipping (only 1 panel)")
            except Exception as e:
                logging.error(f"[ERROR] Failed to place *left* sash: {e}")

            # Right splitter (thumbnails vs timeline)
            try:
                right_panes = self.right_split.panes()
                if len(right_panes) > 1:
                    timeline_panel = getattr(self, "timeline_container", None)
                    if timeline_panel is not None and not getattr(timeline_panel, "expanded", True):
                        # Never apply a saved/expanded fraction onto the collapsed proxy —
                        # that briefly (or permanently, if enforce races) leaves a tall empty bar
                        # with only the title / ▲ and no Captions content.
                        logging.info(
                            "[SPLITTER APPLY] right: timeline collapsed — skip frac sash, enforce header height"
                        )
                    else:
                        frac = frac_right if frac_right is not None else top_fraction
                        if (
                            timeline_panel is not None
                            and getattr(timeline_panel, "expanded", True)
                            and hasattr(timeline_panel, "get_restore_height")
                        ):
                            restore_h = int(timeline_panel.get_restore_height(prefer_current=False))
                            y_sash_right = max(24, min(eff_right_h - 24, eff_right_h - restore_h))
                            logging.info(
                                f"[SPLITTER APPLY] right: using restore_height={restore_h} -> y_sash={y_sash_right} (eff_h={eff_right_h})"
                            )
                        else:
                            y_sash_right = int(eff_right_h * frac)
                        self.right_split.sash_place(0, 0, y_sash_right)
                        logging.info(f"[SPLITTER APPLY] right: frac={frac} -> y_sash={y_sash_right} (eff_h={eff_right_h})")
                        # Verify: read back actual sash coord after placement
                        actual_coord = self.right_split.sash_coord(0)
                        logging.info(f"[SPLITTER APPLY] right: sash_coord after set = {actual_coord}")
                else:
                    logging.info("[SPLITTER APPLY] right: skipping (only 1 panel)")
            except Exception as e:
                logging.error(f"[ERROR] Failed to place *right* sash: {e}")

            self._enforce_collapsed_panel_heights()

        except Exception as e:
            logging.error(f"[ERROR] set_initial_split_heights (outer) failed: {e}")

    def _enforce_collapsed_panel_heights(self):
        """Keep minimized panel headers compact after splitter/DPI layout passes."""
        for attr in ("info_panel_container", "timeline_container"):
            panel = getattr(self, attr, None)
            if panel is not None and hasattr(panel, "enforce_collapsed_height"):
                try:
                    panel.enforce_collapsed_height()
                except Exception as e:
                    logging.debug("[SPLITTER APPLY] enforce collapsed %s failed: %s", attr, e)

    def set_default_window_geometry(self, scale=0.9):
        """Set window size based on screen resolution and DPI scaling."""
        try:
            from screeninfo import get_monitors
            screen = get_monitors()[0]

            dpi_scale = self.get_windows_scaling_factor()
            usable_width = int(screen.width / dpi_scale)
            usable_height = int(screen.height / dpi_scale)

            width = int(usable_width * scale)
            height = int(usable_height * scale)

            self.geometry(f"{width}x{height}")
            logging.info(f"[DEBUG] Default geometry set to {width}x{height} (DPI scale={dpi_scale:.2f})")
        except Exception as e:
            logging.info(f"[ERROR] set_default_window_geometry failed: {e}")
            self.geometry("1280x720")


    def toggle_fullscreen(self, event=None):
            """Toggle between maximized and normal window state."""
            try:
                # ... state toggle ...
                is_zoomed = self.state() == 'zoomed'
                if not is_zoomed:
                    self.last_geometry = self.geometry()
                    self.state('zoomed')
                else:
                    self.state('normal')
                    if hasattr(self, 'last_geometry') and self.last_geometry:
                        self.geometry(self.last_geometry)

                def update_sash_safely():
                    # self.update_idletasks()
                    # --- call without height argument ---
                    self.set_initial_split_heights(top_fraction=0.75)
                    # --- end change ---

                self.after(50, update_sash_safely)

            except Exception as e:
                logging.info(f"[ERROR] toggle_fullscreen failed: {e}")

 
        
    def toggle_all_fields(self):
        """
        Handles the click on the 'All Fields' checkbutton.
        It sets all other individual checkbuttons to match the state of 'All Fields'.
        """
        is_checked = self.file_info_vars["all_fields"].get()

        for key, var in self.file_info_vars.items():
            if key != "all_fields":
                var.set(is_checked)
        
        # NOTE: Make sure you call the correct update function here.
        # If the main update function is named differently, change it.
        self.update_thumbnail_info() 

    def sync_all_fields_checkbox(self):
        """
        Handles clicks on any individual checkbutton (e.g., Name, Path, Size).
        It checks if all individual options are selected and updates the 'All Fields' checkbox.
        """
        all_others_are_checked = True
        
        for key, var in self.file_info_vars.items():
            if key != "all_fields":
                if not var.get():
                    all_others_are_checked = False
                    break 

        self.file_info_vars["all_fields"].set(all_others_are_checked)

        # NOTE: Make sure you call the correct update function here too.
        self.update_thumbnail_info()

    def show_error_message(self, title, message):
        """Display a modal error dialog. Reuses an open one instead of stacking copies."""
        existing = getattr(self, "_error_dialog", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.title(title)
                    label = getattr(self, "_error_dialog_label", None)
                    if label is not None and label.winfo_exists():
                        label.configure(text=message)
                    existing.lift()
                    existing.focus_force()
                    try:
                        existing.grab_set()
                    except tk.TclError:
                        pass
                    return
            except tk.TclError:
                self._error_dialog = None
                self._error_dialog_label = None

        error_window = ctk.CTkToplevel(self)
        self._error_dialog = error_window
        error_window.title(title)
        self._center_toplevel_window(error_window, 400, 200)
        error_window.resizable(False, False)
        try:
            error_window.transient(self)
        except Exception:
            pass
        error_window.attributes("-topmost", True)

        label = ctk.CTkLabel(error_window, text=message, wraplength=350, anchor="w", justify="left")
        label.pack(padx=10, pady=10)
        self._error_dialog_label = label

        def _close():
            if getattr(self, "_error_dialog", None) is error_window:
                self._error_dialog = None
                self._error_dialog_label = None
            try:
                error_window.grab_release()
            except tk.TclError:
                pass
            if error_window.winfo_exists():
                error_window.destroy()

        btn_ok = ctk.CTkButton(error_window, text="OK", command=_close)
        btn_ok.pack(pady=10)
        error_window.protocol("WM_DELETE_WINDOW", _close)
        error_window.bind("<Return>", lambda _e: _close())
        error_window.bind("<Escape>", lambda _e: _close())

        try:
            error_window.grab_set()
        except tk.TclError:
            pass
        error_window.lift()
        error_window.focus_force()
        error_window.wait_window()

    def _center_toplevel_window(
        self,
        window,
        width: int | None = None,
        height: int | None = None,
        *,
        center_on_parent: bool = False,
    ):
        """
        Center a toplevel on the primary screen (default) or on the main app window (center_on_parent=True).
        The latter tracks the monitor where the app lives and avoids tiny geometry on mixed-DPI setups.
        """
        try:
            window.update_idletasks()
            w = width if width is not None else max(window.winfo_width(), window.winfo_reqwidth())
            h = height if height is not None else max(window.winfo_height(), window.winfo_reqheight())
            if center_on_parent:
                self.update_idletasks()
                rx = self.winfo_rootx()
                ry = self.winfo_rooty()
                rw = self.winfo_width()
                rh = self.winfo_height()
                x = rx + max(0, (rw - w) // 2)
                y = ry + max(0, (rh - h) // 2)
            else:
                sw = window.winfo_screenwidth()
                sh = window.winfo_screenheight()
                x = max(0, (sw - w) // 2)
                y = max(0, (sh - h) // 2)
            window.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass
       
    
    def on_directory_change(self, path):
        """Watchdog thread callback — marshal to the Tk main thread and debounce."""
        # Never touch Tk / after() while stopping: that deadlocks with observer.join()
        # on the UI thread (emitter waits for Tcl, UI waits for emitter).
        if getattr(self, "_watchdog_stopping", False):
            return
        if getattr(self, "_watchdog_suspended", False):
            return
        if not getattr(self, "auto_refresh_folder", False):
            return
        watched = getattr(self, "_watched_directory", None)
        if watched and path:
            try:
                abs_path = os.path.normcase(os.path.abspath(path))
                abs_watched = os.path.normcase(watched)
                parent = os.path.normcase(os.path.dirname(abs_path))
                if abs_path != abs_watched and parent != abs_watched:
                    return
            except (OSError, TypeError, ValueError):
                return
        try:
            self.after(0, lambda p=path: self._on_directory_change_main(p))
        except Exception:
            logging.debug("Watchdog: failed to schedule UI callback", exc_info=True)

    def _on_directory_change_main(self, path):
        """Main-thread handler: coalesce bursts of FS events into one soft refresh."""
        if getattr(self, "_watchdog_stopping", False):
            return
        if getattr(self, "_watchdog_suspended", False):
            return
        if not getattr(self, "auto_refresh_folder", False):
            return
        if getattr(self, "search_results_active", False):
            return

        cd = getattr(self, "current_directory", None)
        if not cd or not isinstance(cd, str) or cd.startswith("virtual_library://"):
            return

        cache_root = getattr(self, "thumbnail_cache_path", None)
        app_dir = getattr(self, "default_directory", None)
        try:
            abs_path = os.path.abspath(path)
            abs_cd = os.path.abspath(cd)
            if app_dir:
                abs_app = os.path.abspath(app_dir)
                if os.path.normcase(abs_cd) == os.path.normcase(abs_app):
                    return
            if cache_root:
                abs_cache = os.path.abspath(cache_root)
                if os.path.normcase(abs_path).startswith(os.path.normcase(abs_cache) + os.sep):
                    return
                if os.path.normcase(abs_path) == os.path.normcase(abs_cache):
                    return
            common = os.path.commonpath([abs_path, abs_cd])
        except (ValueError, OSError, TypeError):
            return

        if os.path.normcase(common) != os.path.normcase(abs_cd):
            return

        logging.debug(
            "Watchdog change in current folder: %s (cd=%s)", path, cd
        )
        self._schedule_watchdog_refresh()

    def _cancel_watchdog_debounce(self):
        job = getattr(self, "_watchdog_debounce_id", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
            self._watchdog_debounce_id = None

    def _schedule_watchdog_refresh(self):
        self._cancel_watchdog_debounce()
        delay = int(getattr(self, "_watchdog_debounce_ms", 15000) or 15000)
        self._watchdog_debounce_id = self.after(delay, self._apply_watchdog_refresh)

    def _apply_watchdog_refresh(self):
        self._watchdog_debounce_id = None
        if getattr(self, "_watchdog_stopping", False):
            return
        if getattr(self, "_watchdog_suspended", False):
            return
        if not getattr(self, "auto_refresh_folder", False):
            return
        if getattr(self, "search_results_active", False):
            return
        if getattr(self, "_is_loading", False):
            # Folder load in progress — retry a few times, then drop.
            retries = int(getattr(self, "_watchdog_load_retries", 0))
            if retries >= 25:
                self._watchdog_load_retries = 0
                return
            self._watchdog_load_retries = retries + 1
            self._watchdog_debounce_id = self.after(200, self._apply_watchdog_refresh)
            return
        self._watchdog_load_retries = 0

        cd = getattr(self, "current_directory", None)
        if not cd or not isinstance(cd, str) or cd.startswith("virtual_library://"):
            return
        if not os.path.isdir(cd):
            logging.info("Watchdog: current directory no longer exists: %s", cd)
            return

        logging.info("Watchdog auto-refresh: %s", cd)
        if hasattr(self, "refresh_current_directory"):
            self.refresh_current_directory()
        else:
            self.display_thumbnails(cd, preserve_scroll=True)

    def _watchdog_is_active(self) -> bool:
        return (
            not getattr(self, "_watchdog_stopping", False)
            and not getattr(self, "_watchdog_suspended", False)
            and bool(getattr(self, "auto_refresh_folder", False))
        )

    def _is_watch_forbidden_directory(self, abs_dir: str) -> bool:
        """Do not watch the app install dir (app.log feedback loop) or thumbnail cache."""
        abs_dir_n = os.path.normcase(abs_dir)
        app_dir = getattr(self, "default_directory", None)
        if app_dir:
            try:
                if abs_dir_n == os.path.normcase(os.path.normpath(os.path.abspath(app_dir))):
                    return True
            except (OSError, TypeError, ValueError):
                pass
        cache_root = getattr(self, "thumbnail_cache_path", None)
        if cache_root:
            try:
                abs_cache = os.path.normcase(os.path.normpath(os.path.abspath(cache_root)))
                if abs_dir_n == abs_cache or abs_dir_n.startswith(abs_cache + os.sep):
                    return True
            except (OSError, TypeError, ValueError):
                pass
        return False

    def suspend_directory_watcher(self):
        """Ignore FS events during our own file ops (DnD / paste / delete)."""
        self._watchdog_suspended = True
        try:
            if threading.current_thread() is threading.main_thread():
                self._cancel_watchdog_debounce()
            else:
                self.after(0, self._cancel_watchdog_debounce)
        except Exception:
            pass

    def resume_directory_watcher(self, restart=True):
        """Re-enable FS events; optionally (re)start watch on current folder."""
        self._watchdog_suspended = False
        if not restart:
            return
        cd = getattr(self, "current_directory", None)
        if cd:
            try:
                if threading.current_thread() is threading.main_thread():
                    self.start_directory_watcher(cd)
                else:
                    self.after(0, lambda p=cd: self.start_directory_watcher(p))
            except Exception:
                logging.debug("resume_directory_watcher schedule failed", exc_info=True)

    def stop_directory_watcher(self):
        """Stop and discard the Observer without blocking the UI thread."""
        self._watchdog_stopping = True
        self._cancel_watchdog_debounce()
        observer = getattr(self, "watchdog_observer", None)
        # Detach first so handlers / join cannot race with a new start.
        self.watchdog_observer = None
        self.watchdog_handler = None
        self._watched_directory = None
        if observer is not None:
            try:
                observer.stop()
            except Exception as e:
                logging.debug("Watchdog stop signal error: %s", e)

            def _join_observer():
                try:
                    observer.join(timeout=2.0)
                except Exception:
                    pass

            # Never join on the Tk thread: emitter may be blocked in after()/Tcl.
            threading.Thread(target=_join_observer, daemon=True).start()
        self._watchdog_stopping = False

    def start_directory_watcher(self, dir_path):
        """Watch ``dir_path`` for creates/deletes/moves; soft-refresh the grid (debounced)."""
        if not getattr(self, "auto_refresh_folder", False):
            self.stop_directory_watcher()
            return
        if not dir_path or not isinstance(dir_path, str):
            self.stop_directory_watcher()
            return
        if dir_path.startswith("virtual_library://"):
            self.stop_directory_watcher()
            return
        if getattr(self, "search_results_active", False):
            self.stop_directory_watcher()
            return
        try:
            if not os.path.isdir(dir_path):
                self.stop_directory_watcher()
                return
            abs_dir = os.path.normpath(os.path.abspath(dir_path))
        except (OSError, TypeError, ValueError):
            self.stop_directory_watcher()
            return

        if self._is_watch_forbidden_directory(abs_dir):
            logging.info("Directory watcher skipped (app/cache path): %s", abs_dir)
            self.stop_directory_watcher()
            return

        # Already watching this folder with a live observer — keep it.
        if (
            self.watchdog_observer is not None
            and self.watchdog_observer.is_alive()
            and self._watched_directory is not None
            and os.path.normcase(self._watched_directory) == os.path.normcase(abs_dir)
        ):
            return

        self.stop_directory_watcher()
        try:
            handler = DirectoryChangeHandler(
                self.on_directory_change,
                is_active_callback=self._watchdog_is_active,
            )
            observer = Observer()
            observer.schedule(handler, abs_dir, recursive=False)
            observer.daemon = True
            observer.start()
            self.watchdog_handler = handler
            self.watchdog_observer = observer
            self._watched_directory = abs_dir
            logging.info("Directory watcher started: %s", abs_dir)
        except Exception as e:
            logging.warning("Failed to start directory watcher for %s: %s", abs_dir, e)
            self.watchdog_observer = None
            self.watchdog_handler = None
            self._watched_directory = None

    def _schedule_directory_watcher(self, dir_path):
        """Start watcher after the current UI turn (never inline during folder init)."""
        try:
            self.after_idle(lambda p=dir_path: self.start_directory_watcher(p))
        except Exception:
            try:
                self.after(0, lambda p=dir_path: self.start_directory_watcher(p))
            except Exception:
                logging.debug("Failed to schedule directory watcher", exc_info=True)


