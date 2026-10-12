import logging
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                               QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from config.paths import log_file
from config.ui_config import BugReportConfig
from reporting.bug_report import (BugReport, email_template, log_tail, mailto_url, send_to_relay,
                                  system_details)

logger = logging.getLogger(__name__)


class BugReportDialog(QDialog):
    """Report a bug (guide/distribution_plan.md §4.1). Two pages: write the
    description and pick what to include, then a read-only preview of exactly
    what will be sent, where Send unlocks. The zip is always saved to
    BugReportConfig.reports_dir first. Send posts it to the relay; if that fails,
    or the tester picks "Send it myself", the folder and an email draft open.

    context: "Devices and settings" lines from the open Live Interpreter window
    (LiveInterpreterWindow.report_context), or None if it isn't open."""

    # (sent, message) from the relay worker thread; the slot runs on the GUI thread.
    _send_finished = Signal(bool, str)

    def __init__(self, parent=None, reporter: str = "", context: dict[str, str] | None = None,
                 config: BugReportConfig | None = None):
        super().__init__(parent)
        self.setObjectName("bugReportDialog")
        self.setWindowTitle("Report a bug")
        self.setMinimumSize(560, 420)
        self._config = config or BugReportConfig()
        self._reporter = reporter
        self._context = context
        self._report: BugReport | None = None
        self._zip_path: Path | None = None
        self._sending = False
        self._send_finished.connect(self._on_send_finished)

        self._pages = QStackedWidget()
        self._pages.addWidget(self._build_compose_page())
        self._pages.addWidget(self._build_preview_page())
        layout = QVBoxLayout(self)
        layout.addWidget(self._pages)

    # ---- pages -------------------------------------------------------------
    def _build_compose_page(self) -> QWidget:
        self._description = QPlainTextEdit()
        self._description.setPlaceholderText(
            "What happened, and what did you expect? Include the steps if you can.")
        self._include_system = QCheckBox("System details (Windows and Python versions)")
        self._include_settings = QCheckBox("Devices and settings (device names, languages, "
                                           "model, current problems)")
        self._include_log = QCheckBox(f"Recent log (last {self._config.log_lines} lines)")
        for box in (self._include_system, self._include_settings, self._include_log):
            box.setChecked(True)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        preview = QPushButton("Preview…")
        preview.setDefault(True)
        preview.clicked.connect(self._show_preview)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(preview)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Describe the problem:"))
        layout.addWidget(self._description, stretch=1)
        layout.addWidget(QLabel("Include:"))
        for box in (self._include_system, self._include_settings, self._include_log):
            layout.addWidget(box)
        layout.addLayout(buttons)
        return page

    def _build_preview_page(self) -> QWidget:
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._back_button = QPushButton("Back")
        self._back_button.clicked.connect(lambda: self._pages.setCurrentIndex(0))
        self._manual_button = QPushButton("Send it myself")
        self._manual_button.setToolTip("Save the report and open an email draft to "
                                       f"{self._config.email}, without sending anything automatically.")
        self._manual_button.clicked.connect(self._send_manually)
        self._send_button = QPushButton("Send")
        self._send_button.setProperty("variant", "primary")
        self._send_button.clicked.connect(self._send)
        self._close_button = QPushButton("Close")
        self._close_button.clicked.connect(self.accept)
        self._close_button.setVisible(False)
        buttons = QHBoxLayout()
        buttons.addWidget(self._back_button)
        buttons.addStretch()
        buttons.addWidget(self._manual_button)
        buttons.addWidget(self._send_button)
        buttons.addWidget(self._close_button)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._preview_title = QLabel("This is exactly what will be sent:")
        layout.addWidget(self._preview_title)
        layout.addWidget(self._preview, stretch=1)
        layout.addWidget(self._status)
        layout.addLayout(buttons)
        return page

    # ---- report ------------------------------------------------------------
    def build_report(self) -> BugReport:
        sections = {}
        if self._include_system.isChecked():
            sections["System"] = system_details()
        if self._include_settings.isChecked():
            sections["Devices and settings"] = (
                self._context if self._context is not None
                else {"Live Interpreter": "window not open"})
        log_lines = (log_tail(log_file(), self._config.log_lines)
                     if self._include_log.isChecked() else None)
        return BugReport(description=self._description.toPlainText().strip(),
                         reporter=self._reporter, sections=sections, log_lines=log_lines)

    def _show_preview(self) -> None:
        if not self._description.toPlainText().strip():
            QMessageBox.information(self, "Report a bug", "Please describe the problem first.")
            return
        self._report = self.build_report()  # frozen: Send sends exactly this
        self._zip_path = None
        self._preview.setPlainText(self._report.preview_text())
        self._status.setText("")
        self._pages.setCurrentIndex(1)

    def _save_zip(self) -> Path | None:
        if self._zip_path is None:
            try:
                self._zip_path = self._report.save_zip(self._config.reports_dir)
            except OSError as exc:
                logger.warning("Couldn't save the bug report: %s", exc)
        return self._zip_path

    # ---- sending -----------------------------------------------------------
    def _send(self) -> None:
        self._save_zip()  # kept even when sending works (decision 12)
        if not self._config.endpoint_url:
            logger.info("No bug report relay configured; using the email fallback.")
            self._fall_back("Automatic sending isn't set up in this build.")
            return
        self._set_sending(True)
        self._status.setText("Sending…")
        report, config = self._report, self._config

        def work() -> None:
            try:
                send_to_relay(report, config.endpoint_url, config.timeout_s, config.max_body_chars)
            except Exception as exc:
                logger.warning("Bug report relay failed: %s", exc)
                self._send_finished.emit(False, str(exc))
            else:
                logger.info("Bug report sent through the relay.")
                self._send_finished.emit(True, "")

        threading.Thread(target=work, name="bug-report-send", daemon=True).start()

    def _on_send_finished(self, sent: bool, error: str) -> None:
        self._set_sending(False)
        if not sent:
            self._fall_back("Couldn't send the report automatically.")
            return
        kept = f" A copy is kept at:\n{self._zip_path}" if self._zip_path else ""
        self._finish(f"Sent. Thank you.{kept}")

    def _send_manually(self) -> None:
        self._save_zip()
        self._fall_back("")

    def _fall_back(self, reason: str) -> None:
        """Open the folder with the zip selected, and an email draft to attach it to."""
        if self._zip_path is None:
            self._status.setText(f"{reason} The report couldn't be saved either, so please "
                                 f"email {self._config.email} and describe the problem.".strip())
            return
        try:
            # Opens the folder with the zip already selected.
            subprocess.Popen(f'explorer /select,"{self._zip_path}"')
        except OSError as exc:
            logger.warning("Couldn't open the bug report folder: %s", exc)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._zip_path.parent)))
        # The preview box now shows the email to send, in case no draft opens
        # (or the tester uses webmail): the address and which file to attach.
        self._preview_title.setText("Send this email, with the file attached:")
        self._preview.setPlainText(email_template(self._report, self._config.email, self._zip_path))
        if QDesktopServices.openUrl(QUrl(mailto_url(self._report, self._config.email, self._zip_path))):
            steps = (f"A draft to {self._config.email} opened in your email app (it may be behind this "
                     f"window). Attach {self._zip_path.name} from the folder that opened, then send it.")
        else:
            steps = (f"Please send the email above from your email account, attaching "
                     f"{self._zip_path.name} from the folder that opened.")
        self._finish(f"{reason} {steps}".strip())

    def _finish(self, message: str) -> None:
        self._status.setText(message)
        for button in (self._back_button, self._manual_button, self._send_button):
            button.setVisible(False)
        self._close_button.setVisible(True)
        self._close_button.setDefault(True)

    def _set_sending(self, sending: bool) -> None:
        self._sending = sending
        for button in (self._back_button, self._manual_button, self._send_button):
            button.setEnabled(not sending)

    # The relay thread reports back to this dialog, so it stays open until then
    # (at most BugReportConfig.timeout_s).
    def reject(self) -> None:
        if not self._sending:
            super().reject()

    def closeEvent(self, event) -> None:
        if self._sending:
            event.ignore()
        else:
            super().closeEvent(event)
