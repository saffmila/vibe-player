"""Drag-and-drop (tkinterdnd2) mixin for VideoThumbnailPlayer."""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import threading
import time

import tkinter as tk

import tkinterdnd2 as dnd
from gui_elements import (
    get_conflict_rename_path,
    open_conflict_dialog,
    open_file_op_progress_dialog,
)
from virtual_folders import add_to_virtual_folder, remove_from_virtual_folder
from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS


class VtpDndMixin:
    # ═══════════════════════════════════════════════════════════════════════
    # DRAG & DROP  (tkinterdnd2)
    # ═══════════════════════════════════════════════════════════════════════

    def _dnd_is_internal_drag_active(self) -> bool:
        """Return True for active internal drags (including short DragEnd->Drop race window)."""
        if bool(getattr(self, "_dnd_internal_drag", False)):
            return True
        try:
            end_ts = float(getattr(self, "_dnd_internal_drag_end_ts", 0.0) or 0.0)
        except Exception:
            end_ts = 0.0
        if end_ts <= 0.0:
            return False
        return (time.monotonic() - end_ts) <= 0.8

    def _dnd_mark_internal_drag_payload(self, paths: list[str]):
        """Store normalized internal drag payload for robust Drop-side detection."""
        try:
            normalized = [
                os.path.normcase(os.path.normpath(p))
                for p in paths
                if isinstance(p, str) and p
            ]
        except Exception:
            normalized = []
        self._dnd_last_internal_drag_paths = tuple(sorted(set(normalized)))
        self._dnd_last_internal_drag_ts = time.monotonic()

    def _dnd_mark_drag_out_snapshot(self, paths: list[str]):
        """Capture path + is_dir before OLE move so DragEnd can reconcile external drag-out."""
        snap = []
        for p in paths:
            if not isinstance(p, str) or not p:
                continue
            try:
                if os.path.exists(p):
                    snap.append((p, os.path.isdir(p)))
            except OSError:
                pass
        self._dnd_drag_out_snapshot = tuple(snap) if snap else None

    def _dnd_mark_internal_drop_consumed(self):
        """Internal drop handled the move — skip external drag-out tree reconcile."""
        self._dnd_internal_drop_consumed = True

    def _dnd_purge_cache_for_gone_path(self, path: str, *, was_dir: bool):
        """Best-effort cache cleanup when a dragged-out path no longer exists at source."""
        try:
            cache_root = self.thumbnail_cache_path
            abs_path = os.path.abspath(path)
            rel = abs_path.replace(":", "")
            cache_path = os.path.join(cache_root, rel)

            if was_dir:
                if os.path.isdir(cache_path):
                    shutil.rmtree(cache_path, ignore_errors=True)
            else:
                cache_dir = os.path.dirname(cache_path)
                cache_base = os.path.basename(cache_path)
                if os.path.isdir(cache_dir):
                    for fn in os.listdir(cache_dir):
                        if fn.startswith(cache_base):
                            try:
                                os.remove(os.path.join(cache_dir, fn))
                            except Exception:
                                pass

            tc = getattr(self, "thumbnail_cache", None)
            if tc is not None and hasattr(tc, "cache"):
                tc.cache.pop(path, None)
                tc.cache.pop(os.path.normcase(os.path.normpath(path)), None)
                fps = getattr(tc, "fingerprints", None)
                if isinstance(fps, dict):
                    fps.pop(path, None)
                    fps.pop(os.path.normcase(os.path.normpath(path)), None)
            try:
                self.database.update_cache_status(path, False)
            except Exception:
                pass
        except Exception as e:
            logging.warning("[DnD] cache purge after external drag-out failed for %s: %s", path, e)

    def _dnd_reconcile_external_drag_out(self, snapshot: tuple):
        """Refresh tree/grid after paths were moved out to an external app (Explorer, etc.)."""
        self._dnd_drag_out_snapshot = None
        if getattr(self, "_dnd_internal_drop_consumed", False):
            logging.debug("[DnD] external drag-out reconcile skipped (internal drop handled)")
            self._dnd_internal_drop_consumed = False
            return
        if not snapshot:
            return

        def _norm(p):
            try:
                return os.path.normcase(os.path.normpath(os.path.abspath(p)))
            except Exception:
                return os.path.normcase(os.path.normpath(p))

        gone = [(p, was_dir) for p, was_dir in snapshot if not os.path.exists(p)]
        if not gone:
            logging.debug("[DnD] external drag-out reconcile: nothing removed (copy or cancelled)")
            return

        logging.info("[DnD] external drag-out reconcile: %d path(s) left source", len(gone))

        parents_to_refresh = set()
        current_dir_affected = False
        cur = getattr(self, "current_directory", None)
        cur_norm = _norm(cur) if cur else None

        self._suppress_tree_select_navigation = True
        try:
            for path, was_dir in gone:
                parent = os.path.dirname(path)
                if parent:
                    parents_to_refresh.add(parent)

                if was_dir:
                    node = self.find_node_by_path(path)
                    if node:
                        self.tree.delete(node)
                    elif parent:
                        parent_node = self.find_node_by_path(parent)
                        if parent_node and os.path.isdir(parent):
                            self.process_directory(parent_node, parent)
                    try:
                        self.database.remove_entry(path)
                    except Exception:
                        pass
                    if hasattr(self, "_invalidate_folder_preview_caches"):
                        try:
                            self._invalidate_folder_preview_caches(path)
                        except Exception:
                            pass
                else:
                    try:
                        self.database.remove_entry(path)
                    except Exception:
                        pass

                self._dnd_purge_cache_for_gone_path(path, was_dir=was_dir)

                if cur_norm:
                    pn = _norm(path)
                    if cur_norm == pn or cur_norm.startswith(pn + os.sep):
                        current_dir_affected = True
                    elif not was_dir and cur_norm == _norm(parent):
                        current_dir_affected = True

            for parent in parents_to_refresh:
                if os.path.isdir(parent):
                    self.refresh_folder_icon(parent)
                    parent_node = self.find_node_by_path(parent)
                    if parent_node:
                        self.process_directory(parent_node, parent)

            st = getattr(self, "selected_thumbnails", None) or []
            dead = {_norm(p) for p, _ in gone}
            self.selected_thumbnails = [
                t
                for t in st
                if isinstance(t, (list, tuple))
                and len(t) > 0
                and _norm(str(t[0])) not in dead
            ]
            sfp = getattr(self, "selected_file_path", None)
            if sfp and _norm(sfp) in dead:
                self.selected_file_path = None

            if current_dir_affected and cur:
                for path, was_dir in gone:
                    if not was_dir:
                        continue
                    pn = _norm(path)
                    if cur_norm == pn or (cur_norm and cur_norm.startswith(pn + os.sep)):
                        parent = os.path.dirname(path)
                        if parent and os.path.isdir(parent):
                            self.current_directory = parent
                            try:
                                self.select_current_folder_in_tree()
                            except Exception:
                                pass
                        break

            view = getattr(self, "current_directory", None)
            if view and os.path.isdir(view):
                self.display_thumbnails(view, force_refresh=True, preserve_scroll=True)
        finally:
            self.after_idle(
                lambda: setattr(self, "_suppress_tree_select_navigation", False)
            )

    def _dnd_payload_matches_internal(self, paths: list[str]) -> bool:
        """Check whether dropped paths match the most recent internal drag payload."""
        saved = getattr(self, "_dnd_last_internal_drag_paths", ())
        if not saved:
            return False
        try:
            saved_ts = float(getattr(self, "_dnd_last_internal_drag_ts", 0.0) or 0.0)
        except Exception:
            saved_ts = 0.0
        if saved_ts <= 0.0 or (time.monotonic() - saved_ts) > 12.0:
            return False
        try:
            dropped = sorted(
                {
                    os.path.normcase(os.path.normpath(p))
                    for p in paths
                    if isinstance(p, str) and p
                }
            )
        except Exception:
            return False
        return bool(dropped) and tuple(dropped) == tuple(saved)

    def _setup_dnd(self):
        """
        Register drop targets for canvas (thumbnail grid) and tree.
        Call after widgets exist (end of __init__).
        """
        self.canvas.drop_target_register(dnd.DND_FILES)
        self.canvas.dnd_bind("<<Drop>>",         self._dnd_on_drop_canvas)
        self.canvas.dnd_bind("<<DropEnter>>",    self._dnd_on_enter_canvas)
        self.canvas.dnd_bind("<<DropLeave>>",    self._dnd_on_leave_canvas)
        self.canvas.dnd_bind("<<DropPosition>>", self._dnd_on_position_canvas)

        self.tree.drop_target_register(dnd.DND_FILES)
        self.tree.dnd_bind("<<Drop>>",         self._dnd_on_drop_tree)
        self.tree.dnd_bind("<<DropEnter>>",    self._dnd_on_enter_tree)
        self.tree.dnd_bind("<<DropLeave>>",    self._dnd_on_leave_tree)
        self.tree.dnd_bind("<<DropPosition>>", self._dnd_on_position_tree)

        self._dnd_tree_hover_item: str | None = None
        self._dnd_last_move_preview: bool | None = None  # True = move, False = copy
        # Tree autoscroll during drag: Windows DnD often does not run after() — scroll in <<DropPosition>>.
        self._dnd_tree_autoscroll_margin_px = 40
        self._dnd_tree_autoscroll_lines = 2
        self._dnd_tree_autoscroll_min_step_s = 0.045  # min time between scroll steps
        self._dnd_tree_autoscroll_last_ts = 0.0

        self.tree.drag_source_register(dnd.DND_FILES)
        self.tree.dnd_bind("<<DragInitCmd>>", self._dnd_tree_drag_init)
        self.tree.dnd_bind("<<DragEndCmd>>",  self._dnd_drag_end)

        logging.info("[DnD] Drag & Drop initialized (tkinterdnd2).")

    def _dnd_canvas_folder_under_pointer(self, event):
        """Return the folder path of the grid cell under the drop point, or None.

        Standard cells (``canvas``) and wide strips (``strip``/``img_canvas``) stash
        ``file_path``/``is_folder`` on their widgets, so a drop onto a folder cell can
        target that folder instead of always falling back to the current directory.
        """
        try:
            x_root = int(getattr(event, "x_root", 0))
            y_root = int(getattr(event, "y_root", 0))
        except Exception:
            return None

        # 1) Widget directly under the pointer carries the attributes (canvas/strip).
        try:
            probe = self.winfo_containing(x_root, y_root)
        except Exception:
            probe = None
        for _ in range(6):
            if probe is None:
                break
            if getattr(probe, "is_folder", False) and getattr(probe, "file_path", None):
                path = probe.file_path
                if isinstance(path, str) and os.path.isdir(path):
                    return path
            probe = getattr(probe, "master", None)

        # 2) Geometry hit-test against visible folder slots (covers label/padding gaps).
        if not getattr(self, "_vg_active", False):
            return None
        slot_maps = (
            getattr(self, "_vg_visible_std_slots_by_path", {}),
            getattr(self, "_vg_visible_wide_slots_by_path", {}),
        )
        vg_data = getattr(self, "_vg_data", [])
        for slots in slot_maps:
            for slot in list(slots.values()):
                widget = slot.get("frame") or slot.get("strip") or slot.get("canvas")
                if widget is None:
                    continue
                try:
                    if not widget.winfo_ismapped():
                        continue
                    wx, wy = widget.winfo_rootx(), widget.winfo_rooty()
                    ww, wh = widget.winfo_width(), widget.winfo_height()
                except Exception:
                    continue
                if wx <= x_root < wx + ww and wy <= y_root < wy + wh:
                    data_idx = int(slot.get("data_idx", -1))
                    if 0 <= data_idx < len(vg_data):
                        item = vg_data[data_idx]
                        if item.get("is_folder", False):
                            path = item.get("path")
                            if isinstance(path, str) and os.path.isdir(path):
                                return path
        return None

    def _dnd_on_drop_canvas(self, event):
        self._dnd_reset_canvas_highlight()
        logging.info(f"[DnD] DROP IN raw data: {event.data!r}")
        paths = self._dnd_parse_paths(event.data)
        logging.info(f"[DnD] DROP IN parsed paths: {paths}")
        if not paths:
            return

        current_dir = getattr(self, 'current_directory', None)
        # Drop onto a folder cell targets that folder; otherwise the open directory.
        target_folder = self._dnd_canvas_folder_under_pointer(event)
        dest = target_folder or current_dir
        if not self._dnd_is_valid_drop_dest(dest):
            logging.warning("[DnD] DROP canvas: no valid destination: %r", dest)
            return

        files = [p for p in paths if os.path.isfile(p)]
        dirs  = [p for p in paths if os.path.isdir(p)]
        sources = files + dirs

        if not sources:
            logging.warning("[DnD] DROP canvas: no valid paths: %s", paths)
            return

        # Internal drag: default move, Ctrl = copy. Explorer keeps Windows semantics.
        internal = self._dnd_is_internal_drag_active() or self._dnd_payload_matches_internal(paths)
        try:
            ctrl_down = bool(ctypes.windll.user32.GetKeyState(0x11) & 0x8000)
            shift_down = bool(ctypes.windll.user32.GetKeyState(0x10) & 0x8000)
        except Exception:
            ctrl_down = False
            shift_down = False
        if internal:
            is_move = not ctrl_down
        else:
            is_move = shift_down
        logging.info(
            f"[DnD] DROP IN canvas: files={len(files)} dirs={len(dirs)} dest={dest} "
            f"target_folder={target_folder} internal={internal} is_move={is_move}"
        )

        dest_is_virtual = self._dnd_is_virtual_library_path(dest)
        dropped_into_subfolder = bool(
            target_folder
            and current_dir
            and not dest_is_virtual
            and os.path.normcase(os.path.normpath(target_folder))
            != os.path.normcase(os.path.normpath(current_dir))
        )

        def _after_canvas_op():
            if dest_is_virtual:
                view = current_dir if self._dnd_is_valid_drop_dest(current_dir) else dest
                self.display_thumbnails(view, force_refresh=True, preserve_scroll=True)
                return
            self.refresh_folder_icon(dest)
            if dropped_into_subfolder:
                # Stay on the open folder. Copy leaves source thumbs alone; move
                # surgically drops them (full reload only as fallback).
                self._invalidate_folder_preview_caches(dest)
                if not is_move:
                    return
                view = current_dir if (current_dir and os.path.isdir(current_dir)) else dest
                surgical = False
                if view and getattr(self, "_vg_active", False):
                    try:
                        surgical = bool(self._vg_remove_paths(sources))
                    except Exception:
                        logging.exception("[DnD] surgical thumb remove after canvas move failed")
                        surgical = False
                if not surgical:
                    self.display_thumbnails(view, force_refresh=True, preserve_scroll=True)
            else:
                # Drop into the open directory — new items must appear.
                self.display_thumbnails(dest, force_refresh=True, preserve_scroll=True)

        self._dnd_confirm_and_execute(
            sources=sources,
            dest=dest,
            is_move=is_move,
            on_success=_after_canvas_op
        )

    def _dnd_on_enter_canvas(self, event):
        try:
            self.canvas.configure(highlightthickness=2, highlightbackground="#3a7ebf")
        except Exception:
            pass

    def _dnd_on_position_canvas(self, event):
        """Tell tkdnd / OLE which effect applies (move vs copy) so the system drag cursor can differ."""
        internal = self._dnd_is_internal_drag_active()
        try:
            ctrl_down = bool(ctypes.windll.user32.GetKeyState(0x11) & 0x8000)
            shift_down = bool(ctypes.windll.user32.GetKeyState(0x10) & 0x8000)
        except Exception:
            ctrl_down = False
            shift_down = False
        is_move = (not ctrl_down) if internal else shift_down
        return dnd.MOVE if is_move else dnd.COPY

    def _dnd_on_leave_canvas(self, event):
        self._dnd_reset_canvas_highlight()

    def _dnd_reset_canvas_highlight(self):
        try:
            self.canvas.configure(highlightthickness=0)
        except Exception:
            pass

    def _dnd_on_drop_tree(self, event):
        """
        Tree drop:
          A) Files/folders → copy/move to folder under cursor (internal Ctrl-copy vs Explorer Shift-move).
          B) Explorer folder with no hover → navigate (fallback).
        """
        hover = self._dnd_tree_hover_item
        if not hover:
            try:
                tree_y = event.y_root - self.tree.winfo_rooty()
                hover = self.tree.identify_row(tree_y) or None
            except Exception:
                pass

        self._dnd_reset_tree_highlight()

        paths = self._dnd_parse_paths(event.data)
        logging.info(f"[DnD] DROP tree: parsed={paths}")
        if not paths:
            logging.warning("[DnD] DROP tree: no paths after parse")
            return

        try:
            ctrl_down = bool(ctypes.windll.user32.GetKeyState(0x11) & 0x8000)
            shift_down = bool(ctypes.windll.user32.GetKeyState(0x10) & 0x8000)
        except Exception:
            ctrl_down = False
            shift_down = False
        internal = self._dnd_is_internal_drag_active() or self._dnd_payload_matches_internal(paths)
        is_move = (not ctrl_down) if internal else shift_down

        logging.info(
            f"[DnD] DROP tree: internal={internal} is_move={is_move} hover={hover} data={event.data!r}"
        )

        files  = [p for p in paths if os.path.isfile(p)]
        dirs   = [p for p in paths if os.path.isdir(p)]
        unknown = [p for p in paths if not os.path.isfile(p) and not os.path.isdir(p)]
        logging.info(f"[DnD] DROP tree: files={files} dirs={dirs} unknown={unknown} hover={hover}")
        sources = files + dirs

        if sources and hover:
            dest_folder = self._dnd_tree_path_from_item(hover)
            # Virtual-library drops are membership adds (cheap/safe) — skip the anti-slip
            # dwell that often rejects quick Explorer drops onto a VL row.
            if not self._dnd_is_virtual_library_path(dest_folder):
                dwell_ms = (time.monotonic() - (self._dnd_tree_hover_since or 0.0)) * 1000.0
                if dwell_ms < self._dnd_target_dwell_ms:
                    logging.info(
                        "[DnD] DROP tree ignored (short hover %.0fms < %dms)",
                        dwell_ms, self._dnd_target_dwell_ms
                    )
                    return
            logging.info(f"[DnD] DROP tree: dest_folder={dest_folder}")
            if not dest_folder:
                logging.warning("[DnD] DROP tree: target not a folder (hover=%s)", hover)
                return

            # Snapshot folder sources BEFORE the FS move. After shutil.move the old
            # paths are gone, so os.path.isdir(src) would wrongly skip tree surgery
            # and leave a ghost node at the original location.
            folder_sources = list(dirs)
            src_parents = {
                os.path.dirname(s)
                for s in sources
                if os.path.dirname(s)
            }

            def _after_tree_op():
                # Always stay on the open folder. Navigating to dirname(sources[0]) after
                # a move jumped into the Explorer source when dropping external files.
                view = getattr(self, "current_directory", None)
                if not self._dnd_is_valid_drop_dest(view):
                    view = dest_folder

                if self._dnd_is_virtual_library_path(dest_folder):
                    # Show the library that received the drop (Explorer → VL must be visible).
                    self.display_thumbnails(
                        dest_folder,
                        force_refresh=True,
                        preserve_scroll=(
                            self._dnd_virtual_library_name(view or "")
                            == self._dnd_virtual_library_name(dest_folder)
                        ),
                    )
                    try:
                        self.select_current_folder_in_tree()
                    except Exception:
                        pass
                    return

                # Only folder moves need tree-node surgery; file drops are not tree nodes
                # (update_tree_view(file) would walk the whole open tree looking for a jpg).
                self._suppress_tree_select_navigation = True
                try:
                    if is_move:
                        for src in folder_sources:
                            self.update_tree_view(src, dest_folder)
                        for src_parent in src_parents:
                            if src_parent and os.path.isdir(src_parent):
                                self.refresh_folder_icon(src_parent)
                        self.refresh_folder_icon(dest_folder)
                        if folder_sources:
                            self.select_current_folder_in_tree()
                        # Move: drop thumbs in-place (same path as delete) — avoids
                        # the clear+reload flash of display_thumbnails.
                        surgical = False
                        if view and getattr(self, "_vg_active", False):
                            try:
                                surgical = bool(self._vg_remove_paths(sources))
                            except Exception:
                                logging.exception(
                                    "[DnD] surgical thumb remove after tree move failed"
                                )
                                surgical = False
                        if not surgical:
                            self.display_thumbnails(
                                view,
                                force_refresh=True,
                                preserve_scroll=True,
                            )
                    else:
                        # Copy: open folder content is unchanged — do not reload thumbs.
                        # Folder copies need dest tree children; file copies only need
                        # the dest folder icon/preview refreshed.
                        if folder_sources:
                            self.refresh_tree_view(dest_folder)
                            self.select_current_folder_in_tree()
                        self.refresh_folder_icon(dest_folder)
                        if hasattr(self, "_invalidate_folder_preview_caches"):
                            try:
                                self._invalidate_folder_preview_caches(dest_folder)
                            except Exception:
                                pass
                finally:
                    self.after_idle(
                        lambda: setattr(
                            self, "_suppress_tree_select_navigation", False
                        )
                    )

            self._dnd_confirm_and_execute(
                sources=sources,
                dest=dest_folder,
                is_move=is_move,
                on_success=_after_tree_op
            )

        elif dirs:
            path = dirs[0]
            logging.info("[DnD] DROP tree: navigate (no hover): %s", path)
            node = self.find_node_by_path(path)
            if node:
                self.tree.see(node)
                self.tree.selection_set(node)
                self.tree.focus(node)
            self.display_thumbnails(path)
        else:
            logging.warning(
                "[DnD] DROP tree: cannot resolve target (hover=%s, sources=%s, unknown=%s)",
                hover, len(sources), unknown,
            )

    def _dnd_confirm_and_execute(
        self,
        sources: list[str],
        dest: str,
        is_move: bool,
        on_success=None
    ):
        """
        Shared DnD helper: confirm dialog via after(1) (outside DnD handler), file work in a thread.
        Virtual library destinations update membership JSON (no filesystem copy/move).
        """
        if not sources or not dest:
            return

        if self._dnd_is_internal_drag_active() or self._dnd_payload_matches_internal(sources):
            self._dnd_mark_internal_drop_consumed()

        if self._dnd_is_virtual_library_path(dest):
            def _deferred_vl():
                self._dnd_transfer_to_virtual_library(
                    sources, dest, is_move, on_success
                )
            self.after(1, _deferred_vl)
            return

        sources = self._dnd_filter_noop_fs_sources(sources, dest)
        if not sources:
            logging.info(
                "[DnD] nothing to %s (all sources already at destination): dest=%s",
                "move" if is_move else "copy",
                dest,
            )
            return

        def _deferred():
            try:
                self._dnd_release_preview_handles()
            except Exception:
                logging.debug("[DnD] preview release before op failed", exc_info=True)
            with_captions = bool(getattr(self, "copy_move_with_captions", True))
            if getattr(self, "dnd_confirm_dialogs", False):
                self._dnd_show_dialog_and_run(
                    sources, dest, is_move, on_success, with_captions=with_captions
                )
            else:
                self._dnd_start_copy_move(
                    sources, dest, is_move, on_success, with_captions=with_captions
                )

        self.after(1, _deferred)

    def _dnd_filter_noop_fs_sources(self, sources: list[str], dest: str) -> list[str]:
        """
        Drop sources that would be a no-op on disk (already live in dest, or nest-into-self).
        Avoids opening the progress dialog / worker just to skip everything — that path
        has raced with tkdnd OLE teardown and caused native Access Violations.
        """
        if not sources or not dest or self._dnd_is_virtual_library_path(dest):
            return list(sources or [])
        dest_norm = os.path.normcase(os.path.normpath(dest))
        kept = []
        for src in sources:
            if not src:
                continue
            try:
                src_norm = os.path.normcase(os.path.normpath(src))
                dst_norm = os.path.normcase(
                    os.path.normpath(os.path.join(dest, os.path.basename(src)))
                )
            except Exception:
                kept.append(src)
                continue
            if src_norm == dst_norm:
                logging.info("[DnD] skip no-op (already in dest): %s", src)
                continue
            if os.path.isdir(src) and (
                dest_norm == src_norm or dest_norm.startswith(src_norm + os.sep)
            ):
                logging.info("[DnD] skip nest-into-self: %s -> %s", src, dest)
                continue
            kept.append(src)
        return kept

    def _dnd_transfer_to_virtual_library(
        self,
        sources: list[str],
        dest: str,
        is_move: bool,
        on_success=None,
    ):
        """
        Drop onto a virtual library: add path references (files stay on disk).

        Move between VLs (internal drag without Ctrl): remove from the open source VL
        when it differs from the destination. External Explorer drops always *add*
        (Shift still means FS-move semantics elsewhere; here membership is additive
        unless dragging from another open VL with move intent).
        """
        dest_name = self._dnd_virtual_library_name(dest)
        if not dest_name:
            logging.warning("[DnD] VL transfer: invalid dest %r", dest)
            return

        sources = [p for p in sources if p and (os.path.isfile(p) or os.path.isdir(p))]
        if not sources:
            logging.warning("[DnD] VL transfer: no valid source paths")
            return

        # IMPORTANT: do NOT bail out when current_directory == dest.
        # That case is "drop into the open virtual library" (Explorer → canvas) and
        # must still add membership. The old same-library early-return blocked it.

        source_vl = self._dnd_virtual_library_name(
            getattr(self, "current_directory", None) or ""
        )
        # Only strip membership from a *different* source VL on move.
        remove_from = (
            source_vl
            if (is_move and source_vl and source_vl != dest_name)
            else None
        )

        added = 0
        for src in sources:
            try:
                add_to_virtual_folder(dest_name, src)
                added += 1
            except Exception as e:
                logging.warning("[DnD] VL add failed for %s: %s", src, e)

        removed = False
        if remove_from:
            try:
                removed = bool(remove_from_virtual_folder(remove_from, sources))
            except Exception as e:
                logging.warning(
                    "[DnD] VL remove from source %r failed: %s", remove_from, e
                )

        logging.info(
            "[DnD] VL transfer: dest=%r added=%d move=%s removed_from=%r",
            dest_name,
            added,
            is_move,
            remove_from if removed else None,
        )

        if hasattr(self, "refresh_virtual_libraries"):
            try:
                self.refresh_virtual_libraries()
            except Exception:
                logging.debug("[DnD] refresh_virtual_libraries failed", exc_info=True)

        if on_success:
            try:
                on_success()
            except Exception:
                logging.debug("[DnD] VL on_success failed", exc_info=True)

        if hasattr(self, "status_bar") and self.status_bar:
            verb = "Moved" if removed else "Added"
            self.status_bar.set_action_message(
                f"DnD: {verb} {added} item(s) -> {dest_name}"
            )

    def _dnd_should_show_progress(self, sources: list[str]) -> bool:
        """Show a blocking progress dialog for multi-item or folder ops (can look frozen otherwise)."""
        if len(sources) >= 2:
            return True
        return any(os.path.isdir(p) for p in sources if p)

    def _dnd_open_progress_dialog(self, sources: list[str], is_move: bool):
        """Open modal progress dialog on the UI thread; store on self for conflict nesting."""
        action = "Moving" if is_move else "Copying"
        title = "Move" if is_move else "Copy"
        try:
            dialog = open_file_op_progress_dialog(
                self,
                title=title,
                total=len(sources),
                action_label=action,
            )
        except Exception:
            logging.debug("[DnD] progress dialog open failed", exc_info=True)
            self._dnd_progress_dialog = None
            return None
        self._dnd_progress_dialog = dialog
        return dialog

    def _dnd_close_progress_dialog(self):
        dialog = getattr(self, "_dnd_progress_dialog", None)
        self._dnd_progress_dialog = None
        if dialog is None:
            return
        try:
            dialog.close()
        except Exception:
            logging.debug("[DnD] progress dialog close failed", exc_info=True)

    def _dnd_report_progress(self, dialog, index: int, total: int, path: str):
        """Schedule a progress update on the main thread (safe from worker)."""
        if dialog is None:
            return
        name = os.path.basename(path) or path

        def _update():
            try:
                if dialog.winfo_exists():
                    dialog.set_progress(index, total, detail=name)
            except Exception:
                pass

        try:
            self.after(0, _update)
        except Exception:
            pass

    def _dnd_start_copy_move(
        self,
        sources: list[str],
        dest: str,
        is_move: bool,
        on_success=None,
        with_captions: bool | None = None,
    ):
        """Open progress (if needed) on UI thread, then run copy/move in a worker."""
        if with_captions is None:
            with_captions = bool(getattr(self, "copy_move_with_captions", True))
        progress = None
        if self._dnd_should_show_progress(sources):
            progress = self._dnd_open_progress_dialog(sources, is_move)

        threading.Thread(
            target=lambda: self._dnd_execute_copy_move_thread(
                sources,
                dest,
                is_move,
                on_success,
                progress=progress,
                with_captions=with_captions,
            ),
            daemon=True,
        ).start()

    def _cache_path_for_fs_path(self, fs_path: str) -> str:
        """
        Map filesystem path to thumbnail_cache layout (e.g. J:\\a\\b -> <cache_root>\\J\\a\\b).
        """
        abs_path = os.path.abspath(fs_path)
        rel = abs_path.replace(":", "")
        return os.path.join(self.thumbnail_cache_path, rel)

    def _bubble_cached_status_to_ancestors(self, dir_path: str) -> None:
        """Mark dir_path and every dirname ancestor as cached (green chain up to drive root)."""
        if not dir_path:
            return
        try:
            if not os.path.isdir(dir_path):
                return
        except Exception:
            return
        p = os.path.abspath(dir_path)
        seen: set[str] = set()
        while p and p not in seen:
            seen.add(p)
            try:
                self.database.update_cache_status(p, True)
            except Exception as e:
                logging.debug("[DnD] bubble cache True %s: %s", p, e)
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent

    def _mark_media_destination_folder_cached(self, src: str, dst: str, is_move: bool) -> None:
        """After a media file lands in a folder, make that folder immediately green."""
        if not dst or os.path.isdir(dst):
            return
        try:
            if not dst.lower().endswith(VIDEO_FORMATS + IMAGE_FORMATS):
                return
            dest_parent = os.path.dirname(dst)
            if dest_parent and os.path.isdir(dest_parent):
                self.database.update_cache_status(dest_parent, True)
                self._bubble_cached_status_to_ancestors(dest_parent)
        except Exception as e:
            logging.debug("[DnD] mark destination folder cached failed %s -> %s: %s", src, dst, e)

    def _folder_contains_media_for_cache_icon(self, folder_path: str) -> bool:
        if not folder_path or not os.path.isdir(folder_path):
            return False
        contains_media = getattr(self, "contains_media_files", None)
        if callable(contains_media):
            return bool(contains_media(folder_path))
        allowed_extensions = VIDEO_FORMATS + IMAGE_FORMATS
        for _root, _dirs, files in os.walk(folder_path):
            if any(name.lower().endswith(allowed_extensions) for name in files):
                return True
        return False

    def _reset_media_source_folder_if_empty(self, src: str, is_move: bool) -> str | None:
        """After moving media out, reset the source folder if it no longer contains media."""
        if not is_move or not src or os.path.isdir(src):
            return None
        try:
            if not src.lower().endswith(VIDEO_FORMATS + IMAGE_FORMATS):
                return None
            source_parent = os.path.dirname(src)
            if not source_parent or not os.path.isdir(source_parent):
                return None
            if self._folder_contains_media_for_cache_icon(source_parent):
                return None
            self.database.update_cache_status(source_parent, False)
            return source_parent
        except Exception as e:
            logging.debug("[DnD] reset source folder cache failed %s: %s", src, e)
            return None

    def _sync_directory_parent_cache_status(self, src: str, dst: str, is_move: bool) -> list[str]:
        """Keep parent folder icons correct after moving/copying a whole folder."""
        changed: list[str] = []
        try:
            if dst and os.path.isdir(dst) and self._folder_contains_media_for_cache_icon(dst):
                self.database.update_cache_status(dst, True)
                self._bubble_cached_status_to_ancestors(dst)
                changed.append(dst)

            if is_move:
                source_parent = os.path.dirname(src)
                if source_parent and os.path.isdir(source_parent):
                    has_media = self._folder_contains_media_for_cache_icon(source_parent)
                    self.database.update_cache_status(source_parent, has_media)
                    changed.append(source_parent)
        except Exception as e:
            logging.debug("[DnD] sync directory parent cache failed %s -> %s: %s", src, dst, e)
        return changed

    def _sync_cache_after_copy_move(self, src: str, dst: str, is_dir: bool, is_move: bool):
        """
        Best-effort disk cache sync after copy/move (folder subtree or file prefix variants).
        Logs only on failure; never blocks the main FS operation.
        """
        try:
            src_cache = self._cache_path_for_fs_path(src)
            dst_cache = self._cache_path_for_fs_path(dst)

            if is_dir:
                if not os.path.exists(src_cache):
                    return
                os.makedirs(os.path.dirname(dst_cache), exist_ok=True)
                if os.path.exists(dst_cache):
                    # merge on cache collision
                    for root, _, files in os.walk(src_cache):
                        rel_root = os.path.relpath(root, src_cache)
                        target_root = os.path.join(dst_cache, rel_root)
                        os.makedirs(target_root, exist_ok=True)
                        for fn in files:
                            src_f = os.path.join(root, fn)
                            dst_f = os.path.join(target_root, fn)
                            if is_move:
                                if os.path.exists(dst_f):
                                    os.remove(dst_f)
                                shutil.move(src_f, dst_f)
                            else:
                                shutil.copy2(src_f, dst_f)
                    if is_move and os.path.isdir(src_cache):
                        shutil.rmtree(src_cache, ignore_errors=True)
                else:
                    if is_move:
                        shutil.move(src_cache, dst_cache)
                    else:
                        shutil.copytree(src_cache, dst_cache)
                # keep DB cache flags in sync for folder icons
                try:
                    self.database.update_cache_status(dst, True)
                    self._bubble_cached_status_to_ancestors(dst)
                    if is_move:
                        self.database.update_cache_status(src, False)
                        old_parent = os.path.dirname(src)
                        if old_parent and os.path.normcase(
                            os.path.normpath(old_parent)
                        ) != os.path.normcase(os.path.normpath(src)):
                            still = self.database.folder_has_cached_descendant(
                                old_parent
                            )
                            self.database.update_cache_status(old_parent, still)
                except Exception as e:
                    logging.warning(f"[DnD][Cache] status update failed for dir {src}->{dst}: {e}")
                return

            # file cache entries like "file.ext_320x240.jpg"
            src_cache_dir = os.path.dirname(src_cache)
            dst_cache_dir = os.path.dirname(dst_cache)
            src_base = os.path.basename(src_cache)
            dst_base = os.path.basename(dst_cache)
            if not os.path.isdir(src_cache_dir):
                return
            os.makedirs(dst_cache_dir, exist_ok=True)

            for fn in os.listdir(src_cache_dir):
                if not fn.startswith(src_base):
                    continue
                src_f = os.path.join(src_cache_dir, fn)
                suffix = fn[len(src_base):]
                dst_f = os.path.join(dst_cache_dir, dst_base + suffix)
                try:
                    if is_move:
                        if os.path.exists(dst_f):
                            os.remove(dst_f)
                        shutil.move(src_f, dst_f)
                    else:
                        shutil.copy2(src_f, dst_f)
                except Exception as e:
                    logging.warning(f"[DnD][Cache] file cache sync failed: {src_f} -> {dst_f}: {e}")
            # set cache status on destination when variants exist
            try:
                self.database.update_cache_status(dst, True)
                if is_move:
                    self.database.update_cache_status(src, False)
            except Exception as e:
                logging.warning(f"[DnD][Cache] status update failed for file {src}->{dst}: {e}")
        except Exception as e:
            logging.warning(f"[DnD][Cache] sync failed for {src} -> {dst}: {e}")

    def _sync_db_after_copy_move(self, src: str, dst: str, is_dir: bool, is_move: bool):
        """
        Best-effort DB sync after FS copy/move (file path update or subtree prefix remap).
        """
        try:
            db = self.database
            src_norm = db.normalize_path(src)
            dst_norm = db.normalize_path(dst)

            if is_dir:
                old_like = src_norm + os.sep + "%"
                rows = list(db.db.query(
                    "SELECT * FROM files WHERE file_path = :src OR file_path LIKE :src_like",
                    src=src_norm, src_like=old_like
                ))
                pairs = []  # (old_fp, new_fp, row)
                for row in rows:
                    old_fp = row.get("file_path")
                    if not old_fp:
                        continue
                    if old_fp == src_norm:
                        new_fp = dst_norm
                    else:
                        suffix = old_fp[len(src_norm):]
                        new_fp = dst_norm + suffix
                    pairs.append((old_fp, new_fp, row))
            else:
                row = db.get_entry(src)
                pairs = []
                if row:
                    pairs.append((src_norm, dst_norm, row))

            for _old_fp, new_fp, row in pairs:
                new_row = dict(row)
                new_row.pop("id", None)
                new_row["file_path"] = new_fp
                new_row["filename"] = os.path.basename(new_fp).strip().lower()
                db.table.upsert(new_row, ["file_path"])

            if is_move:
                for old_fp, _new_fp, _row in pairs:
                    try:
                        db.table.delete(file_path=old_fp)
                    except Exception:
                        pass

            db.clear_entry_cache()
        except Exception as e:
            logging.warning(f"[DnD][DB] sync failed for {src} -> {dst}: {e}")

    def _dnd_sync_db_cache(self, src: str, dst: str, is_dir: bool, is_move: bool):
        """Post-op DB + cache sync (best effort, no exceptions propagated)."""
        self._sync_db_after_copy_move(src, dst, is_dir, is_move)
        self._sync_cache_after_copy_move(src, dst, is_dir, is_move)
        if not is_dir:
            self._mark_media_destination_folder_cached(src, dst, is_move)
            source_folder = self._reset_media_source_folder_if_empty(src, is_move)
            return [source_folder] if source_folder else []
        return self._sync_directory_parent_cache_status(src, dst, is_move)

    def _dnd_release_preview_handles(self):
        """Cancel pending preview/VLC before moving files (same idea as delete prep)."""
        for attr in ("_preview_timer", "_click_timer"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        ip = getattr(self, "info_panel", None)
        if ip is not None and hasattr(ip, "cancel_pending_preview"):
            try:
                ip.cancel_pending_preview()
            except Exception:
                pass
        if hasattr(self, "stop_preview"):
            try:
                self.stop_preview()
            except Exception:
                pass
        if ip is not None and hasattr(ip, "_stop_preview_image_animation"):
            try:
                ip._stop_preview_image_animation()
            except Exception:
                pass
        if ip is not None and hasattr(ip, "preview_image_tk"):
            ip.preview_image_tk = None
        try:
            import gc
            gc.collect()
        except Exception:
            pass

    def _dnd_execute_copy_move_thread(
        self,
        sources: list[str],
        dest: str,
        is_move: bool,
        on_success=None,
        progress=None,
        with_captions: bool = True,
    ):
        """Run copy/move in a worker; report results on main thread via after()."""
        from caption_editor_widget import (
            filter_covered_caption_txt_sources,
            transfer_caption_sidecar,
        )

        action = "move" if is_move else "copy"
        action_past = "moved" if is_move else "copied"
        dest_name = os.path.basename(dest) or dest
        ok, fail = [], []
        changed_cache_folders = []
        replace_all = False
        rename_all = False
        skip_all = False
        cancelled = False
        captions_done = 0

        sources = filter_covered_caption_txt_sources(
            list(sources or []), with_captions=bool(with_captions)
        )
        total = len(sources)

        if hasattr(self, "suspend_directory_watcher"):
            self.suspend_directory_watcher()

        # Pre-flight validation: reject the WHOLE batch before touching the filesystem if the
        # destination lives inside (or equals) any source folder. Doing this check per-item
        # inside the loop below would first move the "innocent" siblings (e.g. images selected
        # alongside the folder) and only then error out on the folder — leaving a half-done op.
        dest_norm = os.path.normcase(os.path.normpath(dest))
        for src in sources:
            if not os.path.isdir(src):
                continue
            src_norm = os.path.normcase(os.path.normpath(src))
            if dest_norm == src_norm or dest_norm.startswith(src_norm + os.sep):
                logging.warning(
                    f"[DnD] BLOCKED (pre-flight) - destination is inside source: {src} -> {dest}"
                )
                if hasattr(self, "resume_directory_watcher"):
                    self.after(0, lambda: self.resume_directory_watcher(restart=True))
                self.after(0, self._dnd_close_progress_dialog)
                self.after(0, lambda s=src, d=dest: self.universal_dialog(
                    title="DnD warning",
                    message=(
                        "Destination folder is inside the source folder.\n\n"
                        "Nothing was moved or copied.\n\n"
                        f"Source: {s}\nDestination: {d}"
                    ),
                    cancel_text="OK"
                ))
                return

        for idx, src in enumerate(sources, start=1):
            if progress is not None and getattr(progress, "cancelled", False):
                cancelled = True
                logging.info("[DnD] %s canceled by user after %s item(s)", action, len(ok))
                break

            self._dnd_report_progress(progress, idx, total, src)

            dst = os.path.join(dest, os.path.basename(src))
            src_norm = os.path.normcase(os.path.normpath(src))
            dest_norm = os.path.normcase(os.path.normpath(dest))
            dst_norm = os.path.normcase(os.path.normpath(dst))

            if src_norm == dst_norm:
                logging.info(f"[DnD] skipping (src == dst): {src}")
                continue

            if os.path.isdir(src):
                if dest_norm == src_norm or dest_norm.startswith(src_norm + os.sep):
                    logging.warning(f"[DnD] BLOCKED - destination is inside source: {src} -> {dest}")
                    self.after(0, lambda s=src, d=dest: self.universal_dialog(
                        title="DnD warning",
                        message=f"Destination folder is inside the source folder.\n\nSource: {s}\nDestination: {d}",
                        cancel_text="OK"
                    ))
                    continue

            try:
                if os.path.exists(dst):
                    if skip_all:
                        logging.info("[DnD] conflict skip-all: %s", dst)
                        continue

                    conflict_action = "replace" if replace_all else "rename" if rename_all else None
                    if conflict_action is None:
                        choice, apply_all = self._dnd_prompt_conflict_choice(dst)
                        if choice == "cancel":
                            logging.info("[DnD] conflict canceled by user: %s", dst)
                            cancelled = True
                            break
                        if choice == "skip":
                            if apply_all:
                                skip_all = True
                            continue
                        conflict_action = choice
                        if apply_all and choice == "replace":
                            replace_all = True
                        elif apply_all and choice == "rename":
                            rename_all = True

                    if conflict_action == "rename":
                        dst = get_conflict_rename_path(dst)
                    elif conflict_action == "replace" and not self._dnd_delete_existing_target(dst):
                        fail.append(src)
                        logging.error("[DnD] replace failed (cannot remove target): %s", dst)
                        continue

                is_dir = os.path.isdir(src)
                logging.info("[DnD] %s start: %s -> %s", action, src, dst)
                last_err = None
                for attempt in range(8):
                    try:
                        if is_dir:
                            shutil.move(src, dst) if is_move else shutil.copytree(src, dst)
                        else:
                            shutil.move(src, dst) if is_move else shutil.copy2(src, dst)
                        last_err = None
                        break
                    except PermissionError as e:
                        last_err = e
                        logging.warning(
                            "[DnD] %s locked (attempt %s/8): %s",
                            action,
                            attempt + 1,
                            src,
                        )
                        time.sleep(0.05 * (attempt + 1))
                if last_err is not None:
                    raise last_err

                if with_captions and (not is_dir) and (
                    src.lower().endswith(IMAGE_FORMATS)
                    or src.lower().endswith(VIDEO_FORMATS)
                ):
                    if transfer_caption_sidecar(src, dst, is_move=is_move):
                        captions_done += 1

                changed_folders = self._dnd_sync_db_cache(
                    src, dst, is_dir=is_dir, is_move=is_move
                )
                changed_cache_folders.extend(changed_folders)
                ok.append(src)
                logging.info(f"[DnD] {action_past}: {src} -> {dst}")
            except Exception as e:
                fail.append(src)
                logging.error(f"[DnD] error during {action}: {src} -> {dst}: {e}")

        def _finish():
            self._dnd_close_progress_dialog()
            try:
                self._dnd_release_preview_handles()
            except Exception:
                pass
            if hasattr(self, "resume_directory_watcher"):
                self.resume_directory_watcher(restart=True)
            if ok:
                msg = f"DnD: {action_past} {len(ok)} item(s) -> {dest_name}"
                if captions_done:
                    msg += f" (+{captions_done} caption)"
                if cancelled:
                    msg += " (canceled)"
                self.status_bar.set_action_message(msg)
                for folder_path in dict.fromkeys(changed_cache_folders):
                    self.refresh_folder_icon(folder_path)
                # Moving media out can empty a source folder (or strip media from an
                # ancestor preview); drop stale folder-preview overlays so the icons
                # rebuild instead of keeping a thumbnail for files that are gone.
                if is_move and hasattr(self, "_invalidate_folder_preview_caches"):
                    for moved_src in ok:
                        try:
                            self._invalidate_folder_preview_caches(moved_src)
                        except Exception:
                            logging.debug(
                                "[DnD] folder preview invalidation failed for %s",
                                moved_src,
                                exc_info=True,
                            )
                if on_success:
                    on_success()
            elif cancelled:
                self.status_bar.set_action_message(f"DnD: {action} canceled")
            if fail:
                names = ", ".join(os.path.basename(f) for f in fail[:3])
                if len(fail) > 3:
                    names += f" ... (+{len(fail)-3})"
                self.universal_dialog(
                    title="DnD error",
                    message=f"Failed to {action}:\n{names}",
                    cancel_text="OK"
                )

        self.after(0, _finish)

    # Ask main UI thread for conflict action from worker threads.
    def _dnd_prompt_conflict_choice(self, dst_path: str) -> tuple[str, bool]:
        done = threading.Event()
        result: dict[str, tuple[str, bool]] = {"value": ("cancel", False)}

        def _ask():
            progress = getattr(self, "_dnd_progress_dialog", None)
            parent = self
            try:
                if progress is not None and progress.winfo_exists():
                    parent = progress
                    try:
                        progress.grab_release()
                    except Exception:
                        pass
            except Exception:
                progress = None
            try:
                result["value"] = open_conflict_dialog(
                    parent, os.path.basename(dst_path) or dst_path
                )
            except Exception:
                result["value"] = ("cancel", False)
            finally:
                try:
                    if progress is not None and progress.winfo_exists():
                        progress.grab_set()
                        progress.lift()
                except Exception:
                    pass
                done.set()

        self.after(0, _ask)
        done.wait()
        return result["value"]

    # Remove destination item before overwrite.
    def _dnd_delete_existing_target(self, dst_path: str) -> bool:
        try:
            if os.path.isdir(dst_path):
                shutil.rmtree(dst_path)
            else:
                os.remove(dst_path)
            return True
        except Exception as e:
            logging.error("[DnD] failed to remove existing target %s: %s", dst_path, e)
            return False

    def _dnd_show_dialog_and_run(
        self,
        sources: list[str],
        dest: str,
        is_move: bool,
        on_success=None,
        with_captions: bool = True,
    ):
        """Show confirmation dialog then run operation in a thread."""
        from caption_editor_widget import count_caption_sidecars

        action      = "move" if is_move else "copy"
        dest_name   = os.path.basename(dest) or dest

        n = len(sources)
        if n == 1:
            detail = f"  {os.path.basename(sources[0])}"
        elif n <= 5:
            detail = "\n".join(f"  {os.path.basename(s)}" for s in sources)
        else:
            detail = "\n".join(f"  {os.path.basename(s)}" for s in sources[:4])
            detail += f"\n  ... and {n - 4} more item(s)"

        n_caps = count_caption_sidecars(sources)
        msg = f"Do you want to {action} {n} item(s)?\n\n{detail}\n\nDestination: {dest}"
        if n_caps:
            msg += f"\n\n{n_caps} image(s) have caption (.txt) sidecars."
        title = f"{'Move' if is_move else 'Copy'} - confirmation"

        caption_var = None
        checkbox_text = None
        if n_caps > 0:
            caption_var = tk.BooleanVar(value=bool(with_captions))
            verb = "Move" if is_move else "Copy"
            checkbox_text = f"{verb} with caption (.txt)"

        def _confirm_run():
            try:
                self._dnd_release_preview_handles()
            except Exception:
                pass
            chosen = bool(caption_var.get()) if caption_var is not None else bool(with_captions)
            # Remember last choice for silent DnD / paste
            self.copy_move_with_captions = chosen
            self._dnd_start_copy_move(
                sources, dest, is_move, on_success, with_captions=chosen
            )

        def _cancel_run():
            logging.info("[DnD] Operation canceled by user.")

        self.universal_dialog(
            title=title,
            message=msg,
            confirm_callback=_confirm_run,
            cancel_callback=_cancel_run,
            confirm_text="Yes",
            cancel_text="No",
            show_cancel=True,
            checkbox_text=checkbox_text,
            checkbox_variable=caption_var,
        )

    def _dnd_on_position_tree(self, event):
        """
        Tree drag position: move/copy preview (internal vs Explorer), modifier via GetKeyState, edge autoscroll.
        """
        try:
            ctrl_down = bool(ctypes.windll.user32.GetKeyState(0x11) & 0x8000)
            shift_down = bool(ctypes.windll.user32.GetKeyState(0x10) & 0x8000)
        except Exception:
            ctrl_down = bool(event.state & 0x0004) if hasattr(event, 'state') else False
            shift_down = bool(event.state & 0x0001) if hasattr(event, 'state') else False

        internal = self._dnd_is_internal_drag_active()
        is_move_preview = (not ctrl_down) if internal else shift_down
        preview_changed = is_move_preview != self._dnd_last_move_preview
        self._dnd_last_move_preview = is_move_preview

        # Row under cursor — event.y in widget coords is often reliable during DnD
        tree_y = -1.0
        item = ""
        try:
            h = int(self.tree.winfo_height())
            if hasattr(event, "y") and event.y is not None:
                yw = int(event.y)
                if -30 <= yw <= h + 30:
                    tree_y = float(yw)
            if tree_y < 0:
                tree_y = float(event.y_root - self.tree.winfo_rooty())
            item = self.tree.identify_row(int(tree_y))
        except Exception:
            item = ""

        self._dnd_maybe_autoscroll_tree(tree_y)

        item_changed = item != self._dnd_tree_hover_item

        if item_changed and self._dnd_tree_hover_item:
            try:
                self.tree.item(self._dnd_tree_hover_item, tags=())
            except Exception:
                pass

        self._dnd_tree_hover_item = item or None
        if item_changed:
            self._dnd_tree_hover_since = time.monotonic()

        if item:
            if is_move_preview:
                bg, fg = "#7a3a00", "#ffcc88"
            else:
                bg, fg = "#2a5080", "#ffffff"

            if item_changed or preview_changed:
                try:
                    self.tree.item(item, tags=("dnd_hover",))
                    self.tree.tag_configure("dnd_hover", background=bg, foreground=fg)
                except Exception:
                    pass

        if preview_changed or item_changed:
            try:
                # Tree cursor: "+" only for copy; empty on move so the OLE drag cursor from
                # <<DropPosition>> (MOVE vs COPY) is visible. (Tk has no standard "minus" cursor.)
                self.tree.configure(
                    cursor="plus" if not is_move_preview else ""
                )
            except Exception:
                pass

        return dnd.MOVE if is_move_preview else dnd.COPY

    def _dnd_on_enter_tree(self, event):
        self._dnd_last_move_preview = None

    def _dnd_on_leave_tree(self, event):
        self._dnd_reset_tree_highlight()

    def _cancel_tree_dnd_autoscroll(self):
        """Reset autoscroll throttle (e.g. after drop or leave)."""
        self._dnd_tree_autoscroll_last_ts = 0.0

    def _dnd_tree_autoscroll_step(self, direction: int):
        """Scroll tree by a few rows; call synchronously from <<DropPosition>>."""
        now = time.monotonic()
        min_dt = float(getattr(self, "_dnd_tree_autoscroll_min_step_s", 0.045) or 0.045)
        last = float(getattr(self, "_dnd_tree_autoscroll_last_ts", 0.0) or 0.0)
        if now - last < min_dt:
            return
        self._dnd_tree_autoscroll_last_ts = now

        n = max(1, int(getattr(self, "_dnd_tree_autoscroll_lines", 2)))
        steps = int(direction) * n
        try:
            self.tree.yview_scroll(steps, "units")
        except tk.TclError:
            try:
                top, _ = self.tree.yview()
                top = float(top)
                delta = 0.09
                if direction < 0:
                    self.tree.yview_moveto(max(0.0, top - delta))
                else:
                    self.tree.yview_moveto(min(1.0, top + delta))
            except Exception:
                pass
        except Exception:
            pass
        try:
            self.refresh_tree_coordinates()
        except Exception:
            pass

    def _dnd_maybe_autoscroll_tree(self, tree_y: float):
        """Autoscroll when cursor is near top/bottom edge (synchronous; OLE drag blocks after())."""
        try:
            tree_h = int(self.tree.winfo_height())
        except Exception:
            tree_h = 0
        margin = int(getattr(self, "_dnd_tree_autoscroll_margin_px", 40))
        if tree_h <= margin * 2 + 10:
            return

        if tree_y < margin:
            self._dnd_tree_autoscroll_step(-1)
        elif tree_y > tree_h - margin:
            self._dnd_tree_autoscroll_step(1)
        else:
            self._dnd_tree_autoscroll_last_ts = 0.0

    def _dnd_reset_tree_highlight(self):
        """Clear hover highlight and reset tree cursor."""
        self._cancel_tree_dnd_autoscroll()
        if self._dnd_tree_hover_item:
            try:
                self.tree.item(self._dnd_tree_hover_item, tags=())
            except Exception:
                pass
        self._dnd_tree_hover_item = None
        self._dnd_tree_hover_since = 0.0
        try:
            self.tree.configure(cursor="")
        except Exception:
            pass

    @staticmethod
    def _dnd_is_virtual_library_path(path) -> bool:
        return isinstance(path, str) and path.startswith("virtual_library://")

    @staticmethod
    def _dnd_virtual_library_name(path: str) -> str | None:
        if not isinstance(path, str) or not path.startswith("virtual_library://"):
            return None
        name = path.split("://", 1)[1].strip()
        return name or None

    def _dnd_is_valid_drop_dest(self, path) -> bool:
        """Real directory or virtual library URI."""
        if not path or not isinstance(path, str):
            return False
        if self._dnd_is_virtual_library_path(path):
            return True
        return os.path.isdir(path)

    def _dnd_tree_path_from_item(self, item: str | None) -> str | None:
        """Absolute path (or virtual_library:// URI) for tree item, or None."""
        if not item:
            return None
        try:
            path = self.tree.set(item, "path")
            if path and self._dnd_is_valid_drop_dest(path):
                return path
        except Exception:
            pass
        return None

    # ── DnD debounce helpers ───────────────────────────────────────────────
    def _dnd_mark_thumb_press(self, event, file_path: str):
        self._dnd_drag_happened = False
        self._thumb_double_click_consumed = False
        self._dnd_press_ts = time.monotonic()
        self._dnd_press_kind = "thumb"
        self._dnd_press_path = getattr(event.widget, "file_path", None) or file_path
        try:
            self._dnd_press_x_root = int(event.x_root)
            self._dnd_press_y_root = int(event.y_root)
        except Exception:
            self._dnd_press_x_root = None
            self._dnd_press_y_root = None

    def _dnd_mark_tree_press(self, event):
        self._dnd_press_ts = time.monotonic()
        self._dnd_press_kind = "tree"
        try:
            self._dnd_press_x_root = int(event.x_root)
            self._dnd_press_y_root = int(event.y_root)
        except Exception:
            self._dnd_press_x_root = None
            self._dnd_press_y_root = None
        item = self.tree.identify_row(event.y) if hasattr(self, "tree") else None
        self._dnd_press_path = self.tree.set(item, "path") if item else None
        try:
            self._dnd_tree_press_selection = tuple(self.tree.selection() or ())
            self._dnd_tree_press_focus = self.tree.focus()
        except Exception:
            self._dnd_tree_press_selection = ()
            self._dnd_tree_press_focus = None
        try:
            self._dnd_tree_press_paths = self.paths_for_tree_context(self._dnd_press_path)
        except Exception:
            self._dnd_tree_press_paths = [self._dnd_press_path] if self._dnd_press_path else []

    def _dnd_hold_elapsed_ok(
        self,
        expected_kind: str,
        expected_path: str | None = None,
        min_hold_ms: float | None = None
    ) -> bool:
        if self._dnd_press_kind != expected_kind:
            return False
        elapsed_ms = (time.monotonic() - self._dnd_press_ts) * 1000.0
        hold_ms = self._dnd_hold_ms if min_hold_ms is None else float(min_hold_ms)
        if elapsed_ms < hold_ms:
            # tkdnd may call DragInit very early (single shot). If LMB is still down,
            # wait the remaining hold time so intentional long-press drag can start.
            remain_ms = hold_ms - elapsed_ms
            if remain_ms > 0 and self._dnd_is_left_button_down():
                deadline = time.monotonic() + (remain_ms / 1000.0)
                while time.monotonic() < deadline:
                    if not self._dnd_is_left_button_down():
                        return False
                    time.sleep(0.005)
                elapsed_ms = (time.monotonic() - self._dnd_press_ts) * 1000.0
            if elapsed_ms < hold_ms:
                return False
        if expected_path and self._dnd_press_path and os.path.normcase(os.path.normpath(expected_path)) != os.path.normcase(os.path.normpath(self._dnd_press_path)):
            return False
        return True

    def _dnd_is_left_button_down(self) -> bool:
        """Best-effort check whether left mouse button is currently held."""
        try:
            return bool(ctypes.windll.user32.GetKeyState(0x01) & 0x8000)  # VK_LBUTTON
        except Exception:
            return False

    def _dnd_cursor_pos(self) -> tuple[int, int] | None:
        """Current global cursor position (x, y) in screen pixels, or None."""
        try:
            class _POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            pt = _POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return (int(pt.x), int(pt.y))
        except Exception:
            pass
        return None

    def _dnd_movement_exceeds_threshold(
        self, min_dist_px: float, max_wait_ms: float
    ) -> bool:
        """
        Distinguish an intentional drag from an accidental micro-move during a click.

        tkinterdnd2's <<DragInitCmd>> is a one-shot fired by Windows OLE as soon as the
        cursor crosses the tiny system drag threshold (~4px) with LMB held — so a small
        hand shake on a folder/expander click already triggers it. We busy-poll here and
        only accept the drag once the cursor has actually travelled min_dist_px from the
        press point. Returns False if the button is released first (= click) or if the
        cursor never moves far enough within max_wait_ms (= long press without intent).
        """
        px = getattr(self, "_dnd_press_x_root", None)
        py = getattr(self, "_dnd_press_y_root", None)
        if px is None or py is None:
            # No baseline recorded — don't block, fall back to legacy behavior.
            return True
        threshold_sq = float(min_dist_px) * float(min_dist_px)
        deadline = time.monotonic() + (float(max_wait_ms) / 1000.0)
        while True:
            if not self._dnd_is_left_button_down():
                return False
            pos = self._dnd_cursor_pos()
            if pos is not None:
                dx = pos[0] - px
                dy = pos[1] - py
                if (dx * dx + dy * dy) >= threshold_sq:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def _dnd_modifiers_down(self) -> bool:
        """Return True when Shift/Ctrl is currently pressed."""
        try:
            user32 = ctypes.windll.user32
            shift = bool(user32.GetKeyState(0x10) & 0x8000)  # VK_SHIFT
            ctrl = bool(user32.GetKeyState(0x11) & 0x8000)   # VK_CONTROL
            return shift or ctrl
        except Exception:
            return False

    def _dnd_thumb_drag_init(self, event, clicked_path: str = None):
        """
        tkinterdnd2 drag init from thumbnail canvas: prefer widget file_path, multi-drag from selection.
        """
        # Real double-click already opened the file — do not start OLE drag on the same press.
        if getattr(self, "_thumb_double_click_consumed", False):
            logging.info("[DnD] DRAG OUT blocked (thumb): double-click already consumed")
            return

        canvas_path = getattr(event.widget, 'file_path', None) or clicked_path

        if not canvas_path:
            logging.warning("[DnD] DRAG OUT: could not resolve path from widget or closure")
            return

        selected_paths = [
            p for p, _, _ in self.selected_thumbnails
            if isinstance(p, str) and p
        ]
        norm_canvas = os.path.normcase(os.path.normpath(canvas_path))
        norm_selected = {
            os.path.normcase(os.path.normpath(p)): p for p in selected_paths
        }

        # DragInit may be raised from a different child than the canvas that received
        # ButtonPress-1 (focus / layout / modifier timing). The press path is the
        # authoritative start of the gesture — align so one-shot DragInit is not
        # rejected before the user can press Ctrl/Shift for copy vs move.
        if self._dnd_press_kind == "thumb" and self._dnd_press_path:
            npp = os.path.normcase(os.path.normpath(self._dnd_press_path))
            if npp != norm_canvas:
                if len(selected_paths) > 1:
                    if npp in norm_selected:
                        canvas_path = self._dnd_press_path
                        norm_canvas = npp
                else:
                    canvas_path = self._dnd_press_path
                    norm_canvas = npp

        is_multi_drag = norm_canvas in norm_selected and len(selected_paths) > 1

        # Multi-drag: shorter hold; path match is relaxed (press widget may differ from DragInit source)
        required_hold_ms = self._dnd_hold_ms_multi if is_multi_drag else self._dnd_hold_ms
        # If user presses Shift/Ctrl during the drag gesture, Tk can re-route DragInit
        # from a sibling selected widget; relax path guard so drag still starts.
        guard_expected_path = None if (is_multi_drag or self._dnd_modifiers_down()) else canvas_path
        if not self._dnd_hold_elapsed_ok("thumb", guard_expected_path, min_hold_ms=required_hold_ms):
            elapsed_ms = (time.monotonic() - self._dnd_press_ts) * 1000.0
            logging.info(
                "[DnD] DRAG OUT blocked (thumb): elapsed=%.1fms required=%.1fms kind=%r press_path=%r canvas_path=%r multi=%s",
                elapsed_ms,
                float(required_hold_ms),
                self._dnd_press_kind,
                self._dnd_press_path,
                canvas_path,
                is_multi_drag
            )
            return

        if not self._dnd_movement_exceeds_threshold(
            getattr(self, "_dnd_drag_min_distance_px_thumb", 16),
            getattr(self, "_dnd_drag_distance_timeout_ms", 800),
        ):
            logging.info(
                "[DnD] DRAG OUT blocked (thumb): movement below threshold (%spx) — treated as click, press_path=%r",
                getattr(self, "_dnd_drag_min_distance_px_thumb", 16),
                self._dnd_press_path,
            )
            return

        if is_multi_drag:
            seen = set()
            paths = []
            for p in selected_paths:
                np = os.path.normcase(os.path.normpath(p))
                if np in seen:
                    continue
                seen.add(np)
                paths.append(p)
        else:
            paths = [canvas_path]

        paths = [p for p in paths if p]
        if not paths:
            return

        data = self._dnd_format_paths(paths)
        logging.info("[DnD] DRAG OUT thumbnail: %d file(s), first=%s", len(paths), paths[0])
        self._dnd_internal_drag = True
        self._dnd_internal_drop_consumed = False
        self._dnd_mark_internal_drag_payload(paths)
        self._dnd_mark_drag_out_snapshot(paths)
        self._dnd_internal_drag_end_ts = 0.0
        self._dnd_drag_happened = True
        return ((dnd.MOVE, dnd.COPY), dnd.DND_FILES, data)

    def _dnd_select_file_after_load(self, file_path: str):
        """
        After file drop: find item in grid, scroll to it, select.
        """
        try:
            norm = os.path.normcase(os.path.normpath(file_path))
            idx = None
            for i, item in enumerate(self.video_files):
                if os.path.normcase(os.path.normpath(item.get('path', ''))) == norm:
                    idx = i
                    break
            if idx is None:
                logging.info("[DnD] file not in grid: %s", file_path)
                return
            self.select_thumbnail(idx, shift=False, ctrl=False, trigger_preview=False)
            label_info = self.thumbnail_labels.get(file_path)
            if label_info:
                widget = label_info.get("canvas") if isinstance(label_info, dict) else label_info
                if widget and widget.winfo_exists():
                    y = widget.winfo_y()
                    canvas_h = self.canvas.winfo_height()
                    scroll_region = self.canvas.cget("scrollregion")
                    if scroll_region:
                        total_h = float(str(scroll_region).split()[3])
                        if total_h > 0:
                            frac = max(0.0, (y - canvas_h / 2) / total_h)
                            self.canvas.yview_moveto(frac)
            logging.info("[DnD] file selected in grid: %s", os.path.basename(file_path))
        except Exception as e:
            logging.warning("[DnD] _dnd_select_file_after_load failed: %s", e)

    def _dnd_tree_drag_init(self, event):
        """
        tkinterdnd2 drag init from tree: return selected folder path.
        """
        # Require a fresh tree press for this gesture. Expander clicks return "break"
        # before _dnd_mark_tree_press; without this guard a stale press_ts makes hold
        # checks pass immediately and starts an accidental drag.
        if self._dnd_press_kind != "tree":
            logging.info(
                "[DnD] DRAG OUT blocked (tree): press_kind=%r (need active tree press)",
                self._dnd_press_kind,
            )
            return

        press_path = self._dnd_press_path if self._dnd_press_kind == "tree" else None
        sel = self.tree.selection()
        path = ""
        if sel:
            item = sel[0]
            try:
                path = self.tree.set(item, "path")
            except Exception:
                path = ""

        if not path and press_path:
            path = press_path

        if not self._dnd_hold_elapsed_ok("tree", min_hold_ms=self._dnd_hold_ms_tree):
            elapsed_ms = (time.monotonic() - self._dnd_press_ts) * 1000.0
            logging.info(
                "[DnD] DRAG OUT blocked (tree): elapsed=%.1fms required=%.1fms press_path=%r selected_path=%r",
                elapsed_ms,
                float(self._dnd_hold_ms_tree),
                press_path,
                path
            )
            return
        if not self._dnd_movement_exceeds_threshold(
            getattr(self, "_dnd_drag_min_distance_px_tree", 12),
            getattr(self, "_dnd_drag_distance_timeout_ms", 800),
        ):
            logging.info(
                "[DnD] DRAG OUT blocked (tree): movement below threshold (%spx) — treated as click, press_path=%r",
                getattr(self, "_dnd_drag_min_distance_px_tree", 12),
                press_path,
            )
            return
        if not path or not os.path.exists(path):
            logging.info(f"[DnD] DRAG OUT tree skipped: invalid path={path!r}")
            return

        press_paths = (
            list(getattr(self, "_dnd_tree_press_paths", []) or [])
            if self._dnd_press_kind == "tree"
            else []
        )
        if press_paths:
            paths = press_paths
        else:
            try:
                paths = self.paths_for_tree_context(press_path or path)
            except Exception:
                paths = [path]
        paths = [p for p in paths if p and os.path.exists(p)]
        if not paths:
            return
        if len(paths) > 1:
            original_selection = [
                item for item in getattr(self, "_dnd_tree_press_selection", ())
                if self.tree.exists(item)
            ]
            if original_selection:
                self._suppress_tree_select_navigation = True
                try:
                    self.tree.selection_set(*original_selection)
                    original_focus = getattr(self, "_dnd_tree_press_focus", None)
                    if original_focus and self.tree.exists(original_focus):
                        self.tree.focus(original_focus)
                finally:
                    self.after_idle(
                        lambda: setattr(self, "_suppress_tree_select_navigation", False)
                    )

        data = self._dnd_format_paths(paths)
        logging.info("[DnD] DRAG OUT tree: %d folder(s), first=%s", len(paths), paths[0])
        self._dnd_internal_drag = True
        self._dnd_internal_drop_consumed = False
        self._dnd_mark_internal_drag_payload(paths)
        self._dnd_mark_drag_out_snapshot(paths)
        self._dnd_internal_drag_end_ts = 0.0
        return ((dnd.MOVE, dnd.COPY), dnd.DND_FILES, data)

    def _dnd_drag_end(self, event):
        self._cancel_tree_dnd_autoscroll()
        end_ts = time.monotonic()
        self._dnd_internal_drag_end_ts = end_ts

        snapshot = getattr(self, "_dnd_drag_out_snapshot", None)
        if snapshot:
            self.after(250, lambda s=snapshot: self._dnd_reconcile_external_drag_out(s))

        # On Windows, DragEnd may fire just before Drop handlers; keep internal marker
        # briefly so Drop can still resolve correct move/copy semantics.
        def _clear_internal_drag():
            try:
                if float(getattr(self, "_dnd_internal_drag_end_ts", 0.0) or 0.0) != end_ts:
                    return
            except Exception:
                return
            self._dnd_internal_drag = False

        self.after(900, _clear_internal_drag)
        logging.info(f"[DnD] Drag ended.")

    @staticmethod
    def _dnd_parse_paths(raw: str) -> list[str]:
        """
        Parse tkinterdnd2 path strings from Windows ({paths with spaces}, multiple files, file:/// URLs).
        """
        def _normalize(p: str) -> str:
            p = p.strip()
            if p.startswith("file:///"):
                p = p[8:]
            p = p.replace("/", "\\")
            if len(p) > 3 and p.endswith("\\"):
                p = p.rstrip("\\")
            return p

        paths = []
        raw = raw.strip()
        i = 0
        while i < len(raw):
            if raw[i] == "{":
                end = raw.find("}", i)
                if end != -1:
                    paths.append(_normalize(raw[i + 1:end]))
                    i = end + 1
                else:
                    paths.append(_normalize(raw[i:]))
                    break
            elif raw[i] == " ":
                i += 1
            else:
                end = raw.find(" ", i)
                if end == -1:
                    paths.append(_normalize(raw[i:]))
                    break
                paths.append(_normalize(raw[i:end]))
                i = end + 1
        return [p for p in paths if p]

    @staticmethod
    def _dnd_format_paths(paths: list[str]) -> str:
        """
        Format paths for tkinterdnd2 drag-out (brace-wrap; forward slashes avoid escape issues).
        """
        return " ".join("{" + p.replace("\\", "/") + "}" for p in paths)
