"""Offline AI upscale orchestration mixin for VideoThumbnailPlayer."""

from __future__ import annotations

import logging
import os
import threading
import time
from tkinter import messagebox

from gui_elements import get_conflict_rename_path, open_conflict_dialog, open_file_op_progress_dialog
from rife_dialog import RifeOptionsDialog
from upscale_dialog import UpscaleOptionsDialog
from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS


def _format_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins = int(seconds // 60)
    secs = seconds - mins * 60
    if mins < 60:
        return f"{mins}m {secs:.0f}s"
    hours = mins // 60
    mins = mins % 60
    return f"{hours}h {mins}m"


class VtpUpscaleMixin:
    """Context-menu driven offline upscale (SeedVR2 and future backends)."""

    def _notify_upscale_issue_once(self, error_code: str | None, message: str):
        """Show pack/weights warnings without spamming dialogs in a batch."""
        flag = f"_upscale_issue_shown_{error_code or 'unknown'}"
        if getattr(self, flag, False):
            return
        setattr(self, flag, True)
        text = message or "Upscale failed."
        self.after(0, lambda: self.status_bar.set_action_message(text))
        title = "Upscale"
        if error_code in ("gpu_pack_missing", "runner_venv_missing", "cuda_unavailable", "runtime_error"):
            title = "SeedVR 2 setup"
        elif error_code == "weights_missing":
            title = "SeedVR 2 weights"
        elif error_code == "runner_missing":
            title = "SeedVR 2 runner"
        elif error_code in ("rife_pack_missing", "rife_model_missing"):
            title = "RIFE pack"
        self.after(0, lambda: messagebox.showwarning(title, text))

    def selected_paths_for_upscale(self, clicked_path: str | None = None) -> list[str]:
        """Resolve image/video paths from multi-select or the right-clicked item."""
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
        skipped_unsupported = 0
        for path in selected_paths:
            ext = os.path.splitext(path)[1].lower()
            if ext in IMAGE_FORMATS or ext in VIDEO_FORMATS:
                supported.append(path)
            else:
                skipped_unsupported += 1
        self._upscale_skipped_unsupported_count = skipped_unsupported
        return supported

    def list_available_upscale_backends(self) -> list:
        pm = getattr(self, "plugin_manager", None)
        if not pm or not hasattr(pm, "list_upscale_plugins"):
            return []
        try:
            return pm.list_upscale_plugins()
        except Exception as exc:
            logging.error("[Upscale] Failed to list backends: %s", exc)
            return []

    def open_upscale_dialog(self, clicked_path: str | None = None):
        """Open upscale options for the current selection."""
        paths = self.selected_paths_for_upscale(clicked_path)
        if not paths:
            messagebox.showinfo("Upscale", "No images or videos selected.")
            return

        backends = self.list_available_upscale_backends()
        # RIFE is frame interpolation — it has its own dialog ("RIFE Interpolate…").
        # The Upscale dialog is SeedVR/DAT-oriented and must not list RIFE.
        backends = [
            b
            for b in backends
            if getattr(b, "id", "") not in ("rife", "birefnet")
        ]
        if not backends:
            messagebox.showerror(
                "Upscale",
                "No upscale plugins loaded. Check app.log for plugin import errors.\n\n"
                "For frame interpolation use right-click → RIFE Interpolate…",
            )
            return

        # Drop formats the primary backend cannot handle (e.g. .mpg for SeedVR2).
        primary = next(
            (b for b in backends if getattr(b, "id", "") == "seedvr2"),
            backends[0],
        )
        if hasattr(primary, "supports"):
            accepted = [p for p in paths if primary.supports(p)]
            skipped = len(paths) - len(accepted)
            if not accepted:
                messagebox.showinfo(
                    "Upscale",
                    "Selected files are not supported by SeedVR 2.\n\n"
                    "Videos: mp4, mkv, mov, avi, webm, flv, wmv.",
                )
                return
            if skipped:
                messagebox.showinfo(
                    "Upscale",
                    f"Skipping {skipped} unsupported file(s).\n"
                    f"Continuing with {len(accepted)} file(s).",
                )
            paths = accepted

        UpscaleOptionsDialog(
            self,
            backends=backends,
            default_paths=paths,
            on_confirm=self.start_upscale_batch,
            controller=self,
        )

    def open_rife_dialog(self, clicked_path: str | None = None):
        """Open RIFE interpolate options for selected videos."""
        paths = self.selected_paths_for_upscale(clicked_path)
        videos = [
            p
            for p in paths
            if os.path.splitext(p)[1].lower() in VIDEO_FORMATS
        ]
        if not videos:
            messagebox.showinfo("RIFE", "No videos selected.")
            return
        RifeOptionsDialog(
            self,
            paths=videos,
            on_confirm=self.start_rife_batch,
            controller=self,
        )

    def start_rife_batch(self, paths: list[str], options: dict):
        """Run RIFE interpolation on a background thread."""
        if getattr(self, "_rife_batch_running", False):
            messagebox.showinfo("RIFE", "A RIFE job is already running.")
            return
        paths = [p for p in (paths or []) if p and os.path.isfile(p)]
        if not paths:
            messagebox.showinfo("RIFE", "No valid videos to interpolate.")
            return

        pm = getattr(self, "plugin_manager", None)
        plugin = pm.get_upscale_plugin("rife") if pm else None
        if not plugin:
            messagebox.showerror("RIFE", "RIFE plugin not loaded. Check app.log.")
            return

        status = plugin.runtime_status() if hasattr(plugin, "runtime_status") else {}
        if not status.get("ready"):
            self._notify_upscale_issue_once(
                status.get("error") or "rife_pack_missing",
                status.get("message") or "RIFE optional pack is not installed.",
            )
            return

        out_dir = (options or {}).get("output_dir") or os.path.dirname(paths[0])
        total = len(paths)
        progress = open_file_op_progress_dialog(
            self,
            title="RIFE Interpolate",
            total=total,
            action_label="Interpolating",
            topmost=False,
            show_preview=False,
        )
        self._rife_progress_dialog = progress
        self._rife_batch_running = True
        self.stop_requested = False
        try:
            self.status_bar.set_stop_callback(lambda: setattr(self, "stop_requested", True))
        except Exception:
            pass

        def _should_stop() -> bool:
            if getattr(self, "stop_requested", False):
                return True
            dlg = getattr(self, "_rife_progress_dialog", None)
            try:
                if dlg is not None and getattr(dlg, "cancelled", False):
                    self.stop_requested = True
                    return True
            except Exception:
                pass
            return False

        def worker():
            self.after(0, self.status_bar.enable_stop)
            self.after(
                0,
                lambda: self.status_bar.set_action_message("RIFE interpolating…"),
            )
            done = 0
            failed = 0
            last_out = None
            try:
                for idx, file_path in enumerate(paths, start=1):
                    if _should_stop():
                        break
                    base = os.path.basename(file_path)
                    self._upscale_progress_update(
                        progress, idx - 1, total, detail=base, phase="upscale"
                    )

                    def _cb(pct, msg, _base=base, _idx=idx):
                        self._upscale_progress_update(
                            progress,
                            _idx - 1,
                            total,
                            detail=f"{_base} — {msg} ({pct:.0f}%)",
                            phase="upscale",
                        )

                    root, ext = os.path.splitext(base)
                    mult = int((options or {}).get("multiplier") or 2)
                    mode = (options or {}).get("mode") or "fps"
                    tag = f"_rife{mult}x" if mode == "fps" else f"_rife_slowmo{mult}x"
                    out_path = os.path.join(out_dir, f"{root}{tag}{ext or '.mp4'}")
                    file_opts = {
                        **(options or {}),
                        "output_path": out_path,
                    }
                    t0 = time.perf_counter()
                    result = plugin.process(
                        file_path,
                        options=file_opts,
                        progress_cb=_cb,
                        should_stop=_should_stop,
                    )
                    elapsed = time.perf_counter() - t0
                    if result.get("ok"):
                        done += 1
                        last_out = result.get("output_path") or out_path
                        logging.info(
                            "[RIFE] OK %s → %s (%.1fs)",
                            file_path,
                            last_out,
                            elapsed,
                        )
                    else:
                        failed += 1
                        err = result.get("error")
                        msg = result.get("message") or "RIFE failed."
                        if err == "aborted":
                            break
                        self._notify_upscale_issue_once(err, msg)
                    self._upscale_progress_update(
                        progress, idx, total, detail=base, phase="upscale"
                    )
            finally:
                self._rife_batch_running = False
                try:
                    self.after(0, self.status_bar.disable_stop)
                except Exception:
                    pass
                summary = f"RIFE done: {done} ok"
                if failed:
                    summary += f", {failed} failed"
                if last_out:
                    summary += f"\nLast: {last_out}"
                self.after(0, lambda s=summary: self.status_bar.set_action_message(s))
                self.after(
                    0,
                    lambda s=summary: messagebox.showinfo("RIFE", s),
                )
                try:
                    self.after(0, progress.destroy)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _upscale_progress_update(
        self,
        dialog,
        current: int,
        total: int,
        detail: str,
        phase: str | None = None,
    ):
        """Schedule FileOpProgressDialog update on the Tk thread."""
        if dialog is None:
            return

        def _update():
            try:
                if dialog.winfo_exists():
                    dialog.set_progress(current, total, detail=detail, phase=phase)
            except Exception:
                pass

        try:
            self.after(0, _update)
        except Exception:
            pass

    def _infer_upscale_phase(self, msg: str) -> str:
        text = (msg or "").lower()
        if any(
            tok in text
            for tok in (
                "loading model",
                "load model",
                "starting persistent",
                "vram keep",
                "worker:",
                "download",
                "resolving model",
                "staging",
                "preparing",
            )
        ):
            return "load"
        return "upscale"

    def _resolve_upscale_output_conflict(
        self,
        output_path: str,
        progress_dialog,
        conflict_policy: dict,
    ) -> str | None:
        """
        If output already exists, ask Replace / Rename / Skip / Cancel.

        Returns the path to write, or None to skip this file.
        Raises InterruptedError on cancel.
        """
        if not output_path or not os.path.exists(output_path):
            return output_path

        action = conflict_policy.get("action")
        if action in ("replace", "rename", "skip") and conflict_policy.get("apply_all"):
            if action == "replace":
                return output_path
            if action == "rename":
                return get_conflict_rename_path(output_path)
            return None  # skip

        holder: dict = {}
        done = threading.Event()

        def _ask():
            try:
                try:
                    if progress_dialog is not None:
                        progress_dialog.grab_release()
                except Exception:
                    pass
                act, apply_all = open_conflict_dialog(self, os.path.basename(output_path))
                holder["action"] = act
                holder["apply_all"] = apply_all
            finally:
                try:
                    if progress_dialog is not None and progress_dialog.winfo_exists():
                        progress_dialog.grab_set()
                        progress_dialog.lift()
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
        raise InterruptedError("Upscale canceled at conflict dialog")

    def start_upscale_batch(self, job: dict):
        """Run an offline upscale batch on a background thread."""
        if not job:
            return
        if getattr(self, "_upscale_batch_running", False):
            messagebox.showinfo("Upscale", "An upscale job is already running.")
            return

        backend_id = job.get("backend_id") or "seedvr2"
        if backend_id == "rife":
            # SeedVR Upscale UI must not drive RIFE; keep a safe redirect.
            messagebox.showinfo(
                "RIFE",
                "RIFE frame interpolation has its own dialog.\n\n"
                "Use right-click → RIFE Interpolate… (or Export → RIFE checkbox).",
            )
            return
        paths = [p for p in (job.get("paths") or []) if p and os.path.isfile(p)]
        options = dict(job.get("options") or {})
        if not paths:
            messagebox.showinfo("Upscale", "No valid files to upscale.")
            return

        pm = getattr(self, "plugin_manager", None)
        plugin = pm.get_upscale_plugin(backend_id) if pm else None
        if not plugin:
            messagebox.showerror("Upscale", f"Upscale backend '{backend_id}' not found.")
            return

        # Allow a fresh warning per batch.
        for attr in list(vars(self)):
            if attr.startswith("_upscale_issue_shown_"):
                try:
                    delattr(self, attr)
                except Exception:
                    setattr(self, attr, False)

        total = len(paths)
        import tempfile

        preview_fd, preview_path = tempfile.mkstemp(prefix="vibe_seedvr2_preview_", suffix=".jpg")
        try:
            os.close(preview_fd)
            os.remove(preview_path)
        except OSError:
            pass

        progress = open_file_op_progress_dialog(
            self,
            title="Upscale",
            total=total,
            action_label="Upscaling",
            # Long job: do not pin above other apps when Vibe is in the background.
            topmost=False,
            show_preview=True,
        )
        try:
            progress.set_preview_path(preview_path)
        except Exception:
            pass
        self._upscale_progress_dialog = progress
        self._upscale_preview_path = preview_path
        self._upscale_batch_running = True
        self.stop_requested = False

        def _request_upscale_stop():
            setattr(self, "stop_requested", True)
            # Kill SeedVR runner immediately so Cancel frees VRAM without waiting
            # for the next progress line.
            try:
                from seedvr2_worker_host import shutdown_seedvr2_worker_host

                shutdown_seedvr2_worker_host()
            except Exception:
                logging.debug("[Upscale] Immediate SeedVR shutdown on Stop failed", exc_info=True)

        try:
            self.status_bar.set_stop_callback(_request_upscale_stop)
        except Exception:
            pass

        def _should_stop() -> bool:
            if getattr(self, "stop_requested", False):
                return True
            dlg = getattr(self, "_upscale_progress_dialog", None)
            try:
                if dlg is not None and getattr(dlg, "cancelled", False):
                    self.stop_requested = True
                    return True
            except Exception:
                pass
            return False

        def worker():
            self.after(0, self.status_bar.enable_stop)
            plugin_name = getattr(plugin, "name", backend_id)
            self.after(
                0,
                lambda: self.status_bar.set_action_message(f"Upscaling with {plugin_name}…"),
            )
            done = 0
            failed = 0
            skipped = 0
            aborted = False
            last_out = None
            last_elapsed = 0.0
            batch_t0 = time.perf_counter()
            conflict_policy: dict = {"action": None, "apply_all": False}

            for idx, file_path in enumerate(paths, start=1):
                if _should_stop():
                    aborted = True
                    self.after(0, lambda: self.status_bar.set_action_message("Upscale aborted."))
                    self._upscale_progress_update(
                        progress,
                        idx - 1,
                        total,
                        "Canceled — finishing current file if needed…",
                    )
                    break

                base = os.path.basename(file_path)
                file_opts = dict(options)
                ext = os.path.splitext(file_path)[1].lower()
                want_rife = bool(file_opts.get("rife_enabled")) and ext in VIDEO_FORMATS
                rife_mult = int(file_opts.get("rife_multiplier") or 2)
                rife_mode = str(file_opts.get("rife_mode") or "fps")
                seedvr_suffix = str(file_opts.get("suffix") or "_seedvr2")

                # Resolve destination + conflict before starting heavy work.
                try:
                    if want_rife:
                        from rife_pipeline import seedvr_rife_final_path

                        suggested = seedvr_rife_final_path(
                            file_path,
                            output_dir=file_opts.get("output_dir"),
                            seedvr_suffix=seedvr_suffix,
                            multiplier=rife_mult,
                            mode=rife_mode,
                        )
                    else:
                        suggested = plugin.suggested_output_path(file_path, file_opts)
                    resolved = self._resolve_upscale_output_conflict(
                        suggested, progress, conflict_policy
                    )
                except InterruptedError:
                    aborted = True
                    break
                except Exception as exc:
                    logging.error("[Upscale] Conflict handling failed: %s", exc)
                    failed += 1
                    continue

                if resolved is None:
                    skipped += 1
                    self._upscale_progress_update(
                        progress,
                        idx,
                        total,
                        f"{base}\nSkipped (output exists)",
                        phase="upscale",
                    )
                    continue

                # SeedVR writes to a temp file when RIFE will produce the final.
                chain_tmpdir = None
                seedvr_out = resolved
                if want_rife:
                    import tempfile as _tempfile

                    chain_tmpdir = _tempfile.mkdtemp(prefix="vibe_seedvr_rife_")
                    seedvr_out = os.path.join(chain_tmpdir, "seedvr_tmp.mp4")
                file_opts["output_path"] = seedvr_out
                preview_path = getattr(self, "_upscale_preview_path", None)
                if preview_path and progress is not None:
                    try:
                        from seedvr2_preview_hook import write_source_preview

                        write_source_preview(file_path, preview_path)
                        name = base

                        def _show_source(p=preview_path, n=name):
                            try:
                                progress.set_preview_path(p)
                                progress.set_preview_caption(
                                    f"Working on: {n}  ·  1:1 center crop"
                                )
                            except Exception:
                                pass

                        self.after(0, _show_source)
                    except Exception:
                        pass
                if preview_path:
                    file_opts["preview_path"] = preview_path
                if "chunk_preview" not in file_opts:
                    file_opts["chunk_preview"] = bool(
                        getattr(self, "seedvr2_chunk_preview", True)
                    )

                self._upscale_progress_update(
                    progress,
                    idx - 1,
                    total,
                    f"{base}\nPreparing…",
                    phase="load",
                )
                pipe_label = "SeedVR 2 + RIFE" if want_rife else "SeedVR 2"
                self.after(
                    0,
                    lambda i=idx, t=total, p=base, lab=pipe_label: self.status_bar.set_action_message(
                        f"[{lab} Batch] Processing {i}/{t}: {p}"
                        if t > 1
                        else f"[{lab}] Processing: {p}"
                    ),
                )

                def progress_cb(frac: float, msg: str, phase: str | None = None, i=idx, t=total, name=base):
                    line = (msg or "").strip() or "Working…"
                    resolved_phase = phase or self._infer_upscale_phase(line)
                    detail = f"{name}\n{line}"
                    self._upscale_progress_update(progress, i - 1, t, detail, phase=resolved_phase)
                    self.after(0, lambda m=line: self.status_bar.set_action_message(m[:120]))
                    low = line.lower()
                    if progress is not None and ("chunk preview" in low or "preview updated" in low):
                        def _cap(m=line, n=name, p=getattr(self, "_upscale_preview_path", None)):
                            try:
                                if p:
                                    progress.set_preview_path(p)
                                progress.set_preview_caption(f"{n}  ·  {m}")
                            except Exception:
                                pass

                        self.after(0, _cap)
                    try:
                        overall = ((i - 1) + max(0.0, min(1.0, float(frac)))) / max(1, t)
                        self.after(0, lambda v=overall: self.status_bar.set_progress(v))
                    except Exception:
                        pass

                t0 = time.perf_counter()
                try:
                    result = plugin.process(
                        file_path,
                        options=file_opts,
                        progress_cb=progress_cb,
                        should_stop=_should_stop,
                    )
                except Exception as exc:
                    logging.error("[Upscale] Failed on %s: %s", file_path, exc)
                    failed += 1
                    self._upscale_progress_update(
                        progress, idx, total, f"{base}\nError: {exc}"
                    )
                    if chain_tmpdir:
                        import shutil as _shutil

                        _shutil.rmtree(chain_tmpdir, ignore_errors=True)
                    continue
                elapsed = time.perf_counter() - t0

                if not result:
                    failed += 1
                    if chain_tmpdir:
                        import shutil as _shutil

                        _shutil.rmtree(chain_tmpdir, ignore_errors=True)
                    continue

                error = result.get("error")
                blocking = (
                    "gpu_pack_missing",
                    "runner_venv_missing",
                    "cuda_unavailable",
                    "runtime_error",
                    "weights_missing",
                    "runner_missing",
                    "not_implemented",
                    "oom",
                    "rife_pack_missing",
                    "rife_model_missing",
                    "ffmpeg_missing",
                )
                if error in blocking:
                    self._notify_upscale_issue_once(error, result.get("message") or error)
                    failed += 1
                    self._upscale_progress_update(
                        progress,
                        idx - 1,
                        total,
                        result.get("message") or error or "Blocked",
                    )
                    if chain_tmpdir:
                        import shutil as _shutil

                        _shutil.rmtree(chain_tmpdir, ignore_errors=True)
                    break

                if error == "aborted" or _should_stop():
                    aborted = True
                    failed += 1
                    if chain_tmpdir:
                        import shutil as _shutil

                        _shutil.rmtree(chain_tmpdir, ignore_errors=True)
                    break

                if result.get("ok") and result.get("output_path") and want_rife:
                    # SeedVR → FFV1 → RIFE → final encode (resolved path).
                    try:
                        from rife_pipeline import interpolate_video, remux_near_lossless

                        seedvr_path = result["output_path"]
                        ffv1_path = os.path.join(chain_tmpdir, "seedvr_ffv1.mkv")

                        def _rife_progress(pct: float, msg: str, i=idx, t=total, name=base):
                            detail = f"{name}\nRIFE: {msg}"
                            self._upscale_progress_update(
                                progress, i - 1, t, detail, phase="upscale"
                            )
                            self.after(
                                0,
                                lambda m=msg: self.status_bar.set_action_message(
                                    f"RIFE: {m}"[:120]
                                ),
                            )

                        progress_cb(0.92, "Near-lossless intermediate (FFV1)…", phase="upscale")
                        mid = remux_near_lossless(
                            seedvr_path,
                            ffv1_path,
                            progress_cb=lambda _f, m: _rife_progress(0, m),
                            should_stop=_should_stop,
                        )
                        if not mid.get("ok"):
                            result = mid
                        else:
                            progress_cb(0.94, "RIFE interpolating…", phase="upscale")
                            rife_res = interpolate_video(
                                mid["output_path"],
                                resolved,
                                multiplier=rife_mult,
                                mode=rife_mode,
                                include_audio=True,
                                encode_settings={
                                    "ext": os.path.splitext(resolved)[1] or ".mp4",
                                    "video_quality": "High",
                                    "audio_bitrate": "192k",
                                    "keep_size": True,
                                },
                                progress_cb=lambda pct, msg: _rife_progress(pct, msg),
                                should_stop=_should_stop,
                            )
                            result = rife_res
                            if rife_res.get("ok"):
                                result["output_path"] = resolved
                    except Exception as exc:
                        logging.exception("[Upscale+RIFE] Chain failed on %s", file_path)
                        result = {
                            "ok": False,
                            "output_path": None,
                            "error": "runtime_error",
                            "message": str(exc),
                        }
                    finally:
                        if chain_tmpdir:
                            import shutil as _shutil

                            _shutil.rmtree(chain_tmpdir, ignore_errors=True)
                    elapsed = time.perf_counter() - t0
                elif chain_tmpdir:
                    import shutil as _shutil

                    _shutil.rmtree(chain_tmpdir, ignore_errors=True)

                if result.get("ok") and result.get("output_path"):
                    done += 1
                    out = result["output_path"]
                    last_out = out
                    last_elapsed = elapsed
                    logging.info(
                        "[Upscale] OK %s → %s (%.1fs)",
                        file_path,
                        out,
                        elapsed,
                    )
                    self._upscale_progress_update(
                        progress,
                        idx,
                        total,
                        f"{base}\nDone → {os.path.basename(out)} ({_format_duration(elapsed)})",
                        phase="upscale",
                    )
                    ok_msg = (
                        f"Upscale OK: {os.path.basename(out)} → {out} "
                        f"({_format_duration(elapsed)})"
                    )
                    self.after(0, lambda m=ok_msg: self.status_bar.set_action_message(m))
                    try:
                        out_dir = os.path.dirname(out)
                        cur = getattr(self, "current_directory", None)
                        if cur and os.path.normcase(os.path.normpath(out_dir)) == os.path.normcase(
                            os.path.normpath(cur)
                        ):
                            self.after(0, lambda: self.display_thumbnails(cur))
                    except Exception:
                        pass
                else:
                    failed += 1
                    msg = result.get("message")
                    if msg:
                        logging.warning("[Upscale] %s: %s", file_path, msg)
                        self._notify_upscale_issue_once(error or result.get("error") or "failed", msg)
                    self._upscale_progress_update(
                        progress,
                        idx,
                        total,
                        f"{base}\nFailed: {msg or error or 'unknown'}",
                    )

            batch_elapsed = time.perf_counter() - batch_t0
            parts = [f"{done} ok"]
            if failed:
                parts.append(f"{failed} failed")
            if skipped:
                parts.append(f"{skipped} skipped")
            counts = ", ".join(parts)
            if aborted:
                summary = f"Upscale aborted: {counts} ({_format_duration(batch_elapsed)})"
            else:
                summary = f"Upscale finished: {counts} ({_format_duration(batch_elapsed)})"

            if done == 1 and last_out:
                status_line = (
                    f"Upscale successful → {last_out} "
                    f"({_format_duration(last_elapsed)})"
                )
            elif done > 1 and last_out:
                status_line = (
                    f"Upscale successful ({done} files, {_format_duration(batch_elapsed)}) "
                    f"— last: {last_out}"
                )
            else:
                status_line = summary

            self.after(0, lambda m=status_line: self.status_bar.set_action_message(m))
            self._upscale_progress_update(
                progress,
                total if not aborted else done,
                total,
                summary,
                phase="done" if done and not aborted else "upscale",
            )

            def _finish():
                try:
                    if progress is not None:
                        progress.close()
                except Exception:
                    pass
                self._upscale_progress_dialog = None
                self._upscale_batch_running = False
                try:
                    p = getattr(self, "_upscale_preview_path", None)
                    if p and os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
                self._upscale_preview_path = None
                if done or failed or skipped or aborted:
                    messagebox.showinfo("Upscale", summary)

            self.after(0, _finish)
            # Keep success path visible longer on the status bar.
            self.after(12000, self.status_bar.clear_action_message)
            self.after(0, lambda: self.status_bar.set_progress(0))
            self.after(0, self.status_bar.disable_stop)
            # Always drop SeedVR VRAM after cancel; also after success unless Keep VRAM.
            try:
                keep = bool(options.get("keep_vram"))
                if aborted or not keep:
                    from seedvr2_worker_host import shutdown_seedvr2_worker_host

                    shutdown_seedvr2_worker_host()
                    if aborted:
                        logging.info("[Upscale] SeedVR worker shut down after cancel (VRAM).")
            except Exception:
                logging.debug("[Upscale] SeedVR shutdown after batch failed", exc_info=True)

        threading.Thread(target=worker, daemon=True).start()
