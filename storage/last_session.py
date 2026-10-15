"""Save and load the Live Interpreter's last bug-report context as
last_session.json in the user data folder.

Testers report bugs from the home window, usually after closing the Live
Interpreter window (and sometimes after restarting the app), so the "Devices and
settings" section is saved when a session ends and when the window closes.
Content-free like the report itself: device names, settings and issue codes only.
"""
import json
import logging
import os
from datetime import datetime

from config.paths import last_session_file

logger = logging.getLogger(__name__)

SAVED_AT_KEY = "Saved at"


def save_last_session(context: dict[str, str]) -> None:
    """Write atomically, stamped with the time. A failed save is only logged:
    it must never get in the way of closing the window or ending a session."""
    path = last_session_file()
    data = {SAVED_AT_KEY: datetime.now().isoformat(timespec="seconds"), **context}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Couldn't save the last session's context: %s", exc)


def load_last_session() -> dict[str, str] | None:
    """The saved context, or None if there's none or it can't be read."""
    path = last_session_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Couldn't read %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): str(value) for key, value in data.items()}
