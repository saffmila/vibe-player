"""Window geometry, DPI scaling, paned splitters, and basic CTk dialogs."""
from __future__ import annotations

import ctypes
import logging
import os
import tkinter as tk

import customtkinter as ctk


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

            last_path = self.get_last_recent_directory()

            if last_path and os.path.exists(last_path):
                logging.info(f"[STARTUP] Restoring last folder: {last_path}")
                self.expand_tree_to_path(last_path)
                self.current_directory = last_path
                self.display_thumbnails(last_path)
                self.update_quick_access_combo(last_path)
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
            expected_min_paned_h = max(80, main_window_height - 160)
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
                    frac = frac_left if frac_left is not None else top_fraction
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
                    frac = frac_right if frac_right is not None else top_fraction
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

        except Exception as e:
            logging.error(f"[ERROR] set_initial_split_heights (outer) failed: {e}")


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
        """Display an error message dialog."""
        error_window = ctk.CTkToplevel(self)
        error_window.title(title)
        self._center_toplevel_window(error_window, 400, 200)
        error_window.resizable(False, False)

        label = ctk.CTkLabel(error_window, text=message, wraplength=350, anchor="w", justify="left")
        label.pack(padx=10, pady=10)

        btn_ok = ctk.CTkButton(error_window, text="OK", command=error_window.destroy)
        btn_ok.pack(pady=10)

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
        """Callback triggered when a directory change is detected."""
        if not os.path.exists(self.current_directory):
            logging.info(f"WARNING: Current directory no longer exists: {self.current_directory}")
            return

        if os.path.commonpath([path, self.current_directory]) == self.current_directory:
            logging.info(f"Directory change detected for: {path}  current dir: {self.current_directory}")
            self.display_thumbnails(
                self.current_directory, preserve_scroll=True
            )  # Refresh thumbnails for the current directory
        else:
            logging.info(f"Change detected outside current directory: {path}  current dir: {self.current_directory} (No action taken)")

    def start_directory_watcher(self, dir_path):
        pass



