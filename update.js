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
          "pip install -r requirements.txt"
        ]
      }
    }
  ]
}
