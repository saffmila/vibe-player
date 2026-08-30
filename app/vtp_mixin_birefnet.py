"""BiRefNet background removal orchestration mixin."""

from __future__ import annotations

import logging
import os
import tempfile
import threading

from tkinter import messagebox

from birefnet_dialog import BirefnetOptionsDialog
from birefnet_pipeline import unload_model
from gui_elements import get_conflict_rename_path, open_conflict_dialog, open_file_op_progress_dialog
from vtp_constants import IMAGE_FORMATS


class VtpBirefnetMixin:
    """Context-menu driven BiRefNet background removal for still images."""

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
