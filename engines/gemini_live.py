import asyncio
import functools
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from app_logging.audio_log import RawAudioDumper, log_interrupted, log_mime_type
from app_logging.gemini_log import ChunkStats, log_transcript
from audio.player import AudioPlayer
from config.audio_config import AudioDeviceConfig

load_dotenv()  # reads .env in the project root into the process environment

MODEL = "gemini-3.5-live-translate-preview" # or "gemini-3.5-live-translate-preview" for preview model
DEFAULT_TARGET_LANGUAGE_CODE = "en"  # used when the caller doesn't pick a language
RECEIVE_SAMPLE_RATE = 24000  # PCM16 rate Gemini streams translated audio back at


def api_key_present() -> bool:
    """True if GEMINI_API_KEY is set (from .env or the environment). Callers check
    this before Start, so a missing key gets a friendly message, not a crash."""
    return bool(os.environ.get("GEMINI_API_KEY"))


@functools.cache
def get_client() -> genai.Client:
    """The shared client, built on first use rather than at import: genai.Client
    raises without a key, and importing this module shouldn't."""
    return genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))


def build_live_config(target_language_code: str, echo_target_language: bool = True) -> types.LiveConnectConfig:
    """LiveConnectConfig for a translation session into target_language_code (BCP-47).

    echo_target_language=True makes the model voice speech that's already in the
    target language too ("parrot"); False keeps it silent for such speech, which
    also stops a re-captured translation from being played again (see DirectionConfig).
    """
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(
            target_language_code=target_language_code,
            echo_target_language=echo_target_language,
        ),
    )

TranscriptCallback = Callable[[dict[str, str]], None]
# ("input" | "output", text) for every transcription delta, as it arrives.
TranscriptDeltaCallback = Callable[[str, str], None]


async def send_audio(session, audio_queue, chunk_stats: ChunkStats | None = None):
    """Stream PCM chunks from audio_queue to Gemini until a None sentinel arrives."""
    while True:
        chunk = await audio_queue.get()
        if chunk is None:
            break
        await session.send_realtime_input(
            audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
        )
        if chunk_stats is not None:
            chunk_stats.sent(chunk)


async def receive_responses(
    session,
    audio_player: AudioPlayer,
    on_transcript: TranscriptCallback | None,
    raw_dumper: RawAudioDumper | None = None,
    on_transcript_delta: TranscriptDeltaCallback | None = None,
    label: str = "",
    chunk_stats: ChunkStats | None = None,
) -> None:
    """Play translated audio through audio_player and report each finished
    input/output transcript pair via on_transcript as {"input": ..., "output": ...}.
    Each individual delta also goes to on_transcript_delta, so the echo detector
    doesn't have to wait for turn_complete."""
    input_text = ""
    output_text = ""
    last_mime_type = None
    async for response in session.receive():
        server_content = response.server_content
        if not server_content:
            continue
        if server_content.interrupted:
            # Spec §10.2: never replay audio from an interrupted response.
            frames = audio_player.flush()
            rate = audio_player.sample_rate
            log_interrupted(1000 * frames / rate if rate else 0.0, label)
        if server_content.input_transcription:
            # .text can be None on some deltas (e.g. an interim/empty update).
            delta = server_content.input_transcription.text or ""
            input_text += delta
            if delta and on_transcript_delta is not None:
                on_transcript_delta("input", delta)
        if server_content.output_transcription:
            delta = server_content.output_transcription.text or ""
            output_text += delta
            if delta and on_transcript_delta is not None:
                on_transcript_delta("output", delta)
        if server_content.model_turn:
            for part in server_content.model_turn.parts:
                if part.inline_data and isinstance(part.inline_data.data, bytes):
                    if part.inline_data.mime_type != last_mime_type:
                        last_mime_type = part.inline_data.mime_type
                        log_mime_type(last_mime_type, label)
                    if chunk_stats is not None:
                        chunk_stats.received(part.inline_data.data)
                    if raw_dumper is not None:
                        # Disk I/O: keep it off the event loop, which two directions share.
                        await asyncio.to_thread(raw_dumper.write, part.inline_data.data)
                    await asyncio.to_thread(audio_player.write, part.inline_data.data)
        if server_content.turn_complete:
            if input_text or output_text:
                transcript = {"input": input_text, "output": output_text}
                log_transcript(transcript, label)
                if on_transcript is not None:
                    on_transcript(transcript)
            input_text = ""
            output_text = ""


async def run_session(
    audio_queue,
    output_device_config: AudioDeviceConfig | None = None,
    on_transcript: TranscriptCallback | None = None,
    dump_raw_audio: bool = True,
    target_language_code: str = DEFAULT_TARGET_LANGUAGE_CODE,
    echo_target_language: bool = True,
    on_transcript_delta: TranscriptDeltaCallback | None = None,
    on_player_ready: Callable[[AudioPlayer], None] | None = None,
    label: str = "",
    on_connected: Callable[[], None] | None = None,
) -> None:
    """Open a Live translation session and run sending/receiving concurrently.

    Speech is translated into target_language_code (BCP-47, e.g. "en", "th").
    The language is fixed for the lifetime of the session. echo_target_language
    is passed to build_live_config.

    on_transcript_delta receives every transcription delta, and on_player_ready
    receives the AudioPlayer once it's playing; both feed audio/health.HealthMonitor.

    label names the direction ("A"/"B", see config/session_config.py). It
    prefixes this session's log lines and goes into the dump filename, so two
    directions running at once stay distinguishable. on_connected is called
    (on the event loop) once the Live connection is open, so a caller can tell
    "connecting" from "translating".

    Translated audio is played out through the audio/ package's AudioPlayer,
    targeting output_device_config (defaults to the virtual cable, matching
    AudioDeviceConfig's default). Each finished input/output transcript pair
    is reported via on_transcript as it completes.

    When dump_raw_audio is True, every received audio chunk is also written,
    unmodified, to debug_dumps/received_<label>_<timestamp>.wav -- bypassing
    AudioPlayer's resample/device-output step entirely, so the file can be
    checked externally to see whether distortion is already present in the
    bytes Gemini sends.

    Stops as soon as either side finishes (e.g. the caller stops feeding
    audio, or the server closes the stream), cancelling the other.
    """
    audio_player = AudioPlayer(
        device_config=output_device_config or AudioDeviceConfig(),
        source_rate=RECEIVE_SAMPLE_RATE,
    )
    audio_player.start()
    if on_player_ready is not None:
        on_player_ready(audio_player)

    raw_dumper: RawAudioDumper | None = None
    if dump_raw_audio:
        dump_dir = Path("debug_dumps")
        dump_dir.mkdir(exist_ok=True)
        # The label keeps two directions started in the same second from
        # opening the same file.
        dump_name = f"received_{label}_" if label else "received_"
        dump_path = dump_dir / f"{dump_name}{datetime.now():%Y%m%d_%H%M%S}.wav"
        raw_dumper = RawAudioDumper(dump_path, sample_rate=RECEIVE_SAMPLE_RATE)

    chunk_stats = ChunkStats(label, player=audio_player)
    log_prefix = f"[{label}] " if label else ""
    try:
        config = build_live_config(target_language_code, echo_target_language)
        async with get_client().aio.live.connect(model=MODEL, config=config) as session:
            print(f"{log_prefix}Session started with translation to {target_language_code!r}")
            if on_connected is not None:
                on_connected()
            send_task = asyncio.create_task(send_audio(session, audio_queue, chunk_stats))
            receive_task = asyncio.create_task(
                receive_responses(
                    session, audio_player, on_transcript, raw_dumper, on_transcript_delta,
                    label, chunk_stats,
                )
            )

            done, pending = await asyncio.wait(
                {send_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()  # re-raise any exception the task hit
    finally:
        chunk_stats.flush()
        # AudioPlayer.stop() sleep-polls until playback drains (up to ~1 s past
        # the buffered audio). Run it off the loop: with two directions sharing
        # one loop, a synchronous wait here would stall the other direction's
        # send/receive while this one stops.
        await asyncio.to_thread(audio_player.stop)
        if raw_dumper is not None:
            raw_dumper.close()


async def main():
    audio_queue = asyncio.Queue()

    # Demo: feed one placeholder PCM chunk, then signal end of stream.
    await audio_queue.put(b"\x00\x00\x00\x00\x00\x00\x00\x00")
    await audio_queue.put(None)

    await run_session(audio_queue, on_transcript=print)


if __name__ == "__main__":
    asyncio.run(main())
