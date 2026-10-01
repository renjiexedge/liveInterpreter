"""Diagnostic logging for the distorted-output investigation in
engines/gemini_live.py.

Three narrow tools, each aimed at one hypothesis from the distortion
triage:
  - log_mime_type: confirms the rate/encoding Gemini actually reports on
    received audio, vs. the RECEIVE_SAMPLE_RATE assumption.
  - log_interrupted: flags server-side turn interruptions and how much
    queued audio the receive loop flushed for each (open question 6 in
    guide/two_way_plan.md: does flushing drop valid translated speech?).
  - RawAudioDumper: writes received PCM straight to a .wav, bypassing
    resample and device playback entirely, to isolate whether corruption
    is already present in the bytes Gemini sends or introduced downstream.
"""

import logging
import sys
import threading
import wave
from pathlib import Path

logger = logging.getLogger("audio_diag")
logger.setLevel(logging.DEBUG)
logger.propagate = False  # don't double-log through the root logger

if not logger.handlers:
    _handler = logging.StreamHandler(stream=sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    logger.addHandler(_handler)


def _prefix(label: str) -> str:
    return f"[{label}] " if label else ""


def log_mime_type(mime_type: str | None, label: str = "") -> None:
    """Log the mime_type Gemini reports on a received audio part. The caller
    only calls this when it changes, not for every part."""
    logger.info("%sReceived audio part mime_type: %r", _prefix(label), mime_type)


def log_interrupted(flushed_ms: float, label: str = "") -> None:
    """Log a server-side turn interruption and how much queued audio was flushed."""
    logger.warning(
        "%sTurn interrupted by server: flushed %.0f ms of queued translated audio",
        _prefix(label),
        flushed_ms,
    )


class RawAudioDumper:
    """Writes received PCM straight to a .wav file, bypassing resample and
    device playback entirely.

    Create one per session, call write() with each raw chunk as it's
    received, and close() when the session ends. Play the resulting file in
    an external player (Audacity, VLC, ffplay): if it sounds correct there,
    the distortion is introduced downstream, in AudioPlayer's resample/
    device-output step; if it's already garbled, the bytes from Gemini are
    already bad (wrong rate/encoding assumption, or an upstream issue).
    """

    def __init__(
        self,
        path: str | Path,
        sample_rate: int,
        channels: int = 1,
        sample_width: int = 2,
    ):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self._path), "wb")
        self._wav.setnchannels(channels)
        self._wav.setsampwidth(sample_width)
        self._wav.setframerate(sample_rate)
        # write() runs on a worker thread (asyncio.to_thread). A cancelled
        # session can leave one still running when close() is called, so both
        # take the lock, and a write after close is dropped.
        self._lock = threading.Lock()
        self._closed = False
        logger.info("Dumping raw received audio to %s (%d Hz)", self._path, sample_rate)

    def write(self, chunk: bytes) -> None:
        with self._lock:
            if not self._closed:
                self._wav.writeframes(chunk)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._wav.close()
        logger.info("Closed raw audio dump at %s", self._path)
