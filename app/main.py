"""
Application entry point for Vibe Player.

Configures faulthandler, logging, FFmpeg on ``PATH``, and Pyglet GC settings.
Launches either a lightweight viewer (Windows file-association \"Superfast Media Mode\")
or the full ``VideoThumbnailPlayer`` app — heavy imports happen only on the full-app path.
"""

import sys

# Frozen splash helper: second process runs only Tk+PIL, then exits (see ``video_thumbnail_player``).
if __name__ == "__main__" and getattr(sys, "frozen", False):
    for _i, _arg in enumerate(sys.argv):
        if _arg == "--vibe-splash" and _i + 1 < len(sys.argv):
            from splash_image import run_splash

            run_splash(sys.argv[_i + 1])
            raise SystemExit(0)

from logging_setup import setup_logging
from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS
import json
import os
import logging
import faulthandler
import signal
import multiprocessing
import subprocess

# --- Step 1: Save original stderr (console) before setup_logging replaces it with StreamToLogger ---
original_stderr = sys.stderr

# --- Step 2: Detect mode and configure logging ---
IS_HEADLESS = original_stderr is None
debug_mode = "--debug" in sys.argv
log_path = setup_logging(debug=debug_mode)

# Directory containing this file (``app/`` when running from source).
APP_DIR = os.path.dirname(os.path.abspath(__file__))

_SUPERFAST_SKIP_ARGS = frozenset({"--debug"})
_SUPERFAST_IMAGE_EXT = frozenset(IMAGE_FORMATS)
_SUPERFAST_VIDEO_EXT = frozenset(VIDEO_FORMATS)
_FILE_ASSOC_APP_NAME = "Vibe Player"
_FILE_ASSOC_APP_KEY = "VibePlayer.exe"
_FILE_ASSOC_DESCRIPTION = "Vibe Player media viewer"
_FILE_ASSOC_IMAGE_PROGID = "VibePlayer.Image"
_FILE_ASSOC_VIDEO_PROGID = "VibePlayer.Video"
_FILE_ASSOC_GROUPS = (
    (_FILE_ASSOC_IMAGE_PROGID, "Vibe Player Image", _SUPERFAST_IMAGE_EXT),
    (_FILE_ASSOC_VIDEO_PROGID, "Vibe Player Video", _SUPERFAST_VIDEO_EXT),
)

_WANTS_SUPERFAST_MEDIA = os.environ.get("VIBE_SUPERFAST_MEDIA", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

# --- Step 3: Determine faulthandler output target ---
faulthandler_output_file = None
log_file_handle = None

if IS_HEADLESS:
    try:
        log_file_handle = open(log_path, "a", encoding="utf-8")
        faulthandler_output_file = log_file_handle
        sys.stdout = log_file_handle
        sys.stderr = log_file_handle
        logging.info("pythonw.exe mode detected, stdout/stderr redirected to log file.")
    except Exception:
        pass
else:
    faulthandler_output_file = original_stderr

# --- Step 4: Enable faulthandler with correct output target ---
if faulthandler_output_file:
    try:
        faulthandler.enable(file=faulthandler_output_file)
        logging.info(
            "Faulthandler enabled successfully (writing to: %s).",
            "log file" if IS_HEADLESS else "original console",
        )
    except Exception as e:
        logging.error("Could not enable faulthandler: %s", e)


def handle_freeze_signal(signum, frame):
    """Handle SIGBREAK to dump thread tracebacks when freeze is detected."""
    logging.error("=" * 20 + " FREEZE DETECTED (SIGNAL) " + "=" * 20)
    if faulthandler_output_file:
        faulthandler.dump_traceback(file=faulthandler_output_file, all_threads=True)
    logging.error("=" * 20 + " THREAD STATE WRITTEN TO LOG " + "=" * 20)


try:
    signal.signal(signal.SIGBREAK, handle_freeze_signal)
except (AttributeError, ValueError):
    logging.warning(
        "Could not register SIGBREAK handler (not on Windows or not in console)."
    )


def _prepare_lightweight_env():
    """Pyglet GL safety + FFmpeg on PATH (same intent as full ``run_application``)."""
    try:
        import pyglet

        pyglet.options["garbage_collect"] = False
        logging.info("Pyglet garbage collection disabled (prevents access violation).")
    except ImportError:
        pass

    ffmpeg_bin_path = os.path.join(_runtime_tools_base_dir(), "tools", "ffmpeg", "bin")
    if os.path.isdir(ffmpeg_bin_path):
        os.environ["PATH"] = ffmpeg_bin_path + os.pathsep + os.environ["PATH"]
        logging.info("FFmpeg path successfully added to PATH: %s", ffmpeg_bin_path)
    else:
        logging.warning("FFmpeg path not found at: %s", ffmpeg_bin_path)


def _runtime_base_dir() -> str:
    """Directory containing bundled runtime assets such as icons and tools."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return APP_DIR


def _source_project_root() -> str:
    """Repository root when running from source."""
    return os.path.dirname(APP_DIR)


def _runtime_tools_base_dir() -> str:
    """Directory containing bundled external tools."""
    if getattr(sys, "frozen", False):
        return _runtime_base_dir()
    return _source_project_root()


def _load_fast_media_prefs():
    """Read VLC-related keys from ``settings.json`` without loading the main window."""
    path = os.path.join(APP_DIR, "settings.json")
    out = {
        "video_output": "direct3d11",
        "audio_output": "default",
        "hardware_decoding": "dxva2",
        "audio_device": "",
        "auto_play": True,
        "video_show_hud": True,
        "gpu_upscale": False,
        "vlc_enable_postproc": False,
        "vlc_postproc_quality": 6,
        "vlc_enable_gradfun": False,
        "vlc_enable_deinterlace": False,
        "vlc_skiploopfilter_disable": False,
        "image_viewer_use_pyglet": False,
    }
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in out:
                if key in data:
                    out[key] = data[key]
    except Exception as exc:
        logging.warning("Superfast mode: could not read settings.json (%s); using defaults.", exc)
    return out


def _parse_cli_media_path(argv):
    """Return first non-flag argument after ``argv[0]``, if any."""
    args = [a for a in argv[1:] if a not in _SUPERFAST_SKIP_ARGS]
    return args[0] if args else None


def _windows_quote(value: str) -> str:
    """Quote one Windows command argument for registry open commands."""
    return '"' + value.replace('"', '\\"') + '"'


def _file_association_command() -> tuple[str, str]:
    """
    Return ``(open_command, icon_target)`` for the current runtime.

    Frozen builds register ``VibePlayer.exe``. Source runs register ``run.bat`` so
    development builds can be tested from Explorer without changing the final path.
    """
    if getattr(sys, "frozen", False):
        target = os.path.abspath(sys.executable)
        return f"{_windows_quote(target)} \"%1\"", target

    project_root = _source_project_root()
    run_bat = os.path.join(project_root, "run.bat")
    if os.path.isfile(run_bat):
        target = os.path.abspath(run_bat)
        return f"{_windows_quote(target)} \"%1\"", target

    main_py = os.path.join(APP_DIR, "main.py")
    target = os.path.abspath(sys.executable)
    return f"{_windows_quote(target)} {_windows_quote(main_py)} \"%1\"", target


def _reg_set_sz(winreg, root, path: str, name: str, value: str) -> None:
    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _reg_create_key(winreg, root, path: str) -> None:
    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_WRITE):
        pass


def _reg_delete_value(winreg, root, path: str, name: str) -> None:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _reg_delete_tree(winreg, root, path: str) -> None:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _reg_delete_tree(winreg, root, path + "\\" + child)
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _register_windows_file_associations() -> int:
    """Register Vibe Player as an Open With/default-app candidate for media files."""
    if os.name != "nt":
        print("File association registration is only supported on Windows.")
        return 1

    import winreg

    classes_root = r"Software\Classes"
    app_path = classes_root + r"\Applications" + "\\" + _FILE_ASSOC_APP_KEY
    capabilities_path = app_path + r"\Capabilities"
    command, icon_target = _file_association_command()
    icon_value = f"{_windows_quote(icon_target)},0"

    try:
        _reg_set_sz(winreg, winreg.HKEY_CURRENT_USER, app_path, "FriendlyAppName", _FILE_ASSOC_APP_NAME)
        _reg_set_sz(
            winreg,
            winreg.HKEY_CURRENT_USER,
            app_path + r"\shell\open\command",
            "",
            command,
        )
        _reg_set_sz(
            winreg,
            winreg.HKEY_CURRENT_USER,
            capabilities_path,
            "ApplicationName",
            _FILE_ASSOC_APP_NAME,
        )
        _reg_set_sz(
            winreg,
            winreg.HKEY_CURRENT_USER,
            capabilities_path,
            "ApplicationDescription",
            _FILE_ASSOC_DESCRIPTION,
        )
        _reg_set_sz(
            winreg,
            winreg.HKEY_CURRENT_USER,
            capabilities_path,
            "ApplicationIcon",
            icon_value,
        )

        for progid, label, extensions in _FILE_ASSOC_GROUPS:
            progid_path = classes_root + "\\" + progid
            _reg_set_sz(winreg, winreg.HKEY_CURRENT_USER, progid_path, "", label)
            _reg_set_sz(
                winreg,
                winreg.HKEY_CURRENT_USER,
                progid_path + r"\DefaultIcon",
                "",
                icon_value,
            )
            _reg_set_sz(
                winreg,
                winreg.HKEY_CURRENT_USER,
                progid_path + r"\shell\open\command",
                "",
                command,
            )

            for ext in sorted(extensions):
                _reg_set_sz(
                    winreg,
                    winreg.HKEY_CURRENT_USER,
                    capabilities_path + r"\FileAssociations",
                    ext,
                    progid,
                )
                _reg_set_sz(
                    winreg,
                    winreg.HKEY_CURRENT_USER,
                    app_path + r"\SupportedTypes",
                    ext,
                    "",
                )
                _reg_set_sz(
                    winreg,
                    winreg.HKEY_CURRENT_USER,
                    progid_path + r"\SupportedTypes",
                    ext,
                    "",
                )
                _reg_set_sz(
                    winreg,
                    winreg.HKEY_CURRENT_USER,
                    classes_root + "\\" + ext + r"\OpenWithProgids",
                    progid,
                    "",
                )
                _reg_create_key(
                    winreg,
                    winreg.HKEY_CURRENT_USER,
                    classes_root + "\\" + ext + r"\OpenWithList" + "\\" + _FILE_ASSOC_APP_KEY,
                )

        _reg_set_sz(
            winreg,
            winreg.HKEY_CURRENT_USER,
            r"Software\RegisteredApplications",
            _FILE_ASSOC_APP_NAME,
            r"Software\Classes\Applications" + "\\" + _FILE_ASSOC_APP_KEY + r"\Capabilities",
        )
    except OSError as exc:
        logging.exception("Failed to register Windows file associations.")
        print(f"Failed to register Windows file associations: {exc}")
        return 1

    print("Vibe Player file associations registered.")
    return 0


def _unregister_windows_file_associations() -> int:
    """Remove registry keys written by ``_register_windows_file_associations``."""
    if os.name != "nt":
        print("File association unregistration is only supported on Windows.")
        return 1

    import winreg

    classes_root = r"Software\Classes"
    try:
        for progid, _, extensions in _FILE_ASSOC_GROUPS:
            for ext in sorted(extensions):
                ext_path = classes_root + "\\" + ext
                _reg_delete_value(
                    winreg,
                    winreg.HKEY_CURRENT_USER,
                    ext_path + r"\OpenWithProgids",
                    progid,
                )
                _reg_delete_tree(
                    winreg,
                    winreg.HKEY_CURRENT_USER,
                    ext_path + r"\OpenWithList" + "\\" + _FILE_ASSOC_APP_KEY,
                )
            _reg_delete_tree(winreg, winreg.HKEY_CURRENT_USER, classes_root + "\\" + progid)

        _reg_delete_tree(
            winreg,
            winreg.HKEY_CURRENT_USER,
            classes_root + r"\Applications" + "\\" + _FILE_ASSOC_APP_KEY,
        )
        _reg_delete_value(
            winreg,
            winreg.HKEY_CURRENT_USER,
            r"Software\RegisteredApplications",
            _FILE_ASSOC_APP_NAME,
        )
    except OSError as exc:
        logging.exception("Failed to unregister Windows file associations.")
        print(f"Failed to unregister Windows file associations: {exc}")
        return 1

    print("Vibe Player file associations removed.")
    return 0


def _handle_file_association_cli() -> None:
    """Handle maintenance commands before media/open-app startup logic runs."""
    if "--register-file-associations" in sys.argv:
        raise SystemExit(_register_windows_file_associations())
    if "--unregister-file-associations" in sys.argv:
        raise SystemExit(_unregister_windows_file_associations())


class _FastVideoController:
    """Minimal stand-in for ``VideoThumbnailPlayer`` when only ``VideoPlayer`` is used."""

    def __init__(self, root, prefs):
        self._root = root
        self.current_video_window = None
        self.current_volume = 100
        self.video_files = []
        self.current_video_index = 0
        self.video_show_hud = bool(prefs.get("video_show_hud", True))
        self.vlc_enable_postproc = bool(prefs.get("vlc_enable_postproc", False))
        self.vlc_postproc_quality = int(prefs.get("vlc_postproc_quality", 6))
        self.vlc_enable_gradfun = bool(prefs.get("vlc_enable_gradfun", False))
        self.vlc_enable_deinterlace = bool(prefs.get("vlc_enable_deinterlace", False))
        self.vlc_skiploopfilter_disable = bool(prefs.get("vlc_skiploopfilter_disable", False))
        self.BackroundColor = "#2b2b2b"
        self.thumb_TextColor = "white"

    def set_loop_start_shortcut(self, event=None):
        win = self.current_video_window
        if win:
            win.set_loop_start()

    def set_loop_end_shortcut(self, event=None):
        win = self.current_video_window
        if win:
            win.set_loop_end()

    def toggle_loop_shortcut(self, event=None):
        win = self.current_video_window
        if win:
            win.toggle_loop()

    def update_current_volume(self, volume):
        self.current_volume = int(volume)

    def _focus_back_after_dialog(self):
        try:
            self._root.quit()
        except Exception:
            pass

    def after(self, ms, cb):
        return self._root.after(ms, cb)

    def Open_playlist(self):
        pass

    def add_selected_to_playlist(self, new_playlist=False):
        pass

    def open_library(self):
        """Launch the full browser app from a lightweight file-association viewer."""
        env = os.environ.copy()
        env["VIBE_SUPERFAST_MEDIA"] = "0"
        project_root = _source_project_root()

        if getattr(sys, "frozen", False):
            cmd = [sys.executable]
            cwd = os.path.dirname(os.path.abspath(sys.executable))
        else:
            run_bat = os.path.join(project_root, "run.bat")
            if os.path.isfile(run_bat):
                cmd = [run_bat]
                cwd = project_root
            else:
                cmd = [sys.executable, os.path.join(APP_DIR, "main.py")]
                cwd = APP_DIR

        try:
            subprocess.Popen(cmd, cwd=cwd, env=env)
        except Exception as exc:
            logging.exception("Could not launch full Vibe Player library.")
            self.universal_dialog(
                "Open Library",
                f"Could not open the full Vibe Player library:\n\n{exc}",
                show_cancel=False,
            )

    def universal_dialog(
        self,
        title,
        message,
        confirm_callback=None,
        cancel_callback=None,
        third_button=None,
        third_callback=None,
        input_field=False,
        default_input="",
        confirm_text="Confirm",
        cancel_text="Cancel",
        show_cancel=True,
    ):
        from tkinter import messagebox, simpledialog

        if input_field:
            val = simpledialog.askstring(
                title, message, initialvalue=default_input, parent=self._root
            )
            if val is not None and confirm_callback:
                confirm_callback(val)
            return
        if messagebox.askokcancel(title, message, parent=self._root):
            if confirm_callback:
                confirm_callback()


def _run_superfast_image(media_path: str) -> None:
    # --- SUPERFAST IMAGE: legacy Tk or Pyglet viewer + minimal CTk ---
    import customtkinter as ctk
    from tkinter import messagebox
    from image_operations import create_image_viewer

    class _FastImageRoot(ctk.CTk):
        """Image viewer uses the same object as ``parent`` and ``controller`` — handlers live here."""

        def __init__(self):
            super().__init__()
            self.title("")
            self.geometry("1x1")
            self.withdraw()
            self.video_files = []
            self.hotkeys_map = {}
            self._viewer = None

        def confirm_delete_item(self, item_ids=None, paths=None):
            paths = [p for p in (paths or []) if p and os.path.isfile(p)]
            if not paths:
                return
            detail = os.path.basename(paths[0])
            if not messagebox.askyesno(
                "Delete", f"Permanently delete this file?\n\n{detail}", parent=self
            ):
                return
            try:
                os.remove(paths[0])
            except OSError as exc:
                messagebox.showerror("Delete failed", str(exc), parent=self)
                return
            if self._viewer is not None:
                self._viewer._do_close()

    os.chdir(APP_DIR)
    _prepare_lightweight_env()
    prefs = _load_fast_media_prefs()
    use_pyglet = bool(prefs.get("image_viewer_use_pyglet", False))
    root = _FastImageRoot()
    name = os.path.basename(media_path)
    viewer = create_image_viewer(root, media_path, name, use_pyglet)
    root._viewer = viewer

    def poll_close():
        v = root._viewer
        if v is not None and not v._running:
            root.after(80, root.quit)
            return
        root.after(200, poll_close)

    root.after(250, poll_close)
    root.mainloop()


def _run_superfast_video(media_path: str) -> None:
    # --- SUPERFAST VIDEO: VLC player + minimal CTk; no database / grid / VideoThumbnailPlayer ---
    os.chdir(APP_DIR)
    _prepare_lightweight_env()
    import customtkinter as ctk
    from video_operations import VideoPlayer

    prefs = _load_fast_media_prefs()
    root = ctk.CTk()
    root.default_directory = _runtime_base_dir()
    root.withdraw()
    stub = _FastVideoController(root, prefs)
    vp = VideoPlayer(
        parent=root,
        controller=stub,
        video_path=media_path,
        video_name=os.path.basename(media_path),
        initial_volume=stub.current_volume,
        vlc_video_output=str(prefs["video_output"]),
        vlc_audio_output=str(prefs["audio_output"]),
        vlc_hw_decoding=str(prefs["hardware_decoding"]),
        vlc_audio_device=str(prefs["audio_device"] or ""),
        auto_play=bool(prefs.get("auto_play", True)),
        subtitles_enabled=False,
        playlist_manager=None,
        embed=False,
        show_video_button_bar=True,
        use_gpu_upscale=bool(prefs.get("gpu_upscale", False)),
    )
    stub.current_video_window = vp

    def start_player():
        # VLC needs a real mapped HWND; starting too early can leave a black surface.
        try:
            vp.video_window.update_idletasks()
            vp.video_window.update()
        except Exception:
            pass
        vp.show_and_play()

    root.after(180, start_player)
    root.mainloop()


def _try_superfast_media_mode():
    """
    If argv points at one supported image/video file, run the lightweight viewer only.
    Returns True if this process should exit after this function (superfast handled).
    """
    if not _WANTS_SUPERFAST_MEDIA:
        return False
    raw = _parse_cli_media_path(sys.argv)
    if not raw:
        return False
    abs_path = os.path.abspath(os.path.normpath(raw))
    if not os.path.isfile(abs_path):
        return False
    ext = os.path.splitext(abs_path)[1].lower()
    handled_media_ext = ext in _SUPERFAST_IMAGE_EXT or ext in _SUPERFAST_VIDEO_EXT
    try:
        if ext in _SUPERFAST_IMAGE_EXT:
            logging.info("Superfast Media Mode (image): %s", abs_path)
            _run_superfast_image(abs_path)
            return True
        if ext in _SUPERFAST_VIDEO_EXT:
            logging.info("Superfast Media Mode (video): %s", abs_path)
            _run_superfast_video(abs_path)
            return True
    except Exception:
        logging.exception("Superfast Media Mode failed; not starting full browser fallback.")
        if handled_media_ext:
            try:
                from tkinter import messagebox

                messagebox.showerror(
                    "Vibe Player",
                    "Could not open this media file in the lightweight viewer.\n\n"
                    "The full library was not started automatically.",
                )
            except Exception:
                pass
            return True
        return False
    return False


# --- Main application entry point ---
def run_application():
    """
    Initialize the application: Pyglet/OpenGL fix, FFmpeg path, and launch main window.
    """
    from video_thumbnail_player import VideoThumbnailPlayer

    _prepare_lightweight_env()
    # Match superfast modes: cwd = ``app/`` so ``settings.json`` and paths resolve consistently.
    os.chdir(APP_DIR)

    debug_mode = "--debug" in sys.argv
    log_path_inner = setup_logging(debug=debug_mode)

    app = VideoThumbnailPlayer(log_path=log_path_inner)
    app.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _handle_file_association_cli()
    if _try_superfast_media_mode():
        sys.exit(0)
    run_application()
