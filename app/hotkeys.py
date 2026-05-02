"""
hotkeys.py — Default keyboard shortcut mappings for the application.

Defines bindings for playback, video controls, image viewer, playlist,
file operations, panels, rating, and debug actions.
"""

from __future__ import annotations

from typing import Any

DEFAULT_HOTKEYS = {
    # --- Playback (Global) ---
    'play_pause': '<space>',
    'skip_next': '<Right>',
    'skip_back': '<Left>',
    'enter_action': '<Return>',
    
    # --- Video Specific ---
    'video_seek_forward': '<Control-Right>',
    'video_seek_backward': '<Control-Left>',
    'video_volume_up': '<Up>',
    'video_volume_down': '<Down>',
    'video_mute': 'm',
    'video_fullscreen': 'f',
    
# --- Image Viewer: Navigation ---
    'image_next': '<Right>',
    'image_prev': '<Left>',
    'image_copy': '<Control-c>',
    'image_save': '<Control-s>',     # Save As
    'image_delete': '<Delete>',
    'close_window': '<Escape>',
    'image_fullscreen': '<F11>',     # Match video fullscreen (or Control-f)

    # --- Image Viewer: Manipulation (NEW) ---
    'image_rotate_left': 'l',        # Rotate Left
    'image_rotate_right': 'r',       # Rotate Right
    'image_flip_h': 'h',             # Flip Horizontal
    'image_flip_v': 'v',             # Flip Vertical
    # --- Image Viewer: Visuals (NEW) ---
    'image_toggle_bg': 'b',          # Background color cycle
    'image_toggle_info': 'i',        # Info HUD toggle
    'image_actual_size': 'a',        # Actual Size (1:1)
    'image_fit_best': 'b',           # Best fit
    'image_fit_width': 'w',          # Fit Width
    'image_zoom_in': '+',            # Zoom In (plus)
    'image_zoom_out': '-',           # Zoom Out (minus)

    # --- Playlist ---
    'add_to_playlist': '<Shift-P>',
    'new_playlist': '<Shift-N>',
    
    # --- Window / UI ---
    'toggle_fullscreen': '<F11>',
    'zoom_thumb': '<Control-MouseWheel>',
    'select_all': '<Control-a>',
    
    # --- File Operations ---
    'delete': '<Delete>',
    'search': '<Control-f>',
    'metadata': '<Control-m>',
    'keywords': '<Shift-K>',
    # Clipboard (paths): main window / thumbnail area — paste into current folder
    'files_clipboard_copy': '<Control-c>',
    'files_clipboard_cut': '<Control-x>',
    'files_clipboard_paste_copy': '<Control-v>',
    # Tk on Windows often uses keysym V (not v) when Shift is held — code also binds lowercase alias
    'files_clipboard_paste_move': '<Control-Shift-V>',
    
    # --- Essentials ---
    'refresh': '<F5>',
    'rename': '<F2>',
    # Extra rename key (Explorer-style is F2; some users expect F6 from other tools)
    'rename_secondary': '<F6>',
    'parent_dir': '<BackSpace>',
    'open_preferences': '<Control-comma>',
    
    # --- Panels ---
    'toggle_info_panel': '<Control-i>',
    'toggle_timeline': '<Control-t>',
    
    # --- Rating ---
    'rate_0': '0',
    'rate_1': '1',
    'rate_2': '2',
    'rate_3': '3',
    'rate_4': '4',
    'rate_5': '5',

    # --- Loop (Video) ---
    'loop_start': '<Shift-S>',
    'loop_end': '<Shift-E>',
    'loop_toggle': '<Shift-L>',
    'bookmark_next': '<Alt-Right>',
    'bookmark_prev': '<Alt-Left>',
    'long_seek_forward': '<Shift-Right>',
    'long_seek_backward': '<Shift-Left>',

    # --- Speed (Video) ---
    'video_speed_up': '<greater>',
    'video_speed_down': '<less>',
    
    # --- Debug / Developer ---
    'run_plugin': '<F7>',
    'show_debug': '<F9>',
    'hide_debug': '<Shift-F9>',
    'toggle_log': '<F12>',
    'view_catalog': '<Shift-D>',
    'debug_thumb': '<Control-Shift-S>'
}

# Ordered groups for the Keyboard Shortcuts help window (all keys should appear once;
# anything missing here is listed under "Other").
HOTKEY_HELP_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Playback & navigation", (
        "play_pause", "skip_next", "skip_back", "enter_action",
        "bookmark_next", "bookmark_prev", "long_seek_forward", "long_seek_backward",
        "add_to_playlist", "new_playlist", "toggle_fullscreen", "parent_dir", "refresh",
    )),
    ("File & clipboard", (
        "delete", "rename", "rename_secondary", "select_all",
        "files_clipboard_copy", "files_clipboard_cut",
        "files_clipboard_paste_copy", "files_clipboard_paste_move",
        "search", "metadata", "keywords",
    )),
    ("Panels & settings", (
        "toggle_info_panel", "toggle_timeline", "open_preferences", "zoom_thumb",
    )),
    ("Rating", (
        "rate_0", "rate_1", "rate_2", "rate_3", "rate_4", "rate_5",
    )),
    ("Video / timeline (when player focused)", (
        "video_seek_forward", "video_seek_backward", "video_volume_up", "video_volume_down",
        "video_mute", "video_fullscreen", "loop_start", "loop_end", "loop_toggle",
        "video_speed_up", "video_speed_down",
    )),
    ("Image viewer window", (
        "image_next", "image_prev", "image_copy", "image_save", "image_delete",
        "close_window", "image_fullscreen", "image_rotate_left", "image_rotate_right",
        "image_flip_h", "image_flip_v", "image_toggle_bg", "image_toggle_info",
        "image_actual_size", "image_fit_best", "image_fit_width",
        "image_zoom_in", "image_zoom_out",
    )),
    ("Developer", (
        "run_plugin", "show_debug", "hide_debug", "toggle_log", "view_catalog", "debug_thumb",
    )),
)


def format_accelerator_menu(seq: str) -> str:
    """Turn a Tk bind sequence into short text for menu accelerators (Windows-style)."""
    s = (seq or "").strip()
    if not s:
        return ""
    if not s.startswith("<"):
        return s.upper() if len(s) == 1 else s

    inner = s[1:-1]
    parts = inner.split("-")
    mods = {"control": "Ctrl", "shift": "Shift", "alt": "Alt", "option": "Alt"}
    tokens = {
        "space": "Space",
        "return": "Enter",
        "backspace": "Backspace",
        "delete": "Del",
        "comma": ",",
        "period": ".",
        "greater": ">",
        "less": "<",
        "mousewheel": "Mouse wheel",
    }
    out: list[str] = []
    for p in parts:
        pl = p.lower()
        if pl in mods:
            out.append(mods[pl])
            continue
        if pl in tokens:
            out.append(tokens[pl])
            continue
        if len(pl) >= 2 and pl[0] == "f" and pl[1:].isdigit():
            out.append("F" + pl[1:])
            continue
        if len(p) == 1:
            out.append(p.upper())
        else:
            out.append(p)
    return "+".join(out)


def menu_accel(hotkeys: dict[str, str], action: str) -> str | None:
    """Accelerator string for ``action``, or None if missing."""
    seq = hotkeys.get(action)
    if not seq:
        return None
    return format_accelerator_menu(seq)


def rename_accelerators_label(hotkeys: dict[str, str]) -> str | None:
    """Menu text for rename (F2 and optional second key, e.g. F6)."""
    a = menu_accel(hotkeys, "rename")
    b = menu_accel(hotkeys, "rename_secondary")
    if a and b:
        return f"{a} / {b}"
    return a or b


def iter_help_sections(hotkeys_map: dict[str, Any]) -> list[tuple[str, list[tuple[str, str]]]]:
    """
    Build (section_title, [(action_key, display_sequence), ...]) for the help UI.
    Uses string values from ``hotkeys_map`` (same shape as DEFAULT_HOTKEYS).
    """
    seen: set[str] = set()
    rows: list[tuple[str, list[tuple[str, str]]]] = []
    for title, keys in HOTKEY_HELP_SECTIONS:
        chunk: list[tuple[str, str]] = []
        for k in keys:
            if k not in hotkeys_map:
                continue
            seq = hotkeys_map[k]
            if not isinstance(seq, str):
                continue
            chunk.append((k, seq))
            seen.add(k)
        if chunk:
            rows.append((title, chunk))
    other: list[tuple[str, str]] = []
    for k in sorted(hotkeys_map.keys()):
        if k in seen:
            continue
        v = hotkeys_map[k]
        if isinstance(v, str):
            other.append((k, v))
    if other:
        rows.append(("Other", other))
    return rows