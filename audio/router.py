import logging
import threading
import time

import numpy as np
import sounddevice as sd

from audio.buffers import JitterBuffer
from audio.devices import find_input_device
from audio.resample import Resampler
from config.audio_config import AudioDeviceConfig, AudioPipelineConfig
from config.health_config import HealthConfig

logger = logging.getLogger(__name__)

_OVERFLOW_LOG_INTERVAL_S = 5.0
_CLIP_LEVEL = HealthConfig.clip_level


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
        # start()/stop() run on worker threads (asyncio.to_thread), and a session
        # shutdown can call stop() while the direction's own cleanup does too, or
        # while start() is still opening the device. The lock serialises them.
        self._lifecycle_lock = threading.Lock()

        # Health stats for audio/health.HealthMonitor. Written by the callback as
        # plain int/float stores (no locking needed), read from the session loop.
        self.device_name = ""
        self.last_callback_at = 0.0
        self.block_count = 0
        self.clipped_blocks = 0
        self.zero_run_blocks = 0  # consecutive blocks of pure digital silence

    @property
    def zero_run_s(self) -> float:
        return self.zero_run_blocks * self._pipeline_config.chunk_ms / 1000

    def start(self) -> None:
        with self._lifecycle_lock:
            self._start()

    def _start(self) -> None:
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
        self.device_name = device.name
        self.last_callback_at = time.monotonic()  # watchdog grace until the first callback
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

        self.last_callback_at = time.monotonic()
        peak = float(np.abs(indata).max()) if frames else 0.0
        self.block_count += 1
        if peak >= _CLIP_LEVEL:
            self.clipped_blocks += 1
        self.zero_run_blocks = self.zero_run_blocks + 1 if peak == 0.0 else 0

        pcm_bytes = self._resampler.process(indata.copy())
        self._jitter_buffer.put_from_thread(pcm_bytes)

    def stop(self) -> None:
        """Idempotent and thread-safe. Always feeds the end-of-stream sentinel."""
        with self._lifecycle_lock:
            self._stop()

    def _stop(self) -> None:
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
