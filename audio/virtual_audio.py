"""VB-Audio checks (guide/distribution_plan.md §4.5).

With the current routing, B's translation is played into Voicemeeter's AUX strip.
If Voicemeeter Banana is closed, that playback most likely still succeeds, so the
candidate silently hears nothing: only a process check can tell."""
import logging
import subprocess
from dataclasses import dataclass

from audio.devices import DeviceInfo, list_input_devices, list_output_devices

logger = logging.getLogger(__name__)

VOICEMEETER_BANANA_EXE = "voicemeeterpro.exe"  # checked in Task Manager > Details, 2026-10-12

# The routing (distribution plan decision 6), as device-name prefixes. Each wire
# has an app end and a WhatsApp end:
#   A (incoming): WhatsApp Speaker -> CABLE Input -> CABLE Output -> app
#   B (outgoing): app -> Voicemeeter AUX Input -> bus B2 only -> Voicemeeter Out B2 -> WhatsApp Microphone
# Not "Voicemeeter Input": that's the Windows default speaker, so it carries all PC sound.
WHATSAPP_INCOMING_DEFAULT = "CABLE Output"
WHATSAPP_OUTGOING_DEFAULT = "Voicemeeter AUX Input"
WHATSAPP_SPEAKER_DEVICE = "CABLE Input"           # "CABLE Input", not the 16-channel "CABLE In 16ch"
WHATSAPP_MICROPHONE_DEVICE = "Voicemeeter Out B2"

VB_CABLE = "VB-Cable"
VOICEMEETER_BANANA = "Voicemeeter Banana"
DOWNLOAD_PAGES = {
    VB_CABLE: "https://vb-audio.com/Cable/index.htm",
    VOICEMEETER_BANANA: "https://vb-audio.com/Voicemeeter/banana.htm",
}


@dataclass(frozen=True)
class RequiredDevice:
    product: str
    is_input: bool
    prefix: str
    used_by: str  # shown in the setup wizard


# The four endpoints the routing needs (setup wizard page 2).
REQUIRED_DEVICES = {
    "app_incoming": RequiredDevice(VB_CABLE, True, WHATSAPP_INCOMING_DEFAULT, "the app listens to the call"),
    "whatsapp_speaker": RequiredDevice(VB_CABLE, False, WHATSAPP_SPEAKER_DEVICE, "WhatsApp's Speaker"),
    "app_outgoing": RequiredDevice(VOICEMEETER_BANANA, False, WHATSAPP_OUTGOING_DEFAULT,
                                   "the app talks into the call"),
    "whatsapp_microphone": RequiredDevice(VOICEMEETER_BANANA, True, WHATSAPP_MICROPHONE_DEVICE,
                                          "WhatsApp's Microphone"),
}


def check_drivers(inputs: list[DeviceInfo] | None = None,
                  outputs: list[DeviceInfo] | None = None) -> dict[str, DeviceInfo | None]:
    """REQUIRED_DEVICES key -> the device found, or None. A product is missing
    when its devices are; call audio.devices.rescan_devices() first to see a
    driver installed since the app started."""
    inputs = list_input_devices() if inputs is None else inputs
    outputs = list_output_devices() if outputs is None else outputs
    return {key: next((d for d in (inputs if req.is_input else outputs) if d.name.startswith(req.prefix)), None)
            for key, req in REQUIRED_DEVICES.items()}


def missing_products(found: dict[str, DeviceInfo | None]) -> list[str]:
    """Products with at least one endpoint missing, in REQUIRED_DEVICES order."""
    missing = []
    for key, req in REQUIRED_DEVICES.items():
        if found.get(key) is None and req.product not in missing:
            missing.append(req.product)
    return missing


def uses_voicemeeter(device_name: str) -> bool:
    return "voicemeeter" in device_name.casefold()


def voicemeeter_running() -> bool:
    """True if Voicemeeter Banana is running. Blocks for ~0.1 s (tasklist), so call
    it off the event loop. If the check itself fails, assume it's running: a
    false warning is worse than none."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {VOICEMEETER_BANANA_EXE}", "/NH"],
            capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Couldn't check whether Voicemeeter is running: %s", exc)
        return True
    return VOICEMEETER_BANANA_EXE.casefold() in out.casefold()
