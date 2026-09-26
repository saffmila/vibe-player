"""
rife_dialog.py — Compact options dialog for offline RIFE interpolation.
"""

from __future__ import annotations

import os
import threading
from tkinter import filedialog, messagebox

import customtkinter as ctk

from promo_banner import PROMO_STRIP_DIALOG_W, attach_promo_strip, sync_promo_strip
from rife_config import PACK_MISSING_MESSAGE, default_rife_dir, runtime_status
from video_encode_settings import RIFE_MODE_LABELS, RIFE_MULT_LABELS

# Match promo strip comfort width so the banner isn't squeezed.
_RIFE_DIALOG_W = PROMO_STRIP_DIALOG_W
_RIFE_DIALOG_H = 520

_STATUS_BG = ("gray86", "#1e1e1e")
_UI_BTN_H = 28


class RifeOptionsDialog(ctk.CTkToplevel):
    """Ask for multiplier / mode / output path, then call on_confirm(paths, options)."""

    def __init__(self, parent, paths: list[str], on_confirm, controller=None):
        super().__init__(parent)
        self.title("RIFE Interpolate")
        self.paths = list(paths or [])
        self.on_confirm = on_confirm
        self.controller = controller
        self.result = None
        self._pack_ready = False
        self._install_running = False

        self.geometry(f"{_RIFE_DIALOG_W}x{_RIFE_DIALOG_H}")
        self.minsize(_RIFE_DIALOG_W, 460)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.mult_var = ctk.StringVar(value="2×")
        self.mode_var = ctk.StringVar(value=RIFE_MODE_LABELS[0])
        self.audio_var = ctk.BooleanVar(value=True)
        default_out = os.path.dirname(self.paths[0]) if self.paths else os.getcwd()
        self.out_dir_var = ctk.StringVar(value=default_out)
        self.status_var = ctk.StringVar(value="Checking…")

        attach_promo_strip(
            self,
            "strip_rife.png",
            dialog_width=_RIFE_DIALOG_W,
            controller=self.controller,
        )

        ctk.CTkLabel(
            self,
            text="RIFE frame interpolation",
            text_color="#00bfff",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(10, 4))

        # Status + install tools (SeedVR / BiRefNet style)
        status_wrap = ctk.CTkFrame(self, fg_color="transparent")
        status_wrap.pack(fill="x", padx=16, pady=(0, 10))

        self._status_box = ctk.CTkFrame(
            status_wrap,
            fg_color=_STATUS_BG,
            corner_radius=6,
            border_width=1,
            border_color=("gray70", "#1a1a1a"),
        )
        self._status_box.pack(fill="x")
        ctk.CTkLabel(
            self._status_box,
            textvariable=self.status_var,
            wraplength=_RIFE_DIALOG_W - 60,
            justify="left",
            text_color="#b0b0b0",
            anchor="w",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=10, pady=10)

        tools_row = ctk.CTkFrame(status_wrap, fg_color="transparent")
        tools_row.pack(fill="x", pady=(8, 0))
        self._install_btn = ctk.CTkButton(
            tools_row,
            text="Install RIFE pack…",
            width=140,
            height=_UI_BTN_H,
            command=self._install_pack,
        )
        self._install_btn.pack(side="left")
        self._open_btn = ctk.CTkButton(
            tools_row,
            text="Open folder",
            width=100,
            height=_UI_BTN_H,
            fg_color="gray30",
            hover_color="gray25",
            command=self._open_rife_folder,
        )
        self._open_btn.pack(side="left", padx=(8, 0))
        self._refresh_btn = ctk.CTkButton(
            tools_row,
            text="Refresh status",
            width=110,
            height=_UI_BTN_H,
            fg_color="gray30",
            hover_color="gray25",
            command=self._refresh_status,
        )
        self._refresh_btn.pack(side="left", padx=(8, 0))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        row1 = ctk.CTkFrame(body, fg_color="transparent")
        row1.pack(fill="x", pady=4)
        ctk.CTkLabel(row1, text="Multiplier:", width=100, anchor="w").pack(side="left")
        self._mult_menu = ctk.CTkOptionMenu(
            row1, variable=self.mult_var, values=list(RIFE_MULT_LABELS), height=28
        )
        self._mult_menu.pack(side="left", fill="x", expand=True)

        row2 = ctk.CTkFrame(body, fg_color="transparent")
        row2.pack(fill="x", pady=4)
        ctk.CTkLabel(row2, text="Mode:", width=100, anchor="w").pack(side="left")
        self._mode_menu = ctk.CTkOptionMenu(
            row2, variable=self.mode_var, values=list(RIFE_MODE_LABELS), height=28
        )
        self._mode_menu.pack(side="left", fill="x", expand=True)

        self._audio_cb = ctk.CTkCheckBox(body, text="Include audio", variable=self.audio_var)
        self._audio_cb.pack(anchor="w", pady=(6, 8))

        out_row = ctk.CTkFrame(body, fg_color="transparent")
        out_row.pack(fill="x", pady=4)
        ctk.CTkLabel(out_row, text="Output folder:", width=100, anchor="w").pack(side="left")
        self._out_entry = ctk.CTkEntry(out_row, textvariable=self.out_dir_var, height=28)
        self._out_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._browse_btn = ctk.CTkButton(
            out_row, text="…", width=36, height=28, command=self._browse
        )
        self._browse_btn.pack(side="left")

        btn = ctk.CTkFrame(self, fg_color="transparent")
        btn.pack(side="bottom", fill="x", padx=16, pady=14)
        ctk.CTkButton(btn, text="Cancel", width=100, command=self.destroy).pack(side="left")
        self.start_btn = ctk.CTkButton(btn, text="Start", command=self._start)
        self.start_btn.pack(side="right", fill="x", expand=True, padx=(10, 0))

        self.lift()
        self.focus_force()
        self.after_idle(lambda: sync_promo_strip(self))
        self.after(80, lambda: sync_promo_strip(self))
        self.after(40, self._refresh_status)

    def _form_widgets(self):
        return (
            self._mult_menu,
            self._mode_menu,
            self._audio_cb,
            self._out_entry,
            self._browse_btn,
            self._install_btn,
            self._open_btn,
            self._refresh_btn,
            self.start_btn,
        )

    def _set_form_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for w in self._form_widgets():
            try:
                w.configure(state=state)
            except Exception:
                pass
        if enabled:
            self.start_btn.configure(
                state="normal" if self._pack_ready else "disabled"
            )
            self._update_install_btn_style()

    def _refresh_status(self) -> None:
        if self._install_running:
            return
        try:
            status = runtime_status()
            self._pack_ready = bool(status.get("ready"))
            n = len(self.paths)
            parts = [f"{n} video(s)"]
            if self._pack_ready:
                models = status.get("models") or []
                parts.append("Optional pack ready.")
                if status.get("exe"):
                    parts.append(f"Exe: {status.get('exe')}")
                if models:
                    parts.append(f"Models: {', '.join(models)}")
                parts.append("Ready to start.")
            else:
                parts.append(status.get("message") or PACK_MISSING_MESSAGE)
                dest = status.get("dir") or str(default_rife_dir())
                parts.append(f"Folder: {dest}")
                parts.append("Use Install RIFE pack… to download automatically.")
            self.status_var.set("\n".join(parts))
            self.start_btn.configure(
                state="normal" if self._pack_ready else "disabled"
            )
            self._update_install_btn_style()
        except Exception as exc:
            self._pack_ready = False
            self.status_var.set(f"Status check failed:\n{exc}")
            self.start_btn.configure(state="disabled")
            self._update_install_btn_style()

    def _update_install_btn_style(self) -> None:
        """Primary when missing; grey secondary when installed (still allows reinstall)."""
        if self._install_running:
            return
        if self._pack_ready:
            self._install_btn.configure(
                text="Reinstall pack…",
                state="normal",
                fg_color="gray30",
                hover_color="gray25",
            )
        else:
            self._install_btn.configure(
                text="Install RIFE pack…",
                state="normal",
                fg_color=["#3B8ED0", "#1F6AA5"],
                hover_color=["#36719F", "#144870"],
            )

    def _open_rife_folder(self) -> None:
        if self._install_running:
            return
        status = runtime_status()
        path = (status.get("dir") or "").strip() or str(default_rife_dir())
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)
        except Exception as exc:
            messagebox.showerror("RIFE", f"Cannot open folder:\n{exc}", parent=self)

    def _install_pack(self) -> None:
        if self._install_running:
            return
        status = runtime_status()
        reinstall = bool(status.get("ready"))

        from rife_setup import (
            RIFE_PACK_DISK_ESTIMATE,
            RIFE_PACK_DOWNLOAD_ESTIMATE,
            RIFE_PACK_HF_NOTE,
            install_rife_pack,
        )

        dest = str(default_rife_dir())
        if reinstall:
            ok = messagebox.askyesno(
                "Reinstall RIFE pack",
                (
                    "RIFE pack is already installed.\n\n"
                    f"{status.get('exe')}\n\n"
                    "Download and overwrite it again?\n\n"
                    f"Download size: about {RIFE_PACK_DOWNLOAD_ESTIMATE}\n"
                    f"Installed size: {RIFE_PACK_DISK_ESTIMATE}"
                ),
                parent=self,
            )
        else:
            ok = messagebox.askyesno(
                "Install RIFE pack",
                (
                    "Download the optional rife-ncnn-vulkan pack?\n\n"
                    f"Source: {RIFE_PACK_HF_NOTE}\n"
                    f"Destination:\n{dest}\n\n"
                    f"Download size: about {RIFE_PACK_DOWNLOAD_ESTIMATE}\n"
                    f"Installed size: {RIFE_PACK_DISK_ESTIMATE}\n"
                    "Network required. Slim install keeps the preferred model only."
                ),
                parent=self,
            )
        if not ok:
            return

        from gui_elements import open_file_op_progress_dialog

        self._install_running = True
        self._set_form_enabled(False)
        progress = open_file_op_progress_dialog(
            self,
            title="Install RIFE pack",
            total=100,
            action_label="Download",
            topmost=True,
            show_preview=False,
        )

        def _on_progress(step: int, total: int, detail: str):
            try:
                self.after(
                    0,
                    lambda s=step, t=total, d=detail: progress.set_progress(
                        s, t, detail=d, phase="load"
                    ),
                )
            except Exception:
                pass

        def _worker():
            result = install_rife_pack(
                dest,
                progress_cb=_on_progress,
                should_stop=lambda: bool(getattr(progress, "cancelled", False)),
            )

            def _done():
                self._install_running = False
                try:
                    progress.close()
                except Exception:
                    pass
                self._set_form_enabled(True)
                self._refresh_status()
                if result.get("ok"):
                    messagebox.showinfo(
                        "Install RIFE pack",
                        result.get("message") or f"RIFE pack ready:\n{dest}",
                        parent=self,
                    )
                elif result.get("error") == "aborted":
                    messagebox.showinfo(
                        "Install RIFE pack",
                        "Download cancelled.",
                        parent=self,
                    )
                else:
                    messagebox.showerror(
                        "Install RIFE pack",
                        result.get("message") or "Install failed.",
                        parent=self,
                    )

            try:
                self.after(0, _done)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="rife-pack-install").start()

    def _browse(self):
        if self._install_running:
            return
        path = filedialog.askdirectory(initialdir=self.out_dir_var.get() or None)
        if path:
            self.out_dir_var.set(path)

    def _ensure_output_dir(self, out_dir: str, on_ready) -> None:
        """If ``out_dir`` exists, call ``on_ready``; else ask to create it via universal_dialog."""
        if os.path.isdir(out_dir):
            on_ready()
            return
        if os.path.exists(out_dir):
            messagebox.showerror(
                "Output folder",
                f"Path exists but is not a folder:\n{out_dir}",
                parent=self,
            )
            return

        def _create() -> None:
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(
                    "Output folder",
                    f"Cannot create folder:\n{out_dir}\n\n{exc}",
                    parent=self,
                )
                return
            on_ready()

        owner = self.controller if hasattr(self.controller, "universal_dialog") else None
        message = (
            f"Output folder does not exist:\n{out_dir}\n\n"
            "Create it?"
        )
        if owner is not None:
            owner.universal_dialog(
                title="Create folder?",
                message=message,
                confirm_callback=_create,
                confirm_text="Create",
                cancel_text="Cancel",
                show_cancel=True,
                parent=self,
            )
            return
        if messagebox.askyesno("Create folder?", message, parent=self):
            _create()

    def _finish_start(self, out_dir: str) -> None:
        mult_raw = (self.mult_var.get() or "2×").strip()
        mult = 4 if mult_raw.startswith("4") else 2
        mode_label = self.mode_var.get() or RIFE_MODE_LABELS[0]
        mode = "slowmo" if "slow" in mode_label.lower() else "fps"
        options = {
            "multiplier": mult,
            "mode": mode,
            "include_audio": bool(self.audio_var.get()),
            "output_dir": out_dir,
        }
        self.result = options
        self.destroy()
        if self.on_confirm:
            self.on_confirm(self.paths, options)

    def _start(self):
        if self._install_running:
            messagebox.showinfo(
                "RIFE",
                "Wait for the pack install to finish (or cancel it).",
                parent=self,
            )
            return
        if not self._pack_ready:
            messagebox.showwarning(
                "RIFE pack missing",
                PACK_MISSING_MESSAGE,
                parent=self,
            )
            return
        out_dir = (self.out_dir_var.get() or "").strip()
        if not out_dir:
            messagebox.showerror(
                "Output folder",
                "Enter or choose an output folder.",
                parent=self,
            )
            return
        self._ensure_output_dir(out_dir, lambda: self._finish_start(out_dir))
