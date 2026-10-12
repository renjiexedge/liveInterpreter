from dataclasses import dataclass, field

from config.model_config import DEFAULT_MODEL


@dataclass
class UserConfig:
    """Per-user settings (guide/distribution_plan.md §4.5), saved as JSON in
    %LOCALAPPDATA% by storage/user_config.py. Never holds the API key
    (that's config/credentials.py)."""

    display_name: str = ""                                  # "" -> the Windows user name
    setup_complete: bool = False
    setup_version: str = ""
    devices: dict[str, str] = field(default_factory=dict)   # device_combos key -> device name
    candidate_language: str = "zh-Hans"
    model: str = DEFAULT_MODEL                              # last used; the combo stays unlocked
