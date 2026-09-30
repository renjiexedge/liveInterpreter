
import asyncio
import logging
import sys
import threading

from PySide6.QtWidgets import (QApplication, QHBoxLayout, QPushButton, QWidget, QMainWindow, QLabel, QVBoxLayout, QComboBox, QScrollArea)
from PySide6.QtCore import Qt, QSize, Signal, QTimer

import sounddevice as sd

from audio.devices import DeviceNotFoundError, find_input_device, list_input_devices, list_output_devices
from audio.health import PRE_START_CODES, HealthMonitor, Issue, IssueCode, Severity
from audio.pipeline import build_audio_pipeline
from audio.routing_check import routing_fingerprint, run_loop_test, validate_routing
from config.audio_config import AudioDeviceConfig
from config.session_config import CANDIDATE_TO_STAFF, DirectionConfig
from engines.gemini_live import run_session
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

# Subclass QMainWindow to customize your application's main window
class MainWindow(QMainWindow):
    # Emitted with {"input": ..., "output": ...} whenever a transcript pair
    # finishes. Qt signals are safe to emit from a non-GUI thread (e.g. the
    # asyncio thread running engines.gemini_live.run_session) — the slot
    # below always runs on the GUI thread regardless of the emitting thread.
    transcript_received = Signal(dict)
    # Emitted from the background session thread when run_session finishes,
    # whether cleanly (empty string) or with an error (str(exception)).
    session_ended = Signal(str)
    # audio.health.Issue raised/updated/cleared by the session's HealthMonitor.
    health_changed = Signal(object)
    # (fingerprint, start_after, list[Issue]) from the loop-test worker thread.
    loop_test_finished = Signal(object)

    def __init__(self):
        super().__init__()
        self.transcript_received.connect(self._on_transcript_received)
        self.session_ended.connect(self._on_session_ended)
        self.health_changed.connect(self._on_health_changed)
        self.loop_test_finished.connect(self._on_loop_test_finished)

        # Set by _translation_main once the pipeline is built, so
        # stop_live_translation (GUI thread) can signal the background
        # session thread to wind down. None while no session is running.
        self._router = None
        self._health = None
        self._session_thread = None
        self._loop_test_running = False
        # Device combinations that passed the loop test; Start requires one.
        self._passed_fingerprints: set[frozenset] = set()

        # Set the window title
        self.setWindowTitle("Xedge Live Interpreter")
        # Set the window dimensions (16:10) for a good default size
        self.setMinimumSize(QSize(1120, 700))

        # self.label = QLabel()
        # self.input = QLineEdit()
        # self.input.textChanged.connect(self.label.setText)

        #App header
        header_label_1 = QLabel("Xedge Live Interpreter")
        header_label_1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_label_2 = QLabel("Translate Language To:")
        header_combo = QComboBox()
        # Restrict selection to the dropdown only; user cannot type/edit the contents.
        header_combo.setEditable(False)
        for language_name, language_code in SOUTHEAST_ASIA_LANGUAGES.items():
            header_combo.addItem(language_name, language_code)
        header_combo.setCurrentText("English (Singapore)")
        # Item data holds the BCP-47 code passed to run_session as the target language.
        self.language_combo = header_combo
        header_label_2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout = QHBoxLayout();
        header_layout.addWidget(header_label_1)
        header_layout.addWidget(header_label_2)
        header_layout.addWidget(header_combo)

        #2nd row of app header: Selecting Input & Output devices.
        input_label = QLabel("Input Device:")
        output_label = QLabel("Output Device:")
        input_combo = QComboBox()
        output_combo = QComboBox()
        input_combo.setEditable(False)
        output_combo.setEditable(False)
        # Populate with real devices; item data holds the sounddevice index.
        for device in list_input_devices():
            input_combo.addItem(device.name, device.index)
        for device in list_output_devices():
            output_combo.addItem(device.name, device.index)
        # Preselect the default (CABLE) device if present; otherwise keep the first entry.
        # try:
        #     input_combo.setCurrentIndex(input_combo.findData(find_input_device(AudioDeviceConfig()).index))
        # except DeviceNotFoundError:
        #     pass

        # TEMP (testing): preselect Voicemeeter devices instead of CABLE.
        input_combo.setCurrentIndex(max(input_combo.findText("Voicemeeter Out B1", Qt.MatchFlag.MatchStartsWith), 0))
        output_combo.setCurrentIndex(max(output_combo.findText("Voicemeeter Input", Qt.MatchFlag.MatchStartsWith), 0))
        self.input_combo = input_combo
        self.output_combo = output_combo
        self.audio_test_button = QPushButton("Test Audio Devices")
        self.audio_test_button.clicked.connect(self.test_audio_devices)

        # Create a horizontal layout for the input/output device selection
        device_layout = QHBoxLayout()
        device_layout.addWidget(input_label)
        device_layout.addWidget(input_combo)
        device_layout.addWidget(output_label)
        device_layout.addWidget(output_combo)

        # Audio health indicator: a dot for the worst active issue, a status line,
        # and the issue panel listing each problem with what to do about it.
        self.status_dot = StatusDot()
        self.status_label = QLabel("Ready. Press Test Audio Devices or Start.")
        status_layout = QHBoxLayout()
        status_layout.addWidget(self.status_dot)
        status_layout.addWidget(self.status_label, stretch=1)
        status_layout.addWidget(self.audio_test_button)
        self.issue_panel = IssuePanel()
        self.issue_panel.resume_clicked.connect(self._resume_audio)

        # Scroll area of transcript pairs, centered in the window. Each
        # completed {"input": ..., "output": ...} pair from the translation
        # session becomes one label, appended as it arrives.
        self.transcript_layout = QVBoxLayout()
        self.transcript_layout.addStretch()
        self.transcript_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        transcript_container = QWidget()
        transcript_container.setLayout(self.transcript_layout)

        

        self.transcript_scroll = QScrollArea()
        self.transcript_scroll.setWidgetResizable(True)
        self.transcript_scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.transcript_scroll.setWidget(transcript_container)

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
        layout.addLayout(device_layout)
        layout.addLayout(status_layout)
        layout.addWidget(self.issue_panel)
        layout.addWidget(self.transcript_scroll, stretch=1)
        layout.addLayout(footer_layout)

        Container = QWidget()
        Container.setLayout(layout)

        # Set the central widget of the Window.
        self.setCentralWidget(Container)

    def test_audio_devices(self):
        """Test Audio Devices button: run the loop test for the selected devices
        without starting a session."""
        self._run_loop_test(start_after=False)

    def start_live_translation(self):
        """Check the routing, make sure this device combination has passed the
        loop test (running it first if not), then run the translation session
        on a background thread with its own asyncio event loop (Qt's event loop
        is not asyncio-based)."""
        if self._session_thread is not None or self._loop_test_running:
            return  # a session or a loop test is already running

        self.issue_panel.clear()
        self._refresh_indicator()
        directions = self._directions()
        issues = validate_routing(directions, self._expected_names())
        if issues:
            self._show_pre_start_issues(issues, "Can't start: fix the audio setup below.")
            return  # hard gate: never start with a known loop
        if routing_fingerprint(self._expected_names()) not in self._passed_fingerprints:
            self._run_loop_test(start_after=True)  # starts the session only on a pass
            return

        self._set_controls_locked(True)
        self.stop_button.setEnabled(True)
        self._set_status("Translating.")
        self._session_thread = threading.Thread(
            target=self._run_session_thread,
            args=(directions[0],),
            daemon=True,
        )
        self._session_thread.start()

    def stop_live_translation(self):
        """Signal the running session to wind down. Stopping the router feeds
        the end-of-stream sentinel through the jitter buffer, which makes
        run_session's send side finish, which cancels the receive side and
        returns — _translation_main's finally then tears the router down."""
        if self._health is not None:
            self._health.stop_watching()  # a deliberate Stop isn't a disconnect
        if self._router is not None:
            self._router.stop()

    def _run_loop_test(self, start_after: bool) -> None:
        """Run audio.routing_check.run_loop_test on a worker thread (it blocks for
        several seconds) and report back via loop_test_finished."""
        if self._session_thread is not None or self._loop_test_running:
            return
        directions = self._directions()  # read widgets on the GUI thread
        issues = validate_routing(directions, self._expected_names())
        if issues:
            self._show_pre_start_issues(issues, "Fix the audio setup below, then test again.")
            return
        fingerprint = routing_fingerprint(self._expected_names())
        self._loop_test_running = True
        self._set_controls_locked(True)  # the test owns the devices until it finishes
        self._set_status("Checking audio… you should hear a short test sound.")
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

    def _run_session_thread(self, direction: DirectionConfig) -> None:
        """Entry point for the background session thread."""
        error_message = ""
        try:
            asyncio.run(self._translation_main(direction))
        except (DeviceNotFoundError, sd.PortAudioError) as exc:
            print(f"Live translation session couldn't open a device: {exc}")
            error_message = "an audio device couldn't be opened. It may be unplugged or in use."
        except Exception as exc:
            error_message = str(exc)
        finally:
            if error_message:
                self.health_changed.emit(Issue(direction.label, IssueCode.SESSION_FAILED,
                                               fields={"detail": error_message}))
            self.session_ended.emit(error_message)

    async def _translation_main(self, direction: DirectionConfig) -> None:
        """Runs on the background thread's event loop: wires the input
        AudioRouter to run_session's audio_queue contract, plays translated
        audio out via run_session's own AudioPlayer, reports transcript pairs
        back to the GUI thread, and runs the HealthMonitor alongside."""
        monitor = HealthMonitor(on_issue=self.health_changed.emit)
        self._health = monitor
        router, jitter_buffer = build_audio_pipeline(direction.capture, direction.pipeline)
        self._router = router
        monitor_task = None
        try:
            router.start()
            monitor.attach(direction, router, jitter_buffer)
            monitor_task = asyncio.create_task(monitor.run())
            await run_session(
                jitter_buffer,
                output_device_config=direction.playback,
                on_transcript=self.transcript_received.emit,
                target_language_code=direction.target_language_code,
                echo_target_language=direction.echo_target_language,
                on_transcript_delta=lambda kind, text: monitor.transcript_delta(direction.label, kind, text),
                on_player_ready=lambda player: monitor.player_ready(direction.label, player),
            )
        finally:
            if monitor_task is not None:
                monitor_task.cancel()
                await asyncio.gather(monitor_task, return_exceptions=True)
            monitor.finish()
            router.stop()
            self._router = None
            self._health = None

    def _on_session_ended(self, error_message: str) -> None:
        """Slot for session_ended: always runs on the GUI thread, so it's
        safe to touch widgets here regardless of how the session ended."""
        self._set_controls_locked(False)
        self.stop_button.setEnabled(False)
        self._session_thread = None
        # The error itself is shown in the issue panel (SESSION_FAILED).
        self._set_status("Stopped with an error." if error_message else "Stopped.")
        print("Live translation session ended.")

    def _on_health_changed(self, issue: Issue) -> None:
        """Slot for health_changed (GUI thread): update the issue panel and dot.
        Red issues also flash the taskbar, since the user is usually looking at
        WhatsApp rather than this window."""
        self.issue_panel.show_issue(issue)
        self._refresh_indicator()
        if issue.active and issue.level is Severity.PROBLEM:
            QApplication.alert(self)

    def _resume_audio(self, label: str) -> None:
        """Resume audio button on an echo row (only shown when HealthConfig.auto_mute_on_echo is on)."""
        if self._health is not None:
            self._health.resume(label)

    def _show_pre_start_issues(self, issues: list[Issue], status: str) -> None:
        self.issue_panel.clear(PRE_START_CODES)
        for issue in issues:
            self.issue_panel.add_issue(issue)
        self._refresh_indicator()
        self._set_status(status)
        QApplication.alert(self)

    def _refresh_indicator(self) -> None:
        self.status_dot.set_severity(self.issue_panel.worst())

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _set_controls_locked(self, locked: bool) -> None:
        """Language and devices are fixed while a session or loop test runs."""
        for widget in (self.language_combo, self.input_combo, self.output_combo,
                       self.audio_test_button, self.start_button):
            widget.setEnabled(not locked)

    def _directions(self) -> list[DirectionConfig]:
        """Today's one-way app is a single direction built from the combos.
        echo_target_language stays True until it's decided which spec direction
        this path is (guide/two_way_plan.md §1.3, §4.6)."""
        return [DirectionConfig(
            label=CANDIDATE_TO_STAFF,
            capture=self.selected_input_config(),
            playback=self.selected_output_config(),
            target_language_code=self.selected_language_code(),
            echo_target_language=True,
        )]

    def _expected_names(self) -> dict[str, str]:
        """Device names the combos show, for validate_routing's stale-index guard
        and the loop-test fingerprint."""
        return {f"{CANDIDATE_TO_STAFF}.capture": self.input_combo.currentText(),
                f"{CANDIDATE_TO_STAFF}.playback": self.output_combo.currentText()}

    def selected_language_code(self) -> str:
        """BCP-47 code for the language chosen in the header combo, passed to
        run_session as target_language_code."""
        return self.language_combo.currentData()

    def selected_input_config(self) -> AudioDeviceConfig:
        """AudioDeviceConfig for the device chosen in the input combo, ready to
        pass to build_audio_pipeline(); AudioRouter resolves it via device_index."""
        return AudioDeviceConfig(device_index=self.input_combo.currentData())

    def selected_output_config(self) -> AudioDeviceConfig:
        """AudioDeviceConfig for the device chosen in the output combo, ready to
        pass to engines.gemini_live.run_session(); AudioPlayer resolves it via
        device_index. No fallback to the "CABLE" substring default when nothing is
        selected: it's ambiguous with CABLE-A and CABLE-B, so validate_routing
        reports it instead."""
        return AudioDeviceConfig(device_index=self.output_combo.currentData())

    def _on_transcript_received(self, pair: dict) -> None:
        """Slot for transcript_received: appends one label per finished
        {"input": ..., "output": ...} pair and scrolls the area to show it."""
        label = QLabel(f"{pair.get('input', '')}\n→ {pair.get('output', '')}")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        # Insert before the trailing stretch so new labels stack top-to-bottom.
        self.transcript_layout.insertWidget(self.transcript_layout.count() - 1, label)

        scrollbar = self.transcript_scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: scrollbar.setValue(scrollbar.maximum()))

    # def the_button_was_clicked(self):
    #     self.button.setText("You already clicked me.")
    #     self.button.setEnabled(False)



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