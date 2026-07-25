"""
Simple sidecar caption editor for image dataset training.

Loads / saves ``<image_stem>.txt`` next to the selected image.
Unsaved edits autosave when switching to another file (dataset-tool style).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox

from vtp_constants import IMAGE_FORMATS


def caption_path_for_image(image_path: str | os.PathLike[str]) -> Path:
    """Return sibling ``.txt`` path for an image (same stem, .txt extension)."""
    p = Path(image_path)
    return p.with_suffix(".txt")


class CaptionEditorWidget(ctk.CTkFrame):
    """Bottom-panel text editor for per-image caption sidecars."""

    def __init__(self, parent, controller=None, **kwargs):
        super().__init__(parent, fg_color="transparent", **kwargs)
        self.controller = controller
        self._image_path: str | None = None
        self._caption_path: Path | None = None
        self._baseline_text: str = ""
        self._loading = False

        toolbar = ctk.CTkFrame(self, fg_color="transparent", height=28)
        toolbar.pack(side="top", fill="x", padx=6, pady=(4, 2))
        toolbar.pack_propagate(False)

        self.status_label = ctk.CTkLabel(
            toolbar,
            text="Select an image to edit its caption (.txt)",
            font=ctk.CTkFont(size=11),
            anchor="w",
            text_color=("gray30", "gray70"),
        )
        self.status_label.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.save_button = ctk.CTkButton(
            toolbar,
            text="Save",
            width=64,
            height=24,
            font=ctk.CTkFont(size=11),
            command=lambda: self.save(silent=False),
            state="disabled",
        )
        self.save_button.pack(side="right")

        self.text = ctk.CTkTextbox(
            self,
            font=ctk.CTkFont(family="Consolas", size=13),
            wrap="word",
            activate_scrollbars=True,
        )
        self.text.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<KeyRelease>", self._on_text_changed)
        self.text.bind("<<Paste>>", self._on_text_changed_later)
        self.text.bind("<<Cut>>", self._on_text_changed_later)
        self.text.bind("<Control-s>", self._on_ctrl_s)
        self.text.bind("<Control-S>", self._on_ctrl_s)
        self.text.bind("<Control-a>", self._on_ctrl_a)
        self.text.bind("<Control-A>", self._on_ctrl_a)
        self._set_enabled(False)

    # --- public API ---------------------------------------------------------

    def get_text(self) -> str:
        try:
            return self.text.get("1.0", "end-1c")
        except tk.TclError:
            return ""

    def is_dirty(self) -> bool:
        if self._caption_path is None:
            return False
        return self.get_text() != self._baseline_text

    def autosave_enabled(self) -> bool:
        ctrl = self.controller
        if ctrl is None:
            return True
        var = getattr(ctrl, "caption_autosave_var", None)
        if var is None:
            return True
        try:
            return bool(var.get())
        except Exception:
            return True

    def commit_before_leave(self) -> bool:
        """
        Resolve unsaved edits before switching image/mode.

        Returns False if the user cancelled (stay on current caption).
        Autosave on  → silent save
        Autosave off → Yes / No / Cancel dialog
        """
        if not self.is_dirty():
            return True

        if self.autosave_enabled():
            return self.save(silent=True)

        name = self._caption_path.name if self._caption_path else "caption"
        try:
            parent = self.winfo_toplevel()
            choice = messagebox.askyesnocancel(
                "Unsaved caption",
                f"Save changes to {name}?\n\n"
                f"Yes = Save\nNo = Discard\nCancel = Keep editing",
                parent=parent,
            )
        except Exception:
            # If dialog fails, fall back to autosave-like save to avoid data loss
            return self.save(silent=True)

        if choice is True:
            return self.save(silent=False)
        if choice is False:
            # Discard — mark clean so we don't re-prompt; next load replaces text
            self._baseline_text = self.get_text()
            self._sync_dirty_ui()
            return True
        return False  # Cancel

    def load_for_path(self, file_path: str | None, *, commit_previous: bool = True) -> bool:
        """Load caption for an image path, or clear if not an image.

        Returns False if leave was cancelled (user still editing previous caption).
        """
        if commit_previous:
            if not self.commit_before_leave():
                return False

        if not file_path or not str(file_path).lower().endswith(IMAGE_FORMATS):
            self.clear(message="Select an image to edit its caption (.txt)")
            return True

        self._image_path = file_path
        self._caption_path = caption_path_for_image(file_path)
        self._loading = True
        try:
            self._set_enabled(True)
            self.text.delete("1.0", "end")
            if self._caption_path.is_file():
                try:
                    content = self._caption_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    content = self._caption_path.read_text(encoding="utf-8", errors="replace")
                # Normalize newlines for stable dirty comparison
                content = content.replace("\r\n", "\n").replace("\r", "\n")
                self.text.insert("1.0", content)
                self._baseline_text = content
                self._set_status(f"{self._caption_path.name}", dirty=False)
            else:
                self._baseline_text = ""
                hint = "autosave on leave" if self.autosave_enabled() else "Save manually or enable Autosave"
                self._set_status(
                    f"{self._caption_path.name} (new — {hint})",
                    dirty=False,
                )
            self._sync_dirty_ui()
            try:
                self.text.edit_modified(False)
            except tk.TclError:
                pass
            return True
        except OSError as exc:
            logging.warning("[CaptionEditor] Failed to load %s: %s", self._caption_path, exc)
            self.clear(message=f"Could not read caption: {exc}")
            return True
        finally:
            self._loading = False

    def flush_if_dirty(self, *, silent: bool = True) -> bool:
        """Save current caption if modified. Returns True if a save was attempted/succeeded."""
        if not self.is_dirty():
            return False
        return self.save(silent=silent)

    def save(self, silent: bool = False) -> bool:
        """Write current text to the sidecar .txt. Returns True on success."""
        if self._caption_path is None:
            return False
        try:
            text = self.get_text()
            self._caption_path.write_text(text, encoding="utf-8", newline="\n")
            self._baseline_text = text
            try:
                self.text.edit_modified(False)
            except tk.TclError:
                pass
            self._sync_dirty_ui()
            self._set_status(f"{self._caption_path.name} — saved", dirty=False)
            # Bottom status bar (skyblue action text) — same style as other app feedback
            label = "Caption autosaved" if silent else "Caption saved"
            self._flash_status(f"{label}: {self._caption_path.name}")
            logging.info("[CaptionEditor] Saved %s", self._caption_path)
            return True
        except OSError as exc:
            logging.error("[CaptionEditor] Save failed for %s: %s", self._caption_path, exc)
            self._set_status(f"Save failed: {exc}", dirty=True)
            if not silent:
                try:
                    parent = self.winfo_toplevel()
                    messagebox.showerror("Caption save failed", str(exc), parent=parent)
                except Exception:
                    pass
            return False

    def clear(self, message: str = "Select an image to edit its caption (.txt)") -> None:
        self._image_path = None
        self._caption_path = None
        self._baseline_text = ""
        self._loading = True
        try:
            self.text.delete("1.0", "end")
            try:
                self.text.edit_modified(False)
            except tk.TclError:
                pass
            self._set_enabled(False)
            self.save_button.configure(state="disabled")
            self._set_status(message, dirty=False)
        finally:
            self._loading = False

    # --- internals ----------------------------------------------------------

    def _set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        try:
            self.text.configure(state=state)
        except tk.TclError:
            pass

    def _set_status(self, text: str, *, dirty: bool) -> None:
        prefix = "● " if dirty else ""
        self.status_label.configure(text=f"{prefix}{text}")

    def _sync_dirty_ui(self) -> None:
        dirty = self.is_dirty()
        self.save_button.configure(state="normal" if dirty else "disabled")
        if self._caption_path is not None:
            self._set_status(self._caption_path.name, dirty=dirty)

    def _flash_status(self, message: str) -> None:
        """Show brief skyblue confirmation in the bottom status bar action label."""
        if self.controller is None:
            return
        status_bar = getattr(self.controller, "status_bar", None)
        if status_bar is None:
            return
        try:
            # None → status bar default skyblue action color
            status_bar.set_action_message(message, color=None)
            self.controller.after(2500, status_bar.clear_action_message)
        except Exception:
            pass

    def _on_modified(self, _event=None):
        if self._loading:
            try:
                self.text.edit_modified(False)
            except tk.TclError:
                pass
            return
        try:
            if self.text.edit_modified():
                self.text.edit_modified(False)
                self._sync_dirty_ui()
        except tk.TclError:
            pass

    def _on_text_changed(self, _event=None):
        if self._loading:
            return
        self._sync_dirty_ui()

    def _on_text_changed_later(self, _event=None):
        if self._loading:
            return
        # Paste/Cut update buffer after the event
        try:
            self.after_idle(self._sync_dirty_ui)
        except Exception:
            self._sync_dirty_ui()

    def _on_ctrl_s(self, _event=None):
        self.save(silent=False)
        return "break"

    def _on_ctrl_a(self, _event=None):
        try:
            self.text.tag_add("sel", "1.0", "end-1c")
            self.text.mark_set("insert", "1.0")
            self.text.see("insert")
        except tk.TclError:
            pass
        return "break"
