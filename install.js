module.exports = {
  run: [
    // Repo is already downloaded by Pinokio — do NOT git clone into "app"
    // (this project already uses app/ for Python sources).
    // Pinokio's venv param creates/activates the environment automatically.
    {
      method: "shell.run",
      params: {
        venv: "env",
        message: [
          // Must use `python -m pip` — bare `pip` cannot self-upgrade on Windows
          "python -m pip install --upgrade pip setuptools wheel",
          "python -m pip install -r requirements.txt"
        ]
      }
    },
    // Bundle FFmpeg into tools/ (same layout as install.bat / get_ffmpeg_path).
    // Skip when already present so re-Install stays fast.
    {
      when: "{{platform === 'win32' && !exists('tools/ffmpeg/bin/ffmpeg.exe')}}",
      method: "shell.run",
      params: {
        message: "powershell -NoProfile -ExecutionPolicy Bypass -File install_ffmpeg.ps1"
      }
    }
  ]
}
