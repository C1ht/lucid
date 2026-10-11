"""Entry point for the PyInstaller-frozen desktop sidecar build. The
container uses `python -m uvicorn app.main:app ...` directly (see
Containerfile); a frozen executable needs a self-contained entry point."""

import os

import uvicorn

from app.main import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8799"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
