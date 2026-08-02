module.exports = {
  // Keep the script attached while the desktop GUI is running
  daemon: true,
  run: [
    {
      method: "shell.run",
      params: {
        // Run from app/ so relative paths match run.bat behavior
        path: "app",
        // env lives at the project root (one level above app/)
        venv: "../env",
        message: "python main.py"
      }
    }
  ]
}
