// pm2 process file for Mocha.
//
// Before starting, create the virtual environment and install the
// dependencies (from the project root):
//
//   python3 -m venv .venv
//   .venv/bin/pip install -r requirements.txt
//
// and create your `.env` and `config.yaml` files (see SETUP.md).
//
// The interpreter path below assumes the venv lives in `.venv`
// inside the project folder. If your venv lives somewhere else,
// point `script` at that Python instead — on Windows that is
// usually `.venv/Scripts/python.exe`.
//
// Tokens and settings are NOT defined here: the bridge loads its
// own `.env` file from the project folder at startup, so this file
// never contains secrets and is safe to commit.
//
// Start with:  pm2 start ecosystem.config.js
// Then run `pm2 save` and `pm2 startup` so the bridge comes back
// automatically after a reboot (see SETUP.md, Option C).

const path = require("path");

module.exports = {
  apps: [
    {
      name: "mocha",
      // The venv's Python is the executable; pm2 must not try to
      // interpret it as a script of its own, hence interpreter
      // "none". The bridge itself is launched as a module.
      script: path.join(__dirname, ".venv", "bin", "python"),
      args: "-m bot.main",
      interpreter: "none",
      // The bridge resolves its default relative paths
      // (config.yaml, data/bridge.db, .env) from the working
      // directory, so it must run from the project root.
      cwd: __dirname,
      autorestart: true,
      restart_delay: 5000,
      env: {
        // Unbuffered Python output, so `pm2 logs` shows the
        // bridge's log lines as they happen instead of in bursts.
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
