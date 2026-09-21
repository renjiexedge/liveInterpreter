import logging
import time

import sounddevice as sd

from audio.buffers import JitterBuffer
from audio.devices import find_input_device
from audio.resample import Resampler
from config.audio_config import AudioDeviceConfig, AudioPipelineConfig

logger = logging.getLogger(__name__)

_OVERFLOW_LOG_INTERVAL_S = 5.0


class AudioRouter:
    """Owns the virtual-cable input device and feeds resampled PCM into a
    JitterBuffer. Runs the capture callback on PortAudio's real-time thread.
    """

    def __init__(
        self,
        device_config: AudioDeviceConfig,
        pipeline_config: AudioPipelineConfig,
        jitter_buffer: JitterBuffer,
    ):
        self._device_config = device_config
        self._pipeline_config = pipeline_config
        self._jitter_buffer = jitter_buffer
        self._resampler: Resampler | None = None
        self._stream: sd.InputStream | None = None
        self._last_overflow_log = 0.0

    def start(self) -> None:
        device = find_input_device(self._device_config)
        native_rate = int(device.default_samplerate)
        channels = min(device.max_input_channels, 2)

        self._resampler = Resampler(
            in_rate=native_rate,
            in_channels=channels,
            out_rate=self._pipeline_config.target_rate,
            quality=self._pipeline_config.resample_quality,
        )

        blocksize = int(native_rate * self._pipeline_config.chunk_ms / 1000)

        self._stream = sd.InputStream(
            device=device.index,
            samplerate=native_rate,
            channels=channels,
            blocksize=blocksize,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()
        logger.info(
            "AudioRouter started on %r (%d Hz, %d ch, %d-frame blocks)",
            device.name,
            native_rate,
            channels,
            blocksize,
        )

    def _callback(self, indata, frames, time_info, status) -> None:
        if status.input_overflow:
            now = time.monotonic()
            if now - self._last_overflow_log > _OVERFLOW_LOG_INTERVAL_S:
                logger.warning("Audio input overflow detected")
                self._last_overflow_log = now

        pcm_bytes = self._resampler.process(indata.copy())
        self._jitter_buffer.put_from_thread(pcm_bytes)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if self._resampler is not None:
            trailing = self._resampler.flush()
            if trailing:
                self._jitter_buffer.put_from_thread(trailing)
            self._resampler = None

        self._jitter_buffer.put_from_thread(None)
        logger.info("AudioRouter stopped")
