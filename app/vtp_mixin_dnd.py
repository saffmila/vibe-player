"""Drag-and-drop (tkinterdnd2) mixin for VideoThumbnailPlayer."""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import threading
import time

import tkinterdnd2 as dnd


class VtpDndMixin:
    # ═══════════════════════════════════════════════════════════════════════
    # DRAG & DROP  (tkinterdnd2)
    # ═══════════════════════════════════════════════════════════════════════

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

    def _dnd_on_drop_canvas(self, event):
        self._dnd_reset_canvas_highlight()
        logging.info(f"[DnD] DROP IN raw data: {event.data!r}")
        paths = self._dnd_parse_paths(event.data)
        logging.info(f"[DnD] DROP IN parsed paths: {paths}")
        if not paths:
            return

        dest = getattr(self, 'current_directory', None)
        if not dest or not os.path.isdir(dest):
            logging.warning("[DnD] DROP canvas: current_directory not set: %r", dest)
            return

        files = [p for p in paths if os.path.isfile(p)]
        dirs  = [p for p in paths if os.path.isdir(p)]
        sources = files + dirs

        if not sources:
            logging.warning("[DnD] DROP canvas: no valid paths: %s", paths)
            return

        # Internal drag: no Shift = move, Shift = copy. Explorer: opposite.
        internal = getattr(self, "_dnd_internal_drag", False)
        try:
            shift_down = bool(ctypes.windll.user32.GetKeyState(0x10) & 0x8000)
        except Exception:
            shift_down = False
        if internal:
            is_move = not shift_down
        else:
            is_move = shift_down
        logging.info(
            f"[DnD] DROP IN canvas: files={len(files)} dirs={len(dirs)} dest={dest} "
            f"internal={internal} is_move={is_move}"
        )

        self._dnd_confirm_and_execute(
            sources=sources,
            dest=dest,
            is_move=is_move,
            on_success=lambda: self.display_thumbnails(dest, force_refresh=True)
        )

    def _dnd_on_enter_canvas(self, event):
        try:
            self.canvas.configure(highlightthickness=2, highlightbackground="#3a7ebf")
        except Exception:
            pass

    def _dnd_on_position_canvas(self, event):
        pass

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
          A) Files/folders → copy/move to folder under cursor (internal vs Explorer shift rules).
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

        try:
            shift_down = bool(ctypes.windll.user32.GetKeyState(0x10) & 0x8000)
        except Exception:
            shift_down = False
        internal = getattr(self, "_dnd_internal_drag", False)
        is_move = (not shift_down) if internal else shift_down

        logging.info(
            f"[DnD] DROP tree: internal={internal} is_move={is_move} hover={hover} data={event.data!r}"
        )

        paths = self._dnd_parse_paths(event.data)
        logging.info(f"[DnD] DROP tree: parsed={paths}")
        if not paths:
            logging.warning("[DnD] DROP tree: no paths after parse")
            return

        files  = [p for p in paths if os.path.isfile(p)]
        dirs   = [p for p in paths if os.path.isdir(p)]
        unknown = [p for p in paths if not os.path.isfile(p) and not os.path.isdir(p)]
        logging.info(f"[DnD] DROP tree: files={files} dirs={dirs} unknown={unknown} hover={hover}")
        sources = files + dirs

        if sources and hover:
            dwell_ms = (time.monotonic() - (self._dnd_tree_hover_since or 0.0)) * 1000.0
            if dwell_ms < self._dnd_target_dwell_ms:
                logging.info(
                    "[DnD] DROP tree ignored (short hover %.0fms < %dms)",
                    dwell_ms, self._dnd_target_dwell_ms
                )
                return
            dest_folder = self._dnd_tree_path_from_item(hover)
            logging.info(f"[DnD] DROP tree: dest_folder={dest_folder}")
            if not dest_folder:
                logging.warning("[DnD] DROP tree: target not a folder (hover=%s)", hover)
                return

            def _after_tree_op():
                if is_move:
                    for src in sources:
                        self.update_tree_view(src, dest_folder)
                        self.refresh_folder_icons_subtree(os.path.dirname(src))
                    parent = os.path.dirname(sources[0])
                    if not parent or not os.path.isdir(parent):
                        parent = dest_folder
                    self.refresh_folder_icons_subtree(dest_folder)
                    self.display_thumbnails(parent, force_refresh=True)
                else:
                    self.refresh_folder_icons_subtree(dest_folder)
                    self.refresh_tree_view(dest_folder)

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
        """
        if not sources or not dest:
            return

        def _deferred():
            if getattr(self, "dnd_confirm_dialogs", False):
                self._dnd_show_dialog_and_run(sources, dest, is_move, on_success)
            else:
                threading.Thread(
                    target=lambda: self._dnd_execute_copy_move_thread(
                        sources, dest, is_move, on_success
                    ),
                    daemon=True,
                ).start()

        self.after(1, _deferred)

    def _cache_path_for_fs_path(self, fs_path: str) -> str:
        """
        Map filesystem path to thumbnail_cache layout (e.g. J:\\a\\b -> <cache_root>\\J\\a\\b).
        """
        abs_path = os.path.abspath(fs_path)
        rel = abs_path.replace(":", "")
        return os.path.join(self.thumbnail_cache_path, rel)

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
                    if is_move:
                        self.database.update_cache_status(src, False)
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

    def _dnd_execute_copy_move_thread(
        self,
        sources: list[str],
        dest: str,
        is_move: bool,
        on_success=None,
    ):
        """Run copy/move in a worker; report results on main thread via after()."""
        action = "move" if is_move else "copy"
        action_past = "moved" if is_move else "copied"
        dest_name = os.path.basename(dest) or dest
        ok, fail = [], []

        def _unique_dst(dst_path: str) -> str:
            if not os.path.exists(dst_path):
                return dst_path
            base, ext = os.path.splitext(dst_path)
            counter = 1
            candidate = f"{base}_copy{ext}"
            while os.path.exists(candidate):
                counter += 1
                candidate = f"{base}_copy{counter}{ext}"
            return candidate

        for src in sources:
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
                dst = _unique_dst(dst)
                is_dir = os.path.isdir(src)
                if is_dir:
                    shutil.move(src, dst) if is_move else shutil.copytree(src, dst)
                else:
                    shutil.move(src, dst) if is_move else shutil.copy2(src, dst)
                self._dnd_sync_db_cache(src, dst, is_dir=is_dir, is_move=is_move)
                ok.append(src)
                logging.info(f"[DnD] {action_past}: {src} -> {dst}")
            except Exception as e:
                fail.append(src)
                logging.error(f"[DnD] error during {action}: {src} -> {dst}: {e}")

        def _finish():
            if ok:
                self.status_bar.set_action_message(
                    f"DnD: {action_past} {len(ok)} item(s) -> {dest_name}"
                )
                if on_success:
                    on_success()
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

    def _dnd_show_dialog_and_run(
        self,
        sources: list[str],
        dest: str,
        is_move: bool,
        on_success=None
    ):
        """Show confirmation dialog then run operation in a thread."""
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

        msg = f"Do you want to {action} {n} item(s)?\n\n{detail}\n\nDestination: {dest}"
        title = f"{'Move' if is_move else 'Copy'} - confirmation"

        def _confirm_run():
            threading.Thread(
                target=lambda: self._dnd_execute_copy_move_thread(
                    sources, dest, is_move, on_success
                ),
                daemon=True,
            ).start()

        def _cancel_run():
            logging.info("[DnD] Operation canceled by user.")

        self.universal_dialog(
            title=title,
            message=msg,
            confirm_callback=_confirm_run,
            cancel_callback=_cancel_run,
            confirm_text="Yes",
            cancel_text="No",
            show_cancel=True
        )

    def _dnd_on_position_tree(self, event):
        """
        Tree drag position: move/copy preview (internal vs Explorer), Shift via GetKeyState, edge autoscroll.
        """
        try:
            shift_down = bool(ctypes.windll.user32.GetKeyState(0x10) & 0x8000)
        except Exception:
            shift_down = bool(event.state & 0x0001) if hasattr(event, 'state') else False

        internal = getattr(self, "_dnd_internal_drag", False)
        is_move_preview = (not shift_down) if internal else shift_down
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
                cursor = "fleur" if is_move_preview else "plus"
                self.tree.configure(cursor=cursor)
            except Exception:
                pass

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

    def _dnd_tree_path_from_item(self, item: str | None) -> str | None:
        """Absolute path for tree item, or None."""
        if not item:
            return None
        try:
            path = self.tree.set(item, "path")
            if path and os.path.isdir(path):
                return path
        except Exception:
            pass
        return None

    # ── DnD debounce helpers ───────────────────────────────────────────────
    def _dnd_mark_thumb_press(self, event, file_path: str):
        self._dnd_drag_happened = False
        self._dnd_press_ts = time.monotonic()
        self._dnd_press_kind = "thumb"
        self._dnd_press_path = getattr(event.widget, "file_path", None) or file_path

    def _dnd_mark_tree_press(self, event):
        self._dnd_press_ts = time.monotonic()
        self._dnd_press_kind = "tree"
        item = self.tree.identify_row(event.y) if hasattr(self, "tree") else None
        self._dnd_press_path = self.tree.set(item, "path") if item else None

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
            return False
        if expected_path and self._dnd_press_path and os.path.normcase(os.path.normpath(expected_path)) != os.path.normcase(os.path.normpath(self._dnd_press_path)):
            return False
        return True

    def _dnd_thumb_drag_init(self, event, clicked_path: str = None):
        """
        tkinterdnd2 drag init from thumbnail canvas: prefer widget file_path, multi-drag from selection.
        """
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
        is_multi_drag = norm_canvas in norm_selected and len(selected_paths) > 1

        # Multi-drag: shorter hold; path match is relaxed (press widget may differ from DragInit source)
        required_hold_ms = self._dnd_hold_ms_multi if is_multi_drag else self._dnd_hold_ms
        guard_expected_path = None if is_multi_drag else canvas_path
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
        self._dnd_drag_happened = True
        return (dnd.COPY, dnd.DND_FILES, data)

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
        if not path or not os.path.exists(path):
            logging.info(f"[DnD] DRAG OUT tree skipped: invalid path={path!r}")
            return
        path_fwd = "{" + path.replace("\\", "/") + "}"
        logging.info(f"[DnD] DRAG OUT tree: {path}")
        self._dnd_internal_drag = True
        return (dnd.COPY, dnd.DND_FILES, path_fwd)

    def _dnd_drag_end(self, event):
        self._cancel_tree_dnd_autoscroll()
        self._dnd_internal_drag = False
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
