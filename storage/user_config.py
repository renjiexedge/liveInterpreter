"""Load and save config/user_config.UserConfig as user_config.json in the user
data folder. Kept out of config/, which holds plain dataclasses only."""
import dataclasses
import json
import logging
import os

from config.paths import user_config_file
from config.user_config import UserConfig

logger = logging.getLogger(__name__)


def load_user_config() -> UserConfig:
    """The saved settings, or defaults if there are none. A damaged file or an
    unknown key never stops the app: bad parts fall back to the defaults."""
    path = user_config_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return UserConfig()
    except (OSError, ValueError) as exc:
        logger.warning("Couldn't read %s, using defaults: %s", path, exc)
        return UserConfig()
    if not isinstance(data, dict):
        logger.warning("%s isn't a JSON object, using defaults", path)
        return UserConfig()
    defaults = UserConfig()
    known = {}
    for f in dataclasses.fields(UserConfig):
        if f.name in data:
            value = data[f.name]
            if isinstance(value, type(getattr(defaults, f.name))):
                known[f.name] = value
            else:
                logger.warning("%s: ignoring %s=%r (wrong type)", path, f.name, value)
    return UserConfig(**known)


def save_user_config(config: UserConfig) -> None:
    """Write atomically, so a crash mid-save can't leave a half-written file."""
    path = user_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dataclasses.asdict(config), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
