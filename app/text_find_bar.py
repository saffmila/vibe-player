"""
Inline find / replace bar for CTkTextbox (caption editor, prompt preview, …).

Owns in-document search, match highlighting, and optional replace UI.
Folder-wide navigation / replace is left to the host via callbacks.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk


def _tk_text(textbox):
    return getattr(textbox, "_textbox", textbox)


class TextFindReplaceBar(ctk.CTkFrame):
    """Compact find bar; pack/unpack via ``show`` / ``hide``."""

    MATCH_TAG = "find_match"
    CURRENT_TAG = "find_current"

    def __init__(
        self,
        parent,
        textbox,
        *,
        allow_replace: bool = True,
        scope_values: tuple[str, ...] | None = ("Current", "Folder"),
        on_after_current_exhausted: Callable[[str, bool], bool] | None = None,
        on_replace_all_folder: Callable[[str, str], None] | None = None,
        **kwargs,
    ):
        super().__init__(parent, fg_color=("gray90", "gray20"), **kwargs)
        self._textbox = textbox
        self._allow_replace = allow_replace
        self._on_after_current_exhausted = on_after_current_exhausted
        self._on_replace_all_folder = on_replace_all_folder
        self._visible = False
        self._replace_visible = False
        self._matches: list[str] = []  # text indices "line.col"
        self._match_index = -1
        self._tags_ready = False

        self.find_var = tk.StringVar()
        self.replace_var = tk.StringVar()
        self.case_var = tk.BooleanVar(value=False)
        self.scope_var = tk.StringVar(
            value=scope_values[0] if scope_values else "Current"
        )

        row1 = ctk.CTkFrame(self, fg_color="transparent")
        row1.pack(fill="x", padx=6, pady=(4, 2))

        ctk.CTkLabel(row1, text="Find", font=ctk.CTkFont(size=11), width=44).pack(
            side="left"
        )
        self.find_entry = ctk.CTkEntry(
            row1,
            textvariable=self.find_var,
            height=24,
            font=ctk.CTkFont(size=12),
            placeholder_text="Search text…",
        )
        self.find_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.case_check = ctk.CTkCheckBox(
            row1,
            text="Aa",
            variable=self.case_var,
            width=40,
            height=20,
            checkbox_width=16,
            checkbox_height=16,
            font=ctk.CTkFont(size=11),
            command=self._on_query_changed,
        )
        self.case_check.pack(side="left", padx=(0, 6))

        if scope_values:
            self.scope_switch = ctk.CTkSegmentedButton(
                row1,
                values=list(scope_values),
                variable=self.scope_var,
                width=140,
                height=22,
                font=ctk.CTkFont(size=10),
                command=lambda _v: self._on_query_changed(),
            )
            self.scope_switch.pack(side="left", padx=(0, 6))
        else:
            self.scope_switch = None

        self.count_label = ctk.CTkLabel(
            row1,
            text="",
            font=ctk.CTkFont(size=11),
            width=72,
            anchor="e",
            text_color=("gray40", "gray65"),
        )
        self.count_label.pack(side="left", padx=(0, 4))

        btn_kw = dict(width=28, height=24, font=ctk.CTkFont(size=12))
        ctk.CTkButton(row1, text="▲", command=self.find_prev, **btn_kw).pack(
            side="left", padx=(0, 2)
        )
        ctk.CTkButton(row1, text="▼", command=self.find_next, **btn_kw).pack(
            side="left", padx=(0, 4)
        )
        ctk.CTkButton(
            row1,
            text="×",
            width=28,
            height=24,
            font=ctk.CTkFont(size=14),
            command=self.hide,
        ).pack(side="left")

        self.row2 = ctk.CTkFrame(self, fg_color="transparent")
        # Packed only in replace mode

        ctk.CTkLabel(self.row2, text="Replace", font=ctk.CTkFont(size=11), width=44).pack(
            side="left"
        )
        self.replace_entry = ctk.CTkEntry(
            self.row2,
            textvariable=self.replace_var,
            height=24,
            font=ctk.CTkFont(size=12),
            placeholder_text="Replace with…",
        )
        self.replace_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        rbtn = dict(width=72, height=24, font=ctk.CTkFont(size=11))
        self.replace_btn = ctk.CTkButton(
            self.row2, text="Replace", command=self.replace_one, **rbtn
        )
        self.replace_btn.pack(side="left", padx=(0, 4))
        self.replace_all_btn = ctk.CTkButton(
            self.row2, text="All", command=self.replace_all, width=48, height=24,
            font=ctk.CTkFont(size=11),
        )
        self.replace_all_btn.pack(side="left")

        self.find_var.trace_add("write", lambda *_: self._on_query_changed())
        self.case_var.trace_add("write", lambda *_: self._on_query_changed())

        def _bind_entry(entry):
            widgets = [entry, getattr(entry, "_entry", None)]
            seen: set[int] = set()
            for w in widgets:
                if w is None or id(w) in seen:
                    continue
                seen.add(id(w))
                w.bind("<Return>", lambda e: self.find_next())
                w.bind("<Shift-Return>", lambda e: self.find_prev())
                w.bind("<Escape>", lambda e: self.hide())
                w.bind("<F3>", lambda e: self.find_next())
                w.bind("<Shift-F3>", lambda e: self.find_prev())
                w.bind("<Control-f>", self._on_ctrl_f_in_bar)
                w.bind("<Control-F>", self._on_ctrl_f_in_bar)
                if allow_replace:
                    w.bind("<Control-h>", self._on_ctrl_h_in_bar)
                    w.bind("<Control-H>", self._on_ctrl_h_in_bar)

        _bind_entry(self.find_entry)
        _bind_entry(self.replace_entry)

    # --- public API ---------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def scope(self) -> str:
        try:
            return str(self.scope_var.get() or "Current")
        except Exception:
            return "Current"

    def show(self, *, replace: bool = False, prefill: str | None = None) -> None:
        self._ensure_tags()
        if prefill is not None:
            self.find_var.set(prefill)
        elif not self.find_var.get():
            sel = self._selected_text()
            if sel and "\n" not in sel and len(sel) < 200:
                self.find_var.set(sel)

        if not self._visible:
            self.pack(side="top", fill="x", padx=6, pady=(0, 2), before=self._textbox)
            self._visible = True

        self._set_replace_visible(bool(replace and self._allow_replace))
        self._on_query_changed()
        try:
            self.find_entry.focus_set()
            self.find_entry.select_range(0, "end")
        except tk.TclError:
            pass

    def hide(self) -> None:
        self._clear_highlights()
        self._matches = []
        self._match_index = -1
        self._update_count_label()
        if self._visible:
            try:
                self.pack_forget()
            except tk.TclError:
                pass
            self._visible = False
        self._set_replace_visible(False)
        try:
            self._textbox.focus_set()
        except tk.TclError:
            pass

    def find_next(self, _event=None):
        self._find_step(+1)
        return "break"

    def find_prev(self, _event=None):
        self._find_step(-1)
        return "break"

    def replace_one(self, _event=None):
        if not self._allow_replace:
            return "break"
        needle = self.find_var.get()
        if not needle:
            return "break"
        tb = _tk_text(self._textbox)
        try:
            if self._match_index < 0 or self._match_index >= len(self._matches):
                self._find_step(+1)
            if self._match_index < 0 or self._match_index >= len(self._matches):
                return "break"
            start = self._matches[self._match_index]
            end = f"{start}+{len(needle)}c"
            tb.delete(start, end)
            replacement = self.replace_var.get()
            if replacement:
                tb.insert(start, replacement)
            # Re-scan from just after the replacement
            self._collect_matches()
            # Stay near the edit point
            self._select_nearest_match(start, direction=+1)
        except tk.TclError:
            pass
        return "break"

    def replace_all(self, _event=None):
        if not self._allow_replace:
            return "break"
        needle = self.find_var.get()
        if not needle:
            return "break"
        if self.scope == "Folder" and self._on_replace_all_folder is not None:
            self._on_replace_all_folder(needle, self.replace_var.get())
            return "break"
        n = self._replace_all_in_current()
        self._collect_matches()
        self._update_count_label(extra=f" · replaced {n}")
        return "break"

    def refresh_highlights(self) -> None:
        """Re-run match scan (e.g. after host loads another caption)."""
        if self._visible:
            self._on_query_changed()

    def bind_to_textbox(self) -> None:
        """Wire Ctrl+F / Ctrl+H / F3 on the host textbox."""
        widgets = [self._textbox, _tk_text(self._textbox)]
        seen: set[int] = set()
        for w in widgets:
            if w is None:
                continue
            wid = id(w)
            if wid in seen:
                continue
            seen.add(wid)
            w.bind("<Control-f>", self._on_ctrl_f_text)
            w.bind("<Control-F>", self._on_ctrl_f_text)
            if self._allow_replace:
                w.bind("<Control-h>", self._on_ctrl_h_text)
                w.bind("<Control-H>", self._on_ctrl_h_text)
            w.bind("<F3>", self.find_next)
            w.bind("<Shift-F3>", self.find_prev)
            w.bind("<Escape>", self._on_escape_text)

    # --- internals ----------------------------------------------------------

    def _set_replace_visible(self, visible: bool) -> None:
        self._replace_visible = visible and self._allow_replace
        try:
            if self._replace_visible:
                if not self.row2.winfo_ismapped():
                    self.row2.pack(fill="x", padx=6, pady=(0, 4))
            else:
                self.row2.pack_forget()
        except tk.TclError:
            pass

    def _on_ctrl_f_text(self, _event=None):
        self.show(replace=False)
        return "break"

    def _on_ctrl_h_text(self, _event=None):
        self.show(replace=True)
        return "break"

    def _on_ctrl_f_in_bar(self, _event=None):
        self.show(replace=self._replace_visible)
        return "break"

    def _on_ctrl_h_in_bar(self, _event=None):
        self.show(replace=True)
        return "break"

    def _on_escape_text(self, _event=None):
        if self._visible:
            self.hide()
            return "break"
        return None

    def _ensure_tags(self) -> None:
        if self._tags_ready:
            return
        tb = _tk_text(self._textbox)
        try:
            tb.tag_configure(self.MATCH_TAG, background="#5c4a1f")
            tb.tag_configure(self.CURRENT_TAG, background="#c9a227", foreground="#1a1a1a")
            # Keep selection-like visibility above other formatting tags
            try:
                tb.tag_raise(self.MATCH_TAG)
                tb.tag_raise(self.CURRENT_TAG)
            except tk.TclError:
                pass
            self._tags_ready = True
        except tk.TclError:
            pass

    def _selected_text(self) -> str:
        tb = _tk_text(self._textbox)
        try:
            return tb.get("sel.first", "sel.last")
        except tk.TclError:
            return ""

    def _on_query_changed(self, *_args) -> None:
        if not self._visible:
            return
        self._collect_matches()
        if self._matches:
            # Prefer match at/after insert
            self._select_nearest_match(None, direction=+1)
        else:
            self._match_index = -1
            self._clear_highlights()
        self._update_count_label()

    def _nocase(self) -> bool:
        return not bool(self.case_var.get())

    def _collect_matches(self) -> None:
        self._ensure_tags()
        needle = self.find_var.get()
        tb = _tk_text(self._textbox)
        self._clear_highlights()
        self._matches = []
        if not needle:
            self._match_index = -1
            return
        start = "1.0"
        while True:
            try:
                pos = tb.search(needle, start, stopindex="end", nocase=self._nocase())
            except tk.TclError:
                break
            if not pos:
                break
            self._matches.append(pos)
            try:
                tb.tag_add(self.MATCH_TAG, pos, f"{pos}+{len(needle)}c")
            except tk.TclError:
                pass
            start = f"{pos}+{len(needle)}c"
        try:
            tb.tag_raise(self.MATCH_TAG)
            tb.tag_raise(self.CURRENT_TAG)
        except tk.TclError:
            pass

    def _clear_highlights(self) -> None:
        tb = _tk_text(self._textbox)
        try:
            tb.tag_remove(self.MATCH_TAG, "1.0", "end")
            tb.tag_remove(self.CURRENT_TAG, "1.0", "end")
        except tk.TclError:
            pass

    def _update_count_label(self, extra: str = "") -> None:
        needle = self.find_var.get()
        if not needle:
            self.count_label.configure(text="")
            return
        total = len(self._matches)
        if total == 0:
            self.count_label.configure(text=f"0/0{extra}")
        else:
            cur = self._match_index + 1 if self._match_index >= 0 else 0
            self.count_label.configure(text=f"{cur}/{total}{extra}")

    def _activate_match(self, index: int) -> None:
        needle = self.find_var.get()
        if not needle or index < 0 or index >= len(self._matches):
            return
        tb = _tk_text(self._textbox)
        self._match_index = index
        try:
            tb.tag_remove(self.CURRENT_TAG, "1.0", "end")
            start = self._matches[index]
            end = f"{start}+{len(needle)}c"
            tb.tag_add(self.CURRENT_TAG, start, end)
            tb.mark_set("insert", end)
            tb.see(start)
            # Also set selection for easy overwrite / copy
            tb.tag_remove("sel", "1.0", "end")
            tb.tag_add("sel", start, end)
        except tk.TclError:
            pass
        self._update_count_label()

    def _select_nearest_match(self, from_index: str | None, *, direction: int) -> None:
        if not self._matches:
            self._match_index = -1
            return
        tb = _tk_text(self._textbox)
        try:
            origin = from_index or tb.index("insert")
        except tk.TclError:
            origin = "1.0"
        best = 0
        if direction >= 0:
            best = 0
            for i, pos in enumerate(self._matches):
                try:
                    if tb.compare(pos, ">=", origin):
                        best = i
                        break
                except tk.TclError:
                    continue
            else:
                best = 0
        else:
            best = len(self._matches) - 1
            for i in range(len(self._matches) - 1, -1, -1):
                pos = self._matches[i]
                try:
                    if tb.compare(pos, "<", origin):
                        best = i
                        break
                except tk.TclError:
                    continue
        self._activate_match(best)

    def _find_step(self, direction: int) -> None:
        needle = self.find_var.get()
        if not needle:
            return
        if not self._visible:
            self.show(replace=self._replace_visible)

        # Refresh in case text changed outside the bar
        prev_idx = self._match_index
        self._collect_matches()

        if self._matches:
            if prev_idx < 0:
                self._select_nearest_match(None, direction=direction)
            else:
                nxt = prev_idx + direction
                wrapped = nxt < 0 or nxt >= len(self._matches)
                if wrapped and self.scope == "Folder" and self._on_after_current_exhausted:
                    # Let host jump to another file; if it returns True, done
                    if self._on_after_current_exhausted(needle, direction > 0):
                        return
                if not self._matches:
                    return
                nxt = nxt % len(self._matches)
                self._activate_match(nxt)
            return

        # No matches in current document
        if self.scope == "Folder" and self._on_after_current_exhausted:
            if self._on_after_current_exhausted(needle, direction > 0):
                return
        self._match_index = -1
        self._update_count_label()

    def _replace_all_in_current(self) -> int:
        needle = self.find_var.get()
        if not needle:
            return 0
        replacement = self.replace_var.get()
        tb = _tk_text(self._textbox)
        try:
            content = tb.get("1.0", "end-1c")
        except tk.TclError:
            return 0
        if self._nocase():
            # Case-insensitive replace preserving original length segments
            import re

            pattern = re.compile(re.escape(needle), re.IGNORECASE)
            new_content, n = pattern.subn(replacement, content)
        else:
            n = content.count(needle)
            new_content = content.replace(needle, replacement) if n else content
        if n == 0:
            return 0
        try:
            tb.delete("1.0", "end")
            tb.insert("1.0", new_content)
        except tk.TclError:
            return 0
        return n
