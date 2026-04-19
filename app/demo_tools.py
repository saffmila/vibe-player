"""
Optional on-screen demo toasts for recordings. Loads copy from ``demo_texts.json``;
safe to delete this module — the main app uses a guarded import.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import customtkinter as ctk


class DemoNotifier:
    """Temporary bottom toast; each key shows at most once by default."""

    def __init__(self, parent, json_path: Path | str | None = None) -> None:
        self.parent = parent
        self.json_path = (
            Path(json_path) if json_path is not None else Path(__file__).resolve().parent / "demo_texts.json"
        )
        self.texts = self._load_texts()
        self.enabled = bool(self.texts)
        self._shown: set[str] = set()
        self._toast_widget: ctk.CTkLabel | None = None
        self._toast_after_id: str | None = None

    def _load_texts(self) -> dict[str, str]:
        if not self.json_path.is_file():
            return {}
        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            logging.debug("DemoNotifier: could not load %s: %s", self.json_path, e)
        return {}

    def _clear_toast(self) -> None:
        if self._toast_after_id is not None:
            try:
                self.parent.after_cancel(self._toast_after_id)
            except Exception:
                pass
            self._toast_after_id = None
        if self._toast_widget is not None:
            try:
                self._toast_widget.destroy()
            except Exception:
                pass
            self._toast_widget = None

    def _on_toast_expire(self) -> None:
        self._toast_after_id = None
        w = self._toast_widget
        self._toast_widget = None
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass

    def show(self, text_key: str, duration: int = 5000, once: bool = True) -> None:
        if not self.enabled:
            return
        if once and text_key in self._shown:
            return
        message = self.texts.get(text_key)
        if not message:
            return
        self._clear_toast()
        if once:
            self._shown.add(text_key)

        notification = ctk.CTkLabel(
            self.parent,
            text=message,
            fg_color=("#3B8ED0", "#1f538d"),
            text_color="white",
            corner_radius=0,
            padx=25,
            pady=12,
            font=ctk.CTkFont(family="Segoe UI", size=30, weight="bold"),
        )
        notification.place(relx=0.5, rely=0.85, anchor="center")
        notification.lift()
        self._toast_widget = notification
        self._toast_after_id = self.parent.after(duration, self._on_toast_expire)
