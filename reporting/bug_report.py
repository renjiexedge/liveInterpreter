"""Bug report contents and delivery (guide/distribution_plan.md §4.1), kept free
of Qt so it can be tested on its own; ui/bug_report.py is the dialog.

A report is content-free: the tester's own description, system details, the
selected devices and settings, the active issue codes and the tail of app.log
(which never holds transcript text, distribution plan decision 13). Never
transcripts, audio or the API key. It goes to the Apps Script relay
(relay/bug_report_relay.gs) as JSON; if that fails, the caller falls back to
saving the zip and opening a mailto: draft."""
import base64
import io
import json
import logging
import platform
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from version import __version__, display_version

logger = logging.getLogger(__name__)

REPORT_FILE = "report.txt"
LOG_FILE = "app.log.txt"
# Sent with every report so the relay can drop posts that aren't from the app.
RELAY_APP_ID = "xedge-live-interpreter"


def app_line() -> str:
    return f"Live Interpreter {display_version()}, version {__version__}"


def system_details() -> dict[str, str]:
    return {"Windows": f"{platform.platform()} (build {platform.version()})",
            "Python": platform.python_version()}


def log_tail(path: Path, lines: int) -> list[str]:
    """The last `lines` lines of app.log, reaching into app.log.1 when app.log
    was just rotated and is short. Missing files give fewer lines, not an error."""
    collected: list[str] = []
    for candidate in (path, path.with_name(path.name + ".1")):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        collected = text.splitlines()[-(lines - len(collected)):] + collected
        if len(collected) >= lines:
            break
    return collected


@dataclass
class BugReport:
    """One report, frozen when the preview is shown, so what is sent is exactly
    what was previewed. `sections` maps a heading to its "key: value" lines."""

    description: str
    reporter: str
    created: datetime = field(default_factory=datetime.now)
    sections: dict[str, dict[str, str]] = field(default_factory=dict)
    log_lines: list[str] | None = None  # None: not included

    @property
    def file_stem(self) -> str:
        return f"bug_{self.created:%Y%m%d_%H%M%S}"

    @property
    def subject(self) -> str:
        return f"Live Interpreter {__version__} bug report from {self.reporter}"

    def report_text(self) -> str:
        lines = [f"Reported: {self.created.isoformat(timespec='seconds')}",
                 f"Reporter: {self.reporter}",
                 f"App: {app_line()}",
                 "", "Description:", self.description]
        for heading, values in self.sections.items():
            lines += ["", f"{heading}:"]
            lines += [f"  {key}: {value}" for key, value in values.items()] or ["  (none)"]
        if self.log_lines is not None:
            lines += ["", f"Log: last {len(self.log_lines)} lines of app.log, in {LOG_FILE}"]
        return "\n".join(lines) + "\n"

    def files(self) -> dict[str, str]:
        """What goes in the zip, by file name."""
        files = {REPORT_FILE: self.report_text()}
        if self.log_lines is not None:
            files[LOG_FILE] = "\n".join(self.log_lines) + "\n"
        return files

    def preview_text(self) -> str:
        return "\n".join(f"===== {name} =====\n{text}" for name, text in self.files().items())

    def zip_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, text in self.files().items():
                archive.writestr(name, text.encode("utf-8"))
        return buffer.getvalue()

    def save_zip(self, folder: Path) -> Path:
        """Kept until uninstall (decision 12); never overwrites an earlier report."""
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{self.file_stem}.zip"
        n = 1
        while path.exists():
            n += 1
            path = folder / f"{self.file_stem}_{n}.zip"
        path.write_bytes(self.zip_bytes())
        logger.info("Bug report saved: %s", path)
        return path


class RelayError(Exception):
    pass


def send_to_relay(report: BugReport, url: str, timeout_s: float, max_body_chars: int) -> None:
    """POST the report to the Apps Script relay; raises on any failure. Apps
    Script answers a POST with a 302 to the result, which urllib follows as a
    GET, as Apps Script expects."""
    if not url:
        raise RelayError("no relay URL configured")
    payload = {"app": RELAY_APP_ID,
               "subject": report.subject,
               "body": report.report_text()[:max_body_chars],
               "zip_name": f"{report.file_stem}.zip",
               "zip_b64": base64.b64encode(report.zip_bytes()).decode("ascii")}
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        raw = response.read()
    try:
        reply = json.loads(raw)
    except ValueError:
        raise RelayError(f"unexpected reply ({len(raw)} bytes, not JSON)") from None
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise RelayError(f"relay refused the report: {reply.get('error') if isinstance(reply, dict) else reply!r}")


def _draft_body(report: BugReport, zip_path: Path, description_chars: int) -> str:
    """Drafts can't carry attachments, so the body says which file to attach.
    Kept short: long mailto: URLs get cut off."""
    description = report.description
    if len(description) > description_chars:
        description = description[:description_chars] + "…"
    return (f"Please attach this file from the folder that just opened:\n{zip_path.name}\n\n"
            f"{description}\n")


def mailto_url(report: BugReport, email: str, zip_path: Path, description_chars: int = 500) -> str:
    """A draft in the PC's email app. Qt logs "There is no mail program
    installed" when the handler is a Store app (the new Outlook registers no
    command line), but then hands the link to Windows, which opens it."""
    body = _draft_body(report, zip_path, description_chars)
    query = urllib.parse.urlencode({"subject": report.subject, "body": body},
                                   quote_via=urllib.parse.quote)
    return f"mailto:{email}?{query}"


def email_template(report: BugReport, email: str, zip_path: Path, description_chars: int = 500) -> str:
    """The same email spelled out, for a tester whose draft didn't open: who to
    send it to and which file to attach (full path)."""
    return (f"To: {email}\n"
            f"Subject: {report.subject}\n"
            f"Attach: {zip_path}\n\n"
            + _draft_body(report, zip_path, description_chars))
