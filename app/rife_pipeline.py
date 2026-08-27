"""
rife_pipeline.py — Offline frame interpolation via rife-ncnn-vulkan + FFmpeg.

Pipeline (same pattern as the upstream README):
  1) Extract frames (+ optional audio) for a clip/segment with FFmpeg
  2) Run rife-ncnn-vulkan on the frame directory (2× per pass; 4× = two passes)
  3) Re-encode frames at the target FPS and mux audio
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from rife_config import find_rife_exe, resolve_model_path, runtime_status

ProgressCb = Callable[[float, str], None]
StopCb = Callable[[], bool]

_SUBPROCESS_STARTUPINFO = None
if os.name == "nt":
    _SUBPROCESS_STARTUPINFO = subprocess.STARTUPINFO()
    _SUBPROCESS_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _SUBPROCESS_STARTUPINFO.wShowWindow = subprocess.SW_HIDE


def _ffmpeg_bin() -> str:
    from file_operations import get_ffmpeg_path

    return get_ffmpeg_path()


def _ffprobe_bin() -> str:
    from file_operations import get_ffprobe_path

    return get_ffprobe_path()


def _run(
    cmd: list[str],
    *,
    label: str,
    should_stop: StopCb | None = None,
) -> None:
    logging.info("[RIFE] %s: %s", label, " ".join(cmd))
    if should_stop and should_stop():
        raise RuntimeError("aborted")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        startupinfo=_SUBPROCESS_STARTUPINFO,
    )
    try:
        while True:
            if should_stop and should_stop():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise RuntimeError("aborted")
            try:
                code = proc.wait(timeout=0.4)
                break
            except subprocess.TimeoutExpired:
                continue
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        if code != 0:
            raise RuntimeError(
                f"{label} failed (exit {code}).\n{stderr[-1200:] if stderr else ''}".strip()
            )
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass


def _probe_fps(video_path: str) -> float:
    """Best-effort source FPS; fall back to 30."""
    try:
        ffprobe = _ffprobe_bin()
        cmd = [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=r_frame_rate,avg_frame_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]
        out = subprocess.check_output(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            startupinfo=_SUBPROCESS_STARTUPINFO,
        ).strip().splitlines()
        for line in out:
            line = (line or "").strip()
            if not line or line.lower() in {"n/a", "0/0"}:
                continue
            if "/" in line:
                num_s, den_s = line.split("/", 1)
                num = float(num_s)
                den = float(den_s) or 1.0
                fps = num / den
            else:
                fps = float(line)
            if 1.0 <= fps <= 240.0:
                return fps
    except Exception as exc:
        logging.warning("[RIFE] FPS probe failed: %s", exc)
    return 30.0


def _count_frames(folder: Path) -> int:
    return sum(1 for p in folder.iterdir() if p.suffix.lower() in {".png", ".jpg", ".webp"})


def _extract_frames(
    ffmpeg: str,
    input_path: str,
    frames_dir: Path,
    *,
    start: float | None,
    end: float | None,
    should_stop: StopCb | None,
) -> None:
    frames_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(frames_dir / "%08d.png")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
    if start is not None and start > 0:
        cmd += ["-ss", f"{float(start):.6f}"]
    cmd += ["-i", input_path]
    if end is not None and start is not None:
        dur = float(end) - float(start)
        if dur > 0:
            cmd += ["-t", f"{dur:.6f}"]
    elif end is not None:
        cmd += ["-t", f"{float(end):.6f}"]
    cmd += ["-vsync", "0", "-q:v", "2", pattern]
    _run(cmd, label="Extract frames", should_stop=should_stop)


def _extract_audio(
    ffmpeg: str,
    input_path: str,
    audio_path: Path,
    *,
    start: float | None,
    end: float | None,
    should_stop: StopCb | None,
) -> bool:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
    if start is not None and start > 0:
        cmd += ["-ss", f"{float(start):.6f}"]
    cmd += ["-i", input_path]
    if end is not None and start is not None:
        dur = float(end) - float(start)
        if dur > 0:
            cmd += ["-t", f"{dur:.6f}"]
    elif end is not None:
        cmd += ["-t", f"{float(end):.6f}"]
    cmd += ["-vn", "-c:a", "aac", "-b:a", "192k", str(audio_path)]
    try:
        _run(cmd, label="Extract audio", should_stop=should_stop)
        return audio_path.is_file() and audio_path.stat().st_size > 0
    except RuntimeError as exc:
        if "aborted" in str(exc).lower():
            raise
        logging.info("[RIFE] No usable audio stream (%s)", exc)
        return False


def _run_rife_pass(
    exe: str,
    model_path: Path,
    input_dir: Path,
    output_dir: Path,
    *,
    uhd: bool,
    should_stop: StopCb | None,
) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        "-i",
        str(input_dir),
        "-o",
        str(output_dir),
        "-m",
        str(model_path),
        "-f",
        "%08d.png",
    ]
    if uhd:
        cmd.append("-u")
    # Working directory = exe folder so relative model paths / Vulkan DLLs resolve.
    logging.info("[RIFE] Interpolate: %s", " ".join(cmd))
    if should_stop and should_stop():
        raise RuntimeError("aborted")
    proc = subprocess.Popen(
        cmd,
        cwd=str(Path(exe).parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        startupinfo=_SUBPROCESS_STARTUPINFO,
    )
    try:
        while True:
            if should_stop and should_stop():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise RuntimeError("aborted")
            try:
                code = proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        stdout = (proc.stdout.read() if proc.stdout else "") or ""
        if code != 0:
            raise RuntimeError(
                f"rife-ncnn-vulkan failed (exit {code}).\n"
                f"{(stderr or stdout)[-1200:]}".strip()
            )
        if _count_frames(output_dir) < 2:
            raise RuntimeError("RIFE produced too few output frames.")
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass


def _encode_frames(
    ffmpeg: str,
    frames_dir: Path,
    output_path: str,
    *,
    fps: float,
    audio_path: Path | None,
    settings: dict[str, Any],
    should_stop: StopCb | None,
) -> None:
    from video_merge import _custom_audio_args, _custom_codec_args

    pattern = str(frames_dir / "%08d.png")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-nostdin",
        "-framerate",
        f"{fps:g}",
        "-i",
        pattern,
    ]
    has_audio = bool(audio_path and audio_path.is_file())
    if has_audio:
        cmd += ["-i", str(audio_path)]
    cmd += ["-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "1:a:0?", "-shortest"]
    cmd += _custom_codec_args(output_path, settings)
    if has_audio:
        cmd += _custom_audio_args(output_path, settings)
    else:
        cmd += ["-an"]
    cmd.append(output_path)
    _run(cmd, label="Encode interpolated video", should_stop=should_stop)


def remux_near_lossless(
    input_path: str,
    output_path: str,
    *,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
) -> dict[str, Any]:
    """
    Re-wrap decoded pixels as FFV1 (lossless) so the next step does not add
    another lossy generation. SeedVR's own mp4 is already one generation.
    """
    try:
        ffmpeg = _ffmpeg_bin()
    except Exception as exc:
        return {
            "ok": False,
            "output_path": None,
            "error": "ffmpeg_missing",
            "message": str(exc),
        }
    if progress_cb:
        try:
            progress_cb(0.0, "Writing near-lossless intermediate (FFV1)…")
        except Exception:
            pass
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-nostdin",
        "-i",
        input_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-g",
        "1",
        "-c:a",
        "copy",
        output_path,
    ]
    try:
        _run(cmd, label="FFV1 intermediate", should_stop=should_stop)
        if not os.path.isfile(output_path) or os.path.getsize(output_path) < 1000:
            return {
                "ok": False,
                "output_path": None,
                "error": "runtime_error",
                "message": "FFV1 intermediate was not created.",
            }
        return {"ok": True, "output_path": output_path, "error": None, "message": None}
    except RuntimeError as exc:
        msg = str(exc)
        if "aborted" in msg.lower():
            return {
                "ok": False,
                "output_path": None,
                "error": "aborted",
                "message": "Cancelled during FFV1 remux.",
            }
        return {
            "ok": False,
            "output_path": None,
            "error": "runtime_error",
            "message": msg,
        }


def seedvr_rife_final_path(
    input_path: str,
    *,
    output_dir: str | None = None,
    seedvr_suffix: str = "_seedvr2",
    multiplier: int = 2,
    mode: str = "fps",
) -> str:
    """Final delivery path for SeedVR → RIFE chain."""
    out_dir = output_dir or os.path.dirname(input_path)
    stem, ext = os.path.splitext(os.path.basename(input_path))
    mult = 4 if int(multiplier) == 4 else 2
    rife_tag = f"_rife{mult}x" if mode == "fps" else f"_rife_slowmo{mult}x"
    return os.path.join(out_dir, f"{stem}{seedvr_suffix}{rife_tag}{ext or '.mp4'}")


def interpolate_video(
    input_path: str,
    output_path: str,
    *,
    multiplier: int = 2,
    mode: str = "fps",
    start: float | None = None,
    end: float | None = None,
    include_audio: bool = True,
    model_name: str | None = None,
    uhd: bool | None = None,
    encode_settings: dict[str, Any] | None = None,
    progress_cb: ProgressCb | None = None,
    should_stop: StopCb | None = None,
) -> dict[str, Any]:
    """
    Interpolate a video (or segment) with rife-ncnn-vulkan.

    mode:
      - ``fps``: keep duration, raise FPS by multiplier (24→48 / 96)
      - ``slowmo``: keep source FPS, stretch duration by multiplier
    """
    status = runtime_status()
    if not status.get("ready"):
        return {
            "ok": False,
            "output_path": None,
            "error": status.get("error") or "rife_pack_missing",
            "message": status.get("message"),
        }

    exe = find_rife_exe()
    model_path = resolve_model_path(model_name)
    if not exe or model_path is None:
        return {
            "ok": False,
            "output_path": None,
            "error": "rife_pack_missing",
            "message": status.get("message"),
        }

    mult = int(multiplier) if int(multiplier) in (2, 4) else 2
    passes = 1 if mult == 2 else 2
    src_fps = _probe_fps(input_path)
    if mode == "slowmo":
        out_fps = src_fps
    else:
        out_fps = src_fps * mult

    settings = dict(encode_settings or {})
    settings.setdefault("ext", os.path.splitext(output_path)[1] or ".mp4")
    settings.setdefault("video_quality", "High")
    settings.setdefault("audio_bitrate", "192k")
    settings.setdefault("keep_size", True)

    # Auto UHD for long-edge >= 1440 unless caller overrides.
    if uhd is None:
        uhd = False
        try:
            ffprobe = _ffprobe_bin()
            cmd = [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                input_path,
            ]
            out = subprocess.check_output(
                cmd,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                startupinfo=_SUBPROCESS_STARTUPINFO,
            ).strip()
            if "x" in out:
                w_s, h_s = out.split("x", 1)
                w, h = int(w_s), int(h_s)
                uhd = max(w, h) >= 1440
        except Exception:
            uhd = False

    def _progress(pct: float, msg: str) -> None:
        if progress_cb:
            try:
                progress_cb(max(0.0, min(100.0, pct)), msg)
            except Exception:
                pass

    tmp_root = Path(
        tempfile.mkdtemp(prefix="vibe_rife_", dir=str(Path(output_path).resolve().parent))
    )
    try:
        ffmpeg = _ffmpeg_bin()
        in_frames = tmp_root / "in"
        out_frames = tmp_root / "out"
        audio_path = tmp_root / "audio.m4a"

        _progress(2, "Extracting frames…")
        _extract_frames(
            ffmpeg,
            input_path,
            in_frames,
            start=start,
            end=end,
            should_stop=should_stop,
        )
        n_in = _count_frames(in_frames)
        if n_in < 2:
            return {
                "ok": False,
                "output_path": None,
                "error": "too_few_frames",
                "message": "Need at least 2 frames to interpolate.",
            }

        have_audio = False
        if include_audio:
            _progress(8, "Extracting audio…")
            have_audio = _extract_audio(
                ffmpeg,
                input_path,
                audio_path,
                start=start,
                end=end,
                should_stop=should_stop,
            )

        current = in_frames
        for i in range(passes):
            target = out_frames if i == passes - 1 else (tmp_root / f"pass{i}")
            _progress(15 + i * 35, f"RIFE pass {i + 1}/{passes}…")
            _run_rife_pass(
                exe,
                model_path,
                current,
                target,
                uhd=bool(uhd),
                should_stop=should_stop,
            )
            if current is not in_frames and current.exists():
                shutil.rmtree(current, ignore_errors=True)
            current = target

        _progress(85, "Encoding video…")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        _encode_frames(
            ffmpeg,
            current,
            output_path,
            fps=out_fps,
            audio_path=audio_path if have_audio else None,
            settings=settings,
            should_stop=should_stop,
        )
        _progress(100, "Done")
        return {
            "ok": True,
            "output_path": output_path,
            "error": None,
            "message": None,
            "src_fps": src_fps,
            "out_fps": out_fps,
            "multiplier": mult,
            "mode": mode,
        }
    except RuntimeError as exc:
        msg = str(exc)
        if "aborted" in msg.lower():
            return {
                "ok": False,
                "output_path": None,
                "error": "aborted",
                "message": "RIFE cancelled.",
            }
        logging.exception("[RIFE] Pipeline failed")
        return {
            "ok": False,
            "output_path": None,
            "error": "runtime_error",
            "message": msg,
        }
    except Exception as exc:
        logging.exception("[RIFE] Pipeline failed")
        return {
            "ok": False,
            "output_path": None,
            "error": "runtime_error",
            "message": str(exc),
        }
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
