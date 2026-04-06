# Vibe Video Player (Public Beta) 🎬

A **no-nonsense, comfortable, and easy-to-use media player based on the famous VLC engine**. Designed for users who want a smooth, organized library experience with advanced features that just work.

###  Key Features
* **Robust Playback Engine**: Powered by the famous VLC backend for universal format support, including legacy files and media with playback errors.
* **Comfortable Media Browsing**: High-speed thumbnail generation and optimized local caching for a smooth, visual library navigation.
* **Searchable Database**: Your library is indexed and instantly searchable using custom keywords and labels.
* **Advanced Media Controls**: Professional timeline featuring precise navigation, custom looping, and visual bookmark management.
* **Smart Automated Tagging**: Automatically generates descriptive keywords for your media to keep your library organized without manual typing.

### Two Ways to Run

#### 1. Portable Version (Recommended for Users)
Download the latest **VibePlayer.zip** from the [Releases](https://github.com/saffmila/vibe-player/releases/) section.
1. Extract the ZIP to any folder.
2. Run **`VibePlayer.exe`**. No installation or Python setup is required.

#### 2. Development Version (From Source)
1. **Installation**: Run `run install.bat`. This sets up a Python 3.11 virtual environment and installs all dependencies.
2. **Run**: Double-click `run.bat` (standard) or `run_debug.bat` (for console logs and debugging).

###  Security & Privacy Audit
We value your privacy. This project includes a dedicated audit tool, `check_build.py`, to ensure that every public release is:
* **Clean**: No local development logs, private configurations, or personal database artifacts (`.db`, `.wal`, `.shm`) are ever included.
* **Private**: All processing, including automated tagging and thumbnail caching, happens locally on your machine.
* **Verified**: Release executables are scanned via VirusTotal to ensure safety.

###  Technical Details
* **Built With**: Python 3.11 and CustomTkinter for a modern, responsive GUI.
* **Core Backend**: VLC Media Player (64-bit) integration.

### ⚠️ Disclaimer
* **Provided "As Is"**: This software is provided without any warranty of any kind.
* **Use at Your Own Risk**: The author is not responsible for any data loss, file corruption, or system instability.
* **Beta Software**: Please be aware that this is a beta version. We recommend testing file-related features on a copy of your data first.