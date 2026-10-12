"""configure_logging(): the app's one logging setup, called by each entry point.

Everything goes to a rotating log file in the user data folder (3 x 1 MB:
app.log, app.log.1, app.log.2) at DEBUG, so ChunkStats playback lines are kept
for bug reports. In dev it also goes to the console. Logs must stay
content-free: failure causes and audio diagnostics, never transcript text
(guide/distribution_plan.md decision 13).

Also routes what would otherwise be lost in a windowed build (no console):
uncaught exceptions on any thread, and Qt's own warnings."""
import logging
import platform
import sys
import threading
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

from config.paths import log_file

LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 2
_FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(threadName)s]: %(message)s"
_CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logger = logging.getLogger("app")

_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def configure_logging(release: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)  # library DEBUG stays out; app loggers set their own level
    for handler in list(root.handlers):
        root.removeHandler(handler)

    path = log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                                           encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        root.addHandler(file_handler)
    except OSError as exc:  # keep running without a file rather than not at all
        print(f"Couldn't open the log file {path}: {exc}", file=sys.stderr)

    # A windowed exe has no stdout (it's None).
    if not release and sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt="%H:%M:%S"))
        root.addHandler(console)

    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_uncaught
    qInstallMessageHandler(_log_qt_message)
    logger.info("Starting (release=%s, Python %s, Windows %s). Log file: %s",
                release, platform.python_version(), platform.version(), path)


def _log_uncaught(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb))


def _log_thread_uncaught(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread is not None else "?"
    logger.critical("Uncaught exception in thread %s", name,
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def _log_qt_message(msg_type, context, message: str) -> None:
    logging.getLogger("qt").log(_QT_LEVELS.get(msg_type, logging.WARNING), "%s", message)
