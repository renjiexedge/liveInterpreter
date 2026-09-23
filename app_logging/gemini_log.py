"""Console logging for engines/gemini_live.py.

Provides a preconfigured logger plus small helpers for the events
gemini_live.py needs visibility into: buffered audio sent to Gemini,
audio received back from Gemini, and completed transcripts.
"""

import logging
import sys

logger = logging.getLogger("gemini_live")
logger.setLevel(logging.DEBUG)
logger.propagate = False  # don't double-log through the root logger

if not logger.handlers:
    _handler = logging.StreamHandler(stream=sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    logger.addHandler(_handler)


def log_audio_sent(chunk: bytes) -> None:
    """Log that a buffered PCM chunk was sent to the Gemini session."""
    logger.debug("Sent audio chunk to Gemini: %d bytes", len(chunk))


def log_audio_received(chunk: bytes) -> None:
    """Log that a translated PCM chunk was received from Gemini and queued for playback."""
    logger.debug("Received audio chunk from Gemini: %d bytes", len(chunk))


def log_transcript(transcript: dict[str, str]) -> None:
    """Log a completed input/output transcript pair."""
    logger.info(
        "Transcript - input: %r | output: %r",
        transcript.get("input", ""),
        transcript.get("output", ""),
    )
