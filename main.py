"""App entry point: the home screen (ui/main_window.py), which opens each feature
in its own window. `python -m ui.live_interpreter` still runs the Live Interpreter on its own."""
import logging
import sys

from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow
from ui.style import apply_stylesheet

if __name__ == "__main__":
    # Without this, INFO logs from the audio/ modules (e.g. the loop test's NCC
    # readings) are dropped: only warnings reach the console.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    app = QApplication(sys.argv)
    apply_stylesheet(app, watch=True)  # watch: live-reload ui/style.qss on save (dev only)

    window = MainWindow()
    window.show()
    app.exec()
