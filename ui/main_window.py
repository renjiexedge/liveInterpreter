from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QLabel,
                               QMainWindow, QMenu, QMessageBox, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from config.ui_config import UserProfile
from ui.bug_report import BugReportDialog
from ui.style import set_style_property

ASSETS_DIR = Path(__file__).with_name("assets")
ICONS_DIR = ASSETS_DIR / "icons"
LOGO_PATH = ASSETS_DIR / "logo_wide.png"
WINDOW_ICON_PATH = ASSETS_DIR / "logo_square.png"
FEATURE_COLUMNS = 3  # TransGull fits three feature buttons per row
PROFILE_NAME_MAX_WIDTH = 180  # px; longer display names are cut short with "…"


@dataclass(frozen=True)
class Feature:
    """One feature button. create() builds the feature's top-level window; a
    feature without one is shown as "Coming soon" and can't be clicked."""

    key: str
    title: str
    icon: str  # file name in ui/assets/icons
    create: Callable[[], QWidget] | None = None


def _create_live_interpreter() -> QWidget:
    # Imported on first open: it pulls in the audio and Gemini stack.
    from ui.live_interpreter import LiveInterpreterWindow
    return LiveInterpreterWindow()


FEATURES = [
    Feature("live_interpreter", "Live Interpreter", "mic.svg", _create_live_interpreter),
    Feature("live_transcript", "Live Transcript", "transcript.svg"),  # coming soon
]


def _transparent_label(text: str = "", role: str | None = None) -> QLabel:
    """A label inside a button: clicks go through to the button."""
    label = QLabel(text)
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    if role:
        label.setProperty("role", role)
    return label


class FeatureButton(QPushButton):
    """TransGull-style pill: icon + label, plus a dot while the feature's window is
    open, or a "Coming soon" tag (and disabled) for a feature that isn't built yet."""

    def __init__(self, feature: Feature):
        super().__init__()
        soon = feature.create is None
        self.setProperty("role", "feature")
        self.setProperty("state", "soon" if soon else "closed")
        self.setFixedSize(280, 52)  # fits icon + label + "Coming soon" tag
        self.setAccessibleName(f"{feature.title} (coming soon)" if soon else feature.title)
        if soon:
            self.setEnabled(False)
        else:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

        icon = _transparent_label()
        icon.setPixmap(QIcon(str(ICONS_DIR / feature.icon)).pixmap(QSize(20, 20)))
        self._open_dot = _transparent_label("●", role="openDot")
        self._open_dot.setToolTip("This feature's window is open.")
        self._open_dot.setVisible(False)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(10)
        layout.addWidget(icon)
        layout.addWidget(_transparent_label(feature.title, role="featureLabel"))
        layout.addWidget(self._open_dot)
        layout.addStretch()
        if soon:
            pill = _transparent_label("Coming soon", role="soonPill")
            pill.setFixedHeight(20)
            layout.addWidget(pill, alignment=Qt.AlignmentFlag.AlignVCenter)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 13))  # rgba(0,0,0,0.05), as on the site
        self.setGraphicsEffect(shadow)

    def set_open(self, is_open: bool) -> None:
        self._open_dot.setVisible(is_open)
        set_style_property(self, "state", "open" if is_open else "closed")


class _ElidedLabel(QLabel):
    """Shows the full text up to its maximum width, then cuts it short with "…".
    Re-elides whenever its font changes (the stylesheet's font arrives after
    construction), and puts the full text in tooltip_target's tooltip when cut."""

    def __init__(self, text: str, max_width: int, tooltip_target: QWidget):
        super().__init__()
        self._full_text = text
        self._tooltip_target = tooltip_target
        self.setMaximumWidth(max_width)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._elide()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._elide()

    def _elide(self) -> None:
        text = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, self.maximumWidth())
        if text != self.text():
            self.setText(text)
        self._tooltip_target.setToolTip(self._full_text if text != self._full_text else "")


class ProfileButton(QPushButton):
    """Avatar + display name. A placeholder until there are user accounts.
    The pill grows to fit the name, up to PROFILE_NAME_MAX_WIDTH for the name."""

    def __init__(self, profile: UserProfile, menu: QMenu):
        super().__init__()
        self.setObjectName("profileButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"Profile: {profile.name()}")
        avatar = _transparent_label(profile.initials())
        avatar.setObjectName("avatar")
        avatar.setFixedSize(32, 32)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name = _ElidedLabel(profile.name(), PROFILE_NAME_MAX_WIDTH, tooltip_target=self)
        name.setProperty("role", "profileName")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 12, 4)
        layout.setSpacing(8)
        layout.addWidget(avatar)
        layout.addWidget(name)
        self.setFixedHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setMenu(menu)

    # QPushButton sizes itself from its own text, ignoring child layouts: use the
    # layout's size instead, so the pill follows the name once its font is styled.
    def sizeHint(self) -> QSize:
        return QSize(self.layout().sizeHint().width(), 40)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()  # the name's size changed: tell the header row
        return super().event(event)


class MainWindow(QMainWindow):
    """Home screen (design.md §1): a header with the logo, Report a bug and the
    profile, then the feature buttons.
    Each feature opens in its own top-level window, one instance per feature."""

    def __init__(self, profile: UserProfile | None = None):
        super().__init__()
        self.setObjectName("homeWindow")
        self.setWindowTitle("Xedge Resource")
        self.setWindowIcon(QIcon(str(WINDOW_ICON_PATH)))
        self.setMinimumSize(QSize(900, 600))
        self.resize(1200, 800)

        self._profile = profile or UserProfile()
        self._windows: dict[str, QWidget] = {}  # open feature windows, by Feature.key
        self._feature_buttons: dict[str, FeatureButton] = {}
        # Set when closing this window waits for a feature window that is
        # stopping its session; this window closes once they're all gone.
        self._close_pending = False

        central = QWidget()
        central.setObjectName("homeCentral")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_header())
        layout.addWidget(self._build_content(), stretch=1)
        self.setCentralWidget(central)
        # Start with no button focused: a focus ring at launch looks like a hover.
        central.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        central.setFocus()

    # ---- layout ------------------------------------------------------------
    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("homeHeader")
        logo = QLabel()
        logo.setObjectName("homeLogo")
        logo.setPixmap(QPixmap(str(LOGO_PATH)).scaledToHeight(
            36, Qt.TransformationMode.SmoothTransformation))
        logo.setAccessibleName("Xedge Resource")

        bug_button = QPushButton("Report a bug")
        bug_button.setProperty("role", "pill")
        bug_button.setIcon(QIcon(str(ICONS_DIR / "bug.svg")))
        bug_button.setCursor(Qt.CursorShape.PointingHandCursor)
        bug_button.clicked.connect(self._report_bug)

        menu = QMenu(self)
        menu.setObjectName("profileMenu")
        menu.addAction("About", self._show_about)

        row = QHBoxLayout(header)
        row.setContentsMargins(24, 12, 24, 12)
        row.setSpacing(12)
        row.addWidget(logo)
        row.addStretch()
        row.addWidget(bug_button)
        row.addWidget(ProfileButton(self._profile, menu))
        return header

    def _build_content(self) -> QWidget:
        content = QWidget()
        content.setObjectName("homeContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        grid = QGridLayout()
        grid.setSpacing(12)
        for i, feature in enumerate(FEATURES):
            button = FeatureButton(feature)
            button.clicked.connect(lambda _=False, f=feature: self.open_feature(f))
            self._feature_buttons[feature.key] = button
            grid.addWidget(button, i // FEATURE_COLUMNS, i % FEATURE_COLUMNS)
        grid.setColumnStretch(FEATURE_COLUMNS, 1)  # keep the buttons left-aligned
        layout.addLayout(grid)
        layout.addStretch()
        return content

    # ---- feature windows ---------------------------------------------------
    def open_feature(self, feature: Feature) -> None:
        """Open the feature's window, or bring it to the front if it's already open:
        two interpreter windows would fight over the same audio devices."""
        window = self._windows.get(feature.key)
        if window is None:
            window = feature.create()
            window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            window.destroyed.connect(lambda _=None, key=feature.key: self._on_feature_closed(key))
            self._windows[feature.key] = window
            self._feature_buttons[feature.key].set_open(True)
        if window.isMinimized():
            window.showNormal()
        window.show()
        window.raise_()
        window.activateWindow()

    def _on_feature_closed(self, key: str) -> None:
        self._windows.pop(key, None)
        button = self._feature_buttons.get(key)
        if button is not None:
            button.set_open(False)
        if self._close_pending and not self._windows:
            self.close()

    def closeEvent(self, event) -> None:
        """Closing the home screen closes every feature window first. A feature
        window may refuse (the user cancelled) or defer (it is stopping its
        session; it sets close_pending and closes itself when idle)."""
        still_open = [window for window in list(self._windows.values()) if not window.close()]
        if still_open:
            self._close_pending = any(getattr(w, "close_pending", False) for w in still_open)
            event.ignore()
            return
        super().closeEvent(event)

    # ---- header actions ----------------------------------------------------
    def _report_bug(self) -> None:
        BugReportDialog(self).exec()

    def _show_about(self) -> None:
        QMessageBox.about(self, "About", "Xedge Resource\nLive Interpreter (preview)")
