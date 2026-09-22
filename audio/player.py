import logging

import numpy as np
import sounddevice as sd
import soxr

from audio.devices import find_output_device
from config.audio_config import AudioDeviceConfig

logger = logging.getLogger(__name__)


class AudioPlayer:
    """Owns the output device and plays incoming mono PCM16 audio through it.

    Mirror of AudioRouter for the output side: AudioRouter pulls hardware
    audio in and resamples it to the rate an engine wants to send; AudioPlayer
    takes an engine's output audio at its native rate and resamples it to
    whatever rate the chosen output device actually runs at.
    """

    def __init__(
        self,
        device_config: AudioDeviceConfig,
        source_rate: int,
        resample_quality: str = "HQ",
    ):
        self._device_config = device_config
        self._source_rate = source_rate
        self._resample_quality = resample_quality
        self._resampler: soxr.ResampleStream | None = None
        self._stream: sd.OutputStream | None = None

    def start(self) -> None:
        device = find_output_device(self._device_config)
        native_rate = int(device.default_samplerate)

        if native_rate != self._source_rate:
            self._resampler = soxr.ResampleStream(
                self._source_rate,
                native_rate,
                1,
                dtype="int16",
                quality=self._resample_quality,
            )

        self._stream = sd.OutputStream(
            device=device.index,
            samplerate=native_rate,
            channels=1,
            dtype="int16",
        )
        self._stream.start()
        logger.info(
            "AudioPlayer started on %r (%d Hz, resampling from %d Hz: %s)",
            device.name,
            native_rate,
            self._source_rate,
            self._resampler is not None,
        )

    def write(self, pcm_bytes: bytes) -> None:
        """Blocking write; call via asyncio.to_thread from the async side."""
        samples = np.frombuffer(pcm_bytes, dtype="<i2")
        if self._resampler is not None:
            samples = self._resampler.resample_chunk(samples)
        if len(samples):
            self._stream.write(samples.reshape(-1, 1))

    def stop(self) -> None:
        if self._resampler is not None:
            trailing = self._resampler.resample_chunk(np.empty(0, dtype="<i2"), last=True)
            if len(trailing) and self._stream is not None:
                self._stream.write(trailing.reshape(-1, 1))
            self._resampler = None

        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        logger.info("AudioPlayer stopped")
