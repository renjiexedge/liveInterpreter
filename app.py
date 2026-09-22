
import asyncio
import sys
import threading

from PySide6.QtWidgets import (QApplication, QHBoxLayout, QPushButton, QWidget, QMainWindow, QLabel, QLineEdit, QVBoxLayout, QComboBox, QScrollArea)
from PySide6.QtCore import Qt, QSize, Signal, QTimer

from audio.devices import DeviceNotFoundError, find_input_device, list_input_devices, list_output_devices
from audio.pipeline import build_audio_pipeline
from config.audio_config import AudioDeviceConfig, AudioPipelineConfig
from engines.gemini_live import run_session
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

    def __init__(self):
        super().__init__()
        self.transcript_received.connect(self._on_transcript_received)
        self.session_ended.connect(self._on_session_ended)

        # Set by _translation_main once the pipeline is built, so
        # stop_live_translation (GUI thread) can signal the background
        # session thread to wind down. None while no session is running.
        self._router = None
        self._session_thread = None

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
        # self.header_combo = header_combo
        # header_combo.currentIndexChanged.connect(self.on_language_changed)
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

        # Create a horizontal layout for the input/output device selection
        device_layout = QHBoxLayout()
        device_layout.addWidget(input_label)
        device_layout.addWidget(input_combo)
        device_layout.addWidget(output_label)
        device_layout.addWidget(output_combo)

        # Show the initially selected language in the label.
        # self.on_language_changed(header_combo.currentIndex())

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
        layout.addWidget(self.transcript_scroll, stretch=1)
        layout.addLayout(footer_layout)

        Container = QWidget()
        Container.setLayout(layout)

        # Set the central widget of the Window.
        self.setCentralWidget(Container)

    # def on_language_changed(self, index):
    #     language_name = self.header_combo.itemText(index)
    #     language_code = self.header_combo.itemData(index)
    #     self.label.setText(f"{language_name}: {language_code}")

    def start_live_translation(self):
        """Build the audio pipeline for the selected devices and run the
        translation session on a background thread with its own asyncio
        event loop (Qt's event loop is not asyncio-based)."""
        if self._session_thread is not None:
            return  # a session is already running

        input_config = self.selected_input_config()
        output_config = self.selected_output_config()

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        self._session_thread = threading.Thread(
            target=self._run_session_thread,
            args=(input_config, output_config),
            daemon=True,
        )
        self._session_thread.start()

    def stop_live_translation(self):
        """Signal the running session to wind down. Stopping the router feeds
        the end-of-stream sentinel through the jitter buffer, which makes
        run_session's send side finish, which cancels the receive side and
        returns — _translation_main's finally then tears the router down."""
        if self._router is not None:
            self._router.stop()

    def _run_session_thread(self, input_config: AudioDeviceConfig, output_config: AudioDeviceConfig) -> None:
        """Entry point for the background session thread."""
        error_message = ""
        try:
            asyncio.run(self._translation_main(input_config, output_config))
        except Exception as exc:
            error_message = str(exc)
        finally:
            self.session_ended.emit(error_message)

    async def _translation_main(self, input_config: AudioDeviceConfig, output_config: AudioDeviceConfig) -> None:
        """Runs on the background thread's event loop: wires the input
        AudioRouter to run_session's audio_queue contract, plays translated
        audio out via run_session's own AudioPlayer, and reports transcript
        pairs back to the GUI thread."""
        router, jitter_buffer = build_audio_pipeline(input_config, AudioPipelineConfig())
        self._router = router
        router.start()
        try:
            await run_session(
                jitter_buffer,
                output_device_config=output_config,
                on_transcript=self.transcript_received.emit,
            )
        finally:
            router.stop()
            self._router = None

    def _on_session_ended(self, error_message: str) -> None:
        """Slot for session_ended: always runs on the GUI thread, so it's
        safe to touch widgets here regardless of how the session ended."""
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._session_thread = None
        if error_message:
            # TODO: surface this in the UI (status bar/dialog) rather than the console.
            print(f"Live translation session ended with an error: {error_message}")
        print("Live translation session ended.")

    def selected_input_config(self) -> AudioDeviceConfig:
        """AudioDeviceConfig for the device chosen in the input combo, ready to
        pass to build_audio_pipeline(); AudioRouter resolves it via device_index."""
        return AudioDeviceConfig(device_index=self.input_combo.currentData())

    def selected_output_config(self) -> AudioDeviceConfig:
        """AudioDeviceConfig for the device chosen in the output combo, ready to
        pass to engines.gemini_live.run_session(); AudioPlayer resolves it via
        device_index. Falls back to the CABLE default when nothing is selected."""
        device_index = self.output_combo.currentData()
        if device_index is None:
            return AudioDeviceConfig()
        return AudioDeviceConfig(device_index=device_index)

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
app = QApplication(sys.argv)

# Create a Qt widget, which will be our window.
window = MainWindow()
window.show()  # IMPORTANT!!!!! Windows are hidden by default.

# Start the event loop.
app.exec()

# Your application won't reach here until you exit and the event
# loop has stopped.

# Create a audio file based on test cases in docs for testing the live translation session, and connect it to a function that initializes the audio pipeline and starts the session using the selected input/output devices and language.
# Need to create a manager to concorently run 2 sessions, one for input and one for output, and connect them to the audio pipeline. 