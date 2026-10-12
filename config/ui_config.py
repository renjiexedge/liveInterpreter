import getpass
from dataclasses import dataclass, field
from pathlib import Path

from config.paths import user_data_dir


@dataclass
class UserProfile:
    """Shown in the main window header. There are no user accounts yet
    (design.md §1): display_name comes from UserConfig (the setup wizard asks for
    it), and an empty one falls back to the Windows user name."""

    display_name: str = ""

    def name(self) -> str:
        return self.display_name or getpass.getuser()

    def initials(self) -> str:
        parts = self.name().replace(".", " ").replace("_", " ").split()
        return "".join(p[0] for p in parts[:2]).upper() or "?"


@dataclass
class BugReportConfig:
    """Where the Report a bug dialog saves reports until a destination
    (email, form or issue tracker) is chosen: the user data folder."""

    reports_dir: Path = field(default_factory=lambda: user_data_dir() / "bug_reports")
