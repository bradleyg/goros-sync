"""Paths and runtime configuration."""

from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "static"
DATA_DIR = Path(os.getenv("SYNC_DATA_DIR", ROOT_DIR / "data")).expanduser()
DB_PATH = DATA_DIR / "sync.db"
GARMIN_TOKEN_DIR = DATA_DIR / "garmin_tokens"

# Optional sign-in for the web UI. Login is required when APP_PASSWORD is set.
APP_USERNAME = (os.getenv("APP_USERNAME") or "admin").strip()
APP_PASSWORD = os.getenv("APP_PASSWORD") or None
SESSION_DAYS = int(os.getenv("SESSION_DAYS") or 30)

DEFAULT_LOOKBACK_DAYS = 7
MAX_LOOKBACK_DAYS = 365


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Credentials and tokens live here; keep it owner-only where we can.
    try:
        os.chmod(DATA_DIR, 0o700)
    except PermissionError:
        pass
