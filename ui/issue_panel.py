from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from audio.health import Issue, IssueCode, Severity
from ui.style import set_style_property

# Severity colours live in ui/style.qss, keyed on the "severity" property.
_ICONS = {Severity.WARNING: "⚠", Severity.PROBLEM: "⛔"}


class StatusDot(QLabel):
    """Coloured ● showing the worst active severity."""

    def __init__(self):
        super().__init__("●")
        self.setObjectName("statusDot")
        self.set_severity(Severity.OK)

    def set_severity(self, severity: Severity) -> None:
        set_style_property(self, "severity", severity.name.lower())


class IssuePanel(QWidget):
    """Lists every active Issue as a message plus what to do about it. Rows are
    grouped by (direction label, code); the panel hides itself when empty.

    direction_names maps a direction label to the name shown at the start of its
    rows (e.g. "A" -> "Candidate-to-staff"), since with two directions a message
    like "Translated audio is stuttering." doesn't say which one."""

    resume_clicked = Signal(str)  # direction label, from an echo row's Resume button

    def __init__(self, direction_names: dict[str, str] | None = None):
        super().__init__()
        self._direction_names = direction_names or {}
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

    def worst(self, label: str | None = None) -> Severity:
        """Worst active severity, over every row or only one direction's."""
        return max((issue.level for (row_label, _), rows in self._rows.items()
                    if label is None or row_label == label
                    for issue, _ in rows),
                   default=Severity.OK)

    def _remove(self, key) -> None:
        for _, row in self._rows.pop(key, []):
            row.deleteLater()

    def _refresh(self) -> None:
        self.setVisible(bool(self._rows))

    def _build_row(self, issue: Issue) -> QWidget:
        frame = QFrame()
        frame.setObjectName("issueRow")
        frame.setProperty("severity", issue.level.name.lower())
        layout = QHBoxLayout(frame)
        text = QVBoxLayout()
        direction = self._direction_names.get(issue.label)
        prefix = f"{direction}: " if direction else ""
        message = QLabel(f"{_ICONS.get(issue.level, '')} {prefix}{issue.message}")
        message.setWordWrap(True)
        message.setProperty("role", "issueMessage")
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
