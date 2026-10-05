import platform
import sys
from datetime import datetime

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QLabel, QMessageBox,
                               QPlainTextEdit, QVBoxLayout)

from config.ui_config import BugReportConfig


class BugReportDialog(QDialog):
    """Report a bug (design.md §1). Where reports go (email, form or issue tracker)
    isn't decided yet, so for now each report is saved as a text file in
    BugReportConfig.reports_dir."""

    def __init__(self, parent=None, config: BugReportConfig | None = None):
        super().__init__(parent)
        self.setObjectName("bugReportDialog")
        self.setWindowTitle("Report a bug")
        self.setMinimumWidth(460)
        self._config = config or BugReportConfig()

        self._description = QPlainTextEdit()
        self._description.setPlaceholderText(
            "What happened, and what did you expect? Include the steps if you can.")
        self._include_system = QCheckBox("Include system details (Windows and Python versions)")
        self._include_system.setChecked(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Describe the problem:"))
        layout.addWidget(self._description, stretch=1)
        layout.addWidget(self._include_system)
        layout.addWidget(buttons)

    def _save(self) -> None:
        text = self._description.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "Report a bug", "Please describe the problem first.")
            return
        now = datetime.now()
        lines = [f"Reported: {now.isoformat(timespec='seconds')}", "", text]
        if self._include_system.isChecked():
            lines += ["", f"OS: {platform.platform()}", f"Python: {sys.version.split()[0]}"]
        try:
            self._config.reports_dir.mkdir(parents=True, exist_ok=True)
            path = self._config.reports_dir / f"bug_{now:%Y%m%d_%H%M%S}.txt"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Report a bug", f"Couldn't save the report: {exc}")
            return
        QMessageBox.information(self, "Report a bug", f"Thanks. The report was saved to:\n{path}")
        self.accept()
