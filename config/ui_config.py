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
    """Report a bug (guide/distribution_plan.md §4.1): reports go to the Apps
    Script relay (relay/bug_report_relay.gs), which emails them to `email`. If
    that fails, the tester attaches the zip saved in reports_dir to a mailto: draft."""

    # Support address shown to testers. When it changes, also change RECIPIENT in
    # relay/bug_report_relay.gs (and redeploy it as a new version).
    email: str = "renjie.xedge@gmail.com"
    endpoint_url: str = "https://script.google.com/macros/s/AKfycbzTk5M3VRMjIDc9fq82Z7ZLPJJIVehcQzksblOTGXTMdSPTYUdnMK-6C0TuVVDcBhtXkA/exec"          # the relay's web app URL (.../exec); "" -> always the email fallback
    timeout_s: float = 20.0
    log_lines: int = 500            # tail of app.log included in a report
    max_body_chars: int = 20_000    # email body cap; the full report.txt is in the zip anyway
    reports_dir: Path = field(default_factory=lambda: user_data_dir() / "bug_reports")
