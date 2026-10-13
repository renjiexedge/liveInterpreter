import base64
import io
import json
import threading
import urllib.parse
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from version import __version__

from reporting.bug_report import (LOG_FILE, REPORT_FILE, BugReport, RelayError, log_tail,
                                  mailto_url, send_to_relay)


def _report(**kwargs) -> BugReport:
    defaults = dict(description="Translation stopped.", reporter="Tester",
                    created=datetime(2026, 10, 12, 9, 30, 0),
                    sections={"Devices and settings": {"Model": "gemini-3.8-live"}},
                    log_lines=["line 1", "line 2"])
    return BugReport(**{**defaults, **kwargs})


def test_log_tail_reaches_into_rotated_file(tmp_path):
    log = tmp_path / "app.log"
    (tmp_path / "app.log.1").write_text("old 1\nold 2\nold 3\n", encoding="utf-8")
    log.write_text("new 1\nnew 2\n", encoding="utf-8")
    assert log_tail(log, 3) == ["old 3", "new 1", "new 2"]
    assert log_tail(log, 2) == ["new 1", "new 2"]
    assert log_tail(tmp_path / "missing.log", 5) == []


def test_zip_matches_preview(tmp_path):
    report = _report()
    path = report.save_zip(tmp_path)
    assert path.name == "bug_20261012_093000.zip"
    with zipfile.ZipFile(path) as archive:
        files = {name: archive.read(name).decode("utf-8") for name in archive.namelist()}
    assert files == report.files()
    assert set(files) == {REPORT_FILE, LOG_FILE}
    for text in files.values():
        assert text in report.preview_text()
    assert "Model: gemini-3.8-live" in files[REPORT_FILE]
    # A second report in the same second doesn't overwrite the first.
    assert report.save_zip(tmp_path).name == "bug_20261012_093000_2.zip"


def test_log_left_out():
    report = _report(log_lines=None)
    assert set(report.files()) == {REPORT_FILE}
    assert "Log:" not in report.report_text()


def test_mailto_names_the_attachment(tmp_path):
    url = mailto_url(_report(description="x" * 2000), "a@example.com", tmp_path / "bug_1.zip")
    assert url.startswith("mailto:a@example.com?")
    query = urllib.parse.parse_qs(url.split("?", 1)[1])
    assert "bug_1.zip" in query["body"][0]
    assert len(query["body"][0]) < 700  # description cut short
    assert query["subject"] == [f"Live Interpreter {__version__} bug report from Tester"]


class _Relay(BaseHTTPRequestHandler):
    """Mimics Apps Script: POST /exec answers 302, and the GET it redirects to has the reply."""
    received: list = []
    reply = {"ok": True, "error": ""}

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Relay.received.append(json.loads(body))
        self.send_response(302)
        self.send_header("Location", "/echo")
        self.end_headers()

    def do_GET(self):
        data = json.dumps(_Relay.reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def relay_url():
    _Relay.received = []
    _Relay.reply = {"ok": True, "error": ""}
    server = HTTPServer(("127.0.0.1", 0), _Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/exec"
    server.shutdown()


def test_send_to_relay(relay_url):
    report = _report()
    send_to_relay(report, relay_url, timeout_s=5, max_body_chars=20_000)
    (payload,) = _Relay.received
    assert payload["app"] == "xedge-live-interpreter"
    assert payload["body"] == report.report_text()
    assert payload["zip_name"] == "bug_20261012_093000.zip"
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["zip_b64"]))) as archive:
        assert archive.read(REPORT_FILE).decode() == report.report_text()


def test_relay_refusal_raises(relay_url):
    _Relay.reply = {"ok": False, "error": "unexpected request shape"}
    with pytest.raises(RelayError, match="unexpected request shape"):
        send_to_relay(_report(), relay_url, timeout_s=5, max_body_chars=20_000)


def test_no_url_raises():
    with pytest.raises(RelayError):
        send_to_relay(_report(), "", timeout_s=5, max_body_chars=20_000)



def test_email_template_names_address_and_file(tmp_path):
    from reporting.bug_report import email_template
    text = email_template(_report(), "support@example.com", tmp_path / "bug_1.zip")
    assert "To: support@example.com" in text
    assert f"Attach: {tmp_path / 'bug_1.zip'}" in text
    assert "Translation stopped." in text
