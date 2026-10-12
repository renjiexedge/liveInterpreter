"""Where the app reads its bundled files and writes its own.

Bundled, read-only files (the loop-test clip, ...) come from resource_path(),
which works from source and from a PyInstaller bundle alike, and never depends
on the working directory: started from a shortcut, a CWD-relative path silently
misses them. Everything the app writes goes under user_data_dir()
(%LOCALAPPDATA%\\Xedge\\LiveInterpreter), never into the install folder."""
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def resource_path(relative: str) -> Path:
    """A bundled file: from the PyInstaller bundle when frozen, else the repo root."""
    base = Path(getattr(sys, "_MEIPASS", _REPO_ROOT))
    return base / relative


def user_data_dir() -> Path:
    """%LOCALAPPDATA%\\Xedge\\LiveInterpreter. Not created here: writers mkdir."""
    root = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    return Path(root) / "Xedge" / "LiveInterpreter"


def log_file() -> Path:
    """app.log, rotated to app.log.1 and app.log.2 (app_logging/setup.py)."""
    return user_data_dir() / "logs" / "app.log"


def user_config_file() -> Path:
    return user_data_dir() / "user_config.json"


def debug_dump_dir() -> Path:
    """Raw audio dumps (engines/gemini_live.py dump_raw_audio). Dev only."""
    return _REPO_ROOT / "debug_dumps"
