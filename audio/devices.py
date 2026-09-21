from dataclasses import dataclass

import sounddevice as sd

from config.audio_config import AudioDeviceConfig


class DeviceNotFoundError(RuntimeError):
    pass


@dataclass
class DeviceInfo:
    index: int
    name: str
    default_samplerate: float
    max_input_channels: int


def list_input_devices() -> list[DeviceInfo]:
    devices = []
    for index, entry in enumerate(sd.query_devices()):
        if entry["max_input_channels"] > 0:
            devices.append(
                DeviceInfo(
                    index=index,
                    name=entry["name"],
                    default_samplerate=entry["default_samplerate"],
                    max_input_channels=entry["max_input_channels"],
                )
            )
    return devices


def find_input_device(config: AudioDeviceConfig) -> DeviceInfo:
    devices = list_input_devices()

    if config.device_index is not None:
        for device in devices:
            if device.index == config.device_index:
                return device
        raise DeviceNotFoundError(
            f"No input device with index {config.device_index}. "
            f"Available input devices: {_format_devices(devices)}"
        )

    if config.exact_name is not None:
        for device in devices:
            if device.name == config.exact_name:
                return device
        raise DeviceNotFoundError(
            f"No input device named {config.exact_name!r}. "
            f"Available input devices: {_format_devices(devices)}"
        )

    needle = config.name_substring.lower()
    for device in devices:
        if needle in device.name.lower():
            return device

    raise DeviceNotFoundError(
        f"No input device matching {config.name_substring!r}. "
        f"Available input devices: {_format_devices(devices)}"
    )


def _format_devices(devices: list[DeviceInfo]) -> str:
    return ", ".join(f"[{d.index}] {d.name!r}" for d in devices) or "(none found)"
