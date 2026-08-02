"""
Open files in external applications (FastStone-style).

- Enumerate Windows "Open with" handlers for a file extension (Win7–11)
- Persist a user-customized program list in settings.json
- Build Tk context-menu cascades and a manage dialog
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox

SETTINGS_FILENAME = "settings.json"
SETTINGS_KEY = "external_apps"

# Skip our own handlers so "Open in external app" does not relaunch Vibe.
_SELF_EXE_NAMES = frozenset(
    {
        "vibeplayer.exe",
        "vlc_player.exe",
        "run.bat",
        "python.exe",
        "pythonw.exe",
    }
)
_SELF_PROGIDS = frozenset(
    {
        "vibeplayer.image",
        "vibeplayer.video",
        "vibeplayer.exe",
    }
)

# Caches — association lookup hits registry + AssocQueryString; keep off the RMB hot path.
_assoc_cache: dict[str, list["ExternalApp"]] = {}
_custom_apps_cache: list["ExternalApp"] | None = None
_custom_apps_mtime: float | None = None
_app_paths_cache: dict[str, str | None] = {}
_assoc_string_cache: dict[tuple[int, str], str | None] = {}
_friendly_name_cache: dict[str, str] = {}

# AssocQueryString
_ASSOCSTR_COMMAND = 1
_ASSOCSTR_EXECUTABLE = 2
_ASSOCSTR_FRIENDLYAPPNAME = 4
_ASSOCF_NONE = 0
_ASSOCF_INIT_IGNOREUNKNOWN = 0x00000400


@dataclass(frozen=True)
class ExternalApp:
    """One launchable external program."""

    name: str
    exe: str
    source: str = "custom"  # "custom" | "associated" | "default"

    def key(self) -> str:
        return os.path.normcase(os.path.normpath(self.exe))


def settings_path() -> Path:
    return Path(SETTINGS_FILENAME).resolve()


def clear_association_cache() -> None:
    """Drop cached Open-with handlers (e.g. after OS default-app changes)."""
    _assoc_cache.clear()
    _assoc_string_cache.clear()
    _app_paths_cache.clear()
    _friendly_name_cache.clear()


def load_custom_apps() -> list[ExternalApp]:
    global _custom_apps_cache, _custom_apps_mtime
    path = settings_path()
    try:
        mtime = path.stat().st_mtime if path.is_file() else None
    except OSError:
        mtime = None
    if _custom_apps_cache is not None and _custom_apps_mtime == mtime:
        return list(_custom_apps_cache)

    if not path.is_file():
        _custom_apps_cache = []
        _custom_apps_mtime = mtime
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logging.warning("external_apps: failed to read settings: %s", exc)
        return []
    raw = data.get(SETTINGS_KEY) if isinstance(data, dict) else None
    if not isinstance(raw, list):
        _custom_apps_cache = []
        _custom_apps_mtime = mtime
        return []
    apps: list[ExternalApp] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        exe = str(item.get("exe") or item.get("path") or "").strip()
        if not exe or not os.path.isfile(exe):
            continue
        key = os.path.normcase(os.path.normpath(exe))
        if key in seen:
            continue
        seen.add(key)
        name = str(item.get("name") or "").strip() or _friendly_name_from_exe(exe)
        apps.append(ExternalApp(name=name, exe=exe, source="custom"))
    _custom_apps_cache = apps
    _custom_apps_mtime = mtime
    return list(apps)


def save_custom_apps(apps: Iterable[ExternalApp]) -> None:
    global _custom_apps_cache, _custom_apps_mtime
    path = settings_path()
    try:
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}
    except Exception:
        data = {}

    payload = []
    seen: set[str] = set()
    saved: list[ExternalApp] = []
    for app in apps:
        exe = str(getattr(app, "exe", "") or "").strip()
        if not exe:
            continue
        key = os.path.normcase(os.path.normpath(exe))
        if key in seen:
            continue
        seen.add(key)
        name = str(getattr(app, "name", "") or "").strip() or _friendly_name_from_exe(exe)
        norm = os.path.normpath(exe)
        payload.append({"name": name, "exe": norm})
        saved.append(ExternalApp(name=name, exe=norm, source="custom"))

    data[SETTINGS_KEY] = payload
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        logging.error("external_apps: failed to save settings: %s", exc)
        raise
    try:
        _custom_apps_mtime = path.stat().st_mtime
    except OSError:
        _custom_apps_mtime = None
    _custom_apps_cache = saved


def _friendly_name_from_exe(exe: str) -> str:
    base = os.path.splitext(os.path.basename(exe))[0]
    return base.replace("_", " ").replace("-", " ").strip() or exe


def _is_self_handler(name: str = "", exe: str = "", progid: str = "") -> bool:
    if progid and progid.strip().lower() in _SELF_PROGIDS:
        return True
    exe_base = os.path.basename(exe).lower() if exe else ""
    if exe_base in _SELF_EXE_NAMES:
        return True
    # Frozen build may be named differently; also match Vibe Player friendly name.
    if name and "vibe player" in name.lower():
        return True
    return False


def _assoc_query_string(assoc_str: int, assoc: str) -> str | None:
    """Query shell association string (WinXP+). Returns None on failure."""
    if sys.platform != "win32":
        return None
    cache_key = (assoc_str, assoc.lower())
    if cache_key in _assoc_string_cache:
        return _assoc_string_cache[cache_key]
    try:
        from ctypes import byref, c_wchar_p, create_unicode_buffer, windll, wintypes

        buf_size = wintypes.DWORD(0)
        flags = _ASSOCF_INIT_IGNOREUNKNOWN
        # First call: required buffer size
        hr = windll.shlwapi.AssocQueryStringW(
            flags,
            assoc_str,
            c_wchar_p(assoc),
            None,
            None,
            byref(buf_size),
        )
        # S_FALSE (1) = buffer too small / size query OK; S_OK (0) unlikely without buffer
        if buf_size.value <= 1:
            _assoc_string_cache[cache_key] = None
            return None
        buf = create_unicode_buffer(buf_size.value)
        hr = windll.shlwapi.AssocQueryStringW(
            flags,
            assoc_str,
            c_wchar_p(assoc),
            None,
            buf,
            byref(buf_size),
        )
        if hr != 0:
            _assoc_string_cache[cache_key] = None
            return None
        value = buf.value.strip()
        result = value or None
        _assoc_string_cache[cache_key] = result
        return result
    except Exception as exc:
        logging.debug("AssocQueryString(%s) failed: %s", assoc, exc)
        _assoc_string_cache[cache_key] = None
        return None


def _reg_enum_values(root, path: str) -> dict[str, str]:
    import winreg

    out: dict[str, str] = {}
    try:
        with winreg.OpenKey(root, path) as key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if name is None or name == "":
                    continue
                # OpenWithProgids often uses REG_NONE (empty); keep the value *name*.
                out[str(name)] = value if isinstance(value, str) else ""
    except OSError:
        pass
    return out


def _reg_enum_subkeys(root, path: str) -> list[str]:
    import winreg

    out: list[str] = []
    try:
        with winreg.OpenKey(root, path) as key:
            i = 0
            while True:
                try:
                    out.append(winreg.EnumKey(key, i))
                except OSError:
                    break
                i += 1
    except OSError:
        pass
    return out


def _reg_get_sz(root, path: str, value_name: str | None = None) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            val, _ = winreg.QueryValueEx(key, value_name)
            if isinstance(val, str) and val.strip():
                return val.strip()
    except OSError:
        pass
    return None


def _resolve_app_paths(exe_name: str) -> str | None:
    """Resolve short exe name via App Paths (HKCU then HKLM)."""
    if not exe_name:
        return None
    cache_key = exe_name.lower()
    if cache_key in _app_paths_cache:
        return _app_paths_cache[cache_key]
    if os.path.isfile(exe_name):
        result = os.path.normpath(exe_name)
        _app_paths_cache[cache_key] = result
        return result
    import winreg

    name = os.path.basename(exe_name)
    result: str | None = None
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        path = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{name}"
        full = _reg_get_sz(root, path, None) or _reg_get_sz(root, path, "")
        if full and os.path.isfile(full):
            result = os.path.normpath(full)
            break
        # Wow6432Node on 64-bit Windows
        path32 = rf"Software\Wow6432Node\Microsoft\Windows\CurrentVersion\App Paths\{name}"
        full = _reg_get_sz(root, path32, None) or _reg_get_sz(root, path32, "")
        if full and os.path.isfile(full):
            result = os.path.normpath(full)
            break
    if result is None:
        # PATH lookup
        try:
            import shutil

            found = shutil.which(name)
            if found and os.path.isfile(found):
                result = os.path.normpath(found)
        except Exception:
            pass
    _app_paths_cache[cache_key] = result
    return result


def _exe_from_command(command: str) -> str | None:
    """Extract executable path from a shell open command string."""
    if not command:
        return None
    cmd = command.strip()
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end > 1:
            candidate = cmd[1:end]
            if os.path.isfile(candidate):
                return os.path.normpath(candidate)
            return _resolve_app_paths(candidate)
    # Unquoted: first token
    token = cmd.split(" ", 1)[0].strip()
    if token.lower().startswith("rundll32"):
        return None
    if os.path.isfile(token):
        return os.path.normpath(token)
    return _resolve_app_paths(token)


def _progid_to_app(progid: str) -> ExternalApp | None:
    if not progid or _is_self_handler(progid=progid):
        return None
    import winreg

    command = _reg_get_sz(winreg.HKEY_CLASSES_ROOT, rf"{progid}\shell\open\command", None)
    if not command:
        command = _reg_get_sz(winreg.HKEY_CLASSES_ROOT, rf"{progid}\shell\edit\command", None)
    exe = _exe_from_command(command or "") if command else None
    if not exe or _is_self_handler(exe=exe, progid=progid):
        return None
    friendly = (
        _assoc_query_string(_ASSOCSTR_FRIENDLYAPPNAME, progid)
        or _reg_get_sz(winreg.HKEY_CLASSES_ROOT, progid, None)
        or _friendly_name_from_exe(exe)
    )
    if _is_self_handler(name=friendly, exe=exe, progid=progid):
        return None
    return ExternalApp(name=friendly, exe=exe, source="associated")


def _apps_from_open_with_list(ext: str) -> list[ExternalApp]:
    """Read Explorer FileExts OpenWithList (MRU of .exe names)."""
    import winreg

    ext = ext if ext.startswith(".") else f".{ext}"
    apps: list[ExternalApp] = []
    path = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{ext}\OpenWithList"
    values = _reg_enum_values(winreg.HKEY_CURRENT_USER, path)
    mru = values.get("MRUList", "")
    ordered_keys = list(mru) if mru else sorted(k for k in values if len(k) == 1)
    for key in ordered_keys:
        exe_name = values.get(key)
        if not exe_name or not exe_name.lower().endswith(".exe"):
            continue
        if _is_self_handler(exe=exe_name):
            continue
        full = _resolve_app_paths(exe_name)
        if not full:
            continue
        friendly = (
            _assoc_query_string(_ASSOCSTR_FRIENDLYAPPNAME, exe_name)
            or _friendly_name_from_exe(full)
        )
        if _is_self_handler(name=friendly, exe=full):
            continue
        apps.append(ExternalApp(name=friendly, exe=full, source="associated"))
    return apps


def _apps_from_open_with_progids(ext: str) -> list[ExternalApp]:
    import winreg

    ext = ext if ext.startswith(".") else f".{ext}"
    apps: list[ExternalApp] = []
    for root, base in (
        (
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{ext}\OpenWithProgids",
        ),
        (winreg.HKEY_CLASSES_ROOT, rf"{ext}\OpenWithProgids"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}\OpenWithProgids"),
    ):
        values = _reg_enum_values(root, base)
        # OpenWithProgids often stores empty REG_NONE values — keys are the ProgIDs
        progids = list(values.keys()) if values else _reg_enum_subkeys(root, base)
        # Also try EnumValue names when values dict is empty but key exists
        if not progids:
            try:
                with winreg.OpenKey(root, base) as key:
                    i = 0
                    while True:
                        try:
                            name, _, _ = winreg.EnumValue(key, i)
                            if name:
                                progids.append(name)
                        except OSError:
                            break
                        i += 1
            except OSError:
                pass
        for progid in progids:
            app = _progid_to_app(progid)
            if app:
                apps.append(app)
    return apps


def _default_app_for_ext(ext: str) -> ExternalApp | None:
    ext = ext if ext.startswith(".") else f".{ext}"
    exe = _assoc_query_string(_ASSOCSTR_EXECUTABLE, ext)
    if not exe or not os.path.isfile(exe) or _is_self_handler(exe=exe):
        # Fallback: ProgID default
        import winreg

        progid = _reg_get_sz(winreg.HKEY_CLASSES_ROOT, ext, None)
        if progid:
            return _progid_to_app(progid)
        return None
    friendly = (
        _assoc_query_string(_ASSOCSTR_FRIENDLYAPPNAME, ext)
        or _friendly_name_from_exe(exe)
    )
    if _is_self_handler(name=friendly, exe=exe):
        return None
    return ExternalApp(name=friendly, exe=exe, source="default")


def get_associated_apps(file_path: str) -> list[ExternalApp]:
    """
    Programs Windows knows for this file's extension.

    Combines default handler, OpenWithList, and OpenWithProgids.
    Works on Windows 7 through Windows 11 (registry + AssocQueryString).
    Results are cached per extension so RMB stays snappy.
    """
    if sys.platform != "win32":
        return []
    ext = os.path.splitext(file_path)[1].lower()
    if not ext:
        return []
    cached = _assoc_cache.get(ext)
    if cached is not None:
        return list(cached)

    ordered: list[ExternalApp] = []
    seen: set[str] = set()

    def _add(app: ExternalApp | None) -> None:
        if app is None:
            return
        key = app.key()
        if key in seen:
            return
        if not os.path.isfile(app.exe):
            return
        seen.add(key)
        ordered.append(app)

    try:
        _add(_default_app_for_ext(ext))
        for app in _apps_from_open_with_list(ext):
            _add(app)
        for app in _apps_from_open_with_progids(ext):
            _add(app)
    except Exception as exc:
        logging.warning("external_apps: association lookup failed for %s: %s", ext, exc)

    _assoc_cache[ext] = ordered
    return list(ordered)


def open_with_app(file_path: str, exe: str) -> None:
    """Launch ``exe`` with ``file_path`` as argument."""
    if not file_path or not os.path.exists(file_path):
        raise FileNotFoundError(file_path)
    if not exe or not os.path.isfile(exe):
        raise FileNotFoundError(exe)
    creationflags = 0
    if sys.platform == "win32":
        # Detach GUI apps so closing Vibe does not kill them.
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    subprocess.Popen(  # noqa: S603
        [exe, file_path],
        close_fds=True,
        creationflags=creationflags,
        cwd=os.path.dirname(exe) or None,
    )


def open_with_default_app(file_path: str) -> None:
    """Open with the Windows default association (may be Vibe if registered)."""
    if sys.platform == "win32":
        os.startfile(file_path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", file_path])  # noqa: S603


def open_with_system_picker(file_path: str) -> None:
    """Show the Windows 'Open with' dialog (ShellExecute openas)."""
    if sys.platform != "win32":
        open_with_default_app(file_path)
        return
    from ctypes import windll

    # openas = system app picker; works WinVista+
    rc = windll.shell32.ShellExecuteW(None, "openas", file_path, None, None, 1)
    if rc <= 32:
        # Fallback: openas sometimes fails on older shells — try rundll32
        subprocess.Popen(  # noqa: S603
            ["rundll32", "shell32.dll,OpenAs_RunDLL", file_path],
            close_fds=True,
        )


def _populate_external_apps_submenu(
    sub: tk.Menu,
    parent: tk.Misc,
    file_path: str,
) -> None:
    """Fill submenu entries (idempotent — clears first)."""
    try:
        sub.delete(0, tk.END)
    except tk.TclError:
        pass

    associated = get_associated_apps(file_path)
    custom = load_custom_apps()
    assoc_keys = {a.key() for a in associated}
    custom_only = [a for a in custom if a.key() not in assoc_keys]

    if associated:
        for app in associated:
            sub.add_command(
                label=app.name,
                command=lambda e=app.exe, fp=file_path: _safe_open(parent, fp, e),
            )
        if custom_only:
            sub.add_separator()

    if custom_only:
        for app in custom_only:
            sub.add_command(
                label=app.name,
                command=lambda e=app.exe, fp=file_path: _safe_open(parent, fp, e),
            )

    if associated or custom_only:
        sub.add_separator()

    sub.add_command(
        label="Open with Default App",
        command=lambda fp=file_path: _safe_default(parent, fp),
    )
    sub.add_command(
        label="Choose Program…",
        command=lambda fp=file_path: _safe_picker(parent, fp),
    )
    sub.add_separator()
    sub.add_command(
        label="Add or Remove Programs…",
        command=lambda: open_manage_external_apps_dialog(parent),
    )


def append_external_apps_cascade(
    menu: tk.Menu,
    parent: tk.Misc,
    file_path: str,
    *,
    label: str = "Open in External App",
    prepare_submenu: Callable[[tk.Menu], Any] | None = None,
) -> tk.Menu:
    """
    Add a FastStone-style submenu to a standard ``tk.Menu``.

    Association lookup runs lazily via ``postcommand`` when the user opens the
    cascade — so RMB itself stays instant.
    """
    sub = tk.Menu(menu, tearoff=0)
    if prepare_submenu is not None:
        prepare_submenu(sub)

    # Placeholder so the cascade is enabled before first open.
    sub.add_command(label="…", state=tk.DISABLED)

    def _on_post(fp: str = file_path, m: tk.Menu = sub, p: tk.Misc = parent) -> None:
        _populate_external_apps_submenu(m, p, fp)

    sub.configure(postcommand=_on_post)
    menu.add_cascade(label=label, menu=sub)
    return sub


def append_external_apps_flat_commands(
    menu: Any,
    parent: tk.Misc,
    file_path: str,
    *,
    max_apps: int = 6,
) -> None:
    """
    Flat items for menus without cascade support (``CTkFlatContextMenu``).

    Prefer cached associations; on a cold cache use custom apps + picker only
    and warm the association list in the background for the next open.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext and ext not in _assoc_cache:
        try:
            parent.after(1, lambda fp=file_path: get_associated_apps(fp))
        except Exception:
            pass
        associated: list[ExternalApp] = []
    else:
        associated = get_associated_apps(file_path) if ext else []

    custom = load_custom_apps()
    seen: set[str] = set()
    apps: list[ExternalApp] = []
    for app in associated + custom:
        if app.key() in seen:
            continue
        seen.add(app.key())
        apps.append(app)
        if len(apps) >= max_apps:
            break

    if apps:
        menu.add_separator()
        for app in apps:
            menu.add_command(
                label=f"Open with {app.name}",
                command=lambda e=app.exe, fp=file_path: _safe_open(parent, fp, e),
            )
        menu.add_command(
            label="Choose Program…",
            command=lambda fp=file_path: _safe_picker(parent, fp),
        )
        menu.add_command(
            label="Add or Remove Programs…",
            command=lambda: open_manage_external_apps_dialog(parent),
        )
    else:
        menu.add_separator()
        menu.add_command(
            label="Open in External App…",
            command=lambda fp=file_path: _safe_picker(parent, fp),
        )
        menu.add_command(
            label="Add or Remove Programs…",
            command=lambda: open_manage_external_apps_dialog(parent),
        )


def _safe_open(parent: tk.Misc, file_path: str, exe: str) -> None:
    try:
        open_with_app(file_path, exe)
    except Exception as exc:
        messagebox.showerror(
            "Open in External App",
            f"Could not open with:\n{exe}\n\n{exc}",
            parent=parent,
        )


def _safe_default(parent: tk.Misc, file_path: str) -> None:
    try:
        open_with_default_app(file_path)
    except Exception as exc:
        messagebox.showerror(
            "Open in External App",
            f"Could not open file:\n{exc}",
            parent=parent,
        )


def _safe_picker(parent: tk.Misc, file_path: str) -> None:
    try:
        open_with_system_picker(file_path)
    except Exception as exc:
        messagebox.showerror(
            "Open in External App",
            f"Could not show Open with dialog:\n{exc}",
            parent=parent,
        )


def open_manage_external_apps_dialog(parent: tk.Misc) -> None:
    """FastStone-style Add/Remove Programs dialog."""
    ManageExternalAppsDialog(parent)


class ManageExternalAppsDialog(ctk.CTkToplevel):
    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("Add or Remove Programs")
        self.geometry("520x360")
        self.attributes("-topmost", True)
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        self._apps: list[ExternalApp] = list(load_custom_apps())

        tip = ctk.CTkLabel(
            self,
            text="Programs listed here appear in the “Open in External App” menu for every file.",
            wraplength=480,
            justify="left",
            anchor="w",
        )
        tip.pack(fill="x", padx=12, pady=(12, 6))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=6)

        self._listbox = tk.Listbox(
            body,
            activestyle="dotbox",
            selectmode=tk.EXTENDED,
            font=("Segoe UI", 10),
            bg="#2b2b2b",
            fg="#e8e8e8",
            selectbackground="#3a6ea5",
            highlightthickness=0,
            borderwidth=0,
        )
        self._listbox.pack(side="left", fill="both", expand=True)

        scroll = ctk.CTkScrollbar(body, command=self._listbox.yview)
        scroll.pack(side="right", fill="y")
        self._listbox.configure(yscrollcommand=scroll.set)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(4, 12))

        ctk.CTkButton(btns, text="Add…", width=90, command=self._add).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btns, text="Remove", width=90, command=self._remove).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btns, text="Rename…", width=90, command=self._rename).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btns, text="Close", width=90, command=self._close).pack(side="right")

        self._refresh()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(50, self._focus_list)

    def _focus_list(self) -> None:
        try:
            self._listbox.focus_set()
        except tk.TclError:
            pass

    def _refresh(self) -> None:
        self._listbox.delete(0, tk.END)
        for app in self._apps:
            self._listbox.insert(tk.END, f"{app.name}  —  {app.exe}")

    def _add(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Select program",
            filetypes=[("Programs", "*.exe"), ("All files", "*.*")],
        )
        if not path:
            return
        path = os.path.normpath(path)
        key = os.path.normcase(path)
        if any(a.key() == key for a in self._apps):
            messagebox.showinfo("Add Program", "This program is already in the list.", parent=self)
            return
        name = _friendly_name_from_exe(path)
        self._apps.append(ExternalApp(name=name, exe=path, source="custom"))
        self._persist()
        self._refresh()

    def _selected_indexes(self) -> list[int]:
        return list(self._listbox.curselection())

    def _remove(self) -> None:
        idxs = self._selected_indexes()
        if not idxs:
            return
        for i in sorted(idxs, reverse=True):
            if 0 <= i < len(self._apps):
                del self._apps[i]
        self._persist()
        self._refresh()

    def _rename(self) -> None:
        idxs = self._selected_indexes()
        if len(idxs) != 1:
            messagebox.showinfo("Rename", "Select a single program to rename.", parent=self)
            return
        i = idxs[0]
        app = self._apps[i]
        dialog = ctk.CTkInputDialog(text="Display name:", title="Rename Program")
        new_name = dialog.get_input()
        if not new_name:
            return
        new_name = new_name.strip()
        if not new_name:
            return
        self._apps[i] = ExternalApp(name=new_name, exe=app.exe, source="custom")
        self._persist()
        self._refresh()

    def _persist(self) -> None:
        try:
            save_custom_apps(self._apps)
        except Exception as exc:
            messagebox.showerror("Save", f"Could not save program list:\n{exc}", parent=self)

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
