import asyncio
import os
from collections.abc import Callable

from dotenv import load_dotenv
from google import genai
from google.genai import types

from audio.player import AudioPlayer
from config.audio_config import AudioDeviceConfig

load_dotenv()  # reads .env in the project root into the process environment

API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL = "gemini-3.5-live-translate-preview" # or "gemini-3.5-live-translate-preview" for preview model
TARGET_LANGUAGE_CODE = "en"  # Set your desired target language code here
RECEIVE_SAMPLE_RATE = 24000  # PCM16 rate Gemini streams translated audio back at

client = genai.Client(api_key=API_KEY)

config = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
    translation_config=types.TranslationConfig(
        target_language_code=TARGET_LANGUAGE_CODE,
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


async def receive_responses(
    session,
    audio_player: AudioPlayer,
    on_transcript: TranscriptCallback | None,
) -> None:
    """Play translated audio through audio_player and report each finished
    input/output transcript pair via on_transcript as {"input": ..., "output": ...}."""
    input_text = ""
    output_text = ""
    async for response in session.receive():
        server_content = response.server_content
        if not server_content:
            continue
        if server_content.input_transcription:
            input_text += server_content.input_transcription.text
        if server_content.output_transcription:
            output_text += server_content.output_transcription.text
        if server_content.model_turn:
            for part in server_content.model_turn.parts:
                if part.inline_data and isinstance(part.inline_data.data, bytes):
                    await asyncio.to_thread(audio_player.write, part.inline_data.data)
        if server_content.turn_complete:
            if (input_text or output_text) and on_transcript is not None:
                on_transcript({"input": input_text, "output": output_text})
            input_text = ""
            output_text = ""


async def run_session(
    audio_queue,
    output_device_config: AudioDeviceConfig | None = None,
    on_transcript: TranscriptCallback | None = None,
) -> None:
    """Open a Live translation session and run sending/receiving concurrently.

    Translated audio is played out through the audio/ package's AudioPlayer,
    targeting output_device_config (defaults to the virtual cable, matching
    AudioDeviceConfig's default). Each finished input/output transcript pair
    is reported via on_transcript as it completes.

    Stops as soon as either side finishes (e.g. the caller stops feeding
    audio, or the server closes the stream), cancelling the other.
    """
    audio_player = AudioPlayer(
        device_config=output_device_config or AudioDeviceConfig(),
        source_rate=RECEIVE_SAMPLE_RATE,
    )
    audio_player.start()
    try:
        async with client.aio.live.connect(model=MODEL, config=config) as session:
            print("Session started with translation")
            send_task = asyncio.create_task(send_audio(session, audio_queue))
            receive_task = asyncio.create_task(
                receive_responses(session, audio_player, on_transcript)
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


async def main():
    audio_queue = asyncio.Queue()

    # Demo: feed one placeholder PCM chunk, then signal end of stream.
    await audio_queue.put(b"\x00\x00\x00\x00\x00\x00\x00\x00")
    await audio_queue.put(None)

    await run_session(audio_queue, on_transcript=print)


if __name__ == "__main__":
    asyncio.run(main())
