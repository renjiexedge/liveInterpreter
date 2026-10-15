"""Logging for engines/gemini_live.py.

Provides a logger plus small helpers for the events gemini_live.py needs
visibility into: buffered audio sent to Gemini, audio received back from
Gemini, completed transcripts (lengths only), and token usage (UsageStats,
from LiveServerMessage.usage_metadata).

Every helper takes the session's direction label ("A"/"B"), so two
concurrent directions can be told apart in the console. Per-chunk audio
traffic is summarised by ChunkStats rather than logged line by line: at
~50 chunks/s each way per direction, per-chunk lines would put about 200
prints/s on the event loop once two directions run.
"""

import logging
import time

logger = logging.getLogger("gemini_live")
# DEBUG: ChunkStats lines are audio diagnostics the log file keeps. Output goes
# through the root logger's handlers (app_logging/setup.py configure_logging).
logger.setLevel(logging.DEBUG)


def _prefix(label: str) -> str:
    return f"[{label}] " if label else ""


_PLAYER_COUNTERS = (
    "frames_requested", "portaudio_underflows", "empty_buffer_underruns", "silence_dropped_frames",
    "overflow_dropped_frames", "flushed_frames",
)


class ChunkStats:
    """Counts audio chunks sent to / received from Gemini for one session and
    logs one DEBUG summary line per interval, instead of one line per chunk.

    Given the session's AudioPlayer, the line also shows its playback side for
    the same window: how much device time the output stream actually pulled
    (under 100% means callbacks were lost, which turns into lag), how much is
    queued and how far behind real time playback is, underruns split by cause,
    and audio dropped to keep up (silence while queued, the oldest audio past
    the lag cap, and interrupted turns)."""

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
            f"queued {self._player.buffered_ms:.0f} ms, behind {self._player.lag_ms:.0f} ms, "
            f"PortAudio underflows {delta['portaudio_underflows']}, "
            f"empty-buffer underruns {delta['empty_buffer_underruns']}, "
            f"silence dropped {1000 * delta['silence_dropped_frames'] / rate:.0f} ms, "
            f"over cap dropped {1000 * delta['overflow_dropped_frames'] / rate:.0f} ms, "
            f"flushed on interrupt {1000 * delta['flushed_frames'] / rate:.0f} ms"
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


class _TokenTally:
    """Summed usage_metadata counts: prompt/response/total plus per-modality."""

    def __init__(self):
        self.messages = 0
        self.prompt = self.response = self.total = 0
        self.prompt_by_modality: dict[str, int] = {}
        self.response_by_modality: dict[str, int] = {}

    def add(self, usage) -> None:
        self.messages += 1
        self.prompt += usage.prompt_token_count or 0
        self.response += usage.response_token_count or 0
        self.total += usage.total_token_count or 0
        for details, tally in ((usage.prompt_tokens_details, self.prompt_by_modality),
                               (usage.response_tokens_details, self.response_by_modality)):
            for item in details or []:
                modality = str(getattr(item.modality, "value", item.modality) or "?")
                tally[modality] = tally.get(modality, 0) + (item.token_count or 0)

    def __str__(self) -> str:
        def by_modality(tally: dict[str, int]) -> str:
            return ", ".join(f"{name} {count}" for name, count in tally.items()) or "-"
        return (f"{self.messages} message(s): prompt {self.prompt} ({by_modality(self.prompt_by_modality)}), "
                f"response {self.response} ({by_modality(self.response_by_modality)}), total {self.total}")


class UsageStats:
    """Logs LiveServerMessage.usage_metadata for one direction (Phase 6b: cost per
    call per model). Each message's counts cover only that message, not the
    connection so far (checked 2026-10-06 on gemini-3.5-live-translate-preview:
    the values don't grow), so they're summed. Check this per model: a model
    whose counts only grow is reporting running totals instead.

    Logs a running total at most once per interval (DEBUG), the total per
    connection when it ends, and the session total, which is the §7.6 "tokens
    per call" figure."""

    def __init__(self, label: str = "", model: str = "", interval_s: float = 30.0):
        self._label = label
        self._model = model
        self._interval_s = interval_s
        self._last_log = time.monotonic()
        self._connection = 0
        self._conn = _TokenTally()
        self._session = _TokenTally()

    def connection_started(self) -> None:
        self._connection += 1
        self._conn = _TokenTally()

    def record(self, usage) -> None:
        self._conn.add(usage)
        self._session.add(usage)
        now = time.monotonic()
        if now - self._last_log >= self._interval_s:
            logger.debug("%sUsage so far (connection %d): %s", _prefix(self._label), self._connection, self._conn)
            self._last_log = now

    def connection_ended(self) -> None:
        logger.info("%sUsage (%s, connection %d ended): %s",
                    _prefix(self._label), self._model, self._connection, self._conn)

    def session_ended(self, duration_s: float) -> None:
        logger.info("%sUsage for the session (%s, %.0f s, %d connection(s)): %s",
                    _prefix(self._label), self._model, duration_s, self._connection, self._session)


def log_transcript(transcript: dict[str, str], label: str = "") -> None:
    """Log that an input/output transcript pair completed: character counts only,
    never the text (logs go into bug reports; guide/distribution_plan.md decision 13)."""
    logger.info(
        "%sTranscript - input: %d chars | output: %d chars",
        _prefix(label),
        len(transcript.get("input", "")),
        len(transcript.get("output", "")),
    )
