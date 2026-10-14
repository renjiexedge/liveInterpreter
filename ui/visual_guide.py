"""Picture guides (guide/distribution_plan.md Phase 7): a screenshot plus
instructions, one step at a time.

GuideDialog is a separate, non-modal window, so testers can follow it while they
work in the installer or WhatsApp. The screenshots live in ui/assets/guide/
(copied from the gitignored guide/images, renamed, width-capped and re-encoded);
GUIDE_IMAGES lists them for the self-test. The SmartScreen guide is separate."""
import html
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout

from audio.virtual_audio import DOWNLOAD_PAGES, VB_CABLE, VOICEMEETER_BANANA
from config.paths import resource_path

GUIDE_DIR = "ui/assets/guide"


@dataclass(frozen=True)
class GuideStep:
    text: str                # rich text; http(s) links open in the browser
    image: str | None = None  # file name in GUIDE_DIR


class ImageView(QLabel):
    """Shows a pixmap scaled to fit (never enlarged), rescaled on resize."""

    def __init__(self):
        super().__init__()
        self._pixmap = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Ignored: the label's size must not follow the pixmap, or every rescale
        # would grow the window.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumSize(200, 120)

    def set_image(self, name: str | None) -> None:
        self._pixmap = QPixmap(str(resource_path(f"{GUIDE_DIR}/{name}"))) if name else QPixmap()
        self.setVisible(not self._pixmap.isNull())
        self._rescale()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap.isNull():
            self.clear()
            return
        ratio = self.devicePixelRatioF()
        target = self.size() * ratio
        pixmap = self._pixmap
        if pixmap.width() > target.width() or pixmap.height() > target.height():
            pixmap = pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
        pixmap.setDevicePixelRatio(ratio)
        self.setPixmap(pixmap)


class GuideDialog(QDialog):
    """Steps with Previous / Next; the last step's Next reads Close. An optional
    side page (`aside`: button text, step) is opened by its own button and left
    with Back, outside the step sequence."""

    def __init__(self, title: str, steps: list[GuideStep], parent=None,
                 aside: tuple[str, GuideStep] | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.resize(960, 760)
        self._steps = steps
        self._aside = aside
        self._index = 0
        self._in_aside = False
        self._counter = QLabel()
        self._image = ImageView()
        self._text = QLabel()
        self._text.setWordWrap(True)
        self._text.setTextFormat(Qt.TextFormat.RichText)
        self._text.setOpenExternalLinks(True)
        self._text.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._aside_button = QPushButton(aside[0] if aside else "")
        self._aside_button.setVisible(aside is not None)
        self._aside_button.clicked.connect(self._toggle_aside)
        self._previous = QPushButton("Previous")
        self._previous.clicked.connect(lambda: self._go(self._index - 1))
        self._next = QPushButton()
        self._next.setDefault(True)
        self._next.clicked.connect(self._next_clicked)
        buttons = QHBoxLayout()
        buttons.addWidget(self._aside_button)
        buttons.addStretch()
        buttons.addWidget(self._previous)
        buttons.addWidget(self._next)
        layout = self._layout = QVBoxLayout(self)
        layout.addWidget(self._counter)
        layout.addWidget(self._image, stretch=1)
        layout.addWidget(self._text)
        layout.addLayout(buttons)
        self._go(0)

    def _go(self, index: int) -> None:
        self._index = max(0, min(index, len(self._steps) - 1))
        self._in_aside = False
        self._show(self._steps[self._index])

    def _show(self, step: GuideStep) -> None:
        many = len(self._steps) > 1 and not self._in_aside
        self._counter.setText(f"<b>Step {self._index + 1} of {len(self._steps)}</b>")
        self._counter.setVisible(many)
        self._image.set_image(step.image)
        # A step without a picture: the text takes its space, from the top.
        self._layout.setStretchFactor(self._text, 0 if step.image else 1)
        self._text.setText(step.text)
        self._previous.setVisible(many)
        self._previous.setEnabled(self._index > 0)
        self._next.setVisible(not self._in_aside)
        self._next.setText("Close" if self._index == len(self._steps) - 1 else "Next")
        if self._aside:
            self._aside_button.setText("Back" if self._in_aside else self._aside[0])

    def _next_clicked(self) -> None:
        if self._index == len(self._steps) - 1:
            self.close()
        else:
            self._go(self._index + 1)

    def _toggle_aside(self) -> None:
        if self._in_aside:
            self._go(self._index)
        else:
            self._in_aside = True
            self._show(self._aside[1])

    def present(self) -> None:
        """Show, or bring back to the front if it's already open."""
        self.show()
        self.raise_()
        self.activateWindow()


def _link(product: str) -> str:
    url = DOWNLOAD_PAGES[product]
    return f'<a href="{url}">{html.escape(url)}</a>'


def drivers_guide(parent=None) -> GuideDialog:
    """Installing VB-Cable and Voicemeeter Banana (setup wizard, drivers page)."""
    steps = [
        GuideStep(f"Open the VB-Cable page: {_link(VB_CABLE)}<br>Under <b>Download and Install VB-CABLE "
                  "Driver Now!</b>, click the orange Windows <b>Download</b> button (the New Package). "
                  "The free cable is all you need: you don't need VB-CABLE A+B or C+D.",
                  "drivers_01_vbcable_download_page.png"),
        GuideStep(f"Open the Voicemeeter Banana page: {_link(VOICEMEETER_BANANA)}<br>Scroll down to "
                  "<b>Voicemeeter (ZIP Package)</b> and click <b>Download</b>.",
                  "drivers_02_banana_download_page.png"),
        GuideStep("Both files are now in your <b>Downloads</b> folder: <b>VBCABLE_Driver_Pack…</b> and "
                  "<b>VoicemeeterSetup_v…</b>. Right-click each one, choose <b>Extract All…</b>, then "
                  "<b>Extract</b>.",
                  "drivers_03_downloads_folder.png"),
        GuideStep("Open the extracted <b>VBCABLE_Driver_Pack…</b> folder. Right-click "
                  "<b>VBCABLE_Setup_x64</b>, choose <b>Run as administrator</b>, and click <b>Yes</b> "
                  "when Windows asks.",
                  "drivers_04_vbcable_setup_file.png"),
        GuideStep("Click <b>Install Driver</b> and wait for it to finish.<br>This picture shows "
                  "<b>Remove Driver</b> because VB-Cable was already installed on that computer. If you "
                  "see it too, VB-Cable is already installed: close the window, don't remove it.",
                  "drivers_05_vbcable_installer.png"),
        GuideStep("Open the extracted <b>VoicemeeterSetup_v…</b> folder. Right-click "
                  "<b>voicemeeterprosetup</b>, choose <b>Run as administrator</b>, and click <b>Yes</b> "
                  "when Windows asks.",
                  "drivers_06_banana_setup_file.png"),
        GuideStep("Click <b>Install</b> and follow the installer to the end.",
                  "drivers_07_banana_installer.png"),
        GuideStep("<b>Restart the computer.</b> Both drivers need a restart before their audio devices work."
                  "<br><br>Then open Xedge Live Interpreter again. Setup continues by itself (or open it "
                  "from the profile menu → <b>Setup wizard…</b>), and this page should find both drivers. "
                  "If Voicemeeter Banana shows as closed, open it from the Start menu and press "
                  "<b>Re-check</b>."),
    ]
    return GuideDialog("Install the virtual audio drivers", steps, parent)


def voicemeeter_guide(parent=None) -> GuideDialog:
    """Routing the AUX strip to B2 only (setup wizard, audio test page). A fresh
    Banana install has B2 off, so the first audio test fails until this is done."""
    step = GuideStep(
        "Open <b>Voicemeeter Banana</b> from the Start menu. The last input strip is "
        "<b>Voicemeeter AUX</b> (renamed <b>ForTranslator</b> in this picture); "
        "<ol><li>On the <b>AUX</b> strip, turn <b>B2 on</b>, and A1, A2, A3 and B1 off.</li>"
        "<li>On <b>every other strip</b>, make sure <b>B2 is off</b>. Anything else on B2 reaches the "
        "candidate untranslated: PC sound, notifications, your own voice.</li>"
        "<li>Leave the rest as it is, then press <b>Run audio test</b> again.</li></ol>"
        "Voicemeeter remembers this, so you only do it once.",
        "voicemeeter_routing.png")
    return GuideDialog("Voicemeeter Banana routing", [step], parent)


def whatsapp_guide(speaker: str, microphone: str, parent=None) -> GuideDialog:
    """WhatsApp's per-call audio devices (Live Interpreter window and setup
    wizard), with a side page on where the menu is."""
    settings = GuideStep(
        "In every WhatsApp call, open the call's audio menu and choose:"
        f"<ul><li>under <b>Microphone</b>: <b>{html.escape(microphone)}</b></li>"
        f"<li>under <b>Speakers</b>: <b>{html.escape(speaker)}</b> </li></ul>"
        "WhatsApp cuts the names short, so match the start of the name. A tick marks the current choice. "
        "WhatsApp forgets these after each call. If Microphone is left on your headset, the candidate "
        "hears you <b>untranslated</b>, and the app can't detect it.",
        "whatsapp_02_audio_menu.png")
    where = GuideStep(
        "During the call, click the small arrow <b>˅</b> next to the <b>microphone</b> button, at the "
        "bottom left of the call window. The menu lists the microphones first, then the speakers.",
        "whatsapp_01_call_window.png")
    return GuideDialog("WhatsApp call settings", [settings], parent,
                       aside=("Where is this menu?", where))


# Every screenshot the guides above use, checked by selftest.py.
GUIDE_IMAGES = tuple(f"{GUIDE_DIR}/{name}" for name in (
    "drivers_01_vbcable_download_page.png", "drivers_02_banana_download_page.png",
    "drivers_03_downloads_folder.png", "drivers_04_vbcable_setup_file.png",
    "drivers_05_vbcable_installer.png", "drivers_06_banana_setup_file.png",
    "drivers_07_banana_installer.png", "voicemeeter_routing.png",
    "whatsapp_01_call_window.png", "whatsapp_02_audio_menu.png",
))
