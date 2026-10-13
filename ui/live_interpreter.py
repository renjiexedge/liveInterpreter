import html
import logging
import sys
import threading


from PySide6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QPushButton, QWidget,
                               QMainWindow, QLabel, QVBoxLayout, QComboBox, QScrollArea, QMessageBox)
from PySide6.QtCore import Qt, QSize, Signal, QTimer
from PySide6.QtGui import QIcon

from audio.devices import default_device_indices, list_input_devices, list_output_devices
from app_logging.setup import configure_logging
from audio.health import PRE_START_CODES, Issue, IssueCode, Severity, log_issue
from audio.routing_check import cable_key, routing_fingerprint, run_loop_test, validate_routing
from config.audio_config import AudioDeviceConfig
from config.model_config import DEFAULT_MODEL, LIVE_MODELS
from config.paths import resource_path
from config.session_config import (CANDIDATE_TO_STAFF, DIRECTION_NAMES, STAFF_OUTPUT_LANGUAGE,
                                   STAFF_TO_CANDIDATE, DirectionConfig, TwoWayConfig)
from engines.gemini_live import api_key_present
from sessions.manager import DirectionState, SessionManager, State
from ui.issue_panel import IssuePanel, StatusDot
from ui.style import apply_stylesheet
from version import RELEASE, display_version

logger = logging.getLogger(__name__)

WINDOW_ICON_PATH = resource_path("ui/assets/logo_square.png")


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    """A titled white card (QFrame[role="card"] in ui/style.qss). Returns the card
    and the layout to put its contents in."""
    card = QFrame()
    card.setProperty("role", "card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 12, 16, 16)
    layout.setSpacing(10)
    heading = QLabel(title)
    heading.setProperty("role", "cardTitle")
    layout.addWidget(heading)
    return card, layout


def _divider() -> QFrame:
    """Thin vertical line between the two direction columns."""
    line = QFrame()
    line.setProperty("role", "divider")
    line.setFixedWidth(1)
    return line

# Southeast Asian languages: display name -> BCP-47 language code
SOUTHEAST_ASIA_LANGUAGES = {
    "Burmese (Myanmar)": "my",
    "Filipino (Philippines)": "fil",
    "Indonesian (Indonesia)": "id",
    "Khmer (Cambodia)": "km",
    "Lao (Laos)": "lo",
    "Malay (Malaysia)": "ms",
    "Tamil (Singapore)": "ta",
    "Thai (Thailand)": "th",
    "Vietnamese (Vietnam)": "vi",
    "Chinese, Mandarin (Singapore)": "zh-Hans",
    "English (Singapore)": "en",
}

# Group box titles for the two directions.
DIRECTION_TITLES = {
    CANDIDATE_TO_STAFF: "Candidate → Staff (staff hear English)",
    STAFF_TO_CANDIDATE: "Staff → Candidate (candidate hears the chosen language)",
}

# The four device roles, keyed "<direction label>.<capture|playback>" — the same
# keys validate_routing and routing_fingerprint use for expected device names.
# Value: (combo label, True for an input device / False for an output device).
DEVICE_ROLES = {
    f"{CANDIDATE_TO_STAFF}.capture": ("WhatsApp incoming (capture):", True),
    f"{CANDIDATE_TO_STAFF}.playback": ("Headset playback:", False),
    f"{STAFF_TO_CANDIDATE}.capture": ("Headset microphone:", True),
    f"{STAFF_TO_CANDIDATE}.playback": ("WhatsApp outgoing (playback):", False),
}

# Spec §4.2 mute controls: each silences one direction's translated audio only.
MUTE_BUTTON_TEXT = {
    CANDIDATE_TO_STAFF: "Mute Candidate Translation",  # stops translated audio to staff's headset
    STAFF_TO_CANDIDATE: "Mute Staff Translation",      # stops translated audio sent to the candidate
}

_DIRECTION_STATUS = {
    DirectionState.CONNECTING: "Connecting…",
    DirectionState.CONNECTED: "Connected. Waiting for the other direction…",
    DirectionState.RECONNECTING: "Reconnecting…",
    DirectionState.STOPPED: "Stopped.",
    DirectionState.FAILED: "Failed. See the message below.",
}


DEFAULT_CANDIDATE_LANGUAGE = "Chinese, Mandarin (Singapore)"

# Each direction needs its own virtual wire to/from WhatsApp, and the free VB-Cable
# is only one wire (CABLE Input -> CABLE Output). Voicemeeter's AUX strip is the second:
#   A (incoming): WhatsApp speaker -> CABLE Input -> CABLE Output -> app
#   B (outgoing): app -> Voicemeeter AUX Input -> bus B2 only -> Voicemeeter Out B2 -> WhatsApp mic
# Not "Voicemeeter Input": that's the Windows default speaker, so it carries all PC sound.
WHATSAPP_INCOMING_DEFAULT = "CABLE Output"
WHATSAPP_OUTGOING_DEFAULT = "Voicemeeter AUX Input"
# Voicemeeter virtual-input strip -> the B bus to route it to (alone) for WhatsApp's mic.
VOICEMEETER_STRIP_BUS = {"Voicemeeter AUX Input": "B2", "Voicemeeter VAIO3 Input": "B3", "Voicemeeter Input": "B1"}
# The two WhatsApp combos -> what installs their device. In release they're locked
# to the defaults above (guide/distribution_plan.md decision 6: testers only pick
# their headset); in dev they stay editable so other cables can be tried.
WHATSAPP_PRODUCTS = {
    f"{CANDIDATE_TO_STAFF}.capture": "VB-Cable",
    f"{STAFF_TO_CANDIDATE}.playback": "Voicemeeter Banana",
}
LOCK_WHATSAPP_DEVICES = RELEASE
_VIRTUAL_DEVICE_WORDS = ("cable", "voicemeeter", "vb-audio")


def is_virtual_device(name: str) -> bool:
    """VB-Cable / Voicemeeter endpoints. Never preselected for the headset roles."""
    return any(word in name.casefold() for word in _VIRTUAL_DEVICE_WORDS)


def _headset_candidates(default_index: int | None, devices) -> list[int]:
    """The Windows default device, unless it's a virtual one (e.g. Voicemeeter set
    as the Windows default), then the first real device."""
    names = {d.index: d.name for d in devices}
    real = [d.index for d in devices if not is_virtual_device(d.name)]
    first = [default_index] if default_index in names and not is_virtual_device(names[default_index]) else []
    return first + real[:1]


def _preselect_candidates(inputs, outputs) -> dict[str, list[str | int]]:
    """Per role, what to preselect: a device-name prefix or a sounddevice index.
    The first one present wins; if none is, the combo starts empty and
    validate_routing asks the user to choose."""
    default_input, default_output = default_device_indices()
    return {
        f"{CANDIDATE_TO_STAFF}.capture": [WHATSAPP_INCOMING_DEFAULT],
        f"{CANDIDATE_TO_STAFF}.playback": _headset_candidates(default_output, outputs),
        f"{STAFF_TO_CANDIDATE}.capture": _headset_candidates(default_input, inputs),
        f"{STAFF_TO_CANDIDATE}.playback": [WHATSAPP_OUTGOING_DEFAULT],
    }


def _other_cable_end(device_name: str, other_side) -> str | None:
    """A plain cable's other end: the same name apart from Input/Output."""
    matches = [d.name for d in other_side if cable_key(d.name) == cable_key(device_name)]
    # VB-Cable also has a 16-channel "CABLE In 16ch" end; prefer plain "Input".
    matches.sort(key=lambda name: "input" not in name.casefold())
    return matches[0] if matches else None


def whatsapp_device(key: str, device_name: str, inputs, outputs) -> str | None:
    """The device to choose in WhatsApp for the one picked in a WhatsApp combo
    (the other end of the wire), or None if there's no single answer."""
    if not device_name:
        return None
    incoming = key == f"{CANDIDATE_TO_STAFF}.capture"
    other = _other_cable_end(device_name, outputs if incoming else inputs)
    if other is None and not incoming:
        bus = VOICEMEETER_STRIP_BUS.get(device_name.split(" (")[0])
        if bus is not None:
            short = f"Voicemeeter Out {bus}"
            other = next((d.name for d in inputs if d.name.startswith(short)), short)
    return other


def whatsapp_hint(key: str, device_name: str, inputs, outputs) -> str:
    """What to choose in WhatsApp's audio settings for the device picked in one of
    the WhatsApp combos. WhatsApp needs the OTHER end of the wire: its Speaker
    list only has playback devices and its Microphone list only recording ones."""
    incoming = key == f"{CANDIDATE_TO_STAFF}.capture"
    setting = "Speaker" if incoming else "Microphone"
    if not device_name:
        return f"Choose a device. In WhatsApp, set {setting} to its other end."
    other = _other_cable_end(device_name, outputs if incoming else inputs)
    if other is not None:
        return f"In WhatsApp, set {setting} to: {other}"
    if "voicemeeter" in device_name.casefold():
        if incoming:
            return ("In WhatsApp, set Speaker to a Voicemeeter virtual input that is routed "
                    "only to this bus (in the Voicemeeter app).")
        strip = device_name.split(" (")[0]
        bus = VOICEMEETER_STRIP_BUS.get(strip)
        if bus is None:
            return (f"In the Voicemeeter app, route the '{strip}' strip to one B bus that nothing else "
                    "uses. In WhatsApp, set Microphone to that bus's 'Voicemeeter Out' device.")
        return (f"In the Voicemeeter app, route the '{strip}' strip to {bus} only, and nothing else to {bus}. "
                f"In WhatsApp, set Microphone to: Voicemeeter Out {bus}")
    return f"'{device_name}' isn't a virtual cable, so WhatsApp can't be connected to it."


# The Live Interpreter feature window, opened from ui/main_window.py.
class LiveInterpreterWindow(QMainWindow):
    # The signals below are emitted from sessions.manager.SessionManager's
    # background asyncio thread. Qt signals are safe to emit from a non-GUI
    # thread; the connected slots always run on the GUI thread.
    # (direction label, {"input": ..., "output": ...}) whenever a transcript pair finishes.
    transcript_received = Signal(str, dict)
    # (State.value, message) on every session state change; "idle" is always last.
    session_state_changed = Signal(str, str)
    # (label, DirectionState.value, message) per direction.
    direction_state_changed = Signal(str, str, str)
    # audio.health.Issue raised/updated/cleared by the session's HealthMonitor.
    health_changed = Signal(object)
    # (fingerprint, start_after, partial, list[Issue]) from the loop-test worker thread.
    loop_test_finished = Signal(object)

    def __init__(self):
        super().__init__()
        self.transcript_received.connect(self._on_transcript_received)
        self.session_state_changed.connect(self._on_session_state_changed)
        self.direction_state_changed.connect(self._on_direction_state_changed)
        self.health_changed.connect(self._on_health_changed)
        self.loop_test_finished.connect(self._on_loop_test_finished)

        # Owns the session thread, its asyncio loop, the routers and the health
        # monitor. The GUI only calls start()/stop()/resume() on it.
        self._manager = SessionManager(
            on_state=lambda state, message: self.session_state_changed.emit(state.value, message),
            on_direction_state=lambda label, state, message: self.direction_state_changed.emit(label, state.value, message),
            on_transcript=self.transcript_received.emit,
            on_issue=self.health_changed.emit,
        )
        self._session_error = ""  # set by the ERROR state, shown when the session reaches idle
        self._session_translating = False
        self._direction_states: dict[str, DirectionState] = {}
        self._direction_messages: dict[str, str] = {}  # e.g. "Connection lost. Reconnecting…"
        self._loop_test_running = False
        # Device combinations that passed the loop test; Start requires one.
        self._passed_fingerprints: set[frozenset] = set()
        # ...or passed the Start-triggered test, which doesn't play into the
        # WhatsApp outgoing route (that would play the test sound to the
        # candidate mid-call). Start allows these, with LOOP_TEST_PARTIAL shown.
        self._partial_fingerprints: set[frozenset] = set()
        # Set when closing waits for the session (or loop test) to finish; the
        # window closes itself then. Read by ui/main_window.py.
        self.close_pending = False

        # Styled by the #interpreterWindow rules in ui/style.qss (design.md §6).
        self.setObjectName("interpreterWindow")
        self.setWindowTitle(f"Xedge Live Interpreter {display_version()}")
        self.setWindowIcon(QIcon(str(WINDOW_ICON_PATH)))
        # Set the window dimensions (16:10) for a good default size
        self.setMinimumSize(QSize(1120, 760))

        # Languages: the language each side hears.
        candidate_label = QLabel("Candidate hears:")
        candidate_combo = QComboBox()
        # Restrict selection to the dropdown only; user cannot type/edit the contents.
        candidate_combo.setEditable(False)
        for language_name, language_code in SOUTHEAST_ASIA_LANGUAGES.items():
            candidate_combo.addItem(language_name, language_code)
        candidate_combo.setCurrentText(DEFAULT_CANDIDATE_LANGUAGE)
        # Item data holds the BCP-47 code direction B translates into.
        self.language_combo = candidate_combo
        staff_label = QLabel("Staff hears:")
        # Locked to STAFF_OUTPUT_LANGUAGE (spec V1); a combo so it can be unlocked later.
        self.staff_language_combo = QComboBox()
        self.staff_language_combo.addItem("English", STAFF_OUTPUT_LANGUAGE)
        self.staff_language_combo.setEnabled(False)
        languages_card, languages_body = _card("Languages")
        languages_row = QHBoxLayout()
        languages_row.setSpacing(12)
        languages_row.addWidget(candidate_label)
        languages_row.addWidget(candidate_combo, stretch=1)
        languages_row.addSpacing(24)
        languages_row.addWidget(staff_label)
        languages_row.addWidget(self.staff_language_combo, stretch=1)
        languages_body.addLayout(languages_row)

        # Audio devices: one column per direction with its status dot + text,
        # mute button and two devices. Item data in each device combo holds the
        # sounddevice index.
        inputs, outputs = list_input_devices(), list_output_devices()
        preselect = _preselect_candidates(inputs, outputs)
        self.device_combos: dict[str, QComboBox] = {}
        # Under each WhatsApp combo: what to pick in WhatsApp's own audio settings.
        self.whatsapp_hints: dict[str, QLabel] = {}
        self.direction_dots: dict[str, StatusDot] = {}
        self.direction_status: dict[str, QLabel] = {}
        self.mute_buttons: dict[str, QPushButton] = {}
        self.retry_buttons: dict[str, QPushButton] = {}
        self._locked_device_keys = frozenset(WHATSAPP_PRODUCTS) if LOCK_WHATSAPP_DEVICES else frozenset()
        # Next to Start: what to set in WhatsApp, which resets its devices every call.
        self.whatsapp_checklist = QLabel()
        self.whatsapp_checklist.setWordWrap(True)
        self.whatsapp_checklist.setProperty("role", "checklist")
        devices_card, devices_body = _card("Audio devices")
        directions_layout = QHBoxLayout()
        directions_layout.setSpacing(24)
        devices_body.addLayout(directions_layout)
        for i, (label, title) in enumerate(DIRECTION_TITLES.items()):
            if i:
                directions_layout.addWidget(_divider())
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(8)
            directions_layout.addLayout(grid, stretch=1)
            direction_title = QLabel(title)
            direction_title.setProperty("role", "directionTitle")
            direction_title.setWordWrap(True)  # shares its row with the mute button
            dot, status = StatusDot(), QLabel("Idle.")
            status.setProperty("role", "directionStatus")
            self.direction_dots[label], self.direction_status[label] = dot, status
            # Mute stays usable at all times: before Start it applies once the session runs.
            mute = QPushButton(MUTE_BUTTON_TEXT[label])
            mute.setProperty("role", "mute")
            mute.setCheckable(True)
            mute.toggled.connect(lambda checked, label=label: self._on_mute_toggled(label, checked))
            self.mute_buttons[label] = mute
            # Shown only while this direction has failed and the other keeps running
            # (DEGRADED): restarts just this direction.
            retry = QPushButton("Retry")
            retry.setToolTip("Restart this direction. The other direction keeps translating.")
            retry.clicked.connect(lambda _=False, label=label: self._retry_direction(label))
            retry.setVisible(False)
            self.retry_buttons[label] = retry
            title_row = QHBoxLayout()
            title_row.addWidget(dot)
            title_row.addWidget(direction_title, stretch=1)
            title_row.addWidget(retry)
            title_row.addWidget(mute)
            grid.addLayout(title_row, 0, 0, 1, 2)
            grid.addWidget(status, 1, 0, 1, 2)
            row = 2
            for role in ("capture", "playback"):
                key = f"{label}.{role}"
                role_label, is_input = DEVICE_ROLES[key]
                combo = QComboBox()
                combo.setEditable(False)
                for device in inputs if is_input else outputs:
                    combo.addItem(device.name, device.index)
                self._preselect(combo, preselect[key])
                if key in self._locked_device_keys:
                    combo.setEnabled(False)
                    combo.setToolTip(f"Fixed: the setup guide connects WhatsApp through "
                                     f"{WHATSAPP_PRODUCTS[key]}.")
                self.device_combos[key] = combo
                grid.addWidget(QLabel(role_label), row, 0)
                grid.addWidget(combo, row, 1)
                row += 1
                if key in (f"{CANDIDATE_TO_STAFF}.capture", f"{STAFF_TO_CANDIDATE}.playback"):
                    hint = QLabel()
                    hint.setWordWrap(True)
                    hint.setProperty("role", "hint")
                    self.whatsapp_hints[key] = hint
                    grid.addWidget(hint, row, 1)
                    row += 1
                    combo.currentIndexChanged.connect(
                        lambda _=None, key=key: self._update_whatsapp_hint(key, inputs, outputs))
                    self._update_whatsapp_hint(key, inputs, outputs)
            grid.setColumnStretch(1, 1)
            grid.setRowStretch(row, 1)  # keep both columns top-aligned
        self._update_whatsapp_checklist(inputs, outputs)

        # Header: title, the model picker, then the audio health indicator (a dot
        # for the worst active issue and the status line).
        self.status_dot = StatusDot()
        self.status_label = QLabel("Ready. Press Test Audio Devices or Start.")
        self.status_label.setProperty("role", "statusLine")
        header = QFrame()
        header.setObjectName("interpreterHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        title = QLabel("Live Interpreter")
        title.setProperty("role", "windowTitle")
        # Phase 6b: compare Gemini Live models on quality and cost without code
        # edits. Item data = model id; both directions use it; fixed while running.
        self.model_combo = QComboBox()
        self.model_combo.setEditable(False)
        for model_name, model_id in LIVE_MODELS.items():
            self.model_combo.addItem(model_name, model_id)
            self.model_combo.setItemData(self.model_combo.count() - 1, model_id, Qt.ItemDataRole.ToolTipRole)
        self.model_combo.setCurrentIndex(max(0, self.model_combo.findData(DEFAULT_MODEL)))
        self.model_combo.setToolTip("Translation model for both directions. "
                                    "It can't be changed while translating.")
        header_layout.addWidget(title)
        header_layout.addSpacing(24)
        header_layout.addWidget(QLabel("Model:"))
        header_layout.addWidget(self.model_combo)
        header_layout.addStretch()
        header_layout.addWidget(self.status_dot)
        header_layout.addWidget(self.status_label)

        # Actions: Test Audio Devices on the left; Start and Stop on the right.
        self.audio_test_button = QPushButton("Test Audio Devices")
        self.audio_test_button.setToolTip(
            "Plays a short test sound on every output, including into WhatsApp. "
            "Run it before the call starts: during a call the candidate would hear it.")
        self.audio_test_button.clicked.connect(self.test_audio_devices)
        self.start_button = QPushButton("Start Live Translation")
        self.start_button.setProperty("variant", "primary")
        self.stop_button = QPushButton("Stop Live Translation")
        self.stop_button.setProperty("variant", "danger")
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_live_translation)
        self.stop_button.clicked.connect(self.stop_live_translation)
        actions_layout = QHBoxLayout()
        actions_layout.setSpacing(12)
        actions_layout.addWidget(self.audio_test_button)
        actions_layout.addSpacing(12)
        actions_layout.addWidget(self.whatsapp_checklist, stretch=1)
        actions_layout.addWidget(self.start_button)
        actions_layout.addWidget(self.stop_button)

        # The issue panel lists each problem with what to do about it; it hides
        # itself while there are none.
        self.issue_panel = IssuePanel(direction_names=DIRECTION_NAMES)
        self.issue_panel.resume_clicked.connect(self._resume_audio)

        # One scroll area of transcript pairs per direction, side by side. Each
        # completed {"input": ..., "output": ...} pair becomes one label, appended
        # as it arrives.
        self.transcript_layouts: dict[str, QVBoxLayout] = {}
        self.transcript_scrolls: dict[str, QScrollArea] = {}
        transcripts_card, transcripts_body = _card("Transcripts")
        transcripts_layout = QHBoxLayout()
        transcripts_layout.setSpacing(16)
        transcripts_body.addLayout(transcripts_layout, stretch=1)
        for label in DIRECTION_TITLES:
            layout = QVBoxLayout()
            layout.addStretch()
            layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            container = QWidget()
            container.setObjectName("transcriptList")
            container.setLayout(layout)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            scroll.setWidget(container)
            self.transcript_layouts[label], self.transcript_scrolls[label] = layout, scroll
            column = QVBoxLayout()
            column_title = QLabel(f"{DIRECTION_NAMES[label]} transcript")
            column_title.setProperty("role", "columnTitle")
            column.addWidget(column_title)
            column.addWidget(scroll, stretch=1)
            transcripts_layout.addLayout(column)

        # Final layout: header, then the cards in order (design.md §6).
        content = QVBoxLayout()
        content.setContentsMargins(16, 16, 16, 16)
        content.setSpacing(12)
        content.addWidget(languages_card)
        content.addWidget(devices_card)
        content.addLayout(actions_layout)
        content.addWidget(self.issue_panel)
        content.addWidget(transcripts_card, stretch=1)

        central = QWidget()
        central.setObjectName("interpreterCentral")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addLayout(content, stretch=1)
        self.setCentralWidget(central)
        # Start with nothing focused: a focus ring at launch looks like a selection.
        central.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        central.setFocus()

        # Fail fast: without a key no session can connect, so say so up front.
        self._check_api_key()

    @staticmethod
    def _preselect(combo: QComboBox, candidates: list[str | int | None]) -> None:
        """Select the first candidate present (name prefix or device index); if
        none is, leave the combo empty rather than guess a device."""
        for candidate in candidates:
            if candidate is None:
                continue
            index = (combo.findText(candidate, Qt.MatchFlag.MatchStartsWith) if isinstance(candidate, str)
                     else combo.findData(candidate))
            if index >= 0:
                combo.setCurrentIndex(index)
                return
        combo.setCurrentIndex(-1)

    def _update_whatsapp_hint(self, key: str, inputs, outputs) -> None:
        self.whatsapp_hints[key].setText(
            whatsapp_hint(key, self.device_combos[key].currentText(), inputs, outputs))
        if self.whatsapp_checklist.text():  # filled once both combos exist
            self._update_whatsapp_checklist(inputs, outputs)

    def _update_whatsapp_checklist(self, inputs, outputs) -> None:
        """WhatsApp doesn't remember its audio devices between calls (decision 7),
        and a Microphone left on its default sends staff's voice untranslated,
        which the app can't detect. So the settings sit next to Start."""
        def setting(key: str) -> str:
            name = whatsapp_device(key, self.device_combos[key].currentText(), inputs, outputs)
            return html.escape(name) if name else "the other end of the selected cable"
        # Kept to two lines: a taller actions row squeezes the devices card's hints.
        self.whatsapp_checklist.setText(
            f"<b>Every WhatsApp call:</b> Speaker → {setting(f'{CANDIDATE_TO_STAFF}.capture')}, "
            f"Microphone → {setting(f'{STAFF_TO_CANDIDATE}.playback')}")
        self.whatsapp_checklist.setToolTip(
            "WhatsApp resets its audio devices for each call: set both in the call's audio "
            "settings every time. A Microphone left on its default sends your voice untranslated.")

    def test_audio_devices(self):
        """Test Audio Devices button: run the loop test for the selected devices
        without starting a session."""
        self._run_loop_test(start_after=False)

    def start_live_translation(self):
        """Check the routing, make sure this device combination has passed the
        loop test (running it first if not), then hand both directions to the
        SessionManager, which runs them on its own background asyncio thread
        (Qt's event loop is not asyncio-based)."""
        if self._manager.running or self._loop_test_running:
            return  # a session or a loop test is already running

        self.issue_panel.clear()
        self._refresh_indicator()
        if not self._check_api_key():
            self._set_status("Can't start: the translation service key is missing.")
            return
        directions = self._directions()
        issues = self._routing_issues(directions)
        if issues:
            self._show_pre_start_issues(issues, "Can't start: fix the audio setup below.")
            return  # hard gate: never start with a known loop
        fingerprint = routing_fingerprint(self._expected_names())
        if fingerprint in self._partial_fingerprints and fingerprint not in self._passed_fingerprints:
            # Started without playing into the call: say what wasn't tested.
            self.issue_panel.add_issue(Issue(STAFF_TO_CANDIDATE, IssueCode.LOOP_TEST_PARTIAL, fields={
                "playback": self._expected_names()[f"{STAFF_TO_CANDIDATE}.playback"]}))
            self._refresh_indicator()
        elif fingerprint not in self._passed_fingerprints:
            self._run_loop_test(start_after=True)  # starts the session only on a pass
            return

        self._session_error = ""
        self._session_translating = False
        self._direction_states.clear()
        self._direction_messages.clear()
        self._set_controls_locked(True)
        self.stop_button.setEnabled(True)
        self._manager.start(directions)

    def stop_live_translation(self):
        """Ask the running session to wind down. Thread-safe; the manager stops the
        routers on its own thread, and the UI unlocks when it reports idle."""
        self.stop_button.setEnabled(False)
        self._set_status("Stopping…")
        self._manager.stop()

    def closeEvent(self, event) -> None:
        """Never orphan the audio threads: with a session running, ask, then stop it
        and close once it reports idle. A loop test can't be cancelled, so closing
        waits for it to finish."""
        if not (self._manager.running or self._loop_test_running):
            super().closeEvent(event)
            return
        event.ignore()
        if self.close_pending:
            return  # already waiting
        if self._manager.running:
            answer = QMessageBox.question(
                self, "Stop live translation?",
                "Live translation is running. Stop it and close this window?")
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.close_pending = True
            self.stop_live_translation()
        else:
            self.close_pending = True
            self._set_status("Closing after the audio test finishes…")

    def _run_loop_test(self, start_after: bool) -> None:
        """Run audio.routing_check.run_loop_test on a worker thread (it blocks for
        several seconds) and report back via loop_test_finished."""
        if self._manager.running or self._loop_test_running:
            return
        directions = self._directions()  # read widgets on the GUI thread
        issues = self._routing_issues(directions)
        if issues:
            self._show_pre_start_issues(issues, "Fix the audio setup below, then test again.")
            return
        fingerprint = routing_fingerprint(self._expected_names())
        # Start may run while the WhatsApp call is already going: never play the
        # test sound into the outgoing route then. Only the Test Audio Devices
        # button (meant for before the call) tests that route.
        skip = frozenset({STAFF_TO_CANDIDATE}) if start_after else frozenset()
        self._loop_test_running = True
        self._set_controls_locked(True)  # the test owns the devices until it finishes
        self._set_status("Checking audio… you should hear a short test sound in the headset." if skip else
                         "Checking audio… you should hear a short test sound on each output.")
        threading.Thread(
            target=lambda: self.loop_test_finished.emit(
                (fingerprint, start_after, bool(skip), run_loop_test(directions, skip_playbacks=skip))),
            daemon=True,
        ).start()

    def _on_loop_test_finished(self, result) -> None:
        fingerprint, start_after, partial, issues = result
        self._loop_test_running = False
        if self.close_pending:
            self.close()  # closing was waiting for the test; don't start a session
            return
        self._set_controls_locked(False)
        if issues:
            self._show_pre_start_issues(issues, "Audio test failed: fix the audio setup below.")
            return
        (self._partial_fingerprints if partial else self._passed_fingerprints).add(fingerprint)
        # A pass supersedes any earlier failed check: drop those rows so the dot
        # goes back to green (it shows the worst issue left in the panel).
        self.issue_panel.clear(PRE_START_CODES)
        self._refresh_indicator()
        self._set_status("Audio routing OK. No loop detected.")
        if start_after:
            self.start_live_translation()

    def _on_session_state_changed(self, state: str, message: str) -> None:
        """Slot for session_state_changed (GUI thread). Per-direction failures
        are shown in the issue panel (SESSION_FAILED); this is the status line."""
        match State(state):
            case State.CONNECTING:
                self._set_status("Connecting…")
            case State.TRANSLATING:
                self._session_translating = True
                self._set_status("Translating.")
                for label in DIRECTION_TITLES:
                    self._show_direction_status(label)
            case State.RECONNECTING | State.DEGRADED:
                self._session_translating = True
                self._set_status(message)
                for label in DIRECTION_TITLES:
                    self._show_direction_status(label)
            case State.STOPPING:
                self._session_translating = False
                self._set_status("Stopping…")
                for label in DIRECTION_TITLES:
                    self._show_direction_status(label)  # hides Retry
            case State.ERROR:
                self._session_error = message
            case State.IDLE:
                self._session_translating = False
                self._set_controls_locked(False)
                self.stop_button.setEnabled(False)
                for label in DIRECTION_TITLES:
                    if self._direction_states.get(label) is not DirectionState.FAILED:
                        self._direction_states[label] = DirectionState.STOPPED
                    self._show_direction_status(label)
                self._set_status(f"Stopped with an error: {self._session_error}"
                                 if self._session_error else "Stopped.")
                logger.info("Live translation session ended.")
                if self.close_pending:
                    self.close()

    def _on_direction_state_changed(self, label: str, state: str, message: str) -> None:
        """Slot for direction_state_changed (GUI thread): the direction's status text."""
        self._direction_states[label] = DirectionState(state)
        self._direction_messages[label] = message
        self._show_direction_status(label)

    def _show_direction_status(self, label: str) -> None:
        state = self._direction_states.get(label)
        if state is None:
            text = "Idle."
        elif state is DirectionState.CONNECTED and self._session_translating:
            text = "Translating."
        elif state is DirectionState.RECONNECTING:
            text = self._direction_messages.get(label) or _DIRECTION_STATUS[state]
        else:
            text = _DIRECTION_STATUS[state]
        self.direction_status[label].setText(text)
        # Retry only helps while the other direction is still running.
        self.retry_buttons[label].setVisible(
            state is DirectionState.FAILED and self._session_translating and self._manager.running)

    def _retry_direction(self, label: str) -> None:
        """Retry button (GUI thread). SessionManager.retry is thread-safe."""
        self.retry_buttons[label].setVisible(False)
        self._manager.retry(label)

    def _on_health_changed(self, issue: Issue) -> None:
        """Slot for health_changed (GUI thread): update the issue panel and dots.
        Red issues also flash the taskbar, since the user is usually looking at
        WhatsApp rather than this window."""
        self.issue_panel.show_issue(issue)
        self._refresh_indicator()
        if issue.active and issue.level is Severity.PROBLEM:
            QApplication.alert(self)

    def _check_api_key(self) -> bool:
        """Show API_KEY_MISSING in the issue panel if GEMINI_API_KEY isn't set."""
        if api_key_present():
            return True
        self.issue_panel.show_issue(Issue("", IssueCode.API_KEY_MISSING))
        self._refresh_indicator()
        return False

    def _on_mute_toggled(self, label: str, muted: bool) -> None:
        """Mute button (GUI thread). SessionManager.set_muted is thread-safe."""
        self._manager.set_muted(label, muted)
        self.mute_buttons[label].setText(
            MUTE_BUTTON_TEXT[label].replace("Mute", "Unmute") if muted else MUTE_BUTTON_TEXT[label])

    def _resume_audio(self, label: str) -> None:
        """Resume audio button on an echo row (only shown when HealthConfig.auto_mute_on_echo is on)."""
        self._manager.resume(label)

    def _routing_issues(self, directions: list[DirectionConfig]) -> list[Issue]:
        """validate_routing, except that an empty locked WhatsApp combo means the
        driver isn't installed: there's nothing to choose, so say what to install."""
        issues = []
        for issue in validate_routing(directions, self._expected_names()):
            if issue.code is IssueCode.DEVICE_NOT_SELECTED:
                role = "capture" if issue.fields.get("role") == "input" else "playback"
                key = f"{issue.label}.{role}"
                if key in self._locked_device_keys:
                    default = (WHATSAPP_INCOMING_DEFAULT if key == f"{CANDIDATE_TO_STAFF}.capture"
                               else WHATSAPP_OUTGOING_DEFAULT)
                    issue = Issue(issue.label, IssueCode.DRIVER_MISSING,
                                  fields={"device": default, "product": WHATSAPP_PRODUCTS[key]})
            issues.append(issue)
        return issues

    def _show_pre_start_issues(self, issues: list[Issue], status: str) -> None:
        self.issue_panel.clear(PRE_START_CODES)
        for issue in issues:
            log_issue(issue)
            self.issue_panel.add_issue(issue)
        self._refresh_indicator()
        self._set_status(status)
        QApplication.alert(self)

    def _refresh_indicator(self) -> None:
        self.status_dot.set_severity(self.issue_panel.worst())
        for label, dot in self.direction_dots.items():
            dot.set_severity(self.issue_panel.worst(label))

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _set_controls_locked(self, locked: bool) -> None:
        """Model, language and devices are fixed while a session or loop test runs.
        Locked WhatsApp combos stay disabled either way."""
        combos = [combo for key, combo in self.device_combos.items() if key not in self._locked_device_keys]
        for widget in (self.model_combo, self.language_combo, *combos, self.audio_test_button, self.start_button):
            widget.setEnabled(not locked)

    def _two_way_config(self) -> TwoWayConfig:
        """Both directions from the combos. A translates into STAFF_OUTPUT_LANGUAGE and
        keeps echo_target_language=True (the candidate may mix English in, and staff
        must hear all of it). B translates into the candidate's language with
        echo_target_language=False (staff always speak English, so a re-captured
        translation on B dies after one pass). guide/two_way_plan.md §4.6."""
        return TwoWayConfig(
            candidate_to_staff=DirectionConfig(
                label=CANDIDATE_TO_STAFF,
                capture=self._device_config(f"{CANDIDATE_TO_STAFF}.capture"),
                playback=self._device_config(f"{CANDIDATE_TO_STAFF}.playback"),
                target_language_code=self.staff_language_combo.currentData(),
                echo_target_language=True,
                model=self.selected_model(),
            ),
            staff_to_candidate=DirectionConfig(
                label=STAFF_TO_CANDIDATE,
                capture=self._device_config(f"{STAFF_TO_CANDIDATE}.capture"),
                playback=self._device_config(f"{STAFF_TO_CANDIDATE}.playback"),
                target_language_code=self.selected_language_code(),
                echo_target_language=False,
                model=self.selected_model(),
            ),
        )

    def selected_model(self) -> str:
        """Model id from the header's model combo (config/model_config.py LIVE_MODELS)."""
        return self.model_combo.currentData() or DEFAULT_MODEL

    def _directions(self) -> list[DirectionConfig]:
        return self._two_way_config().directions()

    def _expected_names(self) -> dict[str, str]:
        """Device names the combos show, for validate_routing's stale-index guard
        and the loop-test fingerprint."""
        return {key: combo.currentText() for key, combo in self.device_combos.items()}

    def report_context(self) -> dict[str, str]:
        """The "Devices and settings" section of a bug report (ui/bug_report.py): device
        names, languages, model, session state and the active issue codes. Issue
        codes only, not their fields or text: a report must stay content-free."""
        context = {DEVICE_ROLES[key][0].rstrip(":"): combo.currentText() or "(none)"
                   for key, combo in self.device_combos.items()}
        context["Candidate hears"] = self.selected_language_code()
        context["Staff hears"] = self.staff_language_combo.currentData()
        context["Model"] = self.selected_model()
        context["Session"] = "running" if self._manager.running else "idle"
        context["Problems shown"] = ", ".join(
            f"{issue.label or '-'}:{issue.code.name} ({issue.level.name.lower()})"
            for issue in self.issue_panel.active_issues()) or "none"
        return context

    def selected_language_code(self) -> str:
        """BCP-47 code for the "Candidate hears" combo: direction B's target language."""
        return self.language_combo.currentData()

    def _device_config(self, key: str) -> AudioDeviceConfig:
        """AudioDeviceConfig for the device chosen in one role's combo; AudioRouter
        and AudioPlayer resolve it via device_index. An empty combo gives
        device_index=None, with no fallback to the "CABLE" substring default (it's
        ambiguous: it matches CABLE Output, CABLE Input and CABLE In 16ch alike), so
        validate_routing reports it instead."""
        return AudioDeviceConfig(device_index=self.device_combos[key].currentData())

    def _on_transcript_received(self, direction_label: str, pair: dict) -> None:
        """Slot for transcript_received: appends one label per finished
        {"input": ..., "output": ...} pair to that direction's panel and scrolls
        it to show the new pair."""
        layout = self.transcript_layouts.get(direction_label)
        if layout is None:
            return
        label = QLabel(f"{pair.get('input', '')}\n→ {pair.get('output', '')}")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        label.setProperty("role", "transcriptPair")
        # Insert before the trailing stretch so new labels stack top-to-bottom.
        layout.insertWidget(layout.count() - 1, label)

        scrollbar = self.transcript_scrolls[direction_label].verticalScrollBar()
        QTimer.singleShot(0, lambda: scrollbar.setValue(scrollbar.maximum()))


# Standalone run, for testing this window on its own: from the repo root,
# `python -m ui.live_interpreter` (as a module, so the root-level packages import).
# main.py runs the full app, which opens this window from ui/main_window.py
# (importing this module must not launch it).
if __name__ == "__main__":
    # Log file (and console in dev), plus uncaught exceptions and Qt warnings.
    # Same setup as main.py.
    configure_logging(release=RELEASE)

    # You need one (and only one) QApplication instance per application.
    app = QApplication(sys.argv)
    apply_stylesheet(app, watch=not RELEASE)  # watch: live-reload ui/style.qss on save (dev only)

    window = LiveInterpreterWindow()
    window.show()  # IMPORTANT!!!!! Windows are hidden by default.
    app.exec()
