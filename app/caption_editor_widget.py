"""
Simple sidecar caption editor for image / video dataset training.

Loads / saves ``<media_stem>.txt`` next to the selected image or video.
Unsaved edits autosave when switching to another file (dataset-tool style).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox

from text_find_bar import TextFindReplaceBar
from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS

# Media that can have a sibling ``.txt`` caption sidecar
CAPTIONABLE_FORMATS = IMAGE_FORMATS + VIDEO_FORMATS

_SELECT_HINT = "Select an image or video to edit its caption (.txt)"


def is_captionable_path(file_path: str | os.PathLike[str] | None) -> bool:
    """True if path is a supported image or video (caption sidecar eligible)."""
    try:
        path = str(file_path) if file_path is not None else ""
    except Exception:
        return False
    return bool(path) and path.lower().endswith(CAPTIONABLE_FORMATS)


def caption_path_for_image(image_path: str | os.PathLike[str]) -> Path:
    """Return sibling ``.txt`` path for media (same stem, .txt extension)."""
    p = Path(image_path)
    return p.with_suffix(".txt")


# Alias — same stem/.txt rule for images and videos
caption_path_for_media = caption_path_for_image


def existing_caption_sidecar(media_path: str | os.PathLike[str]) -> str | None:
    """If ``media_path`` is image/video with a sibling ``.txt``, return that path."""
    try:
        path = str(media_path)
    except Exception:
        return None
    if not is_captionable_path(path):
        return None
    cap = caption_path_for_media(path)
    try:
        if cap.is_file():
            return str(cap)
    except OSError:
        return None
    return None


def count_caption_sidecars(paths: list[str] | None) -> int:
    """How many paths already have a ``.txt`` sidecar (images or videos)."""
    if not paths:
        return 0
    n = 0
    for p in paths:
        if existing_caption_sidecar(p):
            n += 1
    return n


def filter_covered_caption_txt_sources(
    sources: list[str], *, with_captions: bool
) -> list[str]:
    """
    When transferring captions with media, drop standalone ``.txt`` entries that
    are sidecars of selected images/videos (those are handled with the media file).
    """
    if not with_captions or not sources:
        return list(sources or [])
    covered: set[str] = set()
    for src in sources:
        cap = existing_caption_sidecar(src)
        if cap:
            try:
                covered.add(os.path.normcase(os.path.normpath(cap)))
            except Exception:
                pass
    if not covered:
        return list(sources)
    out: list[str] = []
    for src in sources:
        try:
            key = os.path.normcase(os.path.normpath(src))
        except Exception:
            out.append(src)
            continue
        if key in covered and str(src).lower().endswith(".txt"):
            continue
        out.append(src)
    return out


def transfer_caption_sidecar(src_media: str, dst_media: str, *, is_move: bool) -> bool:
    """
    Copy or move ``src_media``'s sibling ``.txt`` next to ``dst_media``.

    Returns True if a sidecar was transferred.
    """
    # Prefer caption beside the original path. After a media *move*, the
    # file is gone from src but the .txt usually still sits next to the old name.
    candidates: list[str] = []
    found = existing_caption_sidecar(src_media)
    if found:
        candidates.append(found)
    try:
        candidates.append(str(caption_path_for_media(src_media)))
    except Exception:
        pass

    src_cap = None
    seen: set[str] = set()
    for c in candidates:
        if not c:
            continue
        try:
            key = os.path.normcase(os.path.normpath(c))
        except Exception:
            key = c
        if key in seen:
            continue
        seen.add(key)
        try:
            if os.path.isfile(c):
                src_cap = c
                break
        except OSError:
            continue
    if not src_cap:
        return False

    dst_cap = str(caption_path_for_media(dst_media))
    try:
        src_n = os.path.normcase(os.path.normpath(src_cap))
        dst_n = os.path.normcase(os.path.normpath(dst_cap))
    except Exception:
        src_n = src_cap
        dst_n = dst_cap
    if src_n == dst_n:
        return False

    try:
        import shutil

        parent = os.path.dirname(dst_cap)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if is_move:
            if os.path.exists(dst_cap):
                try:
                    os.remove(dst_cap)
                except OSError:
                    pass
            shutil.move(src_cap, dst_cap)
        else:
            shutil.copy2(src_cap, dst_cap)
        logging.info(
            "[Caption] %s sidecar: %s -> %s",
            "moved" if is_move else "copied",
            src_cap,
            dst_cap,
        )
        return True
    except OSError as exc:
        logging.warning(
            "[Caption] Failed to %s sidecar %s -> %s: %s",
            "move" if is_move else "copy",
            src_cap,
            dst_cap,
            exc,
        )
        return False


class CaptionEditorWidget(ctk.CTkFrame):
    """Bottom-panel text editor for per-file caption sidecars (images and videos)."""

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
            text=_SELECT_HINT,
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

        self.find_button = ctk.CTkButton(
            toolbar,
            text="Find",
            width=52,
            height=24,
            font=ctk.CTkFont(size=11),
            command=lambda: self.open_find(replace=False),
            state="disabled",
        )
        self.find_button.pack(side="right", padx=(0, 6))

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

        self.find_bar = TextFindReplaceBar(
            self,
            self.text,
            allow_replace=True,
            scope_values=("Current", "Folder"),
            on_after_current_exhausted=self._find_in_folder_step,
            on_replace_all_folder=self._replace_all_in_folder,
        )
        self.find_bar.bind_to_textbox()
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
        """Load caption for an image/video path, or clear if not captionable.

        Returns False if leave was cancelled (user still editing previous caption).
        """
        if commit_previous:
            if not self.commit_before_leave():
                return False

        if not is_captionable_path(file_path):
            self.clear(message=_SELECT_HINT)
            return True

        self._image_path = file_path
        self._caption_path = caption_path_for_media(file_path)
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
            if getattr(self, "find_bar", None) is not None and self.find_bar.visible:
                self.find_bar.refresh_highlights()
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

    def clear(self, message: str = _SELECT_HINT) -> None:
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

    def open_find(self, *, replace: bool = False) -> bool:
        """Open find (or find+replace) bar — also bound to Ctrl+F / Ctrl+H."""
        if self._caption_path is None and not self.text_is_enabled():
            return False
        self.find_bar.show(replace=replace)
        return True

    def text_is_enabled(self) -> bool:
        try:
            return str(self.text.cget("state")) != "disabled"
        except tk.TclError:
            return False

    # --- find / replace (folder scope) --------------------------------------

    def _iter_folder_caption_targets(self) -> list[tuple[str, Path]]:
        """
        Return (media_path, caption_path) for captionable files in the current grid.

        Prefers ``controller.video_files`` order so Find Next follows the thumbnail order.
        """
        ctrl = self.controller
        out: list[tuple[str, Path]] = []
        seen: set[str] = set()

        def _add(media: str) -> None:
            if not is_captionable_path(media):
                return
            try:
                key = os.path.normcase(os.path.normpath(media))
            except Exception:
                key = media
            if key in seen:
                return
            seen.add(key)
            out.append((media, caption_path_for_media(media)))

        files = getattr(ctrl, "video_files", None) if ctrl is not None else None
        if files:
            for item in files:
                try:
                    if isinstance(item, dict):
                        if item.get("is_folder"):
                            continue
                        path = item.get("path")
                    else:
                        path = item
                    if path:
                        _add(str(path))
                except Exception:
                    continue
            if out:
                return out

        # Fallback: scan directory of current media / current_directory
        folder = None
        if self._image_path:
            folder = os.path.dirname(self._image_path)
        elif ctrl is not None:
            folder = getattr(ctrl, "current_directory", None)
        if folder and os.path.isdir(folder):
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                names = []
            for name in names:
                _add(os.path.join(folder, name))
        return out

    def _caption_contains(self, cap_path: Path, needle: str, *, nocase: bool) -> bool:
        try:
            if not cap_path.is_file():
                return False
            text = cap_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            try:
                text = cap_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
        if nocase:
            return needle.casefold() in text.casefold()
        return needle in text

    def _select_media_path(self, media_path: str) -> bool:
        """Select thumbnail for ``media_path`` so the caption editor reloads it."""
        ctrl = self.controller
        if ctrl is None:
            return False
        files = getattr(ctrl, "video_files", None) or []
        idx = -1
        try:
            target = os.path.normcase(os.path.normpath(media_path))
        except Exception:
            target = media_path
        for i, item in enumerate(files):
            try:
                path = item.get("path") if isinstance(item, dict) else item
                if not path:
                    continue
                if os.path.normcase(os.path.normpath(str(path))) == target:
                    idx = i
                    break
            except Exception:
                continue
        if idx < 0:
            return False
        try:
            if hasattr(ctrl, "select_thumbnail"):
                ctrl.select_thumbnail(idx, shift=False, ctrl=False)
                return True
        except Exception:
            logging.debug("[CaptionEditor] select_thumbnail failed", exc_info=True)
        return False

    def _find_in_folder_step(self, needle: str, forward: bool) -> bool:
        """Jump to next/previous file in the folder whose caption contains ``needle``."""
        if not needle:
            return False
        targets = self._iter_folder_caption_targets()
        if not targets:
            return False
        nocase = True
        try:
            nocase = not bool(self.find_bar.case_var.get())
        except Exception:
            pass

        # Index of current media in the list
        cur = -1
        if self._image_path:
            try:
                cur_key = os.path.normcase(os.path.normpath(self._image_path))
            except Exception:
                cur_key = self._image_path
            for i, (media, _) in enumerate(targets):
                try:
                    if os.path.normcase(os.path.normpath(media)) == cur_key:
                        cur = i
                        break
                except Exception:
                    continue

        n = len(targets)
        if n == 0:
            return False
        if cur < 0:
            order = range(n) if forward else range(n - 1, -1, -1)
        elif forward:
            order = [(cur + 1 + k) % n for k in range(n)]
        else:
            order = [(cur - 1 - k) % n for k in range(n)]

        for i in order:
            media, cap = targets[i]
            # Skip current file — caller already exhausted it
            if cur >= 0 and i == cur:
                continue
            if not self._caption_contains(cap, needle, nocase=nocase):
                continue
            # Commit current edits before switching
            if not self.commit_before_leave():
                return True  # consumed; stay put
            if self._select_media_path(media):
                # load_for_path is triggered by selection; also force load as fallback
                try:
                    if getattr(self, "_image_path", None) != media:
                        self.load_for_path(media, commit_previous=False)
                except Exception:
                    pass
                # Highlight first/last match after load
                try:
                    self.after(50, lambda: self.find_bar.refresh_highlights())
                except Exception:
                    self.find_bar.refresh_highlights()
                self._flash_status(f"Find: {os.path.basename(media)}")
                return True
            # Selection failed — load directly
            if self.load_for_path(media, commit_previous=False):
                self.find_bar.refresh_highlights()
                self._flash_status(f"Find: {os.path.basename(media)}")
                return True
        return False

    def _replace_all_in_folder(self, needle: str, replacement: str) -> None:
        if not needle:
            return
        targets = self._iter_folder_caption_targets()
        nocase = True
        try:
            nocase = not bool(self.find_bar.case_var.get())
        except Exception:
            pass

        hits: list[tuple[str, Path, str, int]] = []
        pattern = (
            re.compile(re.escape(needle), re.IGNORECASE) if nocase else None
        )
        for media, cap in targets:
            try:
                if not cap.is_file():
                    continue
                text = cap.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                try:
                    text = cap.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if pattern is not None:
                new_text, n = pattern.subn(replacement, text)
            else:
                n = text.count(needle)
                new_text = text.replace(needle, replacement) if n else text
            if n:
                hits.append((media, cap, new_text, n))

        if not hits:
            self._flash_status("Replace All: no matches in folder")
            try:
                self.find_bar.count_label.configure(text="0 folder")
            except Exception:
                pass
            return

        total_repl = sum(h[3] for h in hits)
        try:
            parent = self.winfo_toplevel()
            ok = messagebox.askyesno(
                "Replace in folder",
                f"Replace {total_repl} occurrence(s) of\n  {needle!r}\n"
                f"in {len(hits)} caption file(s) in this folder?\n\n"
                f"This writes .txt sidecars directly.",
                parent=parent,
            )
        except Exception:
            ok = False
        if not ok:
            return

        written = 0
        errors = 0
        current_key = None
        if self._image_path:
            try:
                current_key = os.path.normcase(os.path.normpath(self._image_path))
            except Exception:
                current_key = self._image_path

        for media, cap, new_text, _n in hits:
            try:
                # If this is the open caption, prefer editor buffer (keeps dirty state coherent)
                same = False
                if current_key is not None:
                    try:
                        same = (
                            os.path.normcase(os.path.normpath(media)) == current_key
                        )
                    except Exception:
                        same = media == self._image_path
                if same:
                    try:
                        self.text.delete("1.0", "end")
                        self.text.insert("1.0", new_text)
                    except tk.TclError:
                        pass
                    if not self.save(silent=True):
                        errors += 1
                    else:
                        written += 1
                else:
                    cap.write_text(new_text, encoding="utf-8", newline="\n")
                    written += 1
            except OSError as exc:
                logging.warning("[CaptionEditor] Folder replace failed %s: %s", cap, exc)
                errors += 1

        msg = f"Replace All: {written} file(s), {total_repl} replacement(s)"
        if errors:
            msg += f" ({errors} error(s))"
        self._flash_status(msg)
        if self.find_bar.visible:
            self.find_bar.refresh_highlights()

    # --- internals ----------------------------------------------------------

    def _set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        try:
            self.text.configure(state=state)
        except tk.TclError:
            pass
        try:
            self.find_button.configure(state="normal" if enabled else "disabled")
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
