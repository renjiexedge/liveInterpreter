"""First-run setup wizard (guide/distribution_plan.md Phase 6, §4.5).

main.py opens it before the home screen until UserConfig.setup_complete is set;
the profile menu's "Setup wizard…" re-runs it. Pages: name, VB-Audio drivers,
headset, WhatsApp settings, audio test, Gemini key, done. Nothing is saved until
Finish: the name, the four devices and setup_complete go to user_config.json,
the key to Windows Credential Manager.

The audio test plays into WhatsApp's mic wire and opens WhatsApp's ends of both
wires, which a session never does: run it before any call. The wizard also
re-scans PortAudio (to see drivers installed since launch), which breaks open
streams, so the home window never opens it alongside a feature window."""
import html
import logging
import os
import threading

import httpx
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget, QWizard, QWizardPage)
from keyring.errors import KeyringError

from audio.devices import (DeviceInfo, default_device_indices, list_input_devices, list_output_devices,
                           rescan_devices)
from audio.health import IssueCode
from audio.routing_check import RouteCheckError, check_route, run_loop_test
from audio.virtual_audio import (DOWNLOAD_PAGES, REQUIRED_DEVICES, VB_CABLE, VOICEMEETER_BANANA,
                                 WHATSAPP_OUTGOING_DEFAULT, check_drivers, missing_products,
                                 voicemeeter_running)
from config import credentials
from config.audio_config import AudioDeviceConfig
from config.model_config import DEFAULT_MODEL
from config.paths import resource_path
from config.session_config import (CANDIDATE_TO_STAFF, STAFF_OUTPUT_LANGUAGE, STAFF_TO_CANDIDATE,
                                   DirectionConfig)
from config.ui_config import UserProfile
from engines.gemini_live import reset_client, validate_key
from sessions.manager import failure_code
from storage.user_config import load_user_config, save_user_config
from ui.issue_panel import StatusDot
from ui.live_interpreter import DEVICE_ROLES, headset_candidates, is_virtual_device
from ui.style import set_style_property
from ui.visual_guide import drivers_guide, voicemeeter_guide, whatsapp_guide
from version import RELEASE, __version__

logger = logging.getLogger(__name__)

WINDOW_ICON_PATH = resource_path("ui/assets/logo_square.png")
# In a CheckRow detail: opens the Voicemeeter routing guide (CheckRow.link_activated).
VOICEMEETER_GUIDE_LINK = '<a href="guide:voicemeeter">Show me how</a>'

# UserConfig.devices keys (the Live Interpreter window's device_combos keys).
APP_INCOMING = f"{CANDIDATE_TO_STAFF}.capture"
HEADSET_SPEAKER = f"{CANDIDATE_TO_STAFF}.playback"
HEADSET_MIC = f"{STAFF_TO_CANDIDATE}.capture"
APP_OUTGOING = f"{STAFF_TO_CANDIDATE}.playback"


def _link(product: str) -> str:
    url = DOWNLOAD_PAGES[product]
    return f'<a href="{url}">{html.escape(url)}</a>'


class CheckRow(QWidget):
    """One check: a status dot, a title and a detail line (rich text: http(s)
    links open in the browser, others are emitted as link_activated). States:
    pending (grey), ok, warning, problem."""

    link_activated = Signal(str)

    def __init__(self, title: str):
        super().__init__()
        self.state = "pending"
        self._dot = StatusDot()
        self._title = QLabel(f"<b>{html.escape(title)}</b>")
        self._detail = QLabel()
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._detail.linkActivated.connect(self._on_link)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(self._title)
        text.addWidget(self._detail)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.addWidget(self._dot, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(text, stretch=1)
        self.set_state("pending", "")

    def set_state(self, state: str, detail_html: str) -> None:
        self.state = state
        set_style_property(self._dot, "severity", state)
        self._detail.setText(detail_html)
        self._detail.setVisible(bool(detail_html))

    def _on_link(self, href: str) -> None:
        if href.startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(href))
        else:
            self.link_activated.emit(href)


def _intro(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setOpenExternalLinks(True)
    return label


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

class WelcomePage(QWizardPage):
    """1. Welcome, and the name shown in the profile (decision 11)."""

    def __init__(self, display_name: str):
        super().__init__()
        self.setTitle("Welcome to Xedge Live Interpreter")
        self.setSubTitle("This checks your audio setup and saves the translation service key. "
                         "It takes about five minutes.")
        self.name_edit = QLineEdit(display_name)
        self.name_edit.setPlaceholderText(UserProfile().name())  # the Windows user name
        self.name_edit.setMaxLength(60)
        form = QFormLayout()
        form.addRow("What should we call you?", self.name_edit)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addSpacing(16)
        layout.addWidget(_intro(
            "Have these ready:<ul>"
            "<li>a <b>headset</b> (not speakers),</li>"
            f"<li><b>{VB_CABLE}</b> and <b>{VOICEMEETER_BANANA}</b> installed (the next page has an installation guide),</li>"
            "<li>the <b>API key</b> you were given.</li></ul>"
            "Don't start a WhatsApp call yet: the audio test plays test sounds into WhatsApp's microphone."))
        layout.addStretch()


class DriversPage(QWizardPage):
    """2. The four VB-Audio endpoints, and whether Banana is running."""

    def __init__(self):
        super().__init__()
        self.setTitle("Virtual audio drivers")
        self.setSubTitle("WhatsApp and the app are connected through VB-Cable (the call's sound) "
                         "and Voicemeeter Banana (the translation sent into the call).")
        self.found: dict[str, DeviceInfo | None] = {}
        self._rows = {VB_CABLE: CheckRow(f"{VB_CABLE} installed"),
                      VOICEMEETER_BANANA: CheckRow(f"{VOICEMEETER_BANANA} installed"),
                      "running": CheckRow(f"{VOICEMEETER_BANANA} running")}
        recheck = QPushButton("Re-check")
        recheck.clicked.connect(self.refresh)
        guide = QPushButton("Installation guide")
        guide.setToolTip(f"Step by step: download and install {VB_CABLE} and {VOICEMEETER_BANANA}.")
        guide.clicked.connect(self._show_guide)
        self._guide = None  # created on first use, then reused (keeps its step)
        buttons = QHBoxLayout()
        buttons.addWidget(recheck)
        buttons.addWidget(guide)
        buttons.addStretch()
        layout = QVBoxLayout(self)
        for row in self._rows.values():
            layout.addWidget(row)
        layout.addLayout(buttons)
        layout.addStretch()

    def _show_guide(self) -> None:
        if self._guide is None:
            self._guide = drivers_guide(self)
        self._guide.present()

    def initializePage(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        if not self.wizard().audio_page.running:  # re-scanning would break the test's streams
            rescan_devices()  # a driver installed since launch only shows up after this
        self.found = check_drivers()
        missing = missing_products(self.found)
        for product in (VB_CABLE, VOICEMEETER_BANANA):
            names = [req.prefix for key, req in REQUIRED_DEVICES.items() if req.product == product]
            absent = [req.prefix for key, req in REQUIRED_DEVICES.items()
                      if req.product == product and self.found[key] is None]
            if product in missing:
                self._rows[product].set_state("problem", (
                    f"Not found: {html.escape(', '.join(absent))}. Install {product} from "
                    f"{_link(product)} (see the Installation guide), restart the computer if the installer "
                    "asks, then press Re-check."))
            else:
                self._rows[product].set_state("ok", f"Found {html.escape(' and '.join(names))}.")
        running_row = self._rows["running"]
        if VOICEMEETER_BANANA in missing:
            running_row.set_state("pending", "Checked once Voicemeeter Banana is installed.")
        elif voicemeeter_running():
            running_row.set_state("ok", "It starts with Windows. If you ever close it, open it again "
                                        "from the Start menu.")
        else:
            running_row.set_state("warning", "It's closed, so the translation can't reach the call. "
                                             "Open Voicemeeter Banana from the Start menu, then press Re-check.")
        logger.info("Setup wizard drivers: missing %s, Banana running: %s",
                    missing or "none", running_row.state == "ok")
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        # A closed Banana is only a warning (the process name could differ); the
        # audio test's AUX -> B2 check fails anyway if it really is closed.
        return bool(self.found) and not missing_products(self.found)


class HeadsetPage(QWizardPage):
    """3. The only device choice testers make: the WhatsApp ones are fixed."""

    def __init__(self, saved_devices: dict[str, str]):
        super().__init__()
        self.setTitle("Your headset")
        self.setSubTitle("Choose the headset you wear for interviews. Use a headset, not speakers: "
                         "speakers would let the microphone hear the translation.")
        self._saved = saved_devices
        self._devices: dict[int, DeviceInfo] = {}  # combo item data (index) -> device
        self.mic_combo, self.speaker_combo = QComboBox(), QComboBox()
        self._hint = QLabel()
        self._hint.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Headset microphone:", self.mic_combo)
        form.addRow("Headset speaker:", self.speaker_combo)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._hint)
        layout.addStretch()
        for combo in (self.mic_combo, self.speaker_combo):
            combo.currentIndexChanged.connect(self._changed)

    def initializePage(self) -> None:
        # Rebuilt on every visit: the drivers page may have re-scanned the devices.
        inputs, outputs = list_input_devices(), list_output_devices()
        default_input, default_output = default_device_indices()
        self._devices = {d.index: d for d in (*inputs, *outputs)}
        for combo, devices, key, default in ((self.mic_combo, inputs, HEADSET_MIC, default_input),
                                             (self.speaker_combo, outputs, HEADSET_SPEAKER, default_output)):
            previous = combo.currentText() or self._saved.get(key, "")
            combo.blockSignals(True)
            combo.clear()
            for device in devices:
                combo.addItem(device.name, device.index)
            index = combo.findText(previous) if previous else -1
            if index < 0:
                index = next((combo.findData(i) for i in headset_candidates(default, devices)), -1)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)
        self._changed()

    def _changed(self) -> None:
        virtual = [c.currentText() for c in (self.mic_combo, self.speaker_combo)
                   if c.currentText() and is_virtual_device(c.currentText())]
        self._hint.setText(f"'{virtual[0]}' is a virtual audio device, not a headset. Choose your "
                           "headset unless you know you need it." if virtual else "")
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self.mic_combo.currentIndex() >= 0 and self.speaker_combo.currentIndex() >= 0

    def devices(self) -> dict[str, DeviceInfo]:
        return {HEADSET_MIC: self._devices[self.mic_combo.currentData()],
                HEADSET_SPEAKER: self._devices[self.speaker_combo.currentData()]}


class WhatsAppPage(QWizardPage):
    """4. What to set in WhatsApp, in every call (decision 7)."""

    def __init__(self):
        super().__init__()
        self.setTitle("WhatsApp settings for every call")
        self.setSubTitle("WhatsApp forgets its audio devices after each call, so set them again in every call.")
        self._text = _intro("")
        self._text.setTextFormat(Qt.TextFormat.RichText)
        guide = QPushButton("Visual guide")
        guide.setToolTip("Where these settings are in a WhatsApp call, with screenshots.")
        guide.clicked.connect(self._show_guide)
        self._guide = None
        layout = QVBoxLayout(self)
        layout.addWidget(self._text)
        layout.addWidget(guide, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()

    def _show_guide(self) -> None:
        if self._guide is not None:
            self._guide.close()
            self._guide.deleteLater()
        found = self.wizard().drivers_page.found
        self._guide = whatsapp_guide(found["whatsapp_speaker"].name, found["whatsapp_microphone"].name, self)
        self._guide.present()

    def initializePage(self) -> None:
        found = self.wizard().drivers_page.found
        speaker = found["whatsapp_speaker"].name
        microphone = found["whatsapp_microphone"].name
        self._text.setText(
            "Once the call has started, open WhatsApp's audio settings for the call and choose:"
            f"<ul><li><b>Speaker</b>: {html.escape(speaker)}</li>"
            f"<li><b>Microphone</b>: {html.escape(microphone)}</li></ul>"
            "If the Microphone is set to your headset, the candidate hears you <b>untranslated</b>, "
            "and the app can't detect it. The Live Interpreter window repeats this reminder.")


class AudioTestPage(QWizardPage):
    """5. Loop test (no loops) and route checks (each wire carries sound, PC sound
    stays off WhatsApp's mic). Every check must pass."""

    # (run number, check key, state, detail html), from the worker thread.
    step_done = Signal(int, str, str, str)
    run_finished = Signal(int)

    CHECKS = {
        "loop": "No audio loops",
        "incoming": "The call's sound reaches the app (VB-Cable)",
        "outgoing": "The translation reaches WhatsApp's microphone (Voicemeeter AUX → B2)",
        "pc_sound": "Other PC sound stays out of WhatsApp's microphone",
    }

    def __init__(self):
        super().__init__()
        self.setTitle("Audio test")
        self.setSubTitle("Run this before any call: it plays short test sounds into your headset and "
                         "into WhatsApp's microphone. It takes about half a minute.")
        self._rows = {key: CheckRow(title) for key, title in self.CHECKS.items()}
        for row in self._rows.values():
            row.link_activated.connect(self._on_link)
        self._guide = None  # the Voicemeeter routing guide, from a check's "Show me how"
        self._run_button = QPushButton("Run audio test")
        self._run_button.clicked.connect(self._start)
        self._status = QLabel()
        self._run = 0           # results from an older run are ignored
        self.running = False    # a worker thread holds the devices
        self.step_done.connect(self._on_step)
        self.run_finished.connect(self._on_finished)
        layout = QVBoxLayout(self)
        for row in self._rows.values():
            layout.addWidget(row)
        buttons = QHBoxLayout()
        buttons.addWidget(self._run_button)
        buttons.addWidget(self._status, stretch=1)
        layout.addLayout(buttons)
        layout.addStretch()

    def initializePage(self) -> None:
        # The devices may have changed since the last visit: test again.
        self._run += 1
        for row in self._rows.values():
            row.set_state("pending", "")
        self._status.setText("Checking…" if self.running else "")
        self._run_button.setEnabled(not self.running)
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return not self.running and all(row.state == "ok" for row in self._rows.values())

    def _on_link(self, href: str) -> None:
        if href == "guide:voicemeeter":
            if self._guide is None:
                self._guide = voicemeeter_guide(self)
            self._guide.present()

    def _start(self) -> None:
        if self.running:
            return
        # Read on the GUI thread: the worker gets plain data, never the wizard's widgets.
        devices = self.wizard().devices()
        found = dict(self.wizard().drivers_page.found)
        default_output = default_device_indices()[1]
        default_output = next((d for d in list_output_devices() if d.index == default_output), None)
        self._run += 1
        run = self._run
        self.running = True
        self._run_button.setEnabled(False)
        self._status.setText("Checking… you'll hear short test sounds in your headset.")
        for row in self._rows.values():
            row.set_state("pending", "")
        self.completeChanged.emit()
        threading.Thread(target=self._worker, args=(run, devices, found, default_output), daemon=True).start()

    def _worker(self, run: int, devices: dict[str, DeviceInfo], found: dict[str, DeviceInfo],
                default_output: DeviceInfo | None) -> None:
        checks = {
            "loop": lambda: self._check_loops(devices),
            "incoming": lambda: self._check_incoming(devices, found["whatsapp_speaker"]),
            "outgoing": lambda: self._check_outgoing(devices, found["whatsapp_microphone"]),
            "pc_sound": lambda: self._check_pc_sound(default_output, found["whatsapp_microphone"]),
        }
        try:
            for key, check in checks.items():
                try:
                    state, detail = check()
                except RouteCheckError as exc:
                    state, detail = "problem", (
                        f"Couldn't open '{html.escape(exc.device)}'. It may be in use by another app in "
                        "exclusive mode: close it, then run the test again.")
                except Exception as exc:  # never leave the page stuck on "Checking…"
                    logger.exception("Setup wizard audio check %s failed", key)
                    state, detail = "problem", f"The check couldn't run ({html.escape(type(exc).__name__)})."
                logger.info("Setup wizard audio check %s: %s", key, state)
                self.step_done.emit(run, key, state, detail)
        finally:
            self.run_finished.emit(run)

    @staticmethod
    def _check_loops(devices: dict[str, DeviceInfo]) -> tuple[str, str]:
        def config(key):
            return AudioDeviceConfig(device_index=devices[key].index)
        directions = [
            DirectionConfig(CANDIDATE_TO_STAFF, config(APP_INCOMING), config(HEADSET_SPEAKER), STAFF_OUTPUT_LANGUAGE),
            DirectionConfig(STAFF_TO_CANDIDATE, config(HEADSET_MIC), config(APP_OUTGOING), "zh-Hans"),
        ]
        issues = run_loop_test(directions)  # every playback, including into WhatsApp's mic wire
        if not issues:
            return "ok", "No output leaks back into an input the app listens to."
        return "problem", "<br>".join(f"{html.escape(i.message)} {html.escape(i.action)}" for i in issues)

    @staticmethod
    def _check_incoming(devices: dict[str, DeviceInfo], speaker: DeviceInfo) -> tuple[str, str]:
        if check_route(speaker, devices[APP_INCOMING]):
            return "ok", "Sound WhatsApp plays into CABLE Input arrives at CABLE Output."
        return "problem", (f"The test sound played into '{html.escape(speaker.name)}' didn't arrive at "
                           f"'{html.escape(devices[APP_INCOMING].name)}'. Reinstall {VB_CABLE}, restart the "
                           "computer, then run the test again.")

    @staticmethod
    def _check_outgoing(devices: dict[str, DeviceInfo], microphone: DeviceInfo) -> tuple[str, str]:
        if check_route(devices[APP_OUTGOING], microphone):
            return "ok", "The AUX strip is routed to B2."
        closed = "" if voicemeeter_running() else "Voicemeeter Banana is closed: open it from the Start menu. "
        return "problem", (f"{closed}The test sound played into '{html.escape(devices[APP_OUTGOING].name)}' didn't "
                           f"reach '{html.escape(microphone.name)}'. In Voicemeeter Banana, turn <b>B2</b> on for "
                           f"the <b>AUX</b> strip (and A1, A2, A3, B1 off), then run the test again. "
                           f"{VOICEMEETER_GUIDE_LINK}")

    @staticmethod
    def _check_pc_sound(default_output: DeviceInfo | None, microphone: DeviceInfo) -> tuple[str, str]:
        if default_output is None:
            return "ok", "Windows has no default speaker, so there's no PC sound to leak."
        if default_output.name.startswith(WHATSAPP_OUTGOING_DEFAULT):
            return "problem", (f"Windows plays all PC sound into '{html.escape(default_output.name)}', so the "
                               "candidate would hear it. In Windows sound settings, choose your headset (or "
                               "Voicemeeter Input) as the output device, then run the test again.")
        if check_route(default_output, microphone):
            return "problem", (f"PC sound (played on '{html.escape(default_output.name)}') reaches WhatsApp's "
                               "microphone, so the candidate would hear notifications and music. In Voicemeeter "
                               "Banana, turn <b>B2 off on every strip except AUX</b>, then run the test again. "
                               f"{VOICEMEETER_GUIDE_LINK}")
        return "ok", f"Sound played on the Windows default speaker ('{html.escape(default_output.name)}') stays out."

    def _on_step(self, run: int, key: str, state: str, detail: str) -> None:
        if run == self._run:
            self._rows[key].set_state(state, detail)

    def _on_finished(self, run: int) -> None:
        self.running = False
        self._run_button.setEnabled(True)
        if run == self._run:
            passed = all(row.state == "ok" for row in self._rows.values())
            self._status.setText("All checks passed." if passed else "Fix the problems above, then run the test again.")
        else:
            self._status.setText("")
        self.completeChanged.emit()


class KeyPage(QWizardPage):
    """6. The Gemini key (decision 2): checked, then saved at Finish."""

    key_checked = Signal(int, str, str)  # (attempt, key or "", error html), from the worker thread

    def __init__(self):
        super().__init__()
        self.setTitle("Translation service key")
        self.setSubTitle("Paste the key you were given. It's kept in Windows Credential Manager, "
                         "never in a file, and never included in bug reports.")
        self.verified_key = ""
        self._attempt = 0
        self._has_key = bool(credentials.get_api_key())
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Leave empty to keep the saved key" if self._has_key else "Paste the key here")
        self.key_edit.textChanged.connect(self._edited)
        self._check_button = QPushButton("Check key")
        self._check_button.clicked.connect(self._check)
        self._row = CheckRow("Key")
        self.key_checked.connect(self._on_checked)
        entry = QHBoxLayout()
        entry.addWidget(self.key_edit, stretch=1)
        entry.addWidget(self._check_button)
        layout = QVBoxLayout(self)
        layout.addLayout(entry)
        layout.addWidget(self._row)
        if not RELEASE and credentials.ENV_VAR in os.environ:
            layout.addWidget(_intro(f"Dev: {credentials.ENV_VAR} is set (.env), and it wins over a saved key."))
        layout.addStretch()
        self._edited()

    def _edited(self) -> None:
        self._attempt += 1  # a check still running is for the old text
        self.verified_key = ""
        typed = bool(self.key_edit.text().strip())
        self._check_button.setEnabled(typed)
        if typed:
            self._row.set_state("pending", "Press Check key.")
        elif self._has_key:
            self._row.set_state("ok", "A key is already saved.")
        else:
            self._row.set_state("pending", "")
        self.completeChanged.emit()

    def _check(self) -> None:
        key = self.key_edit.text().strip()
        self._attempt += 1
        attempt = self._attempt
        self._check_button.setEnabled(False)
        self._row.set_state("pending", "Checking…")

        def work():
            try:
                validate_key(key)
                self.key_checked.emit(attempt, key, "")
            except Exception as exc:
                self.key_checked.emit(attempt, "", self._explain(exc))
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _explain(exc: Exception) -> str:
        # Never log the key or the exception text: only its type and status code.
        code = getattr(exc, "code", None)
        logger.warning("Setup wizard key check failed: %s (code %s)", type(exc).__name__, code)
        if code == 404:
            return (f"This key works, but it can't use the translation model ({DEFAULT_MODEL}). "
                    "Contact your administrator.")
        if isinstance(exc, httpx.TransportError):
            return "Couldn't reach the translation service. Check the internet connection, then try again."
        match failure_code(exc):
            case IssueCode.SERVICE_AUTH_FAILED:
                return "The key wasn't accepted. Check you copied all of it, then try again."
            case IssueCode.QUOTA_EXCEEDED:
                return "The service limit is reached. Contact your administrator."
            case IssueCode.NETWORK_LOST:
                return "Couldn't reach the translation service. Check the internet connection, then try again."
        return f"Couldn't check the key ({html.escape(type(exc).__name__)}). Try again in a moment."

    def _on_checked(self, attempt: int, key: str, error: str) -> None:
        if attempt != self._attempt:
            return
        self._check_button.setEnabled(bool(self.key_edit.text().strip()))
        if key:
            self.verified_key = key
            self._row.set_state("ok", f"Valid, and it can use {DEFAULT_MODEL}. It's saved when you press Finish.")
        else:
            self._row.set_state("problem", error)
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        if self.key_edit.text().strip():
            return bool(self.verified_key)
        return self._has_key


class DonePage(QWizardPage):
    """7. Reminders; Finish saves everything."""

    def __init__(self):
        super().__init__()
        self.setTitle("All set")
        self.setSubTitle("Press Finish to save.")
        layout = QVBoxLayout(self)
        layout.addWidget(_intro(
            "Before each interview:<ol>"
            "<li>Keep <b>Voicemeeter Banana</b> open.</li>"
            "<li>Open <b>Live Interpreter</b> and choose the language the candidate hears.</li>"
            "<li>Start the WhatsApp call, then set its <b>Speaker</b> and <b>Microphone</b> "
            "(previous page of this wizard).</li>"
            "<li>Press <b>Start Live Translation</b>.</li></ol>"
            "To change your headset, name or key later, open the profile menu → <b>Setup wizard…</b>"))
        layout.addStretch()


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

class SetupWizard(QWizard):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("setupWizard")
        self.setWindowTitle("Set up Xedge Live Interpreter")
        self.setWindowIcon(QIcon(str(WINDOW_ICON_PATH)))
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage)
        self.setMinimumSize(720, 560)
        self._config = load_user_config()
        self.welcome_page = WelcomePage(self._config.display_name)
        self.drivers_page = DriversPage()
        self.headset_page = HeadsetPage(self._config.devices)
        self.audio_page = AudioTestPage()
        self.key_page = KeyPage()
        for page in (self.welcome_page, self.drivers_page, self.headset_page, WhatsAppPage(),
                     self.audio_page, self.key_page, DonePage()):
            self.addPage(page)

    def devices(self) -> dict[str, DeviceInfo]:
        """All four roles: the routing's fixed devices plus the chosen headset."""
        found = self.drivers_page.found
        return {APP_INCOMING: found["app_incoming"], APP_OUTGOING: found["app_outgoing"],
                **self.headset_page.devices()}

    def reject(self) -> None:
        if self.audio_page.running:  # the worker thread still holds the devices
            QMessageBox.information(self, "Audio test running", "Wait for the audio test to finish.")
            return
        super().reject()

    def accept(self) -> None:
        key = self.key_page.verified_key
        if key:
            try:
                credentials.set_api_key(key)
            except KeyringError as exc:
                logger.error("Couldn't save the key to Credential Manager: %s", type(exc).__name__)
                QMessageBox.warning(self, "Key not saved",
                                    "Couldn't save the key in Windows Credential Manager. Try again, "
                                    "or contact your administrator.")
                return
            reset_client()
        config = load_user_config()  # keep anything else saved meanwhile
        config.display_name = self.welcome_page.name_edit.text().strip()
        config.devices = {key: device.name for key, device in self.devices().items()}
        config.setup_complete = True
        config.setup_version = __version__
        try:
            save_user_config(config)
        except OSError as exc:
            logger.error("Couldn't save the settings: %s", exc)
            QMessageBox.warning(self, "Settings not saved", f"Couldn't save the settings: {exc}")
            return
        logger.info("Setup wizard finished (devices: %s)",
                    {DEVICE_ROLES[k][0].rstrip(':'): v for k, v in config.devices.items()})
        super().accept()
