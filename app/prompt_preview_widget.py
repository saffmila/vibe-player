"""
Read-only preview of embedded ComfyUI / A1111 prompts for the bottom panel.

Auto-load is idle-debounced so selection / playback stay responsive.
Optional Compare (exactly 2 selected media) shows a unified prompt diff.
"""

from __future__ import annotations

import os
import re
import threading
import tkinter as tk

import customtkinter as ctk

from caption_editor_widget import is_captionable_path
from text_find_bar import TextFindReplaceBar
from comfyui_prompt_extractor import (
    PromptInfo,
    build_unified_prompt_diff,
    color_for_ref_token,
    extract_prompt,
    iter_ref_token_spans,
    prompt_text_for_diff,
)

_SELECT_HINT = "Select an image or video to preview its embedded prompt"
_MANUAL_HINT = "Auto load is off — click Load prompt"
_COMPARE_HINT = "Select exactly 2 files, then Compare"
_IDLE_DEBOUNCE_MS = 450
_BUSY_RETRY_MS = 250

# Whole-line section headings in Minimax-style prompts (subject_definitions:, summary:, …)
_SECTION_TITLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*:\s*$")
_META_LINE_RE = re.compile(r"^(Model|Source):\s")

_DIFF_COLORS = {
    "header": ("#9ca3af", None),
    "hunk": ("#60a5fa", None),
    "equal": (None, None),
    "del": ("#fca5a5", "#3f1d1d"),
    "add": ("#86efac", "#14532d"),
    "meta": ("#fbbf24", None),
    # Word/char-level highlights inside changed lines (brighter than line bg)
    "del_word": ("#ffffff", "#b91c1c"),
    "add_word": ("#ffffff", "#15803d"),
}


class PromptPreviewWidget(ctk.CTkFrame):
    """Bottom-panel prompt viewer (Timeline | Captions | Prompt)."""

    def __init__(self, parent, controller=None, **kwargs):
        super().__init__(parent, fg_color="transparent", **kwargs)
        self.controller = controller
        self._file_path: str | None = None
        self._prompt_info: PromptInfo | None = None
        self._req_id: int = 0
        self._idle_after_id: str | None = None
        self._ref_tag_names: list[str] = []
        self._loaded_for: str | None = None
        self._compare_paths: list[str] = []
        self._in_compare: bool = False
        self._diff_tags_ready = False

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

        self.compare_button = ctk.CTkButton(
            toolbar,
            text="Compare",
            width=80,
            height=24,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.compare_now,
        )
        # Packed only when exactly 2 captionable files are selected

        self.load_button = ctk.CTkButton(
            toolbar,
            text="Load prompt",
            width=96,
            height=24,
            font=ctk.CTkFont(size=11),
            command=self.load_now,
            state="disabled",
        )
        # Packed only when Auto load is off (see sync_autoload_chrome)

        self.find_button = ctk.CTkButton(
            toolbar,
            text="Find",
            width=52,
            height=24,
            font=ctk.CTkFont(size=11),
            command=lambda: self.open_find(),
        )
        self.find_button.pack(side="right", padx=(0, 6))

        self.text = ctk.CTkTextbox(
            self,
            font=ctk.CTkFont(family="Consolas", size=13),
            wrap="word",
            activate_scrollbars=True,
        )
        self.text.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.text.bind("<Control-a>", self._on_ctrl_a)
        self.text.bind("<Control-A>", self._on_ctrl_a)

        # Find-only (prompts are read-only); scope stays on the loaded prompt text
        self.find_bar = TextFindReplaceBar(
            self,
            self.text,
            allow_replace=False,
            scope_values=None,
        )
        self.find_bar.bind_to_textbox()

        self._bind_readonly()
        self._ensure_diff_tags()
        self.sync_toolbar_chrome()

    # --- public API ---------------------------------------------------------

    def autoload_enabled(self) -> bool:
        ctrl = self.controller
        if ctrl is None:
            return True
        var = getattr(ctrl, "prompt_autoload_var", None)
        if var is None:
            return True
        try:
            return bool(var.get())
        except Exception:
            return True

    def sync_toolbar_chrome(self) -> None:
        """Show/hide Load + Compare; Compare is rightmost when visible."""
        auto = self.autoload_enabled()
        can_compare = len(self._compare_paths) == 2
        try:
            self.load_button.pack_forget()
            self.compare_button.pack_forget()
            # Pack right-to-left: Compare outermost, then Load
            if can_compare:
                self.compare_button.pack(side="right")
                self.compare_button.configure(state="normal")
            if not auto:
                padx = (0, 6) if can_compare else 0
                self.load_button.pack(side="right", padx=padx)
                state = "normal" if is_captionable_path(self._file_path) else "disabled"
                self.load_button.configure(state=state)
        except tk.TclError:
            pass

    def sync_autoload_chrome(self) -> None:
        self.sync_toolbar_chrome()

    def sync_compare_chrome(self) -> None:
        self.sync_toolbar_chrome()

    def on_selection(
        self,
        file_path: str | None,
        *,
        selected_paths: list[str] | None = None,
        force: bool = False,
    ) -> None:
        """
        React to thumbnail selection while Prompt mode is active.

        With Auto load: idle-debounced extract for the primary path.
        ``selected_paths`` (captionable multi-select) drives Compare visibility.
        """
        self._cancel_idle()
        self._req_id += 1  # invalidate in-flight extract / compare
        self._in_compare = False

        # Dedupe captionable paths, preserve order
        paths: list[str] = []
        seen: set[str] = set()
        for p in selected_paths or ([] if not file_path else [file_path]):
            if not is_captionable_path(p):
                continue
            try:
                key = os.path.normcase(os.path.normpath(p))
            except Exception:
                key = p
            if key in seen:
                continue
            seen.add(key)
            paths.append(p)
        self._compare_paths = paths if len(paths) == 2 else []

        if not is_captionable_path(file_path):
            if self._compare_paths:
                # Multi-select edge case: primary not captionable but 2 others are
                self._file_path = self._compare_paths[-1]
                self.sync_toolbar_chrome()
                self._set_status(_COMPARE_HINT)
                self._show_placeholder(_COMPARE_HINT)
                return
            self.clear(message=_SELECT_HINT)
            return

        self._file_path = file_path
        self.sync_toolbar_chrome()

        if len(paths) > 2:
            # Still preview primary; Compare stays hidden
            pass

        if force or self.autoload_enabled():
            if force:
                self._set_status(f"Loading — {os.path.basename(file_path)}")
                self._start_extract(file_path)
            else:
                hint = (
                    f"Waiting — {os.path.basename(file_path)} (2 selected → Compare)"
                    if self._compare_paths
                    else f"Waiting — {os.path.basename(file_path)}"
                )
                self._set_status(hint)
                self._show_placeholder("Waiting for idle…")
                self._schedule_idle_load(file_path)
        else:
            if self._loaded_for != file_path:
                self._prompt_info = None
                self._loaded_for = None
                self._show_placeholder(_MANUAL_HINT)
            status = f"{os.path.basename(file_path)} — click Load prompt"
            if self._compare_paths:
                status += " · or Compare"
            self._set_status(status)
            try:
                self.load_button.configure(state="normal")
            except tk.TclError:
                pass

    def load_now(self) -> None:
        """Manual Load / Refresh for the current file."""
        if not is_captionable_path(self._file_path):
            return
        self._cancel_idle()
        self._in_compare = False
        self._req_id += 1
        self._set_status(f"Loading — {os.path.basename(self._file_path)}")
        self._start_extract(self._file_path)

    def compare_now(self) -> None:
        """Extract prompts for the two selected files and show a unified diff."""
        if len(self._compare_paths) != 2:
            self._set_status(_COMPARE_HINT)
            return
        path_a, path_b = self._compare_paths
        self._cancel_idle()
        self._req_id += 1
        req_id = self._req_id
        self._in_compare = True
        self._set_status(
            f"Comparing — {os.path.basename(path_a)} → {os.path.basename(path_b)}"
        )
        self._show_placeholder("Loading prompts for compare…")

        def worker():
            info_a = extract_prompt(path_a)
            info_b = extract_prompt(path_b)
            try:
                self.after(
                    0,
                    lambda: self._apply_compare(req_id, path_a, path_b, info_a, info_b),
                )
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True, name="prompt-compare").start()

    def on_autoload_toggled(self) -> None:
        """Header checkbox changed — refresh chrome and maybe kick a load."""
        self.sync_toolbar_chrome()
        if self._in_compare:
            return
        if not is_captionable_path(self._file_path):
            return
        if self.autoload_enabled():
            self.on_selection(
                self._file_path,
                selected_paths=self._compare_paths or [self._file_path],
            )
        else:
            self._cancel_idle()
            if self._loaded_for != self._file_path:
                self._show_placeholder(_MANUAL_HINT)
                self._set_status(
                    f"{os.path.basename(self._file_path)} — click Load prompt"
                )

    def open_find(self) -> bool:
        """Open in-prompt find bar (Ctrl+F while focused here)."""
        self.find_bar.show(replace=False)
        return True

    def clear(self, message: str = _SELECT_HINT) -> None:
        self._cancel_idle()
        self._req_id += 1
        self._file_path = None
        self._prompt_info = None
        self._loaded_for = None
        self._compare_paths = []
        self._in_compare = False
        self._show_placeholder("")
        self._set_status(message)
        self.sync_toolbar_chrome()

    # --- idle / extract -----------------------------------------------------

    def _schedule_idle_load(self, file_path: str) -> None:
        self._cancel_idle()

        def _tick():
            self._idle_after_id = None
            if self._file_path != file_path or self._in_compare:
                return
            if self._player_busy():
                self._set_status(f"Waiting (busy) — {os.path.basename(file_path)}")
                try:
                    self._idle_after_id = self.after(_BUSY_RETRY_MS, _tick)
                except Exception:
                    pass
                return
            self._start_extract(file_path)

        try:
            self._idle_after_id = self.after(_IDLE_DEBOUNCE_MS, _tick)
        except Exception:
            _tick()

    def _cancel_idle(self) -> None:
        aid = self._idle_after_id
        self._idle_after_id = None
        if aid is not None:
            try:
                self.after_cancel(aid)
            except Exception:
                pass

    def _player_busy(self) -> bool:
        """True while the player is doing interactive work we should not compete with."""
        ctrl = self.controller
        if ctrl is None:
            return False
        tw = getattr(ctrl, "timeline_widget", None)
        if tw is not None:
            if getattr(tw, "_bookmark_drag_marker", None) is not None:
                return True
            if getattr(tw, "_scrubbing", False) or getattr(tw, "is_scrubbing", False):
                return True
        if getattr(ctrl, "_loading_thumbnails", False):
            return True
        if getattr(ctrl, "_folder_loading", False):
            return True
        return False

    def _start_extract(self, file_path: str) -> None:
        self._req_id += 1
        req_id = self._req_id
        self._in_compare = False
        self._show_placeholder("Loading prompt…")

        def worker():
            info = extract_prompt(file_path)
            try:
                self.after(0, lambda: self._apply_result(req_id, file_path, info))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True, name="prompt-extract").start()

    def _apply_result(self, req_id: int, file_path: str, info: PromptInfo) -> None:
        if req_id != self._req_id or self._in_compare:
            return
        if self._file_path != file_path:
            return
        self._prompt_info = info
        self._loaded_for = file_path
        body = info.display_text()
        self._set_text(body)
        self._apply_structure_formatting(body)
        self._apply_ref_colors(body)
        if getattr(self, "find_bar", None) is not None and self.find_bar.visible:
            try:
                self.find_bar.refresh_highlights()
            except Exception:
                pass
        if info.found:
            extra = f" · {info.model}" if info.model else ""
            status = f"{os.path.basename(file_path)}{extra}"
        else:
            status = f"{os.path.basename(file_path)} (not found)"
        if self._compare_paths:
            status += " · Compare ready"
        self._set_status(status)
        self.sync_toolbar_chrome()

    def _apply_compare(
        self,
        req_id: int,
        path_a: str,
        path_b: str,
        info_a: PromptInfo,
        info_b: PromptInfo,
    ) -> None:
        if req_id != self._req_id:
            return
        if self._compare_paths != [path_a, path_b]:
            return
        self._in_compare = True

        name_a = os.path.basename(path_a)
        name_b = os.path.basename(path_b)
        text_a = prompt_text_for_diff(info_a)
        text_b = prompt_text_for_diff(info_b)

        if not info_a.found and not info_b.found:
            self._show_placeholder("No embedded prompt found in either file.")
            self._set_status(f"Compare — {name_a} vs {name_b} (none found)")
            return
        if not info_a.found:
            self._show_placeholder(f"No prompt in {name_a}.\n\nShowing {name_b} only is not a diff — both need prompts.")
            self._set_status(f"Compare — missing prompt in {name_a}")
            return
        if not info_b.found:
            self._show_placeholder(f"No prompt in {name_b}.\n\nShowing {name_a} only is not a diff — both need prompts.")
            self._set_status(f"Compare — missing prompt in {name_b}")
            return

        rows = build_unified_prompt_diff(text_a, text_b, name_a=name_a, name_b=name_b)
        self._render_diff_rows(rows)
        if len(rows) == 1 and rows[0][0] == "meta":
            self._set_status(f"Compare — {name_a} ≡ {name_b}")
        else:
            n_add = sum(1 for k, *_ in rows if k == "add")
            n_del = sum(1 for k, *_ in rows if k == "del")
            self._set_status(f"Compare — {name_a} → {name_b}  (−{n_del} / +{n_add})")

    # --- text helpers -------------------------------------------------------

    def _tk_text(self):
        return getattr(self.text, "_textbox", self.text)

    def _ensure_diff_tags(self) -> None:
        if self._diff_tags_ready:
            return
        widget = self._tk_text()
        for kind, (fg, bg) in _DIFF_COLORS.items():
            opts = {}
            if fg:
                opts["foreground"] = fg
            if bg:
                opts["background"] = bg
            if not opts:
                continue
            try:
                widget.tag_config(f"diff_{kind}", **opts)
            except tk.TclError:
                pass
        # Word highlights above line backgrounds
        try:
            widget.tag_raise("diff_del_word")
            widget.tag_raise("diff_add_word")
        except tk.TclError:
            pass
        self._diff_tags_ready = True

    def _render_diff_rows(
        self, rows: list[tuple[str, str, list[tuple[int, int]]]] | list[tuple]
    ) -> None:
        self._ensure_diff_tags()
        self._clear_ref_tags()
        widget = self._tk_text()
        try:
            self.text.configure(state="normal")
        except tk.TclError:
            pass
        try:
            self.text.delete("1.0", "end")
        except tk.TclError:
            try:
                widget.delete("1.0", "end")
            except tk.TclError:
                return

        for row in rows:
            if len(row) >= 3:
                kind, line, spans = row[0], row[1], row[2]
            else:
                kind, line = row[0], row[1]
                spans = []
            try:
                start = widget.index("end-1c")
                widget.insert("end", line + "\n")
                end = widget.index("end-1c")
                if kind in _DIFF_COLORS:
                    widget.tag_add(f"diff_{kind}", start, end)
                # Inline word/char highlights (offsets into the inserted line)
                word_tag = (
                    "diff_del_word"
                    if kind == "del"
                    else "diff_add_word"
                    if kind == "add"
                    else None
                )
                if word_tag and spans:
                    for s, e in spans:
                        if e <= s:
                            continue
                        try:
                            widget.tag_add(
                                word_tag,
                                f"{start}+{s}c",
                                f"{start}+{e}c",
                            )
                        except tk.TclError:
                            pass
            except tk.TclError:
                pass

    def _show_placeholder(self, text: str) -> None:
        self._clear_ref_tags()
        self._set_text(text)

    def _set_text(self, text: str) -> None:
        widget = self._tk_text()
        try:
            self.text.configure(state="normal")
        except tk.TclError:
            pass
        try:
            self.text.delete("1.0", "end")
            if text:
                self.text.insert("1.0", text)
        except tk.TclError:
            try:
                widget.delete("1.0", "end")
                if text:
                    widget.insert("1.0", text)
            except tk.TclError:
                pass
        if getattr(self, "find_bar", None) is not None and self.find_bar.visible:
            try:
                self.find_bar.refresh_highlights()
            except Exception:
                pass

    def _set_status(self, text: str) -> None:
        try:
            self.status_label.configure(text=text)
        except tk.TclError:
            pass

    def _clear_ref_tags(self) -> None:
        widget = self._tk_text()
        for name in self._ref_tag_names:
            try:
                widget.tag_delete(name)
            except tk.TclError:
                try:
                    widget.tag_remove(name, "1.0", "end")
                except tk.TclError:
                    pass
        self._ref_tag_names.clear()
        for name in ("fmt_meta", "fmt_section", "fmt_neg"):
            try:
                widget.tag_remove(name, "1.0", "end")
            except tk.TclError:
                pass

    def _ensure_structure_tags(self) -> None:
        widget = self._tk_text()
        # Native Tk font tuples — reliable on CTkTextbox's underlying Text
        try:
            widget.tag_config(
                "fmt_meta",
                font=("Consolas", 13, "bold"),
                foreground="#e5e7eb",
            )
            widget.tag_config(
                "fmt_section",
                font=("Consolas", 13, "bold italic"),
                foreground="#93c5fd",
            )
            widget.tag_config(
                "fmt_neg",
                font=("Consolas", 13, "bold"),
                foreground="#fbbf24",
            )
        except tk.TclError:
            try:
                widget.tag_config("fmt_meta", font=("Consolas", 13, "bold"))
                widget.tag_config("fmt_section", font=("Consolas", 13, "bold", "italic"))
                widget.tag_config("fmt_neg", font=("Consolas", 13, "bold"))
            except tk.TclError:
                pass

    def _apply_structure_formatting(self, text: str) -> None:
        """Bold Model/Source header; bold-italic for snake_case section titles."""
        if not text:
            return
        self._ensure_structure_tags()
        widget = self._tk_text()
        offset = 0
        for line in text.splitlines(keepends=True):
            raw = line.rstrip("\r\n")
            start = offset
            end = offset + len(raw)
            offset += len(line)
            if not raw.strip():
                continue
            stripped = raw.strip()
            tag = None
            if _META_LINE_RE.match(stripped):
                tag = "fmt_meta"
            elif stripped == "--- Negative ---":
                tag = "fmt_neg"
            elif _SECTION_TITLE_RE.match(stripped):
                tag = "fmt_section"
            if tag is None:
                continue
            try:
                widget.tag_add(tag, f"1.0+{start}c", f"1.0+{end}c")
            except tk.TclError:
                pass

    def _apply_ref_colors(self, text: str) -> None:
        spans = iter_ref_token_spans(text)
        if not spans:
            return
        widget = self._tk_text()
        token_tags: dict[str, str] = {}
        for start, end, canonical in spans:
            tag = token_tags.get(canonical)
            if tag is None:
                tag = f"ref_{len(token_tags)}"
                token_tags[canonical] = tag
                color = color_for_ref_token(canonical)
                try:
                    widget.tag_config(tag, foreground=color)
                except tk.TclError:
                    continue
                self._ref_tag_names.append(tag)
            try:
                widget.tag_add(tag, f"1.0+{start}c", f"1.0+{end}c")
            except tk.TclError:
                pass

    def _bind_readonly(self) -> None:
        widget = self._tk_text()

        def _block_edit(event):
            if getattr(event, "state", 0) & 0x4:
                # Allow copy / select-all / find (Ctrl+F). Replace is disabled.
                if str(getattr(event, "keysym", "")).lower() in ("c", "a", "f"):
                    return None
                return "break"
            key = str(getattr(event, "keysym", ""))
            if key in (
                "Left",
                "Right",
                "Up",
                "Down",
                "Home",
                "End",
                "Prior",
                "Next",
                "F3",
                "Shift_L",
                "Shift_R",
                "Control_L",
                "Control_R",
                "Alt_L",
                "Alt_R",
                "Caps_Lock",
                "Escape",
                "App",
                "Tab",
            ):
                return None
            return "break"

        def _block(_event=None):
            return "break"

        try:
            widget.bind("<Key>", _block_edit)
            self.text.bind("<Key>", _block_edit)
        except tk.TclError:
            pass
        for seq in ("<<Paste>>", "<<Cut>>"):
            try:
                widget.bind(seq, _block)
                self.text.bind(seq, _block)
            except tk.TclError:
                pass

    def _on_ctrl_a(self, _event=None):
        try:
            self.text.tag_add("sel", "1.0", "end-1c")
            self.text.mark_set("insert", "1.0")
            self.text.see("insert")
        except tk.TclError:
            pass
        return "break"
