"""
hotkeys.py — Default keyboard shortcut mappings for the application.

Defines bindings for playback, video controls, image viewer, playlist,
file operations, panels, rating, and debug actions.
"""

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
    'files_clipboard_paste_copy': '<Control-v>',
    # Tk on Windows often uses keysym V (not v) when Shift is held — code also binds lowercase alias
    'files_clipboard_paste_move': '<Control-Shift-V>',
    
    # --- Essentials ---
    'refresh': '<F5>',
    'rename': '<F2>',
    'parent_dir': '<BackSpace>',
    
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