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
    }
  ]
}
