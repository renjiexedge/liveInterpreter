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


_PLAYER_COUNTERS = (
    "frames_requested", "portaudio_underflows", "empty_buffer_underruns", "silence_dropped_frames",
)


class ChunkStats:
    """Counts audio chunks sent to / received from Gemini for one session and
    logs one DEBUG summary line per interval, instead of one line per chunk.

    Given the session's AudioPlayer, the line also shows its playback side for
    the same window: how much device time the output stream actually pulled
    (under 100% means callbacks were lost, which turns into lag), how far
    behind playback is, underruns split by cause, and silence dropped to
    catch up."""

    def __init__(self, label: str = "", interval_s: float = 5.0, player=None):
        self._label = label
        self._interval_s = interval_s
        self._player = player
        self._window_start = time.monotonic()
        self._sent_chunks = self._sent_bytes = 0
        self._received_chunks = self._received_bytes = 0
        self._player_baseline = self._player_counters()

    def _player_counters(self) -> dict[str, int]:
        if self._player is None:
            return {}
        return {name: getattr(self._player, name) for name in _PLAYER_COUNTERS}

    def _player_summary(self, elapsed_s: float) -> str:
        if self._player is None:
            return ""
        now = self._player_counters()
        delta = {name: now[name] - self._player_baseline.get(name, 0) for name in now}
        self._player_baseline = now
        rate = self._player.sample_rate
        if not rate or elapsed_s <= 0:
            return ""
        pulled_s = delta["frames_requested"] / rate
        return (
            f" | playback: device pulled {pulled_s:.2f}s ({100 * pulled_s / elapsed_s:.1f}%), "
            f"behind {self._player.buffered_ms:.0f} ms, "
            f"PortAudio underflows {delta['portaudio_underflows']}, "
            f"empty-buffer underruns {delta['empty_buffer_underruns']}, "
            f"silence dropped {1000 * delta['silence_dropped_frames'] / rate:.0f} ms"
        )

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
        elapsed_s = time.monotonic() - self._window_start
        playback = self._player_summary(elapsed_s)
        if self._sent_chunks or self._received_chunks:
            logger.debug(
                "%sAudio in the last %.1fs: sent %d chunks (%d bytes), received %d chunks (%d bytes)%s",
                _prefix(self._label),
                elapsed_s,
                self._sent_chunks, self._sent_bytes,
                self._received_chunks, self._received_bytes,
                playback,
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
