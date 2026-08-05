module.exports = {
  run: [
    // Pull latest commits for this repository
    {
      method: "shell.run",
      params: {
        message: "git pull"
      }
    },
    // Reinstall dependencies into the existing Pinokio venv
    {
      method: "shell.run",
      params: {
        venv: "env",
        message: [
          "python -m pip install -r requirements.txt"
        ]
      }
    },
    // Ensure FFmpeg is present for installs that predate tools/ bundling
    {
      when: "{{platform === 'win32' && !exists('tools/ffmpeg/bin/ffmpeg.exe')}}",
      method: "shell.run",
      params: {
        message: "powershell -NoProfile -ExecutionPolicy Bypass -File install_ffmpeg.ps1"
      }
    }
  ]
}
