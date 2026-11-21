"""
spandrel_worker_host.py — Run Spandrel upscale in a killable subprocess.

Cancel and app exit call ``kill_process_tree`` so CUDA/VRAM is released
immediately (same approach as SeedVR2 worker host).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable

from seedvr2_worker_host import kill_process_tree


def _creationflags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _worker_command(job_path: str) -> list[str]:
    """Build argv for the Spandrel child process (source + frozen)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--vibe-spandrel-job", job_path]
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spandrel_worker.py")
    return [sys.executable, worker, job_path]


class SpandrelWorkerHost:
    """Tracks the active Spandrel job subprocess for hard cancel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def abort(self) -> None:
        """Kill the current job process tree (frees GPU)."""
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None:
            kill_process_tree(proc, label="Spandrel")
            logging.info("[Spandrel] Job process killed (VRAM released).")

    def run_job(
        self,
        job: dict[str, Any],
        *,
        progress_cb: Callable[[float, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """
        Run one upscale in a child process. Returns a result dict compatible
        with ``spandrel_pipeline.upscale_image_file``.
        """
        if should_stop and should_stop():
            return {
                "ok": False,
                "output_path": None,
                "error": "aborted",
                "message": "Canceled.",
            }

        job_path = None
        try:
            fd, job_path = tempfile.mkstemp(prefix="vibe_spandrel_", suffix=".json")
            os.close(fd)
            with open(job_path, "w", encoding="utf-8") as fh:
                json.dump(job, fh)
        except Exception as exc:
            return {
                "ok": False,
                "output_path": None,
                "error": "runtime_error",
                "message": f"Could not write Spandrel job file: {exc}",
            }

        cmd = _worker_command(job_path)
        env = os.environ.copy()
        # Avoid CUDA context contention noise in the child log.
        env.setdefault("PYTHONUNBUFFERED", "1")

        try:
            with self._lock:
                # Only one GPU job at a time from this host.
                if self._proc is not None and self._proc.poll() is None:
                    kill_process_tree(self._proc, label="Spandrel")
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    creationflags=_creationflags(),
                )
                proc = self._proc
        except Exception as exc:
            self._cleanup_job_file(job_path)
            return {
                "ok": False,
                "output_path": None,
                "error": "runtime_error",
                "message": f"Failed to start Spandrel worker: {exc}",
            }

        assert proc.stdout is not None
        result: dict[str, Any] | None = None
        stderr_tail: list[str] = []

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            try:
                for line in proc.stderr:
                    text = (line or "").rstrip()
                    if not text:
                        continue
                    stderr_tail.append(text)
                    if len(stderr_tail) > 40:
                        del stderr_tail[: len(stderr_tail) - 40]
                    logging.debug("[SpandrelWorker] %s", text)
            except Exception:
                pass

        err_thread = threading.Thread(target=_drain_stderr, daemon=True)
        err_thread.start()

        try:
            for line in proc.stdout:
                if should_stop and should_stop():
                    kill_process_tree(proc, label="Spandrel")
                    return {
                        "ok": False,
                        "output_path": None,
                        "error": "aborted",
                        "message": "Canceled.",
                    }
                text = (line or "").strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except Exception:
                    logging.debug("[Spandrel] Non-JSON worker line: %s", text[:200])
                    continue
                if not isinstance(payload, dict):
                    continue
                kind = str(payload.get("t") or "")
                if kind == "progress":
                    if progress_cb:
                        try:
                            progress_cb(
                                float(payload.get("pct") or 0.0),
                                str(payload.get("msg") or ""),
                            )
                        except Exception:
                            pass
                elif kind == "result":
                    result = {
                        "ok": bool(payload.get("ok")),
                        "output_path": payload.get("output_path"),
                        "error": payload.get("error"),
                        "message": payload.get("message"),
                        "scale": payload.get("scale"),
                        "model_scale": payload.get("model_scale"),
                    }
            rc = proc.wait(timeout=10)
        except Exception as exc:
            kill_process_tree(proc, label="Spandrel")
            return {
                "ok": False,
                "output_path": None,
                "error": "runtime_error",
                "message": f"Spandrel worker failed: {exc}",
            }
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            self._cleanup_job_file(job_path)

        if should_stop and should_stop():
            return {
                "ok": False,
                "output_path": None,
                "error": "aborted",
                "message": "Canceled.",
            }

        if result is not None:
            return result

        detail = "\n".join(stderr_tail[-12:]) if stderr_tail else f"exit code {rc}"
        return {
            "ok": False,
            "output_path": None,
            "error": "runtime_error",
            "message": f"Spandrel worker produced no result.\n{detail}",
        }

    @staticmethod
    def _cleanup_job_file(path: str | None) -> None:
        if not path:
            return
        try:
            os.remove(path)
        except Exception:
            pass


_HOST: SpandrelWorkerHost | None = None
_HOST_LOCK = threading.Lock()


def get_spandrel_worker_host() -> SpandrelWorkerHost:
    global _HOST
    with _HOST_LOCK:
        if _HOST is None:
            _HOST = SpandrelWorkerHost()
        return _HOST


def abort_spandrel_worker() -> None:
    """Kill any active Spandrel job (Cancel / app exit)."""
    host = get_spandrel_worker_host()
    host.abort()


def shutdown_spandrel_worker_host() -> None:
    global _HOST
    with _HOST_LOCK:
        if _HOST is not None:
            _HOST.abort()
            _HOST = None
