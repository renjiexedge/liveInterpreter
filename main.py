"""App entry point: the home screen (ui/main_window.py), which opens each feature
in its own window. `python -m ui.live_interpreter` still runs the Live Interpreter on its own."""
import sys

from PySide6.QtWidgets import QApplication

from app_logging.setup import configure_logging
from ui.main_window import MainWindow
from ui.style import apply_stylesheet
from version import RELEASE

if __name__ == "__main__":
    # Log file (and console in dev), plus uncaught exceptions and Qt warnings.
    configure_logging(release=RELEASE)

    app = QApplication(sys.argv)
    apply_stylesheet(app, watch=not RELEASE)  # watch: live-reload ui/style.qss on save (dev only)

    window = MainWindow()
    window.show()
    app.exec()
