"""
timelinebar_plugin.py — Plugin that opens the timeline bar widget for a video.

Integrates with the main app to show timeline strips and seek handler.
"""

from plugins.plugin_base import TaggingPlugin
import os


class TimelineBarPlugin(TaggingPlugin):
    def __init__(self):
        super().__init__()

    def run(self, app, file_path=None, **kwargs):
        if file_path is None:
            file_path = getattr(app, "selected_thumbnails", [None])[0]
        print(f"[TimelineBarPlugin] run() called with file_path: {file_path}")
        self.open_timeline_bar(app, file_path)
        return {"result": "timeline_shown"}

    def open_timeline_bar(self, app, video_path):
        if isinstance(video_path, tuple):
            print(f"[PATCH][TimelineBarPlugin] video_path byl tuple, beru první prvek: {video_path[0]}")
            video_path = video_path[0]
        print(f"[TimelineBarPlugin] open_timeline_bar() for: {video_path}")

        from timeline_bar_widget import TimelineBarWidget
        timeline_manager = getattr(app, "timeline_manager", None)
        if timeline_manager is None:
            from timeline_manager import TimelineManager
            print(f"[TimelineBarPlugin] Creating local TimelineManager")
            timeline_manager = TimelineManager()
        else:
            print("[TimelineBarPlugin] Using app.timeline_manager")

        # === SEEK HANDLER ===
        seek_handler = None
        if not hasattr(app, "current_video_window") or app.current_video_window is None:
            print("[TimelineBarPlugin] No player, starting new player.")
            video_name = os.path.basename(video_path)
            app.open_video_player(video_path, video_name)
        if hasattr(app, "current_video_window") and app.current_video_window is not None:
            seek_handler = getattr(app.current_video_window, "seek_to_time", None)
        if seek_handler is None:
            print("[TimelineBarPlugin] WARNING: Player has no seek_to_time method!")

        # ZAVŘI STARÝ TIMELINE WIDGET pokud je v app
        if hasattr(app, "timeline_window") and app.timeline_window:
            try:
                app.timeline_window.destroy()
                print("[TimelineBarPlugin] Previous app.timeline_window destroyed.")
            except Exception as ex:
                print(f"[TimelineBarPlugin] Error destroying old timeline_window: {ex}")

        print(f"[TimelineBarPlugin] Creating TimelineBarWidget for {video_path}")

        app.timeline_window = TimelineBarWidget(
            app.root,
            video_path,
            timeline_manager,
            on_seek=seek_handler
        )
        # current_time = 0
        # if hasattr(app, "current_video_window") and app.current_video_window:
            # Pokud tvůj VideoPlayer má getter na aktuální čas v sekundách:
            # if hasattr(app.current_video_window, "get_current_time"):
                # current_time = app.current_video_window.get_current_time()
            # else:
                # Nebo podle potřeby získat čas v ms a převést
                # current_time = app.current_video_window.player.get_time() / 1000.0
       
       # app.timeline_window.set_current_time(current_time)
        
        app.timeline_window.focus()
        print("[TimelineBarPlugin] TimelineBarWidget focused.")

plugin_class = TimelineBarPlugin
