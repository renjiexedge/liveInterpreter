from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from audio.health import Issue, IssueCode, Severity

SEVERITY_COLOURS = {
    Severity.OK: "#2e7d32",
    Severity.WARNING: "#f9a825",
    Severity.PROBLEM: "#c62828",
}
_ICONS = {Severity.WARNING: "⚠", Severity.PROBLEM: "⛔"}


class StatusDot(QLabel):
    """Coloured ● showing the worst active severity."""

    def __init__(self):
        super().__init__("●")
        self.set_severity(Severity.OK)

    def set_severity(self, severity: Severity) -> None:
        self.setStyleSheet(f"color: {SEVERITY_COLOURS[severity]}; font-size: 18px;")


class IssuePanel(QWidget):
    """Lists every active Issue as a message plus what to do about it. Rows are
    grouped by (direction label, code); the panel hides itself when empty."""

    resume_clicked = Signal(str)  # direction label, from an echo row's Resume button

    def __init__(self):
        super().__init__()
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._rows: dict[tuple[str, IssueCode], list[tuple[Issue, QWidget]]] = {}
        self.setVisible(False)

    def show_issue(self, issue: Issue) -> None:
        """Raise, update or clear: replaces any row with the same (label, code)."""
        self._remove((issue.label, issue.code))
        if issue.active:
            self.add_issue(issue)
        self._refresh()

    def add_issue(self, issue: Issue) -> None:
        """Adds a row without replacing others with the same code (pre-start checks
        can report several of one kind)."""
        row = self._build_row(issue)
        self._rows.setdefault((issue.label, issue.code), []).append((issue, row))
        self._layout.addWidget(row)
        self._refresh()

    def clear(self, codes=None) -> None:
        """Remove every row, or only rows whose code is in `codes`."""
        for key in list(self._rows):
            if codes is None or key[1] in codes:
                self._remove(key)
        self._refresh()

    def worst(self) -> Severity:
        return max((issue.level for rows in self._rows.values() for issue, _ in rows),
                   default=Severity.OK)

    def _remove(self, key) -> None:
        for _, row in self._rows.pop(key, []):
            row.deleteLater()

    def _refresh(self) -> None:
        self.setVisible(bool(self._rows))

    def _build_row(self, issue: Issue) -> QWidget:
        colour = SEVERITY_COLOURS[issue.level]
        frame = QFrame()
        frame.setStyleSheet(f"QFrame {{ border-left: 4px solid {colour}; padding-left: 6px; }}"
                            "QLabel { border: none; }")
        layout = QHBoxLayout(frame)
        text = QVBoxLayout()
        message = QLabel(f"{_ICONS.get(issue.level, '')} {issue.message}")
        message.setWordWrap(True)
        message.setStyleSheet("font-weight: bold;")
        action = QLabel(issue.action)
        action.setWordWrap(True)
        text.addWidget(message)
        text.addWidget(action)
        layout.addLayout(text, stretch=1)
        if issue.muted:
            resume = QPushButton("Resume audio")
            resume.clicked.connect(lambda: self.resume_clicked.emit(issue.label))
            layout.addWidget(resume)
        return frame
