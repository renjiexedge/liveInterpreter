
import logging
import sys
import threading

from PySide6.QtWidgets import (QApplication, QGroupBox, QGridLayout, QHBoxLayout, QPushButton, QWidget,
                               QMainWindow, QLabel, QVBoxLayout, QComboBox, QScrollArea)
from PySide6.QtCore import Qt, QSize, Signal, QTimer

from audio.devices import default_device_indices, list_input_devices, list_output_devices
from audio.health import PRE_START_CODES, Issue, IssueCode, Severity
from audio.routing_check import cable_key, routing_fingerprint, run_loop_test, validate_routing
from config.audio_config import AudioDeviceConfig
from config.session_config import (CANDIDATE_TO_STAFF, DIRECTION_NAMES, STAFF_OUTPUT_LANGUAGE,
                                   STAFF_TO_CANDIDATE, DirectionConfig, TwoWayConfig)
from engines.gemini_live import api_key_present
from sessions.manager import DirectionState, SessionManager, State
from ui.issue_panel import IssuePanel, StatusDot
# Only needed for access to command line arguments

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


def whatsapp_hint(key: str, device_name: str, inputs, outputs) -> str:
    """What to choose in WhatsApp's audio settings for the device picked in one of
    the WhatsApp combos. WhatsApp needs the OTHER end of the wire: its Speaker
    list only has playback devices and its Microphone list only recording ones."""
    incoming = key == f"{CANDIDATE_TO_STAFF}.capture"
    setting = "Speaker" if incoming else "Microphone"
    if not device_name:
        return f"Choose a device. In WhatsApp, set {setting} to its other end."
    # A plain cable: the other end has the same name apart from Input/Output.
    other_side = outputs if incoming else inputs
    matches = [d.name for d in other_side if cable_key(d.name) == cable_key(device_name)]
    if matches:
        # VB-Cable also has a 16-channel "CABLE In 16ch" end; prefer plain "Input".
        matches.sort(key=lambda name: "input" not in name.casefold())
        return f"In WhatsApp, set {setting} to: {matches[0]}"
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


# Subclass QMainWindow to customize your application's main window
class MainWindow(QMainWindow):
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
    # (fingerprint, start_after, list[Issue]) from the loop-test worker thread.
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
        self._loop_test_running = False
        # Device combinations that passed the loop test; Start requires one.
        self._passed_fingerprints: set[frozenset] = set()

        # Set the window title
        self.setWindowTitle("Xedge Live Interpreter")
        # Set the window dimensions (16:10) for a good default size
        self.setMinimumSize(QSize(1120, 700))

        #App header: title, then the language each side hears.
        header_label_1 = QLabel("Xedge Live Interpreter")
        header_label_1.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        header_layout = QHBoxLayout()
        header_layout.addWidget(header_label_1)
        header_layout.addStretch()
        header_layout.addWidget(candidate_label)
        header_layout.addWidget(candidate_combo)
        header_layout.addWidget(staff_label)
        header_layout.addWidget(self.staff_language_combo)

        # One group per direction: status dot + text, and its two devices.
        # Item data in each device combo holds the sounddevice index.
        inputs, outputs = list_input_devices(), list_output_devices()
        preselect = _preselect_candidates(inputs, outputs)
        self.device_combos: dict[str, QComboBox] = {}
        # Under each WhatsApp combo: what to pick in WhatsApp's own audio settings.
        self.whatsapp_hints: dict[str, QLabel] = {}
        self.direction_dots: dict[str, StatusDot] = {}
        self.direction_status: dict[str, QLabel] = {}
        self.mute_buttons: dict[str, QPushButton] = {}
        directions_layout = QHBoxLayout()
        for label, title in DIRECTION_TITLES.items():
            group = QGroupBox(title)
            grid = QGridLayout(group)
            dot, status = StatusDot(), QLabel("Idle.")
            self.direction_dots[label], self.direction_status[label] = dot, status
            # Mute stays usable at all times: before Start it applies once the session runs.
            mute = QPushButton(MUTE_BUTTON_TEXT[label])
            mute.setCheckable(True)
            mute.toggled.connect(lambda checked, label=label: self._on_mute_toggled(label, checked))
            self.mute_buttons[label] = mute
            status_row = QHBoxLayout()
            status_row.addWidget(dot)
            status_row.addWidget(status, stretch=1)
            status_row.addWidget(mute)
            grid.addLayout(status_row, 0, 0, 1, 2)
            row = 1
            for role in ("capture", "playback"):
                key = f"{label}.{role}"
                role_label, is_input = DEVICE_ROLES[key]
                combo = QComboBox()
                combo.setEditable(False)
                for device in inputs if is_input else outputs:
                    combo.addItem(device.name, device.index)
                self._preselect(combo, preselect[key])
                self.device_combos[key] = combo
                grid.addWidget(QLabel(role_label), row, 0)
                grid.addWidget(combo, row, 1)
                row += 1
                if key in (f"{CANDIDATE_TO_STAFF}.capture", f"{STAFF_TO_CANDIDATE}.playback"):
                    hint = QLabel()
                    hint.setWordWrap(True)
                    hint.setStyleSheet("font-style: italic;")
                    self.whatsapp_hints[key] = hint
                    grid.addWidget(hint, row, 1)
                    row += 1
                    combo.currentIndexChanged.connect(
                        lambda _=None, key=key: self._update_whatsapp_hint(key, inputs, outputs))
                    self._update_whatsapp_hint(key, inputs, outputs)
            grid.setColumnStretch(1, 1)
            directions_layout.addWidget(group)

        # Audio health indicator: a dot for the worst active issue, a status line,
        # and the issue panel listing each problem with what to do about it.
        self.audio_test_button = QPushButton("Test Audio Devices")
        self.audio_test_button.clicked.connect(self.test_audio_devices)
        self.status_dot = StatusDot()
        self.status_label = QLabel("Ready. Press Test Audio Devices or Start.")
        status_layout = QHBoxLayout()
        status_layout.addWidget(self.status_dot)
        status_layout.addWidget(self.status_label, stretch=1)
        status_layout.addWidget(self.audio_test_button)
        self.issue_panel = IssuePanel(direction_names=DIRECTION_NAMES)
        self.issue_panel.resume_clicked.connect(self._resume_audio)

        # One scroll area of transcript pairs per direction, side by side. Each
        # completed {"input": ..., "output": ...} pair becomes one label, appended
        # as it arrives.
        self.transcript_layouts: dict[str, QVBoxLayout] = {}
        self.transcript_scrolls: dict[str, QScrollArea] = {}
        transcripts_layout = QHBoxLayout()
        for label in DIRECTION_TITLES:
            layout = QVBoxLayout()
            layout.addStretch()
            layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            container = QWidget()
            container.setLayout(layout)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            scroll.setWidget(container)
            self.transcript_layouts[label], self.transcript_scrolls[label] = layout, scroll
            column = QVBoxLayout()
            column.addWidget(QLabel(f"{DIRECTION_NAMES[label]} transcript"))
            column.addWidget(scroll, stretch=1)
            transcripts_layout.addLayout(column)

        #Bottom row of App for start/stop buttons for the live translation session.
        footer_layout = QHBoxLayout()
        self.start_button = QPushButton("Start Live Translation")
        self.stop_button = QPushButton("Stop Live Translation")
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_live_translation)
        self.stop_button.clicked.connect(self.stop_live_translation)
        footer_layout.addWidget(self.start_button)
        footer_layout.addWidget(self.stop_button)

        #Final layout for the app window.
        layout = QVBoxLayout()
        layout.addLayout(header_layout)
        layout.addLayout(directions_layout)
        layout.addLayout(status_layout)
        layout.addWidget(self.issue_panel)
        layout.addLayout(transcripts_layout, stretch=1)
        layout.addLayout(footer_layout)

        Container = QWidget()
        Container.setLayout(layout)

        # Set the central widget of the Window.
        self.setCentralWidget(Container)

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
        issues = validate_routing(directions, self._expected_names())
        if issues:
            self._show_pre_start_issues(issues, "Can't start: fix the audio setup below.")
            return  # hard gate: never start with a known loop
        if routing_fingerprint(self._expected_names()) not in self._passed_fingerprints:
            self._run_loop_test(start_after=True)  # starts the session only on a pass
            return

        self._session_error = ""
        self._session_translating = False
        self._direction_states.clear()
        self._set_controls_locked(True)
        self.stop_button.setEnabled(True)
        self._manager.start(directions)

    def stop_live_translation(self):
        """Ask the running session to wind down. Thread-safe; the manager stops the
        routers on its own thread, and the UI unlocks when it reports idle."""
        self.stop_button.setEnabled(False)
        self._set_status("Stopping…")
        self._manager.stop()

    def _run_loop_test(self, start_after: bool) -> None:
        """Run audio.routing_check.run_loop_test on a worker thread (it blocks for
        several seconds) and report back via loop_test_finished."""
        if self._manager.running or self._loop_test_running:
            return
        directions = self._directions()  # read widgets on the GUI thread
        issues = validate_routing(directions, self._expected_names())
        if issues:
            self._show_pre_start_issues(issues, "Fix the audio setup below, then test again.")
            return
        fingerprint = routing_fingerprint(self._expected_names())
        self._loop_test_running = True
        self._set_controls_locked(True)  # the test owns the devices until it finishes
        self._set_status("Checking audio… you should hear a short test sound on each output.")
        threading.Thread(
            target=lambda: self.loop_test_finished.emit((fingerprint, start_after, run_loop_test(directions))),
            daemon=True,
        ).start()

    def _on_loop_test_finished(self, result) -> None:
        fingerprint, start_after, issues = result
        self._loop_test_running = False
        self._set_controls_locked(False)
        if issues:
            self._show_pre_start_issues(issues, "Audio test failed: fix the audio setup below.")
            return
        self._passed_fingerprints.add(fingerprint)
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
            case State.DEGRADED:
                self._set_status(message)
            case State.STOPPING:
                self._set_status("Stopping…")
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
                print("Live translation session ended.")

    def _on_direction_state_changed(self, label: str, state: str, message: str) -> None:
        """Slot for direction_state_changed (GUI thread): the direction's status text."""
        self._direction_states[label] = DirectionState(state)
        self._show_direction_status(label)

    def _show_direction_status(self, label: str) -> None:
        state = self._direction_states.get(label)
        if state is None:
            text = "Idle."
        elif state is DirectionState.CONNECTED and self._session_translating:
            text = "Translating."
        else:
            text = _DIRECTION_STATUS[state]
        self.direction_status[label].setText(text)

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

    def _show_pre_start_issues(self, issues: list[Issue], status: str) -> None:
        self.issue_panel.clear(PRE_START_CODES)
        for issue in issues:
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
        """Language and devices are fixed while a session or loop test runs."""
        for widget in (self.language_combo, *self.device_combos.values(),
                       self.audio_test_button, self.start_button):
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
            ),
            staff_to_candidate=DirectionConfig(
                label=STAFF_TO_CANDIDATE,
                capture=self._device_config(f"{STAFF_TO_CANDIDATE}.capture"),
                playback=self._device_config(f"{STAFF_TO_CANDIDATE}.playback"),
                target_language_code=self.selected_language_code(),
                echo_target_language=False,
            ),
        )

    def _directions(self) -> list[DirectionConfig]:
        return self._two_way_config().directions()

    def _expected_names(self) -> dict[str, str]:
        """Device names the combos show, for validate_routing's stale-index guard
        and the loop-test fingerprint."""
        return {key: combo.currentText() for key, combo in self.device_combos.items()}

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
        # Insert before the trailing stretch so new labels stack top-to-bottom.
        layout.insertWidget(layout.count() - 1, label)

        scrollbar = self.transcript_scrolls[direction_label].verticalScrollBar()
        QTimer.singleShot(0, lambda: scrollbar.setValue(scrollbar.maximum()))


# You need one (and only one) QApplication instance per application.
# Pass in sys.argv to allow command line arguments for your app.
# If you know you won't use command line arguments QApplication([]) works too.
# Without this, INFO logs from the audio/ modules (e.g. the loop test's NCC
# readings) are dropped: only warnings reach the console. Same setup as run_cli.py.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = QApplication(sys.argv)

# Create a Qt widget, which will be our window.
window = MainWindow()
window.show()  # IMPORTANT!!!!! Windows are hidden by default.

# Start the event loop.
app.exec()

# Your application won't reach here until you exit and the event
# loop has stopped.

#todo:
# Need to create a manager to concorently run 2 sessions, one for input and one for output, and connect them to the audio pipeline.
# Figure out bug in audio buffer where when its overloaded, something breaks and gemini keeps sending repeating audio chunks. No distortion just repeating audio and new inputs does not change the output.
# Figure out why gemini returns a different voice whenever there is a big pause when the user is talking.