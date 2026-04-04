# Vibe Video Player (Public Beta) 🎬

A modern, Python-based video player built with CustomTkinter and VLC. Designed for a smooth experience and easy video management.

### Key Features
* **VLC Backend:** Reliable playback for almost any format.
* **Thumbnail Grid:** Fast visual navigation through your library.
* **Timeline Manager:** Precise control over your video progress.
* **Database Driven:** Your library is indexed and fast to search.

### Quick Start (Windows)
1. **Installation:** Run `run install.bat`. This will automatically set up a Python 3.11 virtual environment, install all dependencies, and download FFmpeg into the `tools/` folder.
2. **Normal Run:** Double-click `run.bat` to start the player (no console window).
3. **Debug Run:** Use `run_debug.bat` if you want to see logs and debug information in the console.

### Requirements
* **Python 3.11** (The installer will check for this).
* **VLC Media Player (64-bit)** installed on your system.

---
*Note: This is a beta version. If you find any bugs, feel free to report them!*

## Model setup
The repository does not include YOLO extra-content models. Before using AI tagging features, place your YOLO model files into `app/models/yolov8/extra/`.