"""BiRefNet background removal orchestration mixin."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
from typing import Callable

from tkinter import messagebox

from birefnet_dialog import BirefnetOptionsDialog
from birefnet_pipeline import unload_model
from gui_elements import get_conflict_rename_path, open_conflict_dialog, open_file_op_progress_dialog
from vtp_constants import IMAGE_FORMATS


class VtpBirefnetMixin:
    """Context-menu driven BiRefNet background removal for still images."""

    def ensure_birefnet_ready(
        self,
        on_ready: Callable[[], None],
        *,
        title: str = "Batch Convert",
        parent=None,
    ) -> None:
        """
        Make sure BiRefNet deps + CUDA torch are ready, offering auto-install.

        Calls ``on_ready`` when the runtime is usable. Install dialogs warn about
        download size/time and support Cancel.
        """
        from birefnet_config import python_deps_status, runtime_status

        owner = parent if parent is not None else self

        deps = python_deps_status()
        if not deps.get("ready"):
            if deps.get("error") == "deps_import_error":
                # Wheels are present but Torch/kornia cannot load in this process
                # (typical right after a CUDA wheel swap on Windows).
                self._offer_restart_after_torch_install(
                    deps.get("message") or "",
                    title=title,
                )
                return
            self.offer_install_birefnet_python_deps(
                list(deps.get("missing") or []),
                lambda: self.ensure_birefnet_ready(
                    on_ready, title=title, parent=parent
                ),
                title=title,
                parent=owner,
            )
            return

        rt = runtime_status(deep=True)
        if rt.get("ready"):
            on_ready()
            return

        err = rt.get("error") or ""
        if getattr(sys, "frozen", False) and err in (
            "gpu_pack_missing",
            "cuda_unavailable",
            "runtime_error",
        ):
            messagebox.showwarning(
                title,
                rt.get("message")
                or "Autotag GPU Pack is not installed.\n"
                "Extract the pack over VibePlayer/ and restart.",
                parent=owner,
            )
            return

        if err in ("cuda_unavailable", "gpu_pack_missing", "runtime_error"):
            self.offer_install_birefnet_torch_cuda(
                lambda: self.ensure_birefnet_ready(
                    on_ready, title=title, parent=parent
                ),
                title=title,
                parent=owner,
            )
            return

        messagebox.showwarning(
            title,
            rt.get("message") or "GPU runtime is not ready.",
            parent=owner,
        )

    def offer_install_birefnet_python_deps(
        self,
        missing: list[str],
        on_success: Callable[[], None],
        *,
        title: str = "Batch Convert",
        parent=None,
    ) -> None:
        """Ask to pip-install missing BiRefNet deps, then call ``on_success``."""
        pkgs = [p for p in (missing or []) if p]
        if not pkgs:
            on_success()
            return

        owner = parent if parent is not None else self
        joined = ", ".join(pkgs)

        def _install() -> None:
            from birefnet_weights_setup import install_birefnet_python_deps

            self._run_birefnet_pip_install_job(
                title=title,
                action_detail=f"pip install {joined}",
                installer=lambda progress_cb, should_stop: install_birefnet_python_deps(
                    pkgs,
                    progress_cb=progress_cb,
                    should_stop=should_stop,
                ),
                on_success=on_success,
            )

        message = (
            f"Missing packages:\n{joined}\n\n"
            "Install them now and continue?\n"
            "(Usually under a minute. You can Cancel anytime.)"
        )
        if hasattr(self, "universal_dialog"):
            self.universal_dialog(
                title="Install packages?",
                headline="Background removal needs extra Python packages.",
                message=message,
                confirm_callback=_install,
                confirm_text="Install",
                cancel_text="Cancel",
                show_cancel=True,
                secondary_cancel=True,
                dialog_width=412,
                parent=owner,
            )
            return
        if messagebox.askyesno(
            "Install packages?",
            "Background removal needs extra Python packages.\n\n" + message,
            parent=owner,
        ):
            _install()

    def offer_install_birefnet_torch_cuda(
        self,
        on_success: Callable[[], None],
        *,
        title: str = "Batch Convert",
        parent=None,
    ) -> None:
        """Ask to install CUDA PyTorch, with size/time warning + Cancel."""
        from birefnet_weights_setup import (
            BIREFNET_TORCH_DISK_ESTIMATE,
            BIREFNET_TORCH_TIME_HINT,
            install_birefnet_torch_cuda,
        )

        owner = parent if parent is not None else self

        def _install() -> None:
            self._run_birefnet_pip_install_job(
                title=title,
                action_detail=f"PyTorch CUDA ({BIREFNET_TORCH_DISK_ESTIMATE})",
                installer=lambda progress_cb, should_stop: install_birefnet_torch_cuda(
                    progress_cb=progress_cb,
                    should_stop=should_stop,
                ),
                on_success=on_success,
                allow_restart=True,
            )

        message = (
            f"Download size: about {BIREFNET_TORCH_DISK_ESTIMATE}\n"
            f"This can take {BIREFNET_TORCH_TIME_HINT} depending on your connection.\n\n"
            "Install now and continue?\n"
            "You can Cancel during the download."
        )
        if hasattr(self, "universal_dialog"):
            self.universal_dialog(
                title="Install PyTorch CUDA?",
                headline="Background removal needs PyTorch with CUDA.",
                message=message,
                confirm_callback=_install,
                confirm_text="Install",
                cancel_text="Cancel",
                show_cancel=True,
                secondary_cancel=True,
                dialog_width=412,  # ~40% wider than default 294
                parent=owner,
            )
            return
        if messagebox.askyesno(
            "Install PyTorch CUDA?",
            "Background removal needs PyTorch with CUDA.\n\n" + message,
            parent=owner,
        ):
            _install()

    def _run_birefnet_pip_install_job(
        self,
        *,
        title: str,
        action_detail: str,
        installer: Callable,
        on_success: Callable[[], None],
        allow_restart: bool = False,
    ) -> None:
        progress = open_file_op_progress_dialog(
            self,
            title=title,
            total=1,
            action_label="Installing",
            topmost=True,
        )
        progress.set_progress(0, detail=action_detail)

        def _worker() -> None:
            result: dict = {
                "ok": False,
                "message": "Install failed unexpectedly.",
            }
            try:
                result = installer(
                    lambda step, total, detail: self.after(
                        0,
                        lambda s=step, d=detail: progress.set_progress(s, detail=d),
                    ),
                    lambda: bool(getattr(progress, "cancelled", False)),
                ) or result
            except Exception as exc:
                logging.exception("BiRefNet pip install worker crashed")
                result = {
                    "ok": False,
                    "message": f"Install crashed:\n{exc}",
                    "cancelled": False,
                }

            def _done() -> None:
                try:
                    progress.close()
                except Exception:
                    pass
                if result.get("cancelled"):
                    return
                if not result.get("ok"):
                    messagebox.showerror(
                        title,
                        result.get("message") or "Install failed.",
                        parent=self,
                    )
                    return
                if allow_restart and result.get("restart_required"):
                    self._offer_restart_after_torch_install(
                        result.get("message") or "",
                        title=title,
                    )
                    return
                try:
                    on_success()
                except Exception:
                    logging.exception("BiRefNet install success callback failed")

            self.after(0, _done)

        threading.Thread(
            target=_worker, daemon=True, name="birefnet-pip-install"
        ).start()

    def _offer_restart_after_torch_install(
        self, message: str, *, title: str = "Batch Convert"
    ) -> None:
        body = (
            "This process still has the old Torch loaded.\n"
            "Restart Vibe Player, then run background removal again.\n\n"
            "Restart now?"
        )

        def _restart() -> None:
            self._restart_vibe_player_process()

        if hasattr(self, "universal_dialog"):
            self.universal_dialog(
                title="Restart required",
                headline="PyTorch CUDA is installed.",
                message=body,
                confirm_callback=_restart,
                confirm_text="Restart",
                cancel_text="Later",
                show_cancel=True,
                secondary_cancel=True,
                dialog_width=412,
                parent=self,
            )
            return
        if messagebox.askyesno(
            "Restart required",
            "PyTorch CUDA is installed.\n\n" + body,
            parent=self,
        ):
            _restart()

    def _restart_vibe_player_process(self) -> None:
        """Relaunch this app and exit (needed after swapping Torch DLLs on Windows)."""
        try:
            main_file = getattr(sys.modules.get("__main__"), "__file__", None)
            app_dir = (
                os.path.dirname(os.path.abspath(main_file))
                if main_file
                else os.path.dirname(os.path.abspath(__file__))
            )
        except Exception:
            app_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(app_dir)
        try:
            if getattr(sys, "frozen", False):
                cmd = [sys.executable]
                cwd = os.path.dirname(os.path.abspath(sys.executable))
            else:
                run_bat = os.path.join(project_root, "run.bat")
                if os.path.isfile(run_bat):
                    cmd = [run_bat]
                    cwd = project_root
                else:
                    cmd = [sys.executable, os.path.join(app_dir, "main.py")]
                    cwd = app_dir
            subprocess.Popen(cmd, cwd=cwd, env={**os.environ})
        except Exception as exc:
            logging.exception("Failed to relaunch Vibe Player")
            messagebox.showerror(
                "Restart",
                f"Could not restart automatically:\n{exc}\n\nPlease restart manually.",
                parent=self,
            )
            return
        try:
            self.after(150, lambda: os._exit(0))
        except Exception:
            os._exit(0)

    def _notify_birefnet_issue_once(self, error_code: str | None, message: str):
        flag = f"_birefnet_issue_shown_{error_code or 'unknown'}"
        if getattr(self, flag, False):
            return
        setattr(self, flag, True)
        text = message or "Background removal failed."
        self.after(0, lambda: self.status_bar.set_action_message(text))
        title = "Remove Background"
        if error_code in ("gpu_pack_missing", "cuda_unavailable", "runtime_error"):
            title = "GPU pack"
        elif error_code == "weights_missing":
            title = "BiRefNet weights"
        elif error_code == "deps_missing":
            title = "Python packages"
        self.after(0, lambda: messagebox.showwarning(title, text))

    def selected_paths_for_birefnet(self, clicked_path: str | None = None) -> list[str]:
        selected_paths: list[str] = []
        if hasattr(self, "selected_thumbnails") and self.selected_thumbnails:
            selected_paths = [
                item[0]
                for item in self.selected_thumbnails
                if item and item[0] and not os.path.isdir(item[0])
            ]
        if not selected_paths and clicked_path and not os.path.isdir(clicked_path):
            selected_paths = [clicked_path]

        supported = []
        for path in selected_paths:
            ext = os.path.splitext(path)[1].lower()
            if ext in IMAGE_FORMATS:
                supported.append(path)
        return supported

    def open_birefnet_dialog(self, clicked_path: str | None = None):
        paths = self.selected_paths_for_birefnet(clicked_path)
        if not paths:
            messagebox.showinfo(
                "Remove Background",
                "No images selected.\n\nSelect one or more image thumbnails first.",
            )
            return
        BirefnetOptionsDialog(
            self,
            paths=paths,
            on_confirm=self.start_birefnet_batch,
            controller=self,
        )

    def start_birefnet_batch(self, paths: list[str], options: dict):
        if getattr(self, "_birefnet_batch_running", False):
            messagebox.showinfo("Remove Background", "A background removal job is already running.")
            return

        paths = [p for p in (paths or []) if p and os.path.isfile(p)]
        if not paths:
            messagebox.showinfo("Remove Background", "No valid images to process.")
            return

        pm = getattr(self, "plugin_manager", None)
        plugin = pm.get_upscale_plugin("birefnet") if pm else None
        if not plugin:
            messagebox.showerror(
                "Remove Background",
                "BiRefNet plugin not loaded. Check app.log.",
            )
            return

        self.ensure_birefnet_ready(
            lambda: self._start_birefnet_batch_after_ready(paths, options, plugin),
            title="Remove Background",
        )

    def _start_birefnet_batch_after_ready(
        self, paths: list[str], options: dict, plugin
    ) -> None:
        if getattr(self, "_birefnet_batch_running", False):
            messagebox.showinfo(
                "Remove Background",
                "A background removal job is already running.",
            )
            return

        status = plugin.runtime_status(deep=True) if hasattr(plugin, "runtime_status") else {}
        if not status.get("ready"):
            self._notify_birefnet_issue_once(
                status.get("error") or "gpu_pack_missing",
                status.get("message") or "Autotag GPU Pack is not installed.",
            )
            return

        out_dir = (options or {}).get("output_dir") or os.path.dirname(paths[0])
        total = len(paths)
        preview_path: str | None = None
        try:
            fd, preview_path = tempfile.mkstemp(
                prefix="vibe_birefnet_preview_", suffix=".jpg"
            )
            os.close(fd)
        except OSError:
            preview_path = None

        progress = open_file_op_progress_dialog(
            self,
            title="Remove Background",
            total=total,
            action_label="Processing",
            topmost=False,
            show_preview=bool(preview_path),
            preview_fit=True,
        )
        bg_mode = str((options or {}).get("bg_mode") or "transparent")
        first_name = os.path.basename(paths[0])
        if preview_path:
            from birefnet_preview_hook import write_input_preview

            if write_input_preview(paths[0], preview_path):
                progress.set_preview_path(preview_path)
                progress.set_preview_caption(f"{first_name} · before")
            else:
                progress.set_preview_path(preview_path)
                progress.set_preview_caption("Preview")
        self._birefnet_preview_path = preview_path
        self._birefnet_progress_dialog = progress
        self._birefnet_batch_running = True
        self.stop_requested = False
        try:
            self.status_bar.set_stop_callback(lambda: setattr(self, "stop_requested", True))
        except Exception:
            pass

        conflict_policy: dict = {"action": None, "apply_all": False}
        errors: list[str] = []
        written: list[str] = []

        def _should_stop() -> bool:
            if getattr(self, "stop_requested", False):
                return True
            dlg = getattr(self, "_birefnet_progress_dialog", None)
            return bool(dlg and getattr(dlg, "cancelled", False))

        def _resolve_conflict(output_path: str, src_path: str) -> str | None:
            if not output_path:
                return None
            try:
                same = os.path.normcase(os.path.abspath(output_path)) == os.path.normcase(
                    os.path.abspath(src_path)
                )
            except Exception:
                same = False
            if same or not os.path.exists(output_path):
                return output_path

            action = conflict_policy.get("action")
            if action in ("replace", "rename", "skip") and conflict_policy.get("apply_all"):
                if action == "replace":
                    return output_path
                if action == "rename":
                    return get_conflict_rename_path(output_path)
                return None

            holder: dict = {}
            done = threading.Event()

            def _ask():
                try:
                    if progress is not None:
                        progress.grab_release()
                except Exception:
                    pass
                try:
                    act, apply_all = open_conflict_dialog(
                        self, os.path.basename(output_path)
                    )
                    holder["action"] = act
                    holder["apply_all"] = apply_all
                finally:
                    try:
                        if progress.winfo_exists():
                            progress.grab_set()
                            progress.lift()
                    except Exception:
                        pass
                    done.set()

            self.after(0, _ask)
            done.wait()
            action = holder.get("action") or "cancel"
            apply_all = bool(holder.get("apply_all"))
            if apply_all and action in ("replace", "rename", "skip"):
                conflict_policy["action"] = action
                conflict_policy["apply_all"] = True

            if action == "replace":
                return output_path
            if action == "rename":
                return get_conflict_rename_path(output_path)
            if action == "skip":
                return None
            raise InterruptedError("Cancelled at conflict dialog.")

        def _worker():
            ok = 0
            skipped = 0
            aborted = False
            try:
                opts = {
                    **(plugin.default_options() if hasattr(plugin, "default_options") else {}),
                    **(options or {}),
                }
                for i, src in enumerate(paths, start=1):
                    if _should_stop():
                        aborted = True
                        break
                    name = os.path.basename(src)
                    self.after(
                        0,
                        lambda i=i, name=name: progress.set_progress(i - 1, detail=name),
                    )
                    try:
                        suggested = plugin.suggested_output_path(src, opts)
                        dest = _resolve_conflict(suggested, src)
                    except InterruptedError:
                        aborted = True
                        break
                    except Exception as exc:
                        errors.append(f"{name}: {exc}")
                        continue

                    if dest is None:
                        skipped += 1
                        continue

                    # Preview: image 1 shows "before" (set at dialog open). While image 2+
                    # runs, keep the last "after" on screen — do not overwrite with next input.

                    def _prog(frac: float, msg: str, _i=i, _name=name):
                        self.after(
                            0,
                            lambda: progress.set_progress(
                                _i - 1 + max(0.0, min(1.0, frac)),
                                detail=msg or _name,
                            ),
                        )

                    result = plugin.process(
                        src,
                        {**opts, "output_path": dest},
                        progress_cb=_prog,
                        should_stop=_should_stop,
                    )
                    if not result.get("ok"):
                        code = result.get("error")
                        msg = result.get("message") or "Failed."
                        if code == "aborted":
                            aborted = True
                            break
                        errors.append(f"{name}: {msg}")
                        if code in ("gpu_pack_missing", "cuda_unavailable", "weights_missing"):
                            self._notify_birefnet_issue_once(code, msg)
                            aborted = True
                            break
                        continue

                    out = result.get("output_path")
                    if out:
                        written.append(out)
                        if preview_path:
                            from birefnet_preview_hook import write_result_preview

                            if write_result_preview(
                                out,
                                preview_path,
                                bg_mode=bg_mode,
                            ):
                                _next_name = (
                                    os.path.basename(paths[i])
                                    if i < total
                                    else None
                                )

                                def _show_after(
                                    p=preview_path,
                                    n=name,
                                    bm=bg_mode,
                                    nn=_next_name,
                                ):
                                    try:
                                        progress.set_preview_path(p)
                                        mode_label = (
                                            "transparent"
                                            if bm != "color"
                                            else "solid color"
                                        )
                                        cap = f"{n} · after ({mode_label})"
                                        if nn:
                                            cap += f" · next: {nn}"
                                        progress.set_preview_caption(cap)
                                    except Exception:
                                        pass

                                self.after(0, _show_after)
                    ok += 1
                    self.after(0, lambda i=i, name=name: progress.set_progress(i, detail=name))
            finally:
                unload_model()
                preview_cleanup = getattr(self, "_birefnet_preview_path", None)
                self._birefnet_preview_path = None

                def _done():
                    self._birefnet_batch_running = False
                    self._birefnet_progress_dialog = None
                    try:
                        progress.close()
                    except Exception:
                        pass
                    if preview_cleanup:
                        try:
                            os.remove(preview_cleanup)
                        except OSError:
                            pass
                    try:
                        self.status_bar.set_stop_callback(None)
                    except Exception:
                        pass
                    parts = [f"Remove Background: {ok}/{total}"]
                    if skipped:
                        parts.append(f"{skipped} skipped")
                    if aborted:
                        parts.append("cancelled")
                    summary = ", ".join(parts)
                    try:
                        self.status_bar.set_action_message(summary)
                    except Exception:
                        pass
                    if errors:
                        shown = "\n".join(errors[:8])
                        more = f"\n…and {len(errors) - 8} more" if len(errors) > 8 else ""
                        messagebox.showwarning(
                            "Remove Background",
                            f"{summary}\n\n{shown}{more}",
                        )
                    elif ok and written:
                        try:
                            cur = getattr(self, "current_directory", None)
                            if cur:
                                cur_key = os.path.normcase(os.path.normpath(cur))
                                if any(
                                    os.path.normcase(os.path.normpath(os.path.dirname(p))) == cur_key
                                    for p in written
                                ):
                                    self.display_thumbnails(
                                        cur, force_refresh=True, preserve_scroll=True
                                    )
                        except Exception as exc:
                            logging.info("BiRefNet folder refresh failed: %s", exc)

                self.after(0, _done)

        threading.Thread(target=_worker, daemon=True, name="birefnet-batch").start()
