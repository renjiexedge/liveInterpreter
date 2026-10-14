"""App entry point: the setup wizard on first run (ui/setup_wizard.py), then the
home screen (ui/main_window.py), which opens each feature in its own window. `python -m ui.live_interpreter` still runs the Live Interpreter on its own.

Arguments (guide/distribution_plan.md §4.2):
  --selftest [--out PATH]  check the build and exit (selftest.py); no window opens
  --uninstall-cleanup      forget the stored API key; run by the uninstaller
  --dev / --release        override the release switch (version.py)
The app's own modules are imported only after the arguments are read, so a
broken import fails --selftest with its exit code instead of crashing first."""
import argparse
import ctypes
import sys

# Named mutex: Inno Setup's AppMutex looks for it to tell the app is running.
MUTEX_NAME = "XedgeLiveInterpreter"
_ERROR_ALREADY_EXISTS = 183
_mutex = None  # held for the life of the process


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="LiveInterpreter")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--out", help="--selftest: also write the report to this file")
    parser.add_argument("--uninstall-cleanup", action="store_true")
    parser.add_argument("--dev", action="store_true")      # read by version.py
    parser.add_argument("--release", action="store_true")  # read by version.py
    args, _qt_args = parser.parse_known_args(argv)  # anything else is left for Qt
    return args


def single_instance() -> bool:
    """False if another copy is already running: two copies would fight over
    the same audio devices."""
    global _mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS


def main() -> int:
    args = parse_args(sys.argv[1:])
    if args.selftest:
        from selftest import selftest
        return selftest(args.out)
    if args.uninstall_cleanup:
        from config.credentials import delete_api_key
        try:
            delete_api_key()
        except Exception:  # the uninstall goes on regardless
            return 1
        return 0

    from PySide6.QtWidgets import QApplication, QMessageBox

    from app_logging.setup import configure_logging
    from ui.style import apply_stylesheet
    from version import RELEASE

    # Checked before logging starts: a second copy must not rotate the first one's app.log.
    first = single_instance()
    if first:
        # Log file (and console in dev), plus uncaught exceptions and Qt warnings.
        configure_logging(release=RELEASE)
    app = QApplication(sys.argv)
    if not first:
        QMessageBox.information(None, "Xedge Live Interpreter",
                                "Live Interpreter is already open. Look for it on the taskbar.")
        return 0
    apply_stylesheet(app, watch=not RELEASE)  # watch: live-reload ui/style.qss on save (dev only)

    # First run: the setup wizard comes before the home screen; cancelling it quits.
    from storage.user_config import load_user_config
    if not load_user_config().setup_complete:
        from PySide6.QtWidgets import QDialog
        from ui.setup_wizard import SetupWizard
        if SetupWizard().exec() != QDialog.DialogCode.Accepted:
            return 0

    from ui.main_window import MainWindow
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
