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
          "pip install --upgrade pip setuptools wheel",
          "pip install -r requirements.txt"
        ]
      }
    }
  ]
}
