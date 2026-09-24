import asyncio
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from app_logging.audio_log import RawAudioDumper, log_interrupted, log_mime_type
from app_logging.gemini_log import log_audio_received, log_audio_sent, log_transcript
from audio.player import AudioPlayer
from config.audio_config import AudioDeviceConfig

load_dotenv()  # reads .env in the project root into the process environment

API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL = "gemini-3.5-live-translate-preview" # or "gemini-3.5-live-translate-preview" for preview model
DEFAULT_TARGET_LANGUAGE_CODE = "en"  # used when the caller doesn't pick a language
RECEIVE_SAMPLE_RATE = 24000  # PCM16 rate Gemini streams translated audio back at

client = genai.Client(api_key=API_KEY)


def build_live_config(target_language_code: str) -> types.LiveConnectConfig:
    """LiveConnectConfig for a translation session into target_language_code (BCP-47)."""
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(
            target_language_code=target_language_code,
            echo_target_language=True,
        ),
    )

TranscriptCallback = Callable[[dict[str, str]], None]


async def send_audio(session, audio_queue):
    """Stream PCM chunks from audio_queue to Gemini until a None sentinel arrives."""
    while True:
        chunk = await audio_queue.get()
        if chunk is None:
            break
        await session.send_realtime_input(
            audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
        )
        log_audio_sent(chunk)


async def receive_responses(
    session,
    audio_player: AudioPlayer,
    on_transcript: TranscriptCallback | None,
    raw_dumper: RawAudioDumper | None = None,
) -> None:
    """Play translated audio through audio_player and report each finished
    input/output transcript pair via on_transcript as {"input": ..., "output": ...}."""
    input_text = ""
    output_text = ""
    async for response in session.receive():
        server_content = response.server_content
        if not server_content:
            continue
        log_interrupted(server_content.interrupted)
        if server_content.input_transcription:
            # .text can be None on some deltas (e.g. an interim/empty update).
            input_text += server_content.input_transcription.text or ""
        if server_content.output_transcription:
            output_text += server_content.output_transcription.text or ""
        if server_content.model_turn:
            for part in server_content.model_turn.parts:
                if part.inline_data and isinstance(part.inline_data.data, bytes):
                    log_mime_type(part.inline_data.mime_type)
                    log_audio_received(part.inline_data.data)
                    if raw_dumper is not None:
                        raw_dumper.write(part.inline_data.data)
                    await asyncio.to_thread(audio_player.write, part.inline_data.data)
        if server_content.turn_complete:
            if input_text or output_text:
                transcript = {"input": input_text, "output": output_text}
                log_transcript(transcript)
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
) -> None:
    """Open a Live translation session and run sending/receiving concurrently.

    Speech is translated into target_language_code (BCP-47, e.g. "en", "th").
    The language is fixed for the lifetime of the session.

    Translated audio is played out through the audio/ package's AudioPlayer,
    targeting output_device_config (defaults to the virtual cable, matching
    AudioDeviceConfig's default). Each finished input/output transcript pair
    is reported via on_transcript as it completes.

    When dump_raw_audio is True, every received audio chunk is also written,
    unmodified, to debug_dumps/received_<timestamp>.wav -- bypassing
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

    raw_dumper: RawAudioDumper | None = None
    if dump_raw_audio:
        dump_dir = Path("debug_dumps")
        dump_dir.mkdir(exist_ok=True)
        dump_path = dump_dir / f"received_{datetime.now():%Y%m%d_%H%M%S}.wav"
        raw_dumper = RawAudioDumper(dump_path, sample_rate=RECEIVE_SAMPLE_RATE)

    try:
        config = build_live_config(target_language_code)
        async with client.aio.live.connect(model=MODEL, config=config) as session:
            print(f"Session started with translation to {target_language_code!r}")
            send_task = asyncio.create_task(send_audio(session, audio_queue))
            receive_task = asyncio.create_task(
                receive_responses(session, audio_player, on_transcript, raw_dumper)
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
        audio_player.stop()
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
