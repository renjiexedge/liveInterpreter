import logging
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soxr

from app_logging.audio_log import RawAudioDumper
from audio.devices import find_output_device
from config.audio_config import AudioDeviceConfig

logger = logging.getLogger(__name__)

_UNDERRUN_LOG_INTERVAL_S = 5.0
_DRAIN_MARGIN_S = 1.0  # slack added on top of the buffer's own playback time in stop()
_STUTTER_GAP_S = 0.5  # audio resuming within this long of running dry counts as a stutter
# Gemini keeps streaming real-time silence while nobody speaks, so the buffer
# never gets a quiet moment to drain, and any device time lost to a stalled
# callback stays as permanent lag. write() drops silent chunks while playback
# is this far behind, so lag heals during pauses without touching speech.
_SILENCE_DROP_MIN_BUFFERED_MS = 100.0
_SILENCE_PEAK = 64  # int16 peak at or below this (about -54 dBFS) counts as silence
# Spec §11 max_output_queue_ms: past this, write() drops the OLDEST queued audio,
# so playback never runs more than this far behind (latency over completeness).
# The backstop for lag built up during speech; silence dropping handles pauses.
MAX_OUTPUT_QUEUE_MS = 1200.0


class AudioPlayer:
    """Owns the output device and plays incoming mono PCM16 audio through it.

    Mirror of AudioRouter for the output side: AudioRouter pulls hardware
    audio in via a callback on PortAudio's real-time thread and hands it off
    to a JitterBuffer for the async side to drain; AudioPlayer takes an
    engine's output audio from the async side and hands it to a callback on
    PortAudio's real-time thread to drain, via an internal buffer.

    This is deliberately callback-driven rather than using OutputStream's
    blocking .write(): A/B testing against sd.play() (which is
    callback-driven internally) with identical post-resample samples showed
    the blocking-write approach produced persistent low-level static
    independent of buffer size or write chunk size, while the callback
    approach was clean.
    """

    def __init__(
        self,
        device_config: AudioDeviceConfig,
        source_rate: int,
        resample_quality: str = "HQ",
        debug_dump_path: str | Path | None = None,
        max_buffered_ms: float | None = MAX_OUTPUT_QUEUE_MS,
    ):
        self._device_config = device_config
        self._max_buffered_ms = max_buffered_ms
        self._source_rate = source_rate
        self._resample_quality = resample_quality
        self._resampler: soxr.ResampleStream | None = None
        self._stream: sd.OutputStream | None = None
        self._debug_dump_path = debug_dump_path
        self._dumper: RawAudioDumper | None = None

        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._underrun_count = 0
        self._last_underrun_log = 0.0
        self._muted = False
        self._user_muted = False

        # Cumulative counters for the periodic stats line (ChunkStats diffs them).
        # Written by the callback / write(), read from the event loop for logging only.
        self.frames_requested = 0  # frames the device asked for, padding included
        self.portaudio_underflows = 0  # callback ran late; the device played silence
        self.empty_buffer_underruns = 0  # callback ran on time but our buffer was short
        self.silence_dropped_frames = 0  # silent audio write() discarded while behind
        self.overflow_dropped_frames = 0  # oldest audio discarded past max_buffered_ms
        self.flushed_frames = 0  # queued audio discarded by flush() (interrupted turns)

        # Health stats for audio/health.HealthMonitor, written by the callback.
        self.device_name = ""
        self.last_callback_at = 0.0
        self.mid_speech_underruns = 0
        self._had_audio = False
        self._dry_since = 0.0

    @property
    def underrun_count(self) -> int:
        """How many times the callback ran dry (our buffer had no data, or
        PortAudio itself reported an underflow) since start(). This includes
        every idle callback between turns, so it isn't a stutter signal on its
        own -- see mid_speech_underruns. portaudio_underflows and
        empty_buffer_underruns split it by cause."""
        return self._underrun_count

    @property
    def sample_rate(self) -> int:
        """The device's native rate the stream runs at (0 before start())."""
        return int(self._stream.samplerate) if self._stream is not None else 0

    @property
    def max_buffered_ms(self) -> float | None:
        """The queue cap; None means unbounded."""
        return self._max_buffered_ms

    @property
    def buffered_ms(self) -> float:
        """How far playback is behind: audio queued but not yet played."""
        if self._stream is None:
            return 0.0
        with self._buffer_lock:
            remaining = len(self._buffer)
        return remaining / 2 / self._stream.samplerate * 1000  # mono int16

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        """Echo circuit breaker (audio/health.HealthMonitor). While muted, write()
        drops audio. Muting also discards what's queued, so an echo loop is broken
        immediately rather than after the backlog plays."""
        self._muted = value
        if value:
            with self._buffer_lock:
                self._buffer.clear()

    @property
    def user_muted(self) -> bool:
        return self._user_muted

    @user_muted.setter
    def user_muted(self, value: bool) -> None:
        """The staff's per-direction Mute button (spec §4.2). Same effect as
        `muted`, but a separate flag, so the echo breaker's Resume can't unmute
        a direction the user muted. Safe to set from any thread."""
        self._user_muted = value
        if value:
            with self._buffer_lock:
                self._buffer.clear()

    def flush(self) -> int:
        """Discard everything queued (spec §10.2: never replay audio from an
        interrupted response). Returns how many frames were dropped."""
        with self._buffer_lock:
            frames = len(self._buffer) // 2  # mono int16
            self._buffer.clear()
        self.flushed_frames += frames
        return frames

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
            latency="high",
            callback=self._callback,
        )
        self._stream.start()
        self.device_name = device.name
        self.last_callback_at = time.monotonic()  # watchdog grace until the first callback

        if self._debug_dump_path is not None:
            # Captures exactly what gets handed to the device, post-resample
            # -- lets you compare against the pre-resample dump to see
            # whether corruption is introduced by resample_chunk() or by
            # the device write step.
            self._dumper = RawAudioDumper(self._debug_dump_path, sample_rate=native_rate)

        logger.info(
            "AudioPlayer started on %r (%d Hz, resampling from %d Hz: %s)",
            device.name,
            native_rate,
            self._source_rate,
            self._resampler is not None,
        )

    def _callback(self, outdata, frames, time_info, status) -> None:
        """Runs on PortAudio's real-time thread. Pulls exactly `frames`
        samples from the internal buffer, padding with silence if not
        enough has arrived yet rather than blocking."""
        self.frames_requested += frames
        if status.output_underflow:
            self.portaudio_underflows += 1
            self._log_underrun("PortAudio reported an output underflow")

        needed_bytes = frames * 2  # mono int16
        with self._buffer_lock:
            available = len(self._buffer)
            take = min(available, needed_bytes)
            chunk = bytes(self._buffer[:take])
            del self._buffer[:take]

        samples = np.frombuffer(chunk, dtype="<i2")
        outdata[: len(samples), 0] = samples
        if len(samples) < frames:
            outdata[len(samples):, 0] = 0
            self.empty_buffer_underruns += 1
            self._log_underrun(f"internal buffer had {len(samples)}/{frames} frames available")

        # Stutter = audio ran dry and resumed within a moment. The dry spell at
        # the end of every turn doesn't count, because no audio follows it quickly.
        now = time.monotonic()
        self.last_callback_at = now
        has_audio = len(samples) == frames
        if self._had_audio and not has_audio:
            self._dry_since = now
        elif has_audio and not self._had_audio and 0 < now - self._dry_since < _STUTTER_GAP_S:
            self.mid_speech_underruns += 1
        self._had_audio = has_audio

    def _log_underrun(self, reason: str) -> None:
        self._underrun_count += 1
        now = time.monotonic()
        if now - self._last_underrun_log > _UNDERRUN_LOG_INTERVAL_S:
            logger.warning("Output underrun: %s", reason)
            self._last_underrun_log = now

    def write(self, pcm_bytes: bytes) -> None:
        """Resample and append to the internal buffer; the real-time
        callback drains it at its own pace. Non-blocking (aside from a
        brief lock), safe to call from any thread. Dropped while muted, and
        dropped if it's silent while playback is already behind. If the queue
        then holds more than max_buffered_ms, the oldest audio is dropped."""
        if self._muted or self._user_muted:
            return
        samples = np.frombuffer(pcm_bytes, dtype="<i2")
        # Resample before any drop decision, so the resampler's stream state
        # stays continuous.
        if self._resampler is not None:
            samples = self._resampler.resample_chunk(samples)
        if not len(samples):
            return
        if (
            self.buffered_ms > _SILENCE_DROP_MIN_BUFFERED_MS
            and int(np.abs(samples.astype(np.int32)).max()) <= _SILENCE_PEAK
        ):
            self.silence_dropped_frames += len(samples)
            return
        if self._dumper is not None:
            self._dumper.write(samples.tobytes())
        with self._buffer_lock:
            self._buffer.extend(samples.tobytes())
            if self._max_buffered_ms is not None and self._stream is not None:
                max_bytes = int(self._max_buffered_ms / 1000 * self._stream.samplerate) * 2
                excess = len(self._buffer) - max_bytes
                if excess > 0:
                    del self._buffer[:excess]  # even count: stays sample-aligned
                    self.overflow_dropped_frames += excess // 2

    def stop(self) -> None:
        if self._resampler is not None:
            trailing = self._resampler.resample_chunk(np.empty(0, dtype="<i2"), last=True)
            if len(trailing):
                with self._buffer_lock:
                    self._buffer.extend(trailing.tobytes())
            clips = self._resampler.num_clips()
            if clips:
                logger.warning(
                    "Output resampler clipped %d sample(s) this session "
                    "(int16 resample has no headroom -- see audio/resample.py's "
                    "float32 + explicit clip pattern for comparison)",
                    clips,
                )
            self._resampler = None

        if self._stream is not None:
            # Let the callback drain whatever's left so the tail of the
            # audio isn't cut off, but don't wait forever if it can't. The
            # timeout scales with how much is actually buffered (write() no
            # longer blocks, so a caller may have queued more than one
            # chunk's worth ahead of this call) plus a fixed safety margin.
            with self._buffer_lock:
                remaining = len(self._buffer)
            expected_drain_s = remaining / 2 / self._stream.samplerate  # mono int16
            deadline = time.monotonic() + expected_drain_s + _DRAIN_MARGIN_S
            while time.monotonic() < deadline:
                with self._buffer_lock:
                    remaining = len(self._buffer)
                if remaining == 0:
                    break
                time.sleep(0.02)
            else:
                logger.warning("AudioPlayer stop: buffer didn't fully drain before timeout")

            self._stream.stop()
            self._stream.close()
            self._stream = None

        if self._dumper is not None:
            self._dumper.close()
            self._dumper = None

        logger.info(
            "AudioPlayer stopped (%d underrun(s) total: %d PortAudio underflow(s), "
            "%d empty-buffer; dropped frames: %d silent while behind, %d over the "
            "queue cap, %d flushed on interrupt)",
            self._underrun_count,
            self.portaudio_underflows,
            self.empty_buffer_underruns,
            self.silence_dropped_frames,
            self.overflow_dropped_frames,
            self.flushed_frames,
        )
