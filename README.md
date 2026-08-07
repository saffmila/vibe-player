# Vibe Video Player (Public Beta) 🎬

A **no-nonsense, comfortable, and easy-to-use media player based on the famous VLC engine**. Designed for users who want a smooth, organized library experience with advanced features that just work.

###  Key Features
* **Robust Playback Engine**: Powered by the famous VLC backend for universal format support, including legacy files and media with playback errors.
* **Comfortable Media Browsing**: High-speed thumbnail generation and optimized local caching for a smooth, visual library navigation.
* **Searchable Database**: Your library is indexed and instantly searchable using custom keywords and labels.
* **Advanced Media Controls**: Professional timeline featuring precise navigation, custom looping, and visual bookmark management.
* **Smart Automated Tagging**: Automatically generates descriptive keywords for your media to keep your library organized without manual typing.

### Ways to Run

#### 1. Portable Version (Recommended for Users)
Download the latest **VibePlayer.zip** from the [Releases](https://github.com/saffmila/vibe-player/releases/) section.
1. Extract the ZIP to any folder.
2. Run **`VibePlayer.exe`**. No installation or Python setup is required.
3. Optional Windows file associations: run **`register_file_associations.bat`**
   from the extracted folder, then choose **Vibe Player** in Windows Default apps
   or via **Open with** for your image/video extensions.

#### 2. Development Version (From Source)
1. **Installation**: Run `run install.bat`. This sets up a Python 3.11 virtual environment and installs all dependencies.
2. **Run**: Double-click `run.bat` (standard) or `run_debug.bat` (for console logs and debugging).

#### 3. Run via Pinokio (1-Click Setup)
If you use [Pinokio](https://pinokio.computer/) for managing local AI applications, you can run Vibe Player in an isolated environment with a single click:

1. Open Pinokio.
2. Click **Discover** (or *Download from URL*).
3. Paste the repository URL: `https://github.com/saffmila/vibe-player`
4. Click **Install**, and once finished, click **Start Vibe Player**.

### SeedVR 2 Upscale (optional, NVIDIA GPU)

Offline AI image/video upscale via [ComfyUI-SeedVR2](https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler). The app does **not** ship the CUDA runner or model weights — install them once from the Upscale dialog:

1. Right-click a video or image → **Upscale** → open **Advanced**.
2. **Install runner…** — downloads the CLI checkout and creates a local `.venv` with PyTorch CUDA (`cu130`, ~6–8 GB). Needs Python 3.10–3.12 on PATH (Pinokio’s `env` works after Install).
3. **Install weights…** — downloads the recommended **3B FP8** DiT + VAE (~4 GB) from Hugging Face into the weights folder.
4. Status should show **Ready to start**, then click **Start**.

Notes:
* NVIDIA GPU + recent driver required. Flash Attention is optional; SDPA is the default fallback.
* FFmpeg is installed with the app (`install.bat` / Pinokio) and preferred for video I/O.
* Lower VRAM cards: keep the FP8 3B model, enable **Low VRAM (tiled VAE)**, prefer Scale 2×.

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