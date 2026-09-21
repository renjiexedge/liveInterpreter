
import sys

from PySide6.QtWidgets import (QApplication, QHBoxLayout, QPushButton, QWidget, QMainWindow, QLabel, QLineEdit, QVBoxLayout, QComboBox)
from PySide6.QtCore import Qt, QSize

from audio.devices import DeviceNotFoundError, find_input_device, list_input_devices, list_output_devices
from config.audio_config import AudioDeviceConfig
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
    def __init__(self):
        super().__init__()

        # Set the window title
        self.setWindowTitle("Xedge Live Interpreter")
        # Set the window dimensions (16:10) for a good default size
        self.setMinimumSize(QSize(1120, 700))

        self.label = QLabel()
        self.input = QLineEdit()
        self.input.textChanged.connect(self.label.setText)

        #App header
        header_label_1 = QLabel("Xedge Live Interpreter")
        header_label_1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_label_2 = QLabel("Incoming Language:")
        header_combo = QComboBox()
        # Restrict selection to the dropdown only; user cannot type/edit the contents.
        header_combo.setEditable(False)
        for language_name, language_code in SOUTHEAST_ASIA_LANGUAGES.items():
            header_combo.addItem(language_name, language_code)
        header_combo.setCurrentText("English (Singapore)")
        self.header_combo = header_combo
        header_combo.currentIndexChanged.connect(self.on_language_changed)
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
        try:
            input_combo.setCurrentIndex(input_combo.findData(find_input_device(AudioDeviceConfig()).index))
        except DeviceNotFoundError:
            pass
        self.input_combo = input_combo
        self.output_combo = output_combo

        # Create a horizontal layout for the input/output device selection
        device_layout = QHBoxLayout()
        device_layout.addWidget(input_label)
        device_layout.addWidget(input_combo)
        device_layout.addWidget(output_label)
        device_layout.addWidget(output_combo)

        # Show the initially selected language in the label.
        self.on_language_changed(header_combo.currentIndex())

        #Final layout for the app window.
        layout = QVBoxLayout()
        layout.addLayout(header_layout)
        layout.addLayout(device_layout)
        layout.addWidget(self.label)

        Container = QWidget()
        Container.setLayout(layout)

        # Set the central widget of the Window.
        self.setCentralWidget(Container)

    def on_language_changed(self, index):
        language_name = self.header_combo.itemText(index)
        language_code = self.header_combo.itemData(index)
        self.label.setText(f"{language_name}: {language_code}")

    def selected_input_config(self) -> AudioDeviceConfig:
        """AudioDeviceConfig for the device chosen in the input combo, ready to
        pass to build_audio_pipeline(); AudioRouter resolves it via device_index."""
        return AudioDeviceConfig(device_index=self.input_combo.currentData())

    def the_button_was_clicked(self):
        self.button.setText("You already clicked me.")
        self.button.setEnabled(False)



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

