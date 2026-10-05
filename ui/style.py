import re
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget

from ui.theme import TOKENS

STYLESHEET_PATH = Path(__file__).with_name("style.qss")
FONTS_DIR = Path(__file__).with_name("fonts")
_TOKEN = re.compile(r"@([a-z][a-z0-9-]*)")


def load_fonts() -> None:
    """Register the bundled brand fonts (Nunito Sans, Be Vietnam Pro). If one
    fails to load, the .qss falls back to Segoe UI."""
    for path in FONTS_DIR.glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(path))


def _render(text: str) -> str:
    """Replace @token references with ui/theme.TOKENS values; unknown ones are left as-is."""
    return _TOKEN.sub(lambda m: TOKENS.get(m.group(1), m.group(0)), text)


def apply_stylesheet(app: QApplication, watch: bool = False) -> None:
    """Load style.qss onto the whole app. With watch=True, reload it on every
    save so styles can be tweaked without restarting (dev convenience)."""
    load_fonts()
    app.setStyleSheet(_render(STYLESHEET_PATH.read_text(encoding="utf-8")))
    if not watch:
        return
    watcher = QFileSystemWatcher([str(STYLESHEET_PATH)], app)

    def reload(path: str) -> None:
        # Many editors save by replacing the file, which drops it from the watcher.
        if path not in watcher.files() and Path(path).exists():
            watcher.addPath(path)
        try:
            app.setStyleSheet(_render(Path(path).read_text(encoding="utf-8")))
        except OSError:
            pass  # caught mid-save; the next change event reloads it

    watcher.fileChanged.connect(reload)


def set_style_property(widget: QWidget, name: str, value: str) -> None:
    """Set a property used by a [name="value"] selector and re-apply the style;
    Qt doesn't re-evaluate selectors on its own when a property changes."""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
