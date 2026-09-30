"""Console logging for engines/gemini_live.py.

Provides a preconfigured logger plus small helpers for the events
gemini_live.py needs visibility into: buffered audio sent to Gemini,
audio received back from Gemini, and completed transcripts.

Every helper takes the session's direction label ("A"/"B"), so two
concurrent directions can be told apart in the console. Per-chunk audio
traffic is summarised by ChunkStats rather than logged line by line: at
~50 chunks/s each way per direction, per-chunk lines would put about 200
prints/s on the event loop once two directions run.
"""

import logging
import sys
import time

logger = logging.getLogger("gemini_live")
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


class ChunkStats:
    """Counts audio chunks sent to / received from Gemini for one session and
    logs one DEBUG summary line per interval, instead of one line per chunk."""

    def __init__(self, label: str = "", interval_s: float = 5.0):
        self._label = label
        self._interval_s = interval_s
        self._window_start = time.monotonic()
        self._sent_chunks = self._sent_bytes = 0
        self._received_chunks = self._received_bytes = 0

    def sent(self, chunk: bytes) -> None:
        """Record a buffered PCM chunk sent to the Gemini session."""
        self._sent_chunks += 1
        self._sent_bytes += len(chunk)
        self._maybe_log()

    def received(self, chunk: bytes) -> None:
        """Record a translated PCM chunk received from Gemini and queued for playback."""
        self._received_chunks += 1
        self._received_bytes += len(chunk)
        self._maybe_log()

    def flush(self) -> None:
        """Log whatever the current window has counted (e.g. at session end)."""
        if self._sent_chunks or self._received_chunks:
            logger.debug(
                "%sAudio in the last %.1fs: sent %d chunks (%d bytes), received %d chunks (%d bytes)",
                _prefix(self._label),
                time.monotonic() - self._window_start,
                self._sent_chunks, self._sent_bytes,
                self._received_chunks, self._received_bytes,
            )
        self._window_start = time.monotonic()
        self._sent_chunks = self._sent_bytes = 0
        self._received_chunks = self._received_bytes = 0

    def _maybe_log(self) -> None:
        if time.monotonic() - self._window_start >= self._interval_s:
            self.flush()


def log_transcript(transcript: dict[str, str], label: str = "") -> None:
    """Log a completed input/output transcript pair."""
    logger.info(
        "%sTranscript - input: %r | output: %r",
        _prefix(label),
        transcript.get("input", ""),
        transcript.get("output", ""),
    )
