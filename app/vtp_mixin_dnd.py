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
from gui_elements import open_conflict_dialog


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
            f"internal={internal} is_move={is_move}"
        )

        self._dnd_confirm_and_execute(
            sources=sources,
            dest=dest,
            is_move=is_move,
            on_success=lambda: self.display_thumbnails(
                dest, force_refresh=True, preserve_scroll=True
            )
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
                    self.display_thumbnails(
                        parent, force_refresh=True, preserve_scroll=True
                    )
                else:
                    self.refresh_folder_icons_subtree(dest_folder)
                    # refresh_tree_view selects dest_folder in the tree; if the tree has focus,
                    # <<TreeviewSelect>> would load that folder — stay on current_directory instead.
                    self._suppress_tree_select_navigation = True
                    try:
                        self.refresh_tree_view(dest_folder)
                        self.select_current_folder_in_tree()
                        self.display_thumbnails(
                            self.current_directory,
                            force_refresh=True,
                            preserve_scroll=True,
                        )
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
        replace_all = False
        skip_all = False

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
                if os.path.exists(dst):
                    if skip_all:
                        logging.info("[DnD] conflict skip-all: %s", dst)
                        continue

                    should_replace = replace_all
                    if not should_replace:
                        choice, apply_all = self._dnd_prompt_conflict_choice(dst)
                        if choice == "cancel":
                            logging.info("[DnD] conflict canceled by user: %s", dst)
                            break
                        if choice == "skip":
                            if apply_all:
                                skip_all = True
                            continue
                        should_replace = choice == "replace"
                        if apply_all and should_replace:
                            replace_all = True

                    if should_replace and not self._dnd_delete_existing_target(dst):
                        fail.append(src)
                        logging.error("[DnD] replace failed (cannot remove target): %s", dst)
                        continue

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

    # Ask main UI thread for conflict action from worker threads.
    def _dnd_prompt_conflict_choice(self, dst_path: str) -> tuple[str, bool]:
        done = threading.Event()
        result: dict[str, tuple[str, bool]] = {"value": ("cancel", False)}

        def _ask():
            try:
                result["value"] = open_conflict_dialog(
                    self, os.path.basename(dst_path) or dst_path
                )
            except Exception:
                result["value"] = ("cancel", False)
            finally:
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
        self._dnd_mark_internal_drag_payload(paths)
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
        self._dnd_mark_internal_drag_payload([path])
        self._dnd_internal_drag_end_ts = 0.0
        return ((dnd.MOVE, dnd.COPY), dnd.DND_FILES, path_fwd)

    def _dnd_drag_end(self, event):
        self._cancel_tree_dnd_autoscroll()
        end_ts = time.monotonic()
        self._dnd_internal_drag_end_ts = end_ts

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
