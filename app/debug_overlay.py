"""
Floating debug overlay for Vibe Player (cache stats, CPU/RAM).

Toggled from the app (e.g. hotkey); shows thumbnail cache and system load metrics.
"""

import time
import psutil
import tkinter as tk
import logging


class DebugOverlay:
    """Floating debug overlay showing cache stats, CPU/memory usage, and load times. Toggle with Ctrl+D."""

    def __init__(self, root):
        self.root = root
        self.overlay = tk.Toplevel(root)
        self.overlay.title("Debug Overlay")
        self.overlay.geometry("400x220+100+100")
        self.overlay.attributes("-topmost", True)
        self.overlay.withdraw()  # Hidden on startup

        # Semi-transparent, if desired:
        try:
            self.overlay.attributes('-alpha', 0.88)
        except Exception:
            pass  # some window managers may not support it

        self.overlay_label = tk.Label(self.overlay, text="", font=("Helvetica", 12),
                                      bg="black", fg="white", justify="left", anchor="w")
        self.overlay_label.pack(fill="both", expand=True, padx=10, pady=10)
        self.overlay_is_visible = False

        # tracking data
        self.cache_hits = 0
        self.cache_misses = 0
        self.disk_reads = 0
        self.cache_data_size = 0
        self.cache_read_times = []
        self.disk_read_times = []
        self.load_times = []
        self.total_threads = 0
        self.threads_completed = 0
        self.load_time_history = []
        self.start_time = time.time()

        # Keyboard shortcut to toggle overlay (Ctrl+D)
        self._setup_key_binding()

    def _setup_key_binding(self):
        """Bind Ctrl+D to toggle the overlay visibility."""
        self.root.bind("<Control-d>", self.toggle_overlay)

    def show_overlay(self):
        """Display the overlay window and start periodic stats refresh."""
        self.overlay.deiconify()
        self.overlay.lift()
        self.overlay.focus_force()
        self.overlay_is_visible = True
        self.update_text()

    def hide_overlay(self):
        """Hide the overlay window and stop stats refresh."""
        self.overlay.withdraw()
        self.overlay_is_visible = False

    def toggle_overlay(self, event=None):
        """Show or hide the overlay depending on current state."""
        if self.overlay_is_visible:
            self.hide_overlay()
        else:
            self.show_overlay()

    def add_load_time(self, load_time, load_source):
        """Record a load time (seconds) and its source; keeps last 3 entries."""
        if len(self.load_times) >= 3:
            self.load_times.pop(0)
        self.load_times.append((load_time, load_source))

    def increment_thread_count(self, load_source="disk"):
        """Increment completed thread count; when all done, records total load time."""
        self.threads_completed += 1
        if self.threads_completed >= self.total_threads:
            load_time = time.time() - self.start_time
            self.add_load_time(load_time, load_source)
            self.threads_completed = 0

    def update_cache_stats(self, is_cache_hit, read_time, data_size):
        """Update cache hit/miss counters and timing stats."""
        if is_cache_hit:
            self.cache_hits += 1
            self.cache_read_times.append(read_time)
        else:
            self.cache_misses += 1
            self.disk_reads += 1
            self.disk_read_times.append(read_time)
        self.cache_data_size += data_size

    def update_text(self):
        """Refresh overlay label with current stats; reschedules itself if visible."""
        avg_cache_read_time = (sum(self.cache_read_times) / len(self.cache_read_times)) if self.cache_read_times else 0
        recent_load_times = "\n".join(
            f"{i+1}: {load_time:.2f}s from {source}"
            for i, (load_time, source) in enumerate(self.load_times[-3:])
        ) if self.load_times else "No recent loads"

        text = (
            f"Cache Hits: {self.cache_hits}\n"
            f"Cache Misses: {self.cache_misses}\n"
            f"Cache Memory Usage: {self.cache_data_size / 1024:.2f} KB\n"
            f"Avg Cache Read Time: {avg_cache_read_time:.2f} ms\n"
            f"CPU Usage: {psutil.cpu_percent()}%\n"
            f"Memory Usage: {psutil.virtual_memory().percent}%\n"
            f"Uptime: {int(time.time() - self.start_time)} s\n\n"
            f"Recent Load Times:\n{recent_load_times}"
        )

        self.overlay_label.config(text=text)

        if self.overlay_is_visible:
            self.overlay.after(1000, self.update_text)
