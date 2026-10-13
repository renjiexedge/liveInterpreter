"""`main.py --selftest [--out PATH]`: checks a (frozen) build without a person
watching (guide/distribution_plan.md §4.2, test matrix T13). A windowed exe has no
console, so the result is the exit code, plus a report written to --out.

Checks, in order: the app's modules and key libraries import, the bundled files
exist, PortAudio lists the WASAPI devices, the keyring backend is Windows
Credential Manager, and the home and Live Interpreter windows build (offscreen)."""
import importlib
import os
import sys
import traceback
from pathlib import Path

EXIT_OK, EXIT_IMPORT, EXIT_ASSETS, EXIT_AUDIO, EXIT_KEYRING, EXIT_UI = 0, 1, 2, 3, 4, 5

# Phase 6 adds ui.setup_wizard here.
MODULES = ("ui.main_window", "ui.live_interpreter", "ui.bug_report", "sessions.manager",
           "engines.gemini_live", "audio.routing_check", "audio.health", "audio.virtual_audio",
           "reporting.bug_report", "storage.user_config", "config.credentials",
           "google.genai", "soxr", "sounddevice", "PySide6.QtSvg", "keyring")
ASSETS = ("audio_test/loop_test.wav", "ui/style.qss", "ui/assets/logo_square.png",
          "ui/assets/logo_wide.png", "ui/fonts/NunitoSans-Variable.ttf",
          "ui/fonts/BeVietnamPro-SemiBold.ttf")


def _finish(out: str | None, lines: list[str], code: int, detail=None) -> int:
    lines.append("result: OK" if code == EXIT_OK else f"result: FAILED (exit {code}): {detail}")
    text = "\n".join(lines) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
    if sys.stdout is not None:  # None in a windowed exe
        sys.stdout.write(text)
    return code


def selftest(out: str | None = None) -> int:
    from version import RELEASE, __version__
    lines = [f"version {__version__} (release={RELEASE}, frozen={getattr(sys, 'frozen', False)})"]

    for module in MODULES:
        try:
            importlib.import_module(module)
        except Exception:
            return _finish(out, lines, EXIT_IMPORT, f"import {module}\n{traceback.format_exc()}")
    lines.append(f"imports: {len(MODULES)} ok")

    from config.paths import resource_path
    missing = [name for name in ASSETS if not resource_path(name).exists()]
    if missing:
        return _finish(out, lines, EXIT_ASSETS, f"missing {missing}")
    lines.append(f"assets: {len(ASSETS)} ok")

    try:
        from audio.devices import list_input_devices, list_output_devices
        inputs, outputs = list_input_devices(), list_output_devices()  # loads PortAudio + WASAPI
    except Exception:
        return _finish(out, lines, EXIT_AUDIO, traceback.format_exc())
    lines += [f"in:  {d.name}" for d in inputs]
    lines += [f"out: {d.name}" for d in outputs]
    # Informational, not a failure: a test PC may not have the drivers yet.
    names = [d.name.lower() for d in (*inputs, *outputs)]
    lines.append(f"drivers: VB-Cable {'found' if any('cable' in n for n in names) else 'missing'}, "
                 f"Voicemeeter {'found' if any('voicemeeter' in n for n in names) else 'missing'}")

    import keyring
    backend = type(keyring.get_keyring()).__name__
    lines.append(f"keyring: {backend}")
    if backend != "WinVaultKeyring":
        return _finish(out, lines, EXIT_KEYRING, backend)

    try:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"  # never show a window
        from PySide6.QtWidgets import QApplication
        from ui.live_interpreter import LiveInterpreterWindow
        from ui.main_window import MainWindow
        from ui.style import apply_stylesheet
        app = QApplication([])
        apply_stylesheet(app)
        windows = [MainWindow(), LiveInterpreterWindow()]
        app.processEvents()
        for window in windows:
            window.close()
        app.processEvents()
    except Exception:
        return _finish(out, lines, EXIT_UI, traceback.format_exc())
    lines.append("ui: ok")
    return _finish(out, lines, EXIT_OK)
