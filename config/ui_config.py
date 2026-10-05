import getpass
from dataclasses import dataclass
from pathlib import Path


@dataclass
class UserProfile:
    """Shown in the main window header. There are no user accounts yet
    (design.md §1), so this is a placeholder: an empty display_name falls back
    to the Windows user name."""

    display_name: str = "Huang Renjie"

    def name(self) -> str:
        return self.display_name or getpass.getuser()

    def initials(self) -> str:
        parts = self.name().replace(".", " ").replace("_", " ").split()
        return "".join(p[0] for p in parts[:2]).upper() or "?"


@dataclass
class BugReportConfig:
    """Where the Report a bug dialog saves reports until a destination
    (email, form or issue tracker) is chosen. Gitignored."""

    reports_dir: Path = Path(__file__).resolve().parent.parent / "bug_reports"
