"""Legacy middle-button drag/drop for tree and thumbnails (parallel to tkinterdnd2)."""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import tkinter as tk

from clipboard_file_list import (
    clipboard_has_pastable_paths,
    get_clipboard_file_paths,
    set_clipboard_file_paths,
)


class VtpLegacyDragMixin:
    def drag_motion_tree(self, event):
        """Handle dragging motion with improved target detection for tree and thumbnail frames."""
        # Update drag icon position
        if hasattr(self, 'drag_icon') and self.drag_icon:
            self.drag_icon.geometry("+{}+{}".format(event.x_root + 10, event.y_root + 10))

        # Detect the widget under the cursor
        widget = self.winfo_containing(event.x_root, event.y_root)
        widget_details = {
            "widget": widget,
            "type": type(widget),
            "widget_name": str(widget),
        }

        # Debug widget details
        logging.info(f"DEBUG: Widget under cursor details: {widget_details}")

        target_path = None

        if widget == self.tree:
            # Handle dragging over the folder tree
            target_item = self.tree.identify_row(event.y)
            target_column = self.tree.identify_column(event.x)  # Add column detection for accuracy
            logging.info(f"DEBUG: Tree target item: {target_item}, Column: {target_column}")

            if target_item:
                try:
                    # Use a safe approach to extract the path
                    item_values = self.tree.item(target_item, 'values')
                    if item_values and len(item_values) > 0:
                        target_path = item_values[0]
                        logging.info(f"DEBUG: Tree target item resolved to path: {target_path}")
                    else:
                        logging.info(f"WARNING: Tree item '{target_item}' has no 'values' or path is empty.")
                except Exception as e:
                    logging.info(f"ERROR: Failed to retrieve target path for item '{target_item}': {e}")
            else:
                logging.info("DEBUG: Dragging over tree, but no valid item identified.")
        elif hasattr(widget, "is_thumbnail_frame") and widget.is_thumbnail_frame:
            # Handle dragging over the thumbnail view
            target_path = widget.file_path
            if target_path:
                logging.info(f"DEBUG: Dragging over thumbnail frame. Target path: {target_path}")
            else:
                logging.info("WARNING: Dragging over thumbnail frame, but file_path is not set.")
        else:
            logging.info("DEBUG: Dragging over an unsupported widget or area.")

        # Final debug to confirm the resolved target path
        if target_path:
            logging.info(f"DEBUG: Resolved target path: {target_path}")
        else:
            logging.info("DEBUG: No valid target path could be resolved.")

        return target_path  # Return the resolved path for use by other functions


    def on_tree_scrollbar(self, *args):
        """Handle scrolling via the scrollbar."""
        self.tree.yview(*args)  # Pass the arguments to the original `yview` method
        self.refresh_tree_coordinates()
        # logging.info("DEBUG: Tree scrolled via scrollbar.")


    def on_tree_scroll_event(self, event):
        """Handle mouse wheel or other scroll events on the Treeview."""
        # logging.info("DEBUG: Tree scrolled via mouse event.")
        self.refresh_tree_coordinates()


    def refresh_tree_coordinates(self):
        """Refresh the tree's coordinate system using the left frame dimensions."""
        try:
            if not hasattr(self, "tree_style"):
                logging.info("WARNING: tree_style not initialized yet.")
                return

            row_height = self.tree_style.configure('Treeview').get('rowheight', 30)  # fallback default

            y_scroll_fraction_start, y_scroll_fraction_end = self.tree.yview()
            left_frame_height = self.left_frame.winfo_height()

            total_content_height = len(self.tree.get_children()) * row_height
            total_content_height += sum(
                len(self.tree.get_children(item)) * row_height for item in self.tree.get_children()
            )

        except Exception as e:
            logging.info(f"Error refreshing tree coordinates: {e}")


    def highlight_target(self, event):
        """Highlight the potential drop target during dragging."""
        if not hasattr(self, "_highlighted_thumbnail"):
            self._highlighted_thumbnail = None

        # Get the widget under the cursor
        widget = self.winfo_containing(event.x_root, event.y_root)
        logging.info(f"DEBUG: Widget under cursor: {widget}")

        if widget == self.tree:
            # Adjust y-coordinate based on tree's absolute position
            tree_y_start = getattr(self, 'tree_visible_start', self.tree.winfo_rooty())
            adjusted_y = event.y_root - tree_y_start

            logging.info(f"DEBUG: event.y_root={event.y_root}, tree_y_start={tree_y_start}, adjusted_y={adjusted_y}")

            # Identify the tree item under the adjusted y-coordinate
            item = self.tree.identify_row(adjusted_y)
            if item:
                item_text = self.tree.item(item)['text']
                logging.info(f"DEBUG: Tree item identified: {item}, Text: {item_text}")

                self.tree.selection_set(item)
                self.tree.focus(item)

                # Ensure the resolved target path matches the tree item
                resolved_path = self.get_full_path(item)
                logging.info(f"DEBUG: Resolved target path: {resolved_path}")

                # Highlight mismatch detection
                if not resolved_path.endswith(item_text):
                    logging.info(f"WARNING: Highlighted item and resolved path mismatch!\n"
                          f"Tree item: {item_text}, Resolved path: {resolved_path}")
                else:
                    logging.info(f"DEBUG: Highlighted item matches resolved path.")

            else:
                logging.info("DEBUG: No valid tree item found.")
                self.tree.selection_remove(self.tree.selection())

        elif hasattr(widget, "file_path") and widget.is_folder:
            # Thumbnail highlight logic remains unchanged
            if self._highlighted_thumbnail and self._highlighted_thumbnail != widget:
                if self._highlighted_thumbnail.winfo_exists():
                    border_items = self._highlighted_thumbnail.find_withtag("border")
                    if border_items:
                        self._highlighted_thumbnail.itemconfig(border_items[0], outline="black", width=1)
                    logging.info(f"DEBUG: Reset highlight for thumbnail: {self._highlighted_thumbnail.file_path}")
                self._highlighted_thumbnail = None

            border_items = widget.find_withtag("border")
            if border_items:
                widget.itemconfig(border_items[0], outline="blue", width=2)
                self._highlighted_thumbnail = widget
                logging.info(f"DEBUG: Highlighting folder: {widget.file_path}")
        else:
            if self._highlighted_thumbnail:
                border_items = self._highlighted_thumbnail.find_withtag("border")
                if border_items:
                    self._highlighted_thumbnail.itemconfig(border_items[0], outline="black", width=1)
                logging.info(f"DEBUG: Reset highlight for invalid target: {self._highlighted_thumbnail.file_path}")
                self._highlighted_thumbnail = None




    def reset_highlight(self):
        """Reset visual highlights for drag-and-drop targets."""
        # Reset tree selection
        self.tree.selection_remove(self.tree.selection())

        # Reset thumbnail highlights
        for canvas in getattr(self, "thumbnail_canvases", []):  # Assume you have a list of canvases
            border_items = canvas.find_withtag("border")
            if border_items:
                canvas.itemconfig(border_items[0], outline="black", width=1)  # Default border style



    def end_drag(self, event):
        """Complete the drag-and-drop operation."""
        # Destroy drag icon if it exists
        if hasattr(self, "drag_icon") and self.drag_icon.winfo_exists():
            self.drag_icon.destroy()
            del self.drag_icon

        # Reset all highlights in tree and thumbnail views
        self.reset_highlight()
        
         # Unbind Motion event to stop further highlighting
        self.unbind("<Motion>")

        # Ensure any other cleanup logic for dragging is executed
        logging.info("Drag operation finalized.")


    def drop_item(self, event, copy_mode=False):
        """Handle dropping items with delay after suspending the directory watcher."""
        logging.info("Drop event triggered.")

        # Resolve target path
        target_path = self.get_drop_target(event)
        if not target_path:
            logging.info("Error: Could not resolve target path.")
            return
        
             # Log resolved target for debugging
        logging.info(f"DEBUG: Final resolved drop target path: {target_path}")
        if target_path != self.drag_data.get("highlighted_item"):
            logging.info(f"WARNING: Highlighted item and drop target path mismatch!")
            
        # Validate the target path
        if not self.validate_drop_target(target_path):
            return

        # Suspend the watcher
        if self.watchdog_observer and self.watchdog_observer.is_alive():
            logging.info("Suspending directory watcher...")
            self.watchdog_observer.stop()
            time.sleep(0.2)  # Introduce a small delay to stabilize

        # Run file operations in a separate thread
        def process_drop():
            moved_items = []  # Track moved items for refreshing views
            operation = "copy" if copy_mode else "move"

            if not self.selected_thumbnails:
                # Single folder drag (Tree-specific)
                logging.debug("DND: single-item drag")
                self.handle_single_drag(target_path, copy_mode, moved_items)
            else:
                # Multiple items drag (Thumbnail-specific)
                logging.debug("DND: multi-item drag")
                self.handle_multiple_drag(target_path, copy_mode, moved_items)

            # Finalize drop on the main thread
            def finalize():
                self.finalize_drop(target_path, moved_items)
                # Restart the watcher
                try:
                    logging.info("Resuming directory watcher...")
                    time.sleep(0.2)  # Ensure the system stabilizes before resuming the watcher
                    self.watchdog_observer.start()
                except Exception as e:
                    logging.info(f"Error restarting directory watcher: {e}")

            self.after(0, finalize)

        # Spawn the worker thread
        threading.Thread(target=process_drop, daemon=True).start()

        # Ensure the drag icon is cleaned up
        if hasattr(self, 'drag_icon') and self.drag_icon.winfo_exists():
            try:
                self.drag_icon.destroy()
                del self.drag_icon
            except Exception as e:
                logging.info(f"Error destroying drag icon: {e}")
    
    def get_drop_target(self, event):
        """Identify the widget under the cursor and resolve its target path."""
        widget = self.winfo_containing(event.x_root, event.y_root)
        logging.info(f"DEBUG: Widget under cursor details: {widget}")

        if widget == self.tree:
            tree_y_start = getattr(self, 'tree_visible_start', self.tree.winfo_rooty())
            adjusted_y = event.y_root - tree_y_start

            target_item = self.tree.identify_row(adjusted_y)
            if target_item:
                item_text = self.tree.item(target_item)['text']
                path = self.get_full_path(target_item)
                logging.info(f"DEBUG: Tree item: {item_text}, Resolved path: {path}")
                if path.endswith(item_text):
                    logging.info(f"DEBUG: Resolved target path matches tree item text: {item_text}")
                    return path
                else:
                    logging.info(f"WARNING: Mismatch between tree item and resolved path!")
                    return None
            else:
                logging.info("Error: No valid tree item found under cursor.")
                return None

        if hasattr(widget, "file_path") and widget.is_folder:
            path = widget.file_path
            logging.info(f"DEBUG: Thumbnail resolved target path: {path}")
            return path

        logging.info("Error: Unsupported drop target.")
        return None


    def validate_drop_target(self, target_path):
        """Validate that the drop target is a valid directory."""
        if not target_path:
            logging.info("Error: Drop target path is None.")
            return False
        if not os.path.exists(target_path):
            logging.info(f"Error: Drop target path does not exist: {target_path}")
            return False
        if not os.path.isdir(target_path):
            logging.info(f"Error: Drop target is not a directory: {target_path}")
            return False
        logging.info(f"Validated drop target path: {target_path}")
        return True


    def handle_single_drag(self, target_path, copy_mode, moved_items):
        """Handle dragging a single folder from the tree view."""
        logging.info("**************HANDLE SINGLE DRAG!!!")
        
        # Resolve the source path
        source_path_hash = self.drag_data.get("path")
        if not source_path_hash:
            logging.info("Error: No source path provided for the drag operation.")
            return

        # Convert the hash back to a full path
        source_path = self.find_path_by_hash(source_path_hash)
        if not source_path:
            logging.info(f"Error: Could not resolve hash to path: {source_path_hash}")
            return

        # Ensure source_path and target_path are not the same
        if target_path == source_path:
            logging.info("Error: Cannot drop onto the same path.")
            return

        try:
            # Construct the new path
            new_path = os.path.join(target_path, os.path.basename(source_path))
            if not os.path.exists(new_path):
                # Perform the move or copy
                self.perform_move_or_copy(source_path, new_path, copy_mode, moved_items)
                 # Add a small delay to handle potential timing issues
                # time.sleep(0.2)  # 200ms delay to stabilize system operations
                self.move_cache(source_path, new_path)

                # Update the database with the new path
                self.database.update_folder_path(source_path, new_path)

                # Refresh folder icons
                self.refresh_folder_icons_subtree(source_path)
                self.refresh_folder_icons_subtree(new_path)
            else:
                  # Show overwrite confirmation
                message = f"The file '{os.path.basename(new_path)}' already exists in the destination.\nDo you want to overwrite it?"
                self.universal_dialog(
                    title="Overwrite Confirmation",
                    message=message,
                    confirm_callback= self.perform_move_or_copy(source_path, new_path, copy_mode, moved_items)
                )
        except Exception as e:
            logging.info(f"Error during move/copy: {e}")


    def handle_multiple_drag(self, target_path, copy_mode, moved_items):
        """Handle dragging multiple items from the thumbnail view."""
        logging.info("**************HANDLE MULTIPLE DRAG!!!")

        for file_path, _, _ in self.selected_thumbnails:
            if target_path == file_path:
                logging.info(f"Error: Cannot drop {file_path} onto itself.")
                continue

            try:
                new_path = os.path.join(target_path, os.path.basename(file_path))

                if os.path.exists(new_path):
                    def replace_callback():
                        self.perform_move_or_copy(file_path, new_path, copy_mode, moved_items)
                        # time.sleep(0.2)  # 200ms delay to stabilize system operations

                    def skip_callback():
                        logging.info(f"Skipping file: {file_path}")

                    self.universal_dialog(
                            title="File Exists",
                            message=f"The file '{os.path.basename(new_path)}' already exists.\nDo you want to replace it?",
                            confirm_callback=replace_callback,
                            cancel_callback=lambda: logging.info("Operation canceled."),
                            third_button="Skip",
                            third_callback=skip_callback
                     )
                else:
                    # No conflict; perform the move/copy
                    self.database.update_folder_path(file_path, new_path)
                    self.perform_move_or_copy(file_path, new_path, copy_mode, moved_items)
                    # time.sleep(0.2)  # 200ms delay to stabilize system operations

                    if os.path.isdir(file_path):
                        self.move_cache(file_path, new_path)
                        self.refresh_folder_icons_subtree(file_path)
                        self.refresh_folder_icons_subtree(new_path)

            except Exception as e:
                logging.info(f"Error during move/copy of {file_path}: {e}")



    def perform_move_or_copy(self, source_path, new_path, copy_mode, moved_items):
        """Perform the actual move or copy operation."""
        if not os.path.exists(source_path):
            logging.info(f"Error: Source path does not exist: {source_path}")
            return

        try:
            if os.path.exists(new_path):
                logging.info(f"Conflict detected: {new_path} already exists. Skipping.")
                return  # Skip the operation for conflicting files

            if copy_mode:
                if os.path.isfile(source_path):
                    shutil.copy(source_path, new_path)
                else:
                    shutil.copytree(source_path, new_path)
                logging.info(f"Copied {source_path} to {new_path}")
            else:
                shutil.move(source_path, new_path)
                logging.info(f"Moved {source_path} to {new_path}")
                for i, (path, label, index) in enumerate(self.selected_thumbnails):
                    if path == source_path:
                        self.selected_thumbnails[i] = (new_path, label, index)
                moved_items.append(source_path)
        except PermissionError as e:
            logging.info(f"Permission error for {source_path}: {e}")
        except Exception as e:
            logging.info(f"Error performing move/copy from {source_path} to {new_path}: {e}")



    def move_cache(self, source_path, new_path):
        """Move the cache directory for the corresponding folder."""
        def calculate_cache_path(file_path):
            relative_path = os.path.abspath(file_path).replace(":", "")
            return os.path.join(self.thumbnail_cache_path, relative_path)

        cache_path = calculate_cache_path(source_path)
        new_cache_path = calculate_cache_path(new_path)

        if os.path.exists(cache_path):
            try:
                shutil.move(cache_path, new_cache_path)
                logging.info(f"Moved cache from {cache_path} to {new_cache_path}")
            except Exception as e:
                logging.info(f"Error moving cache directory: {e}")
        else:
            logging.info(f"DEBUG: Cache directory not found: {cache_path}")

    def finalize_drop(self, target_path, moved_items):
        """Perform final updates after dropping items."""
        logging.info(f"Finalizing drop to target path: {target_path}")

        # Debug: List moved items
        if moved_items:
            logging.info(f"Moved items: {moved_items}")
        else:
            logging.info("No items moved. Skipping cleanup.")
            return  # Exit early if no items were moved

        # Cleanup stale references in selected thumbnails
        logging.info(f"Selected thumbnails before cleanup: {len(self.selected_thumbnails)}")
        self.selected_thumbnails = [
            (thumb_path, thumb_name, thumb_label)
            for thumb_path, thumb_name, thumb_label in self.selected_thumbnails
            if os.path.exists(thumb_path)
        ]
        logging.info(f"Selected thumbnails after cleanup: {len(self.selected_thumbnails)}")

        # Refresh the tree for all moved items
        for moved_item in moved_items:
            self.update_tree_view(moved_item, target_path)

        # Refresh views
        if self.current_directory:
            self.display_thumbnails(self.current_directory)
            logging.info(f"Thumbnails refreshed for current directory: {self.current_directory}")

        # Restart the directory watcher for the current directory only if it exists
        if os.path.exists(self.current_directory):
            try:
                logging.info(f"Restarting directory watcher for {self.current_directory}")
                self.start_directory_watcher(self.current_directory)
            except Exception as e:
                logging.info(f"Error restarting directory watcher: {e}")
        else:
            logging.info(f"WARNING: Cannot restart watcher, current directory does not exist: {self.current_directory}")

        # Update the status bar
        folder_count, file_count, total_size = self.status_bar.count_folders_and_files(self.current_directory)
        selected_count, selected_size = self.status_bar.count_selected_files_and_size(self.selected_thumbnails)
        self.status_bar.update_status(folder_count, file_count, total_size, selected_count, selected_size)

    def paths_for_clipboard_from_thumb_context(self, primary_path: str) -> list:
        """Use multi-selection when the right-clicked item is part of it; otherwise the single path."""
        primary = os.path.normpath(primary_path)
        raw = list(getattr(self, "selected_thumbnails", []) or [])
        paths = []
        for item in raw:
            p = item[0] if isinstance(item, tuple) and item else item
            if p:
                paths.append(os.path.normpath(p))
        if len(paths) > 1 and primary in paths:
            return paths
        return [primary]

    def _clipboard_status_flash(self, message: str, clear_after_ms: int = 4000) -> None:
        """Brief sky-blue status line (same strip as autotag / scan messages)."""
        try:
            self.status_bar.set_action_message(message)
            self.after(clear_after_ms, self.status_bar.clear_action_message)
        except Exception:
            pass

    def copy_thumb_paths_to_clipboard(self, primary_path: str) -> None:
        paths = self.paths_for_clipboard_from_thumb_context(primary_path)
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return
        if set_clipboard_file_paths(paths, cut=False):
            logging.info("[clipboard] Copied %d path(s) from thumbnail view", len(paths))
            n = len(paths)
            if n == 1:
                self._clipboard_status_flash(
                    f"Copied to clipboard: {os.path.basename(paths[0])}"
                )
            else:
                self._clipboard_status_flash(f"Copied to clipboard ({n} items).")

    def copy_tree_folder_path_to_clipboard(self, folder_path: str) -> None:
        if not folder_path or not os.path.isdir(folder_path):
            return
        if set_clipboard_file_paths([os.path.normpath(folder_path)], cut=False):
            logging.info("[clipboard] Copied folder: %s", folder_path)
            self._clipboard_status_flash(
                f"Copied to clipboard: folder {os.path.basename(folder_path)}"
            )

    def add_clipboard_paste_cascade(self, menu: tk.Menu, dest_dir: str | None) -> None:
        """Append a Paste submenu (Copy here / Move here) to a context menu."""
        paste_sub = tk.Menu(menu, tearoff=0)
        can_paste = bool(
            clipboard_has_pastable_paths() and dest_dir and os.path.isdir(dest_dir)
        )
        paste_sub.add_command(
            label="Copy here",
            command=lambda d=dest_dir: self.paste_clipboard_into_folder(d, True),
            state=tk.NORMAL if can_paste else tk.DISABLED,
        )
        paste_sub.add_command(
            label="Move here",
            command=lambda d=dest_dir: self.paste_clipboard_into_folder(d, False),
            state=tk.NORMAL if can_paste else tk.DISABLED,
        )
        menu.add_cascade(
            label="Paste",
            menu=paste_sub,
            state=tk.NORMAL if can_paste else tk.DISABLED,
        )

    def _paste_move_would_nest_folder_inside_itself(self, source_path: str, dest_dir: str) -> bool:
        if not os.path.isdir(source_path):
            return False
        try:
            src_abs = os.path.normcase(os.path.abspath(source_path))
            dst_abs = os.path.normcase(os.path.abspath(dest_dir))
            common = os.path.commonpath([src_abs, dst_abs])
        except ValueError:
            return False
        return common == src_abs and dst_abs != src_abs

    def _finalize_paste_operations(
        self, dest_dir: str, moved_sources: list, copy_mode: bool = True
    ) -> None:
        """Refresh tree, grid, and watcher after clipboard paste (copy or move)."""
        for src in moved_sources:
            self.update_tree_view(src, dest_dir)
        target_node = self.find_node_by_path(dest_dir)
        if target_node:
            self.process_directory(target_node, dest_dir)
        if getattr(self, "selected_thumbnails", None):
            self.selected_thumbnails = [
                (thumb_path, thumb_name, thumb_label)
                for thumb_path, thumb_name, thumb_label in self.selected_thumbnails
                if os.path.exists(thumb_path)
            ]
        if self.current_directory:
            self.display_thumbnails(self.current_directory)
        if self.current_directory and os.path.exists(self.current_directory):
            try:
                self.start_directory_watcher(self.current_directory)
            except Exception as e:
                logging.info(f"Error restarting directory watcher after paste: {e}")
        folder_count, file_count, total_size = self.status_bar.count_folders_and_files(self.current_directory)
        selected_count, selected_size = self.status_bar.count_selected_files_and_size(self.selected_thumbnails)
        self.status_bar.update_status(folder_count, file_count, total_size, selected_count, selected_size)
        if copy_mode:
            self._clipboard_status_flash("Pasted into folder (copy).")
        else:
            self._clipboard_status_flash("Pasted into folder (move).")

    def paste_clipboard_into_folder(self, dest_dir: str, copy_mode: bool = True) -> None:
        """Paste paths from the system (or in-app) file clipboard into dest_dir."""
        sources = get_clipboard_file_paths()
        if not sources:
            logging.info("[clipboard] Paste: no paths on clipboard")
            return
        dest_dir = os.path.normpath(dest_dir)
        if not dest_dir or not os.path.isdir(dest_dir):
            logging.info("[clipboard] Paste: invalid destination %s", dest_dir)
            return

        if self.watchdog_observer and self.watchdog_observer.is_alive():
            logging.info("Suspending directory watcher for paste...")
            self.watchdog_observer.stop()
            time.sleep(0.2)

        def process_paste():
            moved_sources = []

            for file_path in sources:
                if not os.path.exists(file_path):
                    continue
                if os.path.normcase(dest_dir) == os.path.normcase(file_path):
                    continue
                if not copy_mode and self._paste_move_would_nest_folder_inside_itself(file_path, dest_dir):
                    logging.info("[clipboard] Skip move into self: %s -> %s", file_path, dest_dir)
                    continue

                new_path = os.path.join(dest_dir, os.path.basename(file_path))

                try:
                    if os.path.exists(new_path):

                        def replace_callback():
                            try:
                                if os.path.isdir(new_path):
                                    shutil.rmtree(new_path)
                                else:
                                    os.remove(new_path)
                            except OSError as e:
                                logging.info("Paste replace: could not remove %s: %s", new_path, e)
                                return
                            if not copy_mode:
                                self.database.update_folder_path(file_path, new_path)
                            self.perform_move_or_copy(file_path, new_path, copy_mode, moved_sources)
                            if os.path.isdir(file_path):
                                self.move_cache(file_path, new_path)
                                self.refresh_folder_icons_subtree(file_path)
                                self.refresh_folder_icons_subtree(new_path)

                        def skip_callback():
                            logging.info(f"[clipboard] Skipped: {file_path}")

                        self.universal_dialog(
                            title="File Exists",
                            message=(
                                f"The item '{os.path.basename(new_path)}' already exists.\n"
                                "Replace it?"
                            ),
                            confirm_callback=replace_callback,
                            cancel_callback=lambda: logging.info("Paste canceled."),
                            third_button="Skip",
                            third_callback=skip_callback,
                        )
                    else:
                        if not copy_mode:
                            self.database.update_folder_path(file_path, new_path)
                        self.perform_move_or_copy(file_path, new_path, copy_mode, moved_sources)
                        if os.path.isdir(file_path):
                            self.move_cache(file_path, new_path)
                            self.refresh_folder_icons_subtree(file_path)
                            self.refresh_folder_icons_subtree(new_path)
                except Exception as e:
                    logging.info("Paste error for %s: %s", file_path, e)

            def finalize():
                self._finalize_paste_operations(
                    dest_dir, [] if copy_mode else moved_sources, copy_mode
                )
                try:
                    logging.info("Resuming directory watcher after paste...")
                    time.sleep(0.2)
                    self.watchdog_observer.start()
                except Exception as e:
                    logging.info(f"Error restarting directory watcher: {e}")

            self.after(0, finalize)

        threading.Thread(target=process_paste, daemon=True).start()

    def in_thumbnail_area(self, event):
        """Check if the drop is in the thumbnail view."""
        x, y = event.x_root, event.y_root
        canvas_bbox = self.canvas.bbox(self.scrollable_frame)
        return canvas_bbox and canvas_bbox[0] <= x <= canvas_bbox[2] and canvas_bbox[1] <= y <= canvas_bbox[3]

    def start_drag(self, event, source_type):
        """Start dragging an item from tree or thumbnails."""
        if source_type == "tree":
            # Handle dragging a folder from the tree
            item = self.tree.identify_row(event.y)
            if item:
                values = self.tree.item(item, 'values')
                if values:
                    path = values[0]  # Absolute path
                    path_hash = values[1]  # Path hash
                    self.drag_data["item_id"] = item
                    self.drag_data["path"] = path_hash  # Use hash for dragging
                    logging.info(f"Dragging folder (hash): {path_hash}")
                else:
                    logging.info("Warning: Tree item has no 'values'.")
        elif source_type == "thumbnail":
            # Handle dragging a file from the thumbnails
            canvas = event.widget
            if hasattr(canvas, "file_path"):
                file_path = canvas.file_path
                self.drag_data["item_id"] = None  # Thumbnails don't have tree nodes
                self.drag_data["path"] = file_path
                logging.info(f"Dragging file: {file_path}")
            else:
                logging.info("Error: No file_path associated with this thumbnail.")
                return  # Abort dragging if no file_path is found
        # Bind motion events to highlight potential drop targets
        self.bind("<Motion>", self.highlight_target)
        # Create drag icon for visual feedback
        self.create_drag_icon(event)



    def find_path_by_hash(self, path_hash):
        """Resolve a path hash back to its original path."""
        for item in self.tree.get_children(''):  # Iterate through top-level nodes
            resolved_path = self._find_path_by_hash_recursive(item, path_hash)
            if resolved_path:
                return resolved_path
        logging.info(f"Error: Path not found for hash: {path_hash}")
        return None

    def _find_path_by_hash_recursive(self, parent_item, path_hash):
        """Recursive helper to locate the path hash."""
        # Check current item's values
        values = self.tree.item(parent_item, 'values')
        if values and len(values) > 1 and values[1] == path_hash:  # Check the hash in values
            return values[0]  # Return the full path

        # Recursively check children
        for child_item in self.tree.get_children(parent_item):
            resolved_path = self._find_path_by_hash_recursive(child_item, path_hash)
            if resolved_path:
                return resolved_path

        return None  # Path not found in this subtree


    def create_drag_icon(self, event):
        """Create a visual drag icon for both tree and thumbnail items."""
        # Get the source path from drag data
        source_path = self.drag_data.get("path")
        if not source_path:
            logging.info("DEBUG: No source path available for drag icon.")
            return

        # Create a drag icon (Toplevel window)
        self.drag_icon = tk.Toplevel(self)
        self.drag_icon.overrideredirect(True)
        self.drag_icon.geometry("+{}+{}".format(event.x_root + 10, event.y_root + 10))
        self.drag_icon.attributes("-topmost", True)

        # Add visual content to the drag icon
        if self.drag_data.get("source_type") == "tree":
            # For tree items, display the folder name
            item = self.drag_data.get("item_id")
            label_text = self.tree.item(item)['text'] if item else "Unknown"
        elif self.drag_data.get("source_type") == "thumbnail":
            # For thumbnails, display the file or folder name
            label_text = os.path.basename(source_path)
        else:
            label_text = "Dragging..."

        label = tk.Label(self.drag_icon, text=label_text,
                         bg="lightgray", fg="black", padx=5, pady=2)
        label.pack()

