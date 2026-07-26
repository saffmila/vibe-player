"""
seedvr2_worker_host.py — Manage a long-lived SeedVR2 worker from Vibe Player.

When "Keep model in VRAM" is enabled, the host starts one subprocess (runner venv)
and reuses it across upscale jobs until app exit / unload.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable


class SeedVR2WorkerHost:
    """Singleton-style host for the persistent SeedVR2 worker process."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._reader_lock = threading.Lock()
        self._python: str | None = None
        self._runner_dir: str | None = None
        self._worker_script: str | None = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ensure_started(self, python_exe: str, runner_dir: str, worker_script: str) -> None:
        with self._lock:
            if (
                self.alive
                and self._python == python_exe
                and self._runner_dir == runner_dir
            ):
                return
            self._shutdown_unlocked()
            self._python = python_exe
            self._runner_dir = runner_dir
            self._worker_script = worker_script
            self._start_unlocked()

    def _start_unlocked(self) -> None:
        assert self._python and self._runner_dir and self._worker_script
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # Prefer bundled ffmpeg if host already put it on PATH.
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        cmd = [
            self._python,
            self._worker_script,
            "--runner-dir",
            self._runner_dir,
        ]
        logging.info("[SeedVR2] Starting persistent worker: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=str(Path(self._worker_script).resolve().parent),
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        # Drain stderr in background so the pipe never fills.
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        ready = self._read_event(timeout_s=120.0)
        if not ready or ready.get("event") != "ready":
            err = ready
            self._shutdown_unlocked()
            raise RuntimeError(f"SeedVR2 worker failed to start: {err}")

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                text = line.rstrip()
                if text:
                    logging.info("[SeedVR2:worker] %s", text)
        except Exception:
            pass

    def _read_event(self, timeout_s: float = 3600.0) -> dict | None:
        """Read one JSON line from worker stdout (blocking; caller holds lock)."""
        import select
        import time

        proc = self._proc
        if proc is None or proc.stdout is None:
            return None

        # On Windows, select() doesn't work on pipes — just readline with thread.
        if os.name == "nt":
            holder: dict[str, Any] = {"line": None, "err": None}

            def _read():
                try:
                    holder["line"] = proc.stdout.readline()
                except Exception as exc:
                    holder["err"] = exc

            t = threading.Thread(target=_read, daemon=True)
            t.start()
            t.join(timeout=timeout_s)
            if t.is_alive():
                return {"event": "timeout", "ok": False, "message": "Worker read timeout"}
            if holder["err"]:
                return {"event": "error", "ok": False, "message": str(holder["err"])}
            line = holder["line"]
            if not line:
                return {"event": "eof", "ok": False, "message": "Worker exited"}
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                logging.warning("[SeedVR2] Non-JSON worker line: %s", line[:200])
                return self._read_event(timeout_s=timeout_s)

        # POSIX: select-based timeout
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if not ready:
                if proc.poll() is not None:
                    return {"event": "eof", "ok": False, "message": "Worker exited"}
                continue
            line = proc.stdout.readline()
            if not line:
                return {"event": "eof", "ok": False, "message": "Worker exited"}
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                logging.warning("[SeedVR2] Non-JSON worker line: %s", line[:200])
                continue
        return {"event": "timeout", "ok": False, "message": "Worker read timeout"}

    def upscale(
        self,
        *,
        input_path: str,
        output_path: str,
        model_dir: str,
        dit_model: str,
        cuda_device: str,
        options: dict | None = None,
        progress_cb: Callable[[float, str], None] | Callable[..., None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict:
        with self._lock:
            if not self.alive:
                if not (self._python and self._runner_dir and self._worker_script):
                    return {
                        "ok": False,
                        "error": "worker_down",
                        "message": "Persistent worker is not configured.",
                    }
                try:
                    self._start_unlocked()
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": "worker_start_failed",
                        "message": str(exc),
                    }

            assert self._proc is not None and self._proc.stdin is not None
            req = {
                "cmd": "upscale",
                "input": input_path,
                "output": output_path,
                "model_dir": model_dir,
                "dit_model": dit_model,
                "cuda_device": cuda_device,
                "options": options or {},
            }
            try:
                self._proc.stdin.write(json.dumps(req) + "\n")
                self._proc.stdin.flush()
            except Exception as exc:
                self._shutdown_unlocked()
                return {
                    "ok": False,
                    "error": "worker_write_failed",
                    "message": str(exc),
                }

            while True:
                if should_stop and should_stop():
                    self._shutdown_unlocked()
                    return {
                        "ok": False,
                        "error": "aborted",
                        "message": "Upscale aborted.",
                    }
                evt = self._read_event(timeout_s=3600.0)
                if not evt:
                    self._shutdown_unlocked()
                    return {
                        "ok": False,
                        "error": "worker_dead",
                        "message": "Worker stopped responding.",
                    }
                kind = evt.get("event")
                if kind == "progress":
                    if progress_cb:
                        phase = evt.get("phase") or "upscale"
                        progress_cb(0.5, str(evt.get("msg") or "Working…"), phase)
                    continue
                if kind == "preview":
                    # UI polls the file; optional nudge via progress text.
                    if progress_cb:
                        progress_cb(0.5, "Live preview ready…", "upscale")
                    continue
                if kind == "result":
                    if evt.get("ok"):
                        return {
                            "ok": True,
                            "output_path": evt.get("output_path") or output_path,
                            "error": None,
                            "message": None,
                        }
                    return {
                        "ok": False,
                        "output_path": None,
                        "error": evt.get("error") or "runner_failed",
                        "message": evt.get("message") or "Upscale failed",
                    }
                if kind in ("eof", "timeout", "fatal", "error"):
                    self._shutdown_unlocked()
                    return {
                        "ok": False,
                        "output_path": None,
                        "error": kind,
                        "message": evt.get("message") or "Worker error",
                    }
                # ignore unknown events

    def unload(self) -> None:
        with self._lock:
            if not self.alive or self._proc is None or self._proc.stdin is None:
                return
            try:
                self._proc.stdin.write(json.dumps({"cmd": "unload"}) + "\n")
                self._proc.stdin.flush()
                self._read_event(timeout_s=60.0)
            except Exception:
                self._shutdown_unlocked()

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown_unlocked()

    def _shutdown_unlocked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                proc.stdin.flush()
                proc.wait(timeout=8)
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        logging.info("[SeedVR2] Persistent worker stopped (VRAM released).")


_HOST: SeedVR2WorkerHost | None = None
_HOST_LOCK = threading.Lock()


def get_seedvr2_worker_host() -> SeedVR2WorkerHost:
    global _HOST
    with _HOST_LOCK:
        if _HOST is None:
            _HOST = SeedVR2WorkerHost()
        return _HOST


def shutdown_seedvr2_worker_host() -> None:
    global _HOST
    with _HOST_LOCK:
        if _HOST is not None:
            _HOST.shutdown()
            _HOST = None
